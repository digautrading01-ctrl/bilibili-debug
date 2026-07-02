import os
import re
import uuid
import json
import time
import shutil
import sys
import threading
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, quote

from flask import Flask, render_template, request, jsonify, send_file, Response
import yt_dlp
from yt_dlp import cookies as ytdlp_cookies

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
COOKIES_FILE = BASE_DIR / "cookies.txt"

# Common subtitle extensions. We do not strictly limit subtitle formats to this list
# since some sites (including Bilibili) may expose subtitles in formats like `json3`.
_SUBTITLE_EXTS = {".srt", ".vtt", ".ass", ".ssa", ".ttml", ".json", ".json3", ".xml"}

# ---------------------------------------------------------------------------
# Thread-safe task store
# ---------------------------------------------------------------------------
tasks: dict = {}
tasks_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Bilibili quality labels (quality-id → human label)
# ---------------------------------------------------------------------------
BILIBILI_QUALITY_LABELS: dict[int, str] = {
    127: "8K Ultra HD (4320p)",
    126: "Dolby Vision (2160p)",
    125: "HDR Real (2160p)",
    120: "4K Ultra HD (2160p)",
    116: "1080P60 High Frame Rate",
    112: "1080P+ High Bandwidth",
    80:  "1080P High Definition",
    74:  "720P60 High Frame Rate",
    64:  "720P High Definition",
    32:  "480P Clear",
    16:  "360P Smooth",
}

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

_BILIBILI_HOSTS = frozenset([
    "bilibili.com",
    "www.bilibili.com",
    "m.bilibili.com",
    "b23.tv",
    "bili2233.cn",
])

_COOKIE_DOMAINS = (
    ".bilibili.com",
    "bilibili.com",
    ".b23.tv",
    "b23.tv",
)

_THUMBNAIL_HOST_SUFFIXES = (
    ".hdslb.com",
    "hdslb.com",
    ".biliimg.com",
    "biliimg.com",
    ".bilibili.com",
    "bilibili.com",
)

_DEFAULT_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}

_BROWSER_COOKIE_RESULT_CACHE: dict | None = None
_BROWSER_COOKIE_CHECKED = False

_BROWSER_LABELS = {
    "safari": "Safari",
    "edge": "Edge",
    "chrome": "Chrome",
    "firefox": "Firefox",
    "brave": "Brave",
    "opera": "Opera",
    "vivaldi": "Vivaldi",
}

_BROWSER_COOKIE_SOURCES = (
    ("safari", "edge", "chrome", "firefox", "brave", "opera", "vivaldi")
    if sys.platform == "darwin" else
    ("edge", "chrome", "firefox", "brave", "opera", "vivaldi", "safari")
)


def is_bilibili_url(url: str) -> bool:
    try:
        host = urlparse(url.strip()).netloc.lower()
        # Strip port if present
        host = host.split(":")[0]
        return host in _BILIBILI_HOSTS or host.endswith(".bilibili.com")
    except Exception:
        return False


def normalize_bilibili_url(url: str) -> str:
    """
    Normalise a Bilibili URL:
      - Ensure https scheme
      - Normalise hostname to www.bilibili.com (or keep b23.tv as-is)
      - Strip path trailing slash
      - Keep only the 'p' (part/episode) query parameter; discard tracking params
    """
    url = url.strip()

    # Short-link: keep as-is — yt-dlp follows the redirect itself
    parsed = urlparse(url)
    if "b23.tv" in parsed.netloc.lower():
        return url

    # Ensure scheme
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)

    # Normalise host to www.bilibili.com
    host = re.sub(r"^(www\.|m\.)", "", parsed.netloc.lower().split(":")[0])
    netloc = "www." + host

    # Keep only the 'p' query param (multi-part playlist index)
    params = parse_qs(parsed.query)
    clean: dict = {}
    if "p" in params:
        clean["p"] = params["p"][0]

    return urlunparse((
        "https",
        netloc,
        parsed.path.rstrip("/"),
        "",
        urlencode(clean),
        "",
    ))


