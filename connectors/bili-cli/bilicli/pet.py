"""bili-pet: the autonomous-agent write surface — policy gates + ledgered writes.

Faithful port of dsh-bili-whale's policy layer (share-policy.mjs / quota.mjs / dm.mjs /
comment.mjs, MIT, Alterssss), adapted to this connector's home: one credential store
(~/.bili-cli/), JSON single-line output, and a standing-consent contract instead of a
settings page.  Alignment decisions (whale's own, kept verbatim):

  * hard caps are code, not knobs: shares <= shareDaily/day (default 3), one share per
    video per 7 days, replies at most once per person per 30 days (owner exempt)
  * share validation: enabled flag, --reason >= 8 chars, --score 0-20 >= minScore (13),
    rank 1 only — comment @ and DM share ONE budget (dedup key = the BV)
  * silence decay: no owner reaction for silentAfterDays(3) -> cap decays linearly to
    0 at silentStopDays(7); diaries are never gated, only shares
  * the ledger is fail-closed: a corrupt quota.json means "state unknown", refuse to post
  * every write refuses to run without an explicit --dry or --go
  * DM hard limits (not overridable): owner mid only, mutual follow required, one
    recipient per day (unlimited messages to that one)

Config lives in ~/.bili-cli/pet.json (owner/self/share/quota/preferences/night/notify);
the wiki persona page is the declaration — the pet skill materializes this file from it.
State lives in ~/.bili-cli/pet/ (quota.json / replied.json / dm-lock.json / feedback.json).
"""

import json
import re
import time
import uuid
from pathlib import Path

from .api import BiliAPI
from .config import API_BASE, CONFIG_DIR

PET_DIR = CONFIG_DIR / "pet"
PET_CONFIG = CONFIG_DIR / "pet.json"
QUOTA_FILE = PET_DIR / "quota.json"
REPLIED_FILE = PET_DIR / "replied.json"
DM_LOCK_FILE = PET_DIR / "dm-lock.json"
FEEDBACK_FILE = PET_DIR / "feedback.json"

ONCE_PER_PERSON_DAYS = 30
SHARE_DEDUP_MS = 7 * 24 * 3600 * 1000
MESSAGE_MAX = 1000

DEFAULTS = {
    "owner": {"mid": "", "name": ""},
    "self": {"mid": "", "name": ""},
    "share": {"enabled": True, "via": "comment", "minScore": 13,
              "silentAfterDays": 3, "silentStopDays": 7},
    "quota": {"shareDaily": 3},
    "listen": {"oncePerPerson": True},
    "preferences": {"like": [], "dislike": [], "keywords": []},
    "night": {"windows": [{"start": "20:00", "end": "24:00", "wakes": 1}],
              "skipChance": 0.3, "minGapMin": 10, "maxVideos": 5},
    "notify": {"type": "none", "url": "", "cmd": ""},
}


class PetError(RuntimeError):
    """Policy refusal / write failure — message is user-facing."""


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base.get(k, {}), v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config() -> dict:
    try:
        user = json.loads(PET_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        user = {}
    return _merge(DEFAULTS, user)


def _read_json(path: Path, fallback):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else fallback


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def load_quota():
    """Returns (quota, corrupt). Corrupt ledger = state unknown = refuse to post (fail-closed)."""
    try:
        q = _read_json(QUOTA_FILE, {"log": [], "shareDaily": None})
        if not isinstance(q.get("log"), list):
            return {"log": []}, True
        return q, False
    except ValueError:
        return {"log": []}, True


def save_quota(q) -> None:
    _write_json(QUOTA_FILE, q)


def _today() -> str:
    return time.strftime("%Y-%m-%d")            # local date (whale's UTC/local fix kept in mind)


def _day_of(at: str) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(time.mktime(time.strptime(at[:19], "%Y-%m-%dT%H:%M:%S"))))
    except ValueError:
        return ""


def _used(q, pred) -> int:
    return sum(1 for x in q["log"] if _day_of(x.get("at", "")) == _today() and pred(x))


