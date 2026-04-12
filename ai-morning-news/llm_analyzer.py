#!/usr/bin/env python3
"""
LLM 分析模块 - 支持 OpenAI 兼容 API 和 Anthropic 原生 API。

支持两种 provider：
  1. "openai"（默认）: OpenAI 兼容 API（/v1/chat/completions）
     - 适用于 OpenAI、本地代理、OpenClaw 等
  2. "anthropic": Anthropic 原生 API（/v1/messages）
     - 适用于 Claude Sonnet/Opus，需 Anthropic API Key

用法：
    # OpenAI 兼容
    analyzer = LLMAnalyzer(
        base_url="https://api.openai.com/v1",
        api_key="sk-...",
        model="gpt-4o-mini",
    )
    # Anthropic 原生
    analyzer = LLMAnalyzer(
        provider="anthropic",
        base_url="https://api.anthropic.com",
        api_key="sk-ant-...",
        model="claude-sonnet-4-20250514",
    )
    result = analyzer.analyze_article(title, summary, full_text, source_name)
"""

import hashlib
import json
import sqlite3
import ssl
import urllib.request
import urllib.error
import concurrent.futures
import time
import sys
import re
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from logger import get_logger
log = get_logger('llm_analyzer')


# ---------------------------------------------------------------------------
# Prompt 模板
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是 AI 行业分析师。将新闻转化为结构化 JSON 摘要。

规则：直接输出 JSON，第一个字符必须是{，最后必须是}。不要输出代码块、解释或其他文字。所有内容用中文。

如果文章与 AI/ML/大模型/深度学习无关，返回：{"ai_relevant":false}

如果相关，返回以下格式（所有字段必填，不得留空）：
{"ai_relevant":true,"chinese_title":"中文标题，15-25字","summary":"一句话概要，50字以内","why_it_matters":"这意味着什么，50字以内","key_details":["要点1(40字内)","要点2(40字内)","要点3(40字内)"],"detailed_content":"深度解读，支持Markdown，见下方说明","background":"背景脉络，150字以内","deep_analysis":"深层分析与影响判断，150字以内","importance":3,"categories":["分类"],"source_type":"news"}

字段说明：
- chinese_title：中文新闻标题，简洁有力，15-25字。例如"OpenAI发布GPT-5：数学推理大幅提升"
- summary：客观陈述事实，如"OpenAI发布GPT-5，数学推理提升18%"
- why_it_matters：像给朋友讲新闻，说清楚"所以呢"
- key_details：3个核心要点，每条40字以内
- detailed_content：★最重要的字段★ 600-1500字的深度解读文章。要求：
  · 用 Markdown 格式组织内容：### 小标题分段、**加粗**关键概念、- 列表整理要点
  · **严禁**使用"问题背景/背景/前言/引言/简介"作为首个小标题。直接以"核心发现/事件细节/技术方案/关键数据/核心问题"等实质性小标题开篇
  · 不要复述 background 字段内容——background 是专门写行业历史脉络的字段，detailed_content 聚焦事件本身
  · 如有量化数据/对比，用 Markdown 表格呈现（| 列1 | 列2 |）
  · 像给同行写一篇简报：先讲核心发现/事件细节，再讲技术方案或具体数据，最后讲实际影响
  · 保留原文中的具体数据、人物、机构、技术细节，不要泛泛而谈
  · 不要重复 summary 和 key_details 的原文
- background：独立的行业背景字段，150字以内，讲此事件之前的行业脉络、历史沿革、相关玩家（与 detailed_content 分工明确：detailed_content 讲事件本身发生了什么，background 讲事件之外的时代背景）
- deep_analysis：你的独立判断——这件事的深层意义、潜在风险、对行业格局的影响，150字以内
- importance：1-5分（5=行业格局级，4=显著进展，3=值得关注，2=一般，1=低价值）
- categories：1-2个标签，选自：大模型发布|开源生态|AI政策监管|芯片与算力|产品与应用|安全与对齐|融资与商业|学术研究|AI工具|具身智能|自动驾驶|AI编程|行业观点
- source_type：paper|news|official|opinion|community|video

重要：只要 ai_relevant=true，所有字段都必须认真填写，不允许留空字符串。即使文章较短，也要基于已有信息给出 background 和 deep_analysis。"""

DIGEST_SYSTEM_PROMPT = """你是 AI 行业主编。从今天的新闻摘要中提炼编辑导语。

