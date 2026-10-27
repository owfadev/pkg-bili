"""Six-channel video comprehension materials — the debrief pipeline.

Ported from dsh-bili-whale tools/commands/debrief.mjs (MIT, Alterssss). The division of
labor is whale's own contract: the script gathers, aligns and buckets; the model reads
the images and writes the verdict (素材不是指令 — everything from danmaku/comments/subtitles
is third-party text, never instructions).

Channels:
  ① meta (view)            what it claims to be      — api.video (guard-cached)
  ② cover                  the thumbnail promise     — downloaded for read_image
  ③ official subtitles     what was actually said    — api.subtitle (CC/AI chain)
  ④ danmaku hot zones      where the crowd erupted   — XML endpoint, spam-normalized,
                                                        bucketed, aligned to ③'s timeline
  ⑤ hot comments           what finished viewers say — api.comments
  ⑥ frame grid             what it looks like        — frames.py (scene-snapped tiles)

Pure functions are separated from the orchestrator so tests need no network and no ffmpeg.
"""

import html
import json
import re
import time
import unicodedata
import zlib
from pathlib import Path

from . import frames
from .config import APP_URL

_D_RE = re.compile(r'<d p="([^"]*)">([^<]*)</d>')


def fmt_mmss(s: float) -> str:
    return f"{int(s // 60):02d}:{int(s % 60):02d}"


# -- ④ danmaku: parse, normalize, spam, bucket ---------------------------------

def parse_danmaku(data: bytes) -> list[dict]:
    """comment.bilibili.com/{cid}.xml -> [{t, text}]. The endpoint historically returns a
    deflate stream WITHOUT content-encoding (requests won't inflate it) — hence the ladder."""
    candidates = [data]
    for dec in (lambda b: zlib.decompress(b, -15), zlib.decompress,
                lambda b: zlib.decompress(b, 31)):  # raw deflate / zlib / gzip
        try:
            candidates.append(dec(data))
        except zlib.error:
            continue
    for buf in candidates:
        try:
            text = buf.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if "<d " not in text and "<i>" not in text:
            continue
        out = []
        for m in _D_RE.finditer(text):
            p = m.group(1).split(",")
            try:
                t = float(p[0])
            except (IndexError, ValueError):
                continue
            out.append({"t": t, "text": html.unescape(m.group(2))})
        return out
    return []


def normalize_text(text: str) -> str:
    """Keep only letters/digits — punctuation/symbol-only spam waves normalize to ''
    (whale's fix: skipping them entirely made '?'+500 waves invisible to the ratio rule)."""
    return "".join(c for c in text
                   if unicodedata.category(c)[:1] in ("L", "N"))


def find_spam(danmaku: list[dict], min_count: int = 20, ratio: float = 0.3) -> set[str]:
    """Single normalized sentences occupying >ratio of the sample with >=min_count hits."""
    counts: dict[str, int] = {}
    for x in danmaku:
        k = normalize_text(x["text"])
        counts[k] = counts.get(k, 0) + 1
    total = max(1, len(danmaku))
    return {k for k, c in counts.items() if c >= min_count and c / total > ratio}


def is_spam(text: str, spam: set[str]) -> bool:
    k = normalize_text(text)
    return k == "" or any(s in k for s in spam)


