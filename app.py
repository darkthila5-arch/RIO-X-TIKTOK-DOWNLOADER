"""TikTok Downloader x Rio - Flask backend.

Flow
    /api/info     -> look the video up (3 providers, first one that works wins)
    /api/prepare  -> start a download. Answers with either
                       {"file": "/dl/<token>"}  a direct download that is ready now, or
                       {"job": "<id>"}          ffmpeg work (watermark / MP3) to poll at /api/job/<id>
    /dl/<token>   -> streams a ready direct download (video, original file, cover, details)
    /file/<token> -> serves a finished ffmpeg result (watermarked video, MP3)

Every download goes through /api/prepare first, so a failure always becomes a readable message
in the page instead of a broken file in the browser.
"""
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

try:
    import yt_dlp
except ImportError:  # the other providers still work
    yt_dlp = None
try:
    from curl_cffi import requests as cffi  # Chrome-like TLS fingerprint
except ImportError:
    cffi = None
try:
    import imageio_ffmpeg  # ships a ready-made ffmpeg binary
except ImportError:
    imageio_ffmpeg = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
app.logger.setLevel(logging.INFO)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # API bodies are tiny; refuse anything big


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return int(default)


def _float_env(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


# ---------------------------------------------------------------------------
# Config (all optional, set in Render > Environment)
# ---------------------------------------------------------------------------
RATE_LIMIT = _int_env("RATE_LIMIT", 12)                  # requests per window, per visitor
RATE_WINDOW = _int_env("RATE_WINDOW", 60)                # seconds
GLOBAL_RATE_LIMIT = _int_env("GLOBAL_RATE_LIMIT", 240)   # safety net for the whole site, can't be spoofed
PROXY_HOPS = _int_env("PROXY_HOPS", 2)                   # proxies in front of the app (0 = ignore X-Forwarded-For)
CACHE_TTL = _int_env("CACHE_TTL", 600)
PROXY_URL = os.environ.get("PROXY_URL") or None
DEBUG_ERRORS = os.environ.get("DEBUG_ERRORS") == "1"     # show real errors in the UI + enable /api/debug/ip
PROVIDERS = [p.strip() for p in os.environ.get("PROVIDERS", "own,tikwm,ytdlp").split(",") if p.strip()]
IMPERSONATE = os.environ.get("IMPERSONATE", "chrome")
RESOLVE_BUDGET = _int_env("RESOLVE_BUDGET", 50)          # seconds spent on all providers together

# Watermark = the ghost logo in static/watermark.png (needs ffmpeg only)
WATERMARK_ENABLED = os.environ.get("WATERMARK", "1") != "0"
WATERMARK_IMAGE = os.environ.get("WATERMARK_IMAGE", os.path.join(BASE_DIR, "static", "watermark.png"))
WATERMARK_SIZE = _float_env("WATERMARK_SIZE", 0.30)      # logo width as a share of the video width
WATERMARK_OPACITY = min(1.0, max(0.1, _float_env("WATERMARK_OPACITY", 0.92)))
WATERMARK_POSITIONS = ("br", "bl", "tr", "tl")           # bottom-right, bottom-left, top-right, top-left
WATERMARK_DEFAULT_POS = os.environ.get("WATERMARK_POSITION", "br")
if WATERMARK_DEFAULT_POS not in WATERMARK_POSITIONS:
    WATERMARK_DEFAULT_POS = "br"

FFMPEG_PRESET = os.environ.get("FFMPEG_PRESET", "ultrafast")  # faster = bigger file
FFMPEG_TIMEOUT = _int_env("FFMPEG_TIMEOUT", 240)
MAX_SOURCE_MB = _int_env("MAX_SOURCE_MB", 80)
MAX_ACTIVE_JOBS = _int_env("MAX_ACTIVE_JOBS", 5)
FILE_TTL = 600     # how long a finished ffmpeg file stays downloadable
TICKET_TTL = 900   # how long a direct-download link stays valid
JOB_TTL = 900

# Live download counter. Set the two Upstash variables to keep the number forever;
# without them it is saved to a temp file that Render wipes on restart.
UPSTASH_URL = (os.environ.get("UPSTASH_REDIS_REST_URL") or "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or ""
COUNTER_KEY = os.environ.get("COUNTER_KEY", "tt_downloads")
COUNTER_FILE = os.environ.get("COUNTER_FILE", os.path.join(tempfile.gettempdir(), "tt_counter.json"))

UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)
# A link ends at whitespace, quotes, brackets or CJK / full-width punctuation.
URL_RE = re.compile(r"https?://[^\s<>\"'`“”‘’「」『』。、，！？）】]+")
TRAILING = ".,;:!?)]}>*_"
FORMAT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")


class ResolveError(Exception):
    pass


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
def json_body():
    """The request body as a dict, whatever the client sent."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def text_field(data, key, limit=2000):
    value = data.get(key)
    return value.strip()[:limit] if isinstance(value, str) else ""


def extract_url(text):
    """Accept a bare link or the whole share text and pull the link out."""
    match = URL_RE.search(text if isinstance(text, str) else "")
    return match.group(0).rstrip(TRAILING) if match else ""


def is_tiktok_url(url):
    """Only real TikTok hosts, so the server can't be used as an open proxy."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and (host == "tiktok.com" or host.endswith(".tiktok.com"))


def error(message, status, detail=None):
    body = {"error": message}
    if DEBUG_ERRORS and detail:
        body["detail"] = str(detail)[:300]
    resp = jsonify(body)
    resp.status_code = status
    return resp


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def iso_date(epoch=None, yyyymmdd=None):
    try:
        if epoch:
            return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d")
        if yyyymmdd and len(str(yyyymmdd)) == 8:
            s = str(yyyymmdd)
            return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    except (ValueError, OSError, OverflowError):
        pass
    return None


def safe_name(value, fallback="video"):
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))
    return cleaned[:40] or fallback


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
_hits = defaultdict(deque)
_global_hits = deque()
_rate_lock = threading.Lock()
MAX_TRACKED_IPS = 5000


