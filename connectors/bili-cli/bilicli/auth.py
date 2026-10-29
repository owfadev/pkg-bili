"""Cookie import and loading: SESSDATA/bili_jct (+buvid3), stored 0600 in ~/.bili-cli/cookies.json.

Three ways in:
  * `login --qrcode`  scan with the app (see qrlogin.py) — no copying
  * `login --cookie`  paste the browser's Cookie header (still supported, e.g. for a borrowed profile)
  * `login --file`    same string from a file

The cookie is a full-power credential — stored only in the local CONFIG_DIR,
never in any repo, never echoed in full. Session rotation (bilibili refreshes
SESSDATA via Set-Cookie) is written back by persist_session(), so a working
login keeps sliding forward instead of expiring on a fixed date.
"""

import json
import os

from .config import AUTH_KEYS, COOKIE_FILE


def parse_cookie_string(raw: str) -> dict:
    """Parse a "k=v; k2=v2" cookie string (the Request Header value copied straight from browser DevTools)."""
    jar = {}
    for part in raw.replace("\n", ";").split(";"):
        if "=" not in part:
            continue
        k, _, v = part.strip().partition("=")
        if k and v:
            jar[k] = v
    return jar


def _write_jar(jar: dict) -> None:
    """Atomic 0600 write: a half-written credentials file is worse than none."""
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = COOKIE_FILE.with_name(COOKIE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(jar, ensure_ascii=False, indent=2), encoding="utf-8")
    try:  # effective on non-Windows; best-effort on Windows
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, COOKIE_FILE)


def save_cookies(jar: dict) -> None:
    missing = [k for k in AUTH_KEYS if k not in jar]
    if missing:
        raise SystemExit(
            f"cookie is missing login keys {missing} (SESSDATA and bili_jct required) — "
            f"prefer `bili-cli login --qrcode`, or paste the browser Cookie header with --cookie")
    _write_jar(jar)


def load_cookies() -> dict:
    if not COOKIE_FILE.exists():
        raise SystemExit(
            "not logged in: run `bili-cli login --qrcode` (scan with the app) "
            "or `bili-cli login --cookie '...'`; credentials stay in ~/.bili-cli/ only")
    return json.loads(COOKIE_FILE.read_text(encoding="utf-8"))


def _bili_jar(session) -> dict:
    """Cookie-name -> value for the bilibili domains only (the session may also hold other hosts)."""
    jar = {}
    for c in session.cookies:
        if c.value and c.domain and "bilibili.com" in c.domain:
            jar[c.name] = c.value
    return jar


def persist_session(session) -> bool:
    """Write rotated cookies back to disk; returns True when something actually changed.

    bilibili slides SESSDATA's expiry forward with normal traffic. Persisting that rotation is
    what makes a QR login last for months instead of until the next restart.
    """
    jar = _bili_jar(session)
    if "SESSDATA" not in jar:  # logged out / server cleared it -> keep whatever we have
        return False
    current = {}
    if COOKIE_FILE.exists():
        try:
            current = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
    merged = {**current, **jar}
    if merged == current:
        return False
    _write_jar(merged)
    return True


def clear() -> bool:
    """Remove the stored credential (logout)."""
    try:
        COOKIE_FILE.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        raise SystemExit(f"cannot remove {COOKIE_FILE}: {e}")