def resolve_bilibili_url(url: str) -> str:
    """
    Resolve supported short links such as b23.tv to the final Bilibili URL
    before passing them into yt-dlp.
    """
    url = url.strip()
    if not urlparse(url).scheme:
        url = "https://" + url

    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0]

    if host != "b23.tv":
        return normalize_bilibili_url(url)

    request_obj = Request(url, headers=_DEFAULT_HTTP_HEADERS, method="GET")
    try:
        with urlopen(request_obj, timeout=10) as response:
            final_url = response.geturl()
    except (HTTPError, URLError, TimeoutError):
        return url

    if is_bilibili_url(final_url):
        return normalize_bilibili_url(final_url)
    return url


def _cookie_matches_targets(cookie) -> bool:
    domain = (getattr(cookie, "domain", "") or "").lower()
    if not domain:
        return False

    return any(
        domain == target.lstrip(".") or domain.endswith(target)
        for target in _COOKIE_DOMAINS
    )


def _cookiejar_has_target_cookies(cookie_jar) -> bool:
    return any(_cookie_matches_targets(cookie) for cookie in cookie_jar)


def _browser_label(browser_name: str) -> str:
    return _BROWSER_LABELS.get(browser_name, browser_name.title())


def _detect_browser_cookie_source() -> dict:
    """
    Try common local browsers in order and return the first browser name whose
    profile contains Bilibili cookies that yt-dlp can read directly.
    """
    global _BROWSER_COOKIE_RESULT_CACHE, _BROWSER_COOKIE_CHECKED

    if _BROWSER_COOKIE_CHECKED:
        return _BROWSER_COOKIE_RESULT_CACHE or {"browser": None, "issues": []}

    issues: list[dict] = []

    for browser_name in _BROWSER_COOKIE_SOURCES:
        try:
            cookie_jar = ytdlp_cookies.extract_cookies_from_browser(browser_name)
        except PermissionError as exc:
            issues.append({
                "browser": browser_name,
                "kind": "permission",
                "error": str(exc),
            })
            continue
        except Exception:
            continue

        if _cookiejar_has_target_cookies(cookie_jar):
            _BROWSER_COOKIE_RESULT_CACHE = {
                "browser": browser_name,
                "issues": issues,
            }
            _BROWSER_COOKIE_CHECKED = True
            return _BROWSER_COOKIE_RESULT_CACHE

    _BROWSER_COOKIE_RESULT_CACHE = {
        "browser": None,
        "issues": issues,
    }
    _BROWSER_COOKIE_CHECKED = True
    return _BROWSER_COOKIE_RESULT_CACHE


def _resolve_auth_context() -> dict:
    """
    Determine which authentication source will be used and return both the
    yt-dlp options and a small UI-facing status payload.
    """
    if COOKIES_FILE.exists():
        return {
            "mode": "cookiefile",
            "has_auth": True,
            "source": "cookies.txt",
            "level": "info",
            "message": "Using `cookies.txt` from the project root for authenticated access.",
            "yt_dlp_opts": {"cookiefile": str(COOKIES_FILE)},
        }

    browser_result = _detect_browser_cookie_source()
    browser_name = browser_result.get("browser")

    if browser_name:
        browser_label = _browser_label(browser_name)
        return {
            "mode": "browser",
            "has_auth": True,
            "source": f"{browser_label} cookies",
            "level": "info",
            "message": f"Using local {browser_label} cookies for authenticated access.",
            "yt_dlp_opts": {"cookiesfrombrowser": (browser_name,)},
        }

    safari_permission_issue = next(
        (
            issue for issue in browser_result.get("issues", [])
            if issue.get("browser") == "safari" and issue.get("kind") == "permission"
        ),
        None,
    )

    if safari_permission_issue:
        return {
            "mode": "none",
            "has_auth": False,
            "source": None,
            "level": "warning",
            "message": (
                "Safari cookies could not be read because macOS denied access. "
                "Grant Full Disk Access to the app running Python, or place an "
                "exported `cookies.txt` file in the project root."
            ),
            "yt_dlp_opts": {},
        }

    return {
        "mode": "none",
        "has_auth": False,
        "source": None,
        "level": "info",
        "message": (
            "No Bilibili login cookies were found. Login-gated qualities such as "
            "1080p may be unavailable unless you add `cookies.txt` or log in via "
            "a supported local browser profile."
        ),
        "yt_dlp_opts": {},
    }


