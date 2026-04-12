"""
content_fetcher.py — RSS 源抓取 & 文章正文提取

从 fetch_news.py 拆分而来，包含：
- SSL 上下文管理（懒加载，避免模块导入时发起网络请求）
- HTTP 请求工具
- 文章正文抓取（多策略级联）
- 文章图片提取
- YouTube / GitHub / Hacker News / 知乎 / 小红书 / Twitter / 微信 特殊源
- 源健康度监控（SourceHealthTracker）
- fetch_feed() 统一入口（协议分发注册表）
"""

import json
import re
import ssl
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import concurrent.futures
from datetime import datetime, timezone, timedelta
from pathlib import Path

from logger import get_logger
from rss_parser import clean_html, parse_rss

log = get_logger('content_fetcher')


# ═══════════════════════════════════════════════════════════════════════
# SSL 证书修复 — macOS Python 可能缺少系统证书链
# 懒加载：首次调用 get_ssl_context() 时才创建（避免模块导入即发起网络请求）
# ═══════════════════════════════════════════════════════════════════════

_SSL_CTX = None


def _create_ssl_context():
    """创建兼容的 SSL context，自动处理证书缺失问题"""
    test_url = "https://www.google.com"  # 用稳定站点做连通性测试

    # 优先使用 certifi 的证书（必须验证能用，launchd 环境下可能失效）
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        urllib.request.urlopen(test_url, timeout=5, context=ctx)
        return ctx
    except Exception:
        pass
    # 尝试系统默认证书
    try:
        ctx = ssl.create_default_context()
        urllib.request.urlopen(test_url, timeout=5, context=ctx)
        return ctx
    except Exception:
        pass
    # 最后 fallback：禁用验证（不安全，但至少能跑）
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    log.warning("⚠️ SSL 证书验证已禁用（建议运行: /Applications/Python*/Install\\ Certificates.command）")
    return ctx


def get_ssl_context():
    """获取 SSL context（懒加载单例）"""
    global _SSL_CTX
    if _SSL_CTX is None:
        _SSL_CTX = _create_ssl_context()
    return _SSL_CTX


# ═══════════════════════════════════════════════════════════════════════
# 第二部分：文章正文抓取
# ═══════════════════════════════════════════════════════════════════════

