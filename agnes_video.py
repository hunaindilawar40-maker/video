#!/usr/bin/env python3
"""Agnes Studio CLI — turn a text prompt into a short video.

Examples
--------
    python3 agnes_video.py -p "a silver sports car drifting through neon rain"
    python3 agnes_video.py --prompt-file brief.txt -s 8 --aspect-ratio 9:16
    echo "a cat astronaut" | python3 agnes_video.py
    python3 agnes_video.py -p "..." --dry-run          # free: prints the body only
    python3 agnes_video.py -p "..." --mode keyframe --first-frame https://.../a.png

Exit codes: 0 success, 1 API/generation failure, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import agnes_api as api

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


class _Parser(argparse.ArgumentParser):
    """argparse with exit code 2 for usage errors (its own default, made explicit)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("%s: error: %s\n" % (self.prog, message))
        raise SystemExit(EXIT_USAGE)


def _read_prompt_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise ValueError("cannot read --prompt-file %s: %s" % (path, exc))


def _stdin_prompt():
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    try:
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""


def _human_seconds(seconds):
    return "%ds" % int(seconds)


def build_parser():
    parser = _Parser(
        prog="agnes_video.py",
        description="Generate a short video with the Agnes AI video API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples")[1] if "Examples" in __doc__ else None,
    )
    parser.add_argument("-p", "--prompt", help="text prompt (stdin is used when piped)")
    parser.add_argument("--prompt-file", help="read the prompt from a file")
    parser.add_argument("--model", default=api.DEFAULT_MODEL,
                        help="model id (default: %(default)s)")
    parser.add_argument("--mode", default="text", choices=list(api.MODES),
                        help="generation mode (default: %(default)s)")
    parser.add_argument("-s", "--seconds", default=str(api.DEFAULT_SECONDS),
                        help="clip length, 4-12 (default: %(default)s)")
    parser.add_argument("--size", default=api.DEFAULT_SIZE,
                        help="720P | 1080P | 1K | 2K (default: %(default)s)")
    parser.add_argument("--aspect-ratio", default=api.DEFAULT_ASPECT,
                        choices=list(api.ASPECT_RATIOS),
                        help="frame shape (default: %(default)s)")
    parser.add_argument("--seed", type=int, default=None, help="integer seed")
    parser.add_argument("--first-frame", default=None, help="keyframe mode: first frame URL")
    parser.add_argument("--last-frame", default=None, help="keyframe mode: last frame URL")
    parser.add_argument("--image", action="append", default=[], metavar="URL",
                        help="reference image URL (repeatable)")
    parser.add_argument("--audio", action="append", default=[], metavar="URL",
                        help="reference audio URL (repeatable)")
    parser.add_argument("--video", action="append", default=[], metavar="URL",
                        help="reference video URL (repeatable)")
    parser.add_argument("-o", "--out", default=None,
                        help="output file (default: output/agnes-<timestamp>.mp4)")
    parser.add_argument("--out-dir", default="output", help="output directory")
    parser.add_argument("--key", default=None, help="API key (default: $AGNES_API_KEY)")
    parser.add_argument("--base-url", default=None,
                        help="API base URL (default: $AGNES_BASE_URL or %s)"
                             % api.DEFAULT_BASE_URL)
    parser.add_argument("--poll-interval", type=float, default=8.0,
                        help="seconds between polls (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=900.0,
                        help="max total wait in seconds (default: %(default)s)")
    parser.add_argument("--no-download", action="store_true",
                        help="print the URL but do not download the mp4")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit machine-readable JSON")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the request body and exit (spends nothing)")
    parser.add_argument("--version", action="version", version="Agnes Studio " + api.__version__)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    api.load_dotenv()
    key = api.resolve_api_key(args.key)
    base_url = api.normalize_base_url(args.base_url)

    # ---- prompt -----------------------------------------------------------
    prompt = args.prompt or ""
    if args.prompt_file:
        try:
            prompt = _read_prompt_file(args.prompt_file)
        except ValueError as exc:
            parser.error(str(exc))
    if not prompt.strip():
        prompt = _stdin_prompt()
    if not prompt.strip():
        parser.error("a prompt is required (use -p, --prompt-file, or pipe stdin)")

    # ---- payload (all validation happens locally, before any network call) --
    try:
        payload = api.build_payload(
            prompt=prompt,
            model=args.model,
            mode=args.mode,
            seconds=args.seconds,
            size=args.size,
            aspect_ratio=args.aspect_ratio,
            seed=args.seed,
            first_frame=args.first_frame,
            last_frame=args.last_frame,
            images=args.image,
            audios=args.audio,
            videos=args.video,
        )
    except api.AgnesValidationError as exc:
        sys.stderr.write("invalid arguments: %s\n" % exc)
        return EXIT_USAGE

    if args.dry_run:
        body = {"url": base_url + "/v1/videos", "method": "POST",
                "headers": {"Authorization": "Bearer " + (api._mask_key(key) or "<unset>"),
                            "Content-Type": "application/json"},
                "payload": payload}
        if args.as_json:
            print(json.dumps(body, indent=2))
        else:
            print("dry run — no request sent, no credits spent")
            print("POST %s" % body["url"])
            print("Authorization: Bearer %s" % (api._mask_key(key) or "<no key>"))
            print("Content-Type: application/json")
            print()
            print(json.dumps(payload, indent=2))
        return EXIT_OK

    if not key:
        sys.stderr.write("no API key: set AGNES_API_KEY, use --key, or add it to .env "
                         "(see .env.example)\n")
        return EXIT_USAGE

    # ---- create -----------------------------------------------------------
    if not args.as_json:
        print("creating task (%s, %ss, %s, %s)…"
              % (payload["model"], payload.get("seconds"), payload.get("size"),
                 payload.get("aspect_ratio")), flush=True)
    try:
        created = api.create_task(payload, api_key=key, base_url=base_url)
    except api.AgnesError as exc:
        return _fail(exc, args.as_json)

    ids = api.task_ids(created)
    video_id = ids["video_id"]
    model_name = ids["model"] or payload["model"]
    if not video_id:
        return _fail(api.AgnesError("create response had no video_id: %s"
                                    % json.dumps(created)[:400]), args.as_json)

    if not args.as_json:
        print("video_id: %s" % video_id, flush=True)

    # ---- poll -------------------------------------------------------------
    def on_update(snap, elapsed):
        if args.as_json:
            return
        progress = snap.get("progress")
        pct = "" if progress is None else " %d%%" % progress
        sys.stdout.write("\rstatus: %-12s%s (%s)   "
                         % (snap.get("status") or "unknown", pct,
                            _human_seconds(elapsed)))
        sys.stdout.flush()

    try:
        result = api.wait_for_video(
            video_id, model_name=model_name, api_key=key, base_url=base_url,
            poll_interval=args.poll_interval, timeout=args.timeout, on_update=on_update)
    except api.AgnesError as exc:
        if not args.as_json:
            sys.stdout.write("\n")
            sys.stdout.flush()
        return _fail(exc, args.as_json)
    if not args.as_json:
        sys.stdout.write("\n")
        sys.stdout.flush()

    url = result.get("video_url")
    saved = None
    if url and not args.no_download:
        if args.out:
            dest = args.out
        else:
            os.makedirs(args.out_dir, exist_ok=True)
            dest = os.path.join(args.out_dir, api.timestamp_name() + ".mp4")
        try:
            saved = api.download(url, dest)
        except api.AgnesError as exc:
            if args.as_json:
                print(json.dumps({"video_id": video_id, "url": url,
                                  "error": str(exc), "saved": None}))
            else:
                print("download failed: %s" % exc)
            return EXIT_FAIL

    if args.as_json:
        print(json.dumps({
            "ok": True,
            "video_id": video_id,
            "model": model_name,
            "url": url,
            "saved": saved,
            "progress": result.get("progress"),
        }, indent=2))
    else:
        print("url:   %s" % (url or "(none)"))
        if saved:
            print("saved: %s" % saved)
        elif args.no_download:
            print("saved: (skipped, --no-download)")
    return EXIT_OK


def _fail(exc, as_json):
    if as_json:
        print(json.dumps({"ok": False, "error": str(exc), "status": exc.status}, indent=2))
    else:
        sys.stderr.write("error: %s\n" % exc)
        if exc.status in (401, 403):
            sys.stderr.write("hint: the API rejected the key — check AGNES_API_KEY.\n")
        elif exc.status == 400:
            sys.stderr.write("hint: bad request — check the model/size/mode combination.\n")
        elif exc.status == 429:
            sys.stderr.write("hint: rate limited (~16 requests/min). Wait and retry.\n")
        elif "network error" in str(exc).lower():
            sys.stderr.write("hint: if your machine is firewalled, try the web UI in "
                             "browser-direct mode or the Chrome extension.\n")
    return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
