"""bili-cli command surface: argparse subcommands, output always single-line JSON.

Read = query-and-answer (distilled fields, not full passthrough); write = low-risk whitelist (watchlater/fav/like),
all requiring the explicit --yes gate — the agent layer adds its own "explicit user verb" discipline:
a double gate against accidental writes.
"""

import argparse
import json
import sys
import time

from . import __version__, auth
from .api import BiliAPI


def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def _ts(v):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(v)) if v else None


def _need_yes(args, action: str) -> None:
    if not getattr(args, "yes", False):
        print(f"write operation {action} requires --yes (the agent layer additionally requires an explicit user verb)", file=sys.stderr)
        raise SystemExit(2)


def _report_login() -> dict:
    """Verify the stored credential against nav and report who we are."""
    api = BiliAPI()
    me = api.me() or {}
    return {"ok": bool(me.get("isLogin")), "mid": me.get("mid"), "uname": me.get("uname"),
            "is_login": me.get("isLogin"), "stored": str(auth.COOKIE_FILE)}


def cmd_login(args):
    if getattr(args, "qrcode", False):
        from . import qrlogin
        try:
            jar = qrlogin.login(
                timeout_s=args.timeout,
                png_path=None if args.no_png else (args.png or qrlogin.QR_PNG),
                ascii_qr=args.ascii,
                keep_qr=args.keep_qr,
                json_events=args.json_events,
            )
        except KeyboardInterrupt:
            print("[login] 已取消", file=sys.stderr)
            raise SystemExit(130)
        auth.save_cookies(jar)
        out = _report_login()
        if not out.get("ok"):
            print("[login] 凭证已写入，但 nav 校验未通过（风控或已失效）——重试 --qrcode", file=sys.stderr)
            raise SystemExit(1)
        _emit(out)
        return
    if args.file or args.cookie:
        jar = auth.parse_cookie_string(args.file.read()) if args.file \
            else auth.parse_cookie_string(args.cookie)
        auth.save_cookies(jar)
        _emit(_report_login())
        return
    # no import requested: report state instead of guessing
    try:
        auth.load_cookies()
    except SystemExit:
        _emit({"ok": False, "logged_in": False,
               "hint": "bili-cli login --qrcode  (scan with the bilibili app)"})
        return
    out = _report_login()
    out["logged_in"] = out.pop("is_login")
    if not out["logged_in"]:
        out["hint"] = "bili-cli login --qrcode  (scan with the bilibili app)"
    _emit(out)


def cmd_logout(args):
    _emit({"ok": True, "removed": auth.clear(), "stored": str(auth.COOKIE_FILE)})


def cmd_dm(args):
    """Restricted DM: guards first (dm.py), --dry preview, --yes to send, everything ledgered."""
    from . import dm
    api = BiliAPI(); api.require_auth("dm")
    if args.status:
        _emit(dm.status())
        return
    if not args.to or not args.msg:
        raise SystemExit("dm needs --to <mid|@nickname> --msg <text>  (or --status)")
    _need_yes(args, "dm send") if not args.dry else None
    try:
        who = dm.resolve(api, args.to)
        me = api.me() or {}
        info = dm.check(dm.load_ledger(), who["mid"], args.msg, me_mid=me.get("mid"))
        rel = dm.relation(api, who["mid"])
        if not rel["mutual"] and not args.allow_non_mutual:
            raise dm.DMPolicyError(
                f"未互关（自己 attribute={rel['attribute']}，对方 be={rel['be_attribute']}）——"
                f"确需发送加 --allow-non-mutual（会记入台账）")
        payload = {"to": who, "text": args.msg, **info,
                   "mutual": rel["mutual"], "forced": bool(args.allow_non_mutual)}
        if args.dry:
            _emit({**payload, "ok": True, "sent": False, "dry_run": True})
            return
        dm.send(api, who["mid"], args.msg)
        dm.record(dm.entry(who["mid"], args.msg, ok=True, mutual=rel["mutual"],
                           forced=bool(args.allow_non_mutual), me_mid=me.get("mid"),
                           name=who.get("name")))
    except dm.DMPolicyError as e:
        print(f"[dm] {e}", file=sys.stderr)
        raise SystemExit(1)
    _emit({**payload, "ok": True, "sent": True})


def cmd_me(args):
    api = BiliAPI(); api.require_auth("me")
    d = api.me() or {}
    vip = d.get("vip") or {}
    _emit({
        "mid": d.get("mid"), "uname": d.get("uname"),
        "level": (d.get("level_info") or {}).get("current_level"),
        "coins": d.get("coins"),
        "vip": {"status": vip.get("status"), "due": _ts(vip.get("due_date"))},
        "following": d.get("following"), "follower": d.get("follower"),
    })


