#!/usr/bin/env python3
"""多音字/专名读音修正 — 给 Kokoro(misaki)TTS 用。

misaki.zh 的定音路径 = jieba 分词 → 每词 pypinyin.lazy_pinyin(TONE3)。
所以修一个多音字读音要两步:
  1. jieba.add_word(词)         —— 保证关键短语不被切碎(切碎了短语词典不命中);
  2. pypinyin.load_phrases_dict(词→读音) —— 强制该词读音。
两步都作用于全局, misaki 随后调用 lazy_pinyin 时即生效(已端到端验证到 ZHG2P 音素)。

只收"实测被 pypinyin+jieba 读错"的词(2026-06-14 用户反馈多音字), 不过度覆盖——
pypinyin 本身相当准(角色 jué sè、模样 mú yàng、量化 liàng、黄仁勋 xūn、部署 bù shǔ
都正确), 乱加反而可能把对的读错。

新词热加: 在同目录放 pronounce_fixes.json, 格式 {"词": "带调拼音 空格分隔"},
例 {"长尾词": "cháng wěi cí"}。无需改代码、无需重新部署逻辑。
"""
import json
from pathlib import Path

# 词 → 带调拼音(空格分隔, 每字一个音节)。均经 misaki 路径实测"读错→读对"验证。
_BUILTIN_FIXES = {
    # 调: 微调=fine-tune 读 tiáo(pypinyin 误作 diào)
    '微调': 'wēi tiáo',
    # 切换=switch 读 qiē(误作 qiè)
    '切换': 'qiē huàn',
    # 重X: "重新做"义读 chóng(pypinyin 短语词典缺这几个, 误作 zhòng)
    '重置': 'chóng zhì',
    '重训': 'chóng xùn',
    '重跑': 'chóng pǎo',
    # 长X: "长度大/时间久"义读 cháng; jieba 常把"长"单独切出 → 默认误读 zhǎng
    '长上下文': 'cháng shàng xià wén',
    '超长上下文': 'chāo cháng shàng xià wén',
    '长文本': 'cháng wén běn',
    '长序列': 'cháng xù liè',
    '长链': 'cháng liàn',
    '长尾': 'cháng wěi',
    '长视频': 'cháng shì pín',
    '长难句': 'cháng nán jù',
}

_FIXES_JSON = Path(__file__).parent / 'pronounce_fixes.json'
_applied = False


def _to_phrase_entry(pinyin_str):
    """"wēi tiáo" → [['wēi'], ['tiáo']](pypinyin load_phrases_dict 入参格式)。"""
    syl = [s for s in str(pinyin_str).split() if s]
    return [[s] for s in syl] if syl else None


def _merge_external(fixes):
    """合并 pronounce_fixes.json(用户在此加词, 同名覆盖内置)。失败忽略。"""
    if not _FIXES_JSON.exists():
        return fixes
    try:
        with open(_FIXES_JSON, 'r', encoding='utf-8') as f:
            ext = json.load(f)
        if isinstance(ext, dict):
            fixes = {**fixes, **{str(k): str(v) for k, v in ext.items()}}
    except Exception:
        pass
    return fixes


def apply_pronunciation_fixes(log=None):
    """把读音修正灌进 pypinyin 短语词典 + jieba 用户词典。

    幂等(本进程内重复调用只生效一次), 任何失败都不抛(TTS 不应因读音修正崩)。
    必须在 misaki ZHG2P 实际调用之前执行。返回生效条数。
    """
    global _applied
    if _applied:
        return 0
    try:
        import jieba
        from pypinyin import load_phrases_dict
    except Exception as e:
        if log:
            log(f"⚠️ 读音修正依赖缺失({e}), 跳过")
        return 0

    fixes = _merge_external(dict(_BUILTIN_FIXES))
    phrase_dict = {}
    for word, py in fixes.items():
        entry = _to_phrase_entry(py)
        if not entry or len(word) < 2:   # pypinyin 短语词典要求 ≥2 字
            continue
        phrase_dict[word] = entry
        try:
            jieba.add_word(word)   # 保证整词不被切碎, 短语读音才命中
        except Exception:
            pass
    if not phrase_dict:
        return 0
    try:
        load_phrases_dict(phrase_dict)
    except Exception as e:
        if log:
            log(f"⚠️ 读音修正写入失败({e})")
        return 0
    _applied = True
    if log:
        log(f"🗣️ 多音字读音修正: {len(phrase_dict)} 条已加载")
    return len(phrase_dict)
