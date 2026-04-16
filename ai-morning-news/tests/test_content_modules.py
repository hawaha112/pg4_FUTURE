"""
test_content_modules.py — Tests for refactored content fetcher modules

Covers:
- http_client.py: HTTP GET with encoding fallback
- health_tracker.py: SourceHealthTracker with alerts and auto-skip
- extractors/article.py: Article body and image extraction strategies
- extractors/youtube.py: YouTube date estimation
- config_validator.py: Configuration validation
- exceptions.py: Exception hierarchy
"""

import os
import sys
import json
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from http_client import _http_get, get_ssl_context
from health_tracker import SourceHealthTracker
from extractors.article import _extract_article_body, _extract_article_images
from extractors.youtube import _estimate_youtube_date
from config_validator import validate_config
from exceptions import FetchError, SourceUnavailableError, ParseError


# ═══════════════════════════════════════════════════════════════════════
# http_client Tests
# ═══════════════════════════════════════════════════════════════════════

class TestHttpClient(unittest.TestCase):
    """Test HTTP GET with mock urllib.request.urlopen"""

    @patch('urllib.request.urlopen')
    def test_http_get_success_utf8(self, mock_urlopen):
        """Test successful HTTP GET with UTF-8 encoding"""
        mock_response = MagicMock()
        mock_response.read.return_value = b'Hello World'
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = None
        mock_urlopen.return_value = mock_response

        result = _http_get('https://example.com')
        assert result == 'Hello World'
        mock_urlopen.assert_called_once()

    @patch('urllib.request.urlopen')
    def test_http_get_encoding_fallback_latin1(self, mock_urlopen):
        """Test encoding fallback: UTF-8 fails, falls back to latin-1"""
        # Byte sequence that is valid latin-1 but invalid UTF-8
        invalid_utf8 = b'\xff\xfe'
        mock_response = MagicMock()
        mock_response.read.return_value = invalid_utf8
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = None
        mock_urlopen.return_value = mock_response

        result = _http_get('https://example.com')
        # Should decode as latin-1
        assert result == '\xff\xfe'

    @patch('urllib.request.urlopen')
    def test_http_get_encoding_fallback_gb2312(self, mock_urlopen):
        """Test encoding fallback: Chinese GB2312"""
        # 中文 "你好" in GB2312
        gb2312_bytes = '你好'.encode('gb2312')
        mock_response = MagicMock()
        mock_response.read.return_value = gb2312_bytes
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = None
        mock_urlopen.return_value = mock_response

        result = _http_get('https://example.com')
        assert '你好' in result

    @patch('urllib.request.urlopen')
    def test_http_get_encoding_fallback_replace(self, mock_urlopen):
        """Test encoding fallback with errors='replace' for truly invalid bytes"""
        # Some arbitrary bytes that fail all encodings
        bad_bytes = bytes([0x80, 0x81, 0x82])
        mock_response = MagicMock()
        mock_response.read.return_value = bad_bytes
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = None
        mock_urlopen.return_value = mock_response

        result = _http_get('https://example.com')
        # Should fall back to UTF-8 with errors='replace'
        assert isinstance(result, str)

    @patch('urllib.request.urlopen')
    def test_http_get_user_agent_header(self, mock_urlopen):
        """Test that User-Agent header is set"""
        mock_response = MagicMock()
        mock_response.read.return_value = b'OK'
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = None
        mock_urlopen.return_value = mock_response

        _http_get('https://example.com')

        # Check that urlopen was called with a Request object
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        assert 'Mozilla' in request_obj.headers.get('User-agent', '')

    @patch('urllib.request.urlopen')
    def test_http_get_size_limit(self, mock_urlopen):
        """Test that read() limits data to 800KB"""
        mock_response = MagicMock()
        mock_response.read.return_value = b'X' * 100000  # 100KB
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = None
        mock_urlopen.return_value = mock_response

        _http_get('https://example.com')
        # Verify read was called with 800_000 limit
        mock_response.read.assert_called_once_with(800_000)