def cmd_watchlater(args):
    api = BiliAPI(); api.require_auth("watchlater")
    if args.action == "list":
        rows = (api.watchlater() or {}).get("list") or []
        _emit([{
            "bvid": r.get("bvid"), "title": r.get("title"),
            "owner": r.get("owner", {}).get("name"),
            "added_at": _ts(r.get("add_at")), "duration_s": r.get("duration"),
        } for r in rows])
        return
    _need_yes(args, args.action)
    fn = {"add": api.watchlater_add, "remove": api.watchlater_del}[args.action]
    _emit({**fn(args.bvid), "action": f"watchlater {args.action}", "bvid": args.bvid})


def cmd_fav(args):
    api = BiliAPI(); api.require_auth("fav")
    if not args.action or args.action == "list":
        if args.fid:
            rows = api.fav_list(args.fid, args.limit) or []
            _emit([{
                "bvid": r.get("bvid"), "title": r.get("title"),
                "upper": (r.get("upper") or {}).get("name"),
                "duration_s": r.get("duration"), "fav_time": _ts(r.get("fav_time")),
            } for r in rows])
        else:
            rows = api.fav_folders() or []
            _emit([{
                "fid": r.get("id"), "title": r.get("title"),
                "count": r.get("media_count"), "fav_time": _ts(r.get("fav_time")),
            } for r in rows])
        return
    _need_yes(args, args.action)
    if args.action == "move":
        if not (args.src_fid and args.dst_fid):
            raise SystemExit("fav move requires --from <source fid> --to <target fid> (run `bili-cli fav` first to list folders)")
        _emit({**api.fav_move(args.bvid, args.src_fid, args.dst_fid), "action": "fav move",
               "bvid": args.bvid, "from": args.src_fid, "to": args.dst_fid})
        return
    if not args.fid:
        raise SystemExit("--fid is required (favorites folder id; run `bili-cli fav` first to list folders)")
    fn = {"add": api.fav_add, "remove": api.fav_del}[args.action]
    _emit({**fn(args.bvid, args.fid), "action": f"fav {args.action}",
           "bvid": args.bvid, "fid": args.fid})


def cmd_history(args):
    api = BiliAPI(); api.require_auth("history")
    rows = api.history(args.limit)
    _emit([{
        "bvid": r.get("bvid"), "title": r.get("title"),
        "owner": (r.get("owner") or {}).get("name"),
        "progress_s": r.get("progress"), "duration_s": r.get("duration"),
        "view_at": _ts(r.get("view_at")),
    } for r in rows])


def cmd_search(args):
    import re
    clean = lambda t: re.sub(r"<[^>]+>", "", t or "")  # strip search-highlight <em> tags
    rows = BiliAPI().search(args.query, args.kind, args.limit)
    if args.kind == "up":
        _emit([{
            "mid": r.get("mid"), "name": clean(r.get("uname")), "fans": r.get("fans"),
            "sign": clean(r.get("usign")), "videos": r.get("videos"),
        } for r in rows])
    else:
        _emit([{
            "bvid": r.get("bvid"), "title": clean(r.get("title")),
            "author": clean(r.get("author")), "play": r.get("play"),
            "duration": r.get("duration"), "pubdate": _ts(r.get("pubdate")),
        } for r in rows])


def cmd_video(args):
    d = BiliAPI().video(args.bvid) or {}
    stat, pages = d.get("stat") or {}, d.get("pages") or []
    _emit({
        "bvid": d.get("bvid"), "aid": d.get("aid"), "title": d.get("title"),
        "owner": {"mid": (d.get("owner") or {}).get("mid"),
                  "name": (d.get("owner") or {}).get("name")},
        "pubdate": _ts(d.get("pubdate")), "duration_s": d.get("duration"),
        "stat": {k: stat.get(k) for k in ("view", "danmaku", "reply", "favorite", "coin", "like")},
        "pages": [{"page": p.get("page"), "part": p.get("part"), "cid": p.get("cid"),
                   "duration_s": p.get("duration")} for p in pages],
        "desc_head": (d.get("desc") or "")[:200],
    })


def cmd_subtitle(args):
    _emit(BiliAPI().subtitle(args.bvid, args.page, ai=args.ai))


def cmd_summary(args):
    api = BiliAPI(); api.require_auth("summary")  # measured -101: the official summary is only available logged in
    _emit(api.summary(args.bvid))


def cmd_up(args):
    api = BiliAPI()
    d = api.up_info(args.mid) or {}
    out = {
        "mid": d.get("mid"), "name": d.get("name"), "sign": d.get("sign"),
        "fans": d.get("follower"), "videos": d.get("archive_count"),
        "joined": _ts(d.get("jointime")),
    }
    if args.arcs:
        out["arcs"] = [{
            "bvid": v.get("bvid"), "title": v.get("title"),
            "created": _ts(v.get("created")), "play": v.get("play"),
        } for v in api.up_arcs(args.mid, args.limit)]
    _emit(out)