def silence(cfg, q) -> dict:
    """Owner-silence decay: from the last owner reaction (feedback.json), else the first
    share ever. day<3 full cap, 3..6 linear down to 1, >=7 zero. Diaries never gated."""
    fb = _read_json(FEEDBACK_FILE, {"last": None, "events": []})
    base = fb.get("last")
    if not base:
        base = next((x["at"] for x in q["log"] if x.get("share")), None)
    try:
        days = int((time.time() * 1000 - _parse_iso(base)) // 86_400_000) if base else 0
    except (ValueError, TypeError):
        days = 0
    after = int(cfg["share"].get("silentAfterDays", 3))
    stop_at = max(after + 1, int(cfg["share"].get("silentStopDays", 7)))
    base_cap = int(cfg["quota"].get("shareDaily", 3))
    if days < after:
        cap = base_cap
    elif days >= stop_at:
        cap = 0
    else:
        cap = max(1, base_cap * (stop_at - days) // (stop_at - after))
    return {"days": days, "cap": cap, "penalty": base_cap - cap,
            "band": "normal" if days < after else "quiet" if days < stop_at else "faint"}


def _parse_iso(s) -> float:
    return time.mktime(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")) * 1000


def validate_share(cfg, reason, score, rank=1) -> dict:
    """Both comment-@ and DM sharing obey the same settings (whale share-policy.mjs)."""
    if cfg["share"].get("enabled") is False:
        raise PetError("sharing disabled (share.enabled=false)")
    if not isinstance(reason, str) or len(reason.strip()) < 8:
        raise PetError("sharing needs --reason (>= 8 chars) — say WHY it is worth the owner's attention")
    try:
        n = int(score)
    except (TypeError, ValueError):
        raise PetError("sharing needs --score 0-20 (the debrief scorecard)")
    if not 0 <= n <= 20:
        raise PetError("score must be 0-20")
    if n < int(cfg["share"].get("minScore", 13)):
        raise PetError(f"score {n} below threshold {cfg['share'].get('minScore', 13)} — today has nothing worth sharing")
    if rank != 1:
        raise PetError("only --rank 1 may be shared (the single best of the day)")
    return {"score": n, "reason": reason.strip(), "rank": 1}


def check_share(cfg, q, corrupt, target_bv=None) -> dict:
    """Share gate: daily cap x silence decay + 7-day per-video dedup. Fail-closed."""
    if corrupt:
        raise PetError(f"quota ledger corrupt ({QUOTA_FILE}) — state unknown, refusing to post; "
                       "fix or delete it (delete = today's budget restarts at 0)")
    if cfg["share"].get("enabled") is False:
        raise PetError("sharing disabled")
    used = _used(q, lambda x: x.get("share"))
    sil = silence(cfg, q)
    cap = min(int(cfg["quota"].get("shareDaily", 3)), sil["cap"])
    if used >= cap:
        raise PetError(f"share budget used: {used} today (effective cap {cap}"
                       + (f", silence decay {sil['days']}d -> cap {sil['cap']}" if sil["penalty"] else "")
                       + ") — the rest is for tomorrow")
    if target_bv:
        now = time.time() * 1000
        for x in q["log"]:
            if (x.get("share") and str(x.get("target", "")) == target_bv
                    and now - _parse_iso(x.get("at", "")) < SHARE_DEDUP_MS):
                raise PetError(f"this video was already shared within 7 days ({x.get('at')} {target_bv})")
    return {"used": used, "cap": cap, "silence": sil}


def record(q, entry: dict) -> None:
    q["log"].append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry})
    q["log"] = q["log"][-500:]
    save_quota(q)


# -- once-per-person (reply dedup; owner exempt — replying to the owner must never lock them out)

def replied_recent(target_uid, owner_mid) -> list:
    if str(target_uid) == str(owner_mid):
        return []                       # owner never locks out (whale 2026-10-22 fix)
    rep = _read_json(REPLIED_FILE, {"byUid": {}})
    now = time.time() * 1000
    out = []
    for x in rep.get("byUid", {}).get(str(target_uid), []):
        if now - _parse_iso(x.get("at", "")) < ONCE_PER_PERSON_DAYS * 86_400_000:
            out.append(x)
    return out


def replied_record(target_uid, target) -> None:
    rep = _read_json(REPLIED_FILE, {"byUid": {}})
    rep.setdefault("byUid", {}).setdefault(str(target_uid), []).append(
        {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "target": target})
    _write_json(REPLIED_FILE, rep)


# -- DM hard limits (three, not overridable — riskiest interface on the platform)

def dm_lock_status(cfg) -> dict:
    lock = _read_json(DM_LOCK_FILE, {})
    return {"today": _today(), "locked_to": lock.get("date") == _today() and lock.get("mid"),
            "raw": lock}


def dm_check_lock(cfg, owner_mid) -> None:
    if not owner_mid:
        raise PetError("owner.mid not configured — DM refuses to send")
    lock = _read_json(DM_LOCK_FILE, {})
    if lock.get("date") == _today() and str(lock.get("mid")) != str(owner_mid):
        raise PetError(f"one recipient per day (already locked to mid {lock.get('mid')}) — "
                       "unlimited messages to that one, nobody else today")


def dm_record_lock(owner_mid) -> None:
    _write_json(DM_LOCK_FILE, {"date": _today(), "mid": str(owner_mid)})


def _device_id() -> str:
    f = PET_DIR / "dm-device-id"
    try:
        v = f.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"WEB-[0-9A-F-]{20,}", v, re.I):
            return v
    except OSError:
        pass
    v = "WEB-" + str(uuid.uuid4()).upper()
    _write_json(f, v)
    return v


