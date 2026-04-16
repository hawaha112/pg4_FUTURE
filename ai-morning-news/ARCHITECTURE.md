# AI Morning News — 架构说明 (v4)

> 本文档描述当前生产路径。写作时间 2026-04-17。如果 `run_daily.sh` 或 `run_collect.sh` 的编排逻辑有变，请同步更新。

## 一、数据流（生产路径）

```
Feeds (config.json.sources, ~40 active)
        │
        │  run_collect.sh (每 2h, launchd)
        │  run_daily.sh   (每日 06:30, launchd)
        ▼
┌─────────────────────────────────────────────────────────┐
│  collector.py                                           │
│    ├── content_fetcher.fetch_feed                       │
│    │     └── extractors/{article,youtube,special}.py    │
│    ├── SourceHealthTracker   (health_tracker.py)        │
│    ├── DedupEngine           (dedup_engine.py)          │
│    │     — MinHash/LSH + TF-IDF；                       │
│    │       config.dedup.threshold_en / threshold_cjk    │
│    ├── RuleAnalyzer / TieredAnalyzer (rule_analyzer.py) │
│    │     — Level 0 规则兜底分析                          │
│    ├── LLMAnalyzer           (llm_analyzer.py)          │
│    │     — Level 1 LLM 深度分析（Claude Sonnet）         │
│    ├── CausalKB              (causal_engine.py)         │
│    │     — 因果事件匹配                                   │
│    ├── EntityCoverageMatrix  (entity_coverage.py)       │
│    │     — 源覆盖度监控                                   │
│    └── EventStore.save       (event_store.py)           │
│                                                         │
└─────────────────────────────────────────────────────────┘
        │
        ▼
events.db  (SQLite, WAL)
  表：articles / canonical_events / evidence / collection_runs
  状态：rumor → reported → confirmed → official
        │
        │  run_daily.sh (每日)
        ▼
┌─────────────────────────────────────────────────────────┐
│  briefing_renderer.py                                   │
│    ├── EventStore.get_canonical_events_for_briefing     │
│    ├── _has_substance 质量过滤                          │
│    ├── LLMAnalyzer.generate_digest  (今日速览)          │
│    ├── SourceRanker.score_and_filter / enrich_cluster   │
│    └── html_generator.generate_html                     │
│           — 页面装配 + modal_data 组装                   │
└─────────────────────────────────────────────────────────┘
        │
        ▼
output/index.html + output/modal_data.js + output/stats.json
        │
        │  run_daily.sh: 拷贝到仓库根 + git push 到 **部署仓库**
        ▼
hawaha112/ai-morning-briefing (公开仓库)
  根目录: index.html + modal_data.js + archive/ + stats.json
  .github/workflows/deploy.yml（本仓库无副本；workflow 只存在于部署仓库）
        │
        │  push to main → deploy.yml 自动触发
        ▼
GitHub Pages (build_type: workflow)
https://hawaha112.github.io/ai-morning-briefing/
        │
        ▼
读者

备注：本项目（hawaha112/pg4_FUTURE）是私有开发仓库，不承载
页面，只保留源代码。页面部署走独立的公开仓库 ai-morning-briefing。
deploy_now.sh 负责把 output/ 的产物 push 到部署仓库，GitHub
Actions 负责把 push 发布到 Pages。
```

## 二、每个环节的"退化模式"与读者感知

| 环节 | 故障模式 | 读者感知 | 防御机制（现有） |
|---|---|---|---|
| Feeds 抓取 | SSL / 403 / feed 下线 | 当日条目少 | `health_tracker` 连续失败 ≥10 次自动跳过；`config.settings.auto_disable_sources=true` 可在 ≥20 次后自动禁用 |
| Dedup | 中英混合标题阈值不准 | 同事件出现 2 次 或 两件事被合并 | 双阈值 `threshold_en=0.60` / `threshold_cjk=0.50`；可在 `config.dedup` 调 |
| LLMAnalyzer | 代理未启动 / 超时 | 页面在渲染规则兜底内容 | 页首 banner（覆盖率 < 50%）+ Telegram 告警（`run_daily.sh`） |
| EventStore | SQLite corruption | 当日出不来报 | 启动时 `PRAGMA integrity_check`，失败自动从 `events.db.bak.YYYYMMDD` 恢复；`run_daily.sh` 成功后滚动备份 7 份 |
| Clustering | 同一事件被拆成多个 cluster | 重复卡片 | 无（目前靠 dedup 兜底） |
| Rendering | Template 占位符缺失 | 页面空白 | `string.Template.safe_substitute` 不抛异常 |
| Deploy | rebase 冲突 / push 失败 | 读者看到昨天的版本 | `run_daily.sh` 自动修复 `.git/rebase-merge`；建议改用 `.github/workflows/deploy.yml`（当前可选） |

