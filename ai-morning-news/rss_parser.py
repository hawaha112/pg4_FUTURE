"""
rss_parser.py — RSS/Atom feed 解析（纯标准库）

支持 RSS 2.0 和 Atom 格式，自动处理命名空间、CDATA、日期格式。

用法:
    items = parse_rss(xml_text)
    # items: [{"title", "link", "summary", "published", "image"}, ...]
"""

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from logger import get_logger

log = get_logger('rss_parser')


def _text(el, tag, namespaces=None):
    """安全提取子元素文本"""
    if namespaces:
        for ns_prefix, ns_uri in namespaces.items():
            child = el.find(f'{{{ns_uri}}}{tag}')
            if child is not None and child.text:
                return child.text.strip()
    child = el.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return ""


def _parse_date(date_str):
    """解析各种日期格式（使用 dateutil 自动识别，回退到手动模式）"""
    if not date_str:
        return None
    date_str = date_str.strip()
    # 优先使用 dateutil（自动处理 RFC 2822、ISO 8601、各种时区缩写等）
    try:
        from dateutil import parser as dateutil_parser
        dt = dateutil_parser.parse(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, OverflowError):
        pass
    except ImportError:
        pass  # dateutil 未安装，回退手动解析
    # 手动格式匹配（兜底）
    for fmt in ("%a, %d %b %Y %H:%M:%S %z",
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    log.debug("无法解析日期: %s", date_str)
    return None


def clean_html(text):
    """移除 HTML 标签，提取纯文本"""
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&quot;', '"', text)
    text = re.sub(r'&#\d+;', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _extract_image(el, namespaces=None):
    """尝试从条目中提取图片 URL"""
    if namespaces and 'media' in namespaces:
        ns = namespaces['media']
        for tag in ['thumbnail', 'content']:
            media = el.find(f'{{{ns}}}{tag}')
            if media is not None:
                url = media.get('url', '')
                if url:
                    return url
    enc = el.find('enclosure')
    if enc is not None:
        enc_type = enc.get('type', '')
        if 'image' in enc_type:
            return enc.get('url', '')
    for tag in ['description', 'content', 'summary']:
        child = el.find(tag)
        if child is not None and child.text:
            m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', child.text)
            if m:
                return m.group(1)
    if namespaces:
        for ns_prefix, ns_uri in namespaces.items():
            child = el.find(f'{{{ns_uri}}}encoded')
            if child is not None and child.text:
                m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', child.text)
                if m:
                    return m.group(1)
    return ""


def parse_rss(xml_text):
    """解析 RSS/Atom feed，返回条目列表"""
    items = []
    try:
        namespaces = {}
        for m in re.finditer(r'xmlns:(\w+)=["\']([^"\']+)["\']', xml_text[:3000]):
            namespaces[m.group(1)] = m.group(2)
        xml_text = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#)', '&amp;', xml_text)
        xml_text = xml_text.lstrip('\ufeff')
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            cleaned = re.sub(r'<!\[CDATA\[.*?\]\]>', '', xml_text, flags=re.DOTALL)
            root = ET.fromstring(cleaned)
        ns = root.tag.split('}')[0].strip('{') if '}' in root.tag else ''

        if 'atom' in ns.lower() or root.tag.endswith('feed'):
            atom_ns = ns if ns else 'http://www.w3.org/2005/Atom'
            entries = root.findall(f'{{{atom_ns}}}entry')
            if not entries:
                entries = root.findall('entry')
            for entry in entries:
                title_el = entry.find(f'{{{atom_ns}}}title') if atom_ns else entry.find('title')
                title = title_el.text.strip() if title_el is not None and title_el.text else ""
                link = ""
                link_els = entry.findall(f'{{{atom_ns}}}link')
                if not link_els:
                    link_els = entry.findall('link')
                for link_el in link_els:
                    rel = link_el.get('rel', 'alternate')
                    if rel == 'alternate' or not link:
                        link = link_el.get('href', '')
                summary_el = entry.find(f'{{{atom_ns}}}summary')
                if summary_el is None:
                    summary_el = entry.find('summary')
                content_el = entry.find(f'{{{atom_ns}}}content')
                if content_el is None:
                    content_el = entry.find('content')
                summary = ""
                if content_el is not None and content_el.text:
                    summary = clean_html(content_el.text)
                elif summary_el is not None and summary_el.text:
                    summary = clean_html(summary_el.text)
                pub_el = entry.find(f'{{{atom_ns}}}published')
                if pub_el is None:
                    pub_el = entry.find(f'{{{atom_ns}}}updated')
                if pub_el is None:
                    pub_el = entry.find('published')
                if pub_el is None:
                    pub_el = entry.find('updated')
                pub_date = _parse_date(pub_el.text if pub_el is not None else "")
                image = _extract_image(entry, namespaces)
                if title:
                    items.append({
                        'title': title, 'link': link,
                        'summary': summary[:800] if summary else "",
                        'published': pub_date, 'image': image,
                    })
        else:
            channel = root.find('channel')
            if channel is None:
                channel = root
            for item in channel.findall('item'):
                title = _text(item, 'title') or ""
                link = _text(item, 'link') or ""
                description = ""
                for tag in ['description', 'summary']:
                    desc = _text(item, tag)
                    if desc:
                        description = clean_html(desc)
                        break
                content_encoded = ""
                if namespaces:
                    for ns_prefix, ns_uri in namespaces.items():
                        encoded = item.find(f'{{{ns_uri}}}encoded')
                        if encoded is not None and encoded.text:
                            content_encoded = clean_html(encoded.text)
                            break
                best_summary = content_encoded if len(content_encoded) > len(description) else description
                pub_str = _text(item, 'pubDate') or _text(item, 'dc:date') or ""
                if not pub_str and namespaces:
                    for ns_prefix, ns_uri in namespaces.items():
                        pub_str = _text(item, 'date', {ns_prefix: ns_uri})
                        if pub_str:
                            break
                pub_date = _parse_date(pub_str)
                image = _extract_image(item, namespaces)
                if title:
                    items.append({
                        'title': title, 'link': link,
                        'summary': best_summary[:800] if best_summary else "",
                        'published': pub_date, 'image': image,
                    })
    except ET.ParseError as e:
        log.error("[XML解析错误] %s", e)
    except (KeyError, ValueError, AttributeError) as e:
        log.error("[解析异常] %s", e)
    return items