def bucketize(danmaku: list[dict], bucket_s: int, spam: set[str]) -> dict[int, dict]:
    """t -> {raw: [...], real: [...]} — real excludes spam waves; peaks rank on real only
    (ceremonial opening barrages would otherwise drown the actual climax)."""
    buckets: dict[int, dict] = {}
    for x in danmaku:
        k = int(x["t"] // bucket_s) * bucket_s
        b = buckets.setdefault(k, {"raw": [], "real": []})
        b["raw"].append(x["text"])
        if not is_spam(x["text"], spam):
            b["real"].append(x["text"])
    return buckets


def peak_buckets(buckets: dict[int, dict], top_n: int, bucket_s: int = 30,
                 hot_mult: float = 1.5) -> list[dict]:
    """Top buckets by spam-filtered count, each with its own ranked danmaku lines."""
    real_total = sum(len(b["real"]) for b in buckets.values())
    avg = real_total / max(1, len(buckets))
    ranked = sorted(buckets.items(), key=lambda kv: len(kv[1]["real"]), reverse=True)[:top_n]
    peaks = []
    for k, b in ranked:
        counts: dict[str, int] = {}
        for t in b["real"]:
            counts[t] = counts.get(t, 0) + 1
        top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:6]
        peaks.append({
            "t0": k, "from": fmt_mmss(k), "to": fmt_mmss(k + bucket_s),
            "real": len(b["real"]), "raw": len(b["raw"]),
            "hot": len(b["real"]) >= avg * hot_mult,
            "danmaku_top": [{"text": t, "count": c} for t, c in top],
        })
    return peaks


def align_subtitle(lines: list[dict], t: float) -> str | None:
    """The line playing at t; falls back to the latest line that started before t."""
    best = None
    for l in lines:
        frm, to = float(l.get("from", 0)), float(l.get("to", l.get("from", 0) + 3))
        if frm <= t <= max(to, frm):
            return l.get("content", "")
        if frm <= t and (best is None or frm > best[0]):
            best = (frm, l.get("content", ""))
    return best[1] if best else None


def top_texts(danmaku: list[dict], spam: set[str], min_len: int = 2, top_n: int = 12) -> list[dict]:
    counts: dict[str, int] = {}
    for x in danmaku:
        if not is_spam(x["text"], spam) and len(x["text"]) >= min_len:
            counts[x["text"]] = counts.get(x["text"], 0) + 1
    return [{"text": t, "count": c}
            for t, c in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]]


# -- ③ subtitle lines from api.subtitle() raw body ------------------------------

def subtitle_lines(sub_raw: dict) -> list[dict]:
    """api.subtitle() returns full text joined; for alignment we need per-line timings —
    fetch the raw track body separately (the orchestrator does this)."""
    return sub_raw if isinstance(sub_raw, list) else []


# -- orchestrator ----------------------------------------------------------------

def fetch_danmaku(session, cid: int, bvid: str, headers: dict) -> list[dict]:
    from . import traffic
    url = f"https://comment.bilibili.com/{cid}.xml"
    traffic.throttle()
    r = session.get(url, headers=headers, timeout=20)
    return parse_danmaku(r.content)


def fetch_subtitle_body(session, sub_url: str, headers: dict) -> list[dict]:
    from . import traffic
    if sub_url.startswith("//"):
        sub_url = "https:" + sub_url
    traffic.throttle()
    try:
        doc = session.get(sub_url, headers=headers, timeout=15).json()
        return doc.get("body") or []
    except (ValueError, OSError):
        return []


def pick_subtitle_track(subs: list[dict]) -> dict | None:
    if not subs:
        return None
    for lan in ("zh-CN", "ai-zh"):     # human CC first, AI as fallback
        for s in subs:
            if s.get("lan") == lan:
                return s
    return subs[0]