## 三、两大抽象层次

读者视角上有两层实体：

- **article (evidence)** — 一篇原始报道，存于 `articles` 表
- **canonical_event** — 一个"新闻事件"，存于 `canonical_events` 表，下面挂多篇 `evidence`

```
canonical_event  1 ─── N  evidence (articles)
      │                        │
      ▼                        ▼
  status / importance      source / url / collected_at
  cluster_size             role: first_reporter/confirmer/official/follower
  evidence_chain
```

前端渲染用的是 canonical_event 层，但 LLM 分析的是 evidence 层。`briefing_renderer._enrich_item_with_event_info` 负责在出报时把两层 merge 成一个 item。

## 四、哪些文件是主流程，哪些是辅助

**主流程模块（改这些会直接影响产出）**：

```
collector.py                  # 采集 + 分析主入口
briefing_renderer.py          # 出报入口
event_store.py                # SQLite schema + 所有 event/evidence 操作
html_generator.py             # HTML 模板装配
llm_analyzer.py               # LLM 调用 + JSON 校验 + 智能截断
dedup_engine.py               # 去重
content_fetcher.py + extractors/*   # RSS / YouTube / 特殊源
source_ranker.py              # 排序
rule_analyzer.py              # Level 0 规则兜底
causal_engine.py              # 因果事件匹配
entity_coverage.py            # 源覆盖度监控
health_tracker.py             # 源健康度追踪
http_client.py                # SSL/HTTP 工具（certifi 降级）
config_validator.py           # 配置校验
exceptions.py, logger.py, checkpoint.py
prompts/*.txt                 # LLM prompt
templates/{page.html,style.css,script.js}  # 前端模板
```

**配置与运维**：

```
config.json                   # 主配置：sources / llm / dedup / settings
run_daily.sh                  # 每日 06:30 编排
run_collect.sh                # 每 2h 采集编排
com.hawaha.ai-*.plist         # launchd 定时任务
deploy_now.sh                 # 本地 push 部署（可被 .github/workflows/deploy.yml 替代）
```

**遗留 / 归档**：

```
legacy/fetch_news.py          # v3 单体 pipeline，已被 collector + renderer 取代
```

## 五、数据库与状态

```
events.db      # 事件库（canonical_events + evidence + articles），主力
dedup.db       # 去重引擎的 MinHash 签名 + IDF 缓存
llm_cache.db   # LLM 响应缓存（按 URL + content hash 的 TTL）
source_health.json  # 源健康度（plain JSON）
history.json   # 采集 cursor（哪条已看过）
output/stats.json   # 每日产出摘要（被 run_daily.sh 读入 Telegram 消息）
```

备份策略：`run_daily.sh` 在部署成功后拷贝 `events.db/dedup.db/llm_cache.db` 到 `*.bak.YYYYMMDD`，保留 7 天（自动清理）。`EventStore` 启动时 `PRAGMA integrity_check`，失败自动寻找最近的 `.bak.*` 恢复。

## 六、运维检查清单

日常查看页面有异常时，按顺序排查：

1. `tail -n 100 daily_run.log | grep RUN_SUMMARY` — 看最近一次的结构化质量摘要行
2. `stats.json` — 看 `llm_coverage` / `article_count` / `multi_source_count`
3. `source_health.json` — 看哪些源连续失败
4. `launchctl list | grep hawaha` — 看定时任务的最后退出码
5. `output/index.html` vs `index.html` — 如果两者不一致，说明 deploy 环节没同步
