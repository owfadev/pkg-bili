"""Restricted direct messages: one recipient at a time, mutual-follow by default, capped, ledgered.

This is the *only* DM surface the connector offers, and the walls are code, not prompts:

  * mutual follow required (override is explicit: --allow-non-mutual, and it is recorded)
  * 5 messages / rolling 24h, 1 per recipient / 24h, 60 s between any two sends
  * 450 characters max, no sending to yourself, no duplicate to the same recipient
  * every attempt (sent or refused) lands in ~/.bili-cli/dm-ledger.json, 0600

The CLI adds the second gate: --yes. The agent layer adds the first: an explicit user verb.
"""

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from . import traffic
from .config import API_BASE, CONFIG_DIR

DM_MAX_PER_DAY = 5
DM_COOLDOWN_H = 24
DM_MIN_GAP_S = 60
DM_MAX_CHARS = 450
LEDGER_MAX = 500

LEDGER_FILE = CONFIG_DIR / "dm-ledger.json"
DEVICE_FILE = CONFIG_DIR / "dm-device-id"


class DMPolicyError(RuntimeError):
    """A guard refused the send — the message is user-facing."""


# --------------------------------------------------------------------------- ledger

def _read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def load_ledger() -> list:
    rows = _read_json(LEDGER_FILE, [])
    return rows if isinstance(rows, list) else []


def record(entry: dict) -> dict:
    rows = [r for r in load_ledger() if isinstance(r, dict)]
    rows.append(entry)
    _write_json(LEDGER_FILE, rows[-LEDGER_MAX:])
    return entry


def _recent(rows: list, now: float, hours: float) -> list:
    cut = now - hours * 3600
    return [r for r in rows if isinstance(r.get("ts"), (int, float)) and r["ts"] >= cut]


# --------------------------------------------------------------------------- guards

def _fmt_gap(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)} 秒"
    if seconds < 5400:
        return f"{int(seconds // 60)} 分钟"
    return f"{seconds / 3600:.1f} 小时"


def check(ledger: list, mid: int, text: str, *, me_mid=None, now: float | None = None) -> dict:
    """Run every offline guard; returns a summary for the dry-run, raises DMPolicyError otherwise."""
    now = time.time() if now is None else now
    text = (text or "").strip()
    if not text:
        raise DMPolicyError("消息不能为空")
    if len(text) > DM_MAX_CHARS:
        raise DMPolicyError(f"消息太长（{len(text)} > {DM_MAX_CHARS} 字）")
    if me_mid and str(mid) == str(me_mid):
        raise DMPolicyError("不能给自己发私信")

    last24 = _recent(ledger, now, 24)
    if len(last24) >= DM_MAX_PER_DAY:
        oldest = min(float(r["ts"]) for r in last24)
        wait = 24 * 3600 - (now - oldest)
        raise DMPolicyError(
            f"24 小时上限 {DM_MAX_PER_DAY} 条已用完（{_fmt_gap(wait)}后可再发）")

    mine = [r for r in last24 if str(r.get("mid")) == str(mid)]
    if mine:
        wait = DM_COOLDOWN_H * 3600 - (now - float(mine[-1]["ts"]))
        raise DMPolicyError(
            f"同一收件人 {DM_COOLDOWN_H}h 内只能发一条（{_fmt_gap(max(wait, 0))}后可再发）")

    stamped = [float(r["ts"]) for r in ledger if isinstance(r.get("ts"), (int, float))]
    if stamped:
        last = max(stamped)
        if now - last < DM_MIN_GAP_S:
            raise DMPolicyError(
                f"两条私信至少间隔 {DM_MIN_GAP_S}s（还剩 {_fmt_gap(DM_MIN_GAP_S - (now - last))}）")

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    return {
        "chars": len(text),
        "max_chars": DM_MAX_CHARS,
        "sent_24h": len(last24),
        "remaining_24h": DM_MAX_PER_DAY - len(last24) - 1,
        "cooldown_h": DM_COOLDOWN_H,
        "sha256": digest[:12],
    }


