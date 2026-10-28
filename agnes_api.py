"""Agnes Studio API layer.

Everything that talks to the Agnes AI video API lives here, so the CLI
(agnes_video.py), the local server (agnes_server.py) and the offline tests
all share one implementation.

Standard library only.

Typical use::

    import agnes_api as api
    api.load_dotenv()
    payload = api.build_payload(prompt="a neon city at night")
    created = api.create_task(payload)
    ids = api.task_ids(created)
    snap = api.wait_for_video(ids["video_id"], model_name=ids["model"])
    api.download(snap["video_url"], "output/clip.mp4")

API contract (verified against agnes-ai.com/en/docs/agnes-video-25 and
wiki.agnes-ai.com/en/docs/agnes-video-25-flash):

    POST {base}/v1/videos                       create task
    GET  {base}/agnesapi?video_id=&model_name=  poll task
    GET  {base}/v1/models                       validate key / list models
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "1.0.0"

DEFAULT_BASE_URL = "https://apihub.agnes-ai.com"
DEFAULT_MODEL = "agnes-video-2.5-flash"

KNOWN_MODELS = {
    "agnes-video-2.5-flash": {
        "label": "Agnes Video 2.5 Flash",
        "sizes": ["720P"],
        "notes": "Fast, fixed 720P.",
    },
    "agnes-video-2.5": {
        "label": "Agnes Video 2.5",
        "sizes": ["720P", "1080P", "1K", "2K"],
        "notes": "Full quality tiers.",
    },
    "agnes-video-v2.0": {
        "label": "Agnes Video v2.0 (legacy)",
        "sizes": ["720P", "1080P", "1K", "2K"],
        "notes": "Legacy width/height/num_frames/frame_rate payload.",
    },
}

MODES = ("text", "keyframe", "reference")
SIZES = ("720P", "1080P", "1K", "2K")
ASPECT_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")
SECONDS_MIN, SECONDS_MAX = 4, 12
DEFAULT_SECONDS = 5
DEFAULT_SIZE = "720P"
DEFAULT_ASPECT = "16:9"

MEDIA_FIELDS = ("first_frame", "last_frame", "images", "audios", "videos")

TERMINAL_OK = {"completed", "complete", "succeeded", "success", "done", "finished", "ready"}
TERMINAL_FAIL = {"failed", "fail", "failure", "error", "cancelled", "canceled", "expired"}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif", ".heic", ".tif", ".tiff"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".flac", ".opus", ".aiff", ".wma"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi", ".mpeg", ".mpg"}

# Documented output pixel mapping (used only for the legacy v2.0 payload).
DIMENSIONS = {
    "720P": {"21:9": (1470, 630), "16:9": (1280, 720), "4:3": (1112, 834),
             "1:1": (960, 960), "3:4": (834, 1112), "9:16": (720, 1280)},
    "1080P": {"21:9": (2206, 946), "16:9": (1920, 1080), "4:3": (1664, 1248),
              "1:1": (1440, 1440), "3:4": (1248, 1664), "9:16": (1080, 1920)},
    "2K": {"21:9": (2940, 1260), "16:9": (2560, 1440), "4:3": (2224, 1668),
           "1:1": (1920, 1920), "3:4": (1668, 2224), "9:16": (1440, 2560)},
    "1K": {aspect: (1024, 1024) for aspect in ASPECT_RATIOS},
}

USER_AGENT = "AgnesStudio/" + __version__


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class AgnesError(Exception):
    """Any API/network failure. ``status`` is the HTTP status, when there is one."""

    def __init__(self, message, status=None, body=None, retry_after=None):
        super().__init__(message)
        self.message = str(message)
        self.status = status
        self.body = body
        self.retry_after = retry_after

    def __str__(self):
        return self.message

    def to_dict(self):
        return {"error": self.message, "status": self.status}


class AgnesValidationError(AgnesError, ValueError):
    """A request rejected locally: no network call, no credits spent."""

    def __init__(self, message, field=None):
        super().__init__(message, status=None)
        self.field = field


# --------------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------------

def load_dotenv(path=None):
    """Load KEY=VALUE pairs from a .env file into os.environ (no override).

    Looks at ``path`` if given, otherwise ``./.env`` then the file next to
    this module. Returns the path that was loaded, or None.
    """
    candidates = []
    if path:
        candidates.append(path)
    else:
        candidates.append(os.path.join(os.getcwd(), ".env"))
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

    for candidate in candidates:
        if not candidate or not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].strip()
            if key and key not in os.environ:
                os.environ[key] = value
        return candidate
    return None


def resolve_api_key(explicit=None):
    """Explicit key wins, then $AGNES_API_KEY. Returns "" when unset."""
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    env = os.environ.get("AGNES_API_KEY", "")
    return env.strip() if env and env.strip() else ""


def normalize_base_url(raw=None):
    """Base origin, without a trailing slash or a trailing /v1."""
    value = (raw or os.environ.get("AGNES_BASE_URL") or DEFAULT_BASE_URL).strip()
    if not value:
        value = DEFAULT_BASE_URL
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    value = value.rstrip("/")
    if value.lower().endswith("/v1"):
        value = value[:-3].rstrip("/")
    return value


def _mask_key(key):
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "…" + key[-4:]


# --------------------------------------------------------------------------
# Generic JSON helpers (responses are not always the documented shape)
# --------------------------------------------------------------------------

def _walk_bfs(data):
    """Breadth-first walk over every dict inside a JSON tree."""
    queue = [data]
    guard = 0
    while queue and guard < 100000:
        guard += 1
        node = queue.pop(0)
        if isinstance(node, dict):
            yield node
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)


def _usable(value):
    if value is None or value is False:
        return False
    if isinstance(value, (dict, list, tuple, set)):
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def find_first(data, keys, accept=None):
    """Search the whole JSON tree: key priority first, then breadth-first depth.

    ``find_first(resp, ["video_id", "id"])`` returns the first non-empty
    ``video_id`` anywhere in the tree before it considers ``id`` at all.
    """
    if isinstance(keys, str):
        keys = [keys]
    nodes = list(_walk_bfs(data))
    for key in keys:
        for node in nodes:
            if key in node:
                value = node[key]
                if not _usable(value):
                    continue
                if accept is not None and not accept(value):
                    continue
                return value
    return None


def task_ids(data):
    """Extract the identifiers from a create-task response.

    ``video_id`` is preferred for polling; ``id`` and ``task_id`` are the same
    asynchronous task and are kept as fallbacks.
    """
    video_id = find_first(data, ["video_id", "videoId", "videoID"])
    task_id = find_first(data, ["task_id", "taskId", "taskID"])
    ident = find_first(data, ["id", "uuid"])
    model = find_first(data, ["model", "model_name", "modelName"])
    return {
        "video_id": video_id or task_id or ident,
        "task_id": task_id or ident,
        "id": ident,
        "model": model,
    }


def normalize_status(raw):
    """Map an arbitrary status string onto completed / failed / queued / processing."""
    if not raw:
        return None
    text = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    if text in TERMINAL_OK:
        return "completed"
    if text in TERMINAL_FAIL:
        return "failed"
    if text in {"queued", "pending", "waiting", "submitted", "created", "starting", "accepted"}:
        return "queued"
    if text in {"processing", "in_progress", "running", "generating", "working", "rendering",
                "in_progress.", "started"}:
        return "processing"
    return text


def _parse_progress(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        percent = text.endswith("%")
        if percent:
            text = text[:-1].strip()
        try:
            number = float(text)
        except ValueError:
            return None
        if not percent and "." in text and 0.0 <= number <= 1.0:
            number *= 100.0
    elif isinstance(value, (int, float)):
        number = float(value)
        if isinstance(value, float) and 0.0 <= number <= 1.0:
            number *= 100.0
    else:
        return None
    if number < 0:
        return 0
    if number > 100:
        return 100
    return int(round(number))


def _error_text(data):
    """Pull a human-readable failure message out of any response shape."""
    for key in ("error", "error_message", "failure_reason", "detail", "reason"):
        nodes = list(_walk_bfs(data))
        for node in nodes:
            if key not in node:
                continue
            value = node[key]
            if isinstance(value, dict):
                inner = find_first(value, ["message", "msg", "detail", "reason", "code"])
                if inner:
                    return str(inner)
                try:
                    return json.dumps(value)[:400]
                except (TypeError, ValueError):
                    pass
            elif isinstance(value, str) and value.strip():
                return value.strip()
    # Last resort: a top-level message, but only when it is not a success note.
    nodes = list(_walk_bfs(data))
    for node in nodes:
        message = node.get("message") or node.get("msg")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return None


def snapshot(data):
    """Normalise an unknown create/poll response into a predictable dict.

    Returns ``{"status", "progress", "video_url", "error"}`` and never raises
    on odd shapes: values are found by breadth-first search with key priority
    (``video_id`` before ``id``, ``video_url``/``download_url`` before ``url``).
    """
    if not isinstance(data, (dict, list)):
        return {"status": "unknown", "progress": None, "video_url": None, "error": None}

    raw_status = find_first(data, ["status", "state", "task_status", "video_status",
                                   "generation_status", "taskStatus"])
    status = normalize_status(raw_status)

    progress = None
    for key in ("progress", "progress_percent", "percent", "percentage", "completion", "pct"):
        for node in _walk_bfs(data):
            if key in node:
                parsed = _parse_progress(node[key])
                if parsed is not None:
                    progress = parsed
                    break
        if progress is not None:
            break

    def is_http(value):
        return isinstance(value, str) and value.strip().lower().startswith(("http://", "https://"))

    video_url = find_first(
        data,
        ["video_url", "videoUrl", "download_url", "downloadUrl", "url", "file_url",
         "result_url", "video"],
        accept=is_http,
    )
    if isinstance(video_url, str):
        video_url = video_url.strip()

    error = None
    failed = status == "failed"
    if failed:
        error = _error_text(data) or "Video generation failed"
    else:
        explicit = find_first(data, ["error_message", "failure_reason"])
        if isinstance(explicit, str) and explicit.strip():
            error = explicit.strip()

    if status is None:
        status = "completed" if video_url else "unknown"

    return {
        "status": status,
        "progress": progress,
        "video_url": video_url,
        "error": error,
    }


def is_terminal(status):
    return status in ("completed", "failed")


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------

def _parse_json(raw):
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return {"raw": text}


def _http_error(exc):
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - body reading must never mask the error
        raw = ""
    body = _parse_json(raw)
    message = None
    if isinstance(body, dict):
        message = _error_text(body)
    if not message:
        message = (raw or "").strip()[:400]
    if not message:
        message = exc.reason or ("HTTP %s" % exc.code)
    retry_after = None
    try:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
    except Exception:  # noqa: BLE001
        retry_after = None
    return AgnesError(str(message), status=exc.code, body=body, retry_after=retry_after)


def _network_error(exc):
    reason = getattr(exc, "reason", None) or exc
    text = str(reason)
    lowered = text.lower()
    hint = ""
    if isinstance(reason, ssl.SSLError) or any(
        token in lowered
        for token in ("ssl", "tls", "certificate", "connection reset", "timed out",
                      "name or service", "connection refused", "getaddrinfo")
    ):
        hint = (" — the machine making the request may be firewalled from "
                "apihub.agnes-ai.com; this is a network problem, not a bad key.")
    return AgnesError("network error: %s%s" % (text, hint), status=None)


def _request(method, url, api_key=None, payload=None, timeout=30, retries=3,
             raw=False):
    """One HTTP call with 429 backoff. Returns parsed JSON (or bytes if raw)."""
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    key = resolve_api_key(api_key)
    if key:
        headers["Authorization"] = "Bearer " + key
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    attempt = 0
    while True:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                if raw:
                    return body
                return _parse_json(body.decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            error = _http_error(exc)
            if error.status == 429 and attempt < retries:
                delay = 2.0 ** attempt
                try:
                    if error.retry_after:
                        delay = float(error.retry_after)
                except (TypeError, ValueError):
                    pass
                delay = max(1.0, min(delay, 30.0))
                time.sleep(delay)
                attempt += 1
                continue
            raise error
        except urllib.error.URLError as exc:
            raise _network_error(exc)
        except (TimeoutError, OSError) as exc:
            raise _network_error(exc)


# --------------------------------------------------------------------------
# Request building / validation
# --------------------------------------------------------------------------

def classify_url(url):
    """Classify a reference URL by file extension -> 'images' | 'audios' | 'videos'."""
    path = urllib.parse.urlparse(str(url)).path
    ext = os.path.splitext(path)[1].lower()
    if ext in AUDIO_EXTS:
        return "audios"
    if ext in VIDEO_EXTS:
        return "videos"
    if ext in IMAGE_EXTS:
        return "images"
    return "images"  # unknown extension: images are the safest default


def sort_reference_urls(urls):
    """Split a flat list of URLs into images / audios / videos by extension."""
    buckets = {"images": [], "audios": [], "videos": []}
    for url in _as_url_list(urls):
        buckets[classify_url(url)].append(url)
    return buckets


def _as_url_list(value):
    """Accept a string (newline/comma separated) or any iterable of strings."""
    if not value:
        return []
    items = []
    if isinstance(value, str):
        for chunk in re.split(r"[\s,]+", value):
            chunk = chunk.strip()
            if chunk:
                items.append(chunk)
    elif isinstance(value, (list, tuple, set)):
        for entry in value:
            if isinstance(entry, dict):
                if entry.get("url"):
                    items.append(entry)
                continue
            if isinstance(entry, str):
                for chunk in re.split(r"[\s,]+", entry):
                    chunk = chunk.strip()
                    if chunk:
                        items.append(chunk)
    else:
        raise AgnesValidationError("media references must be a list or a string")
    return items


def _check_media_url(url, field):
    if not isinstance(url, str) or not re.match(r"^https?://\S+$", url.strip(), re.I):
        raise AgnesValidationError(
            "%s must be a public http(s) URL, got %r" % (field, url), field=field)
    return url.strip()


def _coerce_seconds(seconds):
    if seconds is None or seconds == "":
        return DEFAULT_SECONDS
    if isinstance(seconds, bool):
        raise AgnesValidationError("seconds must be a whole number between 4 and 12", "seconds")
    if isinstance(seconds, float):
        if not seconds.is_integer():
            raise AgnesValidationError("seconds must be a whole number between 4 and 12", "seconds")
        seconds = int(seconds)
    if isinstance(seconds, str):
        text = seconds.strip()
        if not re.match(r"^\d+$", text):
            raise AgnesValidationError("seconds must be a whole number between 4 and 12", "seconds")
        seconds = int(text)
    if not isinstance(seconds, int):
        raise AgnesValidationError("seconds must be a whole number between 4 and 12", "seconds")
    if seconds < SECONDS_MIN or seconds > SECONDS_MAX:
        raise AgnesValidationError(
            "seconds must be between %d and %d (got %s)" % (SECONDS_MIN, SECONDS_MAX, seconds),
            "seconds")
    return seconds


def _coerce_seed(seed):
    if seed is None or seed == "":
        return None
    if isinstance(seed, bool):
        raise AgnesValidationError("seed must be an integer", "seed")
    if isinstance(seed, str):
        text = seed.strip()
        if not re.match(r"^-?\d+$", text):
            raise AgnesValidationError("seed must be an integer", "seed")
        return int(text)
    if isinstance(seed, float) and seed.is_integer():
        return int(seed)
    if isinstance(seed, int):
        return seed
    raise AgnesValidationError("seed must be an integer", "seed")


def build_payload(prompt="", model=DEFAULT_MODEL, mode="text", seconds=None,
                  size=DEFAULT_SIZE, aspect_ratio=DEFAULT_ASPECT, seed=None,
                  first_frame=None, last_frame=None, images=None, audios=None,
                  videos=None, references=None, n=1):
    """Validate locally and return the exact JSON body for POST /v1/videos.

    Raises :class:`AgnesValidationError` (no network call) on any bad combo.
    ``references`` is a flat list of URLs that is sorted into images / audios /
    videos by file extension.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise AgnesValidationError("prompt is required", "prompt")
    prompt = prompt.strip()

    model = (model or DEFAULT_MODEL).strip()
    if not model:
        raise AgnesValidationError("model is required", "model")

    mode = (mode or "text").strip().lower()
    if mode not in MODES:
        raise AgnesValidationError("mode must be one of: %s" % ", ".join(MODES), "mode")

    seconds_value = _coerce_seconds(seconds)

    size = (size or DEFAULT_SIZE).strip().upper()
    if size not in SIZES:
        raise AgnesValidationError(
            "size must be one of: %s" % ", ".join(SIZES), "size")
    if model == "agnes-video-2.5-flash" and size != "720P":
        raise AgnesValidationError(
            "agnes-video-2.5-flash only supports size 720P (got %s)" % size, "size")

    aspect = (aspect_ratio or DEFAULT_ASPECT).strip()
    if aspect.lower() == "auto" or aspect not in ASPECT_RATIOS:
        raise AgnesValidationError(
            "aspect_ratio must be one of: %s" % ", ".join(ASPECT_RATIOS), "aspect_ratio")

    seed_value = _coerce_seed(seed)

    if n is None:
        n = 1
    if n != 1:
        raise AgnesValidationError("n only supports 1", "n")

    frames = {
        "first_frame": first_frame.strip() if isinstance(first_frame, str) else first_frame,
        "last_frame": last_frame.strip() if isinstance(last_frame, str) else last_frame,
    }
    frames = {key: _check_media_url(value, key) if value else None
              for key, value in frames.items()}

    image_list = [_check_media_url(u, "images") for u in _as_url_list(images)]
    audio_list = [_check_media_url(u, "audios") for u in _as_url_list(audios)]
    video_items = []
    for entry in _as_url_list(videos):
        if isinstance(entry, dict):
            url = _check_media_url(entry.get("url"), "videos")
            item = {"url": url}
            if entry.get("start_seconds") is not None:
                try:
                    item["start_seconds"] = float(entry["start_seconds"])
                except (TypeError, ValueError):
                    raise AgnesValidationError("videos[].start_seconds must be a number", "videos")
            if entry.get("require_audio") is not None:
                item["require_audio"] = bool(entry["require_audio"])
            video_items.append(item)
        else:
            video_items.append({"url": _check_media_url(entry, "videos")})

    if references:
        buckets = sort_reference_urls(references)
        image_list.extend(_check_media_url(u, "images") for u in buckets["images"])
        audio_list.extend(_check_media_url(u, "audios") for u in buckets["audios"])
        video_items.extend({"url": _check_media_url(u, "videos")} for u in buckets["videos"])

    has_frames = bool(frames["first_frame"] or frames["last_frame"])
    has_references = bool(image_list or audio_list or video_items)

    if mode == "text":
        if has_frames or has_references:
            raise AgnesValidationError(
                "text mode does not accept media fields; switch to keyframe or reference mode",
                "mode")
    elif mode == "keyframe":
        if not has_frames:
            raise AgnesValidationError(
                "keyframe mode needs at least one of first_frame or last_frame", "first_frame")
        if has_references:
            raise AgnesValidationError(
                "keyframe mode does not accept images/audios/videos", "mode")
    elif mode == "reference":
        if not has_references:
            raise AgnesValidationError(
                "reference mode needs at least one non-empty images/audios/videos entry",
                "images")
        if has_frames:
            raise AgnesValidationError(
                "reference mode does not accept first_frame/last_frame", "mode")

    if model == "agnes-video-2.5-flash" and mode == "reference":
        if len(image_list) > 5:
            raise AgnesValidationError("agnes-video-2.5-flash supports at most 5 images", "images")
        if len(audio_list) > 3:
            raise AgnesValidationError("agnes-video-2.5-flash supports at most 3 audios", "audios")
        if video_items:
            raise AgnesValidationError(
                "agnes-video-2.5-flash does not support reference videos", "videos")

    if model == "agnes-video-v2.0":
        width, height = DIMENSIONS[size][aspect]
        frame_rate = 24
        payload = {
            "model": model,
            "prompt": prompt,
            "mode": mode,
            "width": width,
            "height": height,
            "num_frames": seconds_value * frame_rate,
            "frame_rate": frame_rate,
        }
        if seed_value is not None:
            payload["seed"] = seed_value
        _attach_media(payload, frames, image_list, audio_list, video_items)
        return payload

    payload = {
        "model": model,
        "prompt": prompt,
        "mode": mode,
        "seconds": str(seconds_value),
        "size": size,
        "aspect_ratio": aspect,
    }
    if seed_value is not None:
        payload["seed"] = seed_value
    _attach_media(payload, frames, image_list, audio_list, video_items)
    return payload


