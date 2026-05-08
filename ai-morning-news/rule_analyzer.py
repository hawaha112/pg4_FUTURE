#!/usr/bin/env python3
"""
rule_analyzer.py — 纯规则结构化分析器，零 LLM 依赖

核心理念：
  LLM 是"增强器"，不是"门卫"。
  当 LLM 不可用时，系统仍能输出高质量"事实页"。

职责：
  1. 规则判断 AI 相关性（关键词匹配 + 来源类型加权）
  2. 轻量结构化提取（标题翻译、分类、source_type、importance 估算）
  3. 从正文提取关键句（TextRank 简化版，纯规则）
  4. 为每条文章生成 LLM 兼容的 analysis dict，可无缝接入后续流程

分级分析架构：
  Level 0: RuleAnalyzer（本模块）— 全部条目，零 LLM，毫秒级
  Level 1: LLMAnalyzer（轻量 prompt）— 候选高价值条目，快速摘要
  Level 2: LLMAnalyzer（深度 prompt）— Top N 事件，600-1500 字深度解读

用法:
    analyzer = RuleAnalyzer()
    result = analyzer.analyze(title, summary, full_text, source_name, source_tier)
    # result 格式与 LLMAnalyzer 输出完全兼容
"""

import concurrent.futures
import re
import hashlib
from collections import Counter
from typing import Dict, List, Optional, Tuple

from logger import get_logger
log = get_logger('rule_analyzer')


# ═══════════════════════════════════════════════════════════════════════
# AI 相关性判断
# ═══════════════════════════════════════════════════════════════════════

# 强信号关键词（命中 1 个即可判定为 AI 相关）
# 注意：英文用 \b 做词界，中文不加 \b（CJK 字符没有 word boundary）
_STRONG_KEYWORDS_EN = re.compile(
    r'(?i)\b(?:'
    r'gpt|chatgpt|openai|o1|o3|o4|'
    r'claude|anthropic|sonnet|opus|haiku|'
    r'gemini|gemma|bard|'
    r'deepseek|mistral|llama|qwen|'
    r'midjourney|stable.diffusion|sora|dall.?e|flux|'
    r'copilot|cursor|'
    r'hugging.?face|pytorch|tensorflow|'
    r'nvidia|cuda|tpu'
    r')\b'
)
_STRONG_KEYWORDS_CJK = re.compile(
    r'大模型|大语言模型|AI编程|AI搜索|AI助手|'
    r'ChatGPT|文心一言|通义千问|豆包|Kimi|'
    r'GPT-?\d|Claude|Gemini|DeepSeek|Llama'
)

# 中等信号关键词（需要 2+ 个才判定）
_MEDIUM_KEYWORDS_EN = re.compile(
    r'(?i)\b(?:'
    r'ai|artificial.intelligence|machine.learning|deep.learning|'
    r'neural.net|llm|foundation.model|transformer|'
    r'diffusion|reinforcement.learning|'
    r'nlp|nlu|computer.vision|multi.?modal|'
    r'generative|gen.?ai|agi|alignment|'
    r'embedding|fine.?tun|rag|vector|'
    r'agent|agentic|reasoning|'
    r'chip|gpu|npu|'
    r'robot|autonomous|self.driving|humanoid'
    r')\b'
)
_MEDIUM_KEYWORDS_CJK = re.compile(
    r'人工智能|机器学习|深度学习|神经网络|'
    r'智能体|算力|芯片|自动驾驶|具身智能|'
    r'模型|训练|推理|微调|对齐|多模态'
)

# 高置信度 AI 专业源（来自这些源的文章默认 AI 相关）
_AI_DEDICATED_SOURCES = {
    'OpenAI Blog', 'Anthropic News', 'Google AI Blog', 'DeepMind Blog',
    'Microsoft AI Blog', 'Meta AI Blog', 'Hugging Face Blog',
    'ArXiv AI', 'ArXiv ML',
    'X-OpenAI', 'X-AnthropicAI', 'X-GoogleDeepMind',
    'X-Karpathy', 'X-Jim Fan', 'Yannic Kilcher',
    'X-Yann LeCun', 'X-Andrew Ng', 'X-Sam Altman',
}