def _resolve_auth_opts() -> dict:
    """
    Prefer an explicit cookies.txt file. If it is not present, try the current
    browser profile through yt-dlp's native browser-cookie support.
    """
    return dict(_resolve_auth_context()["yt_dlp_opts"])


def _sanitize_lang(lang: str) -> str | None:
    lang = (lang or "").strip()
    if not lang:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", lang):
        return None
    return lang


def _sanitize_kind(kind: str) -> str | None:
    kind = (kind or "").strip().lower()
    if kind in ("manual", "auto"):
        return kind
    return None


def _sanitize_sub_ext(ext: str) -> str | None:
    ext = (ext or "").strip().lower().lstrip(".")
    if not ext:
        return None
    # keep tight: subtitle formats are simple alphanumerics
    if not re.fullmatch(r"[a-z0-9]{1,10}", ext):
        return None
    return ext


def _extract_subtitle_options(info: dict) -> list[dict]:
    """
    Return a flattened list of available subtitle options:
      - manual subtitles: info["subtitles"]
      - auto captions: info["automatic_captions"]
    Each option is identified by {kind}:{lang}:{ext}
    """
    out: list[dict] = []
    seen: set[str] = set()

    def add(kind: str, mapping: dict | None):
        if not isinstance(mapping, dict):
            return
        for lang, items in mapping.items():
            lang_s = _sanitize_lang(str(lang))
            if not lang_s or not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                ext = item.get("ext")
                # Do not filter out site-specific formats (e.g. bilibili `json3`);
                # just validate they are safe to echo back to the client.
                ext_s = _sanitize_sub_ext(str(ext)) if ext else None
                if not ext_s:
                    continue
                opt_id = f"{kind}:{lang_s}:{ext_s}"
                if opt_id in seen:
                    continue
                seen.add(opt_id)
                out.append({
                    "id": opt_id,
                    "kind": kind,
                    "lang": lang_s,
                    "ext": ext_s,
                })

    add("manual", info.get("subtitles"))
    add("auto", info.get("automatic_captions"))

    # Sort: manual first, then auto. Within, group by lang then ext
    out.sort(key=lambda x: (0 if x["kind"] == "manual" else 1, x["lang"], x["ext"]))
    return out


def _is_allowed_thumbnail_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().split(":")[0]
        return (
            parsed.scheme in ("https", "http")
            and any(host == suffix.lstrip(".") or host.endswith(suffix) for suffix in _THUMBNAIL_HOST_SUFFIXES)
        )
    except Exception:
        return False


def _best_thumbnail_url(info: dict) -> str:
    thumbnails = [
        item.get("url")
        for item in info.get("thumbnails", [])
        if item.get("url")
    ]
    if thumbnails:
        return thumbnails[-1]
    return info.get("thumbnail") or ""

# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------

def _base_ydl_opts(extra: dict | None = None) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "http_headers": dict(_DEFAULT_HTTP_HEADERS),
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "continuedl": True,
        "concurrent_fragment_downloads": 1,
    }
    opts.update(_resolve_auth_opts())
    if extra:
        opts.update(extra)
    return opts


