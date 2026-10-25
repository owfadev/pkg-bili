import json
import tempfile
import time
import unittest
from unittest import mock

from bilicli import traffic


class FakeCookies(dict):
    pass


class FakeResponse:
    def __init__(self, body=None, status=200):
        self._body = body
        self.status_code = status

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.cookies = FakeCookies()

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.responses.pop(0)


VIEW_URL = "https://api.bilibili.com/x/web-interface/view?bvid=BV1xx"
OK_VIEW = {"code": 0, "data": {"aid": 1, "title": "t"}}


class TrafficTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        traffic._set_state_dir(self._dir.name)
        self._gap = traffic.MIN_GAP_S
        traffic.MIN_GAP_S = 0.0  # tests must not sleep

    def tearDown(self):
        traffic.MIN_GAP_S = self._gap
        self._dir.cleanup()

    def test_cache_hit_skips_second_network_call(self):
        s = FakeSession([FakeResponse(OK_VIEW)])
        first = traffic.guarded_get(s, VIEW_URL)
        second = traffic.guarded_get(s, VIEW_URL)
        self.assertEqual(len(s.calls), 1)
        self.assertEqual(first["data"]["aid"], 1)
        self.assertTrue(second.get("_cached"))



    def test_cache_key_strips_signature_params(self):
        s = FakeSession([FakeResponse(OK_VIEW)])
        traffic.guarded_get(s, VIEW_URL)
        signed = VIEW_URL + "&wts=1700000000&w_rid=abc123"
        out = traffic.guarded_get(s, signed)
        self.assertEqual(len(s.calls), 1)
        self.assertTrue(out.get("_cached"))

    def test_ttl_expiry_refetches(self):
        s = FakeSession([FakeResponse(OK_VIEW), FakeResponse(OK_VIEW)])
        traffic.guarded_get(s, VIEW_URL)
        with mock.patch("bilicli.traffic.time.time", return_value=time.time() + 25 * 3600):
            out = traffic.guarded_get(s, VIEW_URL)
        self.assertEqual(len(s.calls), 2)
        self.assertNotIn("_stale", out)

    def test_http_412_arms_breaker_then_blocks(self):
        s = FakeSession([FakeResponse(status=412)])
        with self.assertRaises(traffic.GuardBlocked):
            traffic.guarded_get(s, VIEW_URL)
        state = traffic.api_state()
        self.assertTrue(state["banned"])
        self.assertEqual(state["ban_minutes_left"], 5)
        with self.assertRaises(traffic.GuardBlocked) as cm:  # no cache to carry it
            traffic.guarded_get(s, VIEW_URL)
        self.assertIn("pause active", str(cm.exception))

    def test_stale_metadata_serves_during_ban(self):
        s = FakeSession([FakeResponse(OK_VIEW)])
        traffic.guarded_get(s, VIEW_URL)   # fresh cache entry (one network call)
        # age it past TTL but inside the 7-day stale window, then arm the breaker
        store = json.loads(traffic.CACHE_FILE.read_text())
        store[next(iter(store))]["at"] = time.time() - 25 * 3600
        traffic._write_json(traffic.CACHE_FILE, store)
        traffic._note_ban("HTTP 412", "/x/web-interface/view")
        out = traffic.guarded_get(s, VIEW_URL)   # no response queued: must NOT hit the network
        self.assertEqual(len(s.calls), 1)
        self.assertTrue(out.get("_stale"))

    def test_interface_ban_code_arms_breaker(self):
        s = FakeSession([FakeResponse({"code": -412, "message": "请求被拦截"})])
        with self.assertRaises(traffic.GuardBlocked):
            traffic.guarded_get(s, VIEW_URL)
        self.assertTrue(traffic.api_state()["banned"])

    def test_streak_escalation_5_10_20_30(self):
        mins = [traffic._note_ban("HTTP 412", "/p") for _ in range(5)]
        self.assertEqual(mins, [5, 10, 20, 30, 30])

    def test_success_clears_streak_on_same_endpoint(self):
        traffic._note_ban("HTTP 412", "/x/web-interface/view")
        traffic._write_json(traffic.BAN_FILE, {**traffic.ban_state(), "until": 0})
        s = FakeSession([FakeResponse(OK_VIEW)])
        traffic.guarded_get(s, VIEW_URL)
        self.assertEqual(traffic.ban_state().get("streak"), 0)

    def test_non_bilibili_hosts_bypass_guard(self):
        s = FakeSession([FakeResponse({"code": 0})])
        out = traffic.guarded_get(s, "https://example.com/api", params=None)
        self.assertEqual(len(s.calls), 1)
        self.assertFalse(out.get("_cached"))

    def test_lru_trim_keeps_400(self):
        store = {f"k{i}": {"at": i, "data": OK_VIEW} for i in range(450)}
        traffic._write_json(traffic.CACHE_FILE, store)
        s = FakeSession([FakeResponse(OK_VIEW)])
        traffic.guarded_get(s, VIEW_URL + "&x=1")
        saved = json.loads(traffic.CACHE_FILE.read_text())
        self.assertLessEqual(len(saved), 400)
        self.assertNotIn("k0", saved)
        self.assertIn("k449", saved)


if __name__ == "__main__":
    unittest.main()
