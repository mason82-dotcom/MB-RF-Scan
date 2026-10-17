"""Lightweight browser contracts: read-only controls and valid local HTML references."""
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import unittest

WEB = Path(__file__).resolve().parents[1] / "keyrfmonitor" / "web"


class IdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key == "id":
                self.ids.add(value)


class BrowserContractTests(unittest.TestCase):
    def test_element_ids_are_present(self):
        parser = IdParser()
        parser.feed((WEB / "index.html").read_text(encoding="utf-8"))
        js = (WEB / "app.js").read_text(encoding="utf-8")
        references = set(re.findall(r"\$\('([a-z][a-z0-9-]+)'\)", js))
        self.assertEqual(references - parser.ids, set())

    def test_only_cli_controls_adsb_and_sessions_endpoint_is_read_only(self):
        js = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertIn("'/api/sessions'", js)
        self.assertIn("comparisonValue", js)
        self.assertIn("'/api/session-detail?id='", js)
        self.assertIn("renderSessionDetail", js)
        self.assertIn("detail-timeline", js)
        self.assertIn("burst_rate", js)
        self.assertNotIn("'/api/scan/start'", js)
        self.assertNotIn("'/api/adsb/stop'", js)

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable on this host")
    def test_javascript_syntax(self):
        result = subprocess.run(
            ["node", "--check", str(WEB / "app.js")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
