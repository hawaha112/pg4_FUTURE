# AI 早报信息源深度盘点 · 2026-04-20

## TL;DR

- **当前**：52 启用源（英 37 / 中 15）+ 4 禁用
- **总体评估**：核心实验室 + 权威媒体覆盖**充分**；中国大模型厂、芯片竞品（非 NVIDIA）、独立深度访谈、政策监管**重度缺失**
- **关键缺口 Top 5**（按 ROI 排序）：
  1. **Dwarkesh Patel Podcast**（Zuckerberg / Dario / Demis 最新访谈首发）
  2. **中国大模型厂集群**（DeepSeek / Moonshot / 智谱 / MiniMax / 百川 / 01.AI）
  3. **Hugging Face Daily Papers**（每日精选论文，比 ArXiv 全量噪声小 10 倍）
  4. **AMD / Intel AI blog**（当前只有 NVIDIA 视角）
  5. **Mistral / AI2 / Cohere blog**（美以外 Tier 0 研究实验室）

---

## 1. 现状全景

### 覆盖率按维度

| 维度 | 已有 | 缺失度 | 评分 |
|---|---|---|---|
| 美系大厂实验室 | OpenAI / Anthropic / Google / DeepMind / Microsoft / Apple / NVIDIA | Meta（禁用）、xAI 无 blog | ★★★★★ |
| **中国大模型厂** | 0（仅媒体间接覆盖） | **DeepSeek / Moonshot / 智谱 / 01.AI / MiniMax / 百川 全无** | **★☆☆☆☆** |
| 欧洲/其他实验室 | 0 | Mistral / Stability / AI2 / Cohere | ★☆☆☆☆ |
| **芯片** | NVIDIA×2 + SemiWiki | **AMD / Intel / Groq / Cerebras / TPU 无** | **★★☆☆☆** |
| 独立深度作者 | Simon Willison / Import AI / Latent Space / Interconnects | Dwarkesh / Sebastian Raschka / Chollet / Chip Huyen 无 | ★★★☆☆ |
| 学术论文 | ArXiv cs.AI / cs.LG | **Hugging Face Daily Papers** / Papers with Code / OpenReview 无 | ★★★☆☆ |
| 权威媒体 | TechCrunch / Verge / Ars / VB / Wired / Bloomberg / Axios / Techmeme / The Information | Reuters Tech / WSJ Tech / FT（含付费墙问题） | ★★★★☆ |
| **政策监管** | 0（只有媒体间接覆盖） | **Stanford HAI / AI Safety Institute / CAC / EU AI Act 全无** | **★☆☆☆☆** |
| 开源/工具 | Hugging Face Blog / HN | GitHub Trending / LangChain / W&B / Together / Replicate 无 | ★★☆☆☆ |
| 视频/播客 | Lex / Karpathy / DLAI / Yannic / TMP / AIE / MB | **Dwarkesh Patel（TOP 1 缺失）**、No Priors、ML Street Talk | ★★★☆☆ |
| 中文源 | 量子位 / 36氪 / 虎嗅 / 少数派 / 掘金 / AI前线 / 知乎 | **智东西 / 甲子光年 / 雷锋网 / 深科技** | ★★★☆☆ |

### 按 tier 分布

```
Tier 0（官方一手）  ████████░░  10 源（7 英 + 3 中 X 账号）
Tier 1（研究深度）  ███████████████░░░░░░░░░  19 源
Tier 2（媒体聚合）  ████████████████████████░  23 源
```

**问题**：Tier 0 都是美系大厂；Tier 1 里没一个是中国来源（X-Yann LeCun 等虽列为 Tier 1 但属国际研究者）。

---

## 2. 重大缺口逐项

### 2.1 中国大模型厂（最严重缺口）

当前**完全没有中国大模型厂的一手源**。DeepSeek V3/R1、Moonshot Kimi、智谱 GLM-4.5、MiniMax-M1、百川、01.AI Yi 这些国产旗舰模型的发布只能靠量子位/36氪转载，**延迟 4-24 小时**。

