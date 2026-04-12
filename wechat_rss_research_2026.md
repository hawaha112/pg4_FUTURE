# WeChat Public Account RSS / Structured Data Research Report
## (Without WeChat Login/QR Scan Authentication)
### Research Date: April 6, 2026

---

## EXECUTIVE SUMMARY

Getting WeChat public account content as RSS **without any form of login** remains extremely difficult in 2026. WeChat's closed ecosystem actively blocks external scraping. Most "solutions" require at least one initial authentication step (WeChat Reading login, cookie extraction, etc.). However, there are a few genuinely login-free options, particularly for the three target accounts (机器之心, 量子位, 新智元) since they all maintain independent websites with RSS capabilities.

---

## 1. DIRECT WEBSITE RSS FEEDS (NO LOGIN REQUIRED -- BEST OPTIONS)

These are the most reliable, completely login-free methods:

### 量子位 (QbitAI)
- **RSS Feed URL**: `https://www.qbitai.com/feed`
- **Status**: WORKING (verified April 6, 2026)
- **Format**: RSS 2.0 (WordPress-based site)
- **Update frequency**: Multiple times daily
- **Content**: Full article content available in feed
- **Notes**: The feed tag is not in the HTML head, but the standard WordPress `/feed` endpoint works perfectly

### 机器之心 (Jiqizhixin) -- English site
- **RSS Feed URL**: `https://syncedreview.com/feed`
- **Status**: WORKING (verified, last update August 2025 in English)
- **Format**: RSS 2.0
- **Notes**: This is the English-language arm of 机器之心. Covers overlapping but not identical content to the Chinese WeChat account

### 机器之心 (Jiqizhixin) -- Chinese site
- **URL tested**: `https://www.jiqizhixin.com/rss`
- **Status**: NOT WORKING -- returns HTML, not RSS
- **Notes**: The Chinese jiqizhixin.com site does NOT have a functioning public RSS feed. No RSS/Atom link tags in the HTML head. The `/rss` endpoint returns the website HTML, not a feed.

### 新智元 (XinZhiYuan / AI Era)
- **Official website**: `https://aiera.com.cn/`
- **RSS Feed URL**: `https://aiera.com.cn/feed` -- returned server error (500)
- **Status**: NOT WORKING directly from official site
- **Notes**: The site exists but the feed endpoint is broken

---

## 2. THIRD-PARTY AGGREGATOR FEEDS (NO LOGIN REQUIRED)