# ═══════════════════════════════════════════════════════════════════════
# health_tracker Tests
# ═══════════════════════════════════════════════════════════════════════

class TestSourceHealthTracker(unittest.TestCase):
    """Test SourceHealthTracker with temp file"""

    def setUp(self):
        """Create temp file for health data"""
        self.temp_dir = tempfile.mkdtemp()
        self.health_file = os.path.join(self.temp_dir, 'health.json')
        self.tracker = SourceHealthTracker(self.health_file, alert_threshold=3)

    def tearDown(self):
        """Clean up temp files"""
        if os.path.exists(self.health_file):
            os.remove(self.health_file)
        os.rmdir(self.temp_dir)

    def test_record_success(self):
        """Test recording a successful fetch"""
        self.tracker.start_timer('source1')
        time.sleep(0.01)  # Small delay to ensure elapsed > 0
        self.tracker.record_success('source1', item_count=5)

        data = self.tracker.data['source1']
        assert data['status'] == 'ok'
        assert data['consecutive_failures'] == 0
        assert data['last_count'] == 5
        assert data['total_successes'] == 1
        assert data['total_runs'] == 1
        assert data['success_rate'] == 100.0

    def test_record_failure(self):
        """Test recording a failed fetch"""
        self.tracker.start_timer('source1')
        time.sleep(0.01)
        self.tracker.record_failure('source1', error='Network timeout')

        data = self.tracker.data['source1']
        assert data['status'] == 'failing'
        assert data['consecutive_failures'] == 1
        assert data['last_error'] == 'Network timeout'
        assert data['total_runs'] == 1
        assert data['total_successes'] == 0
        assert data['success_rate'] == 0.0

    def test_consecutive_failures(self):
        """Test tracking consecutive failures"""
        for i in range(3):
            self.tracker.start_timer('source1')
            self.tracker.record_failure('source1', error=f'Error {i}')

        data = self.tracker.data['source1']
        assert data['consecutive_failures'] == 3
        assert data['total_runs'] == 3

    def test_reset_on_success(self):
        """Test that consecutive_failures resets on success"""
        # Record 2 failures
        self.tracker.record_failure('source1', error='Error 1')
        self.tracker.record_failure('source1', error='Error 2')
        assert self.tracker.data['source1']['consecutive_failures'] == 2

        # Record success
        self.tracker.start_timer('source1')
        self.tracker.record_success('source1', item_count=3)
        assert self.tracker.data['source1']['consecutive_failures'] == 0
        assert self.tracker.data['source1']['total_successes'] == 1

    def test_get_alerts_below_threshold(self):
        """Test that alerts are empty when failures < threshold"""
        self.tracker.record_failure('source1', error='Error')
        self.tracker.record_failure('source1', error='Error')
        alerts = self.tracker.get_alerts()
        assert len(alerts) == 0

    def test_get_alerts_at_threshold(self):
        """Test that alerts fire when failures >= threshold"""
        # Tracker initialized with alert_threshold=3
        for _ in range(3):
            self.tracker.record_failure('source1', error='Network error')

        alerts = self.tracker.get_alerts()
        assert len(alerts) == 1
        assert alerts[0]['source'] == 'source1'
        assert alerts[0]['consecutive_failures'] == 3

    def test_should_skip_below_auto_skip_threshold(self):
        """Test should_skip returns False when below threshold"""
        for _ in range(5):
            self.tracker.record_failure('source1', error='Error')

        # auto_skip_threshold=10, consecutive_failures=5
        should_skip = self.tracker.should_skip('source1', auto_skip_threshold=10)
        assert should_skip is False

    def test_should_skip_at_auto_skip_threshold(self):
        """Test should_skip returns True when at threshold"""
        for _ in range(10):
            self.tracker.record_failure('source1', error='Error')

        # First call should return False and record retry time
        should_skip = self.tracker.should_skip('source1', auto_skip_threshold=10)
        assert should_skip is False

        # Second call within 24 hours should return True
        should_skip = self.tracker.should_skip('source1', auto_skip_threshold=10)
        assert should_skip is True

    def test_should_skip_after_24_hours(self):
        """Test should_skip allows retry after 24 hours"""
        # Record 10 failures
        for _ in range(10):
            self.tracker.record_failure('source1', error='Error')

        # First should_skip call
        self.tracker.should_skip('source1', auto_skip_threshold=10)

        # Manually set last_retry to 24+ hours ago
        self.tracker.data['source1']['last_auto_skip_retry'] = (
            datetime.now(timezone.utc) - timedelta(hours=25)
        ).isoformat()

        # Should allow retry now
        should_skip = self.tracker.should_skip('source1', auto_skip_threshold=10)
        assert should_skip is False

    def test_save_and_load(self):
        """Test persistence: save and load health data"""
        self.tracker.record_success('source1', item_count=5)
        self.tracker.record_failure('source2', error='Test error')
        self.tracker.save()

        # Load from file
        tracker2 = SourceHealthTracker(self.health_file)
        assert tracker2.data['source1']['status'] == 'ok'
        assert tracker2.data['source2']['status'] == 'failing'
        assert tracker2.data['source2']['last_error'] == 'Test error'

    def test_load_nonexistent_file(self):
        """Test loading from non-existent file returns empty dict"""
        nonexistent = os.path.join(self.temp_dir, 'nonexistent.json')
        tracker = SourceHealthTracker(nonexistent)
        assert tracker.data == {}

    def test_load_corrupted_json(self):
        """Test loading from corrupted JSON file falls back to empty dict"""
        with open(self.health_file, 'w') as f:
            f.write('{ invalid json }')

        tracker = SourceHealthTracker(self.health_file)
        assert tracker.data == {}


