#!/usr/bin/env python3
"""archive_appender.py — 把本班次的结构化归档(事件+判断)追加进部署仓 archive/data/。

用法: python3 archive_appender.py <archive_payload.json> <deploy_repo>/archive/data

产物(全部 git 友好的追加型纯文本, 永久保存——突破 events.db 30 天清理上限):
  events-YYYY-MM.jsonl    每行一个事件(全字段), 按 canonical_event_id/event_id 幂等去重
  search-YYYY-MM.jsonl    跨期搜索极简索引 {i,d,t,k,m}(id/日期班次/标题/域/重要度)
  judgments.jsonl         每班 3 个判断(含证据标题), 供"判断记分牌"每周复盘
  manifest.json           已有月份清单, 前端搜索按月懒加载

幂等: 同一 payload 重复执行不产生重复行(部署重试/手动补跑安全)。
任何失败 exit 0 —— 归档是增值件, 不挡部署。由 run_daily.sh 部署块在 clone 之后调用。
"""
import json
import sys
from pathlib import Path


def log(msg):
    print(f"[archive_appender] {msg}", flush=True)


def _load_keys(path: Path, key_fn):
    keys = set()
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                keys.add(key_fn(json.loads(line)))
            except (json.JSONDecodeError, KeyError):
                continue
    return keys


def _append_lines(path: Path, rows):
    if not rows:
        return
    with open(path, 'a', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')


def main() -> int:
    if len(sys.argv) < 3:
        log("用法: archive_appender.py <payload.json> <data_dir>")
        return 0
    payload_path, data_dir = Path(sys.argv[1]), Path(sys.argv[2])
    if not payload_path.exists():
        log(f"无 payload({payload_path}), 跳过")
        return 0
    try:
        payload = json.loads(payload_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as e:
        log(f"⚠️ payload 解析失败: {e}")
        return 0

    date = (payload.get('date') or '').strip()          # YYYY-MM-DD
    shift = (payload.get('shift') or '').strip()
    if not date or len(date) < 7:
        log("⚠️ payload 缺 date, 跳过")
        return 0
    month = date[:7]
    dkey = f"{date}-{shift}" if shift else date
    data_dir.mkdir(parents=True, exist_ok=True)

    def _ev_key(e):
        return e.get('canonical_event_id') or e.get('event_id') or e.get('link', '')

    # ── 1. 事件全量 (按事件 id 幂等; 跨班重复出现的老事件不重复入档) ──
    # 上月 key 也纳入去重: 月末首报、次月仍有新进展重渲染的事件, 不在新月份重复入档
    ev_path = data_dir / f'events-{month}.jsonl'
    seen = _load_keys(ev_path, _ev_key)
    try:
        y, m = int(month[:4]), int(month[5:7])
        prev_month = f'{y - 1}-12' if m == 1 else f'{y}-{m - 1:02d}'
        seen |= _load_keys(data_dir / f'events-{prev_month}.jsonl', _ev_key)
    except ValueError:
        pass
    new_events, new_search = [], []
    for e in payload.get('events') or []:
        k = _ev_key(e)
        if not k or k in seen:
            continue
        seen.add(k)
        e['archived_from'] = dkey
        new_events.append(e)
        new_search.append({
            'i': k,
            'd': dkey,
            't': (e.get('chinese_title') or e.get('title') or '')[:90],
            'k': e.get('topic_domain_key') or 'other',
            'm': e.get('importance', 0),
        })
    _append_lines(ev_path, new_events)

    # ── 2. 搜索索引 (与事件同 key 同步追加) ──
    se_path = data_dir / f'search-{month}.jsonl'
    se_seen = _load_keys(se_path, lambda r: r.get('i', ''))
    _append_lines(se_path, [r for r in new_search if r['i'] not in se_seen])

    # ── 3. 判断 (按 日期+班次+标题 幂等) ──
    ju_path = data_dir / 'judgments.jsonl'
    ju_seen = _load_keys(ju_path, lambda r: f"{r.get('date')}|{r.get('shift')}|{r.get('title')}")
    new_juds = []
    for j in payload.get('judgments') or []:
        title = (j.get('title') or '').strip()
        if not title:
            continue
        key = f"{date}|{shift}|{title}"
        if key in ju_seen:
            continue
        ju_seen.add(key)
        new_juds.append({
            'date': date, 'shift': shift,
            'emoji': j.get('emoji', ''),
            'title': title,
            'body': (j.get('body') or '').strip(),
            'evidence_titles': j.get('evidence_titles') or [],
        })
    _append_lines(ju_path, new_juds)

    # ── 4. manifest (月份清单, 前端按月懒加载) ──
    months = sorted({p.name[len('search-'):-len('.jsonl')]
                     for p in data_dir.glob('search-*.jsonl')}, reverse=True)
    (data_dir / 'manifest.json').write_text(
        json.dumps({'months': months, 'updated': dkey}, ensure_ascii=False),
        encoding='utf-8')

    log(f"✅ {dkey}: +{len(new_events)} 事件, +{len(new_juds)} 判断 (月份 {months})")
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:   # 不挡部署
        log(f"⚠️ 未捕获异常(跳过归档): {e}")
        sys.exit(0)
