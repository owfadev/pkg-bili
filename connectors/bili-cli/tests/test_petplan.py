import random
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from bilicli import petplan

NIGHT = {"windows": [{"start": "20:00", "end": "24:00", "wakes": 1}],
         "skipChance": 0.3, "minGapMin": 10, "maxVideos": 5}


def ms(dt: datetime) -> float:
    return dt.timestamp() * 1000


class PlanTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._plan = petplan.PLAN_FILE
        petplan.PLAN_FILE = Path(self._dir.name) / "plan.json"

    def tearDown(self):
        petplan.PLAN_FILE = self._plan
        self._dir.cleanup()

    def today(self, h: float, add_days: int = 0) -> float:
        d = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=add_days)
        return ms(d + timedelta(hours=h))


class WindowsTests(PlanTestCase):
    def test_wrap_midnight(self):
        w = petplan.windows_of({"windows": [{"start": "22:00", "end": "01:00", "wakes": 2}]})[0]
        self.assertTrue(w["wrap"])
        self.assertEqual(w["end"], 25 * 60)

    def test_wakes_cap(self):
        w = petplan.windows_of({"windows": [{"start": "0:00", "end": "24:00", "wakes": 99}]})[0]
        self.assertEqual(w["wakes"], petplan.WAKES_DAY_MAX)


class MakePlanTests(PlanTestCase):
    def test_rolls_within_window(self):
        now = self.today(19)
        p = petplan.make_plan(NIGHT, {}, now, rng=random.Random(42))
        self.assertIn(p["action"], ("wake", "skip"))
        self.assertGreaterEqual(p["atMs"], self.today(20))
        self.assertLess(p["atMs"], self.today(24))
        self.assertEqual(p["maxVideos"], 5)

    def test_pending_plan_never_rerolled(self):
        now = self.today(19)
        first = petplan.make_plan(NIGHT, {}, now, rng=random.Random(1))
        again = petplan.make_plan(NIGHT, first, now + 600_000, rng=random.Random(999))
        self.assertEqual(first["atMs"], again["atMs"])

    def test_after_window_done(self):
        p = petplan.make_plan(NIGHT, {}, self.today(23.9))
        # window closes in 6 min — still fits one wake
        self.assertIn(p["action"], ("wake", "skip"))
        p2 = petplan.make_plan(NIGHT, {}, self.today(23.995), rng=random.Random(7))
        # 23:59:42 — inside the last-30s guard zone -> no room for a wake
        self.assertEqual(p2["action"], "done")

    def test_min_gap_hard_floor(self):
        # woke 5 min ago inside the window; gap 10 -> next earliest = +10min; window has room
        state = {"wokeAt": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 300))}
        p = petplan.make_plan(NIGHT, state, self.today(21), rng=random.Random(3))
        self.assertGreaterEqual(p["atMs"], self.today(21) + 5 * 60_000)


class CompletePlanTests(PlanTestCase):
    def test_skip_consumes_slot_like_wake(self):
        now = self.today(19)
        p = petplan.make_plan(NIGHT, {}, now, rng=random.Random(42))
        done = petplan.complete_plan(p, now)
        self.assertTrue(done["fired"])
        key = done["windowKey"]
        self.assertEqual(done["usedCount"][key], 1)
        self.assertEqual(done["wakes"], 1)
        self.assertEqual(done["woke"], p["action"] == "wake")

    def test_used_up_today(self):
        now = self.today(19)
        p = petplan.make_plan(NIGHT, {}, now, rng=random.Random(42))
        done = petplan.complete_plan(p, now)
        nxt = petplan.make_plan(NIGHT, done, now, rng=random.Random(42))
        self.assertEqual(nxt["action"], "done")   # single-wake window consumed


if __name__ == "__main__":
    unittest.main()