# ═══════════════════════════════════════════════════════════════════════
# article extractor Tests
# ═══════════════════════════════════════════════════════════════════════

class TestExtractArticleBody(unittest.TestCase):
    """Test article body extraction with multiple strategies"""

    def test_extract_from_article_tag(self):
        """Strategy 1: Extract from <article> tag"""
        html = '''<article>
            <h1>Title</h1>
            <p>This is the main article content with substantial length to pass 100 char threshold.</p>
        </article>'''
        result = _extract_article_body(html)
        assert 'main article content' in result

    def test_extract_from_semantic_class(self):
        """Strategy 2: Extract from semantic class names"""
        html = '''<div class="article-body">
            <p>Content from semantic class pattern here in the article body section.</p>
        </div>'''
        result = _extract_article_body(html)
        assert 'semantic class' in result

    def test_extract_from_json_ld(self):
        """Strategy 3: Extract from JSON-LD articleBody"""
        html = '''<script type="application/ld+json">
        {"articleBody": "This is content from JSON-LD with sufficient length for extraction."}
        </script>'''
        result = _extract_article_body(html)
        assert 'JSON-LD' in result

    def test_extract_from_meta_description(self):
        """Strategy 4: Extract from meta description"""
        html = '''<meta name="description" content="This is a long description meta tag with enough characters to pass minimum length.">'''
        result = _extract_article_body(html)
        assert 'description meta tag' in result

    def test_extract_from_paragraphs(self):
        """Strategy 5: Extract from <p> tags"""
        html = '''<div>
            <p>First paragraph with substantial content for extraction purposes.</p>
            <p>Second paragraph also with good content to combine into article text.</p>
        </div>'''
        result = _extract_article_body(html)
        assert 'First paragraph' in result
        assert 'Second paragraph' in result

    def test_strip_script_and_style(self):
        """Test removal of script/style/nav tags"""
        html = '''<script>alert("xss");</script>
        <style>.hidden { display: none; }</style>
        <article><p>Real content that we want here and it is long.</p></article>
        <nav>Navigation</nav>'''
        result = _extract_article_body(html)
        assert 'alert' not in result
        assert 'Real content' in result

    def test_empty_html(self):
        """Test empty HTML returns empty string"""
        assert _extract_article_body("") == ""
        assert _extract_article_body(None) == ""

    def test_html_without_content(self):
        """Test HTML without sufficient content returns empty string"""
        html = '<div><p>Short</p></div>'
        result = _extract_article_body(html)
        assert result == ""

    def test_noise_removal(self):
        """Test removal of common noise patterns"""
        html = '''<article>
            <p>Real content here that is long enough to pass the threshold requirement.</p>
            <p>Subscribe to our newsletter for updates and more content!</p>
            <p>Follow us on social media for additional information.</p>
        </article>'''
        result = _extract_article_body(html)
        assert 'Real content' in result
        assert 'Subscribe' not in result or 'newsletter' not in result

    def test_max_length_truncation(self):
        """Test that result is truncated to 4000 chars"""
        # Create HTML with article longer than 4000 chars
        large_content = 'word ' * 1000  # ~5000 chars
        html = f'<article>{large_content}</article>'
        result = _extract_article_body(html)
        assert len(result) <= 4000


