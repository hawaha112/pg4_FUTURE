"""rss_parser 单元测试"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from rss_parser import parse_rss, clean_html, _parse_date


class TestCleanHtml(unittest.TestCase):
    def test_strips_tags(self):
        self.assertEqual(clean_html("<p>Hello</p>"), "Hello")

    def test_strips_nested_tags(self):
        result = clean_html("<div><p>Hello <b>World</b></p></div>")
        self.assertIn("Hello", result)
        self.assertIn("World", result)

    def test_decodes_entities(self):
        self.assertEqual(clean_html("A &amp; B"), "A & B")
        self.assertEqual(clean_html("A &lt; B"), "A < B")

    def test_empty(self):
        self.assertEqual(clean_html(""), "")
        self.assertEqual(clean_html(None), "")

    def test_collapses_whitespace(self):
        result = clean_html("Hello    World")
        self.assertEqual(result, "Hello World")


class TestParseDate(unittest.TestCase):
    def test_rfc2822(self):
        dt = _parse_date("Mon, 07 Apr 2025 12:00:00 +0000")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2025)
        self.assertEqual(dt.month, 4)

    def test_iso8601(self):
        dt = _parse_date("2025-04-07T12:00:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2025)

    def test_date_only(self):
        dt = _parse_date("2025-04-07")
        self.assertIsNotNone(dt)

    def test_empty(self):
        self.assertIsNone(_parse_date(""))
        self.assertIsNone(_parse_date(None))

    def test_garbage(self):
        self.assertIsNone(_parse_date("not a date"))


class TestParseRss(unittest.TestCase):
    def test_rss2(self):
        xml = '''<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
        <channel>
            <title>Test Feed</title>
            <item>
                <title>Article 1</title>
                <link>https://example.com/1</link>
                <description>Summary of article 1</description>
                <pubDate>Mon, 07 Apr 2025 12:00:00 +0000</pubDate>
            </item>
            <item>
                <title>Article 2</title>
                <link>https://example.com/2</link>
            </item>
        </channel>
        </rss>'''
        items = parse_rss(xml)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['title'], 'Article 1')
        self.assertEqual(items[0]['link'], 'https://example.com/1')
        self.assertIn('Summary', items[0]['summary'])

    def test_atom(self):
        xml = '''<?xml version="1.0" encoding="utf-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <title>Test Atom Feed</title>
            <entry>
                <title>Atom Entry</title>
                <link href="https://example.com/atom1" rel="alternate"/>
                <summary>Atom summary</summary>
                <published>2025-04-07T12:00:00Z</published>
            </entry>
        </feed>'''
        items = parse_rss(xml)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['title'], 'Atom Entry')
        self.assertEqual(items[0]['link'], 'https://example.com/atom1')

    def test_empty_feed(self):
        xml = '''<?xml version="1.0"?>
        <rss version="2.0"><channel><title>Empty</title></channel></rss>'''
        items = parse_rss(xml)
        self.assertEqual(len(items), 0)

    def test_malformed_xml(self):
        items = parse_rss("this is not xml at all")
        self.assertEqual(len(items), 0)

    def test_cdata_handling(self):
        xml = '''<?xml version="1.0"?>
        <rss version="2.0"><channel>
            <item>
                <title><![CDATA[Title with CDATA]]></title>
                <link>https://example.com/cdata</link>
            </item>
        </channel></rss>'''
        items = parse_rss(xml)
        self.assertGreaterEqual(len(items), 0)  # Should not crash


if __name__ == '__main__':
    unittest.main()