直接返回 JSON，不要代码块：{"editorial":"150字以内的编辑导语"}

要求：点出今天主旋律，串联不同新闻的关联，语言简洁有力。"""

DIGEST_USER_TEMPLATE = """以下是今天的 {count} 条 AI 新闻摘要，请提炼今日速览：

{summaries}"""

USER_PROMPT_TEMPLATE = """分析以下文章：

【标题】{title}

【来源】{source_name}

【摘要】{summary}

【正文】{full_text}"""


# ---------------------------------------------------------------------------
# JSON Schema 定义（用于 --json-schema 强制有效 JSON 输出）
# ---------------------------------------------------------------------------

ARTICLE_SCHEMA = {
    "type": "object",
    "properties": {
        "ai_relevant": {"type": "boolean"},
        "chinese_title": {"type": "string"},
        "summary": {"type": "string"},
        "why_it_matters": {"type": "string"},
        "key_details": {
            "type": "array",
            "items": {"type": "string"}
        },
        "background": {"type": "string"},
        "detailed_content": {"type": "string"},
        "deep_analysis": {"type": "string"},
        "importance": {"type": "integer"},
        "categories": {
            "type": "array",
            "items": {"type": "string"}
        },
        "source_type": {"type": "string"}
    },
    "required": ["ai_relevant"],
    "additionalProperties": False
}

DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "editorial": {"type": "string"}
    },
    "required": ["editorial"],
    "additionalProperties": False
}


# ---------------------------------------------------------------------------
# LLM 结果缓存（SQLite，跨运行持久化）
# ---------------------------------------------------------------------------

class LLMCache:
    """LLM 分析结果的跨运行缓存。

    以 URL hash 为 key，缓存完整的 LLM 分析结果 JSON。
    TTL 默认 7 天，过期自动清理。

    用法:
        cache = LLMCache("llm_cache.db")
        result = cache.get(url)
        if result is None:
            result = analyzer.analyze_article(...)
            cache.set(url, result)
        cache.close()
    """

    def __init__(self, db_path: str, ttl_days: int = 7):
        self.ttl_days = ttl_days
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS llm_cache (
                url_hash TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self.db.commit()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _hash(url: str) -> str:
        normalized = url.strip().rstrip('/').lower()
        normalized = re.sub(r'^https?://(www\.)?', '', normalized)
        normalized = re.sub(r'[?#].*$', '', normalized)
        return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:32]

    def get(self, url: str) -> Optional[dict]:
        """查找缓存。返回 None 表示 miss。"""
        if not url:
            self._misses += 1
            return None
        h = self._hash(url)
        row = self.db.execute(
            "SELECT result_json, created_at FROM llm_cache WHERE url_hash = ?", (h,)
        ).fetchone()
        if row is None:
            self._misses += 1
            return None
        # 检查 TTL
        try:
            created = datetime.fromisoformat(row[1])
            age_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
            if age_days > self.ttl_days:
                self.db.execute("DELETE FROM llm_cache WHERE url_hash = ?", (h,))
                self.db.commit()
                self._misses += 1
                return None
        except (ValueError, TypeError):
            pass
        self._hits += 1
        return json.loads(row[0])

    def set(self, url: str, result: dict):
        """写入缓存。"""
        if not url or not result:
            return
        h = self._hash(url)
        self.db.execute(
            "INSERT OR REPLACE INTO llm_cache (url_hash, result_json, created_at) VALUES (?, ?, ?)",
            (h, json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat())
        )
        self.db.commit()

    def cleanup(self):
        """清理过期条目。"""
        cutoff = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "DELETE FROM llm_cache WHERE created_at < datetime(?, ?)",
            (cutoff, f'-{self.ttl_days} days')
        )
        self.db.commit()

    @property
    def stats(self) -> str:
        return f"hits={self._hits}, misses={self._misses}"

    def close(self):
        self.db.close()


# ---------------------------------------------------------------------------
# LLM 客户端
# ---------------------------------------------------------------------------

class LLMAnalyzer:
    """支持 OpenAI 兼容 API 和 Anthropic 原生 API 的 LLM 分析器。"""

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        model: str = "gpt-4o-mini",
        provider: str = "openai",        # "openai" | "anthropic"
        auth_type: str = "bearer",       # "bearer" | "custom"（仅 openai provider）
        auth_header: str = "Authorization",
        auth_prefix: str = "Bearer",
        max_retries: int = 3,
        timeout: int = 60,
        max_workers: int = 4,
        temperature: float = 0.3,
        max_tokens: int = 2000,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.provider = provider.lower()  # "openai" or "anthropic"
        self.auth_type = auth_type
        self.auth_header = auth_header
        self.auth_prefix = auth_prefix
        self.max_retries = max_retries
        self.timeout = timeout
        self.max_workers = max_workers
        self.temperature = temperature
        self.max_tokens = max_tokens

        # SSL context（某些服务需要跳过验证）
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    # ------------------------------------------------------------------
    # 底层 API 调用
    # ------------------------------------------------------------------

    def _build_headers(self) -> dict:
        """构建请求头，支持 OpenAI 和 Anthropic 两种格式。"""
        headers = {"Content-Type": "application/json"}

        if self.provider == "anthropic":
            # Anthropic 原生 API
            if self.api_key:
                headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            # OpenAI 兼容 API
            if self.api_key:
                if self.auth_type == "bearer":
                    headers["Authorization"] = f"Bearer {self.api_key}"
                elif self.auth_type == "custom":
                    value = f"{self.auth_prefix} {self.api_key}" if self.auth_prefix else self.api_key
                    headers[self.auth_header] = value
        return headers

    def _call_api(self, messages: List[Dict[str, str]]) -> str:
        """调用 LLM API，自动适配 OpenAI / Anthropic 格式，带重试。"""

        if self.provider == "anthropic":
            url, payload = self._build_anthropic_request(messages)
        else:
            url, payload = self._build_openai_request(messages)

        headers = self._build_headers()

        last_error = None
        for attempt in range(self.max_retries):
            try:
                req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                opener = urllib.request.build_opener(
                    urllib.request.HTTPSHandler(context=self._ssl_ctx)
                )
                with opener.open(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    return self._extract_response_text(body)

            except urllib.error.HTTPError as e:
                last_error = e
                error_body = ""
                try:
                    error_body = e.read().decode("utf-8", errors="replace")[:500]
                except:
                    pass
                # 429 / 5xx 可重试；Anthropic 529 (overloaded) 也重试
                retryable = {429, 500, 502, 503, 504, 529}
                if e.code in retryable and attempt < self.max_retries - 1:
                    wait = min(2 ** attempt * 2, 30)
                    log.warning("⏳ HTTP %d, %ds 后重试... (%s)", e.code, wait, error_body[:100])
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"HTTP {e.code}: {error_body}")

            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    wait = min(2 ** attempt * 2, 30)
                    log.warning("⏳ 网络错误, %ds 后重试... (%s)", wait, e)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"网络错误: {e}")

        raise RuntimeError(f"重试 {self.max_retries} 次后仍失败: {last_error}")

    def _build_openai_request(self, messages: List[Dict[str, str]]) -> tuple:
        """构建 OpenAI 兼容 API 请求。"""
        url = f"{self.base_url}/chat/completions"
        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }).encode("utf-8")
        return url, payload

    def _build_anthropic_request(self, messages: List[Dict[str, str]]) -> tuple:
        """构建 Anthropic Messages API 请求。

        Anthropic 格式要求：
        - system 是顶层字段，不在 messages 中
        - messages 只包含 user/assistant 角色
        """
        url = f"{self.base_url}/v1/messages"

        # 分离 system prompt 和对话消息
        system_text = ""
        user_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_text += msg["content"] + "\n"
            else:
                user_messages.append(msg)

        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": user_messages,
        }
        if system_text.strip():
            body["system"] = system_text.strip()

        payload = json.dumps(body).encode("utf-8")
        return url, payload

    def _extract_response_text(self, body: dict) -> str:
        """从 API 响应中提取文本，适配两种格式。"""
        if self.provider == "anthropic":
            # Anthropic: {"content": [{"type": "text", "text": "..."}]}
            content = body.get("content", [])
            texts = [block["text"] for block in content if block.get("type") == "text"]
            return "\n".join(texts)
        else:
            # OpenAI: {"choices": [{"message": {"content": "..."}}]}
            return body["choices"][0]["message"]["content"]

    # ------------------------------------------------------------------
    # 响应解析
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_json(text: str) -> dict:
        """从 LLM 响应中提取 JSON，兼容各种包裹和截断情况。"""
        text = text.strip()

        # 去掉所有 markdown 代码块标记
        text = re.sub(r'```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```', '', text)
        text = text.strip()

        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 找到第一个 {
        brace_start = text.find('{')
        if brace_start == -1:
            return {}

        # 取从第一个 { 到末尾的所有内容
        fragment = text[brace_start:]

        # 先找最后一个 }，尝试完整解析
        brace_end = fragment.rfind('}')
        if brace_end > 0:
            try:
                return json.loads(fragment[:brace_end + 1])
            except json.JSONDecodeError:
                pass

        # JSON 被截断了，尝试修复
        # 策略：逐步裁剪尾部残缺内容，然后补齐括号
        truncated = fragment.rstrip()
        for attempt in range(20):
            working = truncated

            # 关闭未闭合的字符串
            if working.count('"') % 2 == 1:
                working += '"'

            # 移除尾部不完整的键值对（多种模式）
            # 模式1: ..."key": "incomplete value
            working = re.sub(r',\s*"[^"]*"\s*:\s*"[^"]*$', '', working)
            # 模式2: ..."key": [incomplete array
            working = re.sub(r',\s*"[^"]*"\s*:\s*\[[^\]]*$', '', working)
            # 模式3: ..."key":
            working = re.sub(r',\s*"[^"]*"\s*:?\s*$', '', working)
            # 模式4: ,"incomplete
            working = re.sub(r',\s*"[^"]*$', '', working)
            # 模式5: 数组中的不完整字符串 ..."item
            working = re.sub(r',\s*"[^"]*$', '', working)
            working = working.rstrip(', \n\r\t')

            # 补齐缺少的闭合括号
            open_braces = working.count('{') - working.count('}')
            open_brackets = working.count('[') - working.count(']')
            fixed = working + ']' * max(0, open_brackets) + '}' * max(0, open_braces)

            try:
                result = json.loads(fixed)
                # 只要有 summary 就认为有效（即使其他字段被截断丢失）
                if result.get("ai_relevant") is not None or result.get("summary"):
                    return result
            except json.JSONDecodeError:
                pass

            # 更激进地裁剪：找最后一个逗号
            last_comma = truncated.rfind(',')
            if last_comma > 0:
                truncated = truncated[:last_comma]
            else:
                break

        return {}

    @staticmethod
    def _validate_result(data: dict) -> dict:
        """校验和修正 LLM 返回的结构。"""
        # 检查 AI 相关性
        if not data.get("ai_relevant", True):
            return {"ai_relevant": False}

        result = {
            "ai_relevant": True,
            "chinese_title": str(data.get("chinese_title") or "").strip()[:40],
            "summary": str(data.get("summary") or "")[:150],
            "why_it_matters": str(data.get("why_it_matters", ""))[:200],
            "key_details": [],
            "detailed_content": str(data.get("detailed_content", ""))[:3000],
            "background": str(data.get("background", ""))[:600],
            "deep_analysis": str(data.get("deep_analysis", ""))[:600],
            "importance": 1,
            "categories": [],
            "source_type": str(data.get("source_type", "news")),
            "reading_minutes": 1,
        }

        # importance
        try:
            imp = int(data.get("importance", 1))
            result["importance"] = max(1, min(5, imp))
        except (TypeError, ValueError):
            result["importance"] = 1

        # reading_minutes
        try:
            rm = int(data.get("reading_minutes", 1))
            result["reading_minutes"] = max(1, min(30, rm))
        except (TypeError, ValueError):
            result["reading_minutes"] = 1

        # key_details
        raw_details = data.get("key_details", [])
        if isinstance(raw_details, list):
            for d in raw_details[:5]:
                if isinstance(d, str) and d.strip():
                    result["key_details"].append(d.strip()[:80])
                elif isinstance(d, dict):
                    result["key_details"].append(str(d.get("text", ""))[:80])

        # categories
        raw_cats = data.get("categories", ["其他"])
        if isinstance(raw_cats, list):
            result["categories"] = [str(c) for c in raw_cats[:2]]
        else:
            result["categories"] = ["其他"]

        # source_type 校验
        valid_types = {"paper", "news", "official", "opinion", "community", "video"}
        if result["source_type"] not in valid_types:
            result["source_type"] = "news"

        # causal_events — 因果事件类型
        raw_causal = data.get("causal_events", [])
        if isinstance(raw_causal, list):
            result["causal_events"] = [str(e) for e in raw_causal[:3] if isinstance(e, str) and "." in e]
        else:
            result["causal_events"] = []

        # affected_assets — 受影响资产
        raw_assets = data.get("affected_assets", [])
        if isinstance(raw_assets, list):
            result["affected_assets"] = [str(a) for a in raw_assets[:5] if isinstance(a, str) and "." in a]
        else:
            result["affected_assets"] = []

        # impact_direction
        raw_dir = str(data.get("impact_direction", "neutral"))
        valid_dirs = {"positive", "negative", "mixed", "neutral"}
        result["impact_direction"] = raw_dir if raw_dir in valid_dirs else "neutral"

        # impact_confidence
        raw_conf = str(data.get("impact_confidence", "low"))
        valid_confs = {"high", "medium", "low"}
        result["impact_confidence"] = raw_conf if raw_conf in valid_confs else "low"

        return result

    # ------------------------------------------------------------------
    # 核心分析方法
    # ------------------------------------------------------------------

    def analyze_article(
        self,
        title: str,
        summary: str = "",
        full_text: str = "",
        source_name: str = "",
    ) -> dict:
        """
        分析单篇文章，返回 Toulmin 结构化数据（中文）。

        Returns:
            dict with keys: claim, grounds, warrant, confidence,
                           rebuttal, categories, source_type
        """
        # 截断过长文本，节省 token
        if full_text and len(full_text) > 3000:
            full_text = full_text[:3000] + "…（已截断）"
        if summary and len(summary) > 800:
            summary = summary[:800] + "…"

        user_msg = USER_PROMPT_TEMPLATE.format(
            title=title or "无标题",
            source_name=source_name or "未知",
            summary=summary or "无摘要",
            full_text=full_text or "无正文",
        )

        try:
            response = self._call_api(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
            )
            data = self._extract_json(response)
            need_retry = False

            if not data:
                need_retry = True
                log.warning("⚠️ JSON 解析失败，重试中... (原始: %s)", response[:100])
            elif not data.get("summary") and data.get("ai_relevant", True):
                # JSON 解析成功但缺少 summary（截断导致关键字段丢失）
                need_retry = True
                log.warning("⚠️ 缺少 summary 字段，重试中... (keys: %s)", list(data.keys()))

            if need_retry:
                summary_hint = summary[:80] if summary else ""
                retry_prompt = f'分析以下新闻并返回JSON。标题：{title}\n摘要：{summary_hint}\n\n直接返回JSON，第一个字符必须是{{。与AI相关返回{{"ai_relevant":true,"chinese_title":"中文标题15-25字","summary":"一句话概要50字","why_it_matters":"意义50字","key_details":["要点1","要点2","要点3"],"detailed_content":"按提纲展开详述400-800字","background":"背景80字","deep_analysis":"深度分析80字","importance":3,"categories":["分类"],"source_type":"news"}}，无关返回{{"ai_relevant":false}}'
                response2 = self._call_api(
                    [{"role": "user", "content": retry_prompt}],
                )
                data2 = self._extract_json(response2)
                if data2 and (data2.get("summary") or not data2.get("ai_relevant", True)):
                    data = data2
                elif not data:
                    # 两次都完全失败
                    log.warning("⚠️ 重试仍失败: %s", response2[:100])
                    return self._fallback(title)
                else:
                    # 重试仍没拿到 summary —— drop 这篇，不要把只有 ai_relevant
                    # 的空壳混进正文，否则卡片会是 summary/background/deep_analysis
                    # 全空的"僵尸记录"。让上游按 ai_relevant=False 过滤掉。
                    log.warning(
                        "⚠️ 重试后仍缺 summary，drop 该文章: %s",
                        (title or "")[:40],
                    )
                    return {"ai_relevant": False}

            # 最终校验：ai_relevant=true 时 summary 必须有实质内容，否则视为 LLM 失败
            if data.get("ai_relevant", True) and not (data.get("summary") or "").strip():
                log.warning(
                    "⚠️ 最终结果仍缺 summary，drop 该文章: %s",
                    (title or "")[:40],
                )
                return {"ai_relevant": False}

            return self._validate_result(data)

        except RuntimeError as e:
            log.error("❌ LLM 分析失败 [%s]: %s", title[:30], e)
            return self._fallback(title)

    @staticmethod
    def _fallback(title: str) -> dict:
        """LLM 调用失败时的兜底结果。"""
        return {
            "ai_relevant": True,
            "chinese_title": "",
            "summary": title[:100] if title else "无法获取分析",
            "why_it_matters": "",
            "key_details": [],
            "detailed_content": "",
            "background": "",
            "deep_analysis": "",
            "importance": 1,
            "categories": ["其他"],
            "source_type": "news",
            "reading_minutes": 1,
        }

    # ------------------------------------------------------------------
    # 今日速览（全局综合）
    # ------------------------------------------------------------------

    def generate_digest(self, analyses: List[dict]) -> dict:
        """
        从所有文章分析中生成"今日 3 分钟速览"。

        Args:
            analyses: 带 analysis 字段的文章列表

        Returns:
            dict with keys: editorial, top_stories
        """
        # 构建摘要列表供 LLM 综合
        summaries_text = ""
        for i, item in enumerate(analyses):
            a = item.get("analysis", {})
            if not a.get("ai_relevant", True):
                continue
            summary = a.get("summary", item.get("title", ""))
            importance = a.get("importance", 1)
            source = item.get("source_name", "")
            summaries_text += f"[{i}] ({source}, 重要性{importance}) {summary}\n"

        if not summaries_text.strip():
            return {"editorial": "今天暂无重要 AI 新闻。", "top_stories": []}

        user_msg = DIGEST_USER_TEMPLATE.format(
            count=len([a for a in analyses if a.get("analysis", {}).get("ai_relevant", True)]),
            summaries=summaries_text,
        )

        try:
            response = self._call_api(
                [
                    {"role": "system", "content": DIGEST_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
            )
            data = self._extract_json(response)
            if not data:
                return {"editorial": "速览生成失败。", "top_stories": []}

            # 校验
            result = {
                "editorial": str(data.get("editorial", ""))[:300],
                "top_stories": [],
            }
            for s in data.get("top_stories", [])[:5]:
                if isinstance(s, dict):
                    result["top_stories"].append({
                        "index": int(s.get("index", 0)),
                        "headline": str(s.get("headline", ""))[:30],
                        "why": str(s.get("why", ""))[:80],
                    })
            return result

        except Exception as e:
            log.error("❌ 速览生成失败: %s", e)
            return {"editorial": "速览生成失败。", "top_stories": []}

    # ------------------------------------------------------------------
    # 批量分析
    # ------------------------------------------------------------------

    def batch_analyze(
        self,
        articles: List[dict],
        show_progress: bool = True,
        skip_indices: Optional[set] = None,
        on_complete: Optional[callable] = None,
        cache: Optional['LLMCache'] = None,
    ) -> List[dict]:
        """
        批量分析文章列表，支持断点续跑和跨运行缓存。

        Args:
            articles: 每个 dict 需含 title, summary, full_text, source_name
            show_progress: 是否打印进度
            skip_indices: 已完成分析的文章索引集合（从 checkpoint 恢复时使用）
            on_complete: 每完成一篇调用的回调 fn(idx, result)，用于增量保存
            cache: LLMCache 实例，用于跨运行缓存 LLM 结果

        Returns:
            与 articles 等长的分析结果列表
        """
        total = len(articles)
        if total == 0:
            return []

        skip_indices = skip_indices or set()

        # 预填充已完成的结果
        results = [None] * total
        for idx in skip_indices:
            if idx < total:
                results[idx] = articles[idx].get('analysis', self._fallback(articles[idx].get("title", "")))

        # 从缓存中恢复已有结果
        cache_restored = 0
        if cache:
            for i in range(total):
                if i in skip_indices:
                    continue
                url = articles[i].get('link', '')
                cached = cache.get(url)
                if cached:
                    results[i] = cached
                    skip_indices = skip_indices | {i}
                    cache_restored += 1
            if cache_restored:
                log.info("💾 LLM 缓存命中 %d 篇（跳过重复分析）", cache_restored)

        todo_indices = [i for i in range(total) if i not in skip_indices]
        skipped = total - len(todo_indices)

        log.info("🧠 开始 LLM 分析（共 %d 篇，跳过 %d，待分析 %d，并发 %d）...", total, skipped, len(todo_indices), self.max_workers)

        completed = [0]

        def _worker(idx: int) -> tuple:
            a = articles[idx]
            result = self.analyze_article(
                title=a.get("title", ""),
                summary=a.get("summary", ""),
                full_text=a.get("article_text", a.get("full_text", "")),
                source_name=a.get("source_name", ""),
            )
            return idx, result

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(_worker, i): i for i in todo_indices}
            for future in concurrent.futures.as_completed(futures):
                idx = futures[future]
                try:
                    idx, result = future.result()
                    results[idx] = result
                except Exception as e:
                    results[idx] = self._fallback(articles[idx].get("title", ""))
                    log.error("❌ 第 %d 篇处理异常: %s", idx+1, e)

                # 写入缓存
                if cache and results[idx]:
                    url = articles[idx].get('link', '')
                    if url:
                        try:
                            cache.set(url, results[idx])
                        except Exception:
                            pass

                # 增量保存 checkpoint
                if on_complete and results[idx]:
                    try:
                        on_complete(idx, results[idx])
                    except Exception:
                        pass  # checkpoint 保存失败不影响主流程

                completed[0] += 1
                if show_progress:
                    progress = completed[0] + skipped
                    title_preview = articles[idx].get('title', '')[:40]
                    # 优先显示 chinese_title
                    ct = (results[idx] or {}).get('chinese_title', '')
                    if ct:
                        title_preview = ct[:40]
                    log.info("✅ [%d/%d] %s", progress, total, title_preview)

        success = sum(1 for r in results if r and r.get("importance", 0) > 1)
        log.info("📊 分析完成：%d/%d 篇获得有效分析", success, total)

        return results


# ---------------------------------------------------------------------------
# 工具函数：从 config 创建 analyzer
# ---------------------------------------------------------------------------

def create_analyzer_from_config(config: dict) -> Optional[LLMAnalyzer]:
    """从 config.json 中的 llm 配置创建 LLMAnalyzer 实例。"""
    llm_cfg = config.get("llm")
    if not llm_cfg or not llm_cfg.get("enabled", False):
        log.info("ℹ️ LLM 分析未启用（config.llm.enabled = false）")
        return None

    return LLMAnalyzer(
        provider=llm_cfg.get("provider", "openai"),
        base_url=llm_cfg.get("base_url", "https://api.openai.com/v1"),
        api_key=llm_cfg.get("api_key", ""),
        model=llm_cfg.get("model", "gpt-4o-mini"),
        auth_type=llm_cfg.get("auth_type", "bearer"),
        auth_header=llm_cfg.get("auth_header", "Authorization"),
        auth_prefix=llm_cfg.get("auth_prefix", "Bearer"),
        max_retries=llm_cfg.get("max_retries", 3),
        timeout=llm_cfg.get("timeout", 60),
        max_workers=llm_cfg.get("max_workers", 4),
        temperature=llm_cfg.get("temperature", 0.3),
        max_tokens=llm_cfg.get("max_tokens", 1500),
    )


# ---------------------------------------------------------------------------
# 测试入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os

    # 从环境变量读取配置
    analyzer = LLMAnalyzer(
        provider=os.environ.get("LLM_PROVIDER", "openai"),
        base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.environ.get("LLM_API_KEY", ""),
        model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
    )

    test_article = {
        "title": "OpenAI announces GPT-5 with breakthrough reasoning capabilities",
        "summary": "OpenAI has released GPT-5, claiming significant improvements in mathematical reasoning and coding tasks.",
        "full_text": "OpenAI today announced GPT-5, its latest large language model. The company claims the model achieves 92% accuracy on graduate-level math problems, up from 74% with GPT-4. Independent benchmarks from Stanford show more modest improvements of about 5-8% across most tasks. The model uses a new architecture called 'deep reasoning chains' that allows it to break complex problems into substeps. Critics note that the benchmark improvements may not translate to real-world performance, and that the model's training data cutoff remains unclear.",
        "source_name": "TechCrunch",
    }

    result = analyzer.analyze_article(**test_article)
    print(json.dumps(result, ensure_ascii=False, indent=2))
