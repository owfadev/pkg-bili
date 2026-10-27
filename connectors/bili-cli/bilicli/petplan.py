"""Pet night plan: the biological-clock dice, ported from dsh-bili-whale night-plan.mjs
(MIT, Alterssss) — schema 2 semantics only (fresh start, no legacy migration):

  * windows may wrap midnight; each window has its own wakes quota (cap 12/day total)
  * a pending plan is never re-rolled (the random decision AND its time persist)
  * minimum gap between wakes is a HARD floor (minGapMin, default 10) — a window that
    cannot fit the next wake is skipped, never compressed (whale 2026-10-22 fix)
  * "skip today" consumes a wake slot exactly like a wake does (a skip must not void
    the whole window — whale 2026-10-22 fix)

The state file is executor-side (~/.bili-cli/pet/plan.json): the wiki cron task page
records the INTENT (windows/skipChance), this engine rolls and persists the dice.
"""

import random
import time
from datetime import datetime, timedelta

from .pet import PET_DIR, _read_json, _write_json

PLAN_FILE = PET_DIR / "plan.json"
WAKES_DAY_MAX = 12


def _parse_hhmm(s: str) -> int:
    h, m = str(s).split(":")
    h, m = int(h), int(m)
    if h > 24 or m > 59 or (h == 24 and m != 0):
        raise ValueError(f"invalid schedule time: {s}")
    return h * 60 + m


def windows_of(night: dict) -> list[dict]:
    wins = night.get("windows") or [{"start": "20:00", "end": "24:00", "wakes": 1}]
    out = []
    for w in wins:
        start = _parse_hhmm(w.get("start", "20:00"))
        end_raw = _parse_hhmm(w.get("end", "24:00"))
        end = end_raw if end_raw > start else end_raw + 1440
        out.append({"start": start, "end": end, "wrap": end > 1440,
                    "label": f"{w.get('start')}-{w.get('end')}",
                    "wakes": min(WAKES_DAY_MAX, max(0, int(w.get("wakes", 1) or 0)))})
    return out


def max_wakes(night: dict) -> int:
    return min(WAKES_DAY_MAX, sum(w["wakes"] for w in windows_of(night)))


def _occurrences(night: dict, now: float) -> list[dict]:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    days = [today - timedelta(days=1), today] if any(w["wrap"] for w in windows_of(night)) else [today]
    out = []
    for w in windows_of(night):
        for day in ([today - timedelta(days=1), today] if w["wrap"] else [today]):
            starts = day + timedelta(minutes=w["start"])
            ends = day + timedelta(minutes=w["end"])
            if ends.timestamp() * 1000 > now:
                out.append({**w, "key": f"{day:%Y-%m-%d}|{w['label']}",
                            "starts_ms": starts.timestamp() * 1000, "ends_ms": ends.timestamp() * 1000})
    return out


def _history(state: dict, now: float) -> dict:
    used = dict(state.get("usedCount", {}))
    cutoff = time.strftime("%Y-%m-%d", time.localtime(now - 3 * 86_400))
    used = {k: v for k, v in used.items() if k[:10] >= cutoff}
    today = time.strftime("%Y-%m-%d")
    return {"usedCount": used,
            "wakes": int(state.get("wakes", 0)) if state.get("date") == today else 0,
            "wokeAt": state.get("wokeAt")}


def is_pending(state: dict, now: float) -> bool:
    return (state.get("schema") == 2 and state.get("action") in ("wake", "skip")
            and not state.get("fired") and not state.get("woke")
            and isinstance(state.get("atMs"), (int, float))
            and state.get("endsMs", 0) > now)


def make_plan(night: dict, state: dict, now: float | None = None,
              rng: random.Random | None = None) -> dict:
    """Roll tonight's plan. A pending plan is returned untouched (never re-rolled)."""
    rng = rng or random.Random()
    now = now if now is not None else time.time() * 1000
    if is_pending(state, now):
        return state
    h = _history(state, now)
    if h["wakes"] >= max_wakes(night):
        return {**h, "schema": 2, "date": time.strftime("%Y-%m-%d"),
                "action": "done", "done": True, "reason": "今天醒来次数已用完"}
    pool = sorted((w for w in _occurrences(night, now)
                   if w["wakes"] > h["usedCount"].get(w["key"], 0)),
                  key=lambda w: (w["ends_ms"], w["starts_ms"]))
    if not pool:
        return {**h, "schema": 2, "date": time.strftime("%Y-%m-%d"),
                "action": "done", "done": True, "reason": "今天可用窗口已结束"}
    min_gap = max(1, int(night.get("minGapMin", 10))) * 60_000
    last_woke = 0.0
    try:
        last_woke = time.mktime(time.strptime(str(state.get("wokeAt"))[:19],
                                              "%Y-%m-%dT%H:%M:%S")) * 1000
    except (TypeError, ValueError):
        pass
    picked = None
    for cand in pool:
        earliest = max(now, cand["starts_ms"], last_woke + min_gap)   # last_woke=0 -> no constraint
        if earliest >= cand["ends_ms"] - 30_000:
            continue                    # this window cannot fit the gap — skip it, don't compress
        picked = (cand, earliest)
        break
    if not picked:
        return {**h, "schema": 2, "date": time.strftime("%Y-%m-%d"),
                "action": "done", "done": True, "reason": "剩余窗口排不下下一次（最小间隔不够）"}
    cand, earliest = picked
    at_ms = int(earliest + rng.random() * max(1, cand["ends_ms"] - earliest))
    at = datetime.fromtimestamp(at_ms / 1000)
    skip = rng.random() < min(1.0, max(0.0, float(night.get("skipChance", 0.3))))
    return {**h, "schema": 2, "date": time.strftime("%Y-%m-%d"),
            "action": "skip" if skip else "wake", "fired": False, "atMs": at_ms,
            "atText": f"{at:%Y-%m-%d %H:%M}", "window": cand["label"], "windowKey": cand["key"],
            "startsMs": cand["starts_ms"], "endsMs": cand["ends_ms"],
            "withinWindow": f"{cand['label']} 第 {h['usedCount'].get(cand['key'], 0) + 1}/{cand['wakes']} 次",
            "maxVideos": int(night.get("maxVideos", 5)),
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            **({"reason": "本次计划跳过看视频，仍写日记"} if skip else {"roll": "watch"})}


def complete_plan(state: dict, now: float | None = None) -> dict:
    """Mark fired: a skip consumes a slot exactly like a wake (no window blacklisting)."""
    now = now if now is not None else time.time() * 1000
    if state.get("fired"):
        return state
    if not state.get("windowKey"):
        raise ValueError("plan missing windowKey — run plan first")
    h = _history(state, now)
    if state.get("action") in ("skip", "wake"):
        h["usedCount"][state["windowKey"]] = h["usedCount"].get(state["windowKey"], 0) + 1
        h["wakes"] += 1
    return {**state, **h, "fired": True, "woke": state.get("action") == "wake",
            "wokeAt": time.strftime("%Y-%m-%dT%H:%M:%S") if state.get("action") == "wake" else state.get("wokeAt")}


def plan_status() -> dict:
    return _read_json(PLAN_FILE, {})


def plan_save(state: dict) -> None:
    _write_json(PLAN_FILE, state)