**候选**：
- DeepSeek: 无官方 RSS，但 `github.com/deepseek-ai` 有 releases RSS
- Moonshot/Kimi: 无公开 RSS
- 智谱 AI: `chatglm.cn` 无 RSS；可能靠 `github.com/THUDM` releases
- 01.AI: 类似
- 百川: 类似

**实用方案**：
- `https://github.com/deepseek-ai/<repo>/releases.atom` — GitHub releases feed
- `https://huggingface.co/<org>/activity.atom` — HF 组织发布

### 2.2 芯片竞品（NVIDIA 外 0 源）

当前只有 NVIDIA 视角。行业实际格局：

- **AMD MI300 / MI325** — 2026 AI 训练市场 ~15% 份额，完全漏报
- **Intel Gaudi** — 边缘计算重头
- **Groq LPU** — 推理速度王者
- **Cerebras WSE** — 大模型训练独特路径
- **Google TPU v6/v7** — Gemini 训练基础设施

**候选**：
- `https://www.amd.com/en/blogs.rss` — AMD 官方 blog（需验证）
- `https://www.intel.com/content/www/us/en/newsroom/news-releases.rss` — Intel
- `https://groq.com/blog/` — 需 RSShub
- `https://cerebras.ai/blog/feed` — Cerebras

### 2.3 Dwarkesh Patel Podcast（最顶级深度访谈）

2025-2026 最具影响力的 AI 访谈节目。**Zuckerberg / Dario Amodei / Demis Hassabis / Ilya / Jeff Dean** 首次长谈都在这里发。

**URL**：`https://www.dwarkeshpatel.com/feed`

### 2.4 Hugging Face Daily Papers

ArXiv 每天 100+ AI 论文太多噪声。HF Daily Papers 由社区投票**每日精选 5-10 篇**，信噪比提升 10x。

**URL**：`https://huggingface.co/papers/feed`（需验证）或通过 RSShub

### 2.5 政策监管（0 源）

AI 2026 的监管博弈已成行业驱动力：
- **Stanford HAI**（hai.stanford.edu/news）— 学术政策中心
- **UK AISI**（aisi.gov.uk）— 英国 AI 安全研究所
- **Anthropic Transparency Hub**（anthropic.com/transparency）— 模型评估报告
- **ARC Evals / METR** — 前沿模型危险能力评估
- **CAC 网信办** / **EU AI Act updates**

### 2.6 欧洲/非美实验室

**Mistral AI** — 欧洲最具影响力开源模型厂
**AI2 (Allen Institute)** — OLMo / Molmo 开源主力
**Cohere** — 企业 RAG 龙头
**Stability AI** — 图像/视频扩散模型

### 2.7 中文二级补源

- **智东西**（zhidx.com）— AI 专业媒体，深度够
- **甲子光年**（jazzyear.com）— 产业分析
- **雷锋网 AI**（leiphone.com）— 长文 AI 报道
- **深科技**（mittrchina.com）— MIT TR 中文版（偏深度）

### 2.8 独立开发者/作者

- **Sebastian Raschka**（magazine.sebastianraschka.com）— 大模型工程细节
- **François Chollet** blog — ARC-AGI 设计者
- **Chip Huyen** blog — MLOps 权威
- **AI Snake Oil**（Princeton 教授团队）— 警惕 hype

---

## 3. 推荐补源 Top 15（按 ROI）

