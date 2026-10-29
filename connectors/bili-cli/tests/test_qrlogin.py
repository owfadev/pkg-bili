"""QR login flow, driven by a scripted fake session — no network, no real clock."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from bilicli import qrlogin


class Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class Jar:
    def __init__(self, data):
        self._data = dict(data)

    def get_dict(self):
        return dict(self._data)


class FakeSession:
    """Routes generate/poll calls to scripted payloads; records the call sequence."""

    def __init__(self, generates, polls, cookies=None):
        self.headers = {}
        self.cookies = Jar(cookies or {"SESSDATA": "sess", "bili_jct": "csrf", "DedeUserID": "42"})
        self._generates = list(generates)
        self._polls = list(polls)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(("generate" if url == qrlogin.QR_GENERATE else "poll", params))
        if url == qrlogin.QR_GENERATE:
            if not self._generates:
                raise AssertionError("unexpected extra generate call")
            return Resp(self._generates.pop(0))
        if not self._polls:
            raise AssertionError("unexpected extra poll call")
        nxt = self._polls.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return Resp(nxt)


def gen_ok(key="k", url="https://www.bilibili.com/h5/login?qrcode_key=k"):
    return {"code": 0, "data": {"qrcode_key": key, "url": url}}


def poll(code, url=""):
    return {"code": 0, "data": {"code": code, "url": url}}


class LoginFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.png = Path(self.tmp.name) / "qr.png"
        patcher = mock.patch.object(qrlogin, "_sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def run_login(self, session, **kw):
        stream = io.StringIO()
        kw.setdefault("timeout_s", 30)
        jar = qrlogin.login(png_path=self.png, stream=stream, session=session, **kw)
        return jar, stream.getvalue()

    def test_success_returns_jar_and_removes_qr(self):
        s = FakeSession([gen_ok()], [poll(0)])
        jar, out = self.run_login(s)
        self.assertEqual(jar["SESSDATA"], "sess")
        self.assertIn("登录成功", out)
        self.assertFalse(self.png.exists(), "consumed QR must not stay on disk")

    def test_keep_qr_leaves_the_file(self):
        s = FakeSession([gen_ok()], [poll(0)])
        self.run_login(s, keep_qr=True)
        self.assertTrue(self.png.exists())

    def test_png_is_written_owner_only(self):
        s = FakeSession([gen_ok()], [poll(0)])
        self.run_login(s, keep_qr=True)
        self.assertTrue(self.png.read_bytes().startswith(b"\x89PNG"))
        self.assertEqual(self.png.stat().st_mode & 0o777, 0o600)

    def test_status_transitions_are_reported_once(self):
        s = FakeSession([gen_ok()], [poll(86101), poll(86101), poll(86090), poll(0)])
        _, out = self.run_login(s)
        self.assertEqual(out.count("等待扫码"), 1)
        self.assertEqual(out.count("已扫码"), 1)

    def test_json_events_are_ndjson(self):
        s = FakeSession([gen_ok()], [poll(86101), poll(0)])
        _, out = self.run_login(s, json_events=True)
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        names = [e["event"] for e in events]
        self.assertEqual(names[0], "qr")
        self.assertIn("state", names)
        self.assertEqual(names[-1], "success")
        self.assertEqual(events[0]["png"], str(self.png))
        self.assertEqual(events[-1]["mid"], "42")

    def test_expired_qr_is_regenerated_once(self):
        s = FakeSession([gen_ok(), gen_ok(key="k2")], [poll(86038), poll(0)])
        jar, out = self.run_login(s)
        self.assertEqual(jar["SESSDATA"], "sess")
        self.assertIn("自动换一张", out)
        self.assertEqual([c[0] for c in s.calls].count("generate"), 2)

    def test_second_expiry_gives_up_with_exit_code_2(self):
        s = FakeSession([gen_ok(), gen_ok()], [poll(86038), poll(86038)])
        with self.assertRaises(SystemExit) as ctx:
            self.run_login(s)
        self.assertEqual(ctx.exception.code, 2)

    def test_timeout_exits_2_without_polling(self):
        s = FakeSession([gen_ok()], [])
        with self.assertRaises(SystemExit) as ctx:
            self.run_login(s, timeout_s=0)
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual([c[0] for c in s.calls], ["generate"])

    def test_network_errors_are_retried(self):
        s = FakeSession([gen_ok()], [requests.ConnectionError("boom"), poll(0)])
        jar, _ = self.run_login(s)
        self.assertEqual(jar["SESSDATA"], "sess")
        self.assertEqual([c[0] for c in s.calls].count("poll"), 2)

    def test_generate_failure_carries_the_api_message(self):
        s = FakeSession([{"code": -412, "message": "risk control"}], [])
        with self.assertRaises(SystemExit) as ctx:
            self.run_login(s)
        self.assertEqual(ctx.exception.code, 1)

    def test_url_cookies_fill_gaps_left_by_set_cookie(self):
        s = FakeSession([gen_ok()], [poll(0, url="https://x/y?SESSDATA=fresh%2C1&bili_jct=new&DedeUserID=7")])
        jar, _ = self.run_login(s)
        self.assertEqual(jar["SESSDATA"], "sess")       # Set-Cookie wins
        self.assertEqual(jar["DedeUserID"], "42")       # Set-Cookie wins
        self.assertEqual(jar["bili_jct"], "csrf")

    def test_journal_missing_rendering_degrades_to_url(self):
        s = FakeSession([gen_ok()], [poll(0)])
        with mock.patch.object(qrlogin, "matrix_for", side_effect=ImportError):
            _, out = self.run_login(s)
        self.assertIn("uv pip install", out)


class AsciiTests(unittest.TestCase):
    def test_render_ascii_has_blocks(self):
        art = qrlogin.render_ascii("https://www.bilibili.com/h5/login?qrcode_key=k")
        self.assertTrue(art)
        self.assertIn("█", art)

    def test_render_ascii_is_none_without_lib(self):
        with mock.patch.dict("sys.modules", {"qrcode": None}):
            self.assertIsNone(qrlogin.render_ascii("x"))


if __name__ == "__main__":
    unittest.main()
