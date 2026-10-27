"""bili-cli constants: endpoint table, request headers, local credential paths.

Endpoints are unofficial web-side APIs that bilibili may change at any time — measured behavior
is authoritative; uncovered surfaces go through raw passthrough for probing.
"""

from pathlib import Path

APP_URL = "https://www.bilibili.com"
API_BASE = "https://api.bilibili.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# local credentials (0600, never in any repo, never on screen)
CONFIG_DIR = Path.home() / ".bili-cli"
COOKIE_FILE = CONFIG_DIR / "cookies.json"

# minimal cookie keys needed for login state; when buvid3 is absent it is fetched via the spi endpoint (risk-control baseline)
AUTH_KEYS = ("SESSDATA", "bili_jct")
BUVID_KEYS = ("buvid3", "buvid4")

# endpoint -> (path, needs WBI signing)
ENDPOINTS = {
    "nav": ("/x/web-interface/nav", False),            # my info + WBI key source
    "spi": ("/x/frontend/finger/spi", False),          # buvid fetched on demand, no login needed
    "watchlater": ("/x/v2/history/toview/web", False),
    "watchlater_add": ("/x/v2/history/toview/add", False),   # POST aid
    "watchlater_del": ("/x/v2/history/toview/del", False),   # POST aid
    "history": ("/x/v2/history", False),               # GET ps/max pagination
    "fav_folders": ("/x/v3/fav/folder/created/list-all", False),
    "fav_list": ("/x/v3/fav/resource/list", False),    # GET media_id/pn/ps
    "fav_deal": ("/x/v3/fav/resource/deal", False),    # POST favorites add/remove
    "like": ("/x/web-interface/archive/like", False),  # POST bvid/like + csrf
    "search": ("/x/web-interface/search/type", True),  # search_type=video|bili_user
    "view": ("/x/web-interface/view", False),          # video detail, bvid
    "player": ("/x/player/wbi/v2", True),              # subtitle dictionary, bvid/cid
    "up_arc": ("/x/space/wbi/arc/search", True),       # UP uploads, mid/pn
    "up_info": ("/x/space/wbi/acc/info", True),        # UP info, mid
    "conclusion": ("/x/web-interface/view/conclusion/get", True),  # official AI video summary
    "reply": ("/x/v2/reply", False),                  # video comments: type=1 oid=aid sort=2(hot)|0(time), anonymous-readable
    "reply_reply": ("/x/v2/reply/reply", False),      # one comment's floor replies: root=rpid
    "reply_add": ("/x/v2/reply/add", True),           # POST comment/reply (bili-pet surface; WBI sig rides the query)
    "relation": ("/x/relation", False),               # relation with fid (mutual-follow check for DM)
    "dm_send": ("/x/im/dm/sendMsg", False),           # POST web DM (bili-pet surface)
    "popular": ("/x/web-interface/popular", False),   # feed source: popular videos
    "rank": ("/x/web-interface/ranking/v2", False),   # feed source: ranking (rid=0 all)
    "dyn_feed": ("/x/polymer/web-dynamic/v1/feed/all", False),  # feed source: followees' dynamic videos
    "playurl": ("/x/player/wbi/playurl", True),       # stream URL: platform=pc+fnval=1 -> plain mp4 durl
}
