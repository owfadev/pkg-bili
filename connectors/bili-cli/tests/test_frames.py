import unittest

from bilicli.frames import parse_scene_log, pick_times


class SceneLogTests(unittest.TestCase):
    def test_filters_dedupes_sorts(self):
        log = ("frame:0 pts_time:0.100\n"
               "frame:1 pts_time:12.500\n"
               "frame:2 pts_time:5.000\n"
               "frame:3 pts_time:12.500\n")
        self.assertEqual(parse_scene_log(log), [5.0, 12.5])

    def test_empty(self):
        self.assertEqual(parse_scene_log("no matches here"), [])


class PickTimesTests(unittest.TestCase):
    def test_uniform_skeleton_snaps_within_half_slot(self):
        # duration 90, n 3 -> uniform [15, 45, 75], half-slot 15
        times, snapped = pick_times(90, 3, [10.9, 50.0])
        self.assertEqual(times, [10.9, 50.0, 75])
        self.assertEqual(snapped, 2)

    def test_out_of_half_slot_keeps_uniform(self):
        times, snapped = pick_times(90, 3, [65.0])   # |65-45|=20 > 15, |65-75|=10 <= 15
        self.assertEqual(times, [15, 45, 65])
        self.assertEqual(snapped, 1)

    def test_no_scenes_pure_uniform(self):
        times, snapped = pick_times(60, 4, [])
        self.assertEqual(times, [7.5, 22.5, 37.5, 52.5])
        self.assertEqual(snapped, 0)


if __name__ == "__main__":
    unittest.main()
