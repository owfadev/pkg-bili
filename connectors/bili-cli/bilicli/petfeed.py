"""Pet feed: candidate selection aligned with dsh-bili-whale feed.mjs (MIT, Alterssss).

The scoring philosophy is the owner-tuned final form (2026-10-20/22): NO play-count bias
(a big-UP jackpot must not dominate); score = danmaku density (log-scaled) x3 + preference
hits x4 + learned-taste boost (<=7.5, from liked/favorited title tokens).  Junk floor:
play<300 AND danmaku<3 dropped unless >= 2 preference hits (a possible niche gem).
Diversity: per hit-combo <= per-combo cap, zero-hit candidates capped at 3.

Sources: preference-keyword search (alternating click/pubdate order per keyword) +
popular + rank + followees' dynamics; a source that fails degrades silently.
"""

import math
import re
import time
from pathlib import Path

from .pet import (FEEDBACK_FILE, PET_DIR, _read_json, _write_json,
                  load_config, load_quota)

SEEN_FILE = PET_DIR / "seen.json"
SEARCH_CACHE = PET_DIR / "feed-search-cache.json"
SEARCH_TTL_S = 10 * 60
CACHE_MAX = 40

JOIN_WORDS = re.compile(r"[\s·,，、/]+|与|和|及|向|的|类")
_BV_RE = re.compile(r"\bBV[0-9A-Za-z]{10}\b")
STOP_WORDS = {"我为", "添加", "开屏", "广告", "这个", "那个", "什么", "自己", "可以", "就是", "一个"}


def parts_of(pref: str) -> list[str]:
    return list(dict.fromkeys(p.strip() for p in JOIN_WORDS.split(str(pref)) if len(p.strip()) >= 2))


def search_keyword_of(pref: str) -> str:
    """One searchable word per preference: first segment if it splits, a short block as-is,
    else the LAST pure-Chinese bigram (「B站生态八卦」-> 八卦, never the whole sentence)."""
    parts = parts_of(pref)
    if len(parts) > 1:
        return parts[0]
    if len(parts) == 1 and len(parts[0]) < 4:
        return parts[0]
    grams = [pref[i:i + 2] for i in range(len(pref) - 1)
             if re.fullmatch(r"[\u4e00-\u9fa5]{2}", pref[i:i + 2])]
    return grams[-1] if grams else str(pref)


def tokens_of(pref: str) -> list[str]:
    """Hit-testing tokens: full phrase + split parts + pure-Chinese bigram window (a long
    block like 鬼畜与怀旧向剪辑 must still hit on 鬼畜/怀旧/剪辑)."""
    extra = []
    for part in parts_of(pref):
        if len(part) < 4:
            continue
        extra += [part[i:i + 2] for i in range(len(part) - 1)
                  if re.fullmatch(r"[\u4e00-\u9fa5]{2}", part[i:i + 2])]
    return list(dict.fromkeys([str(pref)] + parts_of(pref) + extra))


def learn_tokens(text: str, owner_name: str = "") -> list[str]:
    """Taste tokens from liked/favorited titles+reasons: latin words (>=2) + zh 3-4 grams,
    stopworded, owner-name stripped (title similarity via Jaccard measured useless —
    topic tokens like 'dsh'/'大肥鱼' carry the real signal)."""
    base = re.sub(re.escape(owner_name), " ", str(text)) if owner_name else str(text)
    base = re.sub(r"@\S+", " ", base)
    out = set()
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9]+", base):
        out.add(m.group(0).lower())
    for m in re.finditer(r"[\u4e00-\u9fa5]{2,}", base):
        run = m.group(0)
        for n in (3, 4):
            for i in range(len(run) - n + 1):
                g = run[i:i + n]
                if g not in STOP_WORDS:
                    out.add(g)
    return sorted(out)


def hits_of(title: str, pref_tokens: list[tuple[str, list[str]]]) -> list[str]:
    return [label for label, toks in pref_tokens if any(t in title for t in toks)]


def junk(item: dict, hits: int) -> bool:
    return item.get("play", 0) < 300 and item.get("dm", 0) < 3 and hits < 2


def score_of(item: dict, hits: int, boost: float) -> float:
    density = math.log10(1 + item.get("dm", 0) / max(30, item.get("d", 1)))
    return density * 3 + hits * 4 + min(7.5, boost)


def diversify(items: list[dict], per_combo: int, none_cap: int = 3) -> list[dict]:
    """Per hit-combo <= per_combo; zero-hit candidates capped (anti-filler, but not to 1)."""
    used: dict[str, int] = {}
    out = []
    for x in items:
        key = "+".join(x["_hits"]) if x["_hits"] else "__none__"
        cap = per_combo if x["_hits"] else none_cap
        used[key] = used.get(key, 0)
        if used[key] >= cap:
            continue
        used[key] += 1
        out.append(x)
    return out


def seen_set(quota_log: list) -> set:
    seen = set(_read_json(SEEN_FILE, []))
    for x in quota_log:
        if x.get("target", "").startswith("BV"):
            seen.add(x["target"])
    return seen


def mark_seen(bv: str) -> None:
    lst = _read_json(SEEN_FILE, [])
    if bv not in lst:
        lst.append(bv)
        _write_json(SEEN_FILE, lst[-2000:])


