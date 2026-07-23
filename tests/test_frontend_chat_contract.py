"""Regression tests for the static chat-page DOM contract."""

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHAT_HTML = PROJECT_ROOT / "frontend" / "chat.html"
CHAT_JS = PROJECT_ROOT / "frontend" / "chat.js"


class _ChatMarkupParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.fragment_links = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.ids.add(element_id)

        href = attributes.get("href", "")
        if tag == "a" and href.startswith("#"):
            self.fragment_links.add(href[1:])


class ChatMarkupContractTests(unittest.TestCase):
    def test_message_log_target_used_by_javascript_exists(self):
        javascript = CHAT_JS.read_text(encoding="utf-8")
        selector = re.search(
            r'scroll:\s*\$\(\s*["\']#([^"\']+)["\']\s*\)',
            javascript,
        )
        self.assertIsNotNone(selector, "chat.js must declare its message-log target")

        target_id = selector.group(1)
        parser = _ChatMarkupParser()
        parser.feed(CHAT_HTML.read_text(encoding="utf-8"))

        self.assertIn(
            target_id,
            parser.ids,
            f"chat.html must expose #{target_id} so messages can be appended",
        )
        self.assertIn(
            target_id,
            parser.fragment_links,
            f"the skip link must point to the same #{target_id} message log",
        )


if __name__ == "__main__":
    unittest.main()