def _quality_num(format_id: str) -> int | None:
    """Extract the Bilibili quality number from a yt-dlp format_id."""
    try:
        return int(format_id.split("-")[0])
    except (ValueError, AttributeError):
        return None

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/thumbnail")
def api_thumbnail():
    source_url = (request.args.get("url") or "").strip()
    if not source_url or not _is_allowed_thumbnail_url(source_url):
        return jsonify({"error": "Invalid thumbnail URL."}), 400

    request_obj = Request(source_url, headers=_DEFAULT_HTTP_HEADERS, method="GET")
    try:
        with urlopen(request_obj, timeout=15) as response:
            content = response.read()
            mimetype = response.headers.get_content_type() or "image/jpeg"
    except (HTTPError, URLError, TimeoutError) as exc:
        return jsonify({"error": f"Thumbnail fetch failed: {exc}"}), 502

    return Response(
        content,
        mimetype=mimetype,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.route("/api/info", methods=["POST"])
def api_info():
    """
    POST { "url": "..." }
    Returns video metadata + list of available formats.
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({"error": "URL is required."}), 400
    if not is_bilibili_url(url):
        return jsonify({"error": "Only Bilibili URLs are supported (bilibili.com, b23.tv)."}), 400

    url = resolve_bilibili_url(url)
    auth_context = _resolve_auth_context()

    try:
        # Some extractors (including Bilibili in certain cases) may not populate
        # subtitle metadata unless subtitle options are enabled. We enable both
        # manual and automatic subtitle metadata extraction here, but still do
        # not download any media because download=False below.
        opts = _base_ydl_opts({
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        subtitle_options = _extract_subtitle_options(info)

        # Build a deduplicated, sorted list of video formats
        formats = []
        seen_ids: set = set()

        for fmt in info.get("formats", []):
            fid = str(fmt.get("format_id", ""))
            vcodec = fmt.get("vcodec", "none")
            height = fmt.get("height")

            # Skip audio-only and unknown-height entries
            if vcodec == "none" or not height:
                continue
            if fid in seen_ids:
                continue
            seen_ids.add(fid)

            qnum = _quality_num(fid)
            label = BILIBILI_QUALITY_LABELS.get(qnum, f"{height}p")

            formats.append({
                "id": fid,
                "label": label,
                "height": height,
                "fps": fmt.get("fps"),
                "vcodec": vcodec,
                "acodec": fmt.get("acodec", "none"),
                "ext": fmt.get("ext", "mp4"),
                "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
            })

        # Sort highest quality first
        formats.sort(key=lambda x: (x["height"] or 0, x["fps"] or 0), reverse=True)

        thumbnail_url = _best_thumbnail_url(info)
        if thumbnail_url and _is_allowed_thumbnail_url(thumbnail_url):
            thumbnail_url = f"/api/thumbnail?url={quote(thumbnail_url, safe='')}"
        else:
            thumbnail_url = ""

        return jsonify({
            "title": info.get("title") or "Unknown Title",
            "thumbnail": thumbnail_url,
            "duration": info.get("duration"),
            "uploader": info.get("uploader") or "",
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
            "url": url,
            "formats": formats,
            "subtitle_options": subtitle_options,
            "auth": {
                "mode": auth_context["mode"],
                "has_auth": auth_context["has_auth"],
                "source": auth_context["source"],
                "level": auth_context["level"],
                "message": auth_context["message"],
            },
        })

    except yt_dlp.utils.DownloadError as exc:
        msg = re.sub(r"^ERROR:\s*", "", str(exc)).strip()
        return jsonify({"error": msg}), 400
    except Exception as exc:
        return jsonify({"error": f"Unexpected error: {exc}"}), 500


@app.route("/api/download", methods=["POST"])
def api_download():
    """
    POST { "url": "...", "format_id": "80" }
    Spawns a background download thread, returns { "task_id": "..." }.
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    format_id = str(data.get("format_id") or "").strip()

    if not url or not format_id:
        return jsonify({"error": "url and format_id are required."}), 400

    if not is_bilibili_url(url):
        return jsonify({"error": "Only Bilibili URLs are supported (bilibili.com, b23.tv)."}), 400

    url = resolve_bilibili_url(url)

    task_id = str(uuid.uuid4())

    with tasks_lock:
        tasks[task_id] = {
            "status": "pending",
            "percent": 0.0,
            "speed": None,
            "eta": None,
            "downloaded": 0,
            "total": 0,
            "filepath": None,
            "filename": None,
            "error": None,
        }

    threading.Thread(
        target=_download_worker,
        args=(task_id, url, format_id),
        daemon=True,
    ).start()

    return jsonify({"task_id": task_id})


@app.route("/api/progress/<task_id>")
def api_progress(task_id: str):
    """
    Server-Sent Events stream reporting download progress.
    Closes automatically once status is 'done' or 'error'.
    """
    def generate():
        while True:
            with tasks_lock:
                task = tasks.get(task_id)

            if task is None:
                yield f"data: {json.dumps({'status': 'not_found'})}\n\n"
                return

            # Omit local filepath from client payload
            payload = {k: v for k, v in task.items() if k != "filepath"}
            yield f"data: {json.dumps(payload)}\n\n"

            if task["status"] in ("done", "error"):
                return

            time.sleep(0.4)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable Nginx buffering
        },
    )


