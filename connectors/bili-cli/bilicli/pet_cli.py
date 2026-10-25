"""bili-pet command surface: the autonomous-agent entry (installing the bili-pet plugin
member = standing consent for quota'd social writes; every utterance is ledgered).

Discipline aligned with dsh-bili-whale:
  * every write requires an explicit --dry or --go (refuses to run without either)
  * --dry checks everything (login, policy gates, dedup) and prints the exact request
  * --go re-checks everything, sends exactly once, then ledger + report
  * policy refusals print {ok:false,...} JSON (the pet agent reads and obeys them)
"""

import argparse
import json
import re
import sys
import time

from . import pet, petfeed, petplan
from .api import BiliAPI

_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")


def _emit(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    return code


def _fail(e) -> int:
    return _emit({"ok": False, "blocked": str(e)}, 2)


def _require_dry_go(args) -> None:
    if bool(args.dry) == bool(args.go):
        raise pet.PetError("refusing to run without an explicit --dry or --go (pick one)")


def _api(authed=True) -> BiliAPI:
    return BiliAPI(authed=authed)


# -- commands -------------------------------------------------------------------

def cmd_config(args):
    cfg = pet.load_config()
    return _emit({"ok": True, "config": cfg,
                  "note": "wiki persona page is the declaration; materialize this file from it"})


def cmd_status(args):
    cfg = pet.load_config()
    q, corrupt = pet.load_quota()
    used_share = pet._used(q, lambda x: x.get("share"))
    used_all = pet._used(q, lambda x: True)
    return _emit({"ok": True, "today": pet._today(),
                  "shares": used_share, "shares_cap": min(cfg["quota"]["shareDaily"],
                                                          pet.silence(cfg, q)["cap"]),
                  "silence": pet.silence(cfg, q), "writes_total": used_all,
                  "ledger_corrupt": corrupt, "dm": pet.dm_lock_status(cfg),
                  "plan": petplan.plan_status(),
                  "owner": cfg["owner"] or None or "(unset — DM channel unavailable)"})


def cmd_quota(args):
    cfg = pet.load_config()
    q, corrupt = pet.load_quota()
    if args.action == "status":
        return _emit({"ok": True, "today": pet._today(),
                      "used_share": pet._used(q, lambda x: x.get("share")),
                      "used_all": pet._used(q, lambda x: True),
                      "silence": pet.silence(cfg, q), "corrupt": corrupt,
                      "recent": q["log"][-5:]})
    if args.action == "check":
        try:
            return _emit({"ok": True, **pet.check_share(cfg, q, corrupt, args.target)})
        except pet.PetError as e:
            return _fail(e)
    if args.action == "record":
        pet.record(q, {"type": args.type, "target": args.target or "",
                       "note": (args.note or "")[:40], "auto": not args.manual})
        return _emit({"ok": True})
    raise pet.PetError(f"unknown quota action: {args.action}")


def _parent_author(api, aid, root, parent):
    """Who wrote the comment being replied to (reply/reply lookup; root==parent means the
    target IS the top-level comment, which rides in the same response)."""
    data = api.comment_thread_raw(aid, root) if hasattr(api, "comment_thread_raw") else None
    if not data:
        try:
            data = api._call("reply_reply", {"type": 1, "oid": aid, "root": root, "ps": 20, "pn": 1})
        except SystemExit:
            return None
    for r in (data.get("replies") or []):
        if str(r.get("rpid")) == str(parent):
            return {"mid": str((r.get("member") or {}).get("mid", "")),
                    "uname": (r.get("member") or {}).get("uname", "")}
    rt = data.get("root") or {}
    if str(rt.get("rpid")) == str(parent):
        return {"mid": str((rt.get("member") or {}).get("mid", "")),
                "uname": (rt.get("member") or {}).get("uname", "")}
    return None


def cmd_comment(args):
    _require_dry_go(args)
    cfg = pet.load_config()
    api = _api()
    aid = api._bvid_to_aid(args.bvid)
    message = args.message.strip()
    if not message:
        raise pet.PetError("--message is empty")
    if args.share and "@" not in message and cfg["share"].get("via") == "comment":
        raise pet.PetError("share comments must @ the owner (the whole point is delivering TO them)")

    share_check = None
    if args.share:
        validated = pet.validate_share(cfg, args.reason or "", args.score, args.rank)
        q, corrupt = pet.load_quota()
        share_check = pet.check_share(cfg, q, corrupt, args.bvid)
        share_check["validated"] = validated

    once_info = None
    if args.parent and (args.once_per_person or cfg["listen"].get("oncePerPerson")):
        root = args.root or args.parent       # top-level replies: root==parent
        author = _parent_author(api, aid, root, args.parent)
        if author is None:
            raise pet.PetError("cannot resolve the reply target's author — refusing (once-per-person fail-closed)")
        once_info = {"target": author}
        recent = pet.replied_recent(author["mid"], cfg["owner"]["mid"])
        if recent and args.once_per_person:
            raise pet.PetError(f"already replied to {author['uname']} within {pet.ONCE_PER_PERSON_DAYS}d "
                               f"({recent[0].get('at')}) — once-per-person refuses")

    req = {"endpoint": "x/v2/reply/add", "oid": aid, "type": 1, "message": message,
           "root": args.root, "parent": args.parent, "share": bool(args.share)}
    if args.dry:
        me = api.me() or {}
        return _emit({"ok": True, "dry": True, "account": {"mid": me.get("mid"), "uname": me.get("uname")},
                      "request": req, "share_check": share_check, "once": once_info,
                      "note": "swap --dry for --go to send exactly once (gates re-checked)"})
    data = pet.comment_add(api, aid, message, root=args.root, parent=args.parent)
    q, _ = pet.load_quota()
    pet.record(q, {"type": "comment", "target": args.bvid,
                   "note": message[:40], "auto": not args.manual,
                   **({"share": True} if args.share else {})})
    if args.parent and once_info:
        pet.replied_record(once_info["target"]["mid"], args.bvid)
    return _emit({"ok": True, "sent": req, "rpid": (data or {}).get("rpid"),
                  "report": f"posted comment rpid={(data or {}).get('rpid')} on {args.bvid}"
                            + (" — REPORT to owner (every utterance is reported)" if not args.manual else "")})


def cmd_dm(args):
    cfg = pet.load_config()
    owner_mid = str(cfg["owner"].get("mid", "")).strip()
    if args.status:
        api = _api()
        rel = pet.relation(api, owner_mid) if owner_mid else {"mutual": False, "attribute": None}
        return _emit({"ok": True, "owner_mid": owner_mid or None, "relation": rel,
                      "lock": pet.dm_lock_status(cfg), "today": pet._today()})
    _require_dry_go(args)
    msg = (args.msg or "").strip()
    if not msg:
        raise pet.PetError("--msg is empty")
    m = _BV_RE.search(msg)
    bv = m.group(0) if m else None
    if args.share:
        if not bv:
            raise pet.PetError("share DMs need a BV in the message (7-day per-video dedup keys on it)")
        pet.validate_share(cfg, args.reason or "", args.score, 1)
        q, corrupt = pet.load_quota()
        pet.check_share(cfg, q, corrupt, bv)
    pet.dm_check_lock(cfg, owner_mid)
    if args.dry:
        return _emit({"ok": True, "dry": True, "to": owner_mid, "msg_head": msg[:80],
                      "bv": bv, "note": "swap --dry for --go to send"})
    api = _api()
    out = pet.dm_send(api, owner_mid, msg)
    pet.dm_record_lock(owner_mid)
    q, _ = pet.load_quota()
    pet.record(q, {"type": "dm", "target": bv or f"dm:{owner_mid}", "note": msg[:40],
                   "auto": not args.manual, **({"share": True} if args.share else {})})
    return _emit({"ok": True, **out, "report": "DM sent to owner — ledgered" +
                  (" as share" if args.share else "")})


def cmd_relation(args):
    cfg = pet.load_config()
    fid = args.mid or cfg["owner"].get("mid")
    if not fid:
        raise pet.PetError("no mid given and owner.mid unset")
    return _emit({"ok": True, "mid": fid, **pet.relation(_api(), fid)})


def cmd_whois(args):
    d = _api().up_info(args.mid) or {}
    return _emit({"ok": True, "mid": args.mid, "name": d.get("name"), "sign": d.get("sign")})


def cmd_feed(args):
    return _emit(petfeed.run_feed(_api(), n=args.n, per=args.per, source=args.source))


def cmd_plan(args):
    cfg = pet.load_config()
    if args.complete:
        state = petplan.plan_status()
        nxt = petplan.complete_plan(state)
        petplan.plan_save(nxt)
        return _emit({"ok": True, "plan": nxt})
    state = petplan.plan_status()
    nxt = petplan.make_plan(cfg["night"], state)
    petplan.plan_save(nxt)
    return _emit({"ok": True, "plan": nxt})


def cmd_react(args):
    """Owner reaction register — resets the silence-decay clock (the pet's lifeline)."""
    fb = pet._read_json(pet.FEEDBACK_FILE, {"last": None, "events": []})
    fb["last"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    fb.setdefault("events", []).append(
        {"at": fb["last"], "kind": args.kind, "note": (args.note or "")[:80],
         **({"videoTitle": args.title} if args.title else {})})
    fb["events"] = fb["events"][-200:]
    pet._write_json(pet.FEEDBACK_FILE, fb)
    return _emit({"ok": True, "last": fb["last"],
                  "note": "silence decay reset — shares return to full cap"})


def cmd_seen(args):
    petfeed.mark_seen(args.bvid)
    return _emit({"ok": True, "bvid": args.bvid})


# -- parser ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bili-pet",
                                description="autonomous bilibili agent surface (quota'd social writes under standing consent; writes need --dry|--go)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("config", help="effective config (materialized from the wiki persona page)")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("status", help="one glance: shares/silence/dm-lock/plan")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("quota", help="ledger: status / check / record")
    s.add_argument("action", choices=["status", "check", "record"])
    s.add_argument("--target", help="bvid for the 7-day dedup check")
    s.add_argument("--type", default="comment")
    s.add_argument("--note", default="")
    s.add_argument("--manual", action="store_true", help="mark as owner-authorized (not autonomous)")
    s.set_defaults(fn=cmd_quota)

    s = sub.add_parser("comment", help="post a comment / reply (write: --dry or --go)")
    s.add_argument("bvid")
    s.add_argument("--message", required=True)
    s.add_argument("--root", help="thread root rpid (floor replies)")
    s.add_argument("--parent", help="rpid being replied to")
    s.add_argument("--share", action="store_true", help="counts against the share budget (<=3/day, 7d/video)")
    s.add_argument("--reason", help="why it is worth the owner's attention (>= 8 chars, share only)")
    s.add_argument("--score", type=int, help="debrief scorecard 0-20 (share only)")
    s.add_argument("--rank", type=int, default=1)
    s.add_argument("--once-per-person", action="store_true",
                   help="refuse if this person was replied to within 30 days (owner exempt)")
    s.add_argument("--manual", action="store_true", help="owner-authorized in-session (still ledgered)")
    s.add_argument("--dry", action="store_true")
    s.add_argument("--go", action="store_true")
    s.set_defaults(fn=cmd_comment)

    s = sub.add_parser("dm", help="DM the owner ONLY (mutual follow, one recipient/day)")
    s.add_argument("--msg")
    s.add_argument("--status", action="store_true")
    s.add_argument("--share", action="store_true")
    s.add_argument("--reason", default="")
    s.add_argument("--score", type=int)
    s.add_argument("--manual", action="store_true")
    s.add_argument("--dry", action="store_true")
    s.add_argument("--go", action="store_true")
    s.set_defaults(fn=cmd_dm)

    s = sub.add_parser("relation", help="relation with a mid (default: owner) — mutual check")
    s.add_argument("--mid")
    s.set_defaults(fn=cmd_relation)

    s = sub.add_parser("whois", help="mid -> name (verify before @-mentioning)")
    s.add_argument("mid", type=int)
    s.set_defaults(fn=cmd_whois)

    s = sub.add_parser("feed", help="candidate selection (prefs + popular + rank + following)")
    s.add_argument("--n", type=int, default=5)
    s.add_argument("--per", type=int, default=2, help="per preference-combo cap")
    s.add_argument("--source", choices=["all", "search", "popular", "rank", "following"], default="all")
    s.set_defaults(fn=cmd_feed)

    s = sub.add_parser("plan", help="biological clock: roll tonight's plan / mark complete")
    s.add_argument("--complete", action="store_true")
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("react", help="register an owner reaction (resets silence decay)")
    s.add_argument("--kind", default="reaction")
    s.add_argument("--note", default="")
    s.add_argument("--title", default="", help="video title when reacting to a share (taste learning)")
    s.set_defaults(fn=cmd_react)

    s = sub.add_parser("seen", help="mark a bvid watched (feed exclusion)")
    s.add_argument("bvid")
    s.set_defaults(fn=cmd_seen)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except pet.PetError as e:
        return _fail(e)
    except SystemExit as e:
        return _fail(e)


if __name__ == "__main__":
    sys.exit(main())