def _attach_media(payload, frames, image_list, audio_list, video_items):
    if frames["first_frame"]:
        payload["first_frame"] = frames["first_frame"]
    if frames["last_frame"]:
        payload["last_frame"] = frames["last_frame"]
    if image_list:
        payload["images"] = image_list
    if audio_list:
        payload["audios"] = audio_list
    if video_items:
        payload["videos"] = video_items
    return payload


# --------------------------------------------------------------------------
# API calls
# --------------------------------------------------------------------------

def create_task(payload, api_key=None, base_url=None, timeout=30):
    """POST /v1/videos and return the parsed create response."""
    base = normalize_base_url(base_url)
    return _request("POST", base + "/v1/videos", api_key=api_key, payload=payload,
                    timeout=timeout)


def get_task(video_id, model_name=None, api_key=None, base_url=None, timeout=30):
    """GET /agnesapi?video_id=...&model_name=...

    Falls back to the bare ``video_id`` form when the API answers 400/404.
    """
    if not video_id:
        raise AgnesValidationError("video_id is required", "video_id")
    base = normalize_base_url(base_url)
    query = {"video_id": video_id}
    if model_name:
        query["model_name"] = model_name
    url = base + "/agnesapi?" + urllib.parse.urlencode(query)
    try:
        return _request("GET", url, api_key=api_key, timeout=timeout)
    except AgnesError as exc:
        if model_name and exc.status in (400, 404):
            bare = base + "/agnesapi?" + urllib.parse.urlencode({"video_id": video_id})
            return _request("GET", bare, api_key=api_key, timeout=timeout)
        raise


