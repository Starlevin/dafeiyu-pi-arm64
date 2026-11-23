/* DaFeiYu Home R5.2 — Unified Console & Extended Capabilities API Adapter (Chrome 66 ES5)
 *
 * Strictly adheres to Codex's backend APIs without modifying backend code:
 *   LIVE / REAL ENDPOINTS (Codex unified_console.py + tool_routes.py + fish-house-lan):
 *     GET  /api/session           -> { authenticated, setupAvailable, secure, lanUrl, fixtureDemo? }
 *     POST /api/login             -> { password } => { ok: true }
 *     POST /api/logout            -> {} => { ok: true }
 *     GET  /api/overview          -> { qq, wechat, models, conversations, styleOptions, frequencyOptions, limitOptions }
 *     POST /api/model             -> { platform, id } => { ok: true, verification: { state, observed, lastRead } }
 *     POST /api/conversation      -> { platform, id, field, value } => { ok: true }
 *     GET  /api/usage             -> token & model usage ledger
 *     GET  /api/tools/status      -> NapCat & observed systemd boxes
 *     GET  /api/tools/plugins     -> installed plugins & extension modules
 *     GET  /api/tools/logs        -> desensitized runtime logs
 *     GET  /api/persona           -> active persona summary
 *     GET  /api/status            -> read-only QQ/WeChat status
 *     POST /api/diag              -> non-sensitive rendering telemetry
 *
 *   CONTRACTED EXTENSION ENDPOINTS (auto-bind when Codex wires them; honest placeholder when 404):
 *     GET  /api/memory/status     -> per-group memory isolation, DB size, journal watermark
 *     GET  /api/stickers          -> sticker packs (e.g. whalegirl_batch1), rotation & cooldown policy
 *     GET  /api/proactive/config  -> proactive reply & Swift judge thresholds
 *     GET  /api/trace/summary     -> R5 shared model-hub trace & WeChat/QQ p50/p95 stage breakdown
 *
 * Honesty Rule:
 *   - NEVER masquerade mock/contract data as live production state.
 *   - Every save executes explicit lifecycle: saving -> POST -> re-GET /api/overview -> confirmed|mismatch|error.
 */
