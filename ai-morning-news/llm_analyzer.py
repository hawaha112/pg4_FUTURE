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

# 三道工序流水线: 编辑 + 校对 agent (主编 prompt 仍是 digest_system)
_loaded_editor = _load_prompt('digest_editor')
DIGEST_EDITOR_PROMPT = _loaded_editor if _loaded_editor else """你是编辑, 审主编草稿."""

_loaded_fact_check = _load_prompt('digest_fact_check')
DIGEST_FACT_CHECK_PROMPT = _loaded_fact_check if _loaded_fact_check else """你是校对员."""

_loaded_user = _load_prompt('user')
USER_PROMPT_TEMPLATE = _loaded_user if _loaded_user else """分析以下文章：

【标题】{title}

【来源】{source_name}

【摘要】{summary}

【正文】{full_text}"""

# Retry prompt: 第一次解析失败后用更紧凑的指令重发
_loaded_retry = _load_prompt('article_retry')
ARTICLE_RETRY_TEMPLATE = _loaded_retry if _loaded_retry else (
    '分析以下新闻并返回JSON。标题：{title}\n摘要：{summary_hint}\n\n'
    '直接返回JSON，第一个字符必须是{{。'
    '与AI相关返回{{"ai_relevant":true,"chinese_title":"...","summary":"..."}}'
    '，无关返回{{"ai_relevant":false}}'
)

# ---------------------------------------------------------------------------
# A1 防幻觉: 重要条目上线前, 把卡片要点(摘要/意义/要点)对照原文逐句校验,
# 不被原文支持的事实声明(尤其数字/时间/人名/机构/因果)→ 重写或删除。
# 仅对 importance >= GROUNDING_MIN_IMPORTANCE 的条目跑(控成本); 无原文/失败 → 原样放行。
# ---------------------------------------------------------------------------
GROUNDING_MIN_IMPORTANCE = int(os.environ.get('GROUNDING_MIN_IMPORTANCE', '4'))

