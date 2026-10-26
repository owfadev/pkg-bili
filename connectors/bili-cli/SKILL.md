---
name: bili-cli
owner: framework
description: "Access bilibili.com via the connectors/bili-cli CLI (web-cookie auth, read-mostly): search videos, fetch video details / subtitles / official AI summary, list and manage favorites / watch-later, view history, track UP uploads, relay as answer. Watching pipeline (degradation chain): CC subtitle -> AI subtitle -> official summary -> audio-to-tmp transcription. Small low-risk write whitelist (watch-later add/remove, favorites add/remove/move, like) — every write needs explicit user request plus the CLI --yes gate. Triggers on: bilibili, bili-cli, B站/B 站 (Bilibili), 收藏夹 (favorites), 稍后再看 (watch-later), 观看历史 (view history), UP主/UP 主 (uploader), UP 主投稿 (uploader's uploads), 搜索视频 (search videos), 看视频 (watch a video), 视频摘要 (video summary)."
---

# bili-cli: bilibili Read-Mostly Connector

Pull data from bilibili.com via `connectors/bili-cli/` and relay as answer. Query-and-answer by default: nothing is written to wiki unless the plugin contract (`.meta/plugins/bili/`) says so. Output is single-line JSON; distill before relaying.

## Scope

Read: video details / pages / stats, subtitles (CC + AI tracks), official AI video summary, search (video / UP), personal favorites, watch-later, view history, UP info and recent uploads.
Write: **whitelist only** (2026-10-06 owner decision) — `watchlater add/remove`, `fav add/remove/move`, `like`. Every write requires (a) an explicit user verb in conversation (agent-layer rule) AND (b) the CLI `--yes` gate. Everything else — coin, comment, share, follow/unfollow, dm, danmaku — is **never provided and never attempted via raw**.

## Prerequisites (install / auth)

1. Install (if missing): `cd connectors/bili-cli` and install editable with the host's Python package manager (deps: requests only; e.g. `python -m venv .venv` + the venv's pip on dev desktops, `uv venv .venv && uv pip install -e .` on PEP 668 hosts — instance-level environment declarations in AGENTS.md take precedence; may also run from another venv with requests available via `PYTHONPATH=.`)
2. Login: from a logged-in browser DevTools → Network → any api.bilibili.com request → copy the Cookie header value, then `bili-cli login --cookie "SESSDATA=...; bili_jct=...; buvid3=..."`. Stored at `~/.bili-cli/cookies.json` (0600) — credentials never enter any repo, never echoed in full.
3. Self-check: `bili-cli me` returns mid/uname when the cookie is alive; cookie expiry shows as API code -101 → user re-exports a fresh cookie.

## Steps

1. `bili-cli me` if session state is unknown
2. Pick a command per the need (cheat sheet below); all reads are pull-and-answer — do not project into wiki unless the plugin contract triggers (speed-page refresh / archive emergence)
3. Relay distilled results (language: see Language below); keep bvid / paths / commands verbatim

## Command cheat sheet

| Need | Command |
|---|---|
| My info (mid / level / coins / VIP) | `bili-cli me` |
| Favorites: folder list, then items | `bili-cli fav` → `bili-cli fav --fid <id> [--limit 50]` |
| Watch-later list | `bili-cli watchlater` |
| View history (recent window) | `bili-cli history [--limit 50]` |
| Search videos / UPs | `bili-cli search <q> [--kind up] [--limit 20]` |
| Video detail (pages / stats / desc) | `bili-cli video <bvid>` |
| Subtitle text (CC track default) | `bili-cli subtitle <bvid> [--page 2] [--ai]` (`tracks` field lists all) |
| Official AI video summary | `bili-cli summary <bvid>` (**needs login**; `has_summary: false` = none published, fall through the chain) |
| UP info (+ recent uploads) | `bili-cli up <mid> [--arcs --limit 20]` |
| Writes (whitelist, need `--yes` + explicit user verb) | `bili-cli watchlater add|remove <bvid> --yes` · `bili-cli fav add|remove <bvid> --fid <id> --yes` · `bili-cli fav move <bvid> --from <id> --to <id> --yes` · `bili-cli like <bvid> --yes` |
| Any endpoint passthrough | `bili-cli raw <url> [--post]` (reads/inspection only — never for write ops outside the whitelist) |

## Output notes

Timestamps are rendered `YYYY-MM-DD HH:MM` local time; durations are seconds (`duration_s`). `search --kind video` rows carry play/duration as raw strings. `subtitle` returns empty `tracks: []` when the video has no CC tracks (or the uploader disabled them); **anonymous sessions get an empty track list for EVERY video** (bilibili hides subtitles from logged-out callers, CC included — measured 2026-10-25), so the output carries `login: false` + a `note` in that case, meaning "re-login first", not "no subtitles". Player subtitles need the video's `cid` — the CLI resolves it from the page list.

## Known limits

- Unofficial web API: endpoints and the WBI signing scheme can change without notice; on breakage use `raw` against the browser-observed URL to re-derive, then fix `config.py` endpoint table
- Some endpoints throttle anonymous/refresh traffic (code -412 risk control): keep requests serial, reuse one command per need; do not loop aggressively
- `history` only walks the recent window the API serves; deep history is not exposed
- Search results are the first page only (page: 1); paginate via `raw` if ever needed
- Audio download for transcription is the only materialization path: temp files under `wiki/tmp/`, never into `vault/` or the repo; user confirmation required
- Watching pipeline (degradation chain, stop at first hit): CC subtitle (`subtitle`) -> AI subtitle track (`subtitle --ai`) -> official AI summary (`summary`) -> audio download into `wiki/tmp/` + local transcription (user-confirmed, delete after use). **Measured** (2026-10-25): the player API hides the whole subtitle list (CC included) from anonymous sessions, and official summary / AI tracks need login — anonymous watching therefore degrades straight to transcription; `login: false` in `subtitle` output = cookie expired, re-login instead of degrading
- Cookie = full account credential: treat like a password; page bodies contain personal data (history, favorites) — relay in conversation only, never commit CLI output into any repo

## Prohibitions

- Never perform whitelist-external writes (coin / comment / share / follow / dm / danmaku), never via raw, never by emulation
- Never execute a whitelisted write without an explicit user verb in the conversation, even with `--yes` available — `--yes` is the second gate, not the first
- Cookies and personal pulls (history / favorites / watch-later) never enter the repo or test-repo; never echo the full cookie value

## Language

Follow the upper-layer requirements and the conversation context. Keep bvid, mid, UP names, video titles, file names, and paths verbatim.
