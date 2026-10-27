"""Request protection: metadata cache + risk-control circuit breaker + in-process throttle.

Ported from dsh-bili-whale tools/lib/bili-api.mjs (MIT, Alterssss) — the community-measured
premise: bilibili 412/-412 rate-limits by IP, so hitting another endpoint never helps; the
correct moves are cache what is static, back off when told to, and never fire requests
back-to-back.  State lives beside the credentials (~/.bili-cli/, never in any repo):

  api-cache.json     24h metadata cache (view / acc info; 400 entries, LRU-trimmed)
  api-ban.json       streak-based exponential pause (5/10/20/30 min, 30 min cap)
  api-requests.log   one line per network hit / cache hit (no cookies, no bodies)

BILI_NO_CACHE=1 disables cache and breaker (debug / forced-network probing).
"""

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from .config import API_BASE, CONFIG_DIR

CACHE_FILE = CONFIG_DIR / "api-cache.json"
BAN_FILE = CONFIG_DIR / "api-ban.json"
REQ_LOG = CONFIG_DIR / "api-requests.log"

CACHE_TTL_S = 24 * 3600
STALE_MAX_S = 7 * CACHE_TTL_S          # during a ban, static metadata may serve stale up to 7d
NAV_TTL_S = 10 * 60                    # nav carries the rotating WBI keys — short TTL only
NAV_PATH = "/x/web-interface/nav"
CACHEABLE_PATHS = (
    "/x/web-interface/view",           # video metadata
    "/x/web-interface/wbi/view",
    "/x/space/wbi/acc/info",           # user info
)
BAN_CODES = {-412, -352, -799, -509, -10403}   # interface-level risk control (HTTP 200 body codes)
MIN_GAP_S = 0.3                        # in-process throttle between network hits
CACHE_ENTRIES_MAX = 400

_lock = threading.Lock()
_last_request_at = 0.0


class GuardBlocked(RuntimeError):
    """The guard refused to hit the network (ban active, or risk-control response).
    The message is user-facing; callers translate it to their own exit convention."""


def _no_cache() -> bool:
    return os.environ.get("BILI_NO_CACHE") == "1"


def _read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def _write_json(path: Path, value) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    except OSError:
        pass


def _log_request(hit: bool, path: str) -> None:
    if _no_cache():
        return
    try:
        with REQ_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {'HIT ' if hit else 'NET '} {path}\n")
    except OSError:
        pass


def _account_key(session) -> str:
    """Cache/ban isolation per login state: hash of SESSDATA (never the value itself)."""
    try:
        sess = session.cookies.get("SESSDATA") or ""
    except Exception:
        sess = ""
    return hashlib.sha256(sess.encode()).hexdigest()[:16] if sess else "anon"


def _cache_path(url) -> bool | None:
    m = re.match(r"^https?://[^/]+(/[^?]*)(?:\?.*)?$", url)
    return m.group(1) if m else None


def _cacheable(url: str) -> bool:
    p = _cache_path(url)
    return p in CACHEABLE_PATHS if p else False


def _cache_key(session, url: str) -> str:
    """Strip per-request signature params (wts/w_rid) so signed and unsigned forms collide."""
    base, _, query = url.partition("?")
    p = _cache_path(url) or "/"
    if p == "/x/web-interface/view":
        base = base.replace(p, "/x/web-interface/wbi/view")
    params = sorted(
        kv for kv in query.split("&") if kv and not kv.startswith(("wts=", "w_rid="))
    )
    return f"{_account_key(session)}:{base}?{'&'.join(params)}"


# -- breaker ------------------------------------------------------------------

def ban_state() -> dict:
    return _read_json(BAN_FILE, {})


def _ban_active() -> float:
    b = ban_state()
    until = float(b.get("until") or 0)
    return until if until > time.time() else 0.0


def _note_ban(why: str, endpoint: str) -> int:
    """Exponential pause on consecutive risk-control hits: 5, 10, 20, 30 min (cap)."""
    prev = ban_state()
    streak = int(prev.get("streak") or 0) + 1
    mins = min(30, 5 * 2 ** (streak - 1))
    _write_json(BAN_FILE, {"until": time.time() + mins * 60, "why": why, "at":
                           time.strftime("%Y-%m-%dT%H:%M:%S"), "streak": streak,
                           "endpoint": endpoint})
    return mins


def _clear_streak(endpoint: str) -> None:
    b = ban_state()
    if b.get("streak") and b.get("endpoint") == endpoint:
        _write_json(BAN_FILE, {**b, "streak": 0})


# -- throttle -----------------------------------------------------------------

def throttle() -> None:
    """Sleep just enough to keep MIN_GAP_S between network hits (in-process)."""
    global _last_request_at
    with _lock:
        gap = time.time() - _last_request_at
        if gap < 0:               # clock jumped backwards (NTP, test mocks) — never sleep eons
            gap = MIN_GAP_S       # just enforce one fresh gap
        wait = MIN_GAP_S - gap
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.time()


# -- guard lifecycle ----------------------------------------------------------

