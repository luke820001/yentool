"""
The phone's asset versions must not drift apart.

Found 2026-09-21 by an audit: index.html was still requesting
`app.js?v=16` while sw.js had reached VERSION "v24". The service worker
strips the query string from its own cache key, so ITS cache was fine -- but
the browser's ordinary HTTP cache is keyed on the full URL, so a phone could
keep serving a two-week-old app.js and every UI fix shipped since would have
been invisible on the one device that matters.

Nothing in the app can detect that at runtime. A test can.

    python -m unittest tests.test_mobile_assets -v
"""
import re
import unittest
from pathlib import Path

MOBILE = Path(__file__).resolve().parent.parent / "mobile"


def sw_version():
    text = (MOBILE / "sw.js").read_text(encoding="utf-8")
    m = re.search(r'const VERSION = "(v\d+)"', text)
    assert m, "sw.js has no VERSION constant"
    return m.group(1)


def html_asset_versions():
    text = (MOBILE / "index.html").read_text(encoding="utf-8")
    return re.findall(r'(?:app\.js|styles\.css)\?v=(\d+)', text)


class AssetVersions(unittest.TestCase):
    def test_index_requests_the_current_shell_version(self):
        want = sw_version().lstrip("v")
        got = html_asset_versions()
        self.assertTrue(got, "index.html does not version its assets at all")
        for v in got:
            self.assertEqual(
                v, want,
                "index.html asks for ?v=%s while sw.js is at v%s -- a phone can "
                "serve a stale app.js from the browser cache" % (v, want))

    def test_both_assets_are_versioned(self):
        text = (MOBILE / "index.html").read_text(encoding="utf-8")
        for asset in ("app.js", "styles.css"):
            self.assertRegex(
                text, re.escape(asset) + r"\?v=\d+",
                "%s is loaded without a version query" % asset)

    def test_the_service_worker_still_strips_the_query(self):
        """The SW keys its cache on the URL WITHOUT the query (2026-09-09,
        F20), so bumping ?v= must not orphan its entries."""
        text = (MOBILE / "sw.js").read_text(encoding="utf-8")
        self.assertIn("search", text)


if __name__ == "__main__":
    unittest.main()