def client_ip():
    """The visitor's IP. X-Forwarded-For can be forged by the client, so only the
    entry added by our own proxies (counted from the right) is trusted."""
    parts = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
    if PROXY_HOPS > 0 and parts:
        return parts[-PROXY_HOPS] if len(parts) >= PROXY_HOPS else parts[0]
    return request.remote_addr or "unknown"


def _trim(q, now):
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()


def rate_limited():
    now = time.time()
    with _rate_lock:
        _trim(_global_hits, now)
        if len(_global_hits) >= GLOBAL_RATE_LIMIT:
            return True
        q = _hits[client_ip()]
        _trim(q, now)
        if len(q) >= RATE_LIMIT:
            return True
        q.append(now)
        _global_hits.append(now)
        if len(_hits) > MAX_TRACKED_IPS:  # keep memory bounded, even under spoofed addresses
            for ip in [ip for ip, d in _hits.items() if not d or now - d[-1] > RATE_WINDOW]:
                _hits.pop(ip, None)
            if len(_hits) > MAX_TRACKED_IPS:
                _hits.clear()
    return False


# ---------------------------------------------------------------------------
# Cache (links expire, so keep them only for a few minutes)
# ---------------------------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()


def cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if item and item[0] > time.time():
            return item[1]
        _cache.pop(key, None)
    return None


def cache_set(key, value):
    with _cache_lock:
        if len(_cache) > 200:
            now = time.time()
            for k in [k for k, (exp, _) in _cache.items() if exp < now]:
                _cache.pop(k, None)
            if len(_cache) > 200:
                _cache.clear()
        _cache[key] = (time.time() + CACHE_TTL, value)


# ---------------------------------------------------------------------------
# Provider: TikWM (works from datacenter IPs where yt-dlp usually fails)
# ---------------------------------------------------------------------------
_tikwm_lock = threading.Lock()
_tikwm_last = 0.0


def _abs(u):
    if not u:
        return None
    return u if u.startswith("http") else "https://www.tikwm.com" + u


def from_tikwm(url):
    global _tikwm_last
    with _tikwm_lock:  # the free API allows about 1 request per second
        wait = 1.2 - (time.time() - _tikwm_last)
        if wait > 0:
            time.sleep(wait)
        _tikwm_last = time.time()

    r = requests.get(
        "https://www.tikwm.com/api/",
        params={"url": url, "hd": 1},
        headers={"User-Agent": UA},
        timeout=25,
    )
    r.raise_for_status()
    payload = r.json()
    d = payload.get("data")
    if payload.get("code") != 0 or not d:
        raise ResolveError(f"tikwm: {payload.get('msg') or 'no data'}")

    formats = []
    if d.get("hdplay"):
        formats.append({"id": "hd", "label": "HD", "kind": "video",
                        "size": to_int(d.get("hd_size")), "url": _abs(d["hdplay"])})
    if d.get("play"):
        formats.append({"id": "sd", "label": "Standard", "kind": "video",
                        "size": to_int(d.get("size")), "url": _abs(d["play"])})
    if d.get("wmplay"):
        formats.append({"id": "wm", "label": "With TikTok watermark", "kind": "video",
                        "size": to_int(d.get("wm_size")), "url": _abs(d["wmplay"])})
    if d.get("music"):
        formats.append({"id": "srcaudio", "label": "Sound", "kind": "audio",
                        "size": None, "url": _abs(d["music"])})

    author = d.get("author") or {}
    return {
        "id": str(d.get("id") or "video"),
        "title": (d.get("title") or "TikTok video").strip(),
        "author": author.get("nickname") or "",
        "handle": author.get("unique_id") or "",
        "thumbnail": _abs(d.get("origin_cover") or d.get("cover")),
        "duration": to_int(d.get("duration")),
        "views": to_int(d.get("play_count")),
        "likes": to_int(d.get("digg_count")),
        "comments": to_int(d.get("comment_count")),
        "shares": to_int(d.get("share_count")),
        "created": iso_date(epoch=d.get("create_time")),
        "formats": formats,
    }


