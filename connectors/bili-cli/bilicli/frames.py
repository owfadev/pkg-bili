"""Frame extraction: stream download + scene-snapped sampling + tiles.

Ported from dsh-bili-whale tools/commands/bili-watch.mjs (MIT, Alterssss). Two design
decisions worth keeping verbatim:

  * sampling = uniform skeleton (guarantees coverage) with each frame snapped to the
    nearest scene-change point within half a slot — pure-uniform misses one-second key
    frames, pure-scene clusters all cuts at the start (whale stepped on both, 2026-10-14)
  * one frame per timestamp at original resolution, THEN tile — so the grid and the
    subtitle strip share the same frames
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

QN_LABELS = {16: "360P", 32: "480P", 64: "720P", 80: "1080P"}
_PTS_RE = re.compile(r"pts_time:([0-9.]+)")


def find_ffmpeg() -> str:
    """$FFMPEG -> PATH. Raises FileNotFoundError with an actionable message."""
    exe = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if not exe:
        raise FileNotFoundError("ffmpeg not found — install it or set $FFMPEG "
                                "(only the frame channel needs it; --no-frames skips)")
    return exe


def parse_scene_log(text: str) -> list[float]:
    return sorted({float(m) for m in _PTS_RE.findall(text) if float(m) > 0.2})


def scene_times(ffmpeg: str, mp4: str, threshold: float, timeout: int = 600) -> list[float]:
    """Scene-change timestamps via ffmpeg's select+metadata filter (threshold ladder:
    recorded/lecture footage is flat, so 0.3 may find almost nothing and we relax)."""
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", mp4, "-vf",
             f"select='gt(scene,{threshold})',metadata=print:file=-", "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout).stdout
        return parse_scene_log(out)
    except (subprocess.SubprocessError, OSError):
        return []


def pick_times(duration: float, n: int, scenes: list[float]) -> tuple[list[float], int]:
    """Uniform skeleton over duration, each slot snapped to the nearest scene point within
    half a slot. Returns (times, snapped_count)."""
    uniform = [(i + 0.5) * duration / n for i in range(n)]
    if not scenes:
        return uniform, 0
    half = duration / n / 2
    snapped = 0
    times = []
    for u in uniform:
        near = [s for s in scenes if abs(s - u) <= half]
        if near:
            snapped += 1
            times.append(min(near, key=lambda s: abs(s - u)))
        else:
            times.append(u)
    return times, snapped


def download_stream(session, url: str, dest: Path, referer: str, chunk: int = 1 << 20) -> int:
    """Stream to disk (a 2h 720p video is 1–2 GB; buffering it whole is a memory bug)."""
    from . import traffic
    traffic.throttle()
    with session.get(url, headers={"Referer": referer, "User-Agent": session.headers.get("User-Agent", ""),
                                   "Range": "bytes=0-"}, stream=True, timeout=60) as r:
        r.raise_for_status()
        with dest.open("wb") as fh:
            for chunk_bytes in r.iter_content(chunk_size=chunk):
                fh.write(chunk_bytes)
    return dest.stat().st_size


def _run(ffmpeg: str, args: list[str], timeout: int = 600) -> None:
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", *args],
                   check=True, capture_output=True, timeout=timeout)


def extract_grid(ffmpeg: str, mp4: Path, times: list[float], cols: int, rows: int,
                 tile_px: int, out_dir: Path, tag: str) -> tuple[Path, Path]:
    """One frame per timestamp, then tile into a grid + a bottom-subtitle-strip variant.
    Returns (sheet, subs_strip)."""
    tmp = out_dir / f".frames-{tag}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        for i, t in enumerate(times):
            _run(ffmpeg, ["-ss", f"{t:.2f}", "-i", str(mp4), "-frames:v", "1", "-q:v", "2",
                          str(tmp / f"f{i:02d}.jpg")])
        sheet = out_dir / f"{tag}-{cols}x{rows}.jpg"
        _run(ffmpeg, ["-i", str(tmp / "f%02d.jpg"), "-vf",
                      f"scale={tile_px}:-1,tile={cols}x{rows}", "-frames:v", "1", str(sheet)])
        subs = out_dir / f"{tag}-subs.jpg"
        try:
            _run(ffmpeg, ["-i", str(tmp / "f%02d.jpg"), "-vf",
                          f"crop=iw:ih*0.17:0:ih*0.83,scale=1280:-1,tile=1x{len(times)}",
                          "-frames:v", "1", str(subs)])
        except subprocess.SubprocessError:
            subs = None  # strip is best-effort; the sheet is the deliverable
        return sheet, subs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def substrip_from_video(ffmpeg: str, mp4: Path, every_s: float, duration: float,
                        out: Path) -> Path:
    """Hard-subtitle band collage: crop the bottom 32% of the frame every N seconds and
    stack vertically — one image ≈ the entire dialogue of a video with no CC track."""
    rows = max(4, min(20, int(duration // every_s) or 4))
    _run(ffmpeg, ["-i", str(mp4), "-vf",
                  f"fps=1/{every_s:g},crop=iw:ih*0.32:0:ih*0.57,scale=1280:-1,tile=1x{rows}",
                  "-frames:v", "1", str(out)], timeout=1800)
    return out


def extract_channel(api, bvid: str, view: dict, out_dir: Path, *, grid: str = "3x3",
                    tile: int = 720, uniform: bool = False, substrip_every: int = 0) -> dict:
    """The debrief orchestrator's ⑥ entry: playurl -> download -> sample -> tiles.
    Degrades with explicit reasons (some videos need login for any stream URL)."""
    try:
        cols_s, rows_s = grid.lower().split("x")
        cols, rows = int(cols_s), int(rows_s)
        if cols < 1 or rows < 1 or cols * rows > 36:
            raise ValueError(grid)
    except ValueError:
        return {"error": f"grid must be like 3x3 (<=36 cells), got {grid!r}"}
    try:
        ffmpeg = find_ffmpeg()
    except FileNotFoundError as e:
        return {"error": str(e)}

    cid = (view.get("pages") or [{}])[0].get("cid")
    duration = int(view.get("duration") or 0)
    try:
        p = api.playurl(bvid, cid)
    except SystemExit as e:
        return {"error": f"playurl failed: {e}"}
    url = ((p.get("durl") or [{}])[0].get("url"))
    if not url:
        return {"error": "no plain durl stream (login-gated or dash-only video)"}
    quality = QN_LABELS.get(p.get("quality"), p.get("quality"))

    mp4 = out_dir / f"{bvid}.mp4"
    size = download_stream(api.s, url, mp4, f"https://www.bilibili.com/video/{bvid}/")

    n = cols * rows
    times = [(i + 0.5) * duration / n for i in range(n)]
    snapped = 0
    method = f"uniform ({duration / n:.1f}s apart)"
    if not uniform:
        scenes: list[float] = []
        th = 0.3
        for th in (0.3, 0.15, 0.08):
            scenes = scene_times(ffmpeg, str(mp4), th)
            if len(scenes) >= n:
                break
        if scenes:
            times, snapped = pick_times(duration, n, scenes)
            method = (f"uniform skeleton + scene snap (threshold {th}, {len(scenes)} changes, "
                      f"{snapped}/{n} snapped)")
        else:
            method = f"uniform (no scene changes even at 0.08; {duration / n:.1f}s apart)"

    try:
        sheet, subs = extract_grid(ffmpeg, mp4, times, cols, rows, tile, out_dir, bvid)
    except subprocess.SubprocessError as e:
        return {"error": f"ffmpeg extraction failed: {e}", "mp4": str(mp4)}

    result = {
        "quality": quality, "mp4": str(mp4), "mp4_bytes": size,
        "sheet": str(sheet), "method": method,
        "times": [f"{int(t // 60):02d}:{int(t % 60):02d}" for t in times],
    }
    if subs:
        result["subs_strip"] = str(subs)
    if substrip_every > 0:
        try:
            strip = substrip_from_video(ffmpeg, mp4, substrip_every, duration,
                                        out_dir / f"{bvid}-substrip.png")
            result["substrip"] = str(strip)
        except subprocess.SubprocessError:
            pass
    return result