@app.route("/api/file/<task_id>")
def api_file(task_id: str):
    """Serve the downloaded file as an attachment."""
    with tasks_lock:
        task = tasks.get(task_id)

    if not task or task["status"] != "done":
        return jsonify({"error": "File not ready or task not found."}), 404

    filepath = task.get("filepath")
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "File missing on disk."}), 404

    return send_file(
        filepath,
        as_attachment=True,
        download_name=task["filename"],
    )


@app.route("/api/subtitle")
def api_subtitle():
    """
    Download a single subtitle file *separately* from the video.

    Query params:
      - url: Bilibili video URL
      - kind: "manual" | "auto"
      - lang: subtitle language code, e.g. "en", "zh-Hans"
      - ext: desired subtitle format extension, e.g. "srt", "vtt"
    """
    raw_url = (request.args.get("url") or "").strip()
    kind = _sanitize_kind(request.args.get("kind") or "")
    lang = _sanitize_lang(request.args.get("lang") or "")
    ext = _sanitize_sub_ext(request.args.get("ext") or "")

    if not raw_url:
        return jsonify({"error": "url is required."}), 400
    if not is_bilibili_url(raw_url):
        return jsonify({"error": "Only Bilibili URLs are supported (bilibili.com, b23.tv)."}), 400
    if not kind or not lang or not ext:
        return jsonify({"error": "kind, lang, and ext are required (and must be valid)."}), 400

    url = resolve_bilibili_url(raw_url)

    tmp_root = BASE_DIR / ".runtime" / "subtitles"
    tmp_dir = tmp_root / str(uuid.uuid4())
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        extra: dict = {
            "skip_download": True,
            "outtmpl": str(tmp_dir / "%(title)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            # Subtitles are optional and downloaded only via this endpoint
            "writesubtitles": True,
            "writeautomaticsub": (kind == "auto"),
            "subtitleslangs": [lang],
            "subtitlesformat": ext,
        }

        # Convert subtitles only for formats FFmpeg can reasonably handle.
        # Some sources expose subtitles as JSON (e.g. `json3`) that cannot be
        # converted by FFmpeg; in those cases we will return the original file.
        if ext in ("srt", "vtt", "ass", "ssa", "ttml"):
            extra["postprocessors"] = [
                {"key": "FFmpegSubtitlesConvertor", "format": ext},
            ]

        opts = _base_ydl_opts(extra)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        candidates = [p for p in sorted(tmp_dir.iterdir()) if p.is_file()]
        wanted_suffix = f".{ext}"
        subtitle_files = [p for p in candidates if p.suffix.lower() == wanted_suffix]
        if not subtitle_files:
            # Fall back to any subtitle-like file if conversion didn't happen
            subtitle_files = [p for p in candidates if p.suffix.lower() in _SUBTITLE_EXTS]
        if not subtitle_files and candidates:
            # Last resort: return whatever file yt-dlp actually produced
            subtitle_files = candidates

        if not subtitle_files:
            return jsonify({"error": "No subtitle file was produced for the selected option."}), 404

        # Prefer a file that includes the language code in the name
        preferred = next(
            (p for p in subtitle_files if f".{lang}." in p.name),
            subtitle_files[0],
        )

        content = preferred.read_bytes()
        filename = preferred.name

        # Clean up tmp output
        shutil.rmtree(tmp_dir, ignore_errors=True)

        mimetype = {
            "srt": "application/x-subrip",
            "vtt": "text/vtt",
            "ass": "text/plain",
            "ssa": "text/plain",
            "ttml": "application/ttml+xml",
            "json": "application/json",
            "json3": "application/json",
            "xml": "application/xml",
        }.get(ext, "text/plain")

        return send_file(
            BytesIO(content),
            mimetype=mimetype,
            as_attachment=True,
            download_name=filename,
            max_age=0,
        )

    except yt_dlp.utils.DownloadError as exc:
        msg = re.sub(r"^ERROR:\s*", "", str(exc)).strip()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": msg}), 400
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": f"Unexpected error: {exc}"}), 500


@app.route("/api/cleanup/<task_id>", methods=["DELETE"])
def api_cleanup(task_id: str):
    """Delete the task and its downloaded files from disk."""
    with tasks_lock:
        task = tasks.pop(task_id, None)

    if task:
        task_dir = DOWNLOAD_DIR / task_id
        if task_dir.exists():
            shutil.rmtree(task_dir, ignore_errors=True)

    return jsonify({"ok": True})

# ---------------------------------------------------------------------------
# Background download worker
# ---------------------------------------------------------------------------

def _make_progress_hook(task_id: str):
    def hook(d: dict):
        with tasks_lock:
            if task_id not in tasks:
                return
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                percent = (downloaded / total * 100.0) if total else 0.0
                tasks[task_id].update({
                    "status": "downloading",
                    "percent": round(percent, 1),
                    "speed": d.get("speed"),
                    "eta": d.get("eta"),
                    "downloaded": downloaded,
                    "total": total,
                })
            elif status == "finished":
                # yt-dlp fires "finished" for each stream before post-processing
                tasks[task_id]["status"] = "processing"
    return hook


def _download_worker(task_id: str, url: str, format_id: str):
    task_dir = DOWNLOAD_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Request the selected video quality + best available audio.
        # Fall-back chain ensures we always get *something*:
        #   1. exact format_id + best audio (DASH merge)
        #   2. best video at that format_id (combined stream)
        #   3. overall best available
        fmt_spec = (
            f"{format_id}+bestaudio"
            f"/bestvideo[format_id={format_id}]+bestaudio"
            f"/best"
        )

        opts = _base_ydl_opts({
            "format": fmt_spec,
            "outtmpl": str(task_dir / "%(title)s.%(ext)s"),
            "merge_output_format": "mp4",
            "progress_hooks": [_make_progress_hook(task_id)],
            "quiet": True,
            "no_warnings": True,
            # Embed thumbnail + metadata as bonus
            "writethumbnail": False,
            "postprocessors": [
                {
                    "key": "FFmpegVideoRemuxer",
                    "preferedformat": "mp4",
                }
            ],
        })

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        # Find the video output file (subtitles / metadata may also exist)
        files = [p for p in sorted(task_dir.iterdir()) if p.is_file()]
        if not files:
            raise FileNotFoundError("Download produced no output file.")

        preferred_video_exts = (".mp4", ".mkv", ".webm", ".mov")
        video_files = [p for p in files if p.suffix.lower() in preferred_video_exts]
        filepath = (video_files[0] if video_files else files[0])

        with tasks_lock:
            tasks[task_id].update({
                "status": "done",
                "percent": 100.0,
                "filepath": str(filepath),
                "filename": filepath.name,
            })

    except Exception as exc:
        error_msg = re.sub(r"^ERROR:\s*", "", str(exc)).strip()
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id].update({
                    "status": "error",
                    "error": error_msg,
                })
        # Clean up partial files on failure
        shutil.rmtree(task_dir, ignore_errors=True)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