class TestExtractArticleImages(unittest.TestCase):
    """Test article image extraction"""

    def test_extract_og_image(self):
        """Test extraction of og:image meta tag"""
        html = '<meta property="og:image" content="https://example.com/image.jpg">'
        images = _extract_article_images(html)
        assert 'https://example.com/image.jpg' in images

    def test_extract_twitter_image(self):
        """Test extraction of twitter:image meta tag"""
        html = '<meta property="twitter:image" content="https://example.com/tweet.png">'
        images = _extract_article_images(html)
        assert 'https://example.com/tweet.png' in images

    def test_extract_img_tag(self):
        """Test extraction from <img> tags"""
        html = '''<article>
            <img src="https://example.com/article.jpg" alt="Article">
        </article>'''
        images = _extract_article_images(html)
        assert 'https://example.com/article.jpg' in images

    def test_extract_img_lazy_load_attributes(self):
        """Test extraction from lazy-load data attributes"""
        html = '''<article>
            <img data-src="https://example.com/lazy.jpg">
        </article>'''
        images = _extract_article_images(html)
        assert 'https://example.com/lazy.jpg' in images

    def test_filter_noise_images(self):
        """Test filtering of noise keywords"""
        html = '''<img src="data:image/png;base64,...">
        <img src="https://example.com/logo.svg">
        <img src="https://gravatar.com/avatar.jpg">
        <img src="https://example.com/article-image.jpg">'''
        images = _extract_article_images(html)
        # Should include article-image, exclude logo, svg, gravatar
        assert any('article-image' in img for img in images)
        assert not any('logo' in img for img in images)
        assert not any('gravatar' in img for img in images)

    def test_filter_tracking_pixels(self):
        """Test filtering of tracking pixels"""
        html = '''<img src="https://example.com/1x1.gif">
        <img src="https://example.com/pixel?v=1">
        <img src="https://google-analytics.com/collect?v=1">
        <img src="https://example.com/real-image.jpg">'''
        images = _extract_article_images(html)
        assert any('real-image' in img for img in images)
        assert not any('1x1' in img or 'pixel' in img or 'tracking' in img for img in images)

    def test_filter_social_icons(self):
        """Test filtering of social media icons"""
        html = '''<img src="https://example.com/facebook-icon.png">
        <img src="https://example.com/tweet-button.jpg">
        <img src="https://example.com/content-image.jpg">'''
        images = _extract_article_images(html)
        assert any('content-image' in img for img in images)
        assert not any('facebook' in img or 'tweet' in img for img in images)

    def test_resolve_relative_urls(self):
        """Test resolution of relative URLs"""
        html = '''<img src="/images/article.jpg">'''
        images = _extract_article_images(html, base_url="https://example.com/page")
        assert any('example.com' in img for img in images)

    def test_resolve_protocol_relative_urls(self):
        """Test resolution of protocol-relative URLs"""
        html = '''<img src="//cdn.example.com/image.jpg">'''
        images = _extract_article_images(html)
        assert any('https://cdn.example.com' in img for img in images)

    def test_extract_json_ld_image(self):
        """Test extraction from JSON-LD image field"""
        html = '''<script type="application/ld+json">
        {"image": "https://example.com/json-ld-image.jpg"}
        </script>'''
        images = _extract_article_images(html)
        assert 'https://example.com/json-ld-image.jpg' in images

    def test_max_images_returned(self):
        """Test that max 4 images are returned"""
        html = ''.join([
            f'<img src="https://example.com/img{i}.jpg">' for i in range(10)
        ])
        images = _extract_article_images(html)
        assert len(images) <= 4

    def test_deduplicate_images(self):
        """Test that duplicate URLs are filtered"""
        html = '''<img src="https://example.com/same.jpg">
        <img src="https://example.com/same.jpg">'''
        images = _extract_article_images(html)
        assert images.count('https://example.com/same.jpg') == 1