def wait_for_video(video_id, model_name=None, api_key=None, base_url=None,
                   poll_interval=8.0, timeout=900.0, on_update=None,
                   should_stop=None, sleep=time.sleep):
    """Poll until the task completes or fails. Returns a snapshot dict.

    ``on_update(snapshot, elapsed_seconds)`` is called after every poll.
    ``should_stop()`` may cancel between polls.
    """
    poll_interval = max(0.0, float(poll_interval or 0))
    started = time.monotonic()
    last = None
    last_error = None
    while True:
        if should_stop is not None and should_stop():
            raise AgnesError("polling stopped by user")
        try:
            raw = get_task(video_id, model_name=model_name, api_key=api_key,
                           base_url=base_url)
            last = snapshot(raw)
            elapsed = time.monotonic() - started
            if on_update is not None:
                on_update(last, elapsed)
            if last["status"] == "completed" and last["video_url"]:
                return last
            if last["status"] == "failed":
                raise AgnesError(last["error"] or "Video generation failed",
                                 body=raw)
        except AgnesError as exc:
            if exc.status in (401, 403):
                raise
            last_error = exc
            if exc.status in (400, 404):
                raise
        elapsed = time.monotonic() - started
        if elapsed >= float(timeout):
            detail = last["status"] if last else "unknown"
            extra = ""
            if last_error is not None:
                extra = " (last error: %s)" % last_error
            raise AgnesError(
                "timed out after %.0fs waiting for %s (last status: %s)%s"
                % (float(timeout), video_id, detail, extra))
        if sleep is not None:
            sleep(min(poll_interval, max(0.0, float(timeout) - elapsed)))


