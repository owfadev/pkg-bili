# bili-cli

Connector for the wiki bilibili domain (web-cookie login state, homegrown lightweight implementation: requests + self-implemented WBI signing, no community-library dependency). Contract: `.meta/plugins/bili/`.

## Discipline

- **Read-mostly + low-risk write whitelist** (2026-10-06 owner decision): whitelist = watch-later add/remove, favorites add/remove, like; all three require a double gate — an explicit user verb (agent layer) + `--yes` (CLI layer); write operations outside the whitelist (coin/comment/share/follow/dm/danmaku) are never provided, and raw does not carry them either
- Credentials are browser cookies (SESSDATA / bili_jct = csrf): acquired by QR scan (`login --qrcode`, default) or pasted Cookie header (fallback); stored only on the local machine in `~/.bili-cli/cookies.json` (0600), never in any repo, never on screen, never echoed in full; rotation is persisted automatically
- Personal data (watch history / favorites / watch-later) is instance data: relayed within the session only, never committed to any repo
- **Request protection (0.3.0, ported from [dsh-bili-whale](https://github.com/Alterssss/dsh-bili-whale), MIT)**: every api.bilibili.com GET runs under a guard — 24h metadata cache, 300 ms in-process throttle, exponential 412/-412 breaker (5/10/20/30 min, stale cache serves where safe); on GuardBlocked stop and report, never endpoint-hop. Danmaku XML serves a sample (comparative counts only). Debrief materials land in `--out`; point instance-side runs at the instance tmp zone

## Usage

```
bili-cli login --qrcode                        # scan with the bilibili app; no copy-paste (PNG at ~/.bili-cli/login-qr.png)
                                               #   --ascii/--no-ascii  terminal QR (default: only on a TTY)
                                               #   --no-png / --png PATH / --keep-qr
                                               #   --json-events      NDJSON progress on stderr
bili-cli login                                 # status: logged in? mid/uname? hint when expired
bili-cli login --cookie "SESSDATA=..; bili_jct=.."  # fallback: paste the browser Cookie header (or --file)
bili-cli logout                                # delete the stored credential
bili-cli me                                    # my info (login self-check)
bili-cli fav [--fid <id>] [--limit N]          # favorites folder list / contents
bili-cli watchlater                            # watch-later list
bili-cli history [--limit N]                   # watch history (recent window)
bili-cli search <q> [--kind video|up] [--limit N]
bili-cli video <bvid>                          # detail: pages/stats/description
bili-cli subtitle <bvid> [--page N] [--ai]     # subtitle text (CC track by default, --ai forces the AI track)
bili-cli summary <bvid>                          # official AI video summary (needs login; if none has_summary=false)
bili-cli comments <bvid> [--ps N] [--pn N] [--sort hot|time]   # comments (anonymous-readable)
bili-cli thread <bvid> --root <rpid>            # one comment's floor replies (楼中楼)
bili-cli debrief <bvid> [--grid 3x3] [--tile 720] [--no-frames] [--substrip N] [--out DIR]
                                               # six-channel comprehension materials: meta/cover/subtitle/
                                               # danmaku hot zones (spam-filtered, subtitle-aligned)/
                                               # hot comments/frame grid (ffmpeg; scene-snapped sampling)
bili-cli traffic                                # request-protection state (breaker + cache)
bili-cli up <mid> [--arcs --limit N]           # UP info (+ recent uploads)
# writes (whitelist, double gate):
bili-cli watchlater add|remove <bvid> --yes
bili-cli fav add|remove <bvid> --fid <id> --yes
bili-cli fav move <bvid> --from <id> --to <id> --yes   # cross-folder move (a whitelisted combination)
bili-cli like <bvid> --yes
bili-cli raw <url> [--post]                    # passthrough (probing/reconciliation)
```

Output is always single-line JSON; timestamps are `YYYY-MM-DD HH:MM` local timezone.

## Login

`login --qrcode` drives the web passport flow (`qrcode/generate` → poll every 2 s) and needs
nothing but a phone:

1. prints the QR twice — a PNG at `~/.bili-cli/login-qr.png` (0600, deleted on success) and,
   when stderr is a TTY, an ASCII rendering for SSH/terminal work;
2. reports state transitions `等待扫码` → `已扫码，请在手机上确认` → `登录成功`;
3. regenerates the QR **once** automatically if it expires mid-wait;
4. verifies the new credential against `nav` before declaring success;
5. writes rotated `Set-Cookie` values back after every later command (`persist_session`), so a
   live login keeps sliding forward instead of hard-expiring.

Exit codes: `0` success · `1` API/network failure · `2` expired or timed out (just rerun) · `130` Ctrl-C.

For agent layers, `--json-events` replaces the human lines on stderr with one JSON object per
line — stable enough to parse:

```json
{"event": "qr", "png": "/home/u/.bili-cli/login-qr.png", "expires_in": 180}
{"event": "state", "code": 86101, "label": "等待扫码"}
{"event": "success", "mid": "503383791"}
```

## Implementation notes

- WBI signing: nav hands out the img/sub keys -> rearrange via the 64-entry table, take the first 32 -> add wts to params and sort -> md5 -> `w_rid`; the key is cached in-process
- When buvid3 is absent it is fetched on demand via `/x/frontend/finger/spi` (risk-control baseline)
- Write-operation forms auto-inject csrf (= bili_jct)
- The endpoint table is centralized in `bilicli/config.py` — when bilibili revamps the site, change it there first; uncovered surfaces go through raw

## Known pitfalls (measured feedback)

- Unofficial web API, no stability promise; code -412 = risk control (keep calls serial and restrained)
- Video-watching degradation chain (measured): CC subtitle -> AI subtitle (--ai) -> official summary (summary, **needs login**, -101 measured) -> audio into wiki/tmp/ for local transcription (user-confirmed, discarded after use); in the anonymous state the AI subtitle track is often empty
- search is first page only; deep pagination goes through raw
- Login expired code -101 -> `bili-cli login` reports `logged_in:false`, then `bili-cli login --qrcode`
- The QR is valid ~3 minutes and needs a confirm tap in the app; the CLI polls every 2s and prints each state transition
- A live login slides forward on its own: rotated `Set-Cookie` values are written back after every command (`persist_session`), so SESSDATA's expiry moves with use instead of hard-expiring

## Changelog

- 0.4.0 2026-10-29: QR login — `login --qrcode` (passport generate/poll, transition-only progress, one automatic regeneration on expiry, `nav` verification before success), dual rendering (dependency-free PNG writer + TTY ASCII, optional `qr` extra), `--json-events` NDJSON contract, scriptable exit codes (0/1/2/130), consumed QR deleted from disk, `login` with no args = status, `logout`, and cookie rotation persistence (`persist_session`, atomic 0600 writes) so a live session keeps sliding instead of hard-expiring; 25 new tests (flow driven by a scripted fake session)
- 0.3.0 2026-10-26: comprehension batch (bili-pet prework) — comments / thread (楼中楼) reads; debrief six-channel materials pipeline (cover, timed subtitles, danmaku hot zones with spam-wave filtering + subtitle alignment, hot comments, scene-snapped frame grids, hard-subtitle band collage; ported from dsh-bili-whale debrief.mjs/bili-watch.mjs, MIT); traffic request protection (cache/breaker/throttle, ported from its bili-api.mjs); unittest suite (31 cases, no network, no ffmpeg)
- 0.2.0 2026-10-06: video-ingestion batch (user's four-scenario walkthrough) — subtitle --ai (AI track), summary (official AI summary, measured to need login), fav move (cross-folder move); watching degradation chain disclosed
- 0.1.0 2026-10-06: initial setup — seven design decisions converged through user Q&A (feature surface: personal data + queries + tracking; web-cookie auth; three-item low-risk write whitelist; homegrown light implementation; pull-now, cron later; query-and-answer + emergent archives; full connector + domain set)