# ═══════════════════════════════════════════════════════════════════════
# youtube extractor Tests
# ═══════════════════════════════════════════════════════════════════════

class TestEstimateYoutubeDate(unittest.TestCase):
    """Test YouTube date estimation"""

    def setUp(self):
        """Set up a fixed 'now' for consistent testing"""
        self.now = datetime(2025, 4, 7, 12, 0, 0, tzinfo=timezone.utc)

    def test_estimate_days_ago_full_format(self):
        """Test '3 days ago' format"""
        result = _estimate_youtube_date('3 days ago', self.now)
        expected = self.now - timedelta(days=3)
        assert result == expected

    def test_estimate_days_ago_abbreviated(self):
        """Test '3d ago' abbreviated format"""
        result = _estimate_youtube_date('3d ago', self.now)
        expected = self.now - timedelta(days=3)
        assert result == expected

    def test_estimate_weeks_ago(self):
        """Test '2 weeks ago' format"""
        result = _estimate_youtube_date('2 weeks ago', self.now)
        expected = self.now - timedelta(weeks=2)
        assert result == expected

    def test_estimate_weeks_ago_abbreviated(self):
        """Test '2w ago' abbreviated format"""
        result = _estimate_youtube_date('2w ago', self.now)
        expected = self.now - timedelta(weeks=2)
        assert result == expected

    def test_estimate_hours_ago(self):
        """Test '4 hours ago' format"""
        result = _estimate_youtube_date('4 hours ago', self.now)
        expected = self.now - timedelta(hours=4)
        assert result == expected

    def test_estimate_hours_ago_abbreviated(self):
        """Test '4h ago' abbreviated format"""
        result = _estimate_youtube_date('4h ago', self.now)
        expected = self.now - timedelta(hours=4)
        assert result == expected

    def test_estimate_minutes_ago(self):
        """Test '30 minutes ago' format"""
        result = _estimate_youtube_date('30 minutes ago', self.now)
        expected = self.now - timedelta(minutes=30)
        assert result == expected

    def test_estimate_seconds_ago(self):
        """Test '45 seconds ago' format"""
        result = _estimate_youtube_date('45 seconds ago', self.now)
        expected = self.now - timedelta(seconds=45)
        assert result == expected

    def test_estimate_months_ago(self):
        """Test 'month' handling (30 days approximation)"""
        result = _estimate_youtube_date('1 month ago', self.now)
        expected = self.now - timedelta(days=30)
        assert result == expected

    def test_estimate_years_ago(self):
        """Test 'year' handling (365 days approximation)"""
        result = _estimate_youtube_date('1 year ago', self.now)
        expected = self.now - timedelta(days=365)
        assert result == expected

    def test_estimate_years_ago_abbreviated(self):
        """Test '1y ago' abbreviated format"""
        result = _estimate_youtube_date('1y ago', self.now)
        expected = self.now - timedelta(days=365)
        assert result == expected

    def test_estimate_empty_string(self):
        """Test empty string returns None"""
        assert _estimate_youtube_date('', self.now) is None
        assert _estimate_youtube_date(None, self.now) is None

    def test_estimate_invalid_format(self):
        """Test invalid format returns None"""
        assert _estimate_youtube_date('invalid text', self.now) is None

    def test_estimate_case_insensitive(self):
        """Test that parsing is case-insensitive"""
        result1 = _estimate_youtube_date('3 DAYS AGO', self.now)
        result2 = _estimate_youtube_date('3 days ago', self.now)
        assert result1 == result2

    def test_estimate_singular_form(self):
        """Test singular form like '1 day ago'"""
        result = _estimate_youtube_date('1 day ago', self.now)
        expected = self.now - timedelta(days=1)
        assert result == expected


