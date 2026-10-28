/* Agnes Studio — front-end.
 *
 * Two transports, switchable in the "Network" dropdown:
 *   server  → /api/* on the local Python server (holds the key, proxies files)
 *   direct  → https://apihub.agnes-ai.com from this page with the Bearer token
 *
 * Inside the Chrome extension there is no local server, so the page defaults to
 * direct and offers "Open in tab".
 */
(function () {
  "use strict";

  var DEFAULT_BASE = "https://apihub.agnes-ai.com";
  var POLL_INTERVAL = 8000;        // server transport
  var DIRECT_POLL_INTERVAL = 7000; // direct transport
  var MAX_WAIT = 15 * 60 * 1000;   // 15 minutes
  var IS_EXTENSION = location.protocol === "chrome-extension:";

  var PRESETS = {
    cinematic: "cinematic lighting, shallow depth of field, slow tracking shot, film grain",
    neon: "neon night, rain-slicked streets, magenta and cyan rim light, reflective puddles",
    product: "clean product hero shot, soft studio key light, subtle rotation, seamless background",
    aerial: "aerial drone shot, sweeping descent, golden hour haze, wide establishing view",
    dreamy: "dreamy diffusion, soft pastel haze, gentle slow motion, floating particles",
    macro: "extreme macro detail, razor-thin focus plane, dew and micro texture, probe lens"
  };

  var SIZES_BY_MODEL = {
    "agnes-video-2.5-flash": ["720P"],
    "agnes-video-2.5": ["720P", "1080P", "1K", "2K"],
    "agnes-video-v2.0": ["720P", "1080P", "1K", "2K"]
  };

  var IMAGE_EXTS = ["png", "jpg", "jpeg", "webp", "gif", "bmp", "avif", "heic", "tif", "tiff"];
  var AUDIO_EXTS = ["mp3", "wav", "m4a", "aac", "ogg", "oga", "flac", "opus", "aiff", "wma"];
  var VIDEO_EXTS = ["mp4", "mov", "webm", "mkv", "m4v", "avi", "mpeg", "mpg"];

  // ── elements ─────────────────────────────────────────────
  var el = {};
  ["prompt", "chips", "model", "aspect", "mode", "size", "sizeNote", "seconds",
   "secondsLabel", "seed", "firstFrame", "lastFrame", "references", "keyframeFields",
   "referenceFields", "referenceNote", "generate", "generateLabel", "spinner", "stop",
   "costHint", "emptyState", "stage", "video", "progressWrap", "progressBar",
   "statusText", "elapsedText", "videoId", "videoUrl", "download", "copyLink",
   "regenerate", "openRemote", "errorBox", "errorTitle", "errorText", "historyList",
   "historyEmpty", "clearHistory", "apiKey", "saveKey", "keyPill", "transport",
   "transportHint", "openTab", "footerStatus", "shortcutHint"
  ].forEach(function (id) { el[id] = document.getElementById(id); });

  // ── state ────────────────────────────────────────────────
  var state = {
    key: "",
    transport: "server",
    serverKeyConfigured: false,
    baseUrl: DEFAULT_BASE,
    task: null,          // {video_id, model, prompt, startedAt, transport}
    stopped: false,
    timer: null,
    busy: false,
    history: []
  };

  // ── tiny utils ───────────────────────────────────────────
  function $(id) { return document.getElementById(id); }
  function show(node, visible) {
    if (!node) return;
    node.classList.toggle("hidden", !visible);
  }
  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }
  function fmtElapsed(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    if (s < 60) return s + "s";
    return Math.floor(s / 60) + "m " + (s % 60) + "s";
  }
  function toast(message) {
    var node = document.createElement("div");
    node.className = "toast";
    node.textContent = message;
    document.body.appendChild(node);
    requestAnimationFrame(function () { node.classList.add("show"); });
    setTimeout(function () {
      node.classList.remove("show");
      setTimeout(function () { node.remove(); }, 250);
    }, 2200);
  }
  function pill(text, kind, title) {
    el.keyPill.textContent = text;
    el.keyPill.className = "pill " + (kind || "pill-muted");
    if (title) el.keyPill.title = title;
  }
  function errorText(title, message) {
    el.errorTitle.textContent = title;
    el.errorText.textContent = message;
    show(el.errorBox, true);
  }
  function clearError() { show(el.errorBox, false); }

  function classify(url) {
    var clean = String(url || "").split("?")[0].split("#")[0];
    var ext = (clean.split(".").pop() || "").toLowerCase();
    if (AUDIO_EXTS.indexOf(ext) >= 0) return "audios";
    if (VIDEO_EXTS.indexOf(ext) >= 0) return "videos";
    return "images";
  }

  function sortReferences(text) {
    var buckets = { images: [], audios: [], videos: [] };
    String(text || "").split(/[\s,]+/).forEach(function (raw) {
      var url = raw.trim();
      if (!url) return;
      buckets[classify(url)].push(url);
    });
    return buckets;
  }

  function isHttpUrl(value) {
    return /^https?:\/\/\S+$/i.test(String(value || "").trim());
  }

  // ── payload (mirrors agnes_api.build_payload) ────────────
  function buildPayload() {
    var prompt = el.prompt.value.trim();
    if (!prompt) return { error: "Write a prompt first.", field: "prompt" };

    var model = el.model.value;
    var mode = el.mode.value;
    var seconds = parseInt(el.seconds.value, 10);
    var size = el.size.value;
    var aspect = el.aspect.value;
    var seedRaw = el.seed.value.trim();
    var seed = null;

    if (seedRaw) {
      if (!/^-?\d+$/.test(seedRaw)) {
        return { error: "Seed must be a whole number.", field: "seed" };
      }
      seed = parseInt(seedRaw, 10);
    }
    if (!(seconds >= 4 && seconds <= 12)) {
      return { error: "Duration must be between 4 and 12 seconds.", field: "seconds" };
    }
    if (SIZES_BY_MODEL[model] && SIZES_BY_MODEL[model].indexOf(size) < 0) {
      return { error: "This model only supports " + SIZES_BY_MODEL[model].join(", ") + ".",
               field: "size" };
    }

    var payload = {
      model: model,
      prompt: prompt,
      mode: mode,
      seconds: String(seconds),
      size: size,
      aspect_ratio: aspect
    };
    if (seed !== null) payload.seed = seed;

    if (mode === "keyframe") {
      var first = el.firstFrame.value.trim();
      var last = el.lastFrame.value.trim();
      if (!first && !last) {
        return { error: "Keyframe mode needs a first-frame or last-frame URL.", field: "firstFrame" };
      }
      if (first && !isHttpUrl(first)) return { error: "First frame must be a public http(s) URL.", field: "firstFrame" };
      if (last && !isHttpUrl(last)) return { error: "Last frame must be a public http(s) URL.", field: "lastFrame" };
      if (first) payload.first_frame = first;
      if (last) payload.last_frame = last;
    } else if (mode === "reference") {
      var buckets = sortReferences(el.references.value);
      var total = buckets.images.length + buckets.audios.length + buckets.videos.length;
      if (!total) {
        return { error: "Reference mode needs at least one image, audio or video URL.", field: "references" };
      }
      ["images", "audios", "videos"].forEach(function (bucket) {
        buckets[bucket].forEach(function (url) {
          if (!isHttpUrl(url)) throw new Error("Reference URLs must be public http(s) URLs: " + url);
        });
      });
      if (buckets.images.length) payload.images = buckets.images;
      if (buckets.audios.length) payload.audios = buckets.audios;
      if (buckets.videos.length) {
        payload.videos = buckets.videos.map(function (url) { return { url: url }; });
      }
      if (model === "agnes-video-2.5-flash") {
        if (payload.images && payload.images.length > 5) {
          return { error: "Flash supports at most 5 reference images.", field: "references" };
        }
        if (payload.audios && payload.audios.length > 3) {
          return { error: "Flash supports at most 3 reference audio clips.", field: "references" };
        }
        if (payload.videos) {
          return { error: "Flash does not support reference videos — pick the 2.5 model.", field: "references" };
        }
      }
    }
    return { payload: payload };
  }

  // ── transports ───────────────────────────────────────────
  function apiBase() {
    return (state.baseUrl || DEFAULT_BASE).replace(/\/+$/, "").replace(/\/v1$/, "");
  }

  function directHeaders(extra) {
    var headers = { "Content-Type": "application/json", "Accept": "application/json" };
    if (state.key) headers["Authorization"] = "Bearer " + state.key;
    return Object.assign(headers, extra || {});
  }

  function corsMessage(detail) {
    return (detail ? detail + "\n\n" : "") +
      "This is a network/CORS problem, not a bad key. The browser blocked the request " +
      "to apihub.agnes-ai.com before the API ever saw your key.\n\n" +
      "Fixes: (1) switch Network to \"server\" and run python3 agnes_server.py, " +
      "(2) use the Chrome extension (extension pages bypass CORS), or " +
      "(3) use the CLI.";
  }

  async function serverFetch(path, options) {
    var response;
    try {
      response = await fetch(path, options);
    } catch (err) {
      throw new Error("Could not reach the local server (" + err.message +
        "). Start it with: python3 agnes_server.py — or switch Network to \"direct\".");
    }
    var data = null;
    try { data = await response.json(); } catch (err) { data = {}; }
    if (!response.ok || (data && data.ok === false)) {
      var error = new Error((data && data.error) || ("server returned HTTP " + response.status));
      error.status = response.status;
      error.payload = data;
      throw error;
    }
    return data;
  }

  async function directFetch(url, options) {
    var response;
    try {
      response = await fetch(url, options);
    } catch (err) {
      throw new Error(corsMessage("The request to " + apiBase() + " failed (" + err.message + ")."));
    }
    var text = await response.text();
    var data;
    try { data = text ? JSON.parse(text) : {}; } catch (err) { data = { raw: text }; }
    if (!response.ok) {
      var message = extractError(data) || text || ("HTTP " + response.status);
      var error = new Error(message);
      error.status = response.status;
      error.payload = data;
      throw error;
    }
    return data;
  }

  function extractError(data) {
    if (!data || typeof data !== "object") return "";
    if (typeof data.error === "string") return data.error;
    if (data.error && typeof data.error === "object") {
      return data.error.message || data.error.msg || JSON.stringify(data.error);
    }
    return data.message || data.detail || "";
  }

  // ── response normalisation (mirrors agnes_api.snapshot) ──
  function walk(node, visit, depth) {
    depth = depth || 0;
    if (depth > 24 || node === null || node === undefined) return;
    if (Array.isArray(node)) {
      node.forEach(function (item) { walk(item, visit, depth + 1); });
    } else if (typeof node === "object") {
      visit(node);
      Object.keys(node).forEach(function (key) { walk(node[key], visit, depth + 1); });
    }
  }

  function snapshot(data) {
    var rawStatus = null, progress = null, videoUrl = null;
    var statusKeys = ["status", "state", "task_status", "video_status", "generation_status"];
    var progressKeys = ["progress", "progress_percent", "percent", "percentage", "pct"];
    var urlKeys = ["video_url", "videoUrl", "download_url", "downloadUrl", "url", "file_url"];

    walk(data, function (node) {
      if (rawStatus === null) {
        for (var i = 0; i < statusKeys.length; i += 1) {
          var value = node[statusKeys[i]];
          if (typeof value === "string" && value.trim()) { rawStatus = value.trim(); break; }
        }
      }
    });
    walk(data, function (node) {
      if (progress === null) {
        for (var i = 0; i < progressKeys.length; i += 1) {
          if (node[progressKeys[i]] !== undefined) {
            var p = parseProgress(node[progressKeys[i]]);
            if (p !== null) { progress = p; break; }
          }
        }
      }
    });
    for (var u = 0; u < urlKeys.length && !videoUrl; u += 1) {
      walk(data, function (node) {
        if (videoUrl) return;
        var candidate = node[urlKeys[u]];
        if (typeof candidate === "string" && isHttpUrl(candidate)) videoUrl = candidate.trim();
      });
    }

    var status = String(rawStatus || "").toLowerCase().replace(/[\s-]+/g, "_");
    var map = {
      completed: "completed", complete: "completed", succeeded: "completed",
      success: "completed", done: "completed", finished: "completed", ready: "completed",
      failed: "failed", fail: "failed", failure: "failed", error: "failed",
      cancelled: "failed", canceled: "failed", expired: "failed",
      queued: "queued", pending: "queued", waiting: "queued", submitted: "queued"
    };
    status = map[status] || status || (videoUrl ? "completed" : "unknown");

    var message = null;
    if (status === "failed") {
      walk(data, function (node) {
        if (message) return;
        if (node.error) {
          message = typeof node.error === "string" ? node.error
            : (node.error.message || node.error.msg || "");
        }
      });
      message = message || "Video generation failed";
    }
    return { status: status, progress: progress, video_url: videoUrl, error: message };
  }

  function parseProgress(value) {
    if (value === null || value === undefined || typeof value === "boolean") return null;
    if (typeof value === "number") {
      var n = value > 0 && value <= 1 ? value * 100 : value;
      return Math.max(0, Math.min(100, Math.round(n)));
    }
    if (typeof value === "string") {
      var text = value.trim();
      if (!text) return null;
      var percent = text.indexOf("%") >= 0;
      var num = parseFloat(text.replace("%", ""));
      if (isNaN(num)) return null;
      if (!percent && num > 0 && num <= 1) num *= 100;
      return Math.max(0, Math.min(100, Math.round(num)));
    }
    return null;
  }

  function extractIds(data) {
    var videoId = null, taskId = null, model = null, id = null;
    function pick(keys) {
      var found = null;
      keys.forEach(function (key) {
        if (found) return;
        walk(data, function (node) {
          if (!found && typeof node[key] === "string" && node[key].trim()) found = node[key].trim();
        });
      });
      return found;
    }
    videoId = pick(["video_id", "videoId"]);
    taskId = pick(["task_id", "taskId"]);
    id = pick(["id"]);
    model = pick(["model", "model_name"]);
    return { video_id: videoId || taskId || id, task_id: taskId || id, id: id, model: model };
  }

  // ── UI helpers ───────────────────────────────────────────
  function syncModel() {
    var model = el.model.value;
    var allowed = SIZES_BY_MODEL[model] || ["720P"];
    var locked = allowed.length === 1;
    if (locked) {
      el.size.value = allowed[0];
      el.size.disabled = true;
      el.sizeNote.textContent = "This model is locked to " + allowed[0] + ".";
    } else {
      el.size.disabled = false;
      if (allowed.indexOf(el.size.value) < 0) el.size.value = allowed[0];
      el.sizeNote.textContent = "720P · 1080P · 1K · 2K available.";
    }
    ["1080P", "1K", "2K"].forEach(function (size) {
      var option = el.size.querySelector('option[value="' + size + '"]');
      if (option) option.disabled = locked;
    });
    updateCost();
  }

  function updateCost() {
    var seconds = parseInt(el.seconds.value, 10) || 5;
    var model = el.model.value;
    if (model === "agnes-video-2.5-flash") {
      el.costHint.textContent = seconds + "s · flash is currently free";
      return;
    }
    var rates = { "720P": 0.025, "1080P": 0.04, "1K": 0.04, "2K": 0.055 };
    var rate = rates[el.size.value] || 0.025;
    el.costHint.textContent = "est. $" + (seconds * rate).toFixed(3) + " at " + el.size.value;
  }

  function syncMode() {
    var mode = el.mode.value;
    show(el.keyframeFields, mode === "keyframe");
    show(el.referenceFields, mode === "reference");
    if (mode === "reference") {
      var buckets = sortReferences(el.references.value);
      el.referenceNote.textContent =
        "Detected: " + buckets.images.length + " image(s), " + buckets.audios.length +
        " audio, " + buckets.videos.length + " video — refer to them as <Picture 1>, " +
        "<Audio 1>, <Video 1>.";
    }
  }

  function setBusy(busy) {
    state.busy = busy;
    el.generate.disabled = busy;
    show(el.spinner, busy);
    el.generateLabel.textContent = busy ? "Generating…" : "Generate";
    show(el.stop, busy);
  }

  function startTimer() {
    stopTimer();
    state.timer = setInterval(function () {
      if (!state.task) return;
      el.elapsedText.textContent = fmtElapsed(Date.now() - state.task.startedAt);
    }, 1000);
  }

  function stopTimer() {
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
  }

  function setProgress(percent) {
    var value = percent === null || percent === undefined ? 4 : Math.max(4, percent);
    el.progressBar.style.width = value + "%";
  }

  function playableUrl(entry) {
    if (!entry || !entry.url) return "";
    if (entry.transport === "server") {
      return "/api/file?url=" + encodeURIComponent(entry.url);
    }
    return entry.url;
  }

  // ── history ──────────────────────────────────────────────
  function loadHistory() {
    try {
      var raw = sessionStorage.getItem("agnes.history");
      state.history = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(state.history)) state.history = [];
    } catch (err) {
      state.history = [];
    }
    renderHistory();
  }

  function saveHistory() {
    try { sessionStorage.setItem("agnes.history", JSON.stringify(state.history.slice(0, 12))); }
    catch (err) { /* sessionStorage full or unavailable — history is a nicety */ }
  }

  function addHistory(entry) {
    state.history.unshift(entry);
    state.history = state.history.slice(0, 12);
    saveHistory();
    renderHistory();
  }

  function renderHistory() {
    el.historyList.innerHTML = "";
    show(el.historyEmpty, state.history.length === 0);
    state.history.forEach(function (entry) {
      var li = document.createElement("li");
      li.className = "history-item";
      if (state.task && state.task.video_id === entry.video_id) li.classList.add("active");
      li.title = entry.prompt || "";

      var src = playableUrl(entry);
      if (src) {
        var videoEl = document.createElement("video");
        videoEl.src = src;
        videoEl.muted = true;
        videoEl.playsInline = true;
        videoEl.preload = "metadata";
        videoEl.addEventListener("loadedmetadata", function () {
          // Nudge to the first frame so the tile is not a black rectangle.
          try { videoEl.currentTime = 0.1; } catch (err) { /* ignore */ }
        });
        li.appendChild(videoEl);

        li.addEventListener("mousemove", function (event) {
          if (!isFinite(videoEl.duration) || videoEl.duration <= 0) return;
          var rect = li.getBoundingClientRect();
          var ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
          try { videoEl.currentTime = ratio * videoEl.duration; } catch (err) { /* ignore */ }
        });
        li.addEventListener("mouseleave", function () {
          try { videoEl.currentTime = 0.1; } catch (err) { /* ignore */ }
        });
      } else {
        var fallback = document.createElement("div");
        fallback.className = "thumb-fallback";
        fallback.textContent = "expired?";
        li.appendChild(fallback);
      }

      var caption = document.createElement("div");
      caption.className = "history-caption";
      var when = document.createElement("span");
      when.textContent = new Date(entry.createdAt || Date.now()).toLocaleTimeString([], {
        hour: "2-digit", minute: "2-digit"
      });
      var meta = document.createElement("span");
      meta.textContent = (entry.seconds || "?") + "s · " + (entry.aspect || "16:9");
      caption.appendChild(when);
      caption.appendChild(meta);
      li.appendChild(caption);

      var promptNode = document.createElement("div");
      promptNode.className = "history-prompt";
      promptNode.textContent = entry.prompt || "(no prompt)";
      li.appendChild(promptNode);

      li.addEventListener("click", function () { loadEntry(entry); });
      el.historyList.appendChild(li);
    });
  }

  function loadEntry(entry) {
    clearError();
    show(el.emptyState, false);
    show(el.stage, true);
    state.task = {
      video_id: entry.video_id, model: entry.model, prompt: entry.prompt,
      startedAt: Date.now(), transport: entry.transport
    };
    el.video.src = playableUrl(entry);
    el.videoId.textContent = entry.video_id || "—";
    el.videoUrl.textContent = entry.url || "—";
    el.videoUrl.title = entry.url || "";
    el.download.href = playableUrl(entry);
    el.download.setAttribute("download", "agnes-" + (entry.video_id || "video") + ".mp4");
    show(el.openRemote, !!entry.url);
    el.openRemote.dataset.url = entry.url || "";
    el.progressBar.style.width = "100%";
    el.statusText.textContent = "completed";
    el.elapsedText.textContent = "saved";
    renderHistory();
  }

  // ── generation ───────────────────────────────────────────
  async function generate() {
    if (state.busy) return;
    clearError();

    var built;
    try {
      built = buildPayload();
    } catch (err) {
      errorText("Check the reference URLs", err.message);
      return;
    }
    if (built.error) {
      errorText("Check the composer", built.error);
      return;
    }
    var payload = built.payload;

    if (state.transport === "direct" && !state.key) {
      errorText("No key", "Paste your Agnes API key at the top first. It is kept in " +
        "this tab's sessionStorage only.");
      return;
    }
    if (state.transport === "server" && !state.serverKeyConfigured) {
      errorText("No key on the server",
        "Paste your key and press Save key — the local server keeps it in memory.");
      return;
    }

    setBusy(true);
    state.stopped = false;
    state.task = {
      video_id: null, model: payload.model, prompt: payload.prompt,
      startedAt: Date.now(), transport: state.transport
    };
    show(el.emptyState, false);
    show(el.stage, true);
    el.video.removeAttribute("src");
    el.video.load();
    el.videoId.textContent = "—";
    el.videoUrl.textContent = "—";
    el.download.removeAttribute("href");
    show(el.openRemote, false);
    el.statusText.textContent = "creating task…";
    el.elapsedText.textContent = "0s";
    setProgress(3);
    startTimer();

    try {
      var created, ids;
      if (state.transport === "server") {
        created = await serverFetch("/api/tasks", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ payload: payload })
        });
        ids = { video_id: created.video_id, model: created.model || payload.model };
      } else {
        created = await directFetch(apiBase() + "/v1/videos", {
          method: "POST", headers: directHeaders(), body: JSON.stringify(payload)
        });
        ids = extractIds(created);
      }
      if (!ids.video_id) throw new Error("The create response had no video_id.");
      state.task.video_id = ids.video_id;
      state.task.model = ids.model || payload.model;
      el.videoId.textContent = ids.video_id;
      el.statusText.textContent = "queued";
      setProgress(6);

      var snap = await pollUntilDone(state.task);
      if (!snap) return; // stopped by user

      el.statusText.textContent = "completed";
      el.progressBar.style.width = "100%";
      stopTimer();
      el.elapsedText.textContent = fmtElapsed(Date.now() - state.task.startedAt);

      var entry = {
        video_id: state.task.video_id,
        model: state.task.model,
        prompt: payload.prompt,
        url: snap.video_url,
        seconds: payload.seconds,
        aspect: payload.aspect_ratio,
        transport: state.transport,
        createdAt: Date.now()
      };
      state.task.transport = state.transport;
      showEntry(entry);
      addHistory(entry);
    } catch (err) {
      if (err && err.stopped) {
        el.statusText.textContent = "stopped";
        toast("Polling stopped — the task may still finish on the server.");
      } else {
        el.statusText.textContent = "failed";
        errorText(errorTitleFor(err), err.message || String(err));
      }
    } finally {
      stopTimer();
      setBusy(false);
    }
  }

  function errorTitleFor(err) {
    if (err && err.status === 401) return "Key rejected (401)";
    if (err && err.status === 403) return "Key not authorized (403)";
    if (err && err.status === 400) return "Request rejected (400)";
    if (err && err.status === 429) return "Rate limited (429)";
    return "Generation failed";
  }

  function showEntry(entry) {
    var src = playableUrl(entry);
    show(el.emptyState, false);
    show(el.stage, true);
    el.video.src = src;
    el.videoId.textContent = entry.video_id || "—";
    el.videoUrl.textContent = entry.url || "—";
    el.videoUrl.title = entry.url || "";
    el.download.href = src || "#";
    el.download.setAttribute("download", "agnes-" + (entry.video_id || "video") + ".mp4");
    show(el.openRemote, !!entry.url);
    el.openRemote.dataset.url = entry.url || "";
  }

  function stoppedError() {
    var err = new Error("stopped");
    err.stopped = true;
    return err;
  }

  async function pollUntilDone(task) {
    var interval = state.transport === "server" ? POLL_INTERVAL : DIRECT_POLL_INTERVAL;
    var started = Date.now();
    var backoff = 0;
    var last = null;

    while (true) {
      if (state.stopped) throw stoppedError();
      if (Date.now() - started > MAX_WAIT) {
        throw new Error("Gave up after 15 minutes. The task may still complete — " +
          "check the video_id in the Agnes dashboard.");
      }
      await sleep(interval + backoff);
      if (state.stopped) throw stoppedError();

      var raw;
      try {
        raw = await pollOnce(task);
        backoff = 0;
      } catch (err) {
        if (err.status === 429) {
          backoff = Math.min(30000, backoff ? backoff * 2 : 4000);
          el.statusText.textContent = "rate limited — backing off";
          continue;
        }
        if (err.status === 400 || err.status === 404) throw err;
        if (err.status >= 500 || !err.status) {
          // Transient: keep polling until the 15 minute cap.
          el.statusText.textContent = "network hiccup — retrying";
          continue;
        }
        throw err;
      }

      last = snapshot(raw);
      if (last.progress !== null) setProgress(last.progress);
      el.statusText.textContent = (last.status || "processing") +
        (last.progress !== null ? " " + last.progress + "%" : "");
      el.elapsedText.textContent = fmtElapsed(Date.now() - started);

      if (last.status === "completed" && last.video_url) return last;
      if (last.status === "failed") throw new Error(last.error || "Video generation failed");
    }
  }

  async function pollOnce(task) {
    if (state.transport === "server") {
      var query = "/api/tasks?video_id=" + encodeURIComponent(task.video_id) +
                  "&model_name=" + encodeURIComponent(task.model);
      var data = await serverFetch(query, { headers: { "Accept": "application/json" } });
      return data.snapshot || data.raw || data;
    }
    var base = apiBase();
    var url = base + "/agnesapi?video_id=" + encodeURIComponent(task.video_id) +
              "&model_name=" + encodeURIComponent(task.model);
    try {
      return await directFetch(url, { headers: directHeaders() });
    } catch (err) {
      if (err.status === 400 || err.status === 404) {
        // Bare video_id form (text mode fallback).
        return await directFetch(base + "/agnesapi?video_id=" +
          encodeURIComponent(task.video_id), { headers: directHeaders() });
      }
      throw err;
    }
  }

  // ── key handling ─────────────────────────────────────────
  async function saveKey() {
    var key = el.apiKey.value.trim();
    if (!key) {
      errorText("No key", "Paste an Agnes API key first.");
      return;
    }
    state.key = key;
    try { sessionStorage.setItem("agnes.key", key); } catch (err) { /* private mode */ }

    if (state.transport === "direct" || IS_EXTENSION) {
      pill("key saved · checking…", "pill-muted");
      await verifyDirectKey(key);
      return;
    }

    pill("checking…", "pill-muted");
    try {
      var result = await serverFetch("/api/key", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: key })
      });
      state.serverKeyConfigured = true;
      if (result.warning) {
        pill("key saved · unverified", "pill-warn", result.warning);
        toast("Key saved. Verification failed for a network reason — it is not a bad key.");
      } else {
        pill("key verified", "pill-ok", "The API accepted this key.");
      }
    } catch (err) {
      if (err.status === 401 || err.status === 403) {
        state.serverKeyConfigured = false;
        pill("key rejected", "pill-bad", err.message);
        errorText("Key rejected", "The API answered " + err.status + ": " + err.message +
          "\n\nA real 401/403 is the only case where a key is rejected.");
      } else {
        state.serverKeyConfigured = true;
        pill("key saved · unverified", "pill-warn", err.message);
        toast("Key saved, but verification could not run: " + err.message);
      }
    }
  }

  async function verifyDirectKey(key) {
    try {
      var response = await fetch(apiBase() + "/v1/models", { headers: directHeaders() });
      if (response.status === 401 || response.status === 403) {
        pill("key rejected", "pill-bad", "HTTP " + response.status);
        errorText("Key rejected (HTTP " + response.status + ")",
          "The API explicitly rejected this key. Check that it is copied fully and active.");
        return;
      }
      if (!response.ok) {
        pill("key saved · unverified", "pill-warn", "models returned HTTP " + response.status);
        toast("Key saved — verification was inconclusive (HTTP " + response.status + ").");
        return;
      }
      pill("key verified", "pill-ok", "The API accepted this key.");
    } catch (err) {
      pill("key saved · unverified", "pill-warn", err.message);
      toast("Key saved. Verification failed for a network/CORS reason — it is not a bad key.");
    }
  }

  // ── network switch ───────────────────────────────────────
  function applyTransport(transport) {
    state.transport = transport;
    el.transport.value = transport;
    try { sessionStorage.setItem("agnes.transport", transport); } catch (err) { /* ignore */ }
    el.transportHint.textContent = transport === "server"
      ? "local server proxy · key stays in Python memory"
      : "browser → apihub.agnes-ai.com · key in this tab";
    if (transport === "server") {
      reflectServerKey();
    } else if (state.key) {
      pill(state.key ? "key in this tab" : "no key", "pill-muted");
    } else {
      pill("no key", "pill-muted");
    }
    checkHealth();
  }

  function reflectServerKey() {
    if (state.serverKeyConfigured) {
      pill("key loaded on server", "pill-ok");
    } else {
      pill("no key on server", "pill-muted");
    }
  }

  async function checkHealth() {
    if (state.transport !== "server" && !IS_EXTENSION) {
      el.footerStatus.textContent = "transport: browser direct";
      return;
    }
    if (IS_EXTENSION) {
      el.footerStatus.textContent = "extension · browser direct";
      return;
    }
    try {
      var response = await fetch("/api/health", { headers: { "Accept": "application/json" } });
      if (!response.ok) throw new Error("HTTP " + response.status);
      var health = await response.json();
      state.baseUrl = health.base_url || DEFAULT_BASE;
      state.serverKeyConfigured = !!health.key_configured;
      el.footerStatus.textContent = "server: online · " + health.base_url;
      if (state.transport === "server") reflectServerKey();
    } catch (err) {
      el.footerStatus.textContent = "server: not running (use direct or the extension)";
    }
  }

  // ── wiring ───────────────────────────────────────────────
  function bind() {
    el.chips.addEventListener("click", function (event) {
      var chip = event.target.closest(".chip");
      if (!chip) return;
      var preset = PRESETS[chip.dataset.preset];
      if (!preset) return;
      var current = el.prompt.value.trim();
      el.prompt.value = current ? current.replace(/[\s,]+$/, "") + ", " + preset : preset;
      el.prompt.focus();
      el.prompt.setSelectionRange(el.prompt.value.length, el.prompt.value.length);
    });

    el.prompt.addEventListener("keydown", function (event) {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        generate();
      }
    });

    el.model.addEventListener("change", syncModel);
    el.size.addEventListener("change", updateCost);
    el.seconds.addEventListener("input", function () {
      el.secondsLabel.textContent = el.seconds.value + "s";
      updateCost();
    });
    el.mode.addEventListener("change", syncMode);
    el.references.addEventListener("input", syncMode);

    el.generate.addEventListener("click", generate);
    el.stop.addEventListener("click", function () {
      state.stopped = true;
      el.stop.disabled = true;
      setTimeout(function () { el.stop.disabled = false; }, 800);
    });

    el.saveKey.addEventListener("click", saveKey);
    el.apiKey.addEventListener("keydown", function (event) {
      if (event.key === "Enter") { event.preventDefault(); saveKey(); }
    });

    el.transport.addEventListener("change", function () {
      applyTransport(el.transport.value);
    });

    el.copyLink.addEventListener("click", async function () {
      var url = el.videoUrl.textContent;
      if (!url || url === "—") { toast("Nothing to copy yet."); return; }
      try {
        await navigator.clipboard.writeText(url);
        toast("Video URL copied.");
      } catch (err) {
        toast("Copy failed — select the URL manually.");
      }
    });

    el.regenerate.addEventListener("click", function () { generate(); });

    el.openRemote.addEventListener("click", function () {
      var url = el.openRemote.dataset.url;
      if (url) window.open(url, "_blank", "noopener");
    });

    el.clearHistory.addEventListener("click", function () {
      state.history = [];
      saveHistory();
      renderHistory();
    });

    el.openTab.addEventListener("click", function () {
      if (window.chrome && chrome.tabs && chrome.tabs.create && chrome.runtime) {
        chrome.tabs.create({ url: chrome.runtime.getURL("web/index.html") });
      } else {
        window.open(location.href, "_blank", "noopener");
      }
    });
  }

  function init() {
    bind();

    // Extension pages have no local server: default to direct + show "Open in tab".
    if (IS_EXTENSION) show(el.openTab, true);

    var savedTransport = null;
    var savedKey = "";
    try {
      savedTransport = sessionStorage.getItem("agnes.transport");
      savedKey = sessionStorage.getItem("agnes.key") || "";
    } catch (err) { /* storage unavailable */ }

    if (savedKey) {
      state.key = savedKey;
      el.apiKey.value = savedKey;
    }

    var transport = IS_EXTENSION ? "direct" : (savedTransport === "direct" ? "direct" : "server");
    applyTransport(transport);
    if (state.key && transport === "direct") pill("key in this tab", "pill-muted");

    el.secondsLabel.textContent = el.seconds.value + "s";
    syncModel();
    syncMode();
    loadHistory();
    updateCost();

    if (IS_EXTENSION) checkHealth();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