# ---------------------------------------------------------------------------
# Provider: yt-dlp
# ---------------------------------------------------------------------------
def from_ytdlp(url):
    if yt_dlp is None:
        raise ResolveError("ytdlp: yt-dlp is not installed")
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "socket_timeout": 20}
    if PROXY_URL:
        opts["proxy"] = PROXY_URL

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        candidates = info.get("formats") or [info]
        candidates = sorted(
            candidates,
            key=lambda f: (f.get("height") or 0, f.get("filesize") or f.get("filesize_approx") or 0),
            reverse=True,
        )

        formats, seen = [], set()
        for f in candidates:
            if not f.get("url") or f.get("vcodec") == "none":
                continue
            if (f.get("protocol") or "https") not in ("http", "https"):
                continue
            note = (f.get("format_note") or "").lower()
            if f.get("format_id") == "download" or "watermark" in note:
                label = "Original (watermark)"
            elif f.get("height"):
                label = f"{f['height']}p"
            else:
                label = str(f.get("format_id") or "Video")
            if label in seen:
                continue
            seen.add(label)

            headers = dict(f.get("http_headers") or info.get("http_headers") or {})
            cookie = ydl.cookiejar.get_cookie_header(f["url"])
            if cookie:
                headers["Cookie"] = cookie
            formats.append({
                "id": f"y{len(formats)}", "label": label, "kind": "video",
                "size": to_int(f.get("filesize") or f.get("filesize_approx")),
                "url": f["url"], "headers": headers, "proxy": bool(PROXY_URL),
            })

    return {
        "id": str(info.get("id") or "video"),
        "title": (info.get("title") or info.get("description") or "TikTok video").strip(),
        "author": info.get("uploader") or info.get("creator") or "",
        "handle": info.get("uploader_id") or info.get("channel") or "",
        "thumbnail": info.get("thumbnail"),
        "duration": to_int(info.get("duration")),
        "views": to_int(info.get("view_count")),
        "likes": to_int(info.get("like_count")),
        "comments": to_int(info.get("comment_count")),
        "shares": to_int(info.get("repost_count")),
        "created": iso_date(epoch=info.get("timestamp"), yyyymmdd=info.get("upload_date")),
        "formats": formats,
    }


# ---------------------------------------------------------------------------
# Provider: our own scraper (reads the data TikTok embeds in the video page)
# ---------------------------------------------------------------------------
PAGE_HEADERS = {"Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.tiktok.com/"}
DATA_RE = re.compile(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S)