# 分类关键词映射
_CATEGORY_PATTERNS = {
    '大模型发布': re.compile(r'(?i)(?:launch|release|announc|introduc|unveil|发布|推出|上线|开放|正式)'),
    '开源生态': re.compile(r'(?i)(?:open.?source|github|apache|mit.license|开源|开放源代码)'),
    'AI政策监管': re.compile(r'(?i)(?:regulat|policy|govern|law|ban|restrict|eu.ai|白宫|监管|政策|法规|禁令)'),
    '芯片与算力': re.compile(r'(?i)(?:chip|gpu|tpu|npu|nvidia|amd|intel|cuda|算力|芯片|显卡|半导体)'),
    '产品与应用': re.compile(r'(?i)(?:product|app|feature|api|sdk|tool|service|产品|应用|功能|工具|服务)'),
    '安全与对齐': re.compile(r'(?i)(?:safety|alignment|jailbreak|bias|harm|安全|对齐|越狱|偏见|风险)'),
    '融资与商业': re.compile(r'(?i)(?:funding|invest|valuat|revenue|ipo|acqui|融资|投资|估值|收购|上市)'),
    '学术研究': re.compile(r'(?i)(?:paper|research|study|arxiv|benchmark|论文|研究|实验|基准|学术)'),
    'AI工具': re.compile(r'(?i)(?:tool|plugin|extension|mcp|integration|工具|插件|集成)'),
    '具身智能': re.compile(r'(?i)(?:robot|humanoid|embodied|机器人|人形|具身)'),
    '自动驾驶': re.compile(r'(?i)(?:self.driv|autonom|autopilot|waymo|tesla.fsd|自动驾驶|无人驾驶)'),
    'AI编程': re.compile(r'(?i)(?:code.gen|copilot|cursor|coding|vibe.?cod|AI编程|代码生成)'),
    '行业观点': re.compile(r'(?i)(?:opinion|editorial|think|predict|观点|评论|预测|展望)'),
}

# source_type 推断规则
_SOURCE_TYPE_PATTERNS = {
    'official': re.compile(r'(?i)(?:blog|official|announce|我们|today.we|introducing)'),
    'paper': re.compile(r'(?i)(?:arxiv|paper|abstract|我们提出|we.propose|novel.approach)'),
    'opinion': re.compile(r'(?i)(?:opinion|editorial|think|我认为|I.think|hot.take)'),
    'community': re.compile(r'(?i)(?:reddit|hacker.?news|知乎|forum|讨论)'),
    'video': re.compile(r'(?i)(?:youtube|视频|video|watch|podcast)'),
}

# 实体 → importance 加成
_ENTITY_IMPORTANCE = {
    'openai': 1.5, 'anthropic': 1.5, 'google_deepmind': 1.5,
    'meta_ai': 1.0, 'microsoft': 1.0, 'nvidia': 1.0,
    'deepseek': 0.8, 'mistral': 0.8, 'xai': 0.8,
}


