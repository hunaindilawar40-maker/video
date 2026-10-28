# Agnes Studio

Text prompt → short video, using the [Agnes AI video API](https://agnes-ai.com/en/docs/agnes-video-25).
One codebase, three ways to run it:

| Interface | Run it | Best for |
| --- | --- | --- |
| **Local web UI** | `python3 agnes_server.py` → http://localhost:8777 | exploring prompts, previewing, history |
| **CLI** | `python3 agnes_video.py -p "…"` | scripting, batches, quick dry runs |
| **Chrome extension** | load the folder unpacked | when the machine is firewalled but your browser isn't |

Python 3.8+ standard library only. No npm, no bundler, no dependencies, no `package.json`.

---

## Quick start

```bash
git clone https://github.com/hunaindilawar40-maker/video.git
cd video

cp .env.example .env          # paste your key into AGNES_API_KEY=
python3 agnes_server.py       # → http://localhost:8777
```

The **key → run → generate** flow:

1. **Get a key** from the Agnes AI platform (it is sent as `Authorization: Bearer $AGNES_API_KEY`).
2. **Hand it over** one of three ways:
   - `.env` (`AGNES_API_KEY=…`) or the `AGNES_API_KEY` environment variable — read by the CLI and the server at startup;
   - the key field in the web UI — sent to the local server, kept **in server memory only**, and mirrored in the tab's `sessionStorage`;
   - `--key` on the CLI for one-off runs.
3. **Generate** — type a prompt, pick model/aspect/duration/mode, press **Generate** (or `⌘/Ctrl+Enter`).
   A 5s clip typically takes **40–120s**. The UI shows a live progress bar, status and elapsed timer while it polls.
4. **Save it** — the preview gets `Download mp4`, `Copy link`, `Regenerate`, plus a session history strip with hover-scrubbing thumbnails.

The key is never written to disk by this project. `.env`, `output/` and `__pycache__/` are git-ignored.

---

## API contract (verified against the official docs)

Docs: <https://agnes-ai.com/en/docs/agnes-video-25> · <https://wiki.agnes-ai.com/en/docs/agnes-video-25-flash>

Base URL `https://apihub.agnes-ai.com` (override with `AGNES_BASE_URL`; a trailing `/v1` is tolerated).

### Two-step asynchronous flow

| Step | Request |
| --- | --- |
| 1. create | `POST /v1/videos` |
| 2. poll | `GET /agnesapi?video_id=<VIDEO_ID>&model_name=<MODEL_ID>` |
| key check | `GET /v1/models` |

```jsonc
// POST /v1/videos
{
  "model": "agnes-video-2.5-flash",
  "prompt": "a silver sports car drifting through a rain-soaked neon street at night",
  "mode": "text",
  "seconds": "5",          // STRING, 4–12
  "size": "720P",
  "aspect_ratio": "16:9",
  "seed": 42               // optional
}
```

The create response carries `video_id` (use this for polling), plus `id` / `task_id` (the same task),
`model`, `status`, `progress`, `seconds`, `size`. A completed poll response exposes the video at the
top-level `url` field; `status` and `progress` are the source of truth (ignore `internal_status`).

Always include `model_name` in the poll query. A bare `video_id` only works for `mode: "text"`; the
client falls back to the bare form automatically if the API answers `400`/`404`.

### Parameters

| Field | Values | Notes |
| --- | --- | --- |
| `model` | `agnes-video-2.5-flash` · `agnes-video-2.5` · `agnes-video-v2.0` | flash is fast, 720P only |
| `prompt` | string | required; `<Picture 1>`, `<Audio 1>`, `<Video 1>` in reference mode |
| `mode` | `text` · `keyframe` · `reference` | default `text` |
| `seconds` | string `"4"`–`"12"` | default `"5"` |
| `size` | `720P` · `1080P` · `1K` · `2K` | **flash only accepts `720P`** (others are a 400) |
| `aspect_ratio` | `16:9` · `9:16` · `1:1` · `4:3` · `3:4` · `21:9` | default `16:9`; `auto` is rejected |
| `seed` | integer | optional, improves reproducibility |
| `n` | `1` | not exposed; anything else is a 400 |
| `first_frame` / `last_frame` | URL | keyframe mode, at least one |
| `images[]` / `audios[]` / `videos[{url,…}]` | URLs | reference mode; flash: ≤5 images, ≤3 audios, no videos |

### Mode rules (validated locally — a bad combo never costs a request)

| mode | requires | forbidden |
| --- | --- | --- |
| `text` | — | `first_frame`, `last_frame`, `images`, `audios`, `videos` |
| `keyframe` | ≥1 of `first_frame` / `last_frame` | `images`, `audios`, `videos` |
| `reference` | ≥1 non-empty `images` / `audios` / `videos` | `first_frame`, `last_frame` |

Media URLs must be publicly reachable and must stay valid until the task finishes.
Reference placeholders are numbered per-array from 1: `<Picture 1>`, `<Audio 1>`, `<Video 1>`.

Rate limit is roughly **16 requests/minute**; the client backs off exponentially on `429`.
Polling waits **6–10s** between calls and gives up after **~15 minutes**.

### `agnes-video-v2.0` (legacy)

The full code path is still accepted: the same logical request is translated into
`width` / `height` / `num_frames` / `frame_rate` (24 fps × seconds, dimensions from the documented
720P/1080P/2K tables). All other models use the modern `size` / `aspect_ratio` / `seconds` contract.

---

## CLI

```bash
python3 agnes_video.py -p "a silver sports car drifting through neon rain"
python3 agnes_video.py --prompt-file brief.txt -s 8 --aspect-ratio 9:16 --seed 42
echo "a cat astronaut waving" | python3 agnes_video.py
python3 agnes_video.py -p "…" --mode keyframe --first-frame https://…/a.png --last-frame https://…/b.png
python3 agnes_video.py -p "…" --mode reference --image https://…/p.png --audio https://…/beat.mp3
python3 agnes_video.py -p "…" --dry-run          # prints the body, spends nothing
```

Defaults: `agnes-video-2.5-flash`, 5s, 720P, 16:9, saved to `output/agnes-<timestamp>.mp4`.

| Flag | Meaning |
| --- | --- |
| `-p/--prompt`, `--prompt-file` | prompt text; stdin is used when piped |
| `--model`, `--mode`, `-s/--seconds`, `--size`, `--aspect-ratio`, `--seed` | request shape |
| `--first-frame`, `--last-frame` | keyframe mode |
| `--image`, `--audio`, `--video` | reference mode, repeatable |
| `-o/--out`, `--out-dir` | destination |
| `--key`, `--base-url` | overrides for `.env` / environment |
| `--poll-interval`, `--timeout` | polling cadence and cap (default 8s / 900s) |
| `--no-download`, `--json`, `--dry-run` | print URL only / machine output / free preview |

Live line per poll: `status: processing 40% (12s)`, then the URL and the saved path.
Exit codes: **0** success, **1** API/generation failure, **2** bad arguments.

---

## Web UI

```bash
python3 agnes_server.py [--port 8777] [--host 0.0.0.0] [--base-url https://apihub.agnes-ai.com]
```

Left card = composer, right card = preview. Composer: prompt, style chips
(Cinematic / Neon night / Product / Aerial / Dreamy / Macro), model, aspect, duration slider
(4–12, live label), resolution (auto-locked to 720P and disabled on flash), mode, seed, conditional
first/last frame inputs, conditional reference URLs (auto-sorted into images/audios/videos by file
extension), Generate with spinner, and Stop polling.

### Two transports — “Network” dropdown

| Transport | What happens | Use when |
| --- | --- | --- |
| `server` *(default)* | page → `/api/*` → Python server holds the key and proxies the finished mp4 through `/api/file?url=…` | normal local use; no CORS, no CDN token trouble |
| `direct` | the page calls `https://apihub.agnes-ai.com` itself with the Bearer token | the serving machine cannot reach the API (sandbox, preview proxy, corporate firewall) while your browser can |

A plain fetch from an ordinary web page may be blocked by CORS. When that happens the UI says so
explicitly — *“this is a network/CORS problem, not a bad key”* — and points at the extension.

### Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | `key_configured`, `key_verified`, base URL, defaults |
| `POST` | `/api/key` | `{"key": "…"}` — verify, keep in memory |
| `POST` | `/api/tasks` | create a task (`{"payload": {…}}` re-validated server-side) |
| `GET` | `/api/tasks?video_id=&model_name=` | poll one task |
| `GET` | `/api/file?url=` | stream a download — **only for URLs the API returned in this process** |
| `GET` | `/api/models` | model catalog (no key needed) |

`/api/file` is deliberately not an open proxy: any URL this process did not receive from the API is
rejected with **403**.

### Key-handling rule

A key is rejected **only on a real `401`/`403`**. If verification fails for any other reason
(network, TLS, timeout, `5xx`), the key is still saved and `POST /api/key` returns
`{"ok": true, "verified": false, "warning": "…"}`; the UI shows an amber “key saved · unverified”
pill. A firewalled machine must never make a valid key look invalid.

---

## Chrome extension

`manifest.json` lives at the project root, so **this folder is the extension**.

1. `chrome://extensions` → enable *Developer mode* → **Load unpacked** → pick this folder.
2. Click the toolbar icon → paste your key → prompt → **Generate**.
3. The popup is cramped, so an **Open in tab** button appears; the layout also collapses to one
   column below the 780px media query breakpoint.

MV3, `action.default_popup = "web/index.html"`,
`host_permissions: ["https://apihub.agnes-ai.com/*"]`, icons 16/48/128 (generated with a pure-Python
zlib + struct PNG writer: rounded square, violet→teal gradient, white play triangle).

Extension pages bypass CORS, which makes this the most reliable path when a direct fetch is blocked.
`web/app.js` detects `chrome-extension:` and defaults to the **direct** transport. A key pasted in the
popup lives in that extension page's `sessionStorage` only.

---

## Tests

```bash
python3 -m unittest -v      # 17 tests, offline, no key, no credits
```

Covers payload defaults, flash + 1080P rejection, duration bounds, empty prompt, keyframe needing a
frame, text mode never sending media, reference sorting by extension, flash reference limits, seed
passthrough, task-id extraction priority, nested / percent snapshot parsing, failure parsing,
non-URL rejection, `.env` parsing, and the “only a real 401/403 rejects a key” rule.

### End-to-end against a local mock (never burns credits)

Point the client at a mock on `127.0.0.1:8899` that returns a task, then `processing 30% → 70% →
completed` with a `video_url`, and serves a fake mp4:

```bash
AGNES_BASE_URL=http://127.0.0.1:8899 python3 agnes_video.py \
  --key test --prompt "x" --poll-interval 0.2 --out /tmp/o.mp4
# → status lines, url, saved: /tmp/o.mp4, exit 0

AGNES_BASE_URL=http://127.0.0.1:8899 python3 agnes_server.py --port 8777 &
curl -s localhost:8777/api/health
curl -s -X POST localhost:8777/api/key  -H 'Content-Type: application/json' -d '{"key":"test"}'
curl -s -X POST localhost:8777/api/tasks -H 'Content-Type: application/json' \
     -d '{"payload":{"prompt":"x"}}'
curl -s "localhost:8777/api/tasks?video_id=video_100&model_name=agnes-video-2.5-flash"
curl -s -o /tmp/o.mp4 "localhost:8777/api/file?url=<the returned url>"
curl -s -o /dev/null -w '%{http_code}\n' "localhost:8777/api/file?url=https://example.com/x.mp4"  # 403
```

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| **401 / 403** `Invalid API key` | key wrong, expired, revoked, or lacks access to the model | re-copy the key from the platform; a `401`/`403` is the *only* thing that means “bad key” |
| **400** `size must be 720P` | `agnes-video-2.5-flash` only supports 720P | use `720P` with flash, or switch to `agnes-video-2.5` for 1080P / 1K / 2K. The UI auto-locks the select; the CLI rejects it before sending |
| **400** other | mode/media mismatch, `seconds` outside 4–12, `aspect_ratio: auto`, `n != 1` | the CLI/UI validate all of this locally — if you hit it, you called the API directly |
| **429** | ~16 requests/min exceeded | the client backs off exponentially; lower `--poll-interval`, run fewer parallel jobs |
| **`TLS/SSL connection has been closed`**, `connection reset`, `timed out`, `[Errno 111]` | the **serving machine is firewalled** from `apihub.agnes-ai.com` | **not a bad key.** Switch the web UI's Network dropdown to **direct**, or use the **Chrome extension**, or run the CLI on an unfiltered network |
| UI: “Could not reach the local server” | `agnes_server.py` isn't running | `python3 agnes_server.py`, or switch Network to `direct` |
| UI: “this is a network/CORS problem, not a bad key” | browser blocked the direct call | use `server` transport or the extension |
| Key rejected immediately, no network delay | genuine `401`/`403` from `/v1/models` | check the key |
| `404` on poll | unknown `video_id` | use the `video_id` from the create response |
| Task stuck `queued`/`processing` | queue depth | polling stops at 15 min; the task can still finish — check the platform with the `video_id` |
| `Download` fails in direct mode | CDN needs an authenticated request | use `server` transport: `/api/file` streams it through Python, dodging CORS and CDN tokens |
| Video URL expires | provider retention | regenerate, or copy the file with `Download mp4` |

---

## Layout

```
agnes_api.py        all API logic: payload validation, create, poll, snapshot, download, verify
agnes_video.py      CLI
agnes_server.py     http.server + /api/* + web UI host (holds the key in memory)
test_agnes_api.py   17 offline unit tests
web/index.html      studio UI
web/style.css       dark theme, single file
web/app.js          UI logic, both transports, chrome-extension detection
manifest.json       MV3 manifest — this folder IS the extension
icons/              16 / 48 / 128 png, generated with zlib + struct
.env.example        AGNES_API_KEY=, AGNES_BASE_URL=
```

`agnes_api.py` exports: `load_dotenv()`, `resolve_api_key()`, `build_payload()`, `create_task()`,
`get_task()`, `wait_for_video()`, `download()`, plus `find_first()`, `task_ids()`, `snapshot()` and
`AgnesError` (carrying `.status`). `snapshot()` normalises unknown response shapes: it walks the whole
JSON tree breadth-first with key priority (`video_id` before `id`, `video_url`/`download_url` before
`url`), accepts `"37%"` strings, treats `completed/succeeded/success/done` and
`failed/error/cancelled` as terminal, and only accepts URLs that start with `http`.

## License

MIT — do what you like, keep your key to yourself.