def _http_get(url, timeout=10):
    """通用 HTTP GET（自动处理 SSL 证书问题）"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8',
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=get_ssl_context()) as resp:
        data = resp.read(800_000)
    for enc in ['utf-8', 'latin-1', 'gb2312', 'gbk']:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


def _resolve_hn_real_url(hn_url):
    """Hacker News: 从评论页中提取真正的文章链接"""
    try:
        html = _http_get(hn_url, timeout=10)
        m = re.search(r'class="titleline"[^>]*>\s*<a\s+href="([^"]+)"', html)
        if m:
            real_url = m.group(1)
            if real_url.startswith('item?'):
                return ""
            return real_url
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        pass
    return ""


def _fetch_youtube_description(yt_url):
    """YouTube: 从视频页提取描述文本"""
    try:
        html = _http_get(yt_url, timeout=12)
        m = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', html, re.IGNORECASE)
        desc = m.group(1) if m else ""
        m2 = re.search(r'"shortDescription"\s*:\s*"((?:[^"\\]|\\.){50,})"', html)
        if m2:
            long_desc = m2.group(1).encode().decode('unicode_escape', errors='replace')
            if len(long_desc) > len(desc):
                desc = long_desc
        if len(desc) < 50:
            m3 = re.search(r'<meta\s+property="og:description"\s+content="([^"]*)"', html, re.IGNORECASE)
            if m3 and len(m3.group(1)) > len(desc):
                desc = m3.group(1)
        return clean_html(desc)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return ""


def _scrape_youtube_channel(channel_id, max_items=10):
    """Fallback：当 YouTube RSS feed 返回 404 时，从频道页 HTML 提取视频列表

    解析 ytInitialData JSON，提取视频标题、链接、发布时间。
    比 RSS 更可靠，因为直接从前端页面数据提取。
    """
    # 尝试通过 channel_id 构造频道视频页 URL
    # 注意：不用 _http_get（800KB 限制会截断 ytInitialData），直接读完整页面
    channel_url = f"https://www.youtube.com/channel/{channel_id}/videos"
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        req = urllib.request.Request(channel_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as resp:
            html = resp.read(2_000_000).decode('utf-8', errors='replace')
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        log.warning("⚠️ YouTube 频道页抓取失败: %s", e)
        return []

    # 提取 ytInitialData JSON
    m = re.search(r'ytInitialData\s*=\s*({.*?});</script>', html, re.DOTALL)
    if not m:
        return []

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

    items = []
    now = datetime.now(timezone.utc)

    # 导航到视频列表
    tabs = data.get('contents', {}).get('twoColumnBrowseResultsRenderer', {}).get('tabs', [])
    for tab in tabs:
        tab_content = tab.get('tabRenderer', {}).get('content', {})
        section = tab_content.get('richGridRenderer', {})
        contents = section.get('contents', [])
        for entry in contents[:max_items]:
            vid = (entry.get('richItemRenderer', {})
                   .get('content', {})
                   .get('videoRenderer', {}))
            if not vid:
                continue

            title = vid.get('title', {}).get('runs', [{}])[0].get('text', '')
            vid_id = vid.get('videoId', '')
            if not title or not vid_id:
                continue

            link = f"https://www.youtube.com/watch?v={vid_id}"

            # 尝试从 publishedTimeText 推算发布时间
            pub_text = vid.get('publishedTimeText', {}).get('simpleText', '')
            pub_date = _estimate_youtube_date(pub_text, now)

            # 简短描述
            desc_runs = vid.get('descriptionSnippet', {}).get('runs', [])
            desc = ' '.join(r.get('text', '') for r in desc_runs) if desc_runs else ''

            # 缩略图
            thumbs = vid.get('thumbnail', {}).get('thumbnails', [])
            image = thumbs[-1].get('url', '') if thumbs else ''

            items.append({
                'title': title,
                'link': link,
                'summary': desc[:800] if desc else '',
                'published': pub_date,
                'image': image,
            })
        if items:
            break

    return items


def _estimate_youtube_date(pub_text, now):
    """从 YouTube 的相对时间文本推算大致日期

    支持两种格式：
    - 全称: '3 days ago', '2 weeks ago', '1 year ago'
    - 缩写: '3d ago', '2w ago', '4h ago'
    """
    if not pub_text:
        return None
    pub_text = pub_text.lower().strip()
    try:
        # 全称格式
        m = re.search(r'(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago', pub_text)
        if m:
            n, unit = int(m.group(1)), m.group(2)
        else:
            # 缩写格式: "4w ago", "3d ago", etc.
            m = re.search(r'(\d+)\s*([smhdwy])\w*\s+ago', pub_text)
            if m:
                n = int(m.group(1))
                abbrev = m.group(2)
                unit = {'s': 'second', 'm': 'minute', 'h': 'hour',
                        'd': 'day', 'w': 'week', 'y': 'year'}.get(abbrev, '')
            else:
                return None

        deltas = {
            'second': timedelta(seconds=n),
            'minute': timedelta(minutes=n),
            'hour': timedelta(hours=n),
            'day': timedelta(days=n),
            'week': timedelta(weeks=n),
            'month': timedelta(days=n * 30),
            'year': timedelta(days=n * 365),
        }
        if unit in deltas:
            return now - deltas[unit]
    except (ValueError, OverflowError):
        pass
    return None


def _fetch_github_readme(gh_url):
    """GitHub: 从 API 获取 README 内容"""
    try:
        m = re.match(r'https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$', gh_url)
        if not m:
            return ""
        owner, repo = m.group(1), m.group(2)
        api_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
        headers = {'Accept': 'application/vnd.github.v3.raw', 'User-Agent': 'Mozilla/5.0'}
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10, context=get_ssl_context()) as resp:
            readme = resp.read(100_000).decode('utf-8', errors='replace')
        readme = re.sub(r'!\[[^\]]*\]\([^)]+\)', '', readme)
        readme = re.sub(r'\[[^\]]*\]\([^)]+\)', '', readme)
        readme = re.sub(r'#{1,6}\s*', '', readme)
        readme = re.sub(r'[*_`~]{1,3}', '', readme)
        readme = re.sub(r'\n{3,}', '\n\n', readme)
        return readme[:2000].strip()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return ""


def _extract_article_body(html_text):
    """从 HTML 中提取文章正文，多策略级联"""
    if not html_text:
        return ""
    for tag in ['script', 'style', 'nav', 'header', 'footer', 'aside',
                'noscript', 'iframe', 'form', 'svg', 'button']:
        html_text = re.sub(rf'<{tag}[\s>].*?</{tag}>', ' ', html_text,
                           flags=re.DOTALL | re.IGNORECASE)
    candidates = []

    # 策略1: <article> 标签
    for m in re.finditer(r'<article[^>]*>(.*?)</article>', html_text,
                         re.DOTALL | re.IGNORECASE):
        text = clean_html(m.group(1))
        if len(text) > 100:
            candidates.append(('article_tag', text))

    # 策略2: 语义化 class/id
    semantic_patterns = [
        r'(?:article[_-]?body|post[_-]?content|entry[_-]?content|'
        r'article[_-]?content|story[_-]?body|blog[_-]?post|'
        r'main[_-]?content|article__body|post__body|'
        r'c-entry-content|post-full-content|article-text|'
        r'single[_-]?content|page[_-]?content)',
    ]
    for pat in semantic_patterns:
        for m in re.finditer(
            rf'<(?:div|section|main)[^>]*(?:class|id)="[^"]*{pat}[^"]*"[^>]*>(.*?)</(?:div|section|main)>',
            html_text, re.DOTALL | re.IGNORECASE):
            text = clean_html(m.group(1))
            if len(text) > 100:
                candidates.append(('semantic_class', text))

    # 策略3: JSON-LD
    for m in re.finditer(
        r'<script\s+type="application/ld\+json"[^>]*>(.*?)</script>',
        html_text, re.DOTALL | re.IGNORECASE):
        try:
            ld = json.loads(m.group(1))
            if isinstance(ld, list):
                ld = ld[0]
            body = ld.get('articleBody', '') or ld.get('description', '')
            if body and len(body) > 80:
                candidates.append(('json_ld', clean_html(body)))
        except (json.JSONDecodeError, KeyError, TypeError, IndexError):
            pass

    # 策略4: meta description
    meta_desc = ""
    for m in re.finditer(
        r'<meta\s+(?:name="description"|property="og:description")\s+content="([^"]*)"',
        html_text, re.IGNORECASE):
        if len(m.group(1)) > len(meta_desc):
            meta_desc = m.group(1)
    if meta_desc and len(meta_desc) > 60:
        candidates.append(('meta_desc', clean_html(meta_desc)))

    # 策略5: <p> 段落
    paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html_text,
                            re.DOTALL | re.IGNORECASE)
    good_ps = []
    for p in paragraphs:
        clean = clean_html(p)
        if len(clean) > 40:
            tag_ratio = len(re.findall(r'<[^>]+>', p)) / max(len(clean), 1)
            if tag_ratio < 0.1:
                good_ps.append(clean)
    if good_ps:
        combined = '\u3002'.join(good_ps[:20])
        if len(combined) > 100:
            candidates.append(('paragraphs', combined))

    if not candidates:
        return ""
    candidates.sort(key=lambda x: len(x[1]), reverse=True)
    best = candidates[0][1]

    # 去噪
    noise_patterns = [
        r'Subscribe to.*?(?:newsletter|updates)[.\s]',
        r'Sign up for.*?(?:newsletter|free)[.\s]',
        r'Share this.*?(?:article|story)[.\s]',
        r'Related (?:articles?|stories|posts)[.\s]',
        r'(?:Cookie|Privacy) (?:policy|notice)[.\s]',
        r'Advertisement[.\s]',
        r'Follow us on[.\s]',
    ]
    for np in noise_patterns:
        best = re.sub(np, ' ', best, flags=re.IGNORECASE)
    best = re.sub(r'\s+', ' ', best).strip()
    return best[:4000]


def _extract_article_images(html_text, base_url=""):
    """从文章 HTML 中提取核心图片 URL 列表（去重、过滤噪音图片）"""
    from urllib.parse import urlparse
    images = []
    seen = set()

    NOISE_KEYWORDS = [
        'data:image', '.svg', 'pixel', 'tracking', 'avatar',
        'logo', 'badge', 'emoji', 'button', 'arrow',
        'facebook.com', 'twitter.com', 'linkedin.com', 'pinterest.com',
        'gravatar', 'widget', '/ad/', '/ads-', 'sponsor',
        '1x1', '2x2', 'spacer', 'blank.gif', 'transparent.png',
        'placeholder', 'default_avatar', 'qrcode',
        'mini_icon', 'thumb_icon',
        'google-analytics', 'collect?v=',
        # 新增：站点级 icon / 社交分享按钮 / 许可证图标
        # （arxiv / qbitai / 多数内容站会把这些图片塞进正文）
        '/icon/', '/icons/', '_icon.', '-icon.', 'favicon',
        '/social/', 'share_icon', 'sharebutton', 'bibsonomy',
        '/licenses/', 'license/by', 'creativecommons', 'cc-by',
        '/badges/', 'badge_',
    ]

    def _clean_url(src):
        """补全相对 URL，解码 HTML 实体"""
        if not src:
            return ""
        # 解码 HTML 实体
        src = src.replace('&amp;', '&').replace('&#39;', "'").replace('&quot;', '"')
        if src.startswith('//'):
            return 'https:' + src
        if src.startswith('/') and base_url:
            parsed = urlparse(base_url)
            return f"{parsed.scheme}://{parsed.netloc}{src}"
        return src

    def _is_noise(src):
        """判断是否为噪音图片"""
        lower = src.lower()
        return any(skip in lower for skip in NOISE_KEYWORDS)

    def _add_img(src):
        src = _clean_url(src)
        if src and src.startswith('http') and src not in seen and not _is_noise(src):
            images.append(src)
            seen.add(src)

    # 1. og:image / twitter:image（高质量封面图）
    for pattern in [
        r'<meta\s+(?:property|name)=["\']og:image(?::?\w*)?["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta\s+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']og:image["\']',
        r'<meta\s+(?:property|name)=["\']twitter:image["\'][^>]*content=["\']([^"\']+)["\']',
    ]:
        for url in re.findall(pattern, html_text, re.IGNORECASE):
            _add_img(url)

    # 2. 尝试定位文章主体区域（减少噪音）
    article_html = html_text
    for pattern in [
        r'<article[^>]*>(.*?)</article>',
        r'<div[^>]*class="[^"]*(?:article[-_]?(?:body|content|text|detail)|post[-_]?(?:body|content)|entry[-_]?content|rich[-_]?text|news[-_]?content)[^"]*"[^>]*>(.*?)</div>',
        r'<main[^>]*>(.*?)</main>',
        r'<div[^>]*class="[^"]*content[^"]*"[^>]*>(.*?)</div>',
    ]:
        m = re.search(pattern, html_text, re.DOTALL | re.IGNORECASE)
        if m:
            candidate = m.group(1) if m.lastindex == 1 else m.group(2) if m.lastindex == 2 else m.group(1)
            # 只有正文区域有足够内容才采用
            if len(candidate) > 200:
                article_html = candidate
                break

    # 3. 从文章区域提取 <img> 标签（多种 src 属性兼容懒加载）
    for src_match in re.finditer(
        r'<img[^>]+?(?:src|data-src|data-original|data-lazy-src)=["\']([^"\']+)["\']',
        article_html, re.IGNORECASE
    ):
        _add_img(src_match.group(1))

    # 4. 从 JSON-LD 中提取 image 字段
    for ld_match in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                                html_text, re.DOTALL | re.IGNORECASE):
        try:
            ld = json.loads(ld_match.group(1))
            if isinstance(ld, list):
                ld = ld[0] if ld else {}
            for key in ['image', 'thumbnailUrl']:
                img_val = ld.get(key, '')
                if isinstance(img_val, str) and img_val:
                    _add_img(img_val)
                elif isinstance(img_val, list):
                    for v in img_val[:3]:
                        if isinstance(v, str):
                            _add_img(v)
                        elif isinstance(v, dict):
                            _add_img(v.get('url', ''))
                elif isinstance(img_val, dict):
                    _add_img(img_val.get('url', ''))
        except (json.JSONDecodeError, KeyError, TypeError, IndexError):
            pass

    # 最多返回 4 张核心图片
    return images[:4]


def _fetch_article_text(url, source_name=""):
    """根据来源智能选择抓取策略"""
    if not url or url == '#':
        return ""
    try:
        if 'news.ycombinator.com' in url:
            real_url = _resolve_hn_real_url(url)
            if not real_url:
                return ""
            return _fetch_article_text(real_url, source_name)
        if 'youtube.com' in url or 'youtu.be' in url:
            return _fetch_youtube_description(url)
        if re.match(r'https?://(?:gist\.)?github\.com/', url):
            return _fetch_github_readme(url)
        if url.lower().endswith('.pdf'):
            return ""
        html = _http_get(url, timeout=10)
        return _extract_article_body(html)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return ""


_PAPER_SITES_NO_IMAGE = (
    'arxiv.org',
    'openreview.net',
    'aclanthology.org',
    'papers.nips.cc',
    'proceedings.mlr.press',
)


def _fetch_article_text_and_images(url, source_name=""):
    """抓取文章原文 + 提取文章内图片"""
    if not url or url == '#':
        return "", []
    try:
        if 'news.ycombinator.com' in url:
            real_url = _resolve_hn_real_url(url)
            if not real_url:
                return "", []
            return _fetch_article_text_and_images(real_url, source_name)
        if 'youtube.com' in url or 'youtu.be' in url:
            return _fetch_youtube_description(url), []
        if re.match(r'https?://(?:gist\.)?github\.com/', url):
            return _fetch_github_readme(url), []
        if url.lower().endswith('.pdf'):
            return "", []
        # 论文站点短路：这些页面没有真正的 hero image，
        # 硬抓只会拿到 CC license / 社交分享图标，不如直接返回空列表
        lower_url = url.lower()
        if any(site in lower_url for site in _PAPER_SITES_NO_IMAGE):
            html = _http_get(url, timeout=10)
            return _extract_article_body(html), []
        html = _http_get(url, timeout=10)
        text = _extract_article_body(html)
        images = _extract_article_images(html, url)
        return text, images
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return "", []


def enrich_articles_with_content(items):
    """批量抓取文章原文 + 文章内图片"""
    log.info("📖 抓取文章原文...")
    # 需要补充原文的文章
    need_text = set(i for i, item in enumerate(items) if len(item.get('summary', '')) < 100)
    # 所有文章都尝试提取图片
    to_fetch = list(range(len(items)))
    log.info("🔍 共 %s 篇需要补充原文，%s 篇尝试提取图片...", len(need_text), len(to_fetch))

    def _fetch_one(idx):
        item = items[idx]
        url = item.get('link', '')
        source = item.get('source_name', '')
        text, images = _fetch_article_text_and_images(url, source)
        return idx, text, images

    text_success = 0
    img_success = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch_one, idx): idx for idx in to_fetch}
        for future in concurrent.futures.as_completed(futures):
            try:
                idx, text, images = future.result()
                if text and len(text) > 30:
                    if idx in need_text:
                        items[idx]['article_text'] = text
                        text_success += 1
                    elif not items[idx].get('article_text'):
                        # 即使 summary 够长，也保存原文给 LLM 用
                        items[idx]['article_text'] = text
                if images:
                    items[idx]['extra_images'] = images
                    img_success += 1
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
                pass
    log.info("📥 成功抓取 %s/%s 篇原文", text_success, len(need_text))
    log.info("🖼️ 成功提取 %s/%s 篇文章内图片", img_success, len(to_fetch))

    # Fallback：对抓取失败且无 article_text 的条目，用 RSS summary 兜底
    fallback_count = 0
    for item in items:
        if not item.get('article_text') and item.get('summary'):
            summary = item['summary'].strip()
            if len(summary) > 30:
                item['article_text'] = summary
                fallback_count += 1
    if fallback_count > 0:
        log.info("📋 %s 篇使用 RSS 摘要兜底", fallback_count)

    return items


# ═══════════════════════════════════════════════════════════════════════
# 第三部分：RSS 源抓取 & 源健康度监控
# ═══════════════════════════════════════════════════════════════════════

class SourceHealthTracker:
    """跟踪每个源的抓取健康状态，含响应时间、成功率等指标，连续失败超过阈值时报警"""

    def __init__(self, health_path: str, alert_threshold: int = 3):
        self.health_path = Path(health_path)
        self.alert_threshold = alert_threshold
        self.data = self._load()
        # 运行时计时器
        self._timers: dict = {}

    def _load(self) -> dict:
        if self.health_path.exists():
            try:
                with open(self.health_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def save(self):
        try:
            with open(self.health_path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            log.warning("⚠️ 健康度数据保存失败: %s", e)

    def start_timer(self, source_name: str):
        """开始计时（在 fetch 之前调用）"""
        self._timers[source_name] = time.time()

    def _get_elapsed(self, source_name: str) -> float:
        """获取耗时（秒），没有计时器返回 0"""
        start = self._timers.pop(source_name, None)
        if start is not None:
            return round(time.time() - start, 2)
        return 0.0

    def record_success(self, source_name: str, item_count: int):
        prev = self.data.get(source_name, {})
        elapsed = self._get_elapsed(source_name)

        # 计算滚动平均响应时间（指数移动平均 α=0.3）
        prev_avg = prev.get('avg_response_time', 0.0)
        if prev_avg > 0 and elapsed > 0:
            avg_time = round(prev_avg * 0.7 + elapsed * 0.3, 2)
        else:
            avg_time = elapsed

        # 成功计数
        total_successes = prev.get('total_successes', 0) + 1
        total_runs = prev.get('total_runs', 0) + 1

        self.data[source_name] = {
            'status': 'ok',
            'last_success': datetime.now(timezone.utc).isoformat(),
            'last_count': item_count,
            'consecutive_failures': 0,
            'last_error': '',
            'last_response_time': elapsed,
            'avg_response_time': avg_time,
            'total_successes': total_successes,
            'total_runs': total_runs,
            'success_rate': round(total_successes / total_runs * 100, 1) if total_runs > 0 else 100.0,
        }

    def record_failure(self, source_name: str, error: str):
        prev = self.data.get(source_name, {})
        failures = prev.get('consecutive_failures', 0) + 1
        elapsed = self._get_elapsed(source_name)

        total_runs = prev.get('total_runs', 0) + 1
        total_successes = prev.get('total_successes', 0)

        self.data[source_name] = {
            'status': 'failing',
            'last_success': prev.get('last_success', ''),
            'last_count': prev.get('last_count', 0),
            'consecutive_failures': failures,
            'last_error': str(error)[:200],
            'last_response_time': elapsed,
            'avg_response_time': prev.get('avg_response_time', 0.0),
            'total_successes': total_successes,
            'total_runs': total_runs,
            'success_rate': round(total_successes / total_runs * 100, 1) if total_runs > 0 else 0.0,
        }

    def get_alerts(self) -> list:
        """返回连续失败超过阈值的源列表"""
        alerts = []
        for name, info in self.data.items():
            fails = info.get('consecutive_failures', 0)
            if fails >= self.alert_threshold:
                last_ok = info.get('last_success', '从未成功')
                err = info.get('last_error', '未知错误')
                alerts.append({
                    'source': name,
                    'consecutive_failures': fails,
                    'last_success': last_ok,
                    'last_error': err,
                    'success_rate': info.get('success_rate', 0),
                })
        return alerts

    def print_report(self):
        alerts = self.get_alerts()
        if not alerts:
            return
        log.warning("🚨 源健康度警报：%d 个源连续失败", len(alerts))
        for a in alerts:
            last_ok = a['last_success'][:10] if a['last_success'] != '从未成功' else '从未成功'
            log.warning("⛔ %s: 连续 %d 次失败 | 成功率 %.0f%% | 上次成功: %s | 错误: %s",
                        a['source'], a['consecutive_failures'],
                        a.get('success_rate', 0), last_ok, a['last_error'][:60])


# ═══════════════════════════════════════════════════════════════════════
# 协议处理器 — 各种特殊源的抓取实现
# ═══════════════════════════════════════════════════════════════════════

def _fetch_youtube_feed(source, channel_id, max_items, max_age_hours, health_tracker):
    """YouTube 专用抓取：直接从频道页 HTML 提取视频列表"""
    name = source['name']
    try:
        items = _scrape_youtube_channel(channel_id, max_items)
        if not items:
            raise RuntimeError("频道页解析无结果")

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=max_age_hours)
        with_date = [i for i in items if i.get('published') and i['published'] >= cutoff]
        no_date = [i for i in items if not i.get('published')]
        filtered = with_date + no_date[:3]
        if not filtered and items:
            filtered = items[:min(3, max_items)]

        for item in filtered[:max_items]:
            item['source_name'] = source['name']
            item['source_icon'] = source['icon']
            item['source_color'] = source['color']
            item['source_category'] = source['category']

        count = len(filtered[:max_items])
        log.info("✅ %s: 获取 %s 条", name, count)
        if health_tracker:
            health_tracker.record_success(name, count)
        return filtered[:max_items]

    except Exception as e:
        log.error("❌ %s: %s", name, e)
        if health_tracker:
            health_tracker.record_failure(name, e)
        return []


def _fetch_zhihu_hot(source, max_items, max_age_hours, health_tracker):
    """知乎热榜：通过移动端 API 获取热门话题

    使用 api.zhihu.com 的 hot-lists/total 接口，无需认证。
    返回热榜话题，包含标题、链接、热度、回答数。
    """
    name = source['name']
    try:
        api_url = f"https://api.zhihu.com/topstory/hot-lists/total?limit={max_items}"
        headers = {
            'User-Agent': ('Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) '
                           'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148'),
            'Accept': 'application/json',
        }
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as resp:
            data = json.loads(resp.read().decode('utf-8'))

        raw_items = data.get('data', [])
        if not raw_items:
            raise RuntimeError("知乎热榜 API 返回空数据")

        now = datetime.now(timezone.utc)
        items = []
        for entry in raw_items[:max_items]:
            target = entry.get('target', {})
            title = target.get('title', '')
            qid = target.get('id', '')
            if not title or not qid:
                continue

            link = f"https://www.zhihu.com/question/{qid}"
            excerpt = target.get('excerpt', '') or ''
            heat = entry.get('detail_text', '')  # e.g. "612 万热度"
            answer_count = target.get('answer_count', 0)

            # 用 created 时间戳（如果有的话）
            created_ts = target.get('created', 0)
            pub_date = None
            if created_ts:
                try:
                    pub_date = datetime.fromtimestamp(created_ts, tz=timezone.utc)
                except (OSError, ValueError):
                    pass

            # 构造摘要：热度 + 回答数 + 原文摘要
            summary_parts = []
            if heat:
                summary_parts.append(f"\U0001f525 {heat}")
            if answer_count:
                summary_parts.append(f"\U0001f4ac {answer_count} 个回答")
            if excerpt:
                summary_parts.append(excerpt[:500])
            summary = ' | '.join(summary_parts)

            items.append({
                'title': title,
                'link': link,
                'summary': summary,
                'published': pub_date,
                'image': '',
                'source_name': source['name'],
                'source_icon': source['icon'],
                'source_color': source['color'],
                'source_category': source['category'],
            })

        count = len(items)
        log.info("✅ %s: 获取 %s 条", name, count)
        if health_tracker:
            health_tracker.record_success(name, count)
        return items

    except Exception as e:
        log.error("❌ %s: %s", name, e)
        if health_tracker:
            health_tracker.record_failure(name, e)
        return []


def _fetch_twitter_feed(source, screen_name, max_items, max_age_hours, health_tracker, config):
    """X/Twitter：通过 Nitter RSS 镜像抓取推文（免登录）

    URL 格式：twitter://screen_name

    原理：nitter.net 等 Nitter 实例提供公开的 Twitter 用户 RSS，
    无需任何账号或 cookies。自动尝试多个 Nitter 镜像。
    """
    name = source['name']
    twitter_config = (config or {}).get('twitter', {})
    # 允许用户自定义 Nitter 实例列表
    nitter_instances = twitter_config.get('nitter_instances', [
        'https://nitter.net',
    ])

    rss_xml = None
    last_err = None
    for base in nitter_instances:
        rss_url = f"{base.rstrip('/')}/{screen_name}/rss"
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                              'AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/rss+xml, application/xml, text/xml, */*',
            }
            req = urllib.request.Request(rss_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as resp:
                rss_xml = resp.read().decode('utf-8', errors='replace')
                if '<item>' in rss_xml:
                    break  # 成功拿到有内容的 RSS
                rss_xml = None
        except Exception as e:
            last_err = e
            continue

    if not rss_xml:
        err_msg = f"所有 Nitter 实例均失败: {last_err}"
        log.error("❌ %s: %s", name, err_msg)
        if health_tracker:
            health_tracker.record_failure(name, RuntimeError(err_msg))
        return []

    try:
        items = parse_rss(rss_xml)

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=max_age_hours)
        with_date = [i for i in items if i.get('published') and i['published'] >= cutoff]
        no_date = [i for i in items if not i.get('published')]
        filtered = with_date + no_date[:3]
        if not filtered and items:
            filtered = items[:min(3, max_items)]

        # 修正链接：nitter URL → x.com URL
        for item in filtered[:max_items]:
            if item.get('link'):
                item['link'] = re.sub(
                    r'https?://nitter\.[^/]+/',
                    'https://x.com/',
                    item['link']
                )
                item['link'] = item['link'].replace('#m', '')  # 去掉 nitter 锚点
            item['source_name'] = source['name']
            item['source_icon'] = source['icon']
            item['source_color'] = source['color']
            item['source_category'] = source['category']

        count = len(filtered[:max_items])
        log.info("✅ %s: 获取 %s 条", name, count)
        if health_tracker:
            health_tracker.record_success(name, count)
        return filtered[:max_items]

    except Exception as e:
        log.error("❌ %s: %s", name, e)
        if health_tracker:
            health_tracker.record_failure(name, e)
        return []


def _fetch_xiaohongshu(source, query, max_items, max_age_hours, health_tracker, config):
    """小红书：从 explore 页面 SSR 数据提取笔记（免登录）

    原理：小红书首页 /explore 会在 HTML 中内嵌 window.__INITIAL_STATE__，
    包含 ~25 条推荐笔记的完整数据（标题、作者、点赞、封面等），
    无需任何登录或 cookie。

    URL 格式：
    - xhs://explore                          — 默认推荐 feed
    - xhs://explore/homefeed.career_v3       — 指定频道
    配合 ai_only: false + 关键词预过滤，可筛出 AI 相关内容。

    结构变化检测：如果页面返回但数据路径变更，记录具体断点信息，
    方便快速定位小红书前端改版。
    """
    name = source['name']

    # 已知的 SSR 数据提取策略（按优先级尝试）
    _EXTRACT_STRATEGIES = [
        # 策略1: 当前已知结构 — feed.feeds[].noteCard
        {
            'state_pattern': r'window\.__INITIAL_STATE__\s*=\s*(.*?)(?:</script>)',
            'feed_path': lambda state: state.get('feed', {}).get('feeds', []),
            'note_path': lambda entry: entry.get('noteCard', {}),
            'title_key': 'displayTitle',
            'id_key': lambda entry: entry.get('id', ''),
        },
        # 策略2: 备选路径 — explore.feeds[].noteCard（小红书曾用此路径）
        {
            'state_pattern': r'window\.__INITIAL_STATE__\s*=\s*(.*?)(?:</script>)',
            'feed_path': lambda state: state.get('explore', {}).get('feeds', []),
            'note_path': lambda entry: entry.get('noteCard', {}),
            'title_key': 'displayTitle',
            'id_key': lambda entry: entry.get('id', ''),
        },
        # 策略3: 备选标题字段 — title 替代 displayTitle
        {
            'state_pattern': r'window\.__INITIAL_STATE__\s*=\s*(.*?)(?:</script>)',
            'feed_path': lambda state: state.get('feed', {}).get('feeds', []),
            'note_path': lambda entry: entry.get('noteCard', {}) or entry,
            'title_key': 'title',
            'id_key': lambda entry: entry.get('id', '') or entry.get('noteId', ''),
        },
    ]

    try:
        # 解析频道参数
        channel_id = ''
        if '/' in query:
            _, channel_id = query.split('/', 1)

        explore_url = 'https://www.xiaohongshu.com/explore'
        if channel_id:
            explore_url += f'?channel_id={channel_id}'

        headers = {
            'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/120.0.0.0 Safari/537.36'),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        }
        req = urllib.request.Request(explore_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as resp:
            html = resp.read(1_000_000).decode('utf-8', errors='replace')

        # 依次尝试各提取策略
        state = None
        used_strategy = None
        diagnostics = []

        for si, strategy in enumerate(_EXTRACT_STRATEGIES):
            m = re.search(strategy['state_pattern'], html, re.DOTALL)
            if not m:
                diagnostics.append(f"策略{si+1}: __INITIAL_STATE__ 未匹配")
                continue

            if state is None:
                state_raw = m.group(1).strip().rstrip(';')
                state_raw = state_raw.replace('undefined', 'null')
                try:
                    state = json.loads(state_raw)
                except json.JSONDecodeError as je:
                    diagnostics.append(f"策略{si+1}: JSON 解析失败 — {je}")
                    continue

            feeds = strategy['feed_path'](state)
            if not feeds:
                # 记录 state 顶层 key 帮助诊断
                top_keys = list(state.keys())[:10]
                diagnostics.append(f"策略{si+1}: feed 路径为空 (state keys: {top_keys})")
                continue

            # 验证至少有一条有标题
            test_note = strategy['note_path'](feeds[0])
            test_title = test_note.get(strategy['title_key'], '')
            if not test_title and len(feeds) > 1:
                test_note = strategy['note_path'](feeds[1])
                test_title = test_note.get(strategy['title_key'], '')
            if not test_title:
                note_keys = list(test_note.keys())[:8]
                diagnostics.append(f"策略{si+1}: 有 {len(feeds)} 条 feed 但标题字段 '{strategy['title_key']}' 为空 (note keys: {note_keys})")
                continue

            used_strategy = strategy
            break

        if not used_strategy:
            # 所有策略失败 — 输出诊断信息帮助快速定位结构变化
            has_state = '__INITIAL_STATE__' in html
            diag_msg = "; ".join(diagnostics) if diagnostics else "无诊断信息"
            raise RuntimeError(
                f"SSR 结构变化检测: __INITIAL_STATE__{'存在' if has_state else '不存在'}, "
                f"HTML 长度 {len(html)}, 诊断: {diag_msg}"
            )

        if used_strategy is not _EXTRACT_STRATEGIES[0]:
            log.warning("⚠️ %s: 使用备选提取策略（主策略失败），小红书可能已改版", name)

        items = []
        for entry in feeds:
            note = used_strategy['note_path'](entry)
            if not note:
                continue

            title = note.get(used_strategy['title_key'], '')
            if not title:
                continue

            note_id = used_strategy['id_key'](entry)
            link = f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else ''

            # 作者
            user = note.get('user', {})
            nickname = user.get('nickName', '') or user.get('nickname', '')

            # 互动数据
            interact = note.get('interactInfo', {})
            liked_count = interact.get('likedCount', '') or interact.get('liked_count', '')

            summary_parts = []
            if nickname:
                summary_parts.append(f"\U0001f464 {nickname}")
            if liked_count:
                summary_parts.append(f"\u2764\ufe0f {liked_count}")
            summary = ' | '.join(summary_parts)

            # 封面图
            cover = note.get('cover', {})
            image = cover.get('urlDefault', '') or cover.get('url', '')
            if image and not image.startswith('http'):
                image = f"https://sns-img-bd.xhscdn.com/{image}"

            items.append({
                'title': title,
                'link': link,
                'summary': summary[:800],
                'published': None,  # SSR 数据不含精确时间
                'image': image,
                'source_name': source['name'],
                'source_icon': source['icon'],
                'source_color': source['color'],
                'source_category': source['category'],
            })

            if len(items) >= max_items:
                break

        count = len(items)
        log.info("✅ %s: 获取 %d 条（SSR 免登录）", name, count)
        if health_tracker:
            health_tracker.record_success(name, count)
        return items

    except Exception as e:
        log.error("❌ %s: %s", name, e)
        if health_tracker:
            health_tracker.record_failure(name, e)
        return []


def _fetch_wewe_rss(source, account_id, max_items, max_age_hours, health_tracker, config):
    """微信公众号（WeWe RSS）：通过自建 WeWe RSS 实例获取文章

    URL 格式：wewe-rss://account_id

    需要在 config.json 中配置 wewe_rss 块：
    {
        "wewe_rss": {
            "base_url": "http://localhost:3000",
        }
    }
    部署方式：docker run -d -p 3000:3000 cooderl/wewe-rss-server
    """
    name = source['name']
    wewe_config = (config or {}).get('wewe_rss', {})
    base_url = wewe_config.get('base_url', 'http://localhost:3000').rstrip('/')

    if not base_url or base_url == 'http://localhost:3000':
        # 检查是否能访问
        check_url = f"{base_url}/feeds"
        try:
            req = urllib.request.Request(check_url, headers={
                'User-Agent': 'AI-Morning-Briefing/2.0'
            })
            urllib.request.urlopen(req, timeout=5, context=get_ssl_context())
        except Exception:
            msg = ("WeWe RSS 实例未运行或无法访问\n"
                   "    请部署: docker run -d -p 3000:3000 cooderl/wewe-rss-server")
            log.warning("⏭️ %s: %s", name, msg)
            if health_tracker:
                health_tracker.record_failure(name, RuntimeError(msg))
            return []

    # WeWe RSS 输出标准 RSS
    rss_url = f"{base_url}/feeds/{account_id}.xml"
    headers = {
        'User-Agent': 'AI-Morning-Briefing/2.0',
        'Accept': 'application/rss+xml, application/xml, text/xml, */*',
    }

    try:
        req = urllib.request.Request(rss_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as resp:
            xml_text = resp.read().decode('utf-8', errors='replace')

        items = parse_rss(xml_text)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=max_age_hours)
        with_date = [i for i in items if i.get('published') and i['published'] >= cutoff]
        no_date = [i for i in items if not i.get('published')]
        filtered = with_date + no_date[:3]
        if not filtered and items:
            filtered = items[:min(3, max_items)]

        for item in filtered[:max_items]:
            item['source_name'] = source['name']
            item['source_icon'] = source['icon']
            item['source_color'] = source['color']
            item['source_category'] = source['category']

        count = len(filtered[:max_items])
        log.info("✅ %s: 获取 %s 条", name, count)
        if health_tracker:
            health_tracker.record_success(name, count)
        return filtered[:max_items]

    except Exception as e:
        log.error("❌ %s: %s", name, e)
        if health_tracker:
            health_tracker.record_failure(name, e)
        return []


def _fetch_wechat_feed(source, feed_id, wechat_config, max_items, max_age_hours, health_tracker):
    """微信公众号：通过 we-mp-rss 实例获取 RSS"""
    name = source['name']
    base_url = wechat_config.get('base_url', 'http://localhost:8001').rstrip('/')
    rss_url = f"{base_url}/feed/{feed_id}.xml"

    headers = {
        'User-Agent': 'AI-Morning-Briefing/2.0',
        'Accept': 'application/rss+xml, application/xml, text/xml, */*',
    }
    auth = wechat_config.get('auth', '')
    if auth:
        headers['Authorization'] = f"AK-SK {auth}"

    try:
        req = urllib.request.Request(rss_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as resp:
            xml_text = resp.read().decode('utf-8', errors='replace')

        items = parse_rss(xml_text)

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=max_age_hours)
        with_date = [i for i in items if i.get('published') and i['published'] >= cutoff]
        no_date = [i for i in items if not i.get('published')]
        filtered = with_date + no_date[:3]
        if not filtered and items:
            filtered = items[:min(3, max_items)]

        for item in filtered[:max_items]:
            item['source_name'] = source['name']
            item['source_icon'] = source['icon']
            item['source_color'] = source['color']
            item['source_category'] = source['category']

        count = len(filtered[:max_items])
        log.info("✅ %s: 获取 %s 条", name, count)
        if health_tracker:
            health_tracker.record_success(name, count)
        return filtered[:max_items]

    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        log.error("❌ %s: %s", name, e)
        if health_tracker:
            health_tracker.record_failure(name, e)
        return []


# ═══════════════════════════════════════════════════════════════════════
# fetch_feed() — 统一入口（协议分发注册表）
# ═══════════════════════════════════════════════════════════════════════

# 协议注册表：prefix → handler(source, payload, max_items, max_age_hours, health_tracker, config)
# handler 接收统一参数签名，内部按需使用

def _handle_youtube(source, payload, max_items, max_age_hours, health_tracker, config):
    return _fetch_youtube_feed(source, payload, max_items, max_age_hours, health_tracker)

def _handle_zhihu(source, payload, max_items, max_age_hours, health_tracker, config):
    return _fetch_zhihu_hot(source, max_items, max_age_hours, health_tracker)

def _handle_twitter(source, payload, max_items, max_age_hours, health_tracker, config):
    return _fetch_twitter_feed(source, payload, max_items, max_age_hours, health_tracker, config)

def _handle_xhs(source, payload, max_items, max_age_hours, health_tracker, config):
    return _fetch_xiaohongshu(source, payload, max_items, max_age_hours, health_tracker, config)

def _handle_wewe_rss(source, payload, max_items, max_age_hours, health_tracker, config):
    return _fetch_wewe_rss(source, payload, max_items, max_age_hours, health_tracker, config)

def _handle_wechat(source, payload, max_items, max_age_hours, health_tracker, config):
    feed_id = payload
    if feed_id == 'FEED_ID_HERE':
        log.warning("⏭️ %s: 占位符未替换，跳过", source['name'])
        return []
    wechat_config = (config or {}).get('wechat_rss', {})
    return _fetch_wechat_feed(source, feed_id, wechat_config, max_items, max_age_hours, health_tracker)


_PROTOCOL_REGISTRY = {
    'youtube://': _handle_youtube,
    'zhihu://':   _handle_zhihu,
    'twitter://': _handle_twitter,
    'xhs://':     _handle_xhs,
    'wewe-rss://': _handle_wewe_rss,
    'wechat://':  _handle_wechat,
}


def fetch_feed(source, max_items=10, max_age_hours=24, health_tracker=None, config=None):
    """抓取单个 RSS 源"""
    name = source['name']
    url = source['url']
    log.info("📡 抓取 %s...", name)
    # 开始计时
    if health_tracker:
        health_tracker.start_timer(name)

    # 协议分发：查找注册表中的处理器
    for prefix, handler in _PROTOCOL_REGISTRY.items():
        if url.startswith(prefix):
            payload = url[len(prefix):]
            return handler(source, payload, max_items, max_age_hours, health_tracker, config)

    # 默认：标准 HTTP RSS 抓取
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/rss+xml, application/xml, text/xml, */*',
    }

    class SmartRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return urllib.request.Request(newurl, headers=dict(req.header_items()))

    try:
        req = urllib.request.Request(url, headers=headers)
        opener = urllib.request.build_opener(SmartRedirectHandler)
        with opener.open(req, timeout=20) as resp:
            data = resp.read()
            for encoding in ['utf-8', 'latin-1', 'gb2312', 'gbk']:
                try:
                    xml_text = data.decode(encoding)
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                xml_text = data.decode('utf-8', errors='replace')

        items = parse_rss(xml_text)

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=max_age_hours)
        with_date = [i for i in items if i.get('published') and i['published'] >= cutoff]
        no_date = [i for i in items if not i.get('published')]
        filtered = with_date + no_date[:3]
        if not filtered and items:
            filtered = items[:min(3, max_items)]

        for item in filtered[:max_items]:
            item['source_name'] = source['name']
            item['source_icon'] = source['icon']
            item['source_color'] = source['color']
            item['source_category'] = source['category']

        count = len(filtered[:max_items])
        log.info("✅ %s: 获取 %s 条", name, count)
        if health_tracker:
            health_tracker.record_success(name, count)
        return filtered[:max_items]

    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ET.ParseError, UnicodeDecodeError) as e:
        log.error("❌ %s: %s", name, e)
        if health_tracker:
            health_tracker.record_failure(name, e)
        return []
