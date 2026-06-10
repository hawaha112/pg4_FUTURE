#!/usr/bin/env python3
"""scorecard_update.py — 判断记分牌: 每周复盘过去的「今日判断」,公开对账。

用法: python3 scorecard_update.py <deploy_repo_dir>   (weekly-report.yml 在部署仓 clone 后调用)

数据流(全在部署仓 archive/data/ 下, 由 archive_appender.py 日常累积):
  judgments.jsonl   每班 3 个判断(date/shift/title/body/evidence_titles)
  events-*.jsonl    全量事件归档 → 提供"判断之后实际发生了什么"的证据
  scorecard.jsonl   裁决历史(本脚本追加)
  scorecard.json    聚合(总分/正确率/最近裁决) → 仪表盘前端 fetch 渲染

裁决: 取 5-45 天前、未裁决的判断(每轮 ≤12 条), 把"判断后发生的事件标题"喂给 LLM
一次性批量裁决: correct / partially / wrong / unverifiable + 一句理由 + 关键证据。
- 这是产品的信任资产: 敢公开自己判断的对错, 没有竞品做这个。
- LLM 经 claude_proxy(weekly workflow 已起); 失败安全退出, 不挡周报。
"""
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
MIN_AGE_DAYS = 5      # 太新没法验证
MAX_AGE_DAYS = 45     # 太老的事件归档喂不全, 标 unverifiable 也无意义 → 跳过不裁
MAX_PER_RUN = 12      # 每轮裁决上限(控 token)
MAX_EVIDENCE_LINES = 150

VERDICT_LABELS = {'correct': '✅ 对了', 'partially': '🟡 部分对', 'wrong': '❌ 错了',
                  'unverifiable': '⚪ 无法验证'}


def log(msg):
    print(f"[scorecard] {msg}", flush=True)


def _read_jsonl(path: Path):
    rows = []
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _jkey(j):
    return f"{j.get('date')}|{j.get('shift')}|{j.get('title')}"


