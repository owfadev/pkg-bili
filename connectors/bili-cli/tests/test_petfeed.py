import unittest

from bilicli.petfeed import (diversify, hits_of, junk, learn_tokens,
                             parts_of, score_of, search_keyword_of, tokens_of)


class KeywordTests(unittest.TestCase):
    def test_parts_split_by_join_words(self):
        self.assertEqual(parts_of("鬼畜与怀旧向剪辑"), ["鬼畜", "怀旧", "剪辑"])

    def test_search_keyword_first_segment(self):
        self.assertEqual(search_keyword_of("鬼畜与怀旧向剪辑"), "鬼畜")

    def test_search_keyword_short_block_as_is(self):
        self.assertEqual(search_keyword_of("猫咪"), "猫咪")

    def test_search_keyword_last_chinese_bigram(self):
        self.assertEqual(search_keyword_of("B站生态八卦"), "八卦")

    def test_tokens_include_bigrams_for_long_blocks(self):
        toks = tokens_of("B站生态八卦")
        self.assertIn("B站生态八卦", toks)
        self.assertIn("生态", toks)
        self.assertIn("八卦", toks)


class LearnTokensTests(unittest.TestCase):
    def test_latin_and_zh_grams_with_stopwords(self):
        toks = learn_tokens("dsh 插件开发：我为 dsh 添加了功能", owner_name="")
        self.assertIn("dsh", toks)
        self.assertNotIn("我为", toks)
        self.assertTrue(any(t.startswith("插件") or "插件开" in t for t in toks))

    def test_owner_name_stripped(self):
        toks = learn_tokens("阿鲸看阿鲸的猫", owner_name="阿鲸")
        joined = " ".join(toks)
        self.assertNotIn("阿鲸", joined.split())


class ScoringTests(unittest.TestCase):
    PREFS = [("鬼畜与怀旧向剪辑", tokens_of("鬼畜与怀旧向剪辑")), ("猫咪", tokens_of("猫咪"))]

    def test_hits(self):
        self.assertEqual(hits_of("怀旧名场面剪辑", self.PREFS), ["鬼畜与怀旧向剪辑"])
        self.assertEqual(hits_of("两只猫咪打架", self.PREFS), ["猫咪"])
        self.assertEqual(hits_of("带货直播", self.PREFS), [])

    def test_junk_floor(self):
        tiny = {"play": 100, "dm": 1}
        self.assertTrue(junk(tiny, 0))
        self.assertFalse(junk(tiny, 2))            # 2 pref hits = possible niche gem
        self.assertFalse(junk({"play": 5000, "dm": 1}, 0))

    def test_score_shape(self):
        x = {"dm": 15027, "d": 92}                  # the 163-point density case from whale
        s_raw = score_of(x, 0, 0)
        self.assertLess(s_raw, 20)                  # log-scaled: must not dominate prefs
        s_boost = score_of({"dm": 0, "d": 60}, 2, 99)
        self.assertLessEqual(s_boost, 2 * 4 + 7.5)  # boost capped at 7.5

    def test_diversify_caps(self):
        items = ([{"_hits": ["a"]}, {"_hits": ["a"]}, {"_hits": ["a"]}]
                 + [{"_hits": []}] * 5 + [{"_hits": ["b"]}])
        out = diversify(items, per_combo=2)
        self.assertEqual(sum(1 for x in out if x["_hits"] == ["a"]), 2)
        self.assertEqual(sum(1 for x in out if x["_hits"] == []), 3)
        self.assertEqual(sum(1 for x in out if x["_hits"] == ["b"]), 1)


if __name__ == "__main__":
    unittest.main()