### AnyFeeder / plink.anyfeeder.com
- **Status**: Partially working as of early 2025; some feeds active in 2026
- **新智元 feed**: `https://plink.anyfeeder.com/weixin/AI_era` -- **WORKING** (verified April 6, 2026, showing today's articles)
- **机器之心 feed**: `https://plink.anyfeeder.com/weixin/jiqizhixin2018` -- returned 404 (NOT WORKING)
- **Notes**: Free service, no login required. Coverage is inconsistent -- some accounts work, others don't. The WeChat ID in the URL must match exactly.
- **Website**: https://plink.anyfeeder.com/

### Wechat2RSS (wechat2rss.xlab.app)
- **Status**: ACTIVE and maintained (new frontend UI released Nov 2025)
- **Model**: Free tier with ~350+ public accounts; paid private deployment option
- **机器之心**: INCLUDED in free public list
- **量子位**: NOT in free list
- **新智元**: NOT in free list
- **Feed URL pattern**: `https://wechat2rss.xlab.app/feed/{wechat_id}.xml`
- **Free list**: https://wechat2rss.xlab.app/list/list
- **Latency**: Average 6 hours from publication to RSS availability
- **No login required** for the free public accounts
- **Private deployment**: Requires self-hosting; supports Zeabur one-click deploy

### RSSHub WeChat Routes
- **Status**: Multiple routes available, but ALL require some configuration
- **Route 1 -- NewRank**: `/newrank/wechat/:wxid` -- Requires `NEWRANK_COOKIE` config
- **Route 2 -- Wechat2RSS proxy**: `/wechat/wechat2rss/:id` -- Proxies wechat2rss.xlab.app (works without auth if the account is in their free list)
- **Route 3 -- WeChat MP albums**: `/wechat/mp/msgalbum/:biz/:aid` -- For specific message albums
- **Important**: RSSHub's public instances (e.g., rsshub.app) often have WeChat routes disabled or rate-limited. Self-hosting recommended.
- **Docs**: https://docs.rsshub.app/

---

## 3. PAID / FREEMIUM SERVICES (NO WECHAT LOGIN BUT REQUIRE ACCOUNT)

### 今天看啥 (jintiankansha.me / jintiankansha.com)
- **Status**: ACTIVE as of 2025
- **Model**: Paid service (purchase via WeChat/Taobao/Alipay)
- **Features**: Curated high-quality public accounts, RSS output compatible with Inoreader/Feedly
- **Drawbacks**: Contains ads in free tier; requires payment for full access; limited account selection
- **Website**: https://www.jintiankansha.me/

### WeRSS (werss.app)
- **Status**: LIKELY DEAD -- reports from June 2025 indicate it shut down and could not be renewed
- **Previously**: Paid service for WeChat public account RSS
- **Note**: Do not recommend -- appears defunct

### TianAPI (tianapi.com)
- **Status**: Active
- **Model**: API service, includes a "WeChat article" endpoint for searching trending articles by keyword
- **Limitations**: Keyword search only, not per-account subscription; API key required
- **Website**: https://www.tianapi.com/apiview/1

---

## 4. SELF-HOSTED OPEN SOURCE PROJECTS

### WeWe RSS (cooderl/wewe-rss)
- **GitHub**: https://github.com/cooderl/wewe-rss (6,500+ stars)
- **Status**: ARCHIVED as of January 19, 2026 (read-only). Forks may continue.
- **Mechanism**: Based on WeChat Reading (微信读书) -- **REQUIRES WeChat QR scan login to WeChat Reading**
- **Key issue in 2025**: API changed April 2025; accounts need re-login; strict rate limits (50 requests/day per account, 300/day per IP)
- **Verdict**: NOT login-free. Requires periodic WeChat Reading authentication.

### We-MP-RSS (rachelos/we-mp-rss)
- **GitHub**: https://github.com/rachelos/we-mp-rss
- **Status**: Active, multiple recent releases
- **Tech stack**: Python (FastAPI) + Vue 3 + SQLite/MySQL
- **Features**: Web management UI, scheduled updates, RSS/Markdown/PDF output, Webhook/API support
- **Login requirement**: The initial article link submission is manual, but ongoing scraping may need cookie/auth tokens
- **Verdict**: Partially login-free -- you can submit article share links manually to bootstrap accounts

### WeChat-Feeds (hellodword/wechat-feeds)
- **GitHub**: https://github.com/hellodword/wechat-feeds
- **Status**: STOPPED SERVICE (archived, no longer maintained)
- **Previously**: Supported 6,200+ accounts
- **Verdict**: Dead project

### Feeddd (feeddd/feeds)
- **GitHub**: https://github.com/feeddd/feeds
- **Status**: CLOSED -- project shut down
- **Previously**: Distributed free service with 30,000+ accounts
- **Successor**: A paid Android-only version exists but is not recommended
- **Verdict**: Dead project

---

## 5. SEARCH ENGINES THAT INDEX WECHAT ARTICLES

### 搜狗微信搜索 (Sogou WeChat Search)
- **URL**: https://weixin.sogou.com/
- **Status**: STILL AVAILABLE as of 2025
- **Features**: Search by account name or article keyword; shows recent articles
- **URL pattern for account search**: `https://weixin.sogou.com/weixin?query={account_name}`
- **Limitations**: Only shows recent articles (typically last 10); requires solving CAPTCHAs for heavy use; no RSS output
- **Scraping libraries exist**: e.g., `chyroc/WechatSogou` (Python) and `reveever/go-weixin-sogou` (Go) -- but these may break frequently due to anti-bot measures

---

## 6. TELEGRAM CHANNELS

### 机器之心 Telegram
- **Channel**: @jiqizhixin001
- **Status**: Exists (listed on TGStat analytics)
- **Content**: Mirrors some content from the WeChat account

### AI News CN (General Chinese AI News)
- **Channel**: @AI_News_CN (t.me/AI_News_CN)
- **Status**: ACTIVE, ~11,700 subscribers (Nov 2025)
- **Content**: Aggregates ChatGPT/AI news from across the web, including Chinese sources
- **Note**: Not specifically a mirror of the three target accounts, but covers overlapping AI news

### 量子位 and 新智元
- No dedicated Telegram channels found in searches

---

## 7. OTHER PLATFORMS WHERE THESE ACCOUNTS PUBLISH

All three accounts (机器之心, 量子位, 新智元) publish on multiple platforms beyond WeChat:

| Platform | 机器之心 | 量子位 | 新智元 |
|----------|---------|--------|--------|
| Official Website | jiqizhixin.com | qbitai.com (has RSS!) | aiera.com.cn |
| Zhihu Column | zhuanlan.zhihu.com/jiqizhixin | zhuanlan.zhihu.com/qbitai | zhuanlan.zhihu.com/aiera |
| English Site | syncedreview.com (has RSS!) | - | - |
| Toutiao | Yes | Yes | Yes |
| Baijia/Baidu | Yes | Yes | Yes |
| 163/Netease | Yes | Yes | Yes |
| Sohu | Yes | Yes | Yes |
| Douyin | - | Yes | - |

**RSSHub can generate RSS for many of these alternative platforms** (Zhihu, Toutiao, etc.) without requiring WeChat authentication.

---

## 8. RECOMMENDED PRACTICAL APPROACH (NO WECHAT LOGIN)

For the three specific accounts (机器之心, 量子位, 新智元), here is the most practical no-login approach:

### Tier 1: Direct RSS (Zero Auth)
1. **量子位**: Use `https://www.qbitai.com/feed` (WordPress RSS, fully working)
2. **新智元**: Use `https://plink.anyfeeder.com/weixin/AI_era` (AnyFeeder, verified working today)
3. **机器之心 (English)**: Use `https://syncedreview.com/feed` (direct RSS)
4. **机器之心 (Chinese)**: Use Wechat2RSS free list at `https://wechat2rss.xlab.app` (included in free tier)

### Tier 2: RSSHub Proxy Routes
- Use RSSHub's `/wechat/wechat2rss/:id` route to proxy Wechat2RSS feeds
- Use RSSHub Zhihu routes for their Zhihu columns: `/zhihu/zhuanlan/:id`

### Tier 3: Telegram
- Follow @jiqizhixin001 for 机器之心 content
- Follow @AI_News_CN for general Chinese AI news aggregation

---

## 9. SUMMARY TABLE OF ALL SERVICES

| Service | Status (2026) | Login Required? | Free? | Notes |
|---------|---------------|-----------------|-------|-------|
| qbitai.com/feed | WORKING | No | Yes | Best option for 量子位 |
| syncedreview.com/feed | WORKING | No | Yes | English 机器之心 |
| anyfeeder.com | PARTIAL | No | Yes | Works for some accounts |
| wechat2rss.xlab.app | WORKING | No (free tier) | Free+Paid | 350+ free accounts |
| RSSHub (self-hosted) | WORKING | Config needed | Yes | Multiple WeChat routes |
| 今天看啥 | WORKING | Account needed | Paid | Has ads in free tier |
| WeWe RSS | ARCHIVED | WeChat Reading QR | Yes | Repo archived Jan 2026 |
| We-MP-RSS | ACTIVE | Partial | Yes | Manual link submission |
| WeRSS (werss.app) | DEAD | - | - | Shut down ~2025 |
| Feeddd | DEAD | - | - | Closed |
| WeChat-Feeds | DEAD | - | - | Stopped service |
| 搜狗微信搜索 | AVAILABLE | No | Yes | Search only, no RSS |
| TianAPI | ACTIVE | API key | Freemium | Keyword search only |
| jiqizhixin.com/rss | BROKEN | - | - | Returns HTML, not RSS |
| aiera.com.cn/feed | BROKEN | - | - | Server error 500 |
