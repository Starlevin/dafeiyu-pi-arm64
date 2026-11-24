/* DaFeiYu Home R5.2 — Priority Event Animation State Machine (Chrome 66 ES5)
 *
 * Strictly isolated from chat core: consumes read-only event envelopes,
 * never blocks or mutates QQ/WeChat messaging pipelines.
 *
 * Supported States (9 total, covering all required R5.2 states):
 *   idle | received | thinking | replying | sent | happy | confused | rest | error
 *
 * Features:
 *   - Explicit event priority matrix & preemption rules
 *   - Interrupt & recovery stack (restores interrupted real state if still fresh)
 *   - Event deduplication (by id / seq / type+source+at signature)
 *   - Out-of-order & stale event rejection (fail-closed)
 *   - Per-state TTL expiry & deterministic recovery chains
 *   - Real (verified) vs Demo vs Local-Touch source stratification
 *   - Zero DOM dependencies (runs in Browser Chrome 66+, Firefox, and Node.js tests)
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.DaFeiYuEventEngine = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var VALID_STATES = {
    idle: true,
    received: true,
    thinking: true,
    replying: true,
    sent: true,
    happy: true,
    confused: true,
    rest: true,
    error: true
  };

  var STATE_PRIORITY = {
    error: 100,
    replying: 80,
    thinking: 70,
    sent: 68,
    happy: 64,
    received: 60,
    confused: 40,
    idle: 10,
    rest: 5
  };

  var EVENT_ALIASES = {
    idle: "idle",
    received: "received",
    receive: "received",
    notice: "received",
    thinking: "thinking",
    work: "thinking",
    working: "thinking",
    replying: "replying",
    reply: "replying",
    sent: "sent",
    success: "sent",
    happy: "happy",
    confused: "confused",
    rest: "rest",
    sleep: "rest",
    error: "error",
    fail: "error",
    failed: "error"
  };

  var STATE_LABELS = {
    idle: "待机中",
    received: "收到消息",
    thinking: "思考中…",
    replying: "正在回复",
    sent: "回复已送达",
    happy: "开心互动",
    confused: "有点困惑",
    rest: "小憩中",
    error: "遇到异常"
  };

  var STATE_DEFAULT_BUBBLES = {
    idle: "",
    received: "叮咚，收到新消息啦～",
    thinking: "嗯…让大肥鱼仔细想想",
    replying: "哒哒哒，正在敲字回复！",
    sent: "好耶，回复顺利发出去啦～",
    happy: "哼哼～今天心情超不错！",
    confused: "唔…这里好像有点迷糊？",
    rest: "暖暖的阳光，先打个小盹～",
    error: "呀！刚才出了点小状况，正在排查"
  };

  var REAL_SOURCES = {
    qq: true,
    wechat: true,
    task: true,
    bot: true
  };

  var DEFAULT_OPTIONS = {
    ttlMs: {
      received: 6000,
      thinking: 90000,
      replying: 35000,
      sent: 4800,
      happy: 4200,
      confused: 5000,
      error: 8500
    },
    idleToRestMs: 45000,
    maxEventAgeMs: 60000,
    dedupWindowSize: 80,
    historyLimit: 120,
    nowFn: function () { return Date.now(); }
  };

  function cloneShallow(obj) {
    var out = {};
    var k;
    if (!obj) return out;
    for (k in obj) {
      if (Object.prototype.hasOwnProperty.call(obj, k)) {
        out[k] = obj[k];
      }
    }
    return out;
  }

  function normalizeTimestampMs(rawAt, fallbackNow) {
    var n = Number(rawAt || 0);
    if (!n || isNaN(n) || n <= 0) return fallbackNow;
    return n > 1e12 ? Math.round(n) : Math.round(n * 1000);
  }

  function normalizeInputType(rawType) {
    var key = String(rawType || "idle").toLowerCase().replace(/^\s+|\s+$/g, "");
    return EVENT_ALIASES[key] || null;
  }

  function getPriority(st) {
    return STATE_PRIORITY[st] || 0;
  }

  function createEngine(customOptions) {
    var opts = cloneShallow(DEFAULT_OPTIONS);
    var k;
    opts.ttlMs = cloneShallow(DEFAULT_OPTIONS.ttlMs);
    if (customOptions) {
      for (k in customOptions) {
        if (Object.prototype.hasOwnProperty.call(customOptions, k) && k !== "ttlMs") {
          opts[k] = customOptions[k];
        }
      }
      if (customOptions.ttlMs) {
        for (k in customOptions.ttlMs) {
          if (Object.prototype.hasOwnProperty.call(customOptions.ttlMs, k)) {
            opts.ttlMs[k] = customOptions.ttlMs[k];
          }
        }
      }
    }

    var state = "idle";
    var lastInputEvent = "idle";
    var source = "demo";       // "real" | "demo" | "touch"
    var platform = "";         // "qq" | "wechat" | "task" | "demo" | "touch" | ""
    var verified = false;
    var bubble = "";
    var lastRealSeq = 0;
    var lastRealEventAt = 0;
    var enteredStateAt = opts.nowFn();
    var transitionSeq = 0;
    var dedupIds = [];
    var dedupMap = {};
    var lastSig = "";
    var interruptedState = null; // { state, event, source, platform, verified, bubble, enteredStateAt, ttlMs }
    var history = [];
    var listeners = [];
    var stats = {
      accepted: 0,
      deduped: 0,
      expired: 0,
      staleDropped: 0,
      priorityBlocked: 0,
      invalidDropped: 0,
      interruptedSaved: 0,
      interruptedRestored: 0,
      autoRecoveries: 0
    };

    function rememberDedup(key) {
      if (!key || dedupMap[key]) return;
      dedupMap[key] = true;
      dedupIds.push(key);
      while (dedupIds.length > opts.dedupWindowSize) {
        var oldest = dedupIds.shift();
        delete dedupMap[oldest];
      }
    }

    function snapshot() {
      return {
        state: state,
        label: STATE_LABELS[state] || "待机中",
        priority: getPriority(state),
        event: lastInputEvent,
        source: source,
        platform: platform,
        verified: verified,
        bubble: bubble,
        seq: transitionSeq,
        lastRealSeq: lastRealSeq,
        enteredStateAt: enteredStateAt,
        hasInterrupted: !!interruptedState,
        stats: cloneShallow(stats)
      };
    }

    function emitChange(reason, prevState) {
      var snap = snapshot();
      snap.reason = reason;
      snap.prevState = prevState;
      history.push({
        seq: transitionSeq,
        from: prevState,
        to: state,
        event: lastInputEvent,
        source: source,
        platform: platform,
        verified: verified,
        reason: reason,
        at: opts.nowFn()
      });
      while (history.length > opts.historyLimit) history.shift();
      for (var i = 0; i < listeners.length; i++) {
        try { listeners[i](snap); } catch (e) {}
      }
      return snap;
    }

    function transitionTo(nextState, meta, reason) {
      var prev = state;
      state = nextState;
      lastInputEvent = meta.event || nextState;
      source = meta.source || "demo";
      platform = meta.platform || "";
      verified = !!meta.verified;
      bubble = (typeof meta.bubble === "string")
        ? meta.bubble
        : (STATE_DEFAULT_BUBBLES[nextState] || "");
      enteredStateAt = meta.now || opts.nowFn();
      transitionSeq += 1;
      return emitChange(reason || "event", prev);
    }

    function canRestoreInterrupted(now) {
      if (!interruptedState) return false;
      var ttl = interruptedState.ttlMs || opts.ttlMs[interruptedState.state] || 0;
      if (!ttl) return false;
      return (now - interruptedState.enteredStateAt) < ttl;
    }

    /**
     * Push a unified event envelope into the animation state machine.
     * @param {Object} ev { type|event|phase, id?, seq?, source?, platform?, verified?, at?, text?|bubble?, force?, interrupt? }
     */
    function pushEvent(ev) {
      var now = opts.nowFn();
      if (!ev || typeof ev !== "object") {
        stats.invalidDropped += 1;
        return { accepted: false, reason: "invalid_payload", snapshot: snapshot() };
      }

      var rawType = ev.type || ev.event || ev.phase;
      var normEvent = normalizeInputType(rawType);
      if (!normEvent || !VALID_STATES[normEvent]) {
        stats.invalidDropped += 1;
        return { accepted: false, reason: "unknown_event", snapshot: snapshot() };
      }

      var rawSource = String(ev.source || "demo").toLowerCase();
      var isTouch = (rawSource === "touch");
      var isReal = !!(REAL_SOURCES[rawSource] && ev.verified === true);
      var normSource = isReal ? "real" : (isTouch ? "touch" : "demo");
      var normPlatform = ev.platform ? String(ev.platform) : (REAL_SOURCES[rawSource] ? rawSource : normSource);
      var eventAtMs = normalizeTimestampMs(ev.at, now);
      var hasSeq = (typeof ev.seq === "number" && !isNaN(ev.seq) && ev.seq > 0);
      var evId = ev.id ? String(ev.id) : "";

      // 1. Explicit ID deduplication
      if (evId && dedupMap["id:" + evId]) {
        stats.deduped += 1;
        return { accepted: false, reason: "duplicate_id", snapshot: snapshot() };
      }

      // 2. Local touch interactions NEVER preempt an active real bot state
      if (isTouch && source === "real" && state !== "idle" && state !== "rest") {
        stats.priorityBlocked += 1;
        return { accepted: false, reason: "blocked_by_real_state", snapshot: snapshot() };
      }

      // 3. Real event monotonic sequence, future-timestamp, & TTL staleness guards
      if (isReal) {
        if (hasSeq && ev.seq < lastRealSeq) {
          stats.staleDropped += 1;
          return { accepted: false, reason: "out_of_order_seq", snapshot: snapshot() };
        }
        if (ev.at && normEvent !== "idle") {
          var ageMs = now - eventAtMs;
          if (ageMs < -5000) {
            stats.staleDropped += 1;
            return { accepted: false, reason: "future_timestamp", snapshot: snapshot() };
          }
          var maxAllowed = opts.ttlMs[normEvent] || opts.maxEventAgeMs;
          if (ageMs > maxAllowed) {
            stats.expired += 1;
            if (state !== "idle" && state !== "rest" && source === "real") {
              stats.autoRecoveries += 1;
              interruptedState = null;
              transitionTo("idle", {
                event: "idle",
                source: "demo",
                platform: "",
                verified: false,
                bubble: "",
                now: now
              }, "expired_event_fallback");
            }
            return { accepted: false, reason: "expired_ttl", snapshot: snapshot() };
          }
        }
        if (ev.at && lastRealEventAt && eventAtMs < lastRealEventAt - 1500 && !hasSeq) {
          stats.staleDropped += 1;
          return { accepted: false, reason: "stale_timestamp", snapshot: snapshot() };
        }
      }

      // 4. Priority preemption rule when seq does NOT strictly advance
      var seqAdvanced = isReal && hasSeq && ev.seq > lastRealSeq;
      if (!ev.force && !seqAdvanced && state !== "idle" && state !== "rest") {
        var incomingPrio = getPriority(normEvent);
        var currentPrio = getPriority(state);
        // Demo cannot override active real unless force=true
        if (!isReal && source === "real") {
          stats.priorityBlocked += 1;
          return { accepted: false, reason: "demo_blocked_by_real", snapshot: snapshot() };
        }
        // Same-source concurrent event with lower priority (unless explicit idle/sent/error reset)
        if (normSource === source && normEvent !== "idle" && normEvent !== "sent" &&
            incomingPrio < currentPrio) {
          stats.priorityBlocked += 1;
          return { accepted: false, reason: "lower_priority_blocked", snapshot: snapshot() };
        }
      }

      // 5. Signature deduplication for polling snapshots
      var sig = [
        normEvent,
        normSource,
        normPlatform,
        hasSeq ? String(ev.seq) : "",
        ev.at ? String(eventAtMs) : ""
      ].join("|");

      if (!ev.force) {
        if (sig && sig === lastSig && state === normEvent && source === normSource) {
          stats.deduped += 1;
          return { accepted: false, reason: "duplicate_signature", snapshot: snapshot() };
        }
        if (normEvent === "idle" && (state === "idle" || state === "rest") &&
            normSource === source && !ev.text) {
          stats.deduped += 1;
          return { accepted: false, reason: "already_idle", snapshot: snapshot() };
        }
      }

      // 6. Save interrupted active state if high-priority transient interrupt (e.g. error or explicit interrupt)
      if ((ev.interrupt === true || (normEvent === "error" && ev.terminal !== true)) &&
          (state === "thinking" || state === "replying") &&
          source === "real" && normEvent !== state) {
        interruptedState = {
          state: state,
          event: lastInputEvent,
          source: source,
          platform: platform,
          verified: verified,
          bubble: bubble,
          enteredStateAt: enteredStateAt,
          ttlMs: opts.ttlMs[state] || opts.maxEventAgeMs
        };
        stats.interruptedSaved += 1;
      } else if (normEvent === "idle" || normEvent === "sent" || ev.terminal === true) {
        interruptedState = null;
      }

      if (evId) rememberDedup("id:" + evId);
      lastSig = sig;
      if (isReal) {
        if (hasSeq && ev.seq > lastRealSeq) lastRealSeq = ev.seq;
        if (eventAtMs > lastRealEventAt) lastRealEventAt = eventAtMs;
      }

      stats.accepted += 1;
      var customBubble = (typeof ev.text === "string" && ev.text.length > 0)
        ? ev.text
        : (typeof ev.bubble === "string" && ev.bubble.length > 0 ? ev.bubble : undefined);

      var snap = transitionTo(normEvent, {
        event: normEvent,
        source: normSource,
        platform: normPlatform,
        verified: isReal,
        bubble: customBubble,
        now: now
      }, "event:" + normEvent);

      return { accepted: true, reason: "transitioned", snapshot: snap };
    }

    /**
     * Advance time-based TTL expiration, interrupt recovery, & idle->rest transition.
     */
    function tick(nowOverride) {
      var now = (typeof nowOverride === "number") ? nowOverride : opts.nowFn();
      var elapsed = now - enteredStateAt;

      if (state === "idle") {
        if (opts.idleToRestMs > 0 && elapsed >= opts.idleToRestMs) {
          stats.autoRecoveries += 1;
          return transitionTo("rest", {
            event: "rest",
            source: "demo",
            platform: "",
            verified: false,
            bubble: "",
            now: now
          }, "idle_to_rest");
        }
        return null;
      }

      if (state === "rest") return null;

      var ttl = opts.ttlMs[state];
      if (!ttl || elapsed < ttl) return null;

      // If an interrupted active real state is still fresh, restore it cleanly!
      if (canRestoreInterrupted(now)) {
        var restore = interruptedState;
        interruptedState = null;
        stats.interruptedRestored += 1;
        return transitionTo(restore.state, {
          event: restore.event,
          source: restore.source,
          platform: restore.platform,
          verified: restore.verified,
          bubble: restore.bubble,
          now: restore.enteredStateAt
        }, "interrupt_restored");
      }
      interruptedState = null;

      stats.autoRecoveries += 1;
      if (state === "thinking" || state === "error") {
        var reason = (state === "thinking") ? "thinking_timeout_confused" : "error_recovery_confused";
        return transitionTo("confused", {
          event: "confused",
          source: "demo",
          platform: platform,
          verified: false,
          bubble: state === "thinking" ? "想了好久，先缓一口气～" : "已经稳住啦，继续待命",
          now: now
        }, reason);
      }

      return transitionTo("idle", {
        event: "idle",
        source: "demo",
        platform: "",
        verified: false,
        bubble: "",
        now: now
      }, "ttl_recover_idle");
    }

    function reset() {
      state = "idle";
      lastInputEvent = "idle";
      source = "demo";
      platform = "";
      verified = false;
      bubble = "";
      lastRealSeq = 0;
      lastRealEventAt = 0;
      enteredStateAt = opts.nowFn();
      lastSig = "";
      dedupIds = [];
      dedupMap = {};
      interruptedState = null;
      return emitChange("reset", "idle");
    }

    function subscribe(fn) {
      if (typeof fn !== "function") return function () {};
      listeners.push(fn);
      return function () {
        for (var i = 0; i < listeners.length; i++) {
          if (listeners[i] === fn) {
            listeners.splice(i, 1);
            break;
          }
        }
      };
    }

    return {
      pushEvent: pushEvent,
      tick: tick,
      reset: reset,
      getState: snapshot,
      getHistory: function () { return history.slice(); },
      subscribe: subscribe,
      STATES: VALID_STATES,
      PRIORITIES: STATE_PRIORITY,
      LABELS: STATE_LABELS
    };
  }

  return {
    createEngine: createEngine,
    STATES: VALID_STATES,
    PRIORITIES: STATE_PRIORITY,
    LABELS: STATE_LABELS,
    normalizeInputType: normalizeInputType
  };
});