def run_debrief(api, bvid: str, *, grid: str = "3x3", tile: int = 720, top: int = 5,
                bucket_s: int = 30, n_comments: int = 8, want_frames: bool = True,
                uniform: bool = False, substrip_every: int = 0, out_dir: str | None = None,
                max_chars: int = 200, now: float | None = None) -> dict:
    """Gather all six channels into out_dir; return the manifest (paths + distillations).
    Network failures degrade channel-by-channel — a missing channel is reported, never fatal."""
    now = now if now is not None else time.time()
    view = api.video(bvid)
    cid = (view.get("pages") or [{}])[0].get("cid")
    dur = int(view.get("duration") or 0)
    stat = view.get("stat") or {}
    out = Path(out_dir) if out_dir else Path(f"/tmp/bili-debrief-{bvid}-{int(now)}")
    out.mkdir(parents=True, exist_ok=True)
    headers = {"Referer": f"{APP_URL}/video/{bvid}/"}
    manifest = {
        "ok": True, "bvid": bvid, "aid": view.get("aid"), "cid": cid,
        "title": view.get("title"), "up": (view.get("owner") or {}).get("name"),
        "duration_s": dur, "pubdate": view.get("pubdate"),
        "stat": {k: stat.get(k) for k in ("view", "danmaku", "reply", "like", "favorite")},
        "desc_head": (view.get("desc") or "")[:300],
        "out_dir": str(out), "channels": {},
    }
    ch = manifest["channels"]

    # ② cover
    try:
        cover = out / "cover.jpg"
        r = api.s.get(view.get("pic"), headers=headers, timeout=20)
        cover.write_bytes(r.content)
        ch["cover"] = {"path": str(cover)}
    except OSError:
        ch["cover"] = {"error": "download failed"}

    # ③ official subtitle (timed body for ④ alignment)
    try:
        player = api._call("player", {"bvid": bvid, "cid": cid})
        subs = ((player or {}).get("subtitle") or {}).get("subtitles") or []
        track = pick_subtitle_track(subs)
    except SystemExit:
        track = None
    lines: list[dict] = []
    if track:
        lines = fetch_subtitle_body(api.s, track.get("subtitle_url", ""), headers)
    if lines:
        sub_file = out / "subtitle.txt"
        sub_file.write_text("\n".join(f"[{fmt_mmss(float(l.get('from', 0)))}] {l.get('content', '')}"
                                      for l in lines), encoding="utf-8")
        ch["subtitle"] = {"lines": len(lines), "lan": track.get("lan"), "path": str(sub_file)}
    else:
        ch["subtitle"] = {"lines": 0, "note": "no official track (anonymous sessions see none at all)"}

    # ④ danmaku hot zones
    try:
        danmaku = fetch_danmaku(api.s, cid, bvid, headers)
    except OSError:
        danmaku = []
    if danmaku:
        spam = find_spam(danmaku)
        buckets = bucketize(danmaku, bucket_s, spam)
        peaks = peak_buckets(buckets, top, bucket_s)
        for p in peaks:
            line = align_subtitle(lines, p["t0"] + bucket_s / 2)
            if line:
                p["subtitle_line"] = line[:max_chars]
        claimed = int(stat.get("danmaku") or 0)
        ch["danmaku"] = {
            "fetched": len(danmaku), "claimed_total": claimed,
            "sample_note": f"XML endpoint serves a sample (~1/{max(1, round(claimed / len(danmaku)))})"
                           if claimed else None,
            "spam_removed": len(danmaku) - sum(len(b['real']) for b in buckets.values()),
            "buckets": len(buckets), "bucket_s": bucket_s,
            "peaks": peaks, "top_all": top_texts(danmaku, spam),
        }
    else:
        ch["danmaku"] = {"fetched": 0, "note": "danmaku unavailable (closed or risk-controlled)"}

    # ⑤ hot comments
    try:
        rep = api.comments(bvid, ps=min(20, max(1, n_comments)), pn=1, sort="hot")
        items = []
        for r in (rep.get("replies") or [])[:n_comments]:
            items.append({
                "user": (r.get("member") or {}).get("uname"),
                "like": r.get("like"), "replies": r.get("rcount"),
                "text": re.sub(r"\s+", " ", (r.get("content") or {}).get("message", ""))[:max_chars],
            })
        ch["comments"] = {"count": len(items), "items": items}
    except SystemExit as e:
        ch["comments"] = {"error": str(e)[:120]}

    # ⑥ frames
    if want_frames:
        ch["frames"] = frames.extract_channel(api, bvid, view, out, grid=grid, tile=tile,
                                              uniform=uniform, substrip_every=substrip_every)
    else:
        ch["frames"] = {"skipped": True}

    manifest_file = out / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    manifest["manifest_path"] = str(manifest_file)
    return manifest