def _secs(duration_text: str) -> int:
    p = [int(v) for v in str(duration_text).split(":") if v != ""]
    if not p:
        return 0
    if len(p) == 2:
        return p[0] * 60 + p[1]
    if len(p) == 3:
        return p[0] * 3600 + p[1] * 60 + p[2]
    return 0


def _search_cached(api, kw: str, order: str):
    key = f"{kw}|{order}"
    store = _read_json(SEARCH_CACHE, {})
    hit = store.get(key)
    if hit and time.time() - float(hit.get("at", 0)) < SEARCH_TTL_S:
        return hit["data"]
    data = api._call("search", {"search_type": "video", "keyword": kw, "order": order, "page": 1})
    if data:
        store[key] = {"at": time.time(), "data": data}
        for k in sorted(store, key=lambda k: store[k].get("at", 0))[: max(0, len(store) - CACHE_MAX)]:
            del store[k]
        _write_json(SEARCH_CACHE, store)
    return data


def run_feed(api, cfg: dict | None = None, *, n: int = 5, per: int = 2,
             source: str = "all") -> dict:
    cfg = cfg or load_config()
    prefs = list(cfg["preferences"]["like"])
    dislike = [d for d in cfg["preferences"].get("dislike", [])]
    self_name = str(cfg.get("self", {}).get("name", ""))
    owner_name = str(cfg.get("owner", {}).get("name", ""))
    q, _ = load_quota()
    seen = seen_set(q["log"])

    pool: list[dict] = []

    def add(bv, title, dur, dm, play, up, kw):
        clean = re.sub(r"<[^>]+>", "", str(title))
        if any(d and d in clean for d in dislike):
            return
        pool.append({"bv": bv, "t": clean, "d": dur, "dm": int(dm or 0), "play": int(play or 0),
                     "up": str(up or ""), "kw": kw})

    keywords = list(dict.fromkeys(search_keyword_of(p) for p in prefs[:6]))
    if source in ("search", "all"):
        for i, kw in enumerate(keywords):
            order = "click" if i % 2 == 0 else "pubdate"
            try:
                data = _search_cached(api, kw, order) or {}
                for it in (data.get("result") or [])[: max(4, n // max(1, len(keywords)) + 4)]:
                    add(it.get("bvid"), it.get("title"), _secs(it.get("duration", "0:0")),
                        it.get("video_review") or 0, it.get("play"), it.get("author"), kw)
            except SystemExit:
                pass
    if source in ("popular", "all"):
        try:
            for it in (api._call("popular", {"pn": 1, "ps": 20}) or {}).get("list") or []:
                add(it.get("bvid"), it.get("title"), it.get("duration", 0),
                    (it.get("stat") or {}).get("danmaku"), (it.get("stat") or {}).get("view"),
                    (it.get("owner") or {}).get("name"), "热门")
        except SystemExit:
            pass
    if source in ("rank", "all"):
        try:
            for it in ((api._call("rank", {"rid": 0, "type": "all"}) or {}).get("list") or [])[:20]:
                add(it.get("bvid"), it.get("title"), it.get("duration", 0),
                    (it.get("stat") or {}).get("danmaku"), (it.get("stat") or {}).get("view"),
                    (it.get("owner") or {}).get("name"), "排行")
        except SystemExit:
            pass
    if source in ("following", "all"):
        try:
            for it in ((api._call("dyn_feed", {"type": "video", "page": 1}) or {}).get("items") or []):
                ar = ((it.get("modules") or {}).get("module_dynamic") or {}).get("major", {}).get("archive") or {}
                name = ((it.get("modules") or {}).get("module_author") or {}).get("name", "")
                if ar.get("bvid"):
                    add(ar["bvid"], ar.get("title"), _secs(ar.get("duration_text", "0:0")),
                        0, (ar.get("stat") or {}).get("play"), name, "关注")
        except SystemExit:
            pass

    # learned taste: liked events -> topic tokens -> per-hit boost
    learned = set()
    fb = _read_json(FEEDBACK_FILE, {"events": []})
    for e in fb.get("events", []):
        title = str(e.get("videoTitle", ""))
        if title.strip():
            learned.update(learn_tokens(title + " " + str(e.get("note", "")), owner_name))
    learned_hits = lambda t: sum(1 for tok in learned if tok in t.lower())

    pref_tokens = [(p, tokens_of(p)) for p in prefs]
    uniq = {}
    for x in pool:
        if x["bv"] and x["bv"] not in seen and not (self_name and self_name in x["up"]):
            uniq[x["bv"]] = x
    ranked = []
    for x in uniq.values():
        x["_hits"] = hits_of(x["t"], pref_tokens)
        if junk(x, len(x["_hits"])):
            continue
        x["_score"] = round(score_of(x, len(x["_hits"]), learned_hits(x["t"]) * 2.5), 2)
        ranked.append(x)
    ranked.sort(key=lambda x: x["_score"], reverse=True)
    picked = diversify(ranked, per)[:n]
    return {
        "ok": True, "n": len(picked), "pool": len(pool), "uniq": len(uniq),
        "keywords": keywords, "learned_tokens": len(learned),
        "items": [{k: x[k] for k in ("bv", "t", "d", "dm", "play", "up", "kw", "_hits", "_score")}
                  for x in picked],
    }
