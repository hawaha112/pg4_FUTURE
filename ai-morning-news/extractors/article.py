"""
extractors/article.py — Generic article body and image extraction

Handles:
- Article body extraction using multiple strategies (article tags, semantic classes, JSON-LD, meta tags, paragraphs)
- Article image extraction with noise filtering
- Batch enrichment of articles with full content and images
"""

import json
import re
import concurrent.futures
import urllib.error
from urllib.parse import urlparse

from logger import get_logger
from http_client import _http_get, get_ssl_context
from rss_parser import clean_html

log = get_logger('article_extractor')


_PAPER_SITES_NO_IMAGE = (
    'arxiv.org',
    'openreview.net',
    'aclanthology.org',
    'papers.nips.cc',
    'proceedings.mlr.press',
)


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
        from .special import _resolve_hn_real_url, _fetch_github_readme
        from .youtube import _fetch_youtube_description

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


def _fetch_article_text_and_images(url, source_name=""):
    """抓取文章原文 + 提取文章内图片"""
    if not url or url == '#':
        return "", []
    try:
        from .special import _resolve_hn_real_url, _fetch_github_readme
        from .youtube import _fetch_youtube_description

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
                # Expected — network failure during content enrichment
                pass
            except Exception:
                # Unexpected program bug during content enrichment
                idx = futures.get(future, None)
                if idx is not None:
                    log.warning("⚠️ 文章 #%d 内容抓取意外错误", idx, exc_info=True)
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