# ═══════════════════════════════════════════════════════════════════════
# config_validator Tests
# ═══════════════════════════════════════════════════════════════════════

class TestConfigValidator(unittest.TestCase):
    """Test configuration validation"""

    def test_valid_config(self):
        """Test that valid config passes validation"""
        config = {
            'llm': {
                'enabled': False,
            },
            'sources': {
                'english': [
                    {
                        'name': 'TechNews',
                        'url': 'https://example.com/feed',
                        'icon': '📰',
                        'color': '#FF0000',
                        'category': 'tech',
                    }
                ],
                'chinese': [],
            },
            'settings': {
                'max_items_per_source': 10,
                'max_age_hours': 24,
            },
        }
        issues = validate_config(config)
        assert issues == []

    def test_missing_required_top_level_keys(self):
        """Test detection of missing required top-level keys"""
        config = {'llm': {}}
        issues = validate_config(config)
        assert any('sources' in issue for issue in issues)
        assert any('settings' in issue for issue in issues)

    def test_missing_source_name(self):
        """Test detection of missing 'name' in source"""
        config = {
            'llm': {},
            'sources': {
                'english': [
                    {
                        'url': 'https://example.com',
                        'icon': '📰',
                        'color': '#FF0000',
                        'category': 'tech',
                    }
                ],
            },
            'settings': {},
        }
        issues = validate_config(config)
        assert any('name' in issue for issue in issues)

    def test_missing_source_url(self):
        """Test detection of missing 'url' in source"""
        config = {
            'llm': {},
            'sources': {
                'english': [
                    {
                        'name': 'TechNews',
                        'icon': '📰',
                        'color': '#FF0000',
                        'category': 'tech',
                    }
                ],
            },
            'settings': {},
        }
        issues = validate_config(config)
        assert any('url' in issue for issue in issues)

    def test_missing_required_source_fields(self):
        """Test detection of missing icon, color, category"""
        config = {
            'llm': {},
            'sources': {
                'english': [
                    {
                        'name': 'TechNews',
                        'url': 'https://example.com',
                    }
                ],
            },
            'settings': {},
        }
        issues = validate_config(config)
        assert any('icon' in issue for issue in issues)
        assert any('color' in issue for issue in issues)
        assert any('category' in issue for issue in issues)

    def test_invalid_source_type(self):
        """Test detection of non-dict source"""
        config = {
            'llm': {},
            'sources': {
                'english': ['not a dict'],
            },
            'settings': {},
        }
        issues = validate_config(config)
        assert any('dict' in issue for issue in issues)

    def test_unknown_source_keys(self):
        """Test detection of unknown keys (possible typos)"""
        config = {
            'llm': {},
            'sources': {
                'english': [
                    {
                        'name': 'TechNews',
                        'url': 'https://example.com',
                        'icon': '📰',
                        'color': '#FF0000',
                        'category': 'tech',
                        'typo_key': 'value',
                        'another_typo': 'value',
                    }
                ],
            },
            'settings': {},
        }
        issues = validate_config(config)
        assert any('unknown' in issue or 'typo' in issue for issue in issues)

    def test_invalid_max_items_type(self):
        """Test detection of non-integer max_items"""
        config = {
            'llm': {},
            'sources': {
                'english': [],
                'chinese': [],
            },
            'settings': {
                'max_items_per_source': '10',  # Should be int
            },
        }
        issues = validate_config(config)
        assert any('max_items' in issue and 'integer' in issue for issue in issues)

    def test_invalid_max_items_negative(self):
        """Test detection of non-positive max_items"""
        config = {
            'llm': {},
            'sources': {
                'english': [],
                'chinese': [],
            },
            'settings': {
                'max_items_per_source': 0,
            },
        }
        issues = validate_config(config)
        assert any('max_items' in issue and 'positive' in issue for issue in issues)

    def test_llm_enabled_missing_base_url(self):
        """Test detection of missing base_url when LLM enabled"""
        config = {
            'llm': {
                'enabled': True,
                'model': 'gpt-4',
            },
            'sources': {
                'english': [],
                'chinese': [],
            },
            'settings': {},
        }
        issues = validate_config(config)
        assert any('base_url' in issue for issue in issues)

    def test_llm_enabled_missing_model(self):
        """Test detection of missing model when LLM enabled"""
        config = {
            'llm': {
                'enabled': True,
                'base_url': 'http://localhost:8000',
            },
            'sources': {
                'english': [],
                'chinese': [],
            },
            'settings': {},
        }
        issues = validate_config(config)
        assert any('model' in issue for issue in issues)

    def test_source_authority_unknown_source(self):
        """Test detection of unknown source in source_authority"""
        config = {
            'llm': {},
            'sources': {
                'english': [
                    {
                        'name': 'TechNews',
                        'url': 'https://example.com',
                        'icon': '📰',
                        'color': '#FF0000',
                        'category': 'tech',
                    }
                ],
                'chinese': [],
            },
            'settings': {},
            'source_authority': {
                'UnknownSource': 0.8,
            },
        }
        issues = validate_config(config)
        assert any('UnknownSource' in issue for issue in issues)

    def test_source_authority_valid_source(self):
        """Test that valid source_authority references pass"""
        config = {
            'llm': {},
            'sources': {
                'english': [
                    {
                        'name': 'TechNews',
                        'url': 'https://example.com',
                        'icon': '📰',
                        'color': '#FF0000',
                        'category': 'tech',
                    }
                ],
                'chinese': [],
            },
            'settings': {},
            'source_authority': {
                'TechNews': 0.9,
            },
        }
        issues = validate_config(config)
        assert not any('TechNews' in issue for issue in issues)


