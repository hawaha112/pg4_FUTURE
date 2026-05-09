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
from urllib.parse import urlparse
import concurrent.futures
import time
import sys
import re
import os
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from logger import get_logger
# LLMCache 抽离到独立模块；此处 re-export 保持 `from llm_analyzer import LLMCache` 向后兼容
from llm_cache import LLMCache  # noqa: F401
log = get_logger('llm_analyzer')


# ---------------------------------------------------------------------------
# 截断工具
# ---------------------------------------------------------------------------

# 句末标点集合（优先级：段落 > 中文句末 > 英文句末 > 半角逗号）
_SENTENCE_ENDS = ['\n\n', '\n', '。', '！', '？', '. ', '! ', '? ', '；', '; ']


def _smart_truncate(text: str, limit: int, note: str = "") -> str:
    """按句末截断（而非硬截），若确实被截则追加提示。

    Args:
        text: 待截断文本
        limit: 最大允许字符数
        note: 被截断时追加到末尾的提示（如 "[内容过长已截断，见原链接]"），空串则不追加

    Returns:
        长度 <= limit (+ note 长度) 的字符串。若原文不超限则原样返回。
    """
    if not text or len(text) <= limit:
        return text or ""
    # 为 note 预留空间（确保追加后总长仍不超过 limit 太多）
    search_limit = max(limit - len(note), max(1, limit // 2))
    head = text[:limit]
    # 从 search_limit 位置向前找最近的句末
    best = -1
    for end in _SENTENCE_ENDS:
        pos = head.rfind(end, search_limit)
        if pos > best:
            best = pos + len(end)
    if best <= 0:
        # 找不到合适句末，退化为硬截断
        truncated = head.rstrip()
    else:
        truncated = head[:best].rstrip()
    if note:
        truncated = truncated + ("\n\n" if "\n" in text else "") + note
    return truncated


# ---------------------------------------------------------------------------
# Prompt 模板
# ---------------------------------------------------------------------------

def _load_prompt(name: str) -> str:
    """加载 prompts/{name}.txt 文件，失败时返回空字符串（由常量的默认值兜底）。

    相对于脚本所在目录查找 prompts/ 目录。
    支持模式字符串如 {title}, {summary}, {count}, {summaries} 等。
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    prompt_path = os.path.join(script_dir, 'prompts', f'{name}.txt')
    try:
        with open(prompt_path, 'r', encoding='utf-8') as f:
            return f.read()
    except (FileNotFoundError, IOError):
        return ""

# 尝试从外部文件加载，如果失败则使用内联默认值
_loaded_system = _load_prompt('system')
SYSTEM_PROMPT = _loaded_system if _loaded_system else """你是 AI 行业分析师。将新闻转化为结构化 JSON 摘要。

规则：直接输出 JSON，第一个字符必须是{，最后必须是}。不要输出代码块、解释或其他文字。所有内容用中文。

如果文章与 AI/ML/大模型/深度学习无关，返回：{"ai_relevant":false}

如果相关，返回以下格式（所有字段必填，不得留空）：
{"ai_relevant":true,"chinese_title":"中文标题，完整成句，20-40字","summary":"一句话概要，50字以内","why_it_matters":"这意味着什么，50字以内","key_details":["要点1(40字内)","要点2(40字内)","要点3(40字内)"],"detailed_content":"深度解读，支持Markdown，见下方说明","background":"背景脉络，150字以内","deep_analysis":"深层分析与影响判断，150字以内","importance":3,"categories":["分类"],"source_type":"news"}

字段说明：
- chinese_title：中文新闻标题，简洁有力，20-40字，**必须是完整句子，不要在半截词处停下**。
  · 好例：「OpenAI发布GPT-5：数学推理能力提升18%」、「Anthropic 推出 Claude Opus 4.7，编程能力达 SOTA」
  · 反例：「英伟达发布Nemotron 3 Nano Omni：30B混合MoE多模态开源模」（"模"是"模型"被切了）
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
- importance：1-5分（5=行业格局级，4=显著进展，3=值得关注，2=一般，1=仅限琐碎内容）。大多数AI新闻应在2-4分。
- categories：1-2个标签，选自：大模型发布|开源生态|AI政策监管|芯片与算力|产品与应用|安全与对齐|融资与商业|学术研究|AI工具|具身智能|自动驾驶|AI编程|行业观点
- source_type：paper|news|official|opinion|community|video

⚠️ 输出校验规则（违反任何一条 = 格式错误，需重新生成）：
1. ai_relevant=true 时，以下字段必须有实质内容，绝不允许为空字符串""：
   - chinese_title（15-25字）、summary（20-50字）、why_it_matters（20-50字）
   - detailed_content（至少300字，这是最重要的字段）
   - background（50-150字）、deep_analysis（50-150字）、key_details（至少2条）
2. 即使原文信息较少、importance=1或2，也必须基于已有信息合理撰写所有字段。
3. 输出前自检：逐个检查上述字段是否为空，若为空则补充后再输出。"""

_loaded_digest_system = _load_prompt('digest_system')
DIGEST_SYSTEM_PROMPT = _loaded_digest_system if _loaded_digest_system else """你是 AI 行业主编。从今天的新闻摘要中提炼编辑导语。

直接返回 JSON，不要代码块：{"editorial":"150字以内的编辑导语"}

要求：点出今天主旋律，串联不同新闻的关联，语言简洁有力。"""

_loaded_digest_user = _load_prompt('digest_user')
DIGEST_USER_TEMPLATE = _loaded_digest_user if _loaded_digest_user else """以下是今天的 {count} 条 AI 新闻摘要，请提炼今日速览：

{summaries}"""

_loaded_user = _load_prompt('user')
USER_PROMPT_TEMPLATE = _loaded_user if _loaded_user else """分析以下文章：

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
        "source_type": {"type": "string"},
        "event_signature": {"type": "string"},
        "audience": {
            "type": "array",
            "items": {"type": "string"}
        }
    },
    # required 是 schema 唯一的硬约束 — 历史上只列 ai_relevant 导致
    # event_signature 经常被 LLM 省略，跨语聚类全失效（multi_source=0）。
    # 把决定下游质量的字段都加上：缺一就触发 retry / 兜底。
    "required": ["ai_relevant", "chinese_title", "summary", "importance", "event_signature"],
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

        # SSL context：仅本地代理（localhost/127.0.0.1）允许关验证，
        # 因为本地代理常用自签证书且流量不出本机；远程 API 必须验证
        # 证书，否则 API Key 在中间人攻击下会泄漏。
        host = (urlparse(self.base_url).hostname or "").lower()
        is_local = host in ("localhost", "::1") or host.startswith("127.")
        if is_local:
            self._ssl_ctx = ssl.create_default_context()
            self._ssl_ctx.check_hostname = False
            self._ssl_ctx.verify_mode = ssl.CERT_NONE
        else:
            try:
                import certifi
                self._ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                self._ssl_ctx = ssl.create_default_context()

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

    def _call_api(self, messages: List[Dict[str, str]], json_schema: dict = None) -> str:
        """调用 LLM API，自动适配 OpenAI / Anthropic 格式，带重试。

        Args:
            messages: OpenAI 格式的 messages 列表
            json_schema: 可选 JSON Schema，透传给支持 response_format 的 API
        """

        if self.provider == "anthropic":
            url, payload = self._build_anthropic_request(messages)
        else:
            url, payload = self._build_openai_request(messages, json_schema=json_schema)

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

    def _build_openai_request(self, messages: List[Dict[str, str]], json_schema: dict = None) -> tuple:
        """构建 OpenAI 兼容 API 请求。"""
        url = f"{self.base_url}/chat/completions"
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "result",
                    "schema": json_schema,
                },
            }
        payload = json.dumps(body).encode("utf-8")
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

        # 长字段用句末截断，避免"话没说完一刀切"
        _dc = str(data.get("detailed_content", ""))
        _bg = str(data.get("background", ""))
        _da = str(data.get("deep_analysis", ""))

        result = {
            "ai_relevant": True,
            "chinese_title": str(data.get("chinese_title") or "").strip()[:60],
            "summary": str(data.get("summary") or "")[:150],
            "why_it_matters": str(data.get("why_it_matters", ""))[:200],
            "key_details": [],
            "detailed_content": _smart_truncate(
                _dc, 3000, note="\n\n> *（内容过长已截断，完整版请见原文链接）*"
            ),
            "background": _smart_truncate(_bg, 600, note="…"),
            "deep_analysis": _smart_truncate(_da, 600, note="…"),
            "importance": 1,
            "categories": [],
            "source_type": str(data.get("source_type", "news")),
            "reading_minutes": 1,
            "audience": [],
            # 跨语聚类用：英文规范化的事件指纹（"OpenAI release GPT-5"），≤80 字符
            "event_signature": str(data.get("event_signature", ""))[:80].strip(),
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

        # audience — 目标读者枚举（可多选，默认 general）
        valid_audiences = {"researcher", "developer", "pm", "investor", "general"}
        raw_aud = data.get("audience", [])
        if isinstance(raw_aud, str):
            raw_aud = [raw_aud]
        if isinstance(raw_aud, list):
            result["audience"] = [
                a for a in (str(x).strip().lower() for x in raw_aud)
                if a in valid_audiences
            ][:3]
        if not result["audience"]:
            result["audience"] = ["general"]

        # 中文率校验：chinese_title / summary / why_it_matters 中文占比应 >= 60%
        # 占比过低说明 LLM 偷懒直接返回了英文/原文片段
        def _chinese_ratio(s: str) -> float:
            if not s:
                return 1.0
            chinese = sum(1 for c in s if '\u4e00' <= c <= '\u9fff')
            alpha_nonspace = sum(1 for c in s if not c.isspace() and not c.isdigit())
            if alpha_nonspace == 0:
                return 1.0
            return chinese / alpha_nonspace

        # 记录低中文率字段（供调用方按需重试；这里不改字段值）
        low_zh = []
        for key in ("chinese_title", "summary", "why_it_matters"):
            val = result.get(key, "")
            if val and _chinese_ratio(val) < 0.6:
                low_zh.append(key)
        if low_zh:
            result["_low_chinese_ratio"] = low_zh

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
                json_schema=ARTICLE_SCHEMA,
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
                retry_prompt = f'分析以下新闻并返回JSON。标题：{title}\n摘要：{summary_hint}\n\n直接返回JSON，第一个字符必须是{{。与AI相关返回{{"ai_relevant":true,"chinese_title":"中文标题20-40字必须完整成句","summary":"一句话概要50字","why_it_matters":"意义50字","key_details":["要点1","要点2","要点3"],"detailed_content":"按提纲展开详述400-800字","background":"背景80字","deep_analysis":"深度分析80字","importance":3,"categories":["分类"],"source_type":"news"}}，无关返回{{"ai_relevant":false}}'
                response2 = self._call_api(
                    [{"role": "user", "content": retry_prompt}],
                    json_schema=ARTICLE_SCHEMA,
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

            validated = self._validate_result(data)

            # 兜底：如果 ai_relevant 但关键字段缺失，追加一次专注调用补充
            if validated.get("ai_relevant"):
                has_gaps = (
                    not validated.get("detailed_content", "").strip()
                    or not validated.get("chinese_title", "").strip()
                )
                if has_gaps:
                    validated = self._supplement_missing_fields(
                        validated, title, summary, full_text, source_name
                    )

            return validated

        except RuntimeError as e:
            log.error("❌ LLM 分析失败 [%s]: %s", title[:30], e)
            return self._fallback(title)

    def _supplement_missing_fields(
        self, base: dict, title: str, summary: str, full_text: str, source_name: str,
    ) -> dict:
        """当 LLM 第一次调用未填满关键字段时，追加一次专注调用补充缺失内容。

        这是兜底机制，正常情况下第一次调用应该填满所有字段。
        仅在 detailed_content 或 chinese_title 为空时触发。
        """
        missing = []
        if not base.get("detailed_content", "").strip():
            missing.append("detailed_content")
        if not base.get("chinese_title", "").strip():
            missing.append("chinese_title")
        if not base.get("background", "").strip():
            missing.append("background")
        if not base.get("deep_analysis", "").strip():
            missing.append("deep_analysis")
        if not base.get("why_it_matters", "").strip():
            missing.append("why_it_matters")

        if not missing:
            return base

        log.info("📝 补充缺失字段 (%s): %s",
                 ",".join(missing), (base.get("chinese_title") or title)[:30])

        article_brief = (full_text or summary or title)[:2000]

        # 构建只请求缺失字段的 prompt
        field_specs = {
            "chinese_title": '"chinese_title":"简洁有力的中文标题15-25字"',
            "detailed_content": '"detailed_content":"600-1000字深度解读，用Markdown格式，### 小标题分段"',
            "background": '"background":"150字以内行业背景脉络"',
            "deep_analysis": '"deep_analysis":"150字以内深层分析与影响判断"',
            "why_it_matters": '"why_it_matters":"这意味着什么，50字以内"',
        }
        fields_json = ",".join(field_specs[f] for f in missing if f in field_specs)

        supplement_prompt = (
            f"你是资深 AI 行业分析师。以下是一篇 AI 相关新闻，请为读者补充深度分析。\n\n"
            f"标题：{title}\n"
            f"来源：{source_name}\n"
            f"摘要：{base.get('summary', '') or summary}\n"
            f"原文片段：{article_brief}\n\n"
            f"请直接返回 JSON（第一个字符必须是 {{），只包含以下字段：\n"
            f"{{{fields_json}}}\n\n"
            f"关键要求：\n"
            f"- detailed_content 必须是 400-800 字的深度解读，用 Markdown 格式，包含 ### 小标题、分段论述、要点分析\n"
            f"- 即使原文信息有限，也请结合你的行业知识进行延展分析和背景补充\n"
            f"- background 应提供行业上下文和相关事件脉络\n"
            f"- deep_analysis 应给出影响判断和趋势洞察\n"
            f"- 所有字段必须有实质内容，不允许空字符串"
        )

        try:
            response = self._call_api(
                [{"role": "user", "content": supplement_prompt}],
            )
            data = self._extract_json(response)

            # 如果第一次解析失败，尝试用更宽松的方式提取
            if not data and response and len(response) > 50:
                log.warning("  ⚠️ JSON 解析失败，尝试宽松提取 (响应长度=%d)", len(response))
                # 某些 LLM 返回的 JSON 外层有注释或解释文字
                # 尝试提取所有字段的内容（按字段名搜索）
                data = {}
                for field in missing:
                    # 搜索 "field_name": "value" 或 "field_name":"value"
                    pattern = rf'"{field}"\s*:\s*"((?:[^"\\]|\\.){{10,}})"'
                    match = re.search(pattern, response, re.DOTALL)
                    if match:
                        val = match.group(1)
                        # 反转义
                        val = val.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
                        data[field] = val

            if data:
                field_map = {
                    "chinese_title": (60, "chinese_title"),
                    "detailed_content": (3000, "detailed_content"),
                    "background": (600, "background"),
                    "deep_analysis": (600, "deep_analysis"),
                    "why_it_matters": (200, "why_it_matters"),
                }
                filled = []
                for field in missing:
                    if field in field_map:
                        max_len, key = field_map[field]
                        val = str(data.get(key, "")).strip()
                        if val:
                            base[key] = val[:max_len]
                            filled.append(f"{key}={len(val)}")
                if filled:
                    log.info("  ✅ 补充成功: %s", " ".join(filled))
                else:
                    log.warning("  ⚠️ 补充调用返回但未填充任何字段 (data keys=%s, response[:100]=%s)",
                                list(data.keys()), response[:100])
            else:
                log.warning("  ⚠️ 补充调用JSON完全解析失败 (响应长度=%d, 前100字=%s)",
                            len(response) if response else 0,
                            (response or "")[:100])
        except Exception as e:
            log.warning("  ⚠️ 补充字段失败: %s", e)

        return base

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
                json_schema=DIGEST_SCHEMA,
            )
            data = self._extract_json(response)
            if not data:
                return {"editorial": "速览生成失败。", "top_stories": []}

            # 校验
            # editorial 限 800 字 — 三层结构（主旋律 + 2-4 个分类组 + 收束）
            # 装得下且留余地。300 字时代是单段编辑导语，多段 prompt 后必须放宽。
            result = {
                "editorial": str(data.get("editorial", ""))[:800],
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

        # 从缓存中恢复已有结果（需验证缓存质量）
        cache_restored = 0
        cache_invalidated = 0
        if cache:
            for i in range(total):
                if i in skip_indices:
                    continue
                url = articles[i].get('link', '')
                cached = cache.get(url)
                if cached:
                    # 缓存质量验证：ai_relevant 的文章必须有 detailed_content
                    is_relevant = cached.get('ai_relevant', False)
                    has_substance = (
                        cached.get('detailed_content', '').strip()
                        or not is_relevant
                    )
                    if has_substance:
                        results[i] = cached
                        skip_indices = skip_indices | {i}
                        cache_restored += 1
                    else:
                        cache.delete(url)
                        cache_invalidated += 1
            if cache_restored:
                log.info("💾 LLM 缓存命中 %d 篇（跳过重复分析）", cache_restored)
            if cache_invalidated:
                log.info("♻️ 缓存质量不合格 %d 篇（将重新分析）", cache_invalidated)

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