def download(url, dest, timeout=120, chunk_size=64 * 1024, headers=None):
    """Stream a finished video to ``dest``. Returns dest."""
    if not url or not str(url).lower().startswith(("http://", "https://")):
        raise AgnesValidationError("download needs an http(s) URL", "url")
    parent = os.path.dirname(os.path.abspath(dest))
    if parent:
        os.makedirs(parent, exist_ok=True)
    request = urllib.request.Request(
        url, headers=headers or {"User-Agent": USER_AGENT}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, \
                open(dest, "wb") as handle:
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                handle.write(chunk)
    except urllib.error.HTTPError as exc:
        raise _http_error(exc)
    except urllib.error.URLError as exc:
        raise _network_error(exc)
    except (TimeoutError, OSError) as exc:
        if isinstance(exc, AgnesError):
            raise
        raise _network_error(exc)
    return dest


def fetch_models(api_key=None, base_url=None, timeout=20):
    """GET /v1/models. Raises AgnesError (401/403 for a genuinely bad key)."""
    base = normalize_base_url(base_url)
    response = _request("GET", base + "/v1/models", api_key=api_key, timeout=timeout,
                        retries=1)
    return extract_models(response)


def extract_models(response):
    """Pull a list of model dicts/ids out of whatever /v1/models returns."""
    if isinstance(response, list):
        raw = response
    elif isinstance(response, dict):
        raw = None
        for key in ("data", "models", "items", "result", "results"):
            value = response.get(key)
            if isinstance(value, list):
                raw = value
                break
        if raw is None:
            raw = []
    else:
        raw = []
    models = []
    for item in raw:
        if isinstance(item, str):
            models.append({"id": item})
        elif isinstance(item, dict):
            ident = item.get("id") or item.get("model") or item.get("name")
            entry = dict(item)
            if ident:
                entry["id"] = str(ident)
            models.append(entry)
    return models


def verify_key(api_key=None, base_url=None, timeout=20):
    """Check a key, but only fail for a real 401/403.

    Returns ``{"ok": True, "verified": bool, "warning": str|None, "models": [...]}``.
    Network/TLS/timeout/5xx failures keep the key usable and return a warning.
    """
    key = resolve_api_key(api_key)
    if not key:
        return {"ok": False, "verified": False, "error": "No API key provided",
                "models": []}
    try:
        models = fetch_models(api_key=key, base_url=base_url, timeout=timeout)
    except AgnesError as exc:
        if exc.status in (401, 403):
            return {"ok": False, "verified": False,
                    "error": "The API rejected this key (HTTP %s): %s"
                             % (exc.status, exc.message),
                    "models": []}
        detail = exc.message
        if "network" not in detail.lower() and "firewall" not in detail.lower():
            detail += " — that is a network problem, not necessarily a bad key"
        return {"ok": True, "verified": False, "models": [],
                "warning": "Key saved, but it could not be verified: %s" % detail}
    return {"ok": True, "verified": True, "warning": None, "models": models}


def model_catalog():
    """Static catalog used by the UI (no request, no key needed)."""
    return [
        {"id": model, "label": info["label"], "sizes": info["sizes"],
         "notes": info["notes"]}
        for model, info in KNOWN_MODELS.items()
    ]


# --------------------------------------------------------------------------
# Local output helpers
# --------------------------------------------------------------------------

def timestamp_name(prefix="agnes"):
    return "%s-%s" % (prefix, time.strftime("%Y%m%d-%H%M%S", time.localtime()))


if __name__ == "__main__":  # tiny smoke test, no network
    payload = build_payload(prompt="a silver sports car in the rain", seed=42)
    print(json.dumps(payload, indent=2))
    print(json.dumps(snapshot({"progress": "37%", "data": {"status": "IN_PROGRESS"}}), indent=2),
          file=sys.stderr)