# ═══════════════════════════════════════════════════════════════════════
# exceptions Tests
# ═══════════════════════════════════════════════════════════════════════

class TestExceptions(unittest.TestCase):
    """Test exception hierarchy"""

    def test_fetch_error_is_exception(self):
        """Test that FetchError is a subclass of Exception"""
        assert issubclass(FetchError, Exception)

    def test_source_unavailable_error_is_fetch_error(self):
        """Test that SourceUnavailableError is a subclass of FetchError"""
        assert issubclass(SourceUnavailableError, FetchError)

    def test_parse_error_is_fetch_error(self):
        """Test that ParseError is a subclass of FetchError"""
        assert issubclass(ParseError, FetchError)

    def test_raise_and_catch_fetch_error(self):
        """Test raising and catching FetchError"""
        with unittest.TestCase.assertRaises(self, FetchError):
            raise FetchError("Network timeout")

    def test_raise_and_catch_source_unavailable_error(self):
        """Test raising and catching SourceUnavailableError"""
        with unittest.TestCase.assertRaises(self, SourceUnavailableError):
            raise SourceUnavailableError("Service unavailable")

    def test_source_unavailable_caught_as_fetch_error(self):
        """Test that SourceUnavailableError can be caught as FetchError"""
        try:
            raise SourceUnavailableError("Service down")
        except FetchError:
            pass  # Expected

    def test_parse_error_caught_as_fetch_error(self):
        """Test that ParseError can be caught as FetchError"""
        try:
            raise ParseError("Invalid XML")
        except FetchError:
            pass  # Expected

    def test_exception_message_preserved(self):
        """Test that exception messages are preserved"""
        msg = "Custom error message"
        try:
            raise FetchError(msg)
        except FetchError as e:
            assert str(e) == msg


if __name__ == '__main__':
    unittest.main()