_loaded_grounding = _load_prompt('grounding')
GROUNDING_PROMPT = _loaded_grounding if _loaded_grounding else (
    "你是事实校对编辑。下面是新闻【原文】和基于它生成的【分析】。"
    "逐条核对【分析】里的事实声明是否被【原文】直接支持：\n"
    "- 原文支持的 → 保持原样, 尽量别改措辞。\n"
    "- 原文未提及/无法支持的(尤其数字、时间、人名、机构、因果断言) → 删除, 或改写成原文支持的说法。\n"
    "- 与原文矛盾的 → 改成原文的说法。\n"
    "- 绝不要引入原文里没有的新信息。宁可保守、少说, 也不要编。\n\n"
    "只输出 JSON, 不要解释: "
    '{{"summary":"校对后摘要","why_it_matters":"校对后意义","key_details":["要点1","要点2"],'
    '"fixed":true或false(是否改过),"notes":["改了什么及原因","..."]}}\n\n'
    "【原文】\n{source}\n\n【分析】\n{analysis}"
)


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
        "topic_domain": {"type": "string"},
        "topic_leaf": {"type": "string"},
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

        # 用量 + 解析失败计数器（线程安全, 跨 worker 累加）
        # 不持久化, 每次 collector / renderer 跑一次共享一个 analyzer 实例,
        # 跑完取 .usage_stats() 写入 stats.json
        import threading
        self._stats_lock = threading.Lock()
        self._call_count = 0           # 总 LLM 调用次数 (含重试)
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_tokens = 0
        self._json_parse_fallback = 0  # _extract_json 走完所有兜底仍空的次数

    def _record_usage(self, body: dict) -> None:
        """从 LLM 响应里抽 usage 字段累加。OpenAI / Anthropic 都用 body.usage。
        OpenAI: {prompt_tokens, completion_tokens, total_tokens}
        Anthropic: {input_tokens, output_tokens}
        都兼容。"""
        usage = body.get("usage") or {}
        pt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        ct = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        tt = usage.get("total_tokens") or (pt + ct)
        with self._stats_lock:
            self._call_count += 1
            self._prompt_tokens += int(pt or 0)
            self._completion_tokens += int(ct or 0)
            self._total_tokens += int(tt or 0)

    def _record_parse_fallback(self) -> None:
        with self._stats_lock:
            self._json_parse_fallback += 1

    def usage_stats(self) -> dict:
        """快照当前累计的 LLM 用量与解析失败计数。collector / renderer 跑完
        后调用一次, 把结果合并进 stats.json 让 RUN_SUMMARY 能看到趋势。"""
        with self._stats_lock:
            return {
                'llm_call_count': self._call_count,
                'llm_prompt_tokens': self._prompt_tokens,
                'llm_completion_tokens': self._completion_tokens,
                'llm_total_tokens': self._total_tokens,
                'llm_parse_fallback': self._json_parse_fallback,
            }

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
                    # 累计 token 用量（OpenAI 兼容: body.usage; Anthropic: body.usage 同名）
                    self._record_usage(body)
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
    def _escape_inline_newlines(text: str) -> str:
        """把双引号包围的字符串值里的真换行/制表/回车转成 JSON 转义形式。

        LLM 在写多段内容（editorial、detailed_content）时常吐出违反 JSON
        规范的字符串值，里面有真实 \\n。json.loads 会直接报错。这里只
        替换 JSON 字符串值内部的换行，结构层的空白不动。
        """
        out = []
        in_str = False
        escape_next = False
        for ch in text:
            if escape_next:
                out.append(ch)
                escape_next = False
                continue
            if ch == '\\':
                out.append(ch)
                escape_next = True
                continue
            if ch == '"':
                in_str = not in_str
                out.append(ch)
                continue
            if in_str and ch == '\n':
                out.append('\\n'); continue
            if in_str and ch == '\r':
                out.append('\\r'); continue
            if in_str and ch == '\t':
                out.append('\\t'); continue
            out.append(ch)
        return ''.join(out)

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

        # 容错: LLM 经常在 JSON 字符串值里塞真换行 (写多段 editorial 时尤甚),
        # 这违反 JSON 规范。先把字符串值内部的真 \n / \r 转成 \\n / \\r 再解析。
        try:
            return json.loads(LLMAnalyzer._escape_inline_newlines(text))
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

        # reading_minutes: prompt 不要求此字段, LLM 不返回 → 旧逻辑恒为 1(假信号)。
        # 改成从正文长度确定性估算(中文约 400 字/分钟), 让卡片的"N min"真实有用。
        try:
            rm = int(data.get("reading_minutes", 0))
        except (TypeError, ValueError):
            rm = 0
        if rm <= 1:
            _rt_text = (
                str(data.get("detailed_content", "") or "") +
                str(data.get("summary", "") or "") +
                str(data.get("background", "") or "") +
                str(data.get("deep_analysis", "") or "")
            )
            rm = max(1, len(_rt_text) // 400)
        result["reading_minutes"] = max(1, min(30, rm))

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

        # topic_domain / topic_leaf (MECE 单叶主题域; 渲染层据此分组, 非法值由渲染层关键词兜底)
        result["topic_domain"] = str(data.get("topic_domain", "") or "").strip()[:20]
        result["topic_leaf"] = str(data.get("topic_leaf", "") or "").strip()[:30]

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
                self._record_parse_fallback()
                log.warning("⚠️ JSON 解析失败，重试中... (原始: %s)", response[:100])
            elif not data.get("summary") and data.get("ai_relevant", True):
                # JSON 解析成功但缺少 summary（截断导致关键字段丢失）
                need_retry = True
                log.warning("⚠️ 缺少 summary 字段，重试中... (keys: %s)", list(data.keys()))

            if need_retry:
                summary_hint = summary[:80] if summary else ""
                retry_prompt = ARTICLE_RETRY_TEMPLATE.format(
                    title=title or '',
                    summary_hint=summary_hint,
                )
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

            # A1 防幻觉: 重要条目上线前把要点对照原文校验, 不被支持的声明重写/剥离
            try:
                if (validated.get('ai_relevant')
                        and int(validated.get('importance', 0) or 0) >= GROUNDING_MIN_IMPORTANCE):
                    validated = self._ground_check(validated, full_text, title)
            except Exception as e:
                log.warning("⚠️ grounding 校验跳过(原样放行): %s", e)

            return validated

        except RuntimeError as e:
            log.error("❌ LLM 分析失败 [%s]: %s", title[:30], e)
            return self._fallback(title)

    def _ground_check(self, validated: dict, full_text: str, title: str) -> dict:
        """把卡片要点(summary/why_it_matters/key_details)对照原文逐句校验, 防幻觉。

        原文不足(抓取失败/太短)→ 无可对照, 原样放行(不误伤)。校验失败 → 原样放行。
        校验结果写入 validated['_grounding'] = {checked, fixed, notes} 供透明展示/评测。
        """
        src = (full_text or '').strip()
        if len(src) < 120:
            return validated   # 没有足够原文可对照, 跳过(避免误删真内容)

        payload = json.dumps({
            'summary': validated.get('summary', ''),
            'why_it_matters': validated.get('why_it_matters', ''),
            'key_details': validated.get('key_details', []),
        }, ensure_ascii=False)
        prompt = GROUNDING_PROMPT.format(source=src[:3000], analysis=payload)

        resp = self._call_api([{"role": "user", "content": prompt}])
        d = self._extract_json(resp)
        if not isinstance(d, dict):
            return validated

        # 应用校对后的字段(非空才覆盖, 避免把内容清空)
        for k in ('summary', 'why_it_matters'):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                validated[k] = v.strip()
        kd = d.get('key_details')
        if isinstance(kd, list) and kd:
            validated['key_details'] = [str(x).strip() for x in kd[:5] if str(x).strip()]

        validated['_grounding'] = {
            'checked': True,
            'fixed': bool(d.get('fixed')),
            'notes': [str(n)[:120] for n in (d.get('notes') or [])[:4]],
        }
        if validated['_grounding']['fixed']:
            log.info("🛡️ grounding 修正 [%s]: %s",
                     (validated.get('chinese_title') or title)[:30],
                     '; '.join(validated['_grounding']['notes'])[:120])
        return validated

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
    # 渲染前语义去重（同一事件不同来源/措辞 → 合并成一条）
    # ------------------------------------------------------------------

    def dedupe_same_event(self, items: List[dict]) -> List[dict]:
        """LLM 语义去重：把指向【同一真实事件】的 item 合并成一条（保留最优）。

        为什么不用表层相似度：实测同事件不同措辞的真重复 minhash 仅 0.28，
        调阈值必然漏判或误合并。这里用 LLM 按语义判同（每次出报 1 次小调用）。

        安全：LLM 报错 / 解析空 / 编号越界 → 原样返回，绝不丢条目。
        """
        if not items or len(items) < 3:
            return items
        lines = []
        for i, it in enumerate(items):
            a = it.get('analysis', {}) or {}
            t = (a.get('chinese_title') or it.get('title') or '').strip().replace('\n', ' ')
            lines.append(f'[{i}] {t[:60]}')
        sys_msg = "你是新闻去重助手。只判断哪些标题指向同一真实事件，不做别的。"
        user_msg = (
            "下面是今天的 AI 新闻标题(带编号)。把指向【同一真实事件】的编号分到一组："
            "同一事件 = 同一主体 + 同一动作(哪怕来源/措辞/语言不同)；不同事件不要合并；"
            "泛主题汇总贴(如 newsletter 综述)不算与某条具体新闻同事件。\n"
            '只输出 JSON，不要解释：{"groups": [[同事件编号,…], …]}，只列含 ≥2 个编号的组。\n\n'
            + '\n'.join(lines)
        )
        try:
            resp = self._call_api([
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ])
            groups = (self._extract_json(resp) or {}).get('groups') or []
        except Exception as e:
            log.warning("⚠️ 语义去重调用失败(原样返回): %s", e)
            return items
        if not isinstance(groups, list):
            return items

        n = len(items)

        def _score(idx: int):
            it = items[idx]
            a = it.get('analysis', {}) or {}
            return (int(a.get('importance', 0) or 0),
                    int(it.get('cluster_size', 1) or 1), -idx)

        drop = set()
        for g in groups:
            if not isinstance(g, list):
                continue
            ids = []
            for x in g:
                if isinstance(x, bool):
                    continue
                if isinstance(x, int) or (isinstance(x, str) and x.strip().isdigit()):
                    xi = int(x)
                    if 0 <= xi < n:
                        ids.append(xi)
            ids = list(dict.fromkeys(ids))   # 去重保序
            if len(ids) < 2:
                continue
            keep = max(ids, key=_score)
            for x in ids:
                if x != keep:
                    drop.add(x)
        if not drop:
            return items
        log.info("🧠 语义去重：合并 %d 条同事件重复（%d → %d）",
                 len(drop), n, n - len(drop))
        return [it for i, it in enumerate(items) if i not in drop]

    # 口播稿目标长度: edge-tts 晓晓女声 (zh-CN-XiaoxiaoNeural) 实测 ≈290 字/分钟,
    # 用户要求 5-6 分钟 → 1550-1750 字落点 ≈5.3-6.0 分钟; 越界一次重试。
    # (换声音记得同步: Yunyang≈317/min → 1650-1850; tts_broadcast.CHARS_PER_MIN 也要改)
    BROADCAST_TARGET_CHARS = (1550, 1750)
    BROADCAST_HARD_BOUNDS = (1300, 2050)

    def generate_broadcast_script(self, digest: dict, items: list = None,
                                  shift: str = '', date_str: str = '') -> str:
        """生成 5-6 分钟的每日 AI 口播稿(电台结构), 供 TTS 合成音频 + 页面文字稿。

        结构: 开场(日期+总起) → 头条深讲2-3条 → 快讯串播 → 今日判断 → 收尾。
        shift/date_str 决定开场问候(早班"早上好"/晚班"晚上好")与节目自称——
        不传则 LLM 会默认写成早报口吻(2026-06-11 用户反馈晚班说了"早上好")。
        失败返回 ''(调用方据此不渲染口播稿区块、不合成音频)。
        """
        digest = digest or {}
        judgments = digest.get('judgments') or []
        items = items or []
        if not judgments and not items:
            return ''
        headline = (digest.get('headline') or '').strip()
        shift = (shift or '').lower()
        if shift == 'pm':
            shift_rule = (f"这是一期【晚报】({date_str}): 开场问候必须用「晚上好」, "
                          "节目自称「AI 晚报」, 内容口吻是回顾今天发生的事, "
                          "收尾说「明天早上见」。")
        elif shift == 'am':
            shift_rule = (f"这是一期【早报】({date_str}): 开场问候必须用「早上好」, "
                          "节目自称「AI 早报」, 收尾说「今晚/明天见」。")
        else:
            shift_rule = (f"这是一期日报({date_str}): 开场问候用「大家好」, "
                          "节目自称「AI 日报」。")

        # ── 素材分层: 头条(importance 最高 3 条, 给足上下文) / 快讯(其余 8 条标题+一句话) ──
        def _imp(it):
            return (it.get('analysis', {}) or {}).get('importance', 0) or 0
        ranked = sorted((it for it in items
                         if (it.get('analysis', {}) or {}).get('ai_relevant', True)),
                        key=_imp, reverse=True)
        top, quick = ranked[:3], ranked[3:11]

        def _line(it, full=False):
            a = it.get('analysis', {}) or {}
            t = (a.get('chinese_title') or it.get('title') or '').strip()
            s = (a.get('summary') or '').strip()
            if not full:
                return f'- {t}：{s[:80]}'
            w = (a.get('why_it_matters') or '').strip()
            d = (a.get('deep_analysis') or a.get('detailed_content') or '').strip()
            parts = [f'- {t}', f'  摘要: {s[:200]}']
            if w:
                parts.append(f'  为什么重要: {w[:200]}')
            if d:
                parts.append(f'  深度: {d[:300]}')
            return '\n'.join(parts)

        jud_txt = '\n'.join(
            f"- {(j.get('title') or '').strip()}\n  {(j.get('body') or '').strip()}"
            for j in judgments[:2] if (j.get('title') or '').strip()
        )
        material = (
            (f'【今日主旋律】{headline}\n\n' if headline else '')
            + ('【头条素材(深讲用)】\n' + '\n'.join(_line(it, full=True) for it in top) + '\n\n' if top else '')
            + ('【快讯素材(串播用)】\n' + '\n'.join(_line(it) for it in quick) + '\n\n' if quick else '')
            + ('【今日判断(收尾观点用)】\n' + jud_txt if jud_txt else '')
        )
        lo, hi = self.BROADCAST_TARGET_CHARS

        sys_msg = (
            "你是顶级中文科技电台的主播兼撰稿人, 风格沉稳、口语、有观点。"
            "你写的稿子将直接被 TTS 朗读成音频节目, 听众在通勤路上听。"
        )
        user_msg = (
            f"用下面的素材写一期【{lo}-{hi}字】的 AI 新闻口播稿(念出来约5-6分钟)。\n\n"
            f"【班次】{shift_rule}\n\n"
            "【节目结构(必须按此顺序, 但不要写小标题)】\n"
            "1. 开场(约80字): 按上面班次要求问候 + 报日期 + 用一句话点出今天最值得关注的主线\n"
            "2. 头条深讲(2-3条, 每条250-350字): 每条按 发生了什么→半句背景铺垫→为什么重要(讲机制,不要套话)→一句影响或你的看法。条与条之间用口语过渡(比如「说完这个,再看…」)\n"
            "3. 快讯串播(5-8条, 每条30-50字): 节奏加快, 一条一两句话, 开头说「接下来是几条快讯」\n"
            "4. 今日观点(150-250字): 把「今日判断」用口语讲清楚推理链, 开头说类似「最后聊一个观点」\n"
            "5. 收尾(约50字): 一句收束 + 提醒文字版在简报页面 + 按班次要求道别\n\n"
            "【口播硬规则】\n"
            "- 纯口语短句, 像跟朋友讲事; 一句话只装一个信息点\n"
            "- 专有名词第一次出现给半句铺垫(「做AI编程工具的Cursor」)\n"
            "- 所有数字口语化: 「四十亿美元」不写「$4B」,「百分之三十」不写「30%」; 英文名可保留(GPT、OpenAI)\n"
            "- 不要 markdown、序号、小标题、括号注释、emoji —— 输出将逐字朗读\n"
            "- 段落之间空一行(朗读时自然停顿)\n"
            "- 不确定的事就说「据报道」, 不要把传闻说成事实\n\n"
            f"【素材】\n{material}\n\n"
            f"只输出口播稿正文。再次强调: 总字数控制在 {lo}-{hi} 字。"
        )
        lo_h, hi_h = self.BROADCAST_HARD_BOUNDS
        try:
            resp = (self._call_api([
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ]) or '').strip()
            n = len(resp)
            if resp and not (lo_h <= n <= hi_h):
                # 越界一次重试: 给出当前字数与精确目标
                log.info("🎙 口播稿 %d 字越界 [%d,%d], 重试一次", n, lo_h, hi_h)
                fix = ('压缩' if n > hi_h else '扩充')
                resp2 = (self._call_api([
                    {"role": "system", "content": sys_msg},
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": resp},
                    {"role": "user", "content":
                        f"这版 {n} 字, 不符合 {lo}-{hi} 字要求。请{fix}到 {lo}-{hi} 字: "
                        f"保持同样结构与口语风格, {'删减次要快讯和重复表述' if n > hi_h else '给头条补充背景与影响分析、增加1-2条快讯'}。"
                        "只输出修改后的完整口播稿。"},
                ]) or '').strip()
                if resp2 and lo_h <= len(resp2) <= hi_h:
                    resp = resp2
                elif resp2 and abs(len(resp2) - (lo + hi) / 2) < abs(n - (lo + hi) / 2):
                    resp = resp2  # 没达标但更接近, 取较好的一版
            log.info("🎙 口播稿 %d 字 (目标 %d-%d, ≈%.1f 分钟)",
                     len(resp), lo, hi, len(resp) / 290)
            return resp
        except Exception as e:
            log.warning("⚠️ 口播稿生成失败(跳过): %s", e)
            return ''

    # 今日速览（全局综合）
    # ------------------------------------------------------------------

    def generate_digest(self, analyses: List[dict]) -> dict:
        """
        从所有文章分析中生成"今日 3 个判断", 走 **3 道工序流水线** (v3).

        Pipeline:
            Stage A (主编): 从事件中提炼 3 判断 draft (digest_system prompt)
                ↓
            Stage B (编辑): 找最弱的, 逼重写到更狠 (digest_editor prompt)
                ↓
            Stage C (校对): 提取事实声明, 对照 events 验证 (digest_fact_check prompt)
                ↓
            合并 → 最终 judgments (含 fact_check 字段)

        失败降级:
        - Stage B 失败 → 用 Stage A draft (跳过 editor 加 _skipped_editor 标)
        - Stage C 失败 → 用 Stage B 输出但 fact_check 空 (UI 不显示徽章)
        - Stage A 失败 → 走旧的 fallback editorial 文本

        新输出结构 (v3):
        {
          "headline": "今日主旋律 一句话",
          "judgments": [
              {
                  "emoji": "🏢", "title": "...", "body": "...",
                  "evidence_ids": [0, 5],
                  "fact_check": {  # 新增 (Stage C 输出)
                      "verified_count": 3,
                      "unverified_count": 1,
                      "contradicted_count": 0,
                      "confidence": "high",  # high/medium/low
                      "warnings": [],
                      "claims": [...]
                  },
                  "_was_rewritten": true/false  # 来自 Stage B
              },
              ...
          ],
          "outro": "收束观点 (可选)",
          "editorial": "纯文本兜底版 (judgments 缺失时用)",
          "_pipeline_stages": {  # 调试用, dashboard 不显示
              "editor_ran": true,
              "editor_rewrote": 1,
              "fact_check_ran": true,
              "total_tokens_estimated": 100000
          },
          "top_stories": []
        }
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

        empty_result = {
            "headline": "",
            "judgments": [],
            "outro": "",
            "editorial": "今天暂无重要 AI 新闻。",
            "top_stories": [],
            "_pipeline_stages": {},
        }
        if not summaries_text.strip():
            return empty_result

        count = len([a for a in analyses if a.get("analysis", {}).get("ai_relevant", True)])
        user_msg = DIGEST_USER_TEMPLATE.format(count=count, summaries=summaries_text)

        # ═══ Stage A: 主编生成 draft ═══
        draft_data = self._digest_stage_a_chief(user_msg)
        if not draft_data or not draft_data.get('judgments'):
            log.warning("digest Stage A 失败, 退回旧版纯文本 editorial")
            return self._digest_legacy_fallback(user_msg, empty_result)

        clean_draft = self._clean_judgments(draft_data.get('judgments') or [])
        if not clean_draft:
            return self._digest_legacy_fallback(user_msg, empty_result)

        headline = str(draft_data.get('headline', '')).strip()[:60]
        outro = str(draft_data.get('outro', '')).strip()[:120]

        # ═══ Stage B: 编辑审稿 ═══
        edited_judgments, editor_meta = self._digest_stage_b_editor(
            clean_draft, summaries_text, count
        )
        if edited_judgments:
            final_judgments = edited_judgments
        else:
            log.warning("digest Stage B 失败, 使用 Stage A draft")
            final_judgments = clean_draft
            editor_meta = {'editor_ran': False}

        # ═══ Stage C: 校对事实 ═══
        fact_checks, fact_meta = self._digest_stage_c_fact_check(
            final_judgments, summaries_text, count
        )
        if fact_checks:
            for j, fc in zip(final_judgments, fact_checks):
                j['fact_check'] = fc

        # 拼兜底 editorial
        fallback_parts = []
        if headline:
            fallback_parts.append(headline)
        for j in final_judgments:
            fallback_parts.append(f"{j['emoji']} **{j['title']}**\n{j['body']}")
        if outro:
            fallback_parts.append(outro)

        log.info("✓ 速览(v3 三道工序): %d 判断, 编辑改了 %d 条, 校对 %s",
                 len(final_judgments),
                 editor_meta.get('editor_rewrote', 0),
                 '已跑' if fact_meta.get('fact_check_ran') else '跳过')

        return {
            'headline': headline,
            'judgments': final_judgments,
            'outro': outro,
            'editorial': '\n\n'.join(fallback_parts)[:1500],
            'top_stories': [],
            '_pipeline_stages': {**editor_meta, **fact_meta},
        }

    # ═════════════════════════════════════════════════════
    # Stage A: 主编 (现有 digest_system prompt)
    # ═════════════════════════════════════════════════════
    def _digest_stage_a_chief(self, user_msg: str) -> Optional[dict]:
        """主编 agent: 从事件出 3 判断 draft."""
        try:
            response = self._call_api([
                {"role": "system", "content": DIGEST_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ])
            raw = (response or '').strip()
            raw = re.sub(r'^```(?:\w+)?\s*', '', raw)
            raw = re.sub(r'\s*```\s*$', '', raw).strip()
            return self._extract_json(raw)
        except Exception as e:
            log.error("❌ Stage A (主编) 失败: %s", e)
            return None

    def _clean_judgments(self, judgments: list) -> list:
        """清洗 + 校验 judgments 数组."""
        clean = []
        for j in judgments[:3]:
            if not isinstance(j, dict):
                continue
            title = str(j.get('title', '')).strip()
            body = str(j.get('body', '')).strip()
            emoji = str(j.get('emoji', '🔹')).strip()[:4] or '🔹'
            if not title or len(title) < 5 or not body or len(body) < 20:
                continue
            evidence = j.get('evidence_ids') or []
            if not isinstance(evidence, list):
                evidence = []
            clean.append({
                'emoji': emoji,
                'title': title[:60],
                'body': body[:400],
                'evidence_ids': [int(x) for x in evidence
                                 if isinstance(x, (int, str)) and str(x).isdigit()][:5],
                '_was_rewritten': bool(j.get('_was_rewritten', False)),
            })
        return clean

    # ═════════════════════════════════════════════════════
    # Stage B: 编辑 (审稿 + 找最弱 + 重写)
    # ═════════════════════════════════════════════════════
    def _digest_stage_b_editor(self, draft: list, summaries: str,
                               count: int) -> tuple:
        """编辑 agent: 审 draft, 仅重写最弱 1 条 (新 schema: patch 单条).

        新 schema 优势 (vs 让 LLM 输出整个 revised_judgments 数组):
        - LLM 输出短 → JSON 解析失败率从 ~30% 降到 ~5%
        - 减少 token 消耗 (output 部分降 60%)
        - 主编原意保留性更好 (其他 2 条不改)
        """
        try:
            # 给 LLM 看 draft 时只显示关键字段, 避免内部 underscore 字段干扰
            draft_compact = [
                {'idx': i, 'emoji': j.get('emoji'), 'title': j.get('title'),
                 'body': j.get('body'), 'evidence_ids': j.get('evidence_ids', [])}
                for i, j in enumerate(draft)
            ]
            draft_json = json.dumps(draft_compact, ensure_ascii=False, indent=2)
            user_msg = DIGEST_EDITOR_PROMPT.format(
                draft_judgments=draft_json,
                events_summary=summaries,
                count=count,
            )
            response = self._call_api([
                {"role": "user", "content": user_msg},
            ])
            raw = (response or '').strip()
            raw = re.sub(r'^```(?:\w+)?\s*', '', raw)
            raw = re.sub(r'\s*```\s*$', '', raw).strip()
            data = self._extract_json(raw)
            if not isinstance(data, dict):
                return None, {'editor_ran': False, 'editor_rewrote': 0}

            weakest_idx = data.get('weakest_idx')
            editor_notes = str(data.get('editor_notes', ''))[:200]

            # -1 = 编辑认为草稿已达标, 不改
            if weakest_idx == -1:
                log.info("Stage B (编辑): 主编草稿已达标, 无需重写")
                return draft, {
                    'editor_ran': True, 'editor_rewrote': 0,
                    'editor_notes': editor_notes,
                }

            if (not isinstance(weakest_idx, int) or
                    weakest_idx < 0 or weakest_idx >= len(draft)):
                log.warning("Stage B 输出的 weakest_idx 无效: %r", weakest_idx)
                return None, {'editor_ran': False, 'editor_rewrote': 0}

            revised_title = str(data.get('revised_title', '')).strip()
            revised_body = str(data.get('revised_body', '')).strip()
            if not revised_title or len(revised_title) < 5 or not revised_body or len(revised_body) < 20:
                log.warning("Stage B 重写内容不合规 (title=%d 字, body=%d 字)",
                            len(revised_title), len(revised_body))
                return None, {'editor_ran': False, 'editor_rewrote': 0}

            # patch draft, 只改最弱那一条
            patched = [dict(j) for j in draft]
            evidence = data.get('revised_evidence_ids') or draft[weakest_idx].get('evidence_ids', [])
            if not isinstance(evidence, list):
                evidence = []
            patched[weakest_idx] = {
                'emoji': str(data.get('revised_emoji',
                                      draft[weakest_idx].get('emoji', '🔹'))).strip()[:4] or '🔹',
                'title': revised_title[:60],
                'body': revised_body[:400],
                'evidence_ids': [int(x) for x in evidence
                                 if isinstance(x, (int, str)) and str(x).isdigit()][:5],
                '_was_rewritten': True,
            }
            return patched, {
                'editor_ran': True,
                'editor_rewrote': 1,
                'editor_notes': editor_notes,
            }
        except Exception as e:
            log.warning("⚠️ Stage B (编辑) 失败, 跳过: %s", e)
            return None, {'editor_ran': False, 'editor_rewrote': 0}

    # ═════════════════════════════════════════════════════
    # Stage C: 校对 (事实核对)
    # ═════════════════════════════════════════════════════
    def _digest_stage_c_fact_check(self, judgments: list, summaries: str,
                                   count: int) -> tuple:
        """校对 agent: 抽取事实声明 + 对照 events 验证."""
        try:
            judgments_json = json.dumps(
                [{'idx': i, 'title': j['title'], 'body': j['body']}
                 for i, j in enumerate(judgments)],
                ensure_ascii=False, indent=2
            )
            user_msg = DIGEST_FACT_CHECK_PROMPT.format(
                judgments_json=judgments_json,
                events_summary=summaries,
                count=count,
            )
            response = self._call_api([
                {"role": "user", "content": user_msg},
            ])
            raw = (response or '').strip()
            raw = re.sub(r'^```(?:\w+)?\s*', '', raw)
            raw = re.sub(r'\s*```\s*$', '', raw).strip()
            data = self._extract_json(raw)
            fact_checks = data.get('fact_checks') if isinstance(data, dict) else None
            if not isinstance(fact_checks, list):
                return None, {'fact_check_ran': False}
            # 按 idx 对齐到 judgments 顺序
            result = [None] * len(judgments)
            for fc in fact_checks:
                if not isinstance(fc, dict):
                    continue
                idx = fc.get('idx')
                if isinstance(idx, int) and 0 <= idx < len(judgments):
                    result[idx] = {
                        'verified_count': int(fc.get('verified_count', 0) or 0),
                        'unverified_count': int(fc.get('unverified_count', 0) or 0),
                        'contradicted_count': int(fc.get('contradicted_count', 0) or 0),
                        'confidence': str(fc.get('confidence', 'medium'))[:10],
                        'warnings': (fc.get('warnings') or [])[:3],
                        'claims': (fc.get('claims_found') or [])[:6],
                    }
            # 缺位置用默认 medium 填充
            for i in range(len(result)):
                if result[i] is None:
                    result[i] = {
                        'verified_count': 0, 'unverified_count': 0,
                        'contradicted_count': 0, 'confidence': 'medium',
                        'warnings': [], 'claims': [],
                    }
            return result, {'fact_check_ran': True}
        except Exception as e:
            log.warning("⚠️ Stage C (校对) 失败, 跳过: %s", e)
            return None, {'fact_check_ran': False}

    # ═════════════════════════════════════════════════════
    # Legacy fallback
    # ═════════════════════════════════════════════════════
    def _digest_legacy_fallback(self, user_msg: str, empty_result: dict) -> dict:
        """3 阶段都崩了 → 退到旧版纯文本 editorial."""
        try:
            response = self._call_api([
                {"role": "system", "content": DIGEST_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ])
            raw = (response or '').strip()
            raw = re.sub(r'^```(?:\w+)?\s*', '', raw)
            raw = re.sub(r'\s*```\s*$', '', raw).strip()
            if len(raw) >= 20:
                return {**empty_result, 'editorial': raw[:1500]}
        except Exception as e:
            log.error("❌ legacy fallback 也崩了: %s", e)
        return {**empty_result, 'editorial': "速览生成失败。"}

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
