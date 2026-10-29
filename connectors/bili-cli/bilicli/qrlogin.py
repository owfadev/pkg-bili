"""QR-code login: scan once with the bilibili app — no DevTools, no cookie copy-paste.

Unofficial web passport flow (stable since 2021, the same one the site's login page uses):

    generate  ->  {qrcode_key, url}     url = the QR payload, not a page to open
    poll      ->  0 ok / 86101 unscanned / 86090 scanned-awaiting-confirm / 86038 expired

On success the poll response carries Set-Cookie (SESSDATA / bili_jct / DedeUserID / ...);
the same values also ride the returned url's query string, used here only as a fallback.

Design notes:
  * The QR is rendered locally — PNG by a dependency-free writer (zlib + struct), ASCII by the
    optional `qrcode` package. Neither sends the payload anywhere.
  * Progress goes to stderr in two interchangeable shapes: human lines, or NDJSON events
    (`--json-events`) for agent layers that would rather parse than scrape.
  * An expired QR is regenerated once automatically; the deadline covers the whole attempt.
  * Exit codes: 0 success · 1 API/network failure · 2 expired or timed out (rerun)
"""

import io
import json
import os
import struct
import sys
import time
import zlib
from pathlib import Path

import requests

from .config import CONFIG_DIR, USER_AGENT

PASSPORT = "https://passport.bilibili.com"
QR_GENERATE = PASSPORT + "/x/passport-login/web/qrcode/generate"
QR_POLL = PASSPORT + "/x/passport-login/web/qrcode/poll"

QR_PNG = CONFIG_DIR / "login-qr.png"

# poll status codes -> human labels (measured against the web login page)
QR_STATUS = {
    0: "登录成功",
    86101: "等待扫码",
    86090: "已扫码，请在手机上确认",
    86038: "二维码已过期",
}

# keys worth keeping out of the success payload; everything else is ceremony
KEEP_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")

POLL_INTERVAL_S = 2
NETWORK_RETRIES = 3


def _now() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def status_label(code) -> str:
    return QR_STATUS.get(code, f"未知状态 {code}")


def _fail(msg: str, code: int = 1):
    """Report on stderr and leave with a scriptable exit code (1 = failure, 2 = retryable)."""
    print(msg, file=sys.stderr, flush=True)
    raise SystemExit(code)


# --------------------------------------------------------------------------- payload helpers

def _raw_query(url: str) -> dict:
    """Split a query string WITHOUT unquoting: cookie values must stay percent-encoded."""
    if "?" not in url:
        return {}
    out = {}
    for part in url.split("?", 1)[1].split("&"):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        if k and v:
            out[k] = v
    return out


def cookies_from_url(url: str) -> dict:
    """Fallback extraction: the login-success url carries the same cookies as the Set-Cookie headers."""
    q = _raw_query(url)
    return {k: q[k] for k in KEEP_KEYS if k in q}


def matrix_for(url: str):
    """QR matrix (rows of bool) for the payload; raises ImportError without the qr extra."""
    import qrcode  # optional dependency

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=0)
    qr.add_data(url)
    qr.make(fit=True)
    return qr.get_matrix()


def render_ascii(url: str) -> str | None:
    """Terminal QR (two-cell blocks, inverted for dark themes); None when the qr extra is absent."""
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue()


def write_png(matrix, path: Path = QR_PNG, scale: int = 8, border: int = 4) -> Path:
    """Minimal RGB PNG writer — no Pillow, no external encoder.

    Dark modules on a white field so the camera reads it in either theme.
    """
    n = len(matrix)
    side = (n + border * 2) * scale
    rows = []
    for y in range(side):
        gy = y // scale - border
        row = bytearray([0])  # PNG filter type 0 (None)
        for x in range(side):
            gx = x // scale - border
            dark = 0 <= gy < n and 0 <= gx < n and bool(matrix[gy][gx])
            row += b"\x00\x00\x00" if dark else b"\xff\xff\xff"
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    try:  # the QR is a live credential for ~3 minutes; keep it owner-only
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


# --------------------------------------------------------------------------- progress channel