(function (root) {
  "use strict";

  function requestJson(method, url, body, timeoutMs, cb) {
    var xhr = new XMLHttpRequest();
    var done = false;
    var timer = setTimeout(function () {
      if (done) return;
      done = true;
      try { xhr.abort(); } catch (e) {}
      cb({ status: 0, code: "timeout", message: "请求超时，请检查网络连接" }, null);
    }, timeoutMs || 8000);

    xhr.open(method, url, true);
    xhr.withCredentials = true;
    if (body !== undefined && body !== null) {
      xhr.setRequestHeader("Content-Type", "application/json");
    }
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4 || done) return;
      done = true;
      clearTimeout(timer);
      var parsed = null;
      if (xhr.responseText) {
        try { parsed = JSON.parse(xhr.responseText); } catch (e) { parsed = null; }
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        var msg = (parsed && typeof parsed.error === "string" && parsed.error)
          ? parsed.error
          : (xhr.status === 401 ? "登录状态已失效，请重新登录"
            : (xhr.status === 404 || xhr.status === 405) ? "当前后端尚未挂载该接口"
            : "服务请求失败（HTTP " + xhr.status + "）");
        cb({ status: xhr.status, code: "http_" + xhr.status, message: msg }, parsed);
        return;
      }
      if (method === "POST" && parsed && parsed.ok === false) {
        cb({ status: xhr.status, code: "not_ok", message: parsed.error || "服务未确认本次操作" }, parsed);
        return;
      }
      cb(null, parsed);
    };
    xhr.onerror = function () {
      if (done) return;
      done = true;
      clearTimeout(timer);
      cb({ status: 0, code: "network", message: "无法连接控制台服务，当前可能已断网" }, null);
    };
    try {
      xhr.send(body !== undefined && body !== null ? JSON.stringify(body) : null);
    } catch (e) {
      if (!done) {
        done = true;
        clearTimeout(timer);
        cb({ status: 0, code: "send_fail", message: "请求发送异常" }, null);
      }
    }
  }

  function createApiAdapter(options) {
    var opts = options || {};
    var consoleBase = opts.consoleBase || "";
    var homeBase = opts.homeBase || "";

    var state = {
      consoleMode: "probing", // "connected" | "auth_required" | "readonly_only" | "offline"
      fixtureDemo: false,
      session: null,
      overview: null,
      liveStatus: null,
      extensions: {
        toolsStatus: { status: "unprobed", data: null, endpoint: "/api/tools/status" },
        plugins:     { status: "unprobed", data: null, endpoint: "/api/tools/plugins" },
        logs:        { status: "unprobed", data: null, endpoint: "/api/tools/logs" },
        usage:       { status: "unprobed", data: null, endpoint: "/api/usage" },
        memory:      { status: "unprobed", data: null, endpoint: "/api/memory/status" },
        stickers:    { status: "unprobed", data: null, endpoint: "/api/stickers" },
        proactive:   { status: "unprobed", data: null, endpoint: "/api/proactive/config" },
        trace:       { status: "unprobed", data: null, endpoint: "/api/trace/summary" },
        gemini:      { status: "unprobed", data: null, endpoint: "/api/gemini/health" }
      },
      lastOverviewAt: 0,
      lastStatusAt: 0,
      lastError: "",
      saveState: "idle", // "idle" | "loading" | "saving" | "confirmed" | "mismatch" | "error"
      saveMessage: "",
      busy: false,
      lastVerify: {}
    };

    function cUrl(p) { return consoleBase + p; }
    function hUrl(p) { return homeBase + p; }

    function postDiag(payload) {
      try {
        var xhr = new XMLHttpRequest();
        xhr.open("POST", hUrl("/api/diag"), true);
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.send(JSON.stringify(payload));
      } catch (e) {}
    }

    function probeSession(cb) {
      state.saveState = "loading";
      requestJson("GET", cUrl("/api/session"), null, 5000, function (err, data) {
        state.saveState = "idle";
        if (err) {
          if (err.status === 404 || err.status === 405) {
            state.consoleMode = "readonly_only";
            state.lastError = "当前为只读家园入口，统一控制台写入接口未挂载";
          } else {
            state.consoleMode = "offline";
            state.lastError = err.message;
          }
          state.session = null;
          cb(err, state);
          return;
        }
        state.session = data || {};
        state.fixtureDemo = !!(data && data.fixtureDemo === true);
        state.consoleMode = (data && data.authenticated === true) ? "connected" : "auth_required";
        state.lastError = "";
        cb(null, state);
      });
    }

    function login(password, cb) {
      if (state.busy) { cb({ status: 0, message: "操作进行中，请稍候" }, null); return; }
      state.busy = true;
      state.saveState = "saving";
      state.saveMessage = "正在校验登录凭据…";
      requestJson("POST", cUrl("/api/login"), { password: String(password || "") }, 8000, function (err) {
        state.busy = false;
        if (err) {
          state.saveState = "error";
          state.saveMessage = err.message;
          state.lastError = err.message;
          cb(err, null);
          return;
        }
        state.consoleMode = "connected";
        state.lastError = "";
        fetchOverview(function (oErr, ov) {
          cb(oErr, ov);
        });
      });
    }

    function logout(cb) {
      if (state.busy) { cb({ status: 0, message: "操作进行中" }, null); return; }
      state.busy = true;
      requestJson("POST", cUrl("/api/logout"), {}, 6000, function (err) {
        state.busy = false;
        state.overview = null;
        state.consoleMode = "auth_required";
        state.saveState = "idle";
        state.saveMessage = "";
        var keys = ["toolsStatus", "plugins", "logs", "usage", "memory", "stickers", "proactive", "trace", "gemini"];
        for (var i = 0; i < keys.length; i++) {
          state.extensions[keys[i]].status = "auth_required";
          state.extensions[keys[i]].data = null;
        }
        cb(err, state);
      });
    }

    function fetchOverview(cb) {
      state.saveState = "loading";
      requestJson("GET", cUrl("/api/overview"), null, 7000, function (err, data) {
        if (err) {
          state.saveState = "error";
          state.saveMessage = err.message;
          if (err.status === 401) {
            state.consoleMode = "auth_required";
            state.overview = null;
          } else if (err.status === 404 || err.status === 405) {
            state.consoleMode = "readonly_only";
          }
          state.lastError = err.message;
          cb(err, null);
          return;
        }
        state.overview = data;
        state.consoleMode = "connected";
        state.lastOverviewAt = Date.now();
        state.lastError = "";
        state.saveState = "idle";
        cb(null, data);
      });
    }

    function probeExtension(key, cb) {
      var slot = state.extensions[key];
      if (!slot) { if (cb) cb(null); return; }
      requestJson("GET", cUrl(slot.endpoint), null, 5000, function (err, data) {
        if (err) {
          slot.status = (err.status === 404 || err.status === 405) ? "not_wired"
            : (err.status === 401 ? "auth_required" : "error");
          slot.error = err.message;
          slot.data = null;
        } else {
          slot.status = (data && data.mock === true) ? "fixture" : "live";
          slot.data = data;
          slot.error = "";
          slot.loadedAt = Date.now();
        }
        if (cb) cb(slot);
      });
    }

    function fetchAllExtensions(cb) {
      var keys = ["toolsStatus", "plugins", "logs", "usage", "memory", "stickers", "proactive", "trace", "gemini"];
      var pending = keys.length;
      for (var i = 0; i < keys.length; i++) {
        probeExtension(keys[i], function () {
          pending -= 1;
          if (pending === 0 && typeof cb === "function") cb(state.extensions);
        });
      }
    }

    function setGeminiModel(modelId, cb) {
      if (state.consoleMode !== "connected") {
        cb({ status: 401, message: "未登录统一控制台，无法切换 Gemini 模型" }, null);
        return;
      }
      if (state.busy) { cb({ status: 0, message: "上一个保存请求尚在进行中" }, null); return; }
      state.busy = true;
      state.saveState = "saving";
      state.saveMessage = "正在切换 Gemini 网关目标模型并回读验证…";
      requestJson("POST", cUrl("/api/gemini/model"), { model: modelId }, 8000, function (err, res) {
        if (err) {
          state.busy = false;
          state.saveState = "error";
          state.saveMessage = err.message;
          cb(err, null);
          return;
        }
        probeExtension("gemini", function () {
          state.busy = false;
          var ok = res && res.verification && res.verification.state === "confirmed";
          state.saveState = ok ? "confirmed" : "mismatch";
          state.saveMessage = ok ? ("Gemini 目标模型已回读确认：" + modelId) : "切换已提交但回读不一致";
          cb(null, { result: res, saveState: state.saveState, message: state.saveMessage });
        });
      });
    }

    function saveProactive(group, threshold, freshnessSeconds, cb) {
      if (state.consoleMode !== "connected" || state.busy || state.fixtureDemo) {
        cb("需要已登录的控制台，且当前无其他保存操作。");
        return;
      }
      state.busy = true;
      requestJson("POST", cUrl("/api/proactive/config"), {
        group: group, threshold: threshold, freshnessSeconds: freshnessSeconds
      }, 8000, function (err) {
        if (err) {
          state.busy = false;
          cb(err.message);
          return;
        }
        probeExtension("proactive", function () {
          state.busy = false;
          var data = state.extensions.proactive.data;
          var row = data && data.qqGroups && data.qqGroups[group];
          var matched = row && row.threshold === threshold && row.freshnessSeconds === freshnessSeconds;
          cb(matched ? "" : "设置已提交，回读值尚未确认。");
        });
      });
    }

    // Public-question auto-chat interval (30..1200 integer seconds, default 60).
    // Incremental POST reuses /api/proactive/config; success is NEVER reported on POST
    // alone — probeExtension("proactive") re-GETs the config and must observe the value.
    // Callback: cb(null, result) after read-back (result.verified true|false),
    //           cb(errorMessageString) on rejection / transport failure.
    function savePublicQuestionCooldown(platform, group, seconds, cb) {
      if (typeof cb !== "function") cb = function () {};
      if (state.consoleMode !== "connected" || state.busy || state.fixtureDemo) {
        cb("需要已登录的控制台，且当前无其他保存操作。");
        return;
      }
      if (platform !== "qq" && platform !== "wechat") {
        cb("无效的平台类型，仅支持 qq 或 wechat。");
        return;
      }
      if (typeof group !== "string" || group.replace(/^\s+|\s+$/g, "") === "") {
        cb("无效的群组标识。");
        return;
      }
      if (typeof seconds !== "number" || !isFinite(seconds) ||
          Math.floor(seconds) !== seconds || seconds < 30 || seconds > 1200) {
        cb("公开提问自动接话间隔必须是 30 到 1200 之间的整数秒。");
        return;
      }

      state.busy = true;
      state.saveState = "saving";
      state.saveMessage = "正在保存并回读…";
      requestJson("POST", cUrl("/api/proactive/config"), {
        platform: platform,
        group: group,
        publicQuestionCooldownSeconds: seconds
      }, 8000, function (err, res) {
        if (err) {
          state.busy = false;
          state.saveState = "error";
          state.saveMessage = err.message;
          cb(err.message);
          return;
        }
        probeExtension("proactive", function (slot) {
          state.busy = false;
          var data = slot && slot.data;
          var bucket = data && data.publicQuestionCooldowns && data.publicQuestionCooldowns[platform];
          var observed = (bucket && Object.prototype.hasOwnProperty.call(bucket, group))
            ? bucket[group] : undefined;
          var verified = observed === seconds;
          var result = {
            platform: platform,
            group: group,
            seconds: seconds,
            observed: observed,
            verified: verified,
            result: res,
            data: data
          };
          if (verified) {
            state.saveState = "confirmed";
            state.saveMessage = "已保存并回读确认：" + seconds + " 秒";
          } else {
            state.saveState = "mismatch";
            state.saveMessage = "设置已提交，但回读值不一致（目标 " + seconds +
              " 秒，当前 " + (typeof observed === "number" ? observed : "未知") + " 秒）";
            result.error = state.saveMessage;
          }
          result.saveState = state.saveState;
          result.message = state.saveMessage;
          cb(null, result);
        });
      });
    }

    // Strict POST-then-re-GET contract for model switching
    function setModel(platform, modelId, cb) {
      if (state.consoleMode !== "connected") {
        cb({ status: 401, message: "未登录统一控制台，无法修改模型" }, null);
        return;
      }
      if (state.busy) { cb({ status: 0, message: "上一个保存请求尚在进行中" }, null); return; }
      state.busy = true;
      state.saveState = "saving";
      state.saveMessage = "正在保存 " + (platform === "wechat" ? "微信" : "QQ") + " 模型并等待回读校验…";

      requestJson("POST", cUrl("/api/model"), { platform: platform, id: modelId }, 10000, function (err, res) {
        if (err) {
          state.busy = false;
          state.saveState = "error";
          state.saveMessage = err.message;
          if (err.status === 401) state.consoleMode = "auth_required";
          state.lastError = err.message;
          cb(err, null);
          return;
        }
        var ver = (res && res.verification) ? res.verification : { state: "unconfirmed", observed: "" };
        state.lastVerify[platform] = ver;

        fetchOverview(function (oErr, ov) {
          state.busy = false;
          if (oErr) {
            state.saveState = "error";
            state.saveMessage = "已提交修改，但回读状态失败：" + oErr.message;
            cb(oErr, null);
            return;
          }
          var observedNow = (ov && ov[platform] && ov[platform].model) ? ov[platform].model : "";
          if (ver.state === "confirmed" && (!observedNow || observedNow === modelId)) {
            state.saveState = "confirmed";
            state.saveMessage = "模型已保存并回读确认：" + modelId;
          } else if (ver.state === "mismatch" || (observedNow && observedNow !== modelId)) {
            state.saveState = "mismatch";
            state.saveMessage = "保存后回读值不一致（当前为 " + (observedNow || ver.observed || "未知") + "）";
          } else {
            state.saveState = "confirmed";
            state.saveMessage = "已提交修改，回读状态：待确认";
          }
          cb(null, { result: res, overview: ov, saveState: state.saveState, message: state.saveMessage });
        });
      });
    }

    // Strict POST-then-re-GET contract for conversation setting updates
    function updateConversation(platform, convId, field, value, cb) {
      if (state.consoleMode !== "connected") {
        cb({ status: 401, message: "未登录统一控制台，无法修改会话设置" }, null);
        return;
      }
      if (state.busy) { cb({ status: 0, message: "上一个保存请求尚在进行中" }, null); return; }
      state.busy = true;
      state.saveState = "saving";
      state.saveMessage = "正在保存会话策略并回读确认…";

      requestJson("POST", cUrl("/api/conversation"), {
        platform: platform,
        id: convId,
        field: field,
        value: value
      }, 10000, function (err, res) {
        if (err) {
          state.busy = false;
          state.saveState = "error";
          state.saveMessage = err.message;
          if (err.status === 401) state.consoleMode = "auth_required";
          state.lastError = err.message;
          cb(err, null);
          return;
        }
        fetchOverview(function (oErr, ov) {
          state.busy = false;
          if (oErr) {
            state.saveState = "error";
            state.saveMessage = "写入已提交，但回读失败：" + oErr.message;
            cb(oErr, null);
            return;
          }
          // Verify saved field matches refreshed overview
          var matched = false;
          var list = (ov && ov.conversations) ? ov.conversations : [];
          for (var i = 0; i < list.length; i++) {
            if (list[i].platform === platform && list[i].id === convId && list[i][field] === value) {
              matched = true;
              break;
            }
          }
          state.saveState = matched ? "confirmed" : "mismatch";
          state.saveMessage = matched ? "设置已持久化并回读确认" : "设置已提交，但刷新回读值未变化";
          cb(null, { result: res, overview: ov, verified: matched, message: state.saveMessage });
        });
      });
    }

    function updateLiveStatus(data) {
      if (!data) return;
      state.liveStatus = data;
      state.lastStatusAt = Date.now();
    }

    return {
      state: state,
      postDiag: postDiag,
      probeSession: probeSession,
      login: login,
      logout: logout,
      fetchOverview: fetchOverview,
      fetchAllExtensions: fetchAllExtensions,
      probeExtension: probeExtension,
      setModel: setModel,
      setGeminiModel: setGeminiModel,
      saveProactive: saveProactive,
      savePublicQuestionCooldown: savePublicQuestionCooldown,
      updateConversation: updateConversation,
      updateLiveStatus: updateLiveStatus
    };
  }

  root.DaFeiYuApiAdapter = {
    createApiAdapter: createApiAdapter
  };
  // Backward-compatible alias for R1 callers
  root.DaFeiYuConsoleClient = {
    createConsoleClient: createApiAdapter
  };
})(typeof self !== "undefined" ? self : this);
