# Legacy 目录

此目录下的文件属于**前一代（v3）**架构，已被 v4（collector + event_store + briefing_renderer）取代。

保留原因：作为历史参考；以防 v4 出现无法修复的问题时可以临时回退。

## 里面有什么

- `fetch_news.py` — v3 时期的单体 pipeline（抓取 → LLM → 渲染一次跑完）。v4 下已拆成 `collector.py`（采集/分析）与 `briefing_renderer.py`（出报），不再被任何 shell 脚本或 .py 模块 import。

## 为何没把 `html_generator.py` / `rule_analyzer.py` / `entity_coverage.py` / `causal_engine.py` 也移进来

**它们仍在 v4 主流程中被使用**：

- `html_generator.py` 被 `briefing_renderer.py` import（负责 HTML/模板装配）
- `rule_analyzer.py` 被 `collector.py` import（Level 0 规则兜底分析）
- `entity_coverage.py` 被 `collector.py` import（源覆盖度监控）
- `causal_engine.py` 被 `collector.py` import（因果事件匹配）

先前的架构评估曾建议一并归档这四个文件，后经核对 import 关系证实它们仍是主流程的一部分，因此**保留在顶层**。

## 删除建议

`fetch_news.py` 可在下一次整理时直接删除（git history 里永远能找回）；但建议在至少观察一个季度、确认无运维脚本/个人备忘录引用后再删。