def cffi_proxies():
    return {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None


def parse_item(html):
    """Pull the video object out of the JSON blob TikTok puts in the page."""
    match = DATA_RE.search(html or "")
    if not match:
        raise ResolveError("own: page had no video data (TikTok probably served a block page)")
    try:
        data = json.loads(match.group(1))
    except ValueError:
        raise ResolveError("own: video data was not valid JSON")
    detail = (data.get("__DEFAULT_SCOPE__") or {}).get("webapp.video-detail") or {}
    code = detail.get("statusCode")
    if code not in (None, 0):
        raise ResolveError(f"own: TikTok status {code} {detail.get('statusMsg') or ''}".strip())
    item = (detail.get("itemInfo") or {}).get("itemStruct")
    if not item:
        raise ResolveError("own: video not found in page")
    return item


def quality_of(bitrate_info, addr):
    """TikTok names quality by the shorter side, e.g. 720p for 720x1280."""
    w, h = addr.get("Width") or 0, addr.get("Height") or 0
    short = min(w, h) if w and h else 0
    if not short:
        m = re.search(r"_(\d{3,4})_", bitrate_info.get("GearName") or "")
        short = int(m.group(1)) if m else 0
    return short


def own_data(item, cookies):
    video = item.get("video") or {}
    if item.get("imagePost") and not video.get("playAddr"):
        raise ResolveError("own: this is a photo post, not a video")

    media = {
        "headers": {"Referer": "https://www.tiktok.com/"},
        "cookies": cookies,
        "impersonate": True,
        "proxy": bool(PROXY_URL),
    }

    options = []
    for b in video.get("bitrateInfo") or []:
        addr = b.get("PlayAddr") or {}
        urls = addr.get("UrlList") or []
        if not urls:
            continue
        codec = (b.get("CodecType") or "").lower()
        options.append((quality_of(b, addr), "h264" in codec, b.get("Bitrate") or 0, addr, urls[0]))
    # best quality first; within the same quality prefer h264 (plays everywhere)
    options.sort(key=lambda o: (o[0], o[1], o[2]), reverse=True)

    formats, seen = [], set()
    for short, _, _, addr, url in options:
        label = f"{short}p" if short else "Video"
        if label in seen:
            continue
        seen.add(label)
        formats.append({"id": f"o{len(formats)}", "label": label, "kind": "video",
                        "size": to_int(addr.get("DataSize")), "url": url, **media})

    play, dl = video.get("playAddr"), video.get("downloadAddr")
    if not formats and (play or dl):
        formats.append({"id": "o0", "label": video.get("definition") or "Standard", "kind": "video",
                        "size": None, "url": play or dl, **media})
    if dl and all(f["url"] != dl for f in formats):
        formats.append({"id": f"o{len(formats)}", "label": "Original (download link)", "kind": "video",
                        "size": None, "url": dl, **media})
    if not formats:
        raise ResolveError("own: no video links in page")

    audio = (item.get("music") or {}).get("playUrl")
    if audio:
        formats.append({"id": "srcaudio", "label": "Sound", "kind": "audio",
                        "size": None, "url": audio, **media})

    stats = item.get("stats") or {}
    author = item.get("author") or {}
    return {
        "id": str(item.get("id") or "video"),
        "title": (item.get("desc") or "TikTok video").strip(),
        "author": author.get("nickname") or "",
        "handle": author.get("uniqueId") or "",
        "thumbnail": video.get("originCover") or video.get("cover") or video.get("dynamicCover"),
        "duration": to_int(video.get("duration")),
        "views": to_int(stats.get("playCount")),
        "likes": to_int(stats.get("diggCount")),
        "comments": to_int(stats.get("commentCount")),
        "shares": to_int(stats.get("shareCount")),
        "created": iso_date(epoch=item.get("createTime")),
        "formats": formats,
    }


def from_own(url):
    if cffi is None:
        raise ResolveError("own: curl_cffi is not installed")
    sess = cffi.Session(impersonate=IMPERSONATE, proxies=cffi_proxies())
    try:
        r = sess.get(url, headers=PAGE_HEADERS, timeout=25, allow_redirects=True)
        if r.status_code >= 400:
            raise ResolveError(f"own: TikTok returned HTTP {r.status_code}")
        try:
            item = parse_item(r.text)
        except ResolveError:
            # second try through the generic video page, which is sometimes less strict
            m = re.search(r"/(?:video|photo)/(\d+)", str(r.url))
            if not m:
                raise
            r = sess.get(f"https://www.tiktok.com/@i/video/{m.group(1)}", headers=PAGE_HEADERS, timeout=25)
            item = parse_item(r.text)
        cookies = sess.cookies.get_dict()
    finally:
        sess.close()
    return own_data(item, cookies)


PROVIDER_FUNCS = {"own": from_own, "tikwm": from_tikwm, "ytdlp": from_ytdlp}


def finalize(info):
    """Add the cover image as an internal format so it can be downloaded like the rest."""
    if info.get("thumbnail") and not any(f["id"] == "cover" for f in info["formats"]):
        info["formats"].append({
            "id": "cover", "label": "Cover image", "kind": "image", "size": None, "url": info["thumbnail"],
            "headers": {"User-Agent": UA, "Referer": "https://www.tiktok.com/"},
        })
    return info


def resolve(url, fresh=False):
    if not fresh:
        hit = cache_get(url)
        if hit:
            return hit

    errors, started = [], time.time()
    for name in PROVIDERS:
        fn = PROVIDER_FUNCS.get(name)
        if not fn:
            continue
        if errors and time.time() - started > RESOLVE_BUDGET:
            errors.append("out of time")
            break
        try:
            data = fn(url)
            if data and any(f["kind"] == "video" for f in data["formats"]):
                data = finalize(data)
                cache_set(url, data)
                return data
            errors.append(f"{name}: no formats")
        except Exception as exc:  # noqa: BLE001 - we want to try the next provider
            app.logger.warning("provider %s failed for %s: %s", name, url, exc)
            errors.append(f"{name}: {exc}")
    raise ResolveError(" | ".join(errors) or "no provider configured")


def videos_of(info):
    return [f for f in info["formats"] if f["kind"] == "video"]


def find_format(info, fid):
    for f in info["formats"]:
        if f["id"] == fid:
            return f
    return None


def public_formats(info):
    """What the page sees: video qualities, one MP3, and the Document tab entries."""
    vids = videos_of(info)
    out = [{"id": f["id"], "label": f["label"], "kind": "video", "size": f.get("size")} for f in vids]
    out.append({"id": "mp3", "label": "MP3 audio", "kind": "audio", "size": None})
    if vids:
        out.append({"id": "file", "label": "Original video file (MP4)", "kind": "document",
                    "size": vids[0].get("size")})
    if find_format(info, "cover"):
        out.append({"id": "cover", "label": "Cover image", "kind": "document", "size": None})
    out.append({"id": "txt", "label": "Video details (TXT)", "kind": "document", "size": None})
    return out


# ---------------------------------------------------------------------------
# Opening a media link
# ---------------------------------------------------------------------------
def open_upstream(fmt):
    if fmt.get("impersonate"):
        if cffi is None:
            raise ResolveError("curl_cffi is not installed")
        upstream = cffi.get(
            fmt["url"],
            headers={**(fmt.get("headers") or {}), "Range": "bytes=0-"},
            cookies=fmt.get("cookies") or None,
            impersonate=IMPERSONATE,
            stream=True,
            timeout=30,
            proxies=cffi_proxies() if fmt.get("proxy") else None,
        )
        upstream.raise_for_status()
        return upstream

    proxies = {"http": PROXY_URL, "https": PROXY_URL} if fmt.get("proxy") and PROXY_URL else None
    upstream = requests.get(
        fmt["url"],
        headers=fmt.get("headers") or {"User-Agent": UA},
        stream=True,
        timeout=30,
        proxies=proxies,
    )
    upstream.raise_for_status()
    return upstream


def open_format(url, fid):
    """Find the link for this option and open it; retry once with fresh links."""
    last = None
    for fresh in (False, True):
        try:
            info = resolve(url, fresh=fresh)
            fmt = find_format(info, fid)
            if not fmt:
                raise ResolveError("That download option is not available for this video.")
            return info, fmt, open_upstream(fmt)
        except Exception as exc:  # noqa: BLE001
            last = exc
            app.logger.warning("open_format try failed for %s: %s", url, exc)
    raise last


def fmt_duration(seconds):
    if not seconds:
        return "-"
    return f"{seconds // 60}:{seconds % 60:02d}"


def fmt_count(n):
    return f"{n:,}" if isinstance(n, int) else "-"


def build_details(info, url):
    """A plain-text sheet with the video's details (UTF-8, works with any language)."""
    tags = re.findall(r"#[^\s#]+", info["title"])
    who = info["author"] or "-"
    if info["handle"]:
        who += f" (@{info['handle']})"
    lines = [
        "TikTok video details",
        "====================",
        "",
        f"Caption : {info['title']}",
        f"Creator : {who}",
        f"Posted  : {info['created'] or '-'}",
        f"Length  : {fmt_duration(info['duration'])}",
        "",
        f"Views    : {fmt_count(info['views'])}",
        f"Likes    : {fmt_count(info['likes'])}",
        f"Comments : {fmt_count(info['comments'])}",
        f"Shares   : {fmt_count(info['shares'])}",
        "",
        f"Hashtags: {' '.join(tags) if tags else '-'}",
        f"Link    : {url}",
        "",
        "Saved with TikTok Downloader x Rio",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Live download counter
# ---------------------------------------------------------------------------
_count = 0
_count_lock = threading.Lock()
_persist_lock = threading.Lock()


def _upstash(path):
    r = requests.get(f"{UPSTASH_URL}/{path}", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=5)
    r.raise_for_status()
    return r.json().get("result")


def load_count():
    global _count
    n = 0
    try:
        if UPSTASH_URL and UPSTASH_TOKEN:
            n = to_int(_upstash(f"get/{COUNTER_KEY}")) or 0
        elif os.path.exists(COUNTER_FILE):
            with open(COUNTER_FILE) as fh:
                n = to_int(json.load(fh).get("downloads")) or 0
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("could not load the download counter: %s", exc)
    with _count_lock:
        _count = max(_count, n)


def _persist_count():
    global _count
    try:
        with _persist_lock:
            if UPSTASH_URL and UPSTASH_TOKEN:
                n = to_int(_upstash(f"incr/{COUNTER_KEY}"))
                if n:
                    with _count_lock:
                        _count = max(_count, n)
            else:
                with _count_lock:
                    value = _count  # always write the newest number, never an older one
                tmp = COUNTER_FILE + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump({"downloads": value}, fh)
                os.replace(tmp, COUNTER_FILE)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("could not save the download counter: %s", exc)


def bump_count():
    global _count
    with _count_lock:
        _count += 1
    threading.Thread(target=_persist_count, daemon=True).start()


def wants_whole_file():
    """Browsers and phones sometimes probe a file with small ranges; only a full
    request from the start counts as a download."""
    rng = request.headers.get("Range", "").replace(" ", "")
    return request.method == "GET" and rng in ("", "bytes=0-")


load_count()


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------
_ffmpeg = None
_wm_sem = threading.Semaphore(1)  # one ffmpeg at a time keeps the free plan alive
_jobs, _jobs_lock = {}, threading.Lock()
_files, _files_lock = {}, threading.Lock()
_tickets, _tickets_lock = {}, threading.Lock()


def ffmpeg_path():
    global _ffmpeg
    if _ffmpeg is None:
        found = shutil.which("ffmpeg")
        if not found and imageio_ffmpeg:
            try:
                found = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:  # noqa: BLE001
                found = None
        _ffmpeg = found or ""
    return _ffmpeg or None


def watermark_ready():
    return WATERMARK_ENABLED and ffmpeg_path() is not None and os.path.exists(WATERMARK_IMAGE)


def audio_ready():
    return ffmpeg_path() is not None


def probe_size(path):
    p = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", path], capture_output=True, text=True, timeout=30)
    m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", p.stderr)
    if not m:
        raise RuntimeError("could not read the video size")
    return int(m.group(1)), int(m.group(2))


PROGRESS_KEYS = ("frame=", "fps=", "stream_", "bitrate=", "total_size=", "out_time", "dup_frames=",
                 "drop_frames=", "speed=", "progress=")


def run_ffmpeg(cmd, duration, on_progress):
    """Run ffmpeg, report progress, raise RuntimeError with the tail of its error output."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    timer = threading.Timer(FFMPEG_TIMEOUT, proc.kill)
    timer.start()
    tail = deque(maxlen=12)
    try:
        for line in proc.stdout:
            if line.startswith("out_time_ms="):  # (despite the name this is microseconds)
                try:
                    done = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                if duration:
                    on_progress(max(0, min(99, int(done / duration * 100))))
            elif not line.startswith(PROGRESS_KEYS):
                tail.append(line.strip())
        proc.wait()
    finally:
        timer.cancel()
    if proc.returncode != 0:
        if proc.returncode < 0:
            raise RuntimeError("ffmpeg took too long and was stopped")
        raise RuntimeError("ffmpeg failed: " + " ".join(tail)[-300:])


def watermark_filter(width, pos):
    logo_w = max(120, int(width * WATERMARK_SIZE))
    logo_w -= logo_w % 2
    margin = max(10, int(width * 0.03))
    x = f"W-w-{margin}" if pos.endswith("r") else str(margin)
    y = f"H-h-{margin}" if pos.startswith("b") else str(margin)
    # the last two filters make the size even, which libx264 + yuv420p requires
    return (
        f"[1:v]scale={logo_w}:-1,format=rgba,colorchannelmixer=aa={WATERMARK_OPACITY}[wm];"
        f"[0:v][wm]overlay={x}:{y}:format=auto,scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p[v]"
    )


# ---------------------------------------------------------------------------
# Temporary stores: tickets (direct downloads), files (ffmpeg results), jobs
# ---------------------------------------------------------------------------
def purge_files():
    now = time.time()
    with _files_lock:
        for token in [t for t, f in _files.items() if f["exp"] < now]:
            shutil.rmtree(_files.pop(token)["dir"], ignore_errors=True)
        while len(_files) > 20:
            oldest = min(_files, key=lambda t: _files[t]["exp"])
            shutil.rmtree(_files.pop(oldest)["dir"], ignore_errors=True)


def purge_jobs():
    now = time.time()
    with _jobs_lock:
        for jid in [j for j, v in _jobs.items() if now - v["created"] > JOB_TTL]:
            _jobs.pop(jid, None)


def purge_tickets():
    now = time.time()
    with _tickets_lock:
        for token in [t for t, v in _tickets.items() if v["exp"] < now]:
            _tickets.pop(token, None)
        while len(_tickets) > 500:
            _tickets.pop(min(_tickets, key=lambda t: _tickets[t]["exp"]), None)


def register_file(path, name, mime, workdir):
    purge_files()
    token = secrets.token_urlsafe(16)
    with _files_lock:
        _files[token] = {"path": path, "name": name, "mime": mime, "dir": workdir,
                         "exp": time.time() + FILE_TTL, "counted": False}
    return token


def make_ticket(**fields):
    purge_tickets()
    token = secrets.token_urlsafe(16)
    with _tickets_lock:
        _tickets[token] = {"exp": time.time() + TICKET_TTL, "counted": False, "body": None, "mime": None,
                           "count": True, **fields}
    return token


# ---------------------------------------------------------------------------
# Jobs: watermark and MP3 (both need ffmpeg)
# ---------------------------------------------------------------------------
def download_source(job, url, fid, dest):
    """Save one format to disk, with a size limit. Returns (info, fmt)."""
    info, fmt, upstream = open_format(url, fid)
    size = 0
    try:
        with open(dest, "wb") as fh:
            for chunk in upstream.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_SOURCE_MB * 1024 * 1024:
                    raise ResolveError("This video is too large to process.")
                fh.write(chunk)
    finally:
        upstream.close()
    return info, fmt


def job_watermark(job, workdir, url, fid, pos):
    job["stage"] = "downloading"
    src = os.path.join(workdir, "source.mp4")
    info, fmt = download_source(job, url, fid, src)
    if fmt["kind"] != "video":
        raise ResolveError("Only videos can get a watermark.")

    job["stage"] = "watermarking"
    width, _ = probe_size(src)
    out = os.path.join(workdir, "output.mp4")
    last_error = None
    # copy is instant; re-encode the sound only if the MP4 container refuses it
    for audio_args in (["-c:a", "copy"], ["-c:a", "aac", "-b:a", "128k"]):
        cmd = [
            ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y",
            "-i", src, "-i", WATERMARK_IMAGE,
            "-filter_complex", watermark_filter(width, pos),
            "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", FFMPEG_PRESET, "-crf", "28",
            *audio_args, "-movflags", "+faststart", out,
        ]
        try:
            run_ffmpeg(cmd, info.get("duration"), lambda pct: job.update(percent=pct))
            last_error = None
            break
        except RuntimeError as exc:
            last_error = exc
    if last_error:
        raise last_error
    return out, f"tiktok_{safe_name(info['id'])}_rio.mp4", "video/mp4"


def job_audio(job, workdir, url):
    """Make a real MP3. TikTok's sound link is often AAC/M4A, so it is always converted."""
    info = resolve(url)
    sources = [f["id"] for f in info["formats"] if f["kind"] == "audio"]
    vids = videos_of(info)
    if vids:
        sources.append(vids[-1]["id"])  # the smallest video is enough to read the sound from
    title = re.sub(r"[\r\n\x00]", " ", info["title"])[:120]
    artist = info["author"] or info["handle"] or "TikTok"
    meta = ["-metadata", f"title={title}", "-metadata", f"artist={artist}", "-metadata", "album=TikTok"]
    base = f"tiktok_{safe_name(info['id'])}_audio"
    last_error = None

    for idx, fid in enumerate(sources):
        src = os.path.join(workdir, f"source{idx}")
        try:
            job["stage"] = "downloading"
            download_source(job, url, fid, src)
            job["stage"] = "converting"
            for ext, codec in (("mp3", ["-c:a", "libmp3lame", "-q:a", "2", "-id3v2_version", "3"]),
                               ("m4a", ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"])):
                out = os.path.join(workdir, f"audio{idx}.{ext}")
                cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y",
                       "-i", src, "-vn", "-map", "0:a:0", "-map_metadata", "-1", *codec, *meta, out]
                try:
                    run_ffmpeg(cmd, info.get("duration"), lambda pct: job.update(percent=pct))
                    if os.path.getsize(out) > 1024:
                        return out, f"{base}.{ext}", "audio/mpeg" if ext == "mp3" else "audio/mp4"
                except RuntimeError as exc:
                    last_error = exc
        except ResolveError as exc:
            last_error = exc
        except Exception as exc:  # noqa: BLE001
            app.logger.warning("audio source %s failed for %s: %s", fid, url, exc)
            last_error = exc
    app.logger.error("audio failed for %s: %s", url, last_error)
    if isinstance(last_error, ResolveError):
        raise last_error
    raise ResolveError("Couldn't get the sound from this video. It may have no audio.")


def run_job(job_id, mode, url, fid, pos):
    job = _jobs[job_id]
    workdir = tempfile.mkdtemp(prefix="job_")
    got_slot = False
    try:
        if not _wm_sem.acquire(timeout=180):
            raise ResolveError("The server is busy. Try again in a minute.")
        got_slot = True
        if mode == "audio":
            out, name, mime = job_audio(job, workdir, url)
        else:
            out, name, mime = job_watermark(job, workdir, url, fid, pos)
        token = register_file(out, name, mime, workdir)
        workdir = None  # the file registry owns the folder from now on
        job.update(state="done", stage="done", percent=100, file=f"/file/{token}")
    except ResolveError as exc:
        job.update(state="error", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("%s job failed for %s", mode, url)
        what = "the sound" if mode == "audio" else "the watermark"
        msg = f"Couldn't prepare {what}. Try again" + ("." if mode == "audio" else ", or switch the watermark off.")
        job.update(state="error", error=msg + (f" ({str(exc)[:200]})" if DEBUG_ERRORS else ""))
    finally:
        if got_slot:
            _wm_sem.release()
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Static assets get a version so a new deploy is never served from an old cache
# ---------------------------------------------------------------------------
def asset_version():
    newest = 0
    static = os.path.join(BASE_DIR, "static")
    try:
        for name in os.listdir(static):
            newest = max(newest, int(os.path.getmtime(os.path.join(static, name))))
    except OSError:
        pass
    return newest


ASSET_V = asset_version()


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    if resp.mimetype == "text/html":
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; img-src 'self' data: https:; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'",
        )
    return resp


API_PREFIXES = ("/api/", "/dl/", "/file/")


@app.errorhandler(Exception)
def handle_any_error(exc):
    """The API always answers in JSON, so the page can show a clear message."""
    api = request.path.startswith(API_PREFIXES)
    if isinstance(exc, HTTPException):
        if not api:
            return exc
        messages = {404: "Not found.", 405: "That request type is not allowed.", 413: "The request was too large.",
                    400: "The request was not understood."}
        return error(messages.get(exc.code, "The request failed."), exc.code or 500)
    app.logger.exception("unhandled error on %s", request.path)
    if api:
        return error("Something went wrong on our side. Try again.", 500, exc)
    return Response("Something went wrong. Please try again.", status=500, mimetype="text/plain")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html", v=ASSET_V)


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/debug/ip")
def debug_ip():
    """Only with DEBUG_ERRORS=1: shows which address the rate limit sees (to tune PROXY_HOPS)."""
    if not DEBUG_ERRORS:
        return error("Not found.", 404)
    return jsonify({"seen_as": client_ip(), "remote_addr": request.remote_addr,
                    "x_forwarded_for": request.headers.get("X-Forwarded-For", ""), "proxy_hops": PROXY_HOPS})


@app.post("/api/info")
def api_info():
    if rate_limited():
        return error("Too many requests. Wait a minute and try again.", 429)

    url = extract_url(text_field(json_body(), "url"))
    if not is_tiktok_url(url):
        return error("That doesn't look like a TikTok link.", 400)

    try:
        info = resolve(url)
    except ResolveError as exc:
        app.logger.error("resolve failed for %s: %s", url, exc)
        return error(
            "Couldn't get this video. It may be private or removed, or the download service is busy. "
            "Try again in a moment.", 422, exc)

    return jsonify({
        **{k: v for k, v in info.items() if k != "formats"},
        "formats": public_formats(info),
        "watermark": watermark_ready(),
        "positions": list(WATERMARK_POSITIONS),
        "position": WATERMARK_DEFAULT_POS,
    })


@app.post("/api/prepare")
def api_prepare():
    """Start any download. Replies {"file": ...} (ready now) or {"job": ...} (poll it)."""
    if rate_limited():
        return error("Too many requests. Wait a minute and try again.", 429)

    data = json_body()
    url = extract_url(text_field(data, "url"))
    fid = text_field(data, "f", 24)
    pos = text_field(data, "pos", 2)
    want_wm = data.get("wm") is True
    if not is_tiktok_url(url):
        return error("That doesn't look like a TikTok link.", 400)
    if not FORMAT_ID_RE.match(fid):
        return error("Unknown download option.", 400)
    if pos not in WATERMARK_POSITIONS:
        pos = WATERMARK_DEFAULT_POS

    # ---- ffmpeg work: MP3, or a video with the ghost watermark ------------------------------
    is_video_id = fid not in ("mp3", "file", "cover", "txt", "srcaudio")
    if fid == "mp3" or (is_video_id and want_wm):
        if fid == "mp3" and not audio_ready():
            return error("MP3 conversion isn't available on this server.", 501)
        if fid != "mp3" and not watermark_ready():
            return error("Watermarking isn't available on this server.", 501)
        purge_jobs()
        with _jobs_lock:
            if sum(1 for j in _jobs.values() if j["state"] == "working") >= MAX_ACTIVE_JOBS:
                return error("Lots of videos are being processed right now. Try again in a minute.", 503)
            job_id = secrets.token_urlsafe(8)
            _jobs[job_id] = {"state": "working", "stage": "queued", "percent": 0, "created": time.time()}
        mode = "audio" if fid == "mp3" else "watermark"
        threading.Thread(target=run_job, args=(job_id, mode, url, fid, pos), daemon=True).start()
        return jsonify({"job": job_id})

    # ---- direct downloads: check that the link really works, then hand out a ticket ----------
    try:
        info = resolve(url)
    except ResolveError as exc:
        app.logger.error("resolve failed for %s: %s", url, exc)
        return error("Couldn't get this video. It may be private or removed. Try again in a moment.", 422, exc)

    sid = safe_name(info["id"])
    if fid == "txt":
        body = build_details(info, url).encode("utf-8-sig")  # BOM keeps Sinhala readable in old Notepad
        token = make_ticket(body=body, mime="text/plain; charset=utf-8", name=f"tiktok_{sid}_details.txt",
                            count=False)
        return jsonify({"file": f"/dl/{token}"})

    if fid == "file":
        vids = videos_of(info)
        if not vids:
            return error("No video file is available for this link.", 404)
        target, name, mime = vids[0]["id"], f"tiktok_{sid}_original.mp4", "application/octet-stream"
    elif fid == "cover":
        if not find_format(info, "cover"):
            return error("This video has no cover image.", 404)
        target, name, mime = "cover", f"tiktok_{sid}_cover", None  # extension comes from the real file type
    else:
        fmt = find_format(info, fid)
        if not fmt or fmt["kind"] != "video":
            return error("Unknown download option.", 400)
        target, name, mime = fid, f"tiktok_{sid}_{fid}.mp4", None

    try:
        _, _, probe = open_format(url, target)  # fails here, with a message, if TikTok refuses the link
        probe.close()
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("probe failed for %s (%s): %s", url, target, exc)
        return error("Couldn't download this one. Try again in a moment.", 502, exc)

    token = make_ticket(url=url, fid=target, name=name, mime=mime, count=fid != "cover")
    return jsonify({"file": f"/dl/{token}"})


@app.get("/dl/<token>")
def serve_ticket(token):
    purge_tickets()
    with _tickets_lock:
        t = _tickets.get(token)
    if not t:
        return error("This download link has expired. Start the download again.", 404)

    def count_once():
        if t["count"] and not t["counted"] and wants_whole_file():
            t["counted"] = True
            bump_count()

    if t["body"] is not None:
        count_once()
        resp = Response(t["body"], content_type=t["mime"])
        resp.headers["Content-Disposition"] = f'attachment; filename="{t["name"]}"'
        resp.headers["Cache-Control"] = "no-store"
        return resp

    try:
        _, fmt, upstream = open_format(t["url"], t["fid"])
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("download failed for %s", t["url"])
        return error("Couldn't download this one. Start it again in a moment.", 502, exc)
    count_once()

    def stream():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    up_type = (upstream.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    name = t["name"]
    if fmt["kind"] == "image":
        ctype = up_type if up_type.startswith("image/") else "image/jpeg"
        name += {"image/webp": ".webp", "image/png": ".png"}.get(ctype, ".jpg")
    else:
        ctype = t["mime"] or (up_type if up_type.startswith(("video/", "audio/")) else "video/mp4")

    resp = Response(stream(), content_type=ctype)
    resp.headers["Content-Disposition"] = f'attachment; filename="{name}"'
    resp.headers["Cache-Control"] = "no-store"
    if upstream.headers.get("Content-Length"):
        resp.headers["Content-Length"] = upstream.headers["Content-Length"]
    return resp


@app.get("/api/stats")
def api_stats():
    resp = jsonify({"downloads": _count})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/job/<job_id>")
def api_job(job_id):
    job = _jobs.get(job_id)
    if not job:
        return error("This job has expired. Start the download again.", 404)
    resp = jsonify({k: job.get(k) for k in ("state", "stage", "percent", "file", "error")})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/file/<token>")
def serve_file(token):
    purge_files()
    with _files_lock:
        item = _files.get(token)
    if not item or not os.path.exists(item["path"]):
        return error("This download link has expired. Start the download again.", 404)
    if not item["counted"] and wants_whole_file():
        item["counted"] = True
        bump_count()
    return send_file(item["path"], as_attachment=True, download_name=item["name"], mimetype=item["mime"], max_age=0)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=_int_env("PORT", 5000), debug=os.environ.get("FLASK_DEBUG") == "1")