# -- write operations (custom _call shapes: signature on the query string, form in the body)

def comment_add(api: BiliAPI, oid, message, *, type_id=1, root=None, parent=None):
    """POST x/v2/reply/add — WBI signature rides the query, csrf rides the body (whale PR#25 fix)."""
    if not api.authed:
        raise PetError("comment write needs login (bili-cli login first)")
    if len(message) > MESSAGE_MAX:
        raise PetError(f"message too long ({len(message)} > {MESSAGE_MAX})")
    params = {"type": str(type_id), "oid": str(oid), "message": message, "plat": "1"}
    if root:
        params["root"] = str(root)
    if parent:
        params["parent"] = str(parent)
    signed = api._sign(dict(params))
    query = {"w_rid": signed["w_rid"], "wts": signed["wts"]}   # signature on the query string,
    body = {**params, "csrf": api.csrf, "csrf_token": api.csrf}  # csrf in the body (whale PR#25 fix)
    from . import traffic
    traffic.throttle()
    r = api.s.post(f"{API_BASE}/x/v2/reply/add", params=query, data=body,
                   headers={"Referer": "https://www.bilibili.com/"}, timeout=20)
    try:
        j = r.json()
    except ValueError:
        blocked = traffic.classify(api.s, f"{API_BASE}/x/v2/reply/add", r.status_code, None)
        raise PetError(blocked.args[0] if blocked else f"non-JSON response (HTTP {r.status_code})")
    blocked = traffic.classify(api.s, f"{API_BASE}/x/v2/reply/add", r.status_code, j)
    if blocked is not None:
        raise PetError(str(blocked))
    if j.get("code") != 0:
        raise PetError(f"reply/add code={j.get('code')}: {j.get('message')}")
    return j.get("data") or {}


def relation(api: BiliAPI, fid) -> dict:
    d = api._call("relation", {"fid": fid})
    return {"attribute": d.get("attribute"),
            "be_attribute": (d.get("be_relation") or {}).get("attribute"),
            "mutual": d.get("attribute") == 6 and (d.get("be_relation") or {}).get("attribute") == 6}


def dm_send(api: BiliAPI, owner_mid, text) -> dict:
    """POST x/im/dm/sendMsg — the whale body shape. Owner-only + mutual + one-recipient/day
    are enforced by the CLI layer BEFORE this."""
    if not api.authed:
        raise PetError("DM write needs login (bili-cli login first)")
    me = api.me() or {}
    if not me.get("isLogin"):
        raise PetError("cookie expired (nav reports logged out) — re-login before DM")
    rel = relation(api, owner_mid)
    if not rel["mutual"]:
        raise PetError(f"mutual follow required (attribute={rel['attribute']}, "
                       f"be={rel['be_attribute']}) — set it up manually in both accounts first")
    form = {
        "msg[sender_uid]": str(me.get("mid")),
        "msg[receiver_id]": str(owner_mid),
        "msg[receiver_type]": "1",
        "msg[content]": json.dumps({"content": text, "msgType": 1}, ensure_ascii=False),
        "msg[timestamp]": str(int(time.time() * 1000)),
        "msg[new_device]": "1",
        "msg[dev_id]": _device_id(),
        "from_firework": "0",
        "build": "0",
        "mobi_app": "web",
        "csrf": api.csrf,
        "csrf_token": api.csrf,
    }
    from . import traffic
    traffic.throttle()
    r = api.s.post(f"{API_BASE}/x/im/dm/sendMsg", data=form,
                   headers={"Referer": "https://message.bilibili.com/"}, timeout=20)
    try:
        j = r.json()
    except ValueError:
        raise PetError(f"non-JSON response (HTTP {r.status_code})")
    blocked = traffic.classify(api.s, f"{API_BASE}/x/im/dm/sendMsg", r.status_code, j)
    if blocked is not None:
        raise PetError(str(blocked))
    if j.get("code") != 0:
        raise PetError(f"dm/sendMsg code={j.get('code')}: {j.get('message')}")
    return {"ok": True, "to_mid": str(owner_mid)}