class Events:
    """stderr reporter: human sentences, or one JSON object per line for machine consumers."""

    def __init__(self, stream=None, json_mode: bool = False):
        self.stream = stream or sys.stderr
        self.json_mode = json_mode
        self._last = None

    def _write(self, line: str) -> None:
        print(line, file=self.stream, flush=True)

    def emit(self, event: str, **fields) -> None:
        if event == "state" and fields.get("label") == self._last:
            return  # transitions are the signal; identical polls are noise in both modes
        if event == "state":
            self._last = fields.get("label")
        if self.json_mode:
            self._write(json.dumps({"event": event, **fields}, ensure_ascii=False))
            return
        if event == "qr":
            self._write(f"[login] 二维码已生成：{fields.get('png', '-')}")
            self._write("[login] 用 bilibili App 扫码登录（约 3 分钟有效，扫完还要在手机上点确认）")
        elif event == "state":
            self._write(f"[login] {fields.get('label', '')}")
        elif event == "success":
            self._write("[login] 登录成功，凭证已写入本机")
        elif event == "regenerate":
            self._write("[login] 二维码已过期，自动换一张")
        elif event == "hint":
            self._write(f"[login] {fields.get('text', '')}")


# --------------------------------------------------------------------------- flow

def _request_json(session, url: str, params: dict | None = None, attempts: int = NETWORK_RETRIES) -> dict:
    """GET with bounded retries; the passport host is not under the api rate-limiter."""
    last = None
    for i in range(attempts):
        try:
            return session.get(url, params=params, timeout=15).json()
        except (requests.RequestException, ValueError) as e:  # network hiccup or non-JSON body
            last = e
            if i + 1 < attempts:
                _sleep(1 + i)
    _fail(f"passport 请求失败（已重试 {attempts} 次）：{last}", 1)


def _generate(session) -> tuple[str, str]:
    body = _request_json(session, QR_GENERATE)
    if body.get("code") != 0:
        _fail(f"二维码生成失败：code={body.get('code')} {body.get('message')}", 1)
    data = body.get("data") or {}
    key, url = data.get("qrcode_key"), data.get("url")
    if not key or not url:
        _fail("二维码生成失败：响应缺少 qrcode_key/url", 1)
    return key, url


def _poll(session, key: str) -> dict:
    body = _request_json(session, QR_POLL, params={"qrcode_key": key})
    return body.get("data") or {}


def _jar(session, success_url: str) -> dict:
    jar = dict(session.cookies.get_dict())
    # Set-Cookie wins; the url only fills gaps (it is the same credential, kept for older responses)
    jar.update({k: v for k, v in cookies_from_url(success_url).items() if k not in jar})
    return jar


def login(timeout_s: int = 180, png_path: Path | str | None = QR_PNG, *,
          ascii_qr=None, keep_qr: bool = False, json_events: bool = False,
          stream=None, session=None) -> dict:
    """Generate a QR, wait for the scan, return the cookie jar.

    `ascii_qr=None` means "decide by TTY". Raises SystemExit(2) on expiry/timeout.
    """
    stream = stream or sys.stderr
    events = Events(stream, json_mode=json_events)
    if ascii_qr is None:
        ascii_qr = bool(getattr(stream, "isatty", lambda: False)())

    session = session or requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"})

    deadline = _now() + timeout_s
    regenerate_left = 1  # one automatic replacement when the QR expires mid-wait

    while True:
        key, url = _generate(session)
        png = None
        if png_path:
            try:
                png = write_png(matrix_for(url), Path(png_path))
            except ImportError:
                png = None
            except OSError as e:
                events.emit("hint", text=f"二维码 PNG 写入失败（{e}），改用文本输出")
        if ascii_qr:
            art = render_ascii(url)
            if art:
                print(art, file=stream, end="", flush=True)
        if png is None:
            events.emit("hint", text='未安装二维码渲染依赖（可选）：uv pip install "bilicli[qr]"')
            events.emit("hint", text=f"二维码内容：{url}")
        else:
            events.emit("qr", png=str(png), expires_in=timeout_s)

        expired = False
        while _now() < deadline:
            data = _poll(session, key)
            code = data.get("code")
            if code == 0:
                jar = _jar(session, data.get("url") or "")
                if not keep_qr and png:
                    try:
                        png.unlink()  # a consumed QR is still a credential; don't leave it around
                    except OSError:
                        pass
                events.emit("success", mid=jar.get("DedeUserID"))
                return jar
            events.emit("state", code=code, label=status_label(code))
            if code == 86038:
                expired = True
                break
            _sleep(POLL_INTERVAL_S)

        if expired and regenerate_left and _now() < deadline:
            regenerate_left -= 1
            events.emit("regenerate")
            continue
        if expired:
            events.emit("error", reason="expired")
            _fail(f"二维码已过期（{timeout_s}s 内未完成扫码），请重新运行 bili-cli login --qrcode", 2)
        events.emit("error", reason="timeout")
        _fail(f"等待扫码超时（{timeout_s}s），请重新运行 bili-cli login --qrcode", 2)
