"""
AI Morning Briefing — 统一日志模块

所有模块统一使用 logging，区分 DEBUG/INFO/WARNING/ERROR 级别。
在 launchd 环境中可通过环境变量或参数调整级别。

用法:
    from logger import get_logger
    log = get_logger(__name__)
    log.info("✅ 抓取成功: %d 条", count)
    log.warning("⚠️ 响应超时")
    log.error("❌ 解析失败: %s", e)
    log.debug("调试信息...")

环境变量:
    LOG_LEVEL=DEBUG|INFO|WARNING|ERROR  （默认 INFO）
"""

import logging
import os
import sys

_INITIALIZED = False


def setup_logging(level: str = None):
    """初始化根日志器（幂等，只生效一次）"""
    global _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True

    if level is None:
        level = os.environ.get('LOG_LEVEL', 'INFO').upper()

    numeric_level = getattr(logging, level, logging.INFO)

    # 格式：紧凑、适合终端和日志文件
    fmt = '%(asctime)s [%(levelname).1s] %(name)s: %(message)s'
    datefmt = '%H:%M:%S'

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))

    root = logging.getLogger()
    root.setLevel(numeric_level)
    # 清除已有 handler，避免重复输出
    root.handlers.clear()
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """获取模块级 logger（自动初始化根日志器）"""
    setup_logging()
    return logging.getLogger(name)
