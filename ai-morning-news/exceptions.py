"""Custom exceptions for AI Morning News.

This module defines a hierarchy of custom exceptions that distinguish between:
- Expected errors (FetchError) — network timeouts, HTTP errors, parse errors
  (logged as warnings, recorded in health tracker)
- Unexpected program bugs (caught separately, logged with full traceback)
"""


class FetchError(Exception):
    """Expected error during content fetching.

    Includes network timeouts, HTTP errors (4xx, 5xx), DNS failures,
    parse errors (XML, JSON), encoding issues, etc.

    These should be logged as warnings (level=warning) and recorded
    in the health tracker for monitoring.
    """
    pass


class SourceUnavailableError(FetchError):
    """Source is temporarily unavailable.

    Covers HTTP 503, timeouts, DNS resolution failures, connection refused, etc.
    Indicates the source itself is down, not a problem with our request.
    """
    pass


class ParseError(FetchError):
    """Failed to parse content (RSS, HTML, JSON, XML).

    Covers XML parse errors, JSON decode errors, malformed HTML, etc.
    """
    pass
