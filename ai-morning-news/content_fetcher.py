"""
content_fetcher.py — RSS feed fetching & article content extraction (refactored facade)

主要入口，负责：
- fetch_feed() — 统一的 RSS 源抓取入口（协议分发）
- enrich_articles_with_content() — 批量抓取文章原文 + 图片
- SourceHealthTracker — 源健康度监控

具体实现分解到子模块：
- http_client.py — SSL 上下文管理 + HTTP GET
- health_tracker.py — SourceHealthTracker 类
- extractors/article.py — 通用文章正文/图片提取
- extractors/youtube.py — YouTube 视频专用处理
- extractors/special.py — 其他特殊源（HN、GitHub、知乎、Twitter、小红书、微信等）

向后兼容：所有子模块的导出都在此文件重新导出，保证现有的 from content_fetcher import ... 语句继续工作。
"""

import json
import re
import xml.etree.ElementTree as ET
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

from logger import get_logger

# 从子模块导入
from health_tracker import SourceHealthTracker
from extractors.article import enrich_articles_with_content
from extractors.special import (
    _fetch_zhihu_hot,
    _fetch_twitter_feed,
    _fetch_xiaohongshu,
    _fetch_wewe_rss,
    _fetch_wechat_feed,
)
from extractors.youtube import _scrape_youtube_channel
from http_client import get_ssl_context
from rss_parser import parse_rss

log = get_logger('content_fetcher')

# 重新导出，保证向后兼容
__all__ = [
    'fetch_feed',
    'enrich_articles_with_content',
    'SourceHealthTracker',
]


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

    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, ET.ParseError) as e:
        # Expected network/parse errors — log as warning
        log.warning("⚠️ %s: %s", name, e)
        if health_tracker:
            health_tracker.record_failure(name, e)
        return []
    except Exception as e:
        # Unexpected program bug — log full traceback
        log.error("❌ %s: 意外错误", name, exc_info=True)
        if health_tracker:
            health_tracker.record_failure(name, e)
        return []


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

    # 检查是否应由于连续失败而自动跳过
    if health_tracker and health_tracker.should_skip(name):
        log.warning("⏭️ %s: 连续失败超过阈值，自动跳过（每 24h 重试一次）", name)
        return []

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
        # Expected network/parse errors — log as warning
        log.warning("⚠️ %s: %s", name, e)
        if health_tracker:
            health_tracker.record_failure(name, e)
        return []
    except Exception as e:
        # Unexpected program bug — log full traceback
        log.error("❌ %s: 意外错误", name, exc_info=True)
        if health_tracker:
            health_tracker.record_failure(name, e)
        return []
