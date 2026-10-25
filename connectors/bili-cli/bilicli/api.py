"""Thin wrapper over the bilibili web API: session (cookie/buvid/UA/Referer), WBI signing, endpoint methods.

Read-mostly; write methods are limited to the low-risk whitelist the connector discipline allows
(watch-later/favorites/like); the caller (cli.py) owns the --yes gate and csrf injection.
"""

import time
from hashlib import md5
from urllib.parse import urlencode

import requests

from . import auth
from .config import API_BASE, APP_URL, ENDPOINTS, USER_AGENT

# WBI mixin key rearrangement table (stable community-published values, in use since 2023;
# the keys handed out by nav rotate, the table itself never changes)
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


class BiliAPI:
    def __init__(self, authed: bool = True):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Referer": APP_URL + "/"})
        self.s.params = {}
        self.csrf = ""
        self.authed = False
        if authed:
            try:
                jar = auth.load_cookies()
            except SystemExit:
                jar = None  # no credentials file -> anonymous (public endpoints like search/video/up still work)
            if jar:
                self.s.cookies.update(jar)
                self.csrf = jar.get("bili_jct", "")
                self.authed = True
        # buvid3 is the risk-control baseline; anonymous calls (WBI endpoints like search) need it too
        self._ensure_buvid()
        self._wbi_key = None

    def require_auth(self, cmd: str) -> None:
        """Login gate for personal data and write commands; public queries (search/video/up/subtitle) are ungated."""
        if not self.authed:
            raise SystemExit(
                f"{cmd} requires login state: run bili-cli login --cookie '...' first (credentials stay in ~/.bili-cli/ only)")

    # -- basics ---------------------------------------------------------

    def _ensure_buvid(self):
        if self.s.cookies.get("buvid3"):
            return
        r = self.s.get(API_BASE + ENDPOINTS["spi"][0], timeout=15)
        for k in ("b_3", "b_4"):
            v = (r.json().get("data") or {}).get(k)
            if v:
                self.s.cookies.set({"b_3": "buvid3", "b_4": "buvid4"}[k], v)

    def _wbi_mixin_key(self) -> str:
        if self._wbi_key is None:
            nav = self.s.get(API_BASE + ENDPOINTS["nav"][0], timeout=15).json()
            wbi = nav.get("data", {}).get("wbi_img") or {}
            img = wbi.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
            sub = wbi.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
            raw = img + sub
            self._wbi_key = "".join(raw[i] for i in _MIXIN_TAB[:32])
        return self._wbi_key

    def _sign(self, params: dict) -> dict:
        params = {k: str(v) for k, v in params.items()}
        params["wts"] = int(time.time())
        qs = urlencode(sorted(params.items(), key=lambda kv: kv[0]))
        for ch in "!'()*":
            qs = qs.replace(ch, "")
            # note: the filtering targets values; wholesale replacement suffices here
            # (these characters never appear in keys; appearing in values they get stripped, matching site behavior)
        params["w_rid"] = md5((qs + self._wbi_mixin_key()).encode()).hexdigest()
        return params

    def _call(self, name: str, params: dict | None = None, post: bool = False,
              extra_form: dict | None = None, headers: dict | None = None):
        path, need_wbi = ENDPOINTS[name]
        params = dict(params or {})
        if post:
            form = {**params, **(extra_form or {})}
            if self.csrf:
                form.setdefault("csrf", self.csrf)
                form.setdefault("csrf_token", self.csrf)
            r = self.s.post(API_BASE + path, data=form, timeout=15)
        else:
            if need_wbi:
                params = self._sign(params)
            r = self.s.get(API_BASE + path, params=params, timeout=15, headers=headers)
        try:
            body = r.json()
        except ValueError:
            raise SystemExit(f"non-JSON response (HTTP {r.status_code}) — the endpoint may have changed; verify via raw passthrough")
        if body.get("code") != 0:
            raise SystemExit(f"API error code={body.get('code')}: {body.get('message')}")
        return body.get("data")

    # space-family endpoints (up_info/up_arc) have the strictest risk control: they need the space-page
    # Referer + browser fingerprint params, otherwise -352; the fingerprint values are common community
    # static strings (unrelated to real fingerprints, merely satisfying the existence check)
    _DM_PARAMS = {
        "dm_img_list": "[]",
        "dm_img_str": "V2ViR0wgMS4wIChXaW5kb3dzKQ==",
        "dm_cover_img_str": "QU5HTEUgKEFNUCwgQU1QIEFNUCBTSEExKSBEaXJlY3QzRDExX3ZzXzVfMCBl",
        "dm_img_inter": '{"ds":[],"wh":[0,0,0,0],"of":[0,0,0,0]}',
    }

    def _space_headers(self, mid: int) -> dict:
        return {"Referer": f"https://space.bilibili.com/{mid}/"}

    # -- reads (query-and-answer) ----------------------------------------

    def me(self):
        return self.s.get(API_BASE + ENDPOINTS["nav"][0], timeout=15).json().get("data")

    def watchlater(self):
        return self._call("watchlater")

    def history(self, limit: int = 30):
        out, max_cursor = [], 0
        while len(out) < limit:
            data = self._call("history", {"ps": min(30, limit - len(out)), "max": max_cursor})
            rows = (data or {}).get("list") or []
            if not rows:
                break
            out.extend(rows)
            max_cursor = rows[-1]["view_at"]
        return out[:limit]

    def fav_folders(self):
        return (self._call("fav_folders") or {}).get("list")

    def fav_list(self, fid: int, limit: int = 30):
        data = self._call("fav_list", {
            "media_id": fid, "pn": 1, "ps": min(20 * ((limit + 19) // 20), 20 * 5),
            "keyword": "", "order": "mtime", "type": 0, "tid": 0,
        })
        return (data or {}).get("medias")

    def search(self, query: str, kind: str = "video", limit: int = 10):
        data = self._call("search", {
            "search_type": {"video": "video", "up": "bili_user"}[kind],
            "keyword": query, "page": 1,
        })
        results = (data or {}).get("result") or []
        return results[:limit]

    def video(self, bvid: str):
        return self._call("view", {"bvid": bvid})

    def subtitle(self, bvid: str, page: int = 1, ai: bool = False):
        """Subtitle text. Track selection: by default take the first non-AI track (fall back to AI if none),
        --ai forces the AI track; the tracks field lists all tracks so the agent can judge degradation."""
        view = self._call("view", {"bvid": bvid})
        pages = view.get("pages") or [{}]
        cid = pages[min(page, len(pages)) - 1].get("cid")
        player = self._call("player", {"bvid": bvid, "cid": cid})
        subs = ((player.get("subtitle") or {}).get("subtitles")) or []
        is_ai = lambda s: str(s.get("lan", "")).startswith("ai-")
        tracks = [{"lan": s.get("lan"), "ai": is_ai(s)} for s in subs]
        if not subs:
            return {"cid": cid, "tracks": tracks, "text": None}
        if ai:
            pick = next((s for s in subs if is_ai(s)), subs[0])
        else:
            pick = next((s for s in subs if not is_ai(s)), subs[0])
        url = pick["subtitle_url"]
        if url.startswith("//"):
            url = "https:" + url
        doc = self.s.get(url, timeout=15).json().get("body") or []
        return {
            "cid": cid, "lan": pick.get("lan"), "tracks": tracks, "lines": len(doc),
            "text": "\n".join(line.get("content", "") for line in doc),
        }

    def summary(self, bvid: str):
        """Official AI video summary (the site's "视频速览" feature). The endpoint takes bvid+cid+up_mid
        with WBI signing; data.code != 0 or an empty payload = this video has no official summary
        (degradation chain: see SKILL)."""
        view = self._call("view", {"bvid": bvid})
        cid = (view.get("pages") or [{}])[0].get("cid")
        up_mid = (view.get("owner") or {}).get("mid")
        params = self._sign({"bvid": bvid, "cid": cid, "up_mid": up_mid})
        r = self.s.get(API_BASE + ENDPOINTS["conclusion"][0], params=params, timeout=15,
                       headers={"Referer": f"https://www.bilibili.com/video/{bvid}/"})
        data = (r.json().get("data") or {})
        # measured 2026-10-25 (BV1wvJB6aEp5): model_result is an OBJECT now —
        # {result_type, summary, outline[], subtitle[]} — while older payloads carried a plain string,
        # and the outline can live either under data.outline or inside model_result. Normalize both
        # shapes before reading: slicing a dict as a string raised KeyError here.
        mr = data.get("model_result")
        text = mr if isinstance(mr, str) else ((mr or {}).get("summary") or "")
        outline = data.get("outline") or (mr.get("outline") if isinstance(mr, dict) else None) or []
        return {
            "bvid": bvid,
            "has_summary": data.get("code", 0) == 0 and bool(text or outline),
            "model_result": text[:4000],
            "outline": [{"title": o.get("title"),
                         "bullets": [b.get("content") for b in (o.get("part_outline") or [])][:8]}
                        for o in outline][:10],
        }

    def up_info(self, mid: int):
        return self._call("up_info", {"mid": mid, **self._DM_PARAMS},
                          headers=self._space_headers(mid))

    def up_arcs(self, mid: int, limit: int = 20):
        data = self._call("up_arc", {
            "mid": mid, "pn": 1, "ps": min(limit, 50),
            "order": "pubdate", "platform": "web", "jsonp": "jsonp",
            **self._DM_PARAMS,
        }, headers=self._space_headers(mid))
        vlist = ((data or {}).get("list") or {}).get("vlist") or []
        return vlist[:limit]

    # -- writes (low-risk whitelist; the --yes gate lives in the cli layer) --

    def watchlater_add(self, bvid: str):
        aid = self._bvid_to_aid(bvid)
        return {"ok": bool(self._call("watchlater_add", post=True, extra_form={"aid": aid}))}

    def watchlater_del(self, bvid: str):
        aid = self._bvid_to_aid(bvid)
        return {"ok": bool(self._call("watchlater_del", post=True, extra_form={"aid": aid}))}

    def fav_add(self, bvid: str, fid: int):
        rid = self._bvid_to_aid(bvid)
        return self._call("fav_deal", post=True, extra_form={
            "rid": rid, "type": 2, "add_media_ids": fid, "del_media_ids": "", "media_id": fid,
        })

    def fav_del(self, bvid: str, fid: int):
        rid = self._bvid_to_aid(bvid)
        return self._call("fav_deal", post=True, extra_form={
            "rid": rid, "type": 2, "add_media_ids": "", "del_media_ids": fid, "media_id": fid,
        })

    def fav_move(self, bvid: str, src_fid: int, dst_fid: int):
        """Cross-folder move = add to the target folder + del from the source folder within one deal call (a whitelisted composite operation)."""
        rid = self._bvid_to_aid(bvid)
        return self._call("fav_deal", post=True, extra_form={
            "rid": rid, "type": 2, "add_media_ids": dst_fid, "del_media_ids": src_fid,
            "media_id": src_fid,
        })

    def like(self, bvid: str):
        return self._call("like", post=True, extra_form={"bvid": bvid, "like": 1})

    def _bvid_to_aid(self, bvid: str) -> int:
        return int(self._call("view", {"bvid": bvid}).get("aid") or 0)

    # -- passthrough ------------------------------------------------------

    def raw(self, url: str, post: bool = False):
        r = (self.s.post if post else self.s.get)(url, timeout=20)
        try:
            return r.json()
        except ValueError:
            return {"http_status": r.status_code, "text_head": r.text[:2000]}
