"""
extractors/ — Article content extraction and special source handlers

Sub-modules:
- article: Generic article body/image extraction
- youtube: YouTube-specific extraction
- special: Other special sources (HN, GitHub, Zhihu, Twitter, Xiaohongshu, WeChat, WeWe RSS)

Re-exports key functions for backward compatibility.
"""

from .article import (
    enrich_articles_with_content,
    _extract_article_body,
    _extract_article_images,
    _fetch_article_text,
    _fetch_article_text_and_images,
)

__all__ = [
    'enrich_articles_with_content',
    '_extract_article_body',
    '_extract_article_images',
    '_fetch_article_text',
    '_fetch_article_text_and_images',
]