class RuleAnalyzer:
    """纯规则分析器：零 LLM 依赖的结构化信息提取"""

    def __init__(self, entity_registry: dict = None):
        self.entity_registry = entity_registry or {}
        self._stats = {'analyzed': 0, 'ai_relevant': 0, 'not_relevant': 0}

    def analyze(
        self,
        title: str,
        summary: str = '',
        full_text: str = '',
        source_name: str = '',
        source_tier: int = 2,
        entities: List[str] = None,
    ) -> Dict:
        """分析单篇文章，返回与 LLMAnalyzer 兼容的结构化 dict

        Returns:
            dict 包含 ai_relevant, chinese_title, summary, importance,
            categories, source_type 等字段
        """
        self._stats['analyzed'] += 1
        text = f"{title} {summary} {(full_text or '')[:500]}"

        # ── 1. AI 相关性判断 ──
        ai_relevant = self._judge_relevance(text, source_name, source_tier)
        if not ai_relevant:
            self._stats['not_relevant'] += 1
            return {'ai_relevant': False, '_analysis_level': 0}

        self._stats['ai_relevant'] += 1

        # ── 2. 分类 ──
        categories = self._classify(text)

        # ── 3. source_type ──
        source_type = self._infer_source_type(text, source_name, source_tier)

        # ── 4. importance 估算 ──
        importance = self._estimate_importance(
            text, source_name, source_tier, entities or []
        )

        # ── 5. 提取关键句 ──
        key_details = self._extract_key_sentences(title, summary, full_text)

        # ── 6. 生成中文标题（如果原标题是英文） ──
        chinese_title = self._gen_chinese_title(title, source_name)

        # ── 7. 构建结果 ──
        result = {
            'ai_relevant': True,
            'chinese_title': chinese_title,
            'summary': self._gen_summary(title, summary),
            'why_it_matters': '',  # 规则无法生成"为什么重要"
            'key_details': key_details,
            'detailed_content': '',  # 规则不生成深度内容，留给 LLM
            'background': '',
            'deep_analysis': '',
            'importance': importance,
            'categories': categories,
            'source_type': source_type,
            'reading_minutes': max(1, len(full_text or '') // 1500),
            '_analysis_level': 0,  # 标记为 Level 0（规则分析）
            '_needs_llm_upgrade': importance >= 3,  # 高价值条目需要 LLM 升级
        }

        return result

    def batch_analyze(self, articles: List[dict]) -> List[dict]:
        """批量分析文章列表

        Args:
            articles: 每条需有 title, summary, full_text, source_name, source_tier

        Returns:
            与 articles 等长的分析结果列表
        """
        results = []
        for art in articles:
            result = self.analyze(
                title=art.get('title', ''),
                summary=art.get('summary', ''),
                full_text=art.get('full_text', ''),
                source_name=art.get('source_name', ''),
                source_tier=art.get('source_tier', 2),
                entities=art.get('_entities', []),
            )
            results.append(result)
        return results

    @property
    def stats(self) -> Dict:
        return dict(self._stats)

    # ═══════════════════════════════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════════════════════════════

    def _judge_relevance(self, text: str, source_name: str,
                         source_tier: int) -> bool:
        """判断 AI 相关性

        规则：
        - Tier 0/1 专业源 → 直接相关
        - 强信号关键词命中 1+ → 相关
        - 中等信号关键词命中 2+ → 相关
        - 专业源名单 → 相关
        """
        # Tier 0 一律相关
        if source_tier == 0:
            return True

        # 专业源
        if source_name in _AI_DEDICATED_SOURCES:
            return True

        # 强信号
        strong_hits = (len(_STRONG_KEYWORDS_EN.findall(text)) +
                       len(_STRONG_KEYWORDS_CJK.findall(text)))
        if strong_hits >= 1:
            return True

        # 中等信号
        medium_hits = (len(_MEDIUM_KEYWORDS_EN.findall(text)) +
                       len(_MEDIUM_KEYWORDS_CJK.findall(text)))
        if medium_hits >= 2:
            return True

        # Tier 1 宽松判断（1 个中等信号即可）
        if source_tier <= 1 and medium_hits >= 1:
            return True

        return False

    def _classify(self, text: str) -> List[str]:
        """基于关键词匹配判断分类"""
        scores = {}
        for cat, pattern in _CATEGORY_PATTERNS.items():
            hits = len(pattern.findall(text))
            if hits > 0:
                scores[cat] = hits

        if not scores:
            return ['其他']

        # 取 top 2
        sorted_cats = sorted(scores.items(), key=lambda x: -x[1])
        return [c for c, _ in sorted_cats[:2]]

    def _infer_source_type(self, text: str, source_name: str,
                           source_tier: int) -> str:
        """推断 source_type"""
        # Tier 0 官方源
        if source_tier == 0:
            return 'official'

        # ArXiv → paper
        if 'arxiv' in source_name.lower():
            return 'paper'

        # 模式匹配
        for stype, pattern in _SOURCE_TYPE_PATTERNS.items():
            if pattern.search(text):
                return stype

        return 'news'

    def _estimate_importance(self, text: str, source_name: str,
                             source_tier: int,
                             entities: List[str]) -> int:
        """基于规则估算重要性（1-5 分）

        信号加权：
        - Tier 0: 基础分 3
        - Tier 1: 基础分 2
        - Tier 2: 基础分 1
        - 强信号关键词数量: +0.5/个（上限 1.5）
        - 实体 importance 加成
        - 发布/开源等重大事件词: +1
        - 正文长度加成: 长文通常更重要
        """
        # 基础分
        if source_tier == 0:
            base = 3.0
        elif source_tier == 1:
            base = 2.0
        else:
            base = 1.0

        # 强信号关键词
        strong_count = (len(_STRONG_KEYWORDS_EN.findall(text)) +
                        len(_STRONG_KEYWORDS_CJK.findall(text)))
        base += min(strong_count * 0.5, 1.5)

        # 实体加成
        entity_boost = 0
        for eid in entities:
            entity_boost = max(entity_boost, _ENTITY_IMPORTANCE.get(eid, 0))
        base += entity_boost * 0.5

        # 重大事件词
        major_event = re.compile(
            r'(?i)(?:launch|release|announce|breakthrough|first.ever|record|'
            r'发布|突破|首次|创纪录|正式推出|重大|里程碑)'
        )
        if major_event.search(text):
            base += 0.8

        # 修正范围
        return max(1, min(5, round(base)))

    def _extract_key_sentences(self, title: str, summary: str,
                               full_text: str) -> List[str]:
        """从文本中提取关键句（简化版 TextRank）

        规则：
        1. 从 summary 中取有数据/实体的句子
        2. 从正文前 500 字中取含关键词的句子
        3. 去重，取 top 3
        """
        candidates = []

        # 从 summary 提取
        if summary:
            for sent in self._split_sentences(summary):
                sent = sent.strip()
                if len(sent) < 15:
                    continue
                score = self._sentence_score(sent)
                candidates.append((sent[:80], score))

        # 从正文前 500 字提取
        if full_text:
            for sent in self._split_sentences(full_text[:800]):
                sent = sent.strip()
                if len(sent) < 20:
                    continue
                score = self._sentence_score(sent)
                candidates.append((sent[:80], score))

        # 去重 + 排序
        seen = set()
        unique = []
        for sent, score in candidates:
            key = sent[:30].lower()
            if key not in seen:
                seen.add(key)
                unique.append((sent, score))

        unique.sort(key=lambda x: -x[1])
        return [s for s, _ in unique[:3]]

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        """中英文混合分句"""
        # 中文句号、英文句号+空格、感叹号、问号
        return re.split(r'(?<=[。！？.!?])\s*(?=[^\s])', text)

    @staticmethod
    def _sentence_score(sent: str) -> float:
        """句子价值评分"""
        score = 0.0

        # 含数字 → +2（数据性句子价值高）
        if re.search(r'\d+[%％x倍万亿]|\d+\.\d+', sent):
            score += 2.0

        # 含强信号关键词 → +1
        if _STRONG_KEYWORDS_EN.search(sent) or _STRONG_KEYWORDS_CJK.search(sent):
            score += 1.0

        # 长度适中 → +0.5
        if 30 <= len(sent) <= 80:
            score += 0.5

        # 含引号（引用） → +0.5
        if '"' in sent or '"' in sent or "'" in sent:
            score += 0.5

        return score

    def _gen_chinese_title(self, title: str, source_name: str) -> str:
        """如果标题是英文，生成简短中文标注（非翻译，只是标注）

        规则分析无法做真正翻译，但可以标注来源和关键实体。
        """
        # 检测是否主要是中文
        cjk_count = sum(1 for ch in title if '\u4e00' <= ch <= '\u9fff')
        if cjk_count > len(title) * 0.3:
            return ''  # 已经是中文标题，不需要额外标注

        # 提取已知实体名做标注
        entities_found = []
        entity_labels = {
            'openai': 'OpenAI', 'anthropic': 'Anthropic', 'google': 'Google',
            'deepmind': 'DeepMind', 'meta': 'Meta', 'microsoft': '微软',
            'nvidia': 'NVIDIA', 'deepseek': 'DeepSeek', 'mistral': 'Mistral',
            'hugging': 'HuggingFace', 'apple': 'Apple', 'tesla': 'Tesla',
        }
        lower_title = title.lower()
        for key, label in entity_labels.items():
            if key in lower_title:
                entities_found.append(label)

        # 提取关键产品名
        products = re.findall(
            r'(?i)\b(GPT-?\d\w*|Claude\s*\d*|Gemini\s*\w*|Llama\s*\d*|'
            r'Sora\s*\w*|DALL[-·]?E\s*\d*|Copilot\s*\w*|Cursor\s*\w*)',
            title
        )

        if not entities_found and not products:
            return ''

        parts = entities_found[:2] + products[:1]
        return ' | '.join(parts)

    def _gen_summary(self, title: str, summary: str) -> str:
        """生成简短摘要（优先用 RSS summary，不足时用标题）"""
        if summary and len(summary.strip()) > 20:
            # 取 summary 第一句
            first_sent = self._split_sentences(summary.strip())[0]
            return first_sent[:150]
        return title[:100]


# ═══════════════════════════════════════════════════════════════════════
# 分级分析调度器
# ═══════════════════════════════════════════════════════════════════════

class TieredAnalyzer:
    """分级分析调度器

    Level 0: RuleAnalyzer — 全部条目，零 LLM
    Level 1: LLMAnalyzer（可选）— 候选高价值条目
    Level 2: LLMAnalyzer（可选）— Top N 事件深度摘要

    LLM 不可用时优雅降级到 Level 0。
    """

    def __init__(self, rule_analyzer: RuleAnalyzer,
                 llm_analyzer=None,
                 deep_analysis_top_n: int = 20,
                 llm_candidate_threshold: int = 2):
        """
        Args:
            rule_analyzer: RuleAnalyzer 实例（必需）
            llm_analyzer: LLMAnalyzer 实例（可选，None 则纯规则模式）
            deep_analysis_top_n: 需要深度分析的 Top N 数量
            llm_candidate_threshold: importance >= 此值的候选送 LLM
        """
        self.rule = rule_analyzer
        self.llm = llm_analyzer
        self.top_n = deep_analysis_top_n
        self.llm_threshold = llm_candidate_threshold
        self._llm_available = llm_analyzer is not None

    def analyze_batch(self, articles: List[dict],
                      llm_cache=None) -> List[dict]:
        """分级分析一批文章

        流程：
        1. 全部做 Level 0 规则分析（毫秒级）
        2. 筛选 importance >= threshold 的候选
        3. 候选做 Level 1 LLM 快速摘要（如果 LLM 可用）
        4. Top N 做 Level 2 深度分析（如果 LLM 可用）

        Returns:
            与 articles 等长的分析结果列表
        """
        n = len(articles)
        if n == 0:
            return []

        # ── Level 0: 全量规则分析 ──
        results = self.rule.batch_analyze(articles)

        level0_relevant = sum(1 for r in results if r.get('ai_relevant'))
        log.info("📊 Level 0 规则分析: %d/%d AI 相关", level0_relevant, n)

        if not self._llm_available:
            log.info("⚡ 纯规则模式（LLM 不可用），输出事实页")
            return results

        # ── Level 1: 所有 AI 相关文章送 LLM ──
        # 之前阈值=2会跳过 imp=1 的文章，导致没有 chinese_title 和深度内容
        # 现在：只要 ai_relevant=True 就送 LLM，确保所有展示的文章都有完整分析
        candidates = []
        for i, (art, result) in enumerate(zip(articles, results)):
            if not result.get('ai_relevant'):
                continue
            candidates.append(i)

        if candidates:
            # ── 先吃缓存（串行，SQLite 读无开销）── 剩余候选进 LLM 并发 ──
            pending_indices = []
            for idx in candidates:
                art = articles[idx]
                url = art.get('url', '') or art.get('link', '')
                if llm_cache and url:
                    cached = llm_cache.get(url)
                    if cached:
                        is_relevant = cached.get('ai_relevant', False)
                        has_substance = (
                            cached.get('detailed_content', '').strip()
                            or not is_relevant
                        )
                        if has_substance:
                            results[idx] = cached
                            results[idx]['_analysis_level'] = 1
                            continue
                        log.info("♻️ 缓存命中但 detailed_content 为空，重新分析: %s",
                                 art.get('title', '')[:30])
                        llm_cache.delete(url)
                pending_indices.append(idx)

            if pending_indices:
                max_workers = max(1, getattr(self.llm, 'max_workers', 4))
                log.info("🧠 Level 1 LLM 分析: 缓存命中 %d / 待分析 %d（并发 %d）",
                         len(candidates) - len(pending_indices),
                         len(pending_indices), max_workers)

                import threading
                failure_count = 0
                failure_lock = threading.Lock()
                # 失败率熔断：总失败数 ≥ 3 且 ≥ 30% 则放弃
                MIN_FAILURES = 3
                FAILURE_RATE_CUTOFF = 0.3
                fuse_trigger = max(MIN_FAILURES,
                                   int(len(pending_indices) * FAILURE_RATE_CUTOFF))

                def _analyze_one(idx):
                    art = articles[idx]
                    url = art.get('url', '') or art.get('link', '')
                    try:
                        llm_result = self.llm.analyze_article(
                            title=art.get('title', ''),
                            summary=art.get('summary', ''),
                            full_text=art.get('full_text', ''),
                            source_name=art.get('source_name', ''),
                        )
                        if llm_result and llm_result.get('ai_relevant') is not None:
                            llm_result['_analysis_level'] = 1
                            return idx, llm_result, url
                        return idx, None, url
                    except Exception as e:
                        nonlocal_fail_info = (art.get('title', '')[:30], str(e))
                        return idx, 'ERR', nonlocal_fail_info

                # 识别"配额/限流类错误"的字符串特征（Claude Max 5h window / Anthropic RPM)
                def _is_rate_limit_err(err_msg: str) -> bool:
                    em = (err_msg or '').lower()
                    return any(k in em for k in (
                        '429', 'rate_limit', 'rate limit', 'overloaded',
                        'quota', 'too many requests', 'usage limit',
                    ))

                rate_limit_hit = False
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = {pool.submit(_analyze_one, idx): idx for idx in pending_indices}
                    for fut in concurrent.futures.as_completed(futures):
                        idx, llm_result, extra = fut.result()
                        if llm_result == 'ERR':
                            title30, err = extra
                            with failure_lock:
                                failure_count += 1
                                fc_now = failure_count
                            if _is_rate_limit_err(err):
                                # 命中配额/限流 — 立即熔断，不等累计 → 避免浪费额度
                                if not rate_limit_hit:
                                    rate_limit_hit = True
                                    self._llm_available = False
                                    log.warning("🚨 命中 LLM 限流/配额（%s）— 立即熔断，"
                                                "剩余 %d 条退回规则兜底",
                                                err[:80], len(pending_indices) - fc_now)
                                    # 取消尚未开始的 futures（已执行的无法停）
                                    for f in futures:
                                        if not f.done():
                                            f.cancel()
                                continue
                            log.warning("⚠️ LLM 分析失败 [%s]: %s（累计失败 %d）",
                                        title30, err, fc_now)
                            if fc_now >= fuse_trigger and self._llm_available:
                                self._llm_available = False
                                log.warning("⚠️ 失败率触发熔断（≥%d），LLM 标记为不可用",
                                            fuse_trigger)
                            continue
                        if llm_result is None:
                            continue
                        # 主线程写 results[idx] 与 cache（避免 SQLite 并发写锁争用）
                        results[idx] = llm_result
                        if llm_cache and extra:  # extra = url
                            llm_cache.set(url=extra, result=llm_result)

        # ── 统计 Level 2 候选（深度分析留给 collector.py 按需调用） ──
        level1_count = sum(1 for r in results if r.get('_analysis_level', 0) >= 1)
        log.info("📊 分级分析完成: L0=%d, L1=%d",
                 n - level1_count, level1_count)

        return results

    @property
    def is_llm_available(self) -> bool:
        return self._llm_available
