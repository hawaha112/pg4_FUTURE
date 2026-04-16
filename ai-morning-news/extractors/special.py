"""
extractors/special.py — Special source handlers

Handles:
- Hacker News (HN) — resolving real article URLs from comment pages
- GitHub — fetching README content
- Zhihu — fetching hot topics via mobile API
- Twitter/X — fetching tweets via Nitter RSS mirrors
- Xiaohongshu — fetching notes via SSR data
- WeChat — fetching WeChat Official Account articles
- WeWe RSS — fetching WeChat articles via self-hosted service
"""

import json
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

from logger import get_logger
from http_client import _http_get, get_ssl_context
from rss_parser import clean_html, parse_rss

log = get_logger('special_extractors')


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

    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, json.JSONDecodeError) as e:
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

    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError) as e:
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

    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, json.JSONDecodeError) as e:
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

    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError) as e:
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
