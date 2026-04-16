"""
http_client.py — SSL context management + HTTP GET utility

Handles:
- SSL context creation with certificate fallback strategies
- Lazy-loaded singleton SSL context
- Generic HTTP GET with proper headers and encoding detection
"""

import ssl
import urllib.request
import urllib.error

from logger import get_logger

log = get_logger('http_client')


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
