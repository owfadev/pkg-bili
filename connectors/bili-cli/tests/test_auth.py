"""Auth + QR-login helpers: parsing, storage rotation, PNG rendering (no network)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from bilicli import auth, qrlogin


class ParseTests(unittest.TestCase):
    def test_browser_header(self):
        jar = auth.parse_cookie_string(
            "SESSDATA=abc%2C1700000000%2Cxyz; bili_jct=deadbeef; buvid3=AA-BB")
        self.assertEqual(jar["SESSDATA"], "abc%2C1700000000%2Cxyz")
        self.assertEqual(jar["bili_jct"], "deadbeef")
        self.assertEqual(jar["buvid3"], "AA-BB")

    def test_tolerates_newlines_spaces_and_junk(self):
        jar = auth.parse_cookie_string(" a=1 ;\n b=2 \n\n no_equals ; c=")
        self.assertEqual(jar, {"a": "1", "b": "2"})


class LoginUrlTests(unittest.TestCase):
    URL = ("https://www.bilibili.com/h5/login?DedeUserID=42&SESSDATA=abc%2Cdef"
           "&bili_jct=zz&gourl=https%3A%2F%2Fwww.bilibili.com&upgrade=1")

    def test_keeps_percent_encoding(self):
        jar = qrlogin.cookies_from_url(self.URL)
        # SESSDATA must stay encoded: a decoded comma would break the cookie header
        self.assertEqual(jar["SESSDATA"], "abc%2Cdef")
        self.assertEqual(jar["bili_jct"], "zz")
        self.assertEqual(jar["DedeUserID"], "42")

    def test_ignores_unrelated_keys_and_empty_url(self):
        self.assertNotIn("gourl", qrlogin.cookies_from_url(self.URL))
        self.assertEqual(qrlogin.cookies_from_url(""), {})
        self.assertEqual(qrlogin.cookies_from_url("https://x/y"), {})

    def test_status_labels(self):
        self.assertEqual(qrlogin.status_label(86101), "等待扫码")
        self.assertEqual(qrlogin.status_label(86038), "二维码已过期")
        self.assertIn("未知", qrlogin.status_label(12345))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cookies.json"
        self.patch = mock.patch.object(auth, "COOKIE_FILE", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_save_requires_login_keys(self):
        with self.assertRaises(SystemExit):
            auth.save_cookies({"buvid3": "only"})
        self.assertFalse(self.path.exists())

    def test_roundtrip_and_mode(self):
        auth.save_cookies({"SESSDATA": "s", "bili_jct": "j"})
        self.assertEqual(auth.load_cookies(), {"SESSDATA": "s", "bili_jct": "j"})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_clear(self):
        auth.save_cookies({"SESSDATA": "s", "bili_jct": "j"})
        self.assertTrue(auth.clear())
        self.assertFalse(auth.clear())  # idempotent

    def test_persist_merges_rotation_and_keeps_extra_keys(self):
        auth.save_cookies({"SESSDATA": "old", "bili_jct": "j", "ac_time_value": "keep"})
        s = requests.Session()
        s.cookies.set("SESSDATA", "rotated", domain=".bilibili.com")
        s.cookies.set("DedeUserID", "42", domain=".bilibili.com")
        self.assertTrue(auth.persist_session(s))
        jar = auth.load_cookies()
        self.assertEqual(jar["SESSDATA"], "rotated")
        self.assertEqual(jar["DedeUserID"], "42")
        self.assertEqual(jar["ac_time_value"], "keep")
        self.assertFalse(auth.persist_session(s))  # no further change -> no rewrite

    def test_persist_ignores_sessions_without_bilibili_cookies(self):
        auth.save_cookies({"SESSDATA": "keep", "bili_jct": "j"})
        self.assertFalse(auth.persist_session(requests.Session()))
        self.assertEqual(auth.load_cookies()["SESSDATA"], "keep")


class PngTests(unittest.TestCase):
    def test_png_header_and_size(self):
        matrix = qrlogin.matrix_for("https://example.com/login?k=v")
        self.assertGreater(len(matrix), 20)
        with tempfile.TemporaryDirectory() as d:
            p = qrlogin.write_png(matrix, Path(d) / "qr.png", scale=4, border=2)
            data = p.read_bytes()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(b"IHDR", data[:32])
        self.assertGreater(len(data), 500)


if __name__ == "__main__":
    unittest.main()