def cmd_like(args):
    _need_yes(args, "like")
    api = BiliAPI(); api.require_auth("like")
    _emit({**api.like(args.bvid), "action": "like", "bvid": args.bvid})


def cmd_raw(args):
    _emit(BiliAPI().raw(args.url, post=args.post))


def _comment_row(r: dict, tag: str | None = None) -> dict:
    import re
    return {
        "rpid": r.get("rpid"), "user": (r.get("member") or {}).get("uname"),
        "mid": (r.get("member") or {}).get("mid"), "like": r.get("like"),
        "replies": r.get("rcount"), "time": _ts(r.get("ctime")),
        "text": re.sub(r"\s+", " ", (r.get("content") or {}).get("message", ""))[:200],
        **({"tag": tag} if tag else {}),
    }


def cmd_comments(args):
    api = BiliAPI()
    data = api.comments(args.bvid, ps=args.ps, pn=args.pn, sort=args.sort) or {}
    items = []
    if isinstance(data.get("top"), list):
        items += [_comment_row(r, "top") for r in data["top"]]
    if (data.get("upper") or {}).get("top"):
        items.append(_comment_row(data["upper"]["top"], "upper-top"))
    items += [_comment_row(r) for r in (data.get("replies") or [])]
    _emit({
        "bvid": args.bvid, "total": (data.get("page") or {}).get("count"),
        "sort": args.sort, "page": args.pn, "count": len(items), "items": items,
    })


def cmd_thread(args):
    api = BiliAPI()
    data = api.comment_thread(args.bvid, args.root, pn=args.pn) or {}
    root = _comment_row(data.get("root") or {})
    replies = [_comment_row(r) for r in (data.get("replies") or [])]
    _emit({"bvid": args.bvid, "root": root, "page": args.pn,
           "total": (data.get("page") or {}).get("count"), "replies": replies})


def cmd_debrief(args):
    from . import debrief
    api = BiliAPI()
    manifest = debrief.run_debrief(
        api, args.bvid, grid=args.grid, tile=args.tile, top=args.top,
        bucket_s=args.bucket, n_comments=args.comments,
        want_frames=not args.no_frames, uniform=args.uniform,
        substrip_every=args.substrip, out_dir=args.out)
    _emit(manifest)


