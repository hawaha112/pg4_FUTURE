# AI Morning Briefing

每天早上 06:30 自动从 37 个 RSS 源（22 英文 + 15 中文）抓取 AI 相关新闻，通过 LLM 分析、事件聚类后生成精美的 HTML 早报页面。

## 工作流程

```
RSS 源 (37 个)
    │
    ├─ collector.py  (每 2h, launchd)   ── 抓取 → 全文提取 → 去重 → LLM 分析 → 事件聚类
    │      │
    │      ▼
    │   events.db (canonical_events + evidence + articles)
    │      │
    └─ briefing_renderer.py  (每日 06:30, launchd) ── 出报 → HTML
           │
           ▼
       output/index.html ── 推送至部署仓库 ── GitHub Pages ── Telegram 通知
```

详细数据流与模块边界见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 快速开始

主流程被拆为两个独立入口：采集（collector.py）+ 出报（briefing_renderer.py），由 `run_daily.sh` 统一编排。

```bash
# 一键完整流程（采集 + 出报 + 部署）
bash run_daily.sh

# 仅采集，不生成早报页面
python3 collector.py
python3 collector.py --no-llm           # 跳过 LLM 分析（更快，无需 API）
python3 collector.py --tier 0,1         # 仅采集指定层级的源

# 仅出报（基于现有 events.db）
python3 briefing_renderer.py
python3 briefing_renderer.py --open     # 生成并在浏览器中打开
python3 briefing_renderer.py --hours 12 # 只取最近 12 小时的事件
```

> 注：v3 之前的单体入口 `fetch_news.py` 已迁至 [legacy/](legacy/)，仅作历史保留。

## 配置

编辑 `config.json` 自定义：
- **LLM 设置**：API 端点、模型、认证方式（支持任何 OpenAI 兼容 API）
- **RSS 源**：添加/删除源，每个源包含名称、URL、分类、权威度权重
- **通用设置**：每源最大条目数、文章时效、输出路径

## 定时任务

使用 macOS launchd 调度（`install_launchd.sh` 会自动适配当前路径）：

```bash
bash install_launchd.sh
```

## 敏感配置

Telegram token 等信息存放在外部文件中，不会进入版本控制：

```bash
mkdir -p ~/.config/ai-briefing
cat > ~/.config/ai-briefing/.env << 'EOF'
TG_BOT_TOKEN="your_bot_token"
TG_CHAT_ID="your_chat_id"
BRIEFING_URL="https://your-username.github.io/ai-morning-briefing/"
EOF
```

## 环境要求

- Python 3.9+（纯标准库，无需 pip 安装）
- macOS（用于 launchd 调度；Python 脚本在任何 OS 均可运行）
- OpenAI 兼容 LLM API（可选，`--no-llm` 跳过）

## 许可证

[MIT](LICENSE)
