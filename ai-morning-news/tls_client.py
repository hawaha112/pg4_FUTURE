"""tls_client.py — HTTP client that evades Cloudflare TLS fingerprint checks.

Cloudflare 的高阶防护（如 Axios / Bloomberg 等站）不仅看 User-Agent，还看 TLS
握手指纹（ja3/ja4）。Python `ssl` 的默认 cipher/extension 组合与真实 Chrome 不
同，会被识别为爬虫并返回 403。

本模块优先用 `curl_cffi`（绑定 BoringSSL + 模拟真实浏览器 TLS fingerprint）抓
取；未安装时回退 urllib.request（能过普通站，Cloudflare 严审站会 403）。

统一接口与 urllib 语义对齐：返回 (bytes, status, final_url)，4xx/5xx 抛
HTTPError。
"""

import urllib.request
import urllib.error
from typing import Optional, Dict, Tuple

from logger import get_logger

log = get_logger('tls_client')

try:
    from curl_cffi import requests as _cffi_requests
    _HAS_CURL_CFFI = True
except ImportError:  # pragma: no cover — 可选依赖
    _HAS_CURL_CFFI = False
    log.info("curl_cffi 未安装，Cloudflare 严审站（如 Axios）可能被 403。"
             " pip install curl_cffi 以启用 Chrome TLS 指纹伪装。")

# chrome124 是 curl_cffi 内建较新、稳定的浏览器 fingerprint
_IMPERSONATE = "chrome124"

_DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/121.0.0.0 Safari/537.36',
    'Accept': 'application/rss+xml, application/atom+xml, application/xml;q=0.9, '
              'text/xml;q=0.8, */*;q=0.5',
    'Accept-Language': 'en-US,en;q=0.9',
}


def fetch_bytes(url: str, headers: Optional[Dict[str, str]] = None,
                timeout: int = 20, allow_redirects: bool = True,
                max_bytes: int = 10_000_000) -> Tuple[bytes, int, str]:
    """获取 URL 原始字节。

    Returns:
        (body: bytes, status: int, final_url: str)
    Raises:
        urllib.error.HTTPError: status >= 400（与 urllib 语义对齐）
        OSError / urllib.error.URLError: 网络层错误
    """
    merged = dict(_DEFAULT_HEADERS)
    if headers:
        merged.update(headers)

    if _HAS_CURL_CFFI:
        try:
            r = _cffi_requests.get(
                url,
                headers=merged,
                timeout=timeout,
                allow_redirects=allow_redirects,
                impersonate=_IMPERSONATE,
            )
            data = r.content[:max_bytes] if r.content else b''
            if r.status_code >= 400:
                raise urllib.error.HTTPError(
                    url, r.status_code, f"HTTP {r.status_code}",
                    dict(r.headers) if r.headers else {}, None
                )
            return data, r.status_code, str(r.url)
        except urllib.error.HTTPError:
            raise
        except Exception as e:
            # curl_cffi 内部异常 → 回退 urllib，给普通站一个机会
            log.debug("curl_cffi 失败 %s: %s，回退 urllib", url, e)

    return _fetch_urllib(url, merged, timeout, max_bytes)


def _fetch_urllib(url, headers, timeout, max_bytes):
    """urllib 回退路径，含 SmartRedirectHandler 保留跨域 header。"""

    class _Redir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, h, newurl):
            new_req = super().redirect_request(req, fp, code, msg, h, newurl)
            if new_req is not None:
                existing = {k.lower() for k, _ in new_req.header_items()}
                for k, v in req.header_items():
                    if k.lower() not in existing:
                        new_req.add_header(k, v)
            return new_req

    req = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_Redir)
    with opener.open(req, timeout=timeout) as resp:
        return resp.read(max_bytes), resp.status, resp.url


def is_curl_cffi_available() -> bool:
    """暴露给日志/诊断用，便于运维知道当前能否过 Cloudflare 严审。"""
    return _HAS_CURL_CFFI
