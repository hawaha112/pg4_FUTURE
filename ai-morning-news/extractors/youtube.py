"""
extractors/youtube.py — YouTube-specific content extraction

Handles:
- Fetching YouTube video descriptions from video pages
- Scraping YouTube channel pages for video lists
- Estimating publication dates from YouTube's relative time text
- YouTube RSS feed fetching
"""

import json
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

from logger import get_logger
from http_client import _http_get, get_ssl_context
from rss_parser import clean_html

log = get_logger('youtube_extractor')


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
