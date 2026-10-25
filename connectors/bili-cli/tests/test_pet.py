import json
import tempfile
import time
import unittest
from pathlib import Path

from bilicli import pet


def iso_ago(days: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - days * 86_400))


class PetStateTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        root = Path(self._dir.name)
        self._paths = {
            "QUOTA_FILE": root / "quota.json",
            "REPLIED_FILE": root / "replied.json",
            "DM_LOCK_FILE": root / "dm-lock.json",
            "FEEDBACK_FILE": root / "feedback.json",
        }
        self._saved = {k: getattr(pet, k) for k in self._paths}
        for k, v in self._paths.items():
            setattr(pet, k, v)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(pet, k, v)
        self._dir.cleanup()


class ValidateShareTests(PetStateTestCase):
    CFG = pet._merge(pet.DEFAULTS, {})

    def test_reason_too_short(self):
        with self.assertRaises(pet.PetError):
            pet.validate_share(self.CFG, "短", 15)

    def test_score_below_threshold(self):
        with self.assertRaises(pet.PetError):
            pet.validate_share(self.CFG, "这条视频值得主人看", 12)

    def test_score_out_of_range(self):
        with self.assertRaises(pet.PetError):
            pet.validate_share(self.CFG, "这条视频值得主人看", 21)

    def test_rank_must_be_one(self):
        with self.assertRaises(pet.PetError):
            pet.validate_share(self.CFG, "这条视频值得主人看", 15, rank=2)

    def test_disabled(self):
        cfg = pet._merge(pet.DEFAULTS, {"share": {"enabled": False}})
        with self.assertRaises(pet.PetError):
            pet.validate_share(cfg, "这条视频值得主人看", 15)

    def test_valid(self):
        out = pet.validate_share(self.CFG, "  深度好文，值得主人一看 ", 15)
        self.assertEqual(out["reason"], "深度好文，值得主人一看")


class ShareGateTests(PetStateTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = pet._merge(pet.DEFAULTS, {})

    def test_fail_closed_on_corrupt_ledger(self):
        pet.QUOTA_FILE.write_text("{not json")
        q, corrupt = pet.load_quota()
        self.assertTrue(corrupt)
        with self.assertRaises(pet.PetError):
            pet.check_share(self.cfg, q, corrupt, "BV1xx")

    def test_daily_cap(self):
        q = {"log": [{"at": iso_ago(0.01), "share": True}] * 3}
        with self.assertRaises(pet.PetError):
            pet.check_share(self.cfg, q, False, "BV1new")
        ok = pet.check_share(self.cfg, {"log": []}, False, "BV1xx")
        self.assertEqual(ok["used"], 0)

    def test_seven_day_per_video_dedup(self):
        q = {"log": [{"at": iso_ago(2), "share": True, "target": "BV1xx"}]}
        with self.assertRaises(pet.PetError):
            pet.check_share(self.cfg, q, False, "BV1xx")
        ok = pet.check_share(self.cfg, q, False, "BV1other")
        self.assertEqual(ok["used"], 0)

    def test_record_appends(self):
        q = {"log": []}
        pet.record(q, {"type": "comment", "target": "BV1xx"})
        saved = json.loads(pet.QUOTA_FILE.read_text())
        self.assertEqual(len(saved["log"]), 1)


class SilenceTests(PetStateTestCase):
    def cfg(self, base_cap=3):
        return pet._merge(pet.DEFAULTS, {"quota": {"shareDaily": base_cap}})

    def test_fresh_full_cap(self):
        pet.FEEDBACK_FILE.write_text(json.dumps({"last": iso_ago(1)}))
        s = pet.silence(self.cfg(), {"log": []})
        self.assertEqual((s["days"], s["cap"], s["band"]), (1, 3, "normal"))

    def test_decay_band(self):
        pet.FEEDBACK_FILE.write_text(json.dumps({"last": iso_ago(4)}))
        s = pet.silence(self.cfg(), {"log": []})
        # base 3, after 3, stop 7: day4 -> 3*(7-4)/(7-3) = 2
        self.assertEqual((s["cap"], s["band"]), (2, "quiet"))

    def test_seven_days_zero(self):
        pet.FEEDBACK_FILE.write_text(json.dumps({"last": iso_ago(8)}))
        s = pet.silence(self.cfg(), {"log": []})
        self.assertEqual((s["cap"], s["band"]), (0, "faint"))

    def test_no_feedback_counts_from_first_share(self):
        pet.FEEDBACK_FILE.write_text(json.dumps({"last": None, "events": []}))
        q = {"log": [{"at": iso_ago(2), "share": True}]}
        self.assertEqual(pet.silence(self.cfg(), q)["days"], 2)


class RepliedTests(PetStateTestCase):
    def test_owner_exempt(self):
        self.assertEqual(pet.replied_recent("42", "42"), [])

    def test_thirty_day_window(self):
        pet.REPLIED_FILE.write_text(json.dumps(
            {"byUid": {"7": [{"at": iso_ago(29), "target": "BV1a"}],
                       "8": [{"at": iso_ago(31), "target": "BV1b"}]}}))
        self.assertEqual(len(pet.replied_recent("7", "42")), 1)
        self.assertEqual(pet.replied_recent("8", "42"), [])


class DmLockTests(PetStateTestCase):
    def test_lock_to_other_blocks(self):
        pet.DM_LOCK_FILE.write_text(json.dumps(
            {"date": time.strftime("%Y-%m-%d"), "mid": "999"}))
        with self.assertRaises(pet.PetError):
            pet.dm_check_lock({}, "42")

    def test_lock_to_owner_ok_and_yesterday_expires(self):
        pet.DM_LOCK_FILE.write_text(json.dumps(
            {"date": time.strftime("%Y-%m-%d"), "mid": "42"}))
        pet.dm_check_lock({}, "42")                      # no raise
        pet.DM_LOCK_FILE.write_text(json.dumps(
            {"date": time.strftime("%Y-%m-%d", time.localtime(time.time() - 86_400)), "mid": "999"}))
        pet.dm_check_lock({}, "42")                      # yesterday's lock expired


if __name__ == "__main__":
    unittest.main()