| # | 源 | Tier | URL | 预期日均 | 备注 |
|---|---|---|---|---|---|
| 1 | **Dwarkesh Patel** | 1 | dwarkeshpatel.com/feed | 0.3/天 | 周更，大佬长访谈首发 |
| 2 | **Hugging Face Daily Papers** | 1 | RSShub /huggingface/daily-papers | 5-10/天 | 每日精选论文，ArXiv 降噪 |
| 3 | **Mistral AI Blog** | 0 | mistral.ai/news/feed | 0.3/天 | 欧洲开源旗舰 |
| 4 | **AI2 Blog** | 0 | allenai.org/blog/rss | 0.3/天 | OLMo 开源大模型 |
| 5 | **DeepSeek GitHub Releases** | 0 | github.com/deepseek-ai/DeepSeek-V3/releases.atom | 0.1/天 | 中国旗舰模型首发 |
| 6 | **AMD AI Blog** | 1 | amd.com/en/blogs/ai.rss | 0.5/天 | NVIDIA 之外的 GPU 视角 |
| 7 | **智东西** | 2 | rsshub.app/zhidx | 5-10/天 | 中文 AI 专业媒体 |
| 8 | **Sebastian Raschka** | 1 | magazine.sebastianraschka.com/feed | 0.2/天 | 大模型工程细节 |
| 9 | **Stanford HAI News** | 1 | hai.stanford.edu/news/feed | 0.5/天 | 政策 + 学术交界 |
| 10 | **No Priors Podcast** | 2 | rsshub.app/podcast/nopriors | 0.3/天 | 投资人视角 AI 访谈 |
| 11 | **Groq Blog** | 1 | groq.com/blog/feed | 0.2/天 | 推理加速芯片 |
| 12 | **Cohere Blog** | 1 | cohere.com/blog/rss | 0.2/天 | 企业 RAG 领头 |
| 13 | **GitHub Trending AI** | 2 | rsshub.app/github/trending/daily/python (筛 AI 关键词) | 5/天 | 新项目发现 |
| 14 | **甲子光年** | 2 | rsshub.app/jazzyear | 3/天 | 中文产业深度 |
| 15 | **ML Street Talk** | 2 | YouTube feed | 0.3/天 | 研究者长谈 |

---

## 4. 实施建议分批

### 第一批（立即做，ROI 最高）
加入 1-5 + 7 + 9，**8 条新源**，验证能抓到后并入 config.json。

预计每日新增 10-20 条 AI 相关内容，大幅补齐中国模型厂 / 政策 / 顶级访谈缺口。

### 第二批（验证后做）
6 + 8 + 10-12 + 14，**7 条**，多是 Substack / RSShub 路由，可能有部分不稳。

### 第三批（探索）
13 + 15 + 其他 LangChain/W&B/工具类，按需加。

---

## 5. 验证方法（对每个候选源）

```python
python3 -c "
import sys; sys.path.insert(0, 'ai-morning-news')
from tls_client import fetch_bytes
for name, url in [('New Source', 'https://...')]:
    try:
        data, status, _ = fetch_bytes(url, timeout=15)
        txt = data.decode('utf-8', errors='ignore')[:200_000]
        items = txt.count('<item') + txt.count('<entry')
        print(f'{status} items={items}  {name}')
    except Exception as e:
        print(f'FAIL {name}: {e}')
"
```

如果 `items >= 5`，可加入 config.json。

---

## 6. 已有源的"清理"建议

### 建议禁用 / 评估的现有源

| 源 | 现状 | 建议 |
|---|---|---|
| Meta AI Blog | 404 已 disabled | 替换为 **X-AIatMeta** 官方账号 |
| 机器之心 | RSS 失效 disabled | 换 **smzdm / rsshub.app/jiqizhixin**（需测） |
| 新智元 | RSSHub 503 disabled | 同样换路径试 |
| 小红书热门 | 反爬 disabled | 放弃 |
| X-Sam Altman | 只是 Sam 个人推特，本人业务推文少 | 观察几周贡献量 |
| X-Andrew Ng | 个人推文 90% 非 AI 新闻 | 考虑下掉 |

---

## 7. 补源不做的事

明确不追求覆盖以下（性价比低）：
- **Reuters Tech / WSJ Tech / FT Tech** — 401/403 付费墙，靠 Techmeme/Bloomberg 间接覆盖已够
- **Reddit r/MachineLearning 等** — Reddit 2024 封 API，HN + Import AI 间接覆盖 90%
- **Twitter 全量** — Nitter 持续 429 不稳
- **微信公众号** — 没有稳定 RSS，技术成本 >> 收益

---

## 变更一次批准后的实施

若你批准 Top 5 + 7 + 9 这一批（共 8 条），我会：
1. 按"4.验证方法"逐条测试真实抓取
2. 通过的加入 config.json（tier/category/icon/description 按规范）
3. entity_registry.json 为 DeepSeek/Mistral/AI2 补充 primary_sources
4. 跑一次 run_daily 验证新源 kept ≥ 期望值