def cmd_traffic(args):
    from . import traffic
    _emit(traffic.api_state())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bili-cli",
                                description="bilibili read-mostly connector (web cookie; write commands require --yes)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="QR scan (default path) or browser-cookie import; no args = status")
    s.add_argument("--qrcode", action="store_true",
                   help="scan a QR with the bilibili app (writes a PNG to ~/.bili-cli/login-qr.png)")
    s.add_argument("--timeout", type=int, default=180, help="QR wait timeout in seconds (default 180)")
    s.add_argument("--png", help="QR PNG path (default ~/.bili-cli/login-qr.png)")
    s.add_argument("--no-png", action="store_true", help="do not write a PNG (terminal/ASCII only)")
    s.add_argument("--keep-qr", action="store_true", help="keep the QR PNG after a successful login")
    s.add_argument("--ascii", dest="ascii", action="store_true", default=None,
                   help="force terminal QR rendering (default: only when stderr is a TTY)")
    s.add_argument("--no-ascii", dest="ascii", action="store_false", help="never render the QR in the terminal")
    s.add_argument("--json-events", action="store_true",
                   help="emit NDJSON progress events on stderr (for agent layers)")
    s.add_argument("--cookie", help='instead of QR: cookie string, e.g. "SESSDATA=..; bili_jct=..; buvid3=.."')
    s.add_argument("--file", type=argparse.FileType("r", encoding="utf-8"),
                   help="or read a same-format string from a file")
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("logout", help="delete the stored credential (~/.bili-cli/cookies.json)")
    s.set_defaults(fn=cmd_logout)

    s = sub.add_parser("dm", help="restricted direct message (mutual follow by default, capped, ledgered)")
    s.add_argument("--to", help="recipient: mid or @nickname")
    s.add_argument("--msg", help="message text (<=450 chars)")
    s.add_argument("--dry", action="store_true", help="run the guards and print the exact payload; send nothing")
    s.add_argument("--status", action="store_true", help="today's budget + the last few entries")
    s.add_argument("--allow-non-mutual", action="store_true",
                   help="send without mutual follow (recorded as forced in the ledger)")
    s.add_argument("--yes", action="store_true", help="explicit confirmation gate for the actual send")
    s.set_defaults(fn=cmd_dm)

    s = sub.add_parser("me", help="my info (mid/name/level/VIP)")
    s.set_defaults(fn=cmd_me)

    s = sub.add_parser("watchlater", help="watch-later (read / add-remove writes)")
    s.add_argument("action", nargs="?", choices=["list", "add", "remove"], default="list")
    s.add_argument("bvid", nargs="?", help="write-operation target (e.g. BV1xx411c7mD)")
    s.add_argument("--yes", action="store_true", help="explicit confirmation gate for write operations")
    s.set_defaults(fn=cmd_watchlater)

    s = sub.add_parser("fav", help="favorites (folder list / contents read / add-remove-move writes)")
    s.add_argument("action", nargs="?", choices=["list", "add", "remove", "move"], default="list")
    s.add_argument("bvid", nargs="?", help="write-operation target")
    s.add_argument("--fid", type=int, help="favorites folder id (needed for reading contents and for write operations)")
    s.add_argument("--from", dest="src_fid", type=int, help="move source folder id")
    s.add_argument("--to", dest="dst_fid", type=int, help="move target folder id")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_fav)

    s = sub.add_parser("history", help="view history (recent window)")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(fn=cmd_history)

    s = sub.add_parser("search", help="search (videos / UPs)")
    s.add_argument("query")
    s.add_argument("--kind", choices=["video", "up"], default="video")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("video", help="video detail (pages/stats/description)")
    s.add_argument("bvid")
    s.set_defaults(fn=cmd_video)

    s = sub.add_parser("subtitle", help="subtitle text (CC track by default, --ai forces the AI track)")
    s.add_argument("bvid")
    s.add_argument("--page", type=int, default=1, help="page (part) number")
    s.add_argument("--ai", action="store_true", help="take the AI subtitle track (lan ai-*)")
    s.set_defaults(fn=cmd_subtitle)

    s = sub.add_parser("summary", help="official AI video summary (has_summary=false if none; follow the degradation chain)")
    s.add_argument("bvid")
    s.set_defaults(fn=cmd_summary)

    s = sub.add_parser("up", help="UP info (optionally with the upload list)")
    s.add_argument("mid", type=int)
    s.add_argument("--arcs", action="store_true", help="include recent uploads")
    s.add_argument("--limit", type=int, default=20, help="number of uploads (with --arcs)")
    s.set_defaults(fn=cmd_up)

    s = sub.add_parser("like", help="like a video (write, requires --yes)")
    s.add_argument("bvid")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_like)

    s = sub.add_parser("raw", help="arbitrary endpoint passthrough (GET/POST JSON)")
    s.add_argument("url")
    s.add_argument("--post", action="store_true")
    s.set_defaults(fn=cmd_raw)

    s = sub.add_parser("comments", help="video comments (hot/new sort, anonymous-readable)")
    s.add_argument("bvid")
    s.add_argument("--ps", type=int, default=20)
    s.add_argument("--pn", type=int, default=1)
    s.add_argument("--sort", choices=["hot", "time"], default="hot")
    s.set_defaults(fn=cmd_comments)

    s = sub.add_parser("thread", help="one comment's floor replies (楼中楼)")
    s.add_argument("bvid")
    s.add_argument("--root", required=True, help="rpid of the comment to expand")
    s.add_argument("--pn", type=int, default=1)
    s.set_defaults(fn=cmd_thread)

    s = sub.add_parser("debrief", help="six-channel comprehension materials: meta/cover/subtitle/danmaku hot zones/hot comments/frame grid (ffmpeg optional)")
    s.add_argument("bvid")
    s.add_argument("--grid", default="3x3", help="frame grid layout, cols x rows")
    s.add_argument("--tile", type=int, default=720, help="grid cell width px")
    s.add_argument("--top", type=int, default=5, help="danmaku peak zones to report")
    s.add_argument("--bucket", type=int, default=30, help="danmaku bucket size (seconds)")
    s.add_argument("--comments", type=int, default=8, help="hot comments to include")
    s.add_argument("--no-frames", action="store_true", help="skip the frame channel (no ffmpeg needed)")
    s.add_argument("--uniform", action="store_true", help="force uniform frame sampling (skip scene snap)")
    s.add_argument("--substrip", type=int, default=0, metavar="N",
                   help="also build a hard-subtitle band collage, one row every N seconds")
    s.add_argument("--out", default=None, help="materials directory (default: auto temp dir)")
    s.set_defaults(fn=cmd_debrief)

    s = sub.add_parser("traffic", help="request-protection state (risk-control breaker + cache)")
    s.set_defaults(fn=cmd_traffic)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.fn(args)
    return 0
