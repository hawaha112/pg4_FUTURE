# AI Morning Briefing — 项目介绍与优化建议

## 一句话概括

这是一个**全自动 AI 新闻早报生成器**：每天早上 7 点自动从 30+ 个信息源抓新闻，用 AI 分析筛选，生成一个好看的网页日报，部署到 GitHub Pages，再通过 Telegram 推送给你。

---

## 它做了什么？

想象你每天早上要花 1 小时刷各种科技网站了解 AI 动态。这个项目替你做了这件事：

1. **抓新闻**：同时从 OpenAI 博客、Anthropic、Google AI、The Verge、ArXiv 论文、Hacker News、YouTube 频道、36氪、量子位、知乎热榜、Twitter/X 大佬等 30+ 个渠道拉 RSS，还支持 YouTube 视频和 Twitter 推文
2. **去重**：用"URL 哈希 + 标题哈希 + MinHash 语义去重"三层机制，同一件事被 5 家媒体报道只保留最优质的那条
3. **AI 分析**：把每篇文章喂给 Claude，让它判断是否 AI 相关、翻译成中文标题、写摘要、打重要性评分（1-5 分）、生成深度解读
4. **排序**：根据来源权威度（OpenAI 官方博客 5 分 > Hacker News 2 分）、重要性评分、时间新鲜度综合排序
5. **生成页面**：输出一个暗色主题的响应式 HTML 页面，支持搜索、分类筛选、点击展开看详细分析
6. **部署通知**：自动 push 到 GitHub Pages，发 Telegram 消息提醒你看

---

## 技术架构一览

```
定时触发 (macOS launchd, 每天 7:00)
    │
    ▼
run_daily.sh ─── 启动 Claude Max API 代理 (本地 :3456)
    │
    ▼
fetch_news.py ── 主流程编排
    ├── content_fetcher.py  ── RSS 抓取 + 全文提取（1316 行，最大的文件）
    ├── rss_parser.py       ── RSS/Atom XML 解析
    ├── dedup_engine.py     ── MinHash/LSH + TF-IDF 两层去重（SQLite 持久化）
    ├── llm_analyzer.py     ── LLM 分析（支持 OpenAI / Anthropic API）
    ├── causal_engine.py    ── 因果分析知识库匹配（事件→受影响资产）
    ├── source_ranker.py    ── 来源信誉评分 + 综合排序
    ├── html_generator.py   ── HTML 页面生成（模板在 templates/）
    └── checkpoint.py       ── 断点续跑（LLM 分析中途失败可恢复）
    │
    ▼
output/index.html ── 最终日报页面
    │
    ▼
GitHub Pages 部署 + Telegram 通知
```

**关键设计选择**：

- **纯标准库**：Python 部分不依赖第三方包（没有 requests、没有 feedparser），全靠 urllib + xml.etree
- **本地 LLM 代理**：通过 `claude_proxy.py` 把 Claude Max 订阅额度包装成 OpenAI 兼容 API，省钱
- **断点续跑**：LLM 分析是最贵最慢的步骤，checkpoint 机制保证中途断了不用从头来
- **SQLite 持久化去重**：跨天去重，不会重复推送昨天已经报过的新闻

---

## 代码量分布

| 模块 | 行数 | 职责 |
|------|------|------|
| content_fetcher.py | 1316 | RSS 抓取 + 全文提取（最复杂） |
| llm_analyzer.py | 889 | LLM 调用 + 结果解析 |
| dedup_engine.py | 595 | 去重引擎 |
| fetch_news.py | 436 | 主流程编排 |
| causal_engine.py | 366 | 因果知识库 |
| source_ranker.py | 278 | 排序评分 |
| html_generator.py | 252 | 页面生成 |
| claude_proxy.py | 243 | LLM API 代理 |
| **合计** | **~4800** | |

---

## 可以优化的地方

### 1. content_fetcher.py 太大了，建议拆分

这个文件 1316 行，承担了太多职责：SSL 处理、HTTP 请求、RSS 特殊源适配（YouTube / Twitter / 知乎 / 小红书 / Hacker News / GitHub）、全文提取、图片提取、源健康度监控。建议拆成：

- `http_client.py` — SSL + HTTP 请求工具
- `extractors/` 目录 — 每种特殊源一个文件（youtube.py、twitter.py、zhihu.py 等）
- `health_tracker.py` — 源健康度监控

这样每个文件职责单一，改 YouTube 抓取逻辑时不用担心碰到知乎的代码。

### 2. 错误处理可以更优雅

目前很多地方用 broad `except Exception as e` 捕获所有异常然后 `log.warning` 跳过。这在生产环境中会吞掉真正需要关注的错误。建议：

- 区分"预期内的网络错误"（超时、404）和"意外的程序 bug"
- 对预期错误用具体异常类型捕获
- 对意外错误至少记录完整 traceback

### 3. LLM Prompt 硬编码在 Python 文件里

`llm_analyzer.py` 里的系统 prompt 有将近 80 行，直接嵌在代码中。这意味着每次调整 prompt（比如换分析格式、加字段）都要改 Python 文件。建议把 prompt 模板移到外部文件（如 `prompts/analyze.txt`），方便迭代测试。

### 4. 缺少配置校验

`config.json` 是核心配置文件，但代码里没有对配置做任何校验。如果有人写错了字段名（比如 `ai_olny` 拼错），程序不会报错，只会静默用默认值。建议在启动时加一个简单的 config schema 校验。

### 5. 去重阈值可以自适应

当前语义去重的阈值硬编码为 0.60。对于中文和英文混合内容，这个阈值未必都合适。中文两篇讲同一件事的文章，因为用词差异可能相似度只有 0.55；而英文可能 0.70 才算重复。可以考虑按语言分别设阈值。

### 6. 并发控制可以更精细

RSS 抓取用了 `ThreadPoolExecutor(max_workers=8)` 并发，但 LLM 分析只有 `max_workers=2`。问题在于：

- 对同一个网站（比如 rsshub.rssforever.com 承载了多个中文源）的并发请求没有做限流，容易被对方 ban IP
- 建议加一个 per-host 的速率限制（比如同一个域名间隔 1 秒）

### 7. 日志文件会无限增长

`daily_run.log` 只是追加写入，没有 rotation 机制。跑几个月后这个文件会变得很大。建议加一个简单的日志轮转（比如保留最近 7 天）。

### 8. 测试覆盖可以补充

目前有 4 个测试文件，覆盖了去重引擎、LLM 分析器、RSS 解析器和来源排序。但以下关键路径缺少测试：

- `content_fetcher.py`（最大最复杂的文件反而没有测试）
- `html_generator.py`
- `fetch_news.py` 的主流程集成测试
- 端到端测试（给定固定 RSS 输入，验证最终 HTML 输出）

### 9. GitHub Pages 部署方式可以简化

当前的部署方式是：clone 远程仓库到临时目录 → 复制文件 → commit → push。这个流程有点脆弱（日志里也看到了处理 rebase 卡住的修复代码）。可以考虑改用 GitHub Actions：本地只需 push 源码，CI 自动构建部署，更可靠也更标准。

### 10. 小红书 / 机器之心等失效源的处理

config 里有好几个源被标记为 `enabled: false`（小红书、机器之心、新智元、Meta AI）。这些注释说明了失效原因，但没有自动检测和告警机制。建议 `SourceHealthTracker` 在连续失败 N 次后自动跳过并发通知，而不是靠人手动 disable。
