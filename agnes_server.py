#!/usr/bin/env python3
"""Agnes Studio local web server.

Serves the studio UI from ./web and proxies the Agnes AI API so the key never
lands in the page, in a log, or on disk.

    python3 agnes_server.py [--port 8777] [--host 0.0.0.0]

Endpoints
---------
GET  /api/health                      key_configured, base url, version
POST /api/key                         {"key": "..."} -> verify (only 401/403 rejects)
POST /api/tasks                       {"payload": {...}} -> create task
GET  /api/tasks?video_id=&model_name= -> poll task
GET  /api/file?url=                   -> stream a download (allowlisted URLs only)
GET  /api/models                      -> model catalog

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agnes_api as api

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".txt": "text/plain; charset=utf-8",
}

MAX_BODY = 2 * 1024 * 1024


class StudioState:
    """Per-process state. The API key lives here and nowhere else."""

    def __init__(self, base_url=None):
        self.api_key = ""
        self.base_url = api.normalize_base_url(base_url)
        self.verified = False
        self.warning = None
        self.allowed_urls = set()  # every URL the API has handed us this process

    def remember(self, *urls):
        for url in urls:
            if isinstance(url, str) and url.lower().startswith(("http://", "https://")):
                self.allowed_urls.add(url)


STATE = StudioState()


class Handler(BaseHTTPRequestHandler):
    server_version = "AgnesStudio/" + api.__version__
    protocol_version = "HTTP/1.1"

    # ---------------- helpers -------------------------------------------
    def log_message(self, fmt, *args):  # quieter, still useful
        sys.stderr.write("[agnes] %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status, body=b"", content_type="application/json; charset=utf-8",
              extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload), "application/json; charset=utf-8")

    def _error(self, status, message, **extra):
        payload = {"ok": False, "error": message, "status": status}
        payload.update(extra)
        self._json(status, payload)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise ValueError("request body must be JSON")
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def _api_error(self, exc):
        status = exc.status if exc.status and 400 <= exc.status <= 599 else 502
        self._error(status, str(exc), upstream_status=exc.status)

    # ---------------- routing -------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/api/health":
            return self._json(HTTPStatus.OK, {
                "ok": True,
                "service": "agnes-studio",
                "version": api.__version__,
                "key_configured": bool(STATE.api_key),
                "key_source": "memory" if STATE.api_key else "none",
                "key_verified": STATE.verified,
                "key_warning": STATE.warning,
                "base_url": STATE.base_url,
                "models": len(api.model_catalog()),
                "defaults": {
                    "model": api.DEFAULT_MODEL,
                    "seconds": api.DEFAULT_SECONDS,
                    "size": api.DEFAULT_SIZE,
                    "aspect_ratio": api.DEFAULT_ASPECT,
                },
            })

        if path == "/api/models":
            return self._json(HTTPStatus.OK, {"ok": True, "models": api.model_catalog()})

        if path == "/api/tasks":
            video_id = (query.get("video_id") or [""])[0].strip()
            model_name = (query.get("model_name") or [""])[0].strip() or None
            if not video_id:
                return self._error(HTTPStatus.BAD_REQUEST, "video_id is required")
            if not STATE.api_key:
                return self._error(HTTPStatus.UNAUTHORIZED,
                                   "no API key loaded on the server — POST /api/key first")
            try:
                raw = api.get_task(video_id, model_name=model_name, api_key=STATE.api_key,
                                   base_url=STATE.base_url)
            except api.AgnesError as exc:
                return self._api_error(exc)
            snap = api.snapshot(raw)
            STATE.remember(snap.get("video_url"))
            return self._json(HTTPStatus.OK, {"ok": True, "snapshot": snap, "raw": raw})

        if path == "/api/file":
            return self._serve_file((query.get("url") or [""])[0])

        return self._static(path)

    def do_HEAD(self):
        return self.do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            body = self._read_json()
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))

        if path == "/api/key":
            return self._set_key(body)
        if path == "/api/tasks":
            return self._create_task(body)
        return self._error(HTTPStatus.NOT_FOUND, "unknown endpoint %s" % path)

    # ---------------- endpoints -----------------------------------------
    def _set_key(self, body):
        key = str(body.get("key") or "").strip()
        if not key:
            return self._error(HTTPStatus.BAD_REQUEST, "key is required")
        if body.get("base_url"):
            STATE.base_url = api.normalize_base_url(body["base_url"])
        STATE.api_key = key  # memory only, never written to disk
        result = api.verify_key(key, base_url=STATE.base_url)
        if not result.get("ok"):
            # Only a real 401/403 rejects the key.
            STATE.api_key = ""
            STATE.verified = False
            STATE.warning = None
            return self._error(HTTPStatus.UNAUTHORIZED, result.get("error") or "invalid key",
                               verified=False)
        STATE.verified = bool(result.get("verified"))
        STATE.warning = result.get("warning")
        return self._json(HTTPStatus.OK, {
            "ok": True,
            "verified": STATE.verified,
            "warning": STATE.warning,
            "base_url": STATE.base_url,
            "models": result.get("models") or [],
        })

    def _create_task(self, body):
        payload = body.get("payload") if "payload" in body else body
        if not isinstance(payload, dict):
            return self._error(HTTPStatus.BAD_REQUEST, "payload must be an object")
        try:
            # Re-validate server-side: never send a bad combo upstream.
            clean = api.build_payload(
                prompt=payload.get("prompt", ""),
                model=payload.get("model") or api.DEFAULT_MODEL,
                mode=payload.get("mode") or "text",
                seconds=payload.get("seconds"),
                size=payload.get("size") or api.DEFAULT_SIZE,
                aspect_ratio=payload.get("aspect_ratio") or api.DEFAULT_ASPECT,
                seed=payload.get("seed"),
                first_frame=payload.get("first_frame"),
                last_frame=payload.get("last_frame"),
                images=payload.get("images"),
                audios=payload.get("audios"),
                videos=payload.get("videos"),
                references=payload.get("references"),
            )
        except api.AgnesValidationError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc), field=exc.field)
        if not STATE.api_key:
            return self._error(HTTPStatus.UNAUTHORIZED,
                               "no API key loaded on the server — POST /api/key first")
        try:
            created = api.create_task(clean, api_key=STATE.api_key, base_url=STATE.base_url)
        except api.AgnesError as exc:
            return self._api_error(exc)
        ids = api.task_ids(created)
        snap = api.snapshot(created)
        STATE.remember(snap.get("video_url"))
        return self._json(HTTPStatus.OK, {
            "ok": True,
            "payload": clean,
            "video_id": ids["video_id"],
            "task_id": ids["task_id"],
            "id": ids["id"],
            "model": ids["model"] or clean["model"],
            "snapshot": snap,
            "raw": created,
        })

    def _serve_file(self, url):
        """Stream a download, but ONLY for URLs this process was given by the API."""
        if not url:
            return self._error(HTTPStatus.BAD_REQUEST, "url is required")
        if url not in STATE.allowed_urls:
            return self._error(
                HTTPStatus.FORBIDDEN,
                "that URL was not returned by the API in this process — refusing to proxy it")
        try:
            body = api._request("GET", url, api_key=STATE.api_key, timeout=180, raw=True,
                                retries=1)
        except api.AgnesError as exc:
            return self._api_error(exc)
        path = urllib.parse.urlparse(url).path
        ext = os.path.splitext(path)[1].lower()
        content_type = CONTENT_TYPES.get(ext, "video/mp4")
        name = os.path.basename(path) or "agnes-video.mp4"
        self._send(HTTPStatus.OK, body, content_type, {
            "Content-Disposition": 'attachment; filename="%s"' % name.replace('"', ""),
        })

    def _static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        relative = urllib.parse.unquote(path).lstrip("/")
        candidate = os.path.normpath(os.path.join(WEB_DIR, relative))
        if not candidate.startswith(os.path.abspath(WEB_DIR) + os.sep) and \
                candidate != os.path.abspath(WEB_DIR):
            return self._error(HTTPStatus.FORBIDDEN, "forbidden path")
        if not os.path.isfile(candidate):
            # Allow the extension to load /manifest.json and /icons/* from the root.
            root_candidate = os.path.normpath(
                os.path.join(os.path.dirname(WEB_DIR), relative))
            if os.path.isfile(root_candidate):
                candidate = root_candidate
            else:
                return self._error(HTTPStatus.NOT_FOUND, "not found: %s" % path)
        ext = os.path.splitext(candidate)[1].lower()
        with open(candidate, "rb") as handle:
            data = handle.read()
        self._send(HTTPStatus.OK, data, CONTENT_TYPES.get(ext, "application/octet-stream"))


def make_server(host="0.0.0.0", port=8777, base_url=None):
    STATE.base_url = api.normalize_base_url(base_url or STATE.base_url)
    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Agnes Studio local web server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AGNES_PORT", 8777)))
    parser.add_argument("--host", default=os.environ.get("AGNES_HOST", "0.0.0.0"))
    parser.add_argument("--base-url", default=None, help="override the API base URL")
    args = parser.parse_args(argv)

    api.load_dotenv()
    if args.base_url:
        STATE.base_url = api.normalize_base_url(args.base_url)
    env_key = api.resolve_api_key()
    if env_key:
        STATE.api_key = env_key
        print("API key loaded from environment (%s)" % api._mask_key(env_key))
    else:
        print("No AGNES_API_KEY in the environment — paste a key in the UI "
              "(kept in server memory + tab sessionStorage only).")

    server = make_server(args.host, args.port, STATE.base_url)
    host, port = server.server_address[:2]
    shown = "localhost" if host in ("0.0.0.0", "::") else host
    print("Agnes Studio → http://%s:%d" % (shown, port))
    print("API base URL   → %s" % STATE.base_url)
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
