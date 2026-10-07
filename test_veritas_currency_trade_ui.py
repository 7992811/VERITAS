"""Static integration contract for the isolated broker console."""
from html.parser import HTMLParser
from pathlib import Path
import unittest

import veritas_currency_trade_ui as UI


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.tags = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if "id" in attrs:
            self.ids.append(attrs["id"])


class CurrencyTradeUiTests(unittest.TestCase):
    def test_inert_private_page_and_mobile_controls(self):
        html = UI.render_trading_ui()
        page = PageParser()
        page.feed(html)
        self.assertEqual(len(page.ids), len(set(page.ids)))
        self.assertTrue({"dashboard", "login", "login-form", "setup-code", "feedback"} <= set(page.ids))
        self.assertIn(("html", {"lang": "ru"}), page.tags)
        self.assertTrue(any(t == "meta" and a.get("name") == "viewport" for t, a in page.tags))
        self.assertTrue(any(t == "meta" and a.get("name") == "referrer" and a.get("content") == "no-referrer" for t, a in page.tags))
        self.assertFalse(any(t == "script" and "src" in a for t, a in page.tags))
        self.assertFalse(any(k.startswith("on") for _, a in page.tags for k in a))
        self.assertIn("min-height:44px", html)
        self.assertIn("@media(max-width:700px)", html)
        self.assertIn("font-size:16px", html)  # iPhone inputs do not zoom on focus.

    def test_static_html_has_no_server_secret_injection_or_local_persistence(self):
        source = Path(UI.__file__).read_text()
        for token in ("os.environ", "localStorage", "sessionStorage", "document.cookie", "TBANK_API_TOKEN", "TRADE_SERVICE_KEY", "TRADE_APPROVAL_KEY"):
            self.assertNotIn(token, source)
        self.assertEqual(UI.render_trading_ui(), UI.render_trading_ui())

    def test_navigation_has_single_public_link_to_login_surface(self):
        source = Path(__file__).with_name("veritas_v90_ui.py").read_text()
        self.assertEqual(source.count('href="/integrations/trading"'), 1)
        self.assertEqual(UI.UI_PATH, "/integrations/trading")
        self.assertNotIn("data-action=\"approve\"", UI.render_trading_ui())
        self.assertNotIn("data-action=\"execute\"", UI.render_trading_ui())


if __name__ == "__main__":
    unittest.main()