def _cached(session, url: str, ttl_s: float):
    """TTL-fresh hit, or (ban only) a stale-but-valid hit for static metadata."""
    key = _cache_key(session, url)
    store = _read_json(CACHE_FILE, {})
    hit = store.get(key)
    if not hit:
        return None
    age = time.time() - float(hit.get("at") or 0)
    path = _cache_path(url)
    if path == NAV_PATH and not _cacheable(url):
        # nav: fresh only within NAV_TTL (identity data must not serve stale)
        return {**hit["data"], "_cached": True} if age < NAV_TTL_S else None
    if age < ttl_s:
        return {**hit["data"], "_cached": True}
    if _ban_active() and _cacheable(url) and age < STALE_MAX_S and hit.get("data", {}).get("code") == 0:
        return {**hit["data"], "_cached": True, "_stale": True}
    return None


def _store(session, url: str, body: dict) -> None:
    # cacheable static metadata + nav (the rotating WBI keys make repeated signing cheap)
    if not _cacheable(url) and _cache_path(url) != NAV_PATH:
        return
    key = _cache_key(session, url)
    store = _read_json(CACHE_FILE, {})
    store[key] = {"at": time.time(), "data": body}
    keys = sorted(store, key=lambda k: store[k].get("at", 0))
    for k in keys[: max(0, len(keys) - CACHE_ENTRIES_MAX)]:
        del store[k]
    _write_json(CACHE_FILE, store)


def preflight(session, url: str, ttl_s: float = CACHE_TTL_S):
    """Before any request: breaker first, then cache. Returns a cached body or None.
    Raises GuardBlocked when the breaker is active and no cache can carry the call."""
    if _no_cache() or "bilibili.com" not in url:
        return None
    banned_until = _ban_active()
    if banned_until:
        hit = _cached(session, url, ttl_s)
        if hit is not None:
            _log_request(True, _cache_path(url) or url)
            return hit
        mins = max(1, round((banned_until - time.time()) / 60))
        raise GuardBlocked(
            f"risk-control pause active ({ban_state().get('why')}) — about {mins} min left; "
            "cached answers only until then (state: ~/.bili-cli/api-ban.json)")
    hit = _cached(session, url, ttl_s)
    if hit is not None:
        _log_request(True, _cache_path(url) or url)
        return hit
    return None


def classify(session, url: str, status: int, body: dict | None, *, ttl_s: float = CACHE_TTL_S):
    """After a network response: returns None on pass, or a GuardBlocked with the verdict.
    HTTP 412/429 and interface-level ban codes both arm the exponential breaker."""
    if _no_cache() or "bilibili.com" not in url:
        return None
    path = _cache_path(url) or url
    if status in (412, 429):
        mins = _note_ban(f"HTTP {status}", path)
        return GuardBlocked(f"HTTP {status} (risk control) — paused {mins} min; do not retry immediately")
    if isinstance(body, dict) and body.get("code") in BAN_CODES:
        mins = _note_ban(f"code={body.get('code')}", path)
        return GuardBlocked(
            f"API code={body.get('code')} ({body.get('message', '')}) — risk control, paused {mins} min")
    if isinstance(body, dict) and body.get("code") == 0:
        _clear_streak(path)
        _store(session, url, body)
    return None


def guarded_get(session, url: str, *, params=None, headers=None, timeout=15, ttl_s: float = CACHE_TTL_S):
    """Full GET lifecycle for api.bilibili.com calls: preflight -> throttle -> request ->
    classify. Returns the parsed JSON body (dict); raises GuardBlocked when the guard
    refused (ban) — callers translate to their own error convention."""
    hit = preflight(session, url, ttl_s)
    if hit is not None:
        return hit
    throttle()
    r = session.get(url, params=params, headers=headers, timeout=timeout)
    try:
        body = r.json()
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        blocked = classify(session, url, r.status_code, None, ttl_s=ttl_s)
        raise GuardBlocked(
            blocked.args[0] if blocked else
            f"non-JSON response (HTTP {r.status_code}) — endpoint may have changed or risk control")
    blocked = classify(session, url, r.status_code, body, ttl_s=ttl_s)
    if blocked is not None:
        raise blocked
    _log_request(False, _cache_path(url) or url)
    return body


def api_state() -> dict:
    """Diagnostics for `bili-cli traffic`: breaker + cache at a glance."""
    b = ban_state()
    until = float(b.get("until") or 0)
    return {
        "banned": until > time.time(),
        "ban_minutes_left": max(0, round((until - time.time()) / 60)) if until > time.time() else 0,
        "ban_why": b.get("why"),
        "cache_entries": len(_read_json(CACHE_FILE, {})),
        "cache_file": str(CACHE_FILE),
    }


def _set_state_dir(path) -> None:
    """Tests only: redirect state files into a sandbox directory."""
    global CACHE_FILE, BAN_FILE, REQ_LOG
    p = Path(path)
    CACHE_FILE = p / "api-cache.json"
    BAN_FILE = p / "api-ban.json"
    REQ_LOG = p / "api-requests.log"