def main() -> int:
    if len(sys.argv) < 2:
        log("用法: scorecard_update.py <deploy_repo_dir>")
        return 0
    data_dir = Path(sys.argv[1]) / 'archive' / 'data'
    if not (data_dir / 'judgments.jsonl').exists():
        log("无 judgments.jsonl(归档尚未积累), 跳过")
        return 0

    judgments = _read_jsonl(data_dir / 'judgments.jsonl')
    scored = {_jkey(r) for r in _read_jsonl(data_dir / 'scorecard.jsonl')}
    today = datetime.now(timezone.utc).date()

    pending = []
    for j in judgments:
        try:
            jd = datetime.strptime(j.get('date', ''), '%Y-%m-%d').date()
        except ValueError:
            continue
        age = (today - jd).days
        if MIN_AGE_DAYS <= age <= MAX_AGE_DAYS and _jkey(j) not in scored:
            pending.append((jd, j))
    pending.sort(key=lambda x: x[0])
    pending = pending[:MAX_PER_RUN]
    if not pending:
        log("无待裁决判断(都太新/已裁决), 跳过")
        _write_aggregate(data_dir)   # 仍刷新聚合(幂等)
        return 0
    earliest = pending[0][0]
    log(f"待裁决 {len(pending)} 条 (最早 {earliest})")

    # ── 证据: 严格"判断发表日之后"的事件(带日期戳, prompt 同时要求 LLM 按各判断
    # 的日期自行只采信其后的事件)。⚠️ 不能含发表当天 —— 当天归档事件正是该判断
    # evidence_ids 的来源, 喂回去等于"用判断的依据自证判断", 正确率会系统性虚高。
    events = []
    for p in sorted(data_dir.glob('events-*.jsonl')):
        events.extend(_read_jsonl(p))
    ev_lines = []
    for e in events:
        d = (e.get('archived_from') or '')[:10]
        try:
            ed = datetime.strptime(d, '%Y-%m-%d').date()
        except ValueError:
            continue
        if ed > earliest:   # 严格晚于最老判断日(各判断的精确过滤交给 LLM 按日期戳执行)
            ev_lines.append((-(e.get('importance') or 0), d,
                             f"[{d}] {(e.get('chinese_title') or e.get('title') or '')[:70]}"))
    ev_lines.sort()
    evidence_txt = '\n'.join(x[2] for x in ev_lines[:MAX_EVIDENCE_LINES])

    jud_txt = '\n\n'.join(
        f"[{i}] (发表于 {jd} {j.get('shift','')}) {j.get('title','')}\n{(j.get('body') or '')[:300]}"
        for i, (jd, j) in enumerate(pending))

    prompt = (
        "你是新闻复盘裁判。下面是我们过去发表的若干「判断」(含可证伪的预测/论断), "
        "以及带日期戳的真实事件清单。请逐条裁决每个判断是否成立。\n\n"
        "⚠️ 硬规则: 每个判断只能采信【严格晚于它发表日期】的事件做证据 —— "
        "发表当天及之前的事件是它的写作素材, 不能用来印证它自己。\n\n"
        "裁决标准:\n"
        "- correct: 核心论断/预测被发表之后的事件明确印证\n"
        "- partially: 方向对但程度/时间/主体有偏差\n"
        "- wrong: 被发表之后的事件证伪\n"
        "- unverifiable: 晚于发表日的事件不足以验证(宁可标这个, 不要硬judge)\n\n"
        f"【待裁决判断(各自带发表日期)】\n{jud_txt}\n\n"
        f"【真实事件清单(行首为发生日期)】\n{evidence_txt}\n\n"
        "只输出 JSON 数组, 不要数组外的任何文字/代码围栏, 每个判断一项, 按编号顺序:\n"
        '[{"id": 0, "verdict": "correct|partially|wrong|unverifiable", '
        '"reason": "一句话理由(≤60字, 嵌套引用用「」不用ASCII双引号)", '
        '"evidence": "支撑裁决的关键事件标题(无则空串, 同样不用ASCII双引号)"}]'
    )

    def _parse_array(text):
        """数组感知解析: _extract_json 的修复路径只认 {...}, 对数组会整批丢弃。"""
        t = (text or '').strip()
        if t.startswith('```'):
            t = t.split('```', 2)[1] if t.count('```') >= 2 else t.strip('`')
            if t.startswith('json'):
                t = t[4:]
        s, e = t.find('['), t.rfind(']')
        if s >= 0 and e > s:
            try:
                return json.loads(t[s:e + 1])
            except json.JSONDecodeError:
                pass
        return None

    try:
        with open(SCRIPT_DIR / 'config.json', encoding='utf-8') as f:
            config = json.load(f)
        from llm_analyzer import create_analyzer_from_config
        llm = create_analyzer_from_config(config)
        resp = llm._call_api([{"role": "user", "content": prompt}])
        verdicts = _parse_array(resp)
        if verdicts is None:
            verdicts = llm._extract_json(resp)
        if not isinstance(verdicts, list):
            # 一次严格重试(本仓 LLM JSON 已知惯犯: 前导文字/值内 ASCII 双引号)
            log("⚠️ 裁决解析失败, 严格重试一次")
            resp2 = llm._call_api([{"role": "user", "content":
                prompt + "\n\n⚠️ 上次输出无法解析。严格要求: 直接输出 JSON 数组本身, "
                "第一个字符必须是 [, 最后一个是 ]; 字符串值内禁止 ASCII 双引号。"}])
            verdicts = _parse_array(resp2)
    except Exception as e:
        log(f"⚠️ LLM 裁决失败(下周再试): {e}")
        return 0
    if not isinstance(verdicts, list):
        log(f"⚠️ 裁决返回非数组(下周再试): {str(verdicts)[:120]}")
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()
    rows = []
    seen_ids = set()
    for v in verdicts:
        try:
            i = int(v.get('id'))
        except (TypeError, ValueError, AttributeError):
            continue
        if not (0 <= i < len(pending)) or i in seen_ids:
            continue
        seen_ids.add(i)
        verdict = str(v.get('verdict', '')).strip().lower()
        if verdict not in VERDICT_LABELS:
            continue
        jd, j = pending[i]
        rows.append({
            'date': j.get('date'), 'shift': j.get('shift'),
            'title': j.get('title'), 'emoji': j.get('emoji', ''),
            'verdict': verdict,
            'reason': str(v.get('reason', ''))[:120],
            'evidence': str(v.get('evidence', ''))[:90],
            'scored_at': now_iso,
        })
    if not rows:
        log("⚠️ 没有有效裁决行, 跳过写入")
        return 0
    with open(data_dir / 'scorecard.jsonl', 'a', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    log(f"✅ 本轮裁决 {len(rows)} 条: " + ' '.join(
        f"{VERDICT_LABELS[r['verdict']].split()[0]}" for r in rows))
    _write_aggregate(data_dir)
    return 0


def _write_aggregate(data_dir: Path):
    """scorecard.jsonl → scorecard.json 聚合(仪表盘前端直接 fetch)。幂等。"""
    rows = _read_jsonl(data_dir / 'scorecard.jsonl')
    if not rows:
        return
    cnt = {'correct': 0, 'partially': 0, 'wrong': 0, 'unverifiable': 0}
    for r in rows:
        if r.get('verdict') in cnt:
            cnt[r['verdict']] += 1
    decidable = cnt['correct'] + cnt['partially'] + cnt['wrong']
    accuracy = round((cnt['correct'] + 0.5 * cnt['partially']) / decidable * 100) if decidable else 0
    rows.sort(key=lambda r: (r.get('date') or '', r.get('shift') or ''), reverse=True)
    agg = {
        'total_scored': len(rows),
        'counts': cnt,
        'accuracy_pct': accuracy,
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'recent': [{k: r.get(k, '') for k in
                    ('date', 'shift', 'emoji', 'title', 'verdict', 'reason', 'evidence')}
                   for r in rows[:14]],
    }
    (data_dir / 'scorecard.json').write_text(
        json.dumps(agg, ensure_ascii=False, indent=1), encoding='utf-8')
    log(f"📊 记分牌: {agg['total_scored']} 条已裁 · 正确率 {accuracy}% "
        f"(✅{cnt['correct']} 🟡{cnt['partially']} ❌{cnt['wrong']} ⚪{cnt['unverifiable']})")


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:   # 不挡周报
        log(f"⚠️ 未捕获异常(跳过记分牌): {e}")
        sys.exit(0)