# --------------------------------------------------------------------------- recipient

def _device_id() -> str:
    try:
        v = DEVICE_FILE.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"WEB-[0-9A-F-]{20,}", v, re.I):
            return v
    except OSError:
        pass
    v = "WEB-" + str(uuid.uuid4()).upper()
    DEVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEVICE_FILE.write_text(v, encoding="utf-8")  # plain text: the read side regexes it verbatim
    try:
        os.chmod(DEVICE_FILE, 0o600)
    except OSError:
        pass
    return v


def resolve(api, target: str) -> dict:
    """mid or @name -> {mid, name}. Names must match exactly and unambiguously."""
    target = (target or "").strip()
    if target.isdigit():
        return {"mid": int(target), "name": None}
    name = target.lstrip("@").strip()
    if not name:
        raise DMPolicyError("收件人不能为空（用 mid 或 @昵称）")
    rows = api.search(name, kind="up", limit=10) or []
    exact = [r for r in rows if (r.get("uname") or "") == name]
    if not exact:
        hint = "、".join(r.get("uname", "?") for r in rows[:5]) or "无结果"
        raise DMPolicyError(f"找不到昵称为「{name}」的用户（候选：{hint}）")
    if len(exact) > 1:
        raise DMPolicyError(f"昵称「{name}」有 {len(exact)} 个同名用户，请改用 mid")
    return {"mid": int(exact[0]["mid"]), "name": exact[0]["uname"]}


def relation(api, mid: int) -> dict:
    d = api._call("relation", {"fid": mid}) or {}
    be = (d.get("be_relation") or {}).get("attribute")
    return {"attribute": d.get("attribute"), "be_attribute": be,
            "mutual": d.get("attribute") == 6 and be == 6}


# --------------------------------------------------------------------------- send

def send(api, mid: int, text: str) -> dict:
    """POST x/im/dm/sendMsg (web body shape) — the CLI runs every guard before calling this."""
    me = api.me() or {}
    form = {
        "msg[sender_uid]": str(me.get("mid")),
        "msg[receiver_id]": str(mid),
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
    traffic.throttle()
    r = api.s.post(f"{API_BASE}/x/im/dm/sendMsg", data=form,
                   headers={"Referer": "https://message.bilibili.com/"}, timeout=20)
    try:
        j = r.json()
    except ValueError:
        raise DMPolicyError(f"非 JSON 响应（HTTP {r.status_code}）")
    blocked = traffic.classify(api.s, f"{API_BASE}/x/im/dm/sendMsg", r.status_code, j)
    if blocked is not None:
        raise DMPolicyError(str(blocked))
    if j.get("code") != 0:
        raise DMPolicyError(f"发送失败 code={j.get('code')}: {j.get('message')}")
    return j.get("data") or {}


def entry(mid: int, text: str, *, ok: bool, mutual: bool, forced: bool,
          code=None, me_mid=None, name=None) -> dict:
    return {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": time.time(),
        "mid": str(mid),
        "name": name,
        "me": str(me_mid) if me_mid else None,
        "chars": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "preview": text[:80],
        "mutual": mutual,
        "forced": forced,
        "ok": ok,
        "code": code,
    }


def status(now: float | None = None) -> dict:
    """Today's budget and the last few entries — safe to call without --yes."""
    now = time.time() if now is None else now
    rows = load_ledger()
    last24 = _recent(rows, now, 24)
    return {
        "sent_24h": len(last24),
        "cap_24h": DM_MAX_PER_DAY,
        "remaining_24h": max(0, DM_MAX_PER_DAY - len(last24)),
        "cooldown_h": DM_COOLDOWN_H,
        "min_gap_s": DM_MIN_GAP_S,
        "max_chars": DM_MAX_CHARS,
        "recipients_24h": sorted({r.get("mid") for r in last24}),
        "recent": [{k: r.get(k) for k in ("at", "mid", "name", "ok", "chars", "preview")}
                   for r in rows[-5:]],
    }
