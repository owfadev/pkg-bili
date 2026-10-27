import gzip
import zlib
import unittest

from bilicli.debrief import (align_subtitle, bucketize, find_spam, fmt_mmss,
                             is_spam, normalize_text, parse_danmaku,
                             peak_buckets, top_texts)


def _xml(items):
    rows = "".join(f'<d p="{t},1,25,16777215,0,0,abc,0">{text}</d>' for t, text in items)
    return f"<i>{rows}</i>".encode()


class ParseTests(unittest.TestCase):
    def test_plain_with_entities(self):
        out = parse_danmaku(_xml([(1.5, "前方高能"), (62.0, "名场面&amp;神回")]))
        self.assertEqual(out, [{"t": 1.5, "text": "前方高能"},
                               {"t": 62.0, "text": "名场面&神回"}])

    def test_raw_deflate_without_header(self):
        # the endpoint historically serves a deflate stream with no content-encoding
        co = zlib.compressobj(wbits=-15)
        raw = co.compress(_xml([(3.0, "hhh")])) + co.flush()
        self.assertEqual(parse_danmaku(raw), [{"t": 3.0, "text": "hhh"}])

    def test_zlib_and_gzip_streams(self):
        self.assertEqual(parse_danmaku(zlib.compress(_xml([(2.0, "a")]))),
                         [{"t": 2.0, "text": "a"}])
        self.assertEqual(parse_danmaku(gzip.compress(_xml([(4.0, "b")]))),
                         [{"t": 4.0, "text": "b"}])

    def test_garbage(self):
        self.assertEqual(parse_danmaku(b"\x00\x01not xml at all"), [])


class SpamTests(unittest.TestCase):
    def test_normalize_strips_punctuation_symbols(self):
        self.assertEqual(normalize_text("？？？！！！"), "")
        self.assertEqual(normalize_text("前方高能！！！"), "前方高能")

    def test_spam_wave_detected_by_ratio(self):
        dan = ([{"t": 1, "text": "主力舰"}] * 25 +
               [{"t": 2, "text": f"普通弹幕{i}"} for i in range(50)])
        spam = find_spam(dan)
        self.assertIn("主力舰", spam)
        self.assertTrue(is_spam("主力舰！！", spam))

    def test_pure_symbol_wave_counts_as_spam(self):
        dan = ([{"t": 1, "text": "?"}] * 30 +
               [{"t": 2, "text": f"有内容{i}"} for i in range(50)])
        spam = find_spam(dan)           # '' key: 30/80 > 0.3, count >= 20
        self.assertTrue(is_spam("???!!!", spam))   # normalizes to ''

    def test_below_threshold_not_spam(self):
        dan = ([{"t": 1, "text": "少数"}] * 10 +
               [{"t": 2, "text": f"不同{i}"} for i in range(50)])
        self.assertEqual(find_spam(dan), set())


class BucketTests(unittest.TestCase):
    def test_bucketize_splits_raw_and_real(self):
        dan = ([{"t": 5, "text": f"正常{i}"} for i in range(10)] +
               [{"t": 36, "text": "刷"}] * 40 +
               [{"t": 90, "text": f"别的{i}"} for i in range(5)])
        spam = find_spam(dan)           # 刷: 40/55 > 0.3
        buckets = bucketize(dan, 30, spam)
        self.assertEqual(set(buckets), {0, 30, 90})
        self.assertEqual(len(buckets[0]["real"]), 10)
        self.assertEqual(buckets[30]["raw"], ["刷"] * 40)
        self.assertEqual(buckets[30]["real"], [])
        self.assertEqual(len(buckets[90]["real"]), 5)

    def test_peaks_ranked_by_real_with_hot_flag(self):
        dan = ([{"t": 61 + i * 0.1, "text": "泪目"} for i in range(30)] +
               [{"t": 1 + i * 0.1, "text": f"闲聊{i}"} for i in range(5)] +
               [{"t": 121 + i * 0.1, "text": f"聊天{i}"} for i in range(5)])
        buckets = bucketize(dan, 30, set())
        peaks = peak_buckets(buckets, top_n=2, bucket_s=30)
        # three buckets real=[30,5,5] -> avg 13.3, hot needs >= 20 -> only the first
        self.assertEqual(peaks[0]["t0"], 60)
        self.assertTrue(peaks[0]["hot"])
        self.assertFalse(peaks[1]["hot"])
        self.assertEqual((peaks[0]["from"], peaks[0]["to"]), ("01:00", "01:30"))
        self.assertEqual(peaks[0]["danmaku_top"][0], {"text": "泪目", "count": 30})


class AlignTests(unittest.TestCase):
    LINES = [{"from": 0, "to": 4, "content": "第一句"},
             {"from": 10, "to": 14, "content": "第二句"},
             {"from": 20, "to": 24, "content": "第三句"}]

    def test_contained(self):
        self.assertEqual(align_subtitle(self.LINES, 12), "第二句")

    def test_falls_back_latest_before(self):
        self.assertEqual(align_subtitle(self.LINES, 17), "第二句")

    def test_first_line_before_t(self):
        self.assertEqual(align_subtitle(self.LINES, 5), "第一句")

    def test_empty(self):
        self.assertIsNone(align_subtitle([], 10))


class TopTextsTests(unittest.TestCase):
    def test_min_len_and_order(self):
        dan = ([{"t": 1, "text": "神"}] * 5 +        # len 1 -> excluded
               [{"t": 1, "text": "泪目"}] * 8 +
               [{"t": 1, "text": "爷青回"}] * 3)
        out = top_texts(dan, set(), min_len=2, top_n=2)
        self.assertEqual([x["text"] for x in out], ["泪目", "爷青回"])


class FmtTests(unittest.TestCase):
    def test_fmt(self):
        self.assertEqual(fmt_mmss(0), "00:00")
        self.assertEqual(fmt_mmss(754), "12:34")


if __name__ == "__main__":
    unittest.main()
