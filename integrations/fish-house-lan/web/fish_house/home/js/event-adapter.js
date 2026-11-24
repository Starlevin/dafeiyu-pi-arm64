/* DaFeiYu Home R5.2 — Independent Real Chat Event Adapter Layer (Chrome 66 ES5)
 *
 * Bridges live backend transports into DaFeiYuEventEngine with zero chat-core coupling:
 *   1. SSE (EventSource) on /api/house/events (optional, auto-detected)
 *   2. WebSocket on /ws/house or /ws/v1 (optional, opt-in via config)
 *   3. HTTP Polling fallback on /fish_house/state.json + /api/status
 *
 * Direct parity with Codex's backend/status_adapter.py (commit f435016):
 *   - Supports normalized envelopes ({ seq, phase/type, source, verified, at, bubble })
 *   - Supports raw Codex activity events ({ time, phase, reason, success, failed, unknown, local_handoff })
 *   - Supports Codex bridge-health snapshots ({ last_event, last_success, last_failure, last_unknown })
 *   - Fail-closed 60s staleness guard & disconnect/reconnect exponential backoff
 *   - Built-in Dev Replay scenarios + ring-buffered Event Inspector log
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.DaFeiYuEventAdapter = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var DEFAULT_MAX_AGE_SEC = 60;
  var VISIBLE_RECEIPTS = { visible_outgoing: true, confirmed: true };
  var FAIL_REASONS = { send_exception: true, failed: true, error: true };

  // Direct JS port of Codex backend/status_adapter.py normalize_activity()
  function normalizeCodexActivity(ev, nowSec, maxAgeSec) {
    if (!ev || typeof ev !== "object") return "idle";
    var t = Number(ev.time);
    var maxA = (typeof maxAgeSec === "number") ? maxAgeSec : DEFAULT_MAX_AGE_SEC;
    if (isNaN(t) || !isFinite(t) || isNaN(maxA) || maxA < 0) return "idle";
    var age = Number(nowSec) - t;
    if (age < 0 || age > maxA) return "idle";

    if (typeof ev.phase !== "string" || typeof ev.reason !== "string") return "idle";
    var phase = ev.phase.replace(/^\s+|\s+$/g, "").toLowerCase();
    var reason = ev.reason.replace(/^\s+|\s+$/g, "").toLowerCase();

    if (ev.failed === true || ev.unknown === true || phase === "error" || phase === "failed") {
      return "error";
    }
    if (phase === "received" || phase === "receive") return "received";
    if (phase === "generation" && reason === "started") return "thinking";
    if (phase === "generation" && reason === "ready") return "replying";
    if (phase === "submit" && reason === "started") return "replying";
    if ((phase === "handoff" || phase === "local_handoff") && ev.local_handoff === true) {
      return "replying";
    }
    if (phase === "confirmation") {
      if (ev.success === true && VISIBLE_RECEIPTS[reason]) return "sent";
      if (ev.success === false || FAIL_REASONS[reason]) return "error";
    }
    return "idle";
  }

  function sameMarker(a, b) {
    if (!a || !b || typeof a !== "object" || typeof b !== "object") return false;
    var keys = ["time", "phase", "reason", "trace"];
    for (var i = 0; i < keys.length; i++) {
      if (a[keys[i]] !== b[keys[i]]) return false;
    }
    return true;
  }

  // Direct JS port of Codex backend/status_adapter.py normalize_bridge_health()
  function normalizeCodexBridgeHealth(snapshot, nowSec, maxAgeSec) {
    if (!snapshot || typeof snapshot !== "object") return "idle";
    var fields = [
      { key: "last_event", rank: 3 },
      { key: "last_success", rank: 0 },
      { key: "last_failure", rank: 1 },
      { key: "last_unknown", rank: 2 }
    ];
    var best = null;
    for (var i = 0; i < fields.length; i++) {
      var f = fields[i];
      var ev = snapshot[f.key];
      if (!ev || typeof ev !== "object") continue;
      var at = Number(ev.time);
      if (isNaN(at) || !isFinite(at)) continue;
      if (!best || at > best.at || (at === best.at && f.rank > best.rank)) {
        best = { at: at, rank: f.rank, ev: ev, key: f.key };
      }
    }
    if (!best) return "idle";

    var norm = {};
    var k;
    for (k in best.ev) {
      if (Object.prototype.hasOwnProperty.call(best.ev, k)) norm[k] = best.ev[k];
    }

    if (best.key !== "last_event") {
      norm.success = (best.key === "last_success");
      norm.failed = (best.key === "last_failure");
      norm.unknown = (best.key === "last_unknown");
      return normalizeCodexActivity(norm, nowSec, maxAgeSec);
    }

    if (sameMarker(snapshot.last_success, best.ev)) norm.success = true;
    if (sameMarker(snapshot.last_failure, best.ev)) norm.failed = true;
    if (sameMarker(snapshot.last_unknown, best.ev)) norm.unknown = true;
    return normalizeCodexActivity(norm, nowSec, maxAgeSec);
  }

  // Normalize any backend payload (canonical envelope, raw Codex activity, or bridge-health snapshot)
  function parseIncomingPayload(payload, nowMs) {
    if (!payload || typeof payload !== "object") return null;
    var nowSec = (nowMs || Date.now()) / 1000;

    // Case A: Codex bridge-health snapshot
    if (payload.last_event || payload.last_success || payload.last_failure || payload.last_unknown) {
      var stateA = normalizeCodexBridgeHealth(payload, nowSec, DEFAULT_MAX_AGE_SEC);
      var refEv = payload.last_event || payload.last_failure || payload.last_success || payload.last_unknown;
      return {
        id: payload.id || (refEv && refEv.trace ? String(refEv.trace) + ":" + stateA : ""),
        seq: payload.seq || 0,
        type: stateA,
        source: payload.source || "wechat",
        platform: payload.platform || payload.source || "wechat",
        verified: stateA !== "idle",
        at: refEv && refEv.time ? refEv.time : nowSec,
        text: payload.bubble || payload.text || ""
      };
    }

    // Case B: Raw Codex single activity object (has `time` + `phase` + `reason`)
    if (payload.time !== undefined && typeof payload.phase === "string" && typeof payload.reason === "string") {
      var stateB = normalizeCodexActivity(payload, nowSec, DEFAULT_MAX_AGE_SEC);
      return {
        id: payload.id || (payload.trace ? String(payload.trace) + ":" + stateB : ""),
        seq: payload.seq || 0,
        type: stateB,
        source: payload.source || "wechat",
        platform: payload.platform || payload.source || "wechat",
        verified: stateB !== "idle",
        at: payload.time,
        text: payload.bubble || payload.text || ""
      };
    }

    // Case C: Canonical / fish_house state.json envelope
    if (payload.type || payload.event || payload.phase) {
      return {
        id: payload.id || "",
        seq: Number(payload.seq || 0),
        type: payload.type || payload.event || payload.phase,
        source: payload.source || "demo",
        platform: payload.platform || payload.source || "demo",
        verified: payload.verified === true,
        terminal: payload.terminal === true,
        at: payload.at || payload.timestamp || nowSec,
        expiresAt: payload.expires_at,
        text: payload.text || payload.bubble || ""
      };
    }

    return null;
  }

  function xhrJson(url, timeoutMs, cb) {
    var xhr = new XMLHttpRequest();
    var done = false;
    var timer = setTimeout(function () {
      if (done) return;
      done = true;
      try { xhr.abort(); } catch (e) {}
      cb({ code: "timeout", status: 0, message: "状态拉取超时" }, null);
    }, timeoutMs || 4500);

    xhr.open("GET", url, true);
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4 || done) return;
      done = true;
      clearTimeout(timer);
      if (xhr.status < 200 || xhr.status >= 300) {
        cb({ code: "http_" + xhr.status, status: xhr.status, message: "HTTP " + xhr.status }, null);
        return;
      }
      try {
        cb(null, JSON.parse(xhr.responseText));
      } catch (e) {
        cb({ code: "bad_json", status: xhr.status, message: "JSON 解析失败" }, null);
      }
    };
    xhr.onerror = function () {
      if (done) return;
      done = true;
      clearTimeout(timer);
      cb({ code: "network", status: 0, message: "网络连接中断" }, null);
    };
    try { xhr.send(); } catch (e) {
      if (!done) {
        done = true;
        clearTimeout(timer);
        cb({ code: "send_fail", status: 0, message: "请求发送异常" }, null);
      }
    }
  }

  function createEventAdapter(options) {
    var opts = options || {};
    var engine = opts.engine;
    var basePath = opts.basePath || "";
    var pollMs = opts.pollMs || 2500;
    var sseUrl = opts.sseUrl || (basePath + "/api/house/events");
    var wsUrl = opts.wsUrl || "";
    var stateUrl = opts.stateUrl || (basePath + "/fish_house/state.json");
    var statusUrl = opts.statusUrl || (basePath + "/api/status");
    var onStatusData = opts.onStatusData || function () {};
    var onConnectionChange = opts.onConnectionChange || function () {};

    var running = false;
    var pausedForDemo = false;
    var sse = null;
    var ws = null;
    var pollTimer = 0;
    var reconnectTimer = 0;
    var replayTimers = [];

    var conn = {
      transport: "polling", // "sse" | "ws" | "polling" | "offline"
      connected: false,
      offline: false,
      consecutiveFailures: 0,
      reconnectAttempts: 0,
      nextRetryMs: 0,
      lastMessageAt: 0,
      lastError: "",
      sseSupported: (typeof EventSource === "function"),
      wsSupported: (typeof WebSocket === "function")
    };

    var eventLog = [];

    function recordLog(kind, raw, result) {
      eventLog.unshift({
        at: Date.now(),
        kind: kind,
        type: raw ? (raw.type || raw.phase || "unknown") : "",
        source: raw ? (raw.source || "") : "",
        accepted: result ? !!result.accepted : false,
        reason: result ? String(result.reason || "") : ""
      });
      if (eventLog.length > 40) eventLog.pop();
    }

    function notifyConn() {
      try { onConnectionChange(getConnectionState()); } catch (e) {}
    }

    function ingest(payload, transportLabel) {
      if (pausedForDemo && transportLabel !== "dev") return null;
      var parsed = parseIncomingPayload(payload, Date.now());
      if (!parsed) return null;
      conn.lastMessageAt = Date.now();
      conn.connected = true;
      conn.offline = false;
      conn.consecutiveFailures = 0;
      conn.lastError = "";
      var res = engine.pushEvent(parsed);
      recordLog(transportLabel || conn.transport, parsed, res);
      return res;
    }

    function markFailure(err) {
      conn.consecutiveFailures += 1;
      conn.lastError = (err && err.message) ? err.message : "连接异常";
      if (conn.consecutiveFailures >= 2) {
        conn.connected = false;
        conn.offline = true;
        conn.transport = "offline";
        notifyConn();
      }
    }

    function pollOnce() {
      if (!running || (typeof document !== "undefined" && document.hidden)) return;
      xhrJson(stateUrl, 4500, function (err, data) {
        if (err) {
          markFailure(err);
          return;
        }
        var wasOffline = conn.offline || !conn.connected;
        conn.connected = true;
        conn.offline = false;
        conn.consecutiveFailures = 0;
        conn.lastError = "";
        if (conn.transport === "offline") conn.transport = "polling";
        if (conn.transport !== "sse" && conn.transport !== "ws") ingest(data, "polling");
        if (wasOffline) notifyConn();
      });

      xhrJson(statusUrl, 4500, function (err2, sData) {
        if (!err2 && sData) {
          try { onStatusData(sData); } catch (e) {}
        }
      });
    }

    function scheduleReconnect() {
      if (!running || reconnectTimer) return;
      conn.reconnectAttempts += 1;
      var backoff = Math.min(20000, Math.round(1500 * Math.pow(1.8, Math.min(conn.reconnectAttempts - 1, 4))));
      conn.nextRetryMs = backoff;
      reconnectTimer = setTimeout(function () {
        reconnectTimer = 0;
        if (!running) return;
        tryStartRealtime();
      }, backoff);
    }

    function closeRealtime() {
      if (sse) { try { sse.close(); } catch (e) {} sse = null; }
      if (ws) { try { ws.close(); } catch (e) {} ws = null; }
    }

    function tryStartRealtime() {
      closeRealtime();
      // Prefer WebSocket if explicitly configured
      if (wsUrl && typeof WebSocket === "function") {
        try {
          ws = new WebSocket(wsUrl);
          ws.onopen = function () {
            conn.transport = "ws";
            conn.connected = true;
            conn.offline = false;
            conn.reconnectAttempts = 0;
            conn.lastError = "";
            notifyConn();
          };
          ws.onmessage = function (ev) {
            try { ingest(JSON.parse(ev.data), "ws"); } catch (e) {}
          };
          ws.onerror = function () {};
          ws.onclose = function () {
            ws = null;
            if (conn.transport === "ws") conn.transport = "polling";
            notifyConn();
            scheduleReconnect();
          };
          return;
        } catch (e) { ws = null; }
      }

      // Otherwise probe SSE (/api/house/events); if endpoint returns 404/error, cleanly stay on HTTP polling
      if (sseUrl && typeof EventSource === "function") {
        try {
          sse = new EventSource(sseUrl);
          sse.onopen = function () {
            conn.transport = "sse";
            conn.connected = true;
            conn.offline = false;
            conn.reconnectAttempts = 0;
            conn.lastError = "";
            notifyConn();
          };
          sse.onmessage = function (ev) {
            try { ingest(JSON.parse(ev.data), "sse"); } catch (e) {}
          };
          sse.onerror = function () {
            if (sse) { try { sse.close(); } catch (e) {} sse = null; }
            if (conn.transport === "sse") conn.transport = "polling";
            notifyConn();
            // Do not aggressively spam SSE if server only supports polling; retry slowly after 15s
            if (!reconnectTimer && conn.reconnectAttempts < 3) scheduleReconnect();
          };
        } catch (e) { sse = null; }
      }
    }

    function start() {
      if (running) return;
      running = true;
      pollOnce();
      if (!pollTimer) {
        pollTimer = setInterval(function () {
          // Keep polling alive as fallback & for /api/status dots (lighter frequency when SSE/WS active)
          if (conn.transport === "sse" || conn.transport === "ws") {
            if (!pausedForDemo && !(typeof document !== "undefined" && document.hidden)) {
              xhrJson(statusUrl, 4500, function (err, sData) {
                if (!err && sData) { try { onStatusData(sData); } catch (e) {} }
              });
            }
          } else {
            pollOnce();
          }
        }, pollMs);
      }
      tryStartRealtime();
    }

    function stop() {
      running = false;
      closeRealtime();
      if (pollTimer) { clearInterval(pollTimer); pollTimer = 0; }
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = 0; }
    }

    function retryNow() {
      conn.consecutiveFailures = 0;
      conn.reconnectAttempts = 0;
      conn.lastError = "";
      pollOnce();
      tryStartRealtime();
    }

    function setDemoHold(hold) {
      pausedForDemo = !!hold;
    }

    // ---- Dev Replay Scenarios (for continuous animation debugging without touching live bots) ----
    function clearReplay() {
      for (var i = 0; i < replayTimers.length; i++) clearTimeout(replayTimers[i]);
      replayTimers = [];
    }

    function runReplayScenario(scenarioId, onDone) {
      clearReplay();
      pausedForDemo = true;
      var nowSec = Math.floor(Date.now() / 1000);
      var steps = [];

      if (scenarioId === "codex_bridge") {
        // Replays exact Codex backend/status_adapter.py bridge-health sequence!
        steps = [
          { delay: 0,    raw: { last_event: { time: nowSec, phase: "received", reason: "group", trace: "tr-demo-1" }, source: "demo" }, note: "bridge:received" },
          { delay: 1600, raw: { last_event: { time: nowSec + 2, phase: "generation", reason: "started", trace: "tr-demo-1" }, source: "demo" }, note: "bridge:thinking" },
          { delay: 3400, raw: { last_event: { time: nowSec + 4, phase: "local_handoff", reason: "accepted", local_handoff: true, trace: "tr-demo-1" }, source: "demo" }, note: "bridge:replying" },
          { delay: 5400, raw: { last_event: { time: nowSec + 6, phase: "confirmation", reason: "visible_outgoing", trace: "tr-demo-1" }, last_success: { time: nowSec + 6, phase: "confirmation", reason: "visible_outgoing", trace: "tr-demo-1" }, source: "demo" }, note: "bridge:sent" },
          { delay: 7800, raw: { type: "idle", source: "demo", force: true }, note: "bridge:idle" }
        ];
      } else if (scenarioId === "error_recovery") {
        steps = [
          { delay: 0,    raw: { type: "thinking", source: "demo", text: "正在调用模型推理…", force: true } },
          { delay: 1800, raw: { type: "error", source: "demo", text: "模型上游超时，正在自动切备用线路…", force: true } },
          { delay: 4000, raw: { type: "confused", source: "demo", text: "重新校验线路状态中…", force: true } },
          { delay: 6000, raw: { type: "happy", source: "demo", text: "备用线路恢复正常啦！", force: true } },
          { delay: 8000, raw: { type: "idle", source: "demo", force: true } }
        ];
      } else {
        // Default complete chat lifecycle
        steps = [
          { delay: 0,    raw: { type: "received", source: "demo", text: "叮咚！群里有人 @大肥鱼 啦～", force: true } },
          { delay: 1600, raw: { type: "thinking", source: "demo", text: "翻翻小蓝本，认真组织回复中…", force: true } },
          { delay: 3400, raw: { type: "replying", source: "demo", text: "哒哒哒，飞快敲键盘回复！", force: true } },
          { delay: 5600, raw: { type: "sent",     source: "demo", text: "好耶！回复已经顺利发出啦～", force: true } },
          { delay: 7600, raw: { type: "happy",    source: "demo", text: "开心比耶，随时等你再找我！", force: true } },
          { delay: 9400, raw: { type: "idle",     source: "demo", force: true } }
        ];
      }

      for (var i = 0; i < steps.length; i++) {
        (function (step, isLast) {
          replayTimers.push(setTimeout(function () {
            var ev = parseIncomingPayload(step.raw, Date.now());
            if (ev) {
              // Replay is ALWAYS marked demo (never fakes verified live production!)
              ev.source = "demo";
              ev.verified = false;
              ev.force = true;
              var res = engine.pushEvent(ev);
              recordLog("replay:" + scenarioId, ev, res);
            }
            if (isLast) {
              pausedForDemo = false;
              if (typeof onDone === "function") onDone();
            }
          }, step.delay));
        })(steps[i], i === steps.length - 1);
      }
    }

    function getConnectionState() {
      return {
        transport: conn.transport,
        connected: conn.connected,
        offline: conn.offline,
        consecutiveFailures: conn.consecutiveFailures,
        reconnectAttempts: conn.reconnectAttempts,
        nextRetryMs: conn.nextRetryMs,
        lastMessageAt: conn.lastMessageAt,
        lastError: conn.lastError,
        sseSupported: conn.sseSupported,
        wsSupported: conn.wsSupported
      };
    }

    return {
      start: start,
      stop: stop,
      pollOnce: pollOnce,
      retryNow: retryNow,
      ingest: ingest,
      setDemoHold: setDemoHold,
      runReplayScenario: runReplayScenario,
      clearReplay: clearReplay,
      getConnectionState: getConnectionState,
      getEventLog: function () { return eventLog.slice(); }
    };
  }

  return {
    createEventAdapter: createEventAdapter,
    normalizeCodexActivity: normalizeCodexActivity,
    normalizeCodexBridgeHealth: normalizeCodexBridgeHealth,
    parseIncomingPayload: parseIncomingPayload
  };
});
