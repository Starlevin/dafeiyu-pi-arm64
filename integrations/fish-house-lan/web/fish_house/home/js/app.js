/* DaFeiYu Home R5.2 — Main Application Controller (Chrome 66 ES5 + Firefox + Edge)
 *
 * Orchestrates:
 *   - Priority Event Animation State Machine (DaFeiYuEventEngine)
 *   - Multi-Transport Real Event Adapter (DaFeiYuEventAdapter: WS / SSE / Polling + Codex status_adapter)
 *   - Character Pack Loader, Codec Probe & Cache Manager (DaFeiYuPackLoader)
 *   - Dual-Backend Stage Renderer (DaFeiYuStageRenderer: 2D High/Eco + WebGL1 + Video Fallback)
 *   - Unified 9-Module API Adapter (DaFeiYuApiAdapter)
 *   - Persisted user preferences: form (adult/chibi), theme (auto/day/sunset/night/video),
 *     perfMode (auto/high/eco + webgl toggle), defaultHome (auto/home/console)
 *   - Mobile swipe-down drawer close & gentle stage tap interaction
 *   - Public integration contract: window.DaFeiYuHome
 */
(function (root) {
  "use strict";

  var PREF_KEYS = {
    form: "dafeiyu.home.form",
    theme: "dafeiyu.home.theme",
    perf: "dafeiyu.home.perf",
    webgl: "dafeiyu.home.webgl",
    defaultHome: "dafeiyu.home.defaultHome"
  };

  var THEME_ORDER = ["auto", "day", "sunset", "night", "video"];
  var THEME_ICONS = { auto: "◐", day: "☀", sunset: "🌇", night: "☾", video: "▶" };
  var THEME_NAMES = { auto: "随时间自动", day: "晴朗晨光", sunset: "暖阳午后", night: "星月夜色", video: "原版视频屋" };

  function $(id) { return document.getElementById(id); }

  function readPref(key, allowed, fallback) {
    try {
      var v = window.localStorage.getItem(key);
      if (v && allowed.indexOf(v) >= 0) return v;
    } catch (e) {}
    return fallback;
  }

  function savePref(key, value) {
    try { window.localStorage.setItem(key, String(value)); } catch (e) {}
  }

  function formatClock(ms) {
    if (!ms) return "--";
    var d = new Date(ms);
    function p2(n) { return n < 10 ? "0" + n : String(n); }
    return p2(d.getHours()) + ":" + p2(d.getMinutes()) + ":" + p2(d.getSeconds());
  }

  function clearEl(el) {
    while (el && el.firstChild) el.removeChild(el.firstChild);
  }

  function makeEl(tag, cls, txt) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (txt !== undefined && txt !== null) n.textContent = String(txt);
    return n;
  }

  function makeKvRow(k, v) {
    var row = makeEl("div", "fh-row");
    row.appendChild(makeEl("span", "fh-kv-k", k));
    row.appendChild(makeEl("span", "fh-kv-v", v));
    return row;
  }

  function parseQueryParam(name) {
    try {
      var q = window.location.search || "";
      if (q.charAt(0) === "?") q = q.slice(1);
      var parts = q.split("&");
      for (var i = 0; i < parts.length; i++) {
        var kv = parts[i].split("=");
        if (decodeURIComponent(kv[0] || "") === name) {
          return decodeURIComponent(kv[1] || "");
        }
      }
    } catch (e) {}
    return "";
  }

  // ---- Load & Normalize Persisted Preferences ----
  var currentForm = readPref(PREF_KEYS.form, ["adult", "chibi"], "adult");
  var currentTheme = readPref(PREF_KEYS.theme, THEME_ORDER, "video");
  var rawPerfPref = readPref(PREF_KEYS.perf, ["auto", "high", "eco", "standard", "low"], "auto");
  if (rawPerfPref === "standard") rawPerfPref = "high";
  if (rawPerfPref === "low") rawPerfPref = "eco";
  var prefPerf = rawPerfPref;
  var prefWebgl = readPref(PREF_KEYS.webgl, ["0", "1"], "0") === "1";
  var prefDefaultHome = readPref(PREF_KEYS.defaultHome, ["auto", "home", "console"], "auto");

  // Optional URL query overrides for reproducible testing
  var qForm = parseQueryParam("form");
  if (qForm === "adult" || qForm === "chibi") currentForm = qForm;
  var qTheme = parseQueryParam("theme");
  if (THEME_ORDER.indexOf(qTheme) >= 0) currentTheme = qTheme;
  var qPerf = parseQueryParam("perf");
  if (qPerf === "standard") qPerf = "high";
  if (qPerf === "low") qPerf = "eco";
  if (["auto", "high", "eco"].indexOf(qPerf) >= 0) {
    prefPerf = qPerf;
    if (parseQueryParam("webgl") !== "1") prefWebgl = false;
  }
  if (qPerf === "webgl" || parseQueryParam("webgl") === "1") {
    prefPerf = "high";
    prefWebgl = true;
  }
  var qHome = parseQueryParam("home");
  if (["auto", "home", "console"].indexOf(qHome) >= 0) prefDefaultHome = qHome;

  var activePack = null;
  var morphPending = false;
  var characterUnavailable = false;
  var hudDimTimer = 0;
  var bubbleTimer = 0;
  var toastTimer = 0;
  var drawerOpen = false;
  var activeTab = "console";
  var qqAdminWasAuthorized = false;
  var qqAdminLoadPromise = null;
  var qzoneLoadPromise = null;
  var qqAdminInitialized = false;
  var qzoneInitialized = false;
  var qqAdminCssReady = false;
  var adaptiveEvaluated = false;
  var formSwitchCount = 0;
  var liveFrame = null;
  var liveRetireTimer = 0;
  var liveStill = null;
  var pendingLiveStill = null;
  var failedStillSource = "";
  var liveVideoUnavailable = false;
  var motionPreference = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)");
  var dataConnection = navigator.connection;
  var adaptiveEco = false;

  // ---- Boot Core Modules ----
  var eventEngine = root.DaFeiYuEventEngine.createEngine();
  var packLoader = root.DaFeiYuPackLoader.createPackLoader({ basePath: "./" });
  var renderer = root.DaFeiYuStageRenderer.createStageRenderer({
    container: $("fh-stage"),
    canvas2d: $("fh-canvas-2d"),
    canvasGl: $("fh-canvas-gl"),
    packLoader: packLoader
  });
  var apiAdapter = root.DaFeiYuApiAdapter.createApiAdapter({
    consoleBase: parseQueryParam("consoleBase") || "",
    homeBase: ""
  });
  var eventAdapter = root.DaFeiYuEventAdapter.createEventAdapter({
    engine: eventEngine,
    basePath: "",
    wsUrl: parseQueryParam("wsUrl") || "",
    onStatusData: function (data) {
      apiAdapter.updateLiveStatus(data);
      var snap = eventEngine.getState();
      applyLiveStatusDots(data, snap);
      if (drawerOpen && activeTab === "console") {
        if (apiAdapter.state.overview) renderQQOverview(snap);
        else renderConsoleUI();
      }
    },
    onConnectionChange: function (conn) {
      renderNetworkBanner(conn);
      if (drawerOpen && (activeTab === "console" || activeTab === "diag")) {
        renderConsoleUI();
        refreshDiagUI();
      }
    }
  });

  // ---- UI Feedback Helpers ----
  function showToast(msg, ms) {
    var el = $("fh-toast");
    if (!el) return;
    el.textContent = String(msg || "");
    el.className = "on";
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.className = ""; }, ms || 2600);
  }

  function wakeHud() {
    var hud = $("fh-hud");
    if (!hud) return;
    hud.className = "";
    if (hudDimTimer) clearTimeout(hudDimTimer);
    hudDimTimer = setTimeout(function () {
      if (!drawerOpen) hud.className = "dim";
    }, 3600);
  }

  function showBubble(text, ms) {
    var b = $("fh-bubble");
    if (!b) return;
    if (bubbleTimer) { clearTimeout(bubbleTimer); bubbleTimer = null; }
    if (!text) {
      b.className = "hidden";
      return;
    }
    b.textContent = String(text);
    b.className = "";
    bubbleTimer = setTimeout(function () { b.className = "hidden"; }, ms || 3600);
  }

  function renderNetworkBanner(conn) {
    var b = $("fh-net-banner");
    var txt = $("fh-net-text");
    if (!b || !txt) return;
    if (conn && conn.offline) {
      txt.textContent = "网络断开或状态超时（" + (conn.lastError || "重试中") + "）· 本地家园仍正常运行";
      b.className = "on";
    } else {
      b.className = "";
    }
  }

  function setSegActive(containerId, attr, val) {
    var box = $(containerId);
    if (!box) return;
    var btns = box.querySelectorAll("button");
    for (var i = 0; i < btns.length; i++) {
      var b = btns[i];
      var baseCls = b.className.indexOf("third") >= 0 ? "fh-seg-btn third" : "fh-seg-btn";
      b.className = (b.getAttribute(attr) === val) ? (baseCls + " active") : baseCls;
    }
  }

  function syncTopHud(snap) {
    var fl = $("fh-form-label");
    var sl = $("fh-state-label");
    var st = $("fh-source-tag");
    if (fl) fl.textContent = (currentForm === "chibi") ? "Q版大肥鱼" : "成年大肥鱼";
    if (sl && snap) sl.textContent = snap.label || "待机中";
    if (st && snap) {
      var cls = snap.source === "real" ? "real" : (snap.source === "touch" ? "touch" : "demo");
      var lbl = snap.source === "real" ? "真实" : (snap.source === "touch" ? "互动" : "演示");
      st.className = "fh-tag " + cls;
      st.textContent = lbl;
    }
    var dockMorph = $("fh-dock-morph");
    if (dockMorph) dockMorph.textContent = (currentForm === "adult") ? "切换 Q版" : "切换 成年";
    var dockVideo = $("fh-dock-video");
    if (dockVideo) dockVideo.classList.toggle("active", currentTheme === "video" && currentForm === "adult");
    var quickTheme = $("fh-btn-theme-quick");
    if (quickTheme) quickTheme.textContent = THEME_ICONS[currentTheme] || "☀";
  }

  // ---- Character Dual-Form Morph Switching ----
  function switchForm(targetForm, optsSwitch) {
    var o = optsSwitch || {};
    if (targetForm !== "adult" && targetForm !== "chibi") targetForm = "adult";
    if (morphPending && !o.force) return;
    if (activePack && currentForm === targetForm && !o.forceReload) return;

    morphPending = true;
    var t0 = (window.performance && performance.now) ? performance.now() : Date.now();
    var mTag = $("fh-morph-time-tag");
    if (mTag) mTag.textContent = "预加载中…";

    packLoader.loadPack(targetForm, function (err, pack) {
      morphPending = false;
      if (err || !pack) {
        if (mTag) mTag.textContent = "加载失败";
        showToast("目标形态素材加载失败，已安全保留当前形态", 3200);
        characterUnavailable = !activePack;
        syncStageFallback();
        return;
      }
      var loadDt = Math.round(((window.performance && performance.now) ? performance.now() : Date.now()) - t0);
      var doMorph = !!(activePack && o.animate !== false);
      activePack = pack;
      characterUnavailable = false;
      currentForm = targetForm;
      savePref(PREF_KEYS.form, currentForm);
      if (doMorph) formSwitchCount += 1;

      hideStaticFallback();
      var leavingLive = liveFrame && liveFrame.classList.contains("ready");
      renderer.setPack(pack, doMorph && !leavingLive);
      renderer.setState(eventEngine.getState().state);
      if (o.theme) {
        applyTheme(o.theme);
        // Keep explicit scene links reproducible on reload without reloading now.
        var url = new URL(window.location.href);
        url.searchParams.set("form", currentForm);
        url.searchParams.set("theme", currentTheme);
        window.history.replaceState(null, "", url.href);
      } else {
        syncLiveScene();
      }
      setSegActive("fh-seg-form", "data-form", currentForm);
      syncTopHud(eventEngine.getState());
      wakeHud();

      if (mTag) mTag.textContent = doMorph ? ("切换就绪 " + loadDt + "ms") : "已加载";
      if (doMorph) {
        showBubble(currentForm === "chibi" ? "噗噜～变身 Q版幼年大肥鱼啦！" : "水光散开，成年大肥鱼回来啦～", 2600);
      }

      var other = (targetForm === "adult") ? "chibi" : "adult";
      setTimeout(function () { packLoader.prefetch(other); }, 900);
      refreshDiagUI();
    }, { forceReload: !!o.forceReload, lowPerf: prefPerf === "eco" });
  }

  function showStaticFallback(msg) {
    var fb = $("fh-fallback");
    var img = $("fh-fallback-img");
    var txt = $("fh-fallback-msg");
    if (img) {
      var portrait = window.innerHeight > window.innerWidth;
      img.src = portrait
        ? "./home/assets/rooms/fallback-whale-android.png"
        : "./home/assets/rooms/fallback-whale-pc.png";
    }
    if (txt && msg) txt.textContent = msg;
    if (fb) fb.className = "on";
  }

  function hideStaticFallback() {
    var fb = $("fh-fallback");
    if (fb) fb.className = "";
  }

  function syncStageFallback() {
    var fallback = $("fh-fallback");
    var visualReady = (liveFrame && liveFrame.classList.contains("ready")) ||
      (liveStill && liveStill.complete && liveStill.naturalWidth > 0);
    if (visualReady || !characterUnavailable) {
      hideStaticFallback();
    } else if (fallback && fallback.className !== "on") {
      showStaticFallback("无法载入角色素材，已切换静态兼容画面，控制台仍可使用。");
    }
  }

  // ---- Theme, Performance Modes (auto/high/eco + WebGL toggle) & Default Homepage ----
  function lightweightVideo() {
    var ua = navigator.userAgent || "";
    var android = /Android\s+(\d+)/i.exec(ua);
    var chrome = /Chrome\/(\d+)/i.exec(ua);
    return prefPerf === "eco" || adaptiveEco || !window.EventSource ||
      (android && Number(android[1]) <= 9) || (chrome && Number(chrome[1]) <= 66) ||
      (dataConnection && dataConnection.saveData) || (motionPreference && motionPreference.matches) ||
      (packLoader.isH264Supported && !packLoader.isH264Supported());
  }

  function prepareLiveStill(layer) {
    var src = window.innerHeight > window.innerWidth
      ? "./home/assets/rooms/fallback-whale-android.png"
      : "./home/assets/rooms/fallback-whale-pc.png";
    if (failedStillSource !== src) failedStillSource = "";
    if ((liveStill && liveStill.getAttribute("src") === src) || failedStillSource === src) {
      pendingLiveStill = null;
      return;
    }
    if (pendingLiveStill && pendingLiveStill.getAttribute("src") === src) return;

    var next = makeEl("img", "fh-live-static");
    next.alt = "";
    pendingLiveStill = next;
    next.addEventListener("load", function () {
      if (pendingLiveStill !== next) return;
      pendingLiveStill = null;
      layer.appendChild(next);
      if (liveStill) layer.removeChild(liveStill);
      liveStill = next;
      renderer.pause();
      syncStageFallback();
    });
    next.addEventListener("error", function () {
      if (pendingLiveStill !== next) return;
      pendingLiveStill = null;
      failedStillSource = src;
      syncStageFallback();
    });
    next.src = src;
  }

  function syncLiveScene() {
    var layer = $("fh-live-layer");
    if (!layer) return;
    var videoSelected = currentTheme === "video" && currentForm === "adult";
    var still = videoSelected && (lightweightVideo() || liveVideoUnavailable);
    var consoleCover = drawerOpen && document.body.classList.contains("console-home");
    var visible = !document.hidden && !consoleCover;
    var useFrame = videoSelected && !still && visible;
    if (!useFrame && liveFrame) {
      if (visible && !still && liveFrame.classList.contains("ready")) {
        // Resume the canvas before fading out the decoded video.
        if (!liveRetireTimer) {
          var retiringFrame = liveFrame;
          renderer.resume();
          retiringFrame.classList.add("retiring");
          liveRetireTimer = setTimeout(function () {
            layer.removeChild(retiringFrame);
            liveFrame = null;
            liveRetireTimer = 0;
            syncStageFallback();
          }, 200);
        }
      } else {
        clearTimeout(liveRetireTimer);
        liveRetireTimer = 0;
        layer.removeChild(liveFrame);
        liveFrame = null;
      }
    } else if (useFrame && liveRetireTimer) {
      clearTimeout(liveRetireTimer);
      liveRetireTimer = 0;
      liveFrame.classList.remove("retiring");
    }
    var frameReady = useFrame && liveFrame && liveFrame.classList.contains("ready");
    if (still && visible) prepareLiveStill(layer);
    else pendingLiveStill = null;
    if ((!videoSelected || frameReady) && liveStill) {
      layer.removeChild(liveStill);
      liveStill = null;
    }
    if (useFrame && !liveFrame) {
      var frame = makeEl("iframe", "fh-live-frame");
      frame.title = "实时动态家园";
      frame.tabIndex = -1;
      frame.setAttribute("allow", "autoplay");
      frame.src = "./live.html?mode=home";
      liveFrame = frame;
      layer.appendChild(frame);
      setTimeout(function () {
        if (liveFrame === frame && !frame.classList.contains("ready")) {
          layer.removeChild(frame);
          liveFrame = null;
          liveVideoUnavailable = true;
          syncLiveScene();
        }
      }, 15000);
    }
    if (!visible || liveStill || frameReady) {
      renderer.pause();
    } else {
      renderer.resume();
    }
    syncStageFallback();
  }

  window.addEventListener("message", function (event) {
    if (!liveFrame || event.origin !== window.location.origin ||
        event.source !== liveFrame.contentWindow || !event.data ||
        event.data.type !== "dafeiyu-live-ready") return;
    liveFrame.classList.add("ready");
    if (!liveRetireTimer) syncLiveScene();
  });

  function applyTheme(nextTheme) {
    if (THEME_ORDER.indexOf(nextTheme) < 0) nextTheme = "day";
    liveVideoUnavailable = false;
    failedStillSource = "";
    currentTheme = nextTheme;
    savePref(PREF_KEYS.theme, currentTheme);
    renderer.setTheme(currentTheme === "video" ? "day" : currentTheme);
    syncLiveScene();
    setSegActive("fh-seg-theme", "data-theme", currentTheme);
    syncTopHud(eventEngine.getState());
    wakeHud();
  }

  function selectVideoScene() {
    if (currentForm !== "adult") {
      switchForm("adult", { animate: false, theme: "video" });
    } else {
      applyTheme("video");
    }
    if (lightweightVideo()) showToast("当前设备或省电设置使用静态画面", 2600);
  }

  function applyPerfMode(mode, silent) {
    if (mode === "standard") mode = "high";
    if (mode === "low") mode = "eco";
    if (["auto", "high", "eco"].indexOf(mode) < 0) mode = "auto";
    prefPerf = mode;
    adaptiveEco = false;
    savePref(PREF_KEYS.perf, mode);
    setSegActive("fh-seg-perf", "data-perf", mode);

    if (mode === "eco") {
      prefWebgl = false;
      savePref(PREF_KEYS.webgl, "0");
      renderer.setRendererMode("2d");
      renderer.setPerfMode("eco");
      if (!silent) showToast("已切换省电模式（15 FPS，低内存低发热）", 2200);
    } else {
      renderer.setPerfMode("high");
      renderer.setRendererMode(prefWebgl ? "webgl" : "2d");
      if (!silent && mode === "high") showToast("已切换高画质模式（完整光影与微风细节）", 2000);
    }
    syncWebglButton();
    refreshDiagUI();
    syncLiveScene();
  }

  function setWebglExperimental(enabled, silent) {
    prefWebgl = !!enabled;
    savePref(PREF_KEYS.webgl, prefWebgl ? "1" : "0");
    if (prefWebgl) {
      renderer.setPerfMode("high");
      var ok = renderer.setRendererMode("webgl");
      if (!ok) {
        prefWebgl = false;
        savePref(PREF_KEYS.webgl, "0");
        if (!silent) showToast("当前浏览器不支持 WebGL，已安全保持 2D 渲染", 2600);
      } else if (!silent) {
        showToast("已开启 WebGL 1.0 实验对照渲染器", 2200);
      }
    } else {
      renderer.setRendererMode("2d");
      if (!silent) showToast("已切回轻量 2D Canvas 默认渲染", 2000);
    }
    syncWebglButton();
    refreshDiagUI();
  }

  function syncWebglButton() {
    var btn = $("fh-btn-webgl-toggle");
    if (!btn) return;
    btn.className = prefWebgl ? "fh-toggle-btn on" : "fh-toggle-btn";
    btn.textContent = prefWebgl ? "已开启（WebGL）" : "关闭（默认 2D）";
  }

  function applyDefaultHome(mode, initialBoot) {
    if (["auto", "home", "console"].indexOf(mode) < 0) mode = "auto";
    prefDefaultHome = mode;
    savePref(PREF_KEYS.defaultHome, mode);
    setSegActive("fh-seg-home", "data-home", mode);
    if (mode === "console") {
      document.body.className = "console-home";
      if (initialBoot) openDrawer("console");
    } else {
      document.body.className = "";
    }
    syncViewportChrome();
  }

  function evaluateMeasuredAdaptive() {
    if (adaptiveEvaluated) return;
    adaptiveEvaluated = true;
    // Canvas and character-pack metrics cannot measure the independent player.
    if (liveFrame || liveStill) return;
    var m = renderer.getMetrics();
    var pErrs = packLoader.getMetrics().errors.length;
    var poorPerf = (m.fps > 0 && m.fps < 18 && m.p10Fps < 14) || (m.firstPaintMs > 4500) || (pErrs >= 3);
    if (poorPerf) {
      if (prefPerf === "auto") {
        adaptiveEco = true;
        renderer.setPerfMode("eco");
        syncLiveScene();
        showToast("实测当前设备帧率偏低，已自动切换省电模式", 3200);
      }
      if (prefDefaultHome === "auto" && !drawerOpen) {
        document.body.className = "console-home";
        openDrawer("console");
        showToast("已按实测性能展开轻量控制台（可在外观中固定默认首页）", 3600);
      }
    }
  }

  // ---- Drawer Management + Mobile Swipe-Down Gesture ----
  function openDrawer(tab) {
    drawerOpen = true;
    if (tab) selectTab(tab);
    $("fh-drawer").className = "open";
    $("fh-drawer-mask").className = "open";
    syncLiveScene();
    syncViewportChrome();
    wakeHud();
    if (activeTab === "console") refreshConsoleData();
    if (activeTab === "events") refreshEventLogUI();
    if (activeTab === "diag") refreshDiagUI();
  }

  function closeDrawer() {
    drawerOpen = false;
    $("fh-drawer").className = "";
    $("fh-drawer-mask").className = "";
    syncLiveScene();
    wakeHud();
  }

  function selectTab(tab) {
    activeTab = tab;
    var tabs = document.querySelectorAll(".fh-tab");
    var i;
    for (i = 0; i < tabs.length; i++) {
      var t = tabs[i];
      t.className = (t.getAttribute("data-tab") === tab) ? "fh-tab active" : "fh-tab";
    }
    var panels = document.querySelectorAll(".fh-panel");
    for (i = 0; i < panels.length; i++) {
      var p = panels[i];
      p.className = (p.id === "fh-panel-" + tab) ? "fh-panel active" : "fh-panel";
    }
    if (tab === "console") refreshConsoleData();
    if (tab === "qq-admin") ensureQqAdminLoaded(false, true);
    if (tab === "events") refreshEventLogUI();
    if (tab === "diag") refreshDiagUI();
  }

  function consoleHomeUrl() {
    var address = new URL("/home", window.location.href);
    address.port = "18443";
    return address.href;
  }

  function setQqAdminLoadError(message) {
    var text = $("fh-qq-admin-gate-text");
    var retry = $("fh-qq-admin-retry");
    if (text) text.textContent = message || "QQ 管理模块加载失败，请检查网络后重试。";
    if (retry) retry.hidden = false;
  }

  function ensureQqAdminLoaded(retry, refresh) {
    if (activeTab !== "qq-admin" || apiAdapter.state.consoleMode !== "connected" || apiAdapter.state.fixtureDemo) return;
    var mount = $("toolsMount");
    var gate = $("fh-qq-admin-gate");
    var text = $("fh-qq-admin-gate-text");
    var retryButton = $("fh-qq-admin-retry");
    var link = $("fh-qq-admin-console-link");
    if (gate) gate.hidden = true;
    if (mount) mount.hidden = false;
    if (link) link.hidden = true;
    if (retryButton) retryButton.hidden = true;
    if (window.DaFeiYuAdminTools && qqAdminInitialized && qqAdminCssReady) {
      if (refresh) {
        window.DaFeiYuAdminTools.refresh();
        if (window.DaFeiYuQzone && qzoneInitialized) window.DaFeiYuQzone.refresh();
      }
      ensureQzoneLoaded(retry);
      return;
    }
    if (qqAdminLoadPromise) {
      if (gate) gate.hidden = false;
      if (text) text.textContent = "正在加载 QQ 管理模块…";
      return;
    }
    if (gate) gate.hidden = false;
    if (text) text.textContent = "正在加载 QQ 管理模块…";
    var css = document.getElementById("fh-qq-admin-css");
    var script = document.getElementById("fh-qq-admin-script");
    if (retry) {
      if (css && css.parentNode) css.parentNode.removeChild(css);
      if (script && script.parentNode) script.parentNode.removeChild(script);
      css = null; script = null;
    }
    qqAdminLoadPromise = new Promise(function (resolve, reject) {
      var cssReady = false, jsReady = false, failed = false;
      function complete() {
        if (failed) return;
        if (cssReady && jsReady) resolve();
      }
      function fail() {
        if (failed) return;
        failed = true;
        reject(new Error("QQ 管理模块加载失败，请检查网络后重试。"));
      }
      if (css && qqAdminCssReady) cssReady = true;
      else {
        if (css && css.parentNode) css.parentNode.removeChild(css);
        css = document.createElement("link");
        css.id = "fh-qq-admin-css"; css.rel = "stylesheet";
        css.href = "/admin-tools.css?v=20261123";
        css.onload = function () { cssReady = true; qqAdminCssReady = true; complete(); };
        css.onerror = fail; document.head.appendChild(css);
      }
      if (script) jsReady = !!window.DaFeiYuAdminTools;
      else {
        script = document.createElement("script");
        script.id = "fh-qq-admin-script"; script.src = "/admin-tools.js?v=20261123";
        script.onload = function () { jsReady = !!window.DaFeiYuAdminTools; if (jsReady) complete(); else fail(); };
        script.onerror = fail; document.head.appendChild(script);
      }
      complete();
    }).then(function () {
      qqAdminLoadPromise = null;
      if (activeTab === "qq-admin" && apiAdapter.state.consoleMode === "connected") {
        if (gate) gate.hidden = true;
        if (mount) mount.hidden = false;
        if (!qqAdminInitialized) {
          window.DaFeiYuAdminTools.mount();
          qqAdminInitialized = true;
        }
        ensureQzoneLoaded(retry);
      }
    }).catch(function (err) {
      qqAdminLoadPromise = null;
      var failedCss = document.getElementById("fh-qq-admin-css");
      var failedScript = document.getElementById("fh-qq-admin-script");
      qqAdminCssReady = false;
      if (failedCss && failedCss.parentNode) failedCss.parentNode.removeChild(failedCss);
      if (failedScript && failedScript.parentNode && !window.DaFeiYuAdminTools) failedScript.parentNode.removeChild(failedScript);
      if (activeTab === "qq-admin" && apiAdapter.state.consoleMode === "connected") setQqAdminLoadError(err.message);
    });
  }

  function ensureQzoneLoaded(retry) {
    if (activeTab !== "qq-admin" || apiAdapter.state.consoleMode !== "connected" || apiAdapter.state.fixtureDemo) return;
    var mount = $("toolsMount");
    if (window.DaFeiYuQzone) {
      if (!qzoneInitialized && mount) {
        window.DaFeiYuQzone.mount(mount);
        qzoneInitialized = true;
      }
      return;
    }
    if (qzoneLoadPromise) return;
    var script = document.getElementById("fh-qzone-script");
    if (script && (retry || script.dataset.loadFailed)) {
      if (script.parentNode) script.parentNode.removeChild(script);
      script = null;
    }
    qzoneLoadPromise = new Promise(function (resolve) {
      if (!script) {
        script = document.createElement("script");
        script.id = "fh-qzone-script";
        script.src = "/qzone-panel.js?v=20261123";
      }
      script.onload = function () {
        if (!window.DaFeiYuQzone) script.dataset.loadFailed = "1";
        resolve(!!window.DaFeiYuQzone);
      };
      script.onerror = function () { script.dataset.loadFailed = "1"; resolve(false); };
      if (!script.parentNode) document.head.appendChild(script);
    }).then(function (loaded) {
      qzoneLoadPromise = null;
      if (loaded && activeTab === "qq-admin" && apiAdapter.state.consoleMode === "connected" &&
          !apiAdapter.state.fixtureDemo && window.DaFeiYuQzone && mount && !qzoneInitialized) {
        window.DaFeiYuQzone.mount(mount);
        qzoneInitialized = true;
      }
    });
  }

  // ---- Unified 9-Module Console UI Binding ----
  function qqReplyStatus(info, snap) {
    if (!info || info.connected !== true) {
      return info && info.connected === false ? "离线" : "待确认";
    }
    if (info.running === false) return "在线 · 回复暂停";
    if (snap && snap.source === "real" && snap.verified === true && snap.platform === "qq") {
      if (snap.state === "thinking") return "在线 · 思考中";
      if (snap.state === "tool") return "在线 · 使用工具";
      if (snap.state === "replying") return "在线 · 正在回复";
    }
    if (info.running === true) return "在线 · 自动回复已开启";
    return "在线";
  }

  function applyLiveStatusDots(data, snap) {
    if (!data) return;
    var qqDot = $("fh-qq-dot");
    var qqText = $("fh-qq-text");
    var wxDot = $("fh-wx-dot");
    var wxText = $("fh-wx-text");
    if (data.qq) {
      var qc = data.qq.connected;
      if (qqText) qqText.textContent = qqReplyStatus(data.qq, snap);
      if (qqDot) qqDot.className = "fh-dot " + (qc === true
        ? (data.qq.running === false ? "paused" : "")
        : (qc === false ? "off" : "busy"));
    }
    if (data.wechat) {
      var wc = data.wechat.connected;
      if (wxText) wxText.textContent = wc === true ? "在线" : (wc === false ? "离线" : "待确认");
      if (wxDot) wxDot.className = "fh-dot " + (wc === true ? "" : (wc === false ? "off" : "busy"));
    }
  }

  function renderQQOverview(snap) {
    var st = apiAdapter.state;
    var overview = st.overview && st.overview.qq;
    var live = st.liveStatus && st.liveStatus.qq;
    var qq = live && st.lastStatusAt >= st.lastOverviewAt ? live : overview || live;
    var model = (overview && overview.model) || (live && live.model);
    $("fh-ov-qq").textContent = qq
      ? qqReplyStatus(qq, snap) + (model ? (" · " + model) : "")
      : "未连接";
  }

  function renderSaveBar() {
    var bar = $("fh-save-bar");
    if (!bar) return;
    var st = apiAdapter.state;
    if (st.saveState === "saving" || st.saveState === "confirmed" ||
        st.saveState === "mismatch" || st.saveState === "error") {
      bar.className = st.saveState;
      bar.textContent = st.saveMessage || "";
    } else {
      bar.className = "";
      bar.textContent = "";
    }
  }

  function resetQqAdminModules() {
    if (window.DaFeiYuQzone) window.DaFeiYuQzone.reset();
    if (window.DaFeiYuAdminTools) window.DaFeiYuAdminTools.reset();
    qqAdminWasAuthorized = false;
    qqAdminInitialized = false;
    qzoneInitialized = false;
  }

  function renderConsoleUI() {
    var st = apiAdapter.state;
    var conn = eventAdapter.getConnectionState();
    var banner = $("fh-console-banner");
    var sub = $("fh-drawer-sub");
    var authBox = $("fh-auth-box");
    var logoutBtn = $("fh-logout-btn");
    var qqSel = $("fh-select-qq-model");
    var wxSel = $("fh-select-wx-model");
    var verifyTag = $("fh-model-verify-tag");
    var modelNote = $("fh-model-note");
    var convList = $("fh-conv-list");
    var qqGate = $("fh-qq-admin-gate");
    var qqGateText = $("fh-qq-admin-gate-text");
    var qqTools = $("toolsMount");
    var qqLink = $("fh-qq-admin-console-link");

    if (qqGate && qqGateText && qqTools) {
      var canUseQqTools = st.consoleMode === "connected" && !st.fixtureDemo;
      qqGate.hidden = canUseQqTools;
      qqTools.hidden = !canUseQqTools;
      if (canUseQqTools) {
        qqAdminWasAuthorized = true;
        qqGateText.textContent = "已复用当前统一控制台会话。QQ 登录、插件、配置、重启计划与日志均由同源管理接口提供。";
        if (activeTab === "qq-admin") ensureQqAdminLoaded(false);
      } else if (st.consoleMode === "readonly_only") {
        resetQqAdminModules();
        if ($("fh-qq-admin-retry")) $("fh-qq-admin-retry").hidden = true;
        qqGateText.textContent = "当前家园入口为只读模式。请从现有统一控制台入口登录后使用 QQ 管理。";
        if (qqLink) { qqLink.hidden = false; qqLink.href = consoleHomeUrl(); }
      } else if (st.consoleMode === "auth_required") {
        resetQqAdminModules();
        if ($("fh-qq-admin-retry")) $("fh-qq-admin-retry").hidden = true;
        qqGateText.textContent = "请先在「控制台」页使用统一控制台会话登录，再返回此处管理 QQ。";
        if (qqLink) qqLink.hidden = true;
      } else {
        resetQqAdminModules();
        if ($("fh-qq-admin-retry")) $("fh-qq-admin-retry").hidden = true;
        qqGateText.textContent = "统一控制台当前不可用；QQ 管理仅在同源控制台会话建立后开放。";
        if (qqLink) qqLink.hidden = true;
      }
    }

    renderSaveBar();
    var writable = (st.consoleMode === "connected" && !!st.overview);
    if (authBox) authBox.style.display = (st.consoleMode === "auth_required") ? "block" : "none";
    if (logoutBtn) logoutBtn.style.display = (st.consoleMode === "connected") ? "inline-block" : "none";

    if (st.consoleMode === "connected") {
      if (st.fixtureDemo) {
        banner.className = "fh-banner warn";
        banner.textContent = "本地联调协议桩（非实时生产）：完整验证 Codex 统一控制台读写回读及 R5.2 扩展契约。";
        if (sub) sub.textContent = "本地联调协议桩 · 已认证";
      } else {
        banner.className = "fh-banner ok";
        banner.textContent = "已连接统一控制台真实会话，所有保存操作执行 POST → re-GET 回读确认。";
        if (sub) sub.textContent = "统一控制台 · 已认证";
      }
    } else if (st.consoleMode === "auth_required") {
      banner.className = "fh-banner warn";
      banner.textContent = "统一控制台服务已就绪，请先输入密码登录以解锁真实配置读写。";
      if (sub) sub.textContent = "统一控制台 · 待登录";
    } else if (st.consoleMode === "readonly_only") {
      banner.className = "fh-banner info";
      clearEl(banner);
      banner.appendChild(document.createTextNode("当前连接到只读家园服务（未挂载统一控制台写入接口）。写入控件已安全锁定，绝不伪造生产状态。"));
      var hostNow = (window.location && window.location.hostname) ? window.location.hostname : "";
      if (hostNow && window.location.port !== "18443" && window.location.port !== "18444") {
        var jumpLink = makeEl("a", "fh-btn", "打开统一控制台 (:18443)");
        jumpLink.href = consoleHomeUrl();
        jumpLink.style.marginTop = "8px";
        jumpLink.style.textDecoration = "none";
        banner.appendChild(document.createElement("br"));
        banner.appendChild(jumpLink);
      }
      if (sub) sub.textContent = "家园只读模式";
    } else {
      banner.className = "fh-banner err";
      banner.textContent = st.lastError || "暂时无法连接控制台服务，当前保留家园本地运行。";
      if (sub) sub.textContent = "离线 / 未连接";
    }

    // Module 1: Platform & Transport
    var ov = st.overview;
    var live = st.liveStatus;
    var wxInfo = (ov && ov.wechat) || (live && live.wechat) || null;
    renderQQOverview(eventEngine.getState());
    $("fh-ov-wx").textContent = wxInfo
      ? (wxInfo.connected === true ? "在线" : (wxInfo.connected === false ? "未连接" : "未确认")) +
        (wxInfo.model ? (" · " + wxInfo.model) : "")
      : "未连接";
    $("fh-ov-transport").textContent = conn.offline ? "离线重连中" : (conn.transport.toUpperCase() + (conn.connected ? " 已连接" : " 探测中"));
    $("fh-ov-time").textContent = formatClock(st.lastOverviewAt || st.lastStatusAt);

    // Module 2: Current & Fallback Models
    fillModelSelect(qqSel, ov, "qq", writable);
    fillModelSelect(wxSel, ov, "wechat", writable);
    if (modelNote) {
      modelNote.textContent = writable
        ? "切换后立即调用 POST /api/model 并重新 GET /api/overview 校验回读值。"
        : "未连接到已认证控制台写入接口，模型选择器处于只读锁定状态。";
    }
    var fbText = "后端契约预留（overview.fallbackModels）";
    if (ov && ov.fallbackModels && ov.fallbackModels.length) {
      fbText = ov.fallbackModels.join(" → ") + (st.fixtureDemo ? "（模拟）" : "");
    }
    $("fh-ov-fallback").textContent = fbText;

    var lastV = st.lastVerify.qq || st.lastVerify.wechat;
    if (verifyTag) {
      if (!writable) {
        verifyTag.className = "fh-tag";
        verifyTag.textContent = "只读锁定";
      } else if (lastV && lastV.state === "confirmed") {
        verifyTag.className = "fh-tag real";
        verifyTag.textContent = "回读已确认";
      } else if (lastV && lastV.state === "mismatch") {
        verifyTag.className = "fh-tag demo";
        verifyTag.textContent = "回读不一致";
      } else {
        verifyTag.className = "fh-tag";
        verifyTag.textContent = "已同步";
      }
    }

    // Module 3, 4, 5: Proactive Judge & Conversations
    var proSlot = st.extensions.proactive;
    var proEl = $("fh-proactive-summary");
    if (proEl) {
      if (proSlot && (proSlot.status === "live" || proSlot.status === "fixture") && proSlot.data) {
        var pd = proSlot.data;
        renderProactive(proEl, pd, writable && proSlot.status === "live");
      } else {
        proEl.textContent = "等待统一控制台同步主动发言策略。";
      }
    }

    clearEl(convList);
    var convs = (ov && ov.conversations && ov.conversations.length) ? ov.conversations : [];
    if (!writable) {
      convList.appendChild(makeEl("p", "fh-note",
        st.consoleMode === "auth_required"
          ? "请先在上方登录统一控制台，以查看和修改群聊自动回复、主动频率与群组人格。"
          : "当前无已认证控制台会话，不展示模拟群聊开关。"));
    } else if (!convs.length) {
      convList.appendChild(makeEl("p", "fh-note", "控制台暂未返回可管理的会话列表。"));
    } else {
      var styles = ov.styleOptions || [];
      var freqs = ov.frequencyOptions || [];
      for (var i = 0; i < convs.length; i++) {
        convList.appendChild(buildConversationCard(convs[i], styles, freqs, st.busy));
      }
    }

    // Modules 6, 7, 8, 9: Extended Contracts & Live Tool Routes
    renderExtensionPanels();
  }

  function renderProactive(panel, data, writable) {
    clearEl(panel);
    panel.appendChild(makeEl("p", "fh-note", "发言判断 " + (data.judgeMode || "等待同步")));
    var funnel = data.qqFunnel;
    if (funnel && funnel.readableLogs) {
      var c = funnel.counts;
      var received = typeof c.ordinary === "number" ? " · 普通群消息 " + c.ordinary : "";
      var scopeLabel = "QQ 留存事件（查询最近 24 小时）";
      if (Number.isFinite(funnel.firstEventAt) && Number.isFinite(funnel.lastEventAt) &&
          funnel.firstEventAt > 0 && funnel.lastEventAt >= funnel.firstEventAt) {
        var dateOptions = { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" };
        var firstLabel = new Date(funnel.firstEventAt * 1000).toLocaleString("zh-CN", dateOptions);
        var lastLabel = new Date(funnel.lastEventAt * 1000).toLocaleString("zh-CN", dateOptions);
        scopeLabel += " · " + firstLabel + (firstLabel === lastLabel ? "" : " 至 " + lastLabel);
      } else if (funnel.firstEventAt === null && funnel.lastEventAt === null) {
        scopeLabel += " · 暂无匹配记录";
      }
      panel.appendChild(makeEl("p", "fh-note", scopeLabel + received + " · 候选 " + c.candidate +
        " · 前置通过 " + c.gate_passed + " · 评分通过 " + c.score_passed +
        " · 评分拒绝 " + (c.score_rejected || 0) + " · 评分跳过 " + (c.score_skipped || 0) +
        " · 评分后拦截 " + c.score_after_reject + " · 入队 " + c.enqueued +
        " · 发送确认 " + c.confirmed + (funnel.truncated ? "（部分日志）" : "")));
      function appendReasonSummary(label, source, names) {
        var rows = Object.keys(source || {}).filter(function (reason) {
          return /^[a-z0-9_-]{1,48}$/.test(reason) && Number.isFinite(source[reason]) && source[reason] > 0;
        }).map(function (reason) { return [reason, source[reason]]; });
        rows.sort(function (a, b) { return b[1] - a[1] || a[0].localeCompare(b[0]); });
        if (rows.length) {
          panel.appendChild(makeEl("p", "fh-note", label + " · " + rows.slice(0, 8).map(function (row) {
            return ((names && names[row[0]]) || row[0]) + " " + row[1];
          }).join(" · ")));
        }
      }
      appendReasonSummary("评分拒绝原因", funnel.scoreRejectedReasons);
      appendReasonSummary("评分跳过原因", funnel.scoreSkippedReasons);
      appendReasonSummary("评分后拦截原因", funnel.scoreAfterRejectReasons);
      appendReasonSummary("入队后未发送原因", funnel.afterEnqueueSkipReasons, {
        model_declined: "模型选择静默", invalid_model_decision: "模型输出格式无效",
        expired_before_model: "等候期间过期", expired_after_model: "生成后过期",
        model_failed: "模型调用失败", send_rejected: "平台拒绝发送", send_unconfirmed: "平台发送结果未知"
      });
    }
    var groups = data.qqGroups || {};
    var keys = Object.keys(groups);
    // Even when the legacy QQ threshold form cannot be shown (read-only or no
    // registered QQ groups), the public-question interval section must still
    // render so WeChat pilot groups remain configurable.
    if (!writable || !keys.length) {
      renderPublicQuestionCooldown(panel, data, keys, writable);
      return;
    }

    var form = makeEl("form", "fh-row");
    var picker = makeEl("select", "fh-select");
    picker.setAttribute("aria-label", "QQ 群");
    keys.forEach(function (key) {
      var option = makeEl("option", "", "QQ 群 " + key.slice(6));
      option.value = key;
      picker.appendChild(option);
    });
    var threshold = makeEl("input", "fh-select");
    threshold.type = "number";
    threshold.min = "0.30";
    threshold.max = "0.95";
    threshold.step = "0.01";
    threshold.required = true;
    var freshness = makeEl("input", "fh-select");
    freshness.type = "number";
    freshness.min = "1";
    freshness.max = "3";
    freshness.step = "1";
    freshness.required = true;
    var thresholdLabel = makeEl("label", "fh-note", "评分阈值");
    thresholdLabel.appendChild(threshold);
    var freshnessLabel = makeEl("label", "fh-note", "新鲜度窗口（秒）");
    freshnessLabel.appendChild(freshness);
    var save = makeEl("button", "fh-toggle-btn", "保存 QQ 策略");
    save.type = "submit";
    var feedback = makeEl("p", "fh-note");
    feedback.setAttribute("role", "status");
    function showValues() {
      var row = groups[picker.value];
      threshold.value = typeof row.threshold === "number" ? row.threshold : 0.70;
      freshness.value = row.freshnessSeconds || 3;
    }
    picker.addEventListener("change", showValues);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      save.disabled = true;
      var group = picker.value;
      var nextThreshold = Number(threshold.value);
      var nextFreshness = Number(freshness.value);
      feedback.textContent = "正在保存并回读…";
      apiAdapter.saveProactive(group, nextThreshold, nextFreshness, function (error) {
        save.disabled = false;
        feedback.textContent = error || "策略已保存并回读，Swift 将在下一次刷新时加载。";
        if (error) return;
        groups[group].threshold = nextThreshold;
        groups[group].freshnessSeconds = nextFreshness;
      });
    });
    [picker, thresholdLabel, freshnessLabel, save].forEach(function (control) {
      form.appendChild(control);
    });
    showValues();
    panel.appendChild(form);
    panel.appendChild(feedback);

    renderPublicQuestionCooldown(panel, data, keys, writable);
  }

  // 公开提问自动接话间隔：QQ 与微信试点群共用的频率控制（30..1200 整数秒，默认 60）。
  // 只读/桩模式下不渲染写入控件；微信无可用群组时只给出简短说明，不展示伪造数据。
  function renderPublicQuestionCooldown(panel, data, qqKeys, writable) {
    var heading = makeEl("p", "fh-kv-k", "公开提问自动接话间隔（30–1200 秒，默认 60 秒）");
    panel.appendChild(heading);

    var defaults = data.publicQuestionCooldownDefault || 60;
    var cooldowns = data.publicQuestionCooldowns || {};
    var qqMap = cooldowns.qq || {};
    var wxMap = cooldowns.wechat || {};

    function buildCooldownForm(labelText, platform, groups, valueMap) {
      var form = makeEl("form", "fh-row");
      var picker = makeEl("select", "fh-select");
      picker.setAttribute("aria-label", labelText + "公开提问间隔群组");
      for (var i = 0; i < groups.length; i++) {
        var option = makeEl("option", "", labelText + " " + groups[i]);
        option.value = groups[i];
        picker.appendChild(option);
      }
      var input = makeEl("input", "fh-select");
      input.type = "number";
      input.min = "30";
      input.max = "1200";
      input.step = "1";
      input.required = true;
      var inputLabel = makeEl("label", "fh-note", "间隔（秒）");
      inputLabel.appendChild(input);
      var save = makeEl("button", "fh-toggle-btn", "保存" + labelText + "公开提问间隔");
      save.type = "submit";
      var feedback = makeEl("p", "fh-note");
      feedback.setAttribute("role", "status");

      function showValue() {
        var raw = valueMap[picker.value];
        input.value = (typeof raw === "number") ? String(raw) : String(defaults);
      }
      picker.addEventListener("change", showValue);
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        save.disabled = true;
        var seconds = Number(input.value);
        feedback.textContent = "正在保存并回读…";
        apiAdapter.savePublicQuestionCooldown(platform, picker.value, seconds, function (error, result) {
          save.disabled = false;
          if (error) {
            feedback.textContent = error;
            return;
          }
          if (result && result.verified) {
            feedback.textContent = "已保存并回读确认：" + seconds + " 秒";
            valueMap[picker.value] = seconds;
          } else {
            feedback.textContent = (result && result.message) || "设置已提交，但回读值不一致。";
          }
        });
      });
      [picker, inputLabel, save].forEach(function (control) {
        form.appendChild(control);
      });
      showValue();
      panel.appendChild(form);
      panel.appendChild(feedback);
    }

    if (!writable) {
      panel.appendChild(makeEl("p", "fh-note", "写入控件未解锁：公开提问间隔需在已认证控制台中修改。"));
      return;
    }

    if (qqKeys.length) {
      buildCooldownForm("QQ 群", "qq", qqKeys, qqMap);
    } else {
      panel.appendChild(makeEl("p", "fh-note", "暂无可配置的 QQ 试点群。"));
    }

    var wxGroups = [];
    var seen = {};
    var i;
    if (data.wechatEnabledGroups && data.wechatEnabledGroups.length) {
      for (i = 0; i < data.wechatEnabledGroups.length; i++) {
        var wg = data.wechatEnabledGroups[i];
        if (typeof wg === "string" && wg && !seen[wg]) {
          seen[wg] = true;
          wxGroups.push(wg);
        }
      }
    }
    if (!wxGroups.length) {
      var wxKeys = Object.keys(wxMap);
      for (i = 0; i < wxKeys.length; i++) {
        if (!seen[wxKeys[i]]) {
          seen[wxKeys[i]] = true;
          wxGroups.push(wxKeys[i]);
        }
      }
    }
    if (wxGroups.length) {
      buildCooldownForm("微信群", "wechat", wxGroups, wxMap);
    } else {
      panel.appendChild(makeEl("p", "fh-note", "暂无可配置的微信试点群（不会生成模拟群名）。"));
    }
  }

  function renderExtensionPanels() {
    var ex = apiAdapter.state.extensions;

    // Module 6: Long-Term Memory & DB
    renderExtSlot("fh-mem-tag", "fh-mem-body", ex.memory, function (d, body) {
      body.appendChild(makeKvRow("隔离模式", d.isolation || "per-group"));
      body.appendChild(makeKvRow("已索引群组 / 记忆条数", (d.groups || 0) + " 群 / " + (d.entries || 0) + " 条"));
      body.appendChild(makeKvRow("数据库体积 / Journal", (d.dbSizeMb || "--") + " MB · " + (d.journalState || "ok")));
    });

    // Module 7: Sticker Packs
    renderExtSlot("fh-sticker-tag", "fh-sticker-body", ex.stickers, function (d, body) {
      body.appendChild(makeKvRow("当前表情包", d.activePack || "whalegirl_batch1"));
      body.appendChild(makeKvRow("可用素材 / 轮换防重", (d.count || 0) + " 枚 · " + (d.rotation || "round-robin")));
      body.appendChild(makeKvRow("冷却窗口", (d.cooldownSec || 0) + " 秒"));
    });

    // Module 8: Extensions & Plugins (/api/tools/status + /api/tools/plugins)
    var extTag = $("fh-ext-tag");
    var extBody = $("fh-ext-body");
    if (extTag && extBody) {
      clearEl(extBody);
      var pSlot = ex.plugins;
      var tSlot = ex.toolsStatus;
      if ((pSlot.status === "live" || pSlot.status === "fixture") && pSlot.data) {
        extTag.className = "fh-tag " + (pSlot.status === "live" ? "real" : "demo");
        extTag.textContent = pSlot.status === "live" ? "实时接口" : "模拟数据";
        var plugins = pSlot.data.plugins || [];
        extBody.appendChild(makeKvRow("已载入插件数", plugins.length + " 个"));
        for (var i = 0; i < Math.min(plugins.length, 4); i++) {
          extBody.appendChild(makeKvRow(plugins[i].name || plugins[i].id, plugins[i].enabled ? "已启用" : "停用"));
        }
        if (tSlot.data && tSlot.data.napcat) {
          extBody.appendChild(makeKvRow("NapCat 核心", tSlot.data.napcat.reachable ? "可达" : "未连接"));
        }
      } else {
        extTag.className = "fh-tag";
        extTag.textContent = pSlot.status === "auth_required" ? "需登录" : "待接入";
        extBody.appendChild(makeEl("p", "fh-note",
          pSlot.status === "auth_required"
            ? "登录后自动读取 Codex /api/tools/status 与 /api/tools/plugins。"
            : "只读模式下未挂载 /api/tools/plugins（不显示伪造插件状态）。"));
      }
    }

    // Module 9: Runtime Logs, Usage & R5 Shared Trace
    var trTag = $("fh-trace-tag");
    var trBody = $("fh-trace-body");
    if (trTag && trBody) {
      clearEl(trBody);
      var trSlot = ex.trace;
      var logSlot = ex.logs;
      var traceRequiresLogin = trSlot.status === "auth_required" || apiAdapter.state.consoleMode === "auth_required";
      if (!traceRequiresLogin && (trSlot.status === "live" || trSlot.status === "fixture") && trSlot.data) {
        trTag.className = "fh-tag " + (trSlot.status === "live" ? "real" : "demo");
        trTag.textContent = trSlot.status === "live" ? "实时接口" : "模拟数据";
        var td = trSlot.data;
        var ms = function (value) { return typeof value === "number" ? value + " ms" : "—"; };
        trBody.appendChild(makeKvRow("共享网关", td.gatewayTrace || "暂无路由样本"));
        if (typeof td.recentCalls === "number") {
          trBody.appendChild(makeKvRow("最近请求 / 回退 / 带 Trace",
            td.recentCalls + " / " + td.fallbackCount + " / " + td.gatewayTracedCalls));
        }
        trBody.appendChild(makeKvRow("网关处理 P50 / P95", ms(td.gatewayP50Ms) + " / " + ms(td.gatewayP95Ms)));
        var sampleCount = td.wechatConfirmedSamples || 0;
        trBody.appendChild(makeKvRow("微信入队到确认 P50 / P95",
          sampleCount ? ms(td.wechatP50Ms) + " / " + ms(td.wechatP95Ms) + "（" + sampleCount + " 条）"
            : "暂无已确认样本"));
      } else {
        trTag.className = "fh-tag";
        trTag.textContent = traceRequiresLogin ? "需登录" : "暂不可用";
        trBody.appendChild(makeEl("p", "fh-note",
          traceRequiresLogin ? "登录控制台后查看路由与送达耗时。" : "暂时无法读取路由与送达统计。"));
      }
      if (!traceRequiresLogin && (logSlot.status === "live" || logSlot.status === "fixture") &&
          logSlot.data && logSlot.data.lines) {
        var box = makeEl("div", "fh-log-box");
        box.style.marginTop = "8px";
        var lines = logSlot.data.lines.slice(-6);
        for (var j = 0; j < lines.length; j++) {
          box.appendChild(makeEl("div", "fh-log-line", lines[j]));
        }
        trBody.appendChild(box);
      }

      // Gemini Web Gateway Health & Quick Switch (Side Track Monitoring)
      var gSlot = ex.gemini;
      if (gSlot && (gSlot.status === "live" || gSlot.status === "fixture") && gSlot.data) {
        var gd = gSlot.data;
        var gDisabled = gd.overall === "disabled" || gd.account_status === "DISABLED";
        var gWarn = makeEl("div", "fh-banner " + (gDisabled ? "" : (gd.degraded ? "err" : "ok")));
        gWarn.style.marginTop = "8px";
        gWarn.textContent = gDisabled
          ? "Gemini 网关已停用"
          : gd.degraded
          ? ("Gemini 网关退化告警：" + (gd.account_status || "DEGRADED") + " · 可用模型=[" + ((gd.available_models || []).join(", ") || "无") + "]" + (gd.alert_message ? (" — " + gd.alert_message) : ""))
          : ("Gemini 网关正常：" + (gd.account_status || "AVAILABLE") + " · 可用模型=" + (gd.available_models || []).length + " 个");
        trBody.appendChild(gWarn);
        if (gd.active_model) {
          trBody.appendChild(makeKvRow(gDisabled ? "Gemini 已保存模型" : "Gemini 当前目标模型", gd.active_model));
        }
        if (!gDisabled && apiAdapter.state.consoleMode === "connected" && gd.allowed_models && gd.allowed_models.length) {
          var swRow = makeEl("div", "fh-seg");
          swRow.style.marginTop = "6px";
          for (var gi = 0; gi < gd.allowed_models.length; gi++) {
            (function (mid) {
              var b = makeEl("button", "fh-seg-btn" + (gd.active_model === mid ? " active" : ""), "切至 " + mid);
              b.type = "button";
              b.disabled = !!apiAdapter.state.busy;
              b.onclick = function () {
                renderSaveBar();
                apiAdapter.setGeminiModel(mid, function (err, res) {
                  if (err) showToast(err.message, 3200);
                  else showToast((res && res.message) || ("已切至 " + mid), 2400);
                  renderConsoleUI();
                });
              };
              swRow.appendChild(b);
            })(gd.allowed_models[gi]);
          }
          trBody.appendChild(swRow);
        }
      }
    }
  }

  function renderExtSlot(tagId, bodyId, slot, renderFn) {
    var tag = $(tagId);
    var body = $(bodyId);
    if (!tag || !body) return;
    clearEl(body);
    if (slot && (slot.status === "live" || slot.status === "fixture") && slot.data) {
      tag.className = "fh-tag " + (slot.status === "live" ? "real" : "demo");
      tag.textContent = slot.status === "live" ? "实时接口" : "模拟数据";
      renderFn(slot.data, body);
    } else if (slot && slot.status === "auth_required") {
      tag.className = "fh-tag";
      tag.textContent = "需登录";
      body.appendChild(makeEl("p", "fh-note", "登录统一控制台后读取 " + slot.endpoint + "。"));
    } else {
      tag.className = "fh-tag";
      tag.textContent = "契约预留";
      body.appendChild(makeEl("p", "fh-note",
        "后端接口 " + (slot ? slot.endpoint : "") + " 尚未挂载；本界面拒绝用伪造数据冒充实时运行状态。"));
    }
  }

  function fillModelSelect(sel, ov, platform, writable) {
    if (!sel) return;
    clearEl(sel);
    var models = (ov && ov.models) ? ov.models : [];
    var currentId = (ov && ov[platform] && ov[platform].model) ? ov[platform].model : "";
    if (!models.length) {
      var opt0 = makeEl("option", "", currentId || "未连接到模型列表");
      opt0.value = currentId;
      sel.appendChild(opt0);
      sel.disabled = true;
      return;
    }
    for (var i = 0; i < models.length; i++) {
      var m = models[i];
      if (m.platforms && m.platforms.length && m.platforms.indexOf(platform) < 0) continue;
      var opt = makeEl("option", "", m.name || m.id);
      opt.value = m.id;
      if (m.id === currentId) opt.selected = true;
      sel.appendChild(opt);
    }
    var capModel = !(ov && ov[platform] && ov[platform].capabilities && ov[platform].capabilities.model === false);
    sel.disabled = !writable || !capModel || apiAdapter.state.busy;
  }

  function buildConversationCard(conv, styles, freqs, busy) {
    var box = makeEl("div", "fh-conv-item");
    var head = makeEl("div", "fh-conv-head");
    var platLabel = (conv.platform === "wechat") ? "微信" : "QQ";
    head.appendChild(makeEl("span", "fh-conv-name", "[" + platLabel + "] " + (conv.name || conv.id)));

    var toggle = makeEl("button", "fh-toggle-btn" + (conv.enabled ? " on" : ""),
                        conv.enabled ? "自动回复：开" : "自动回复：关");
    toggle.type = "button";
    toggle.disabled = !!busy;
    toggle.onclick = function () {
      var nextVal = !conv.enabled;
      toggle.disabled = true;
      renderSaveBar();
      apiAdapter.updateConversation(conv.platform, conv.id, "enabled", nextVal, function (err, res) {
        if (err) showToast(err.message, 3000);
        else showToast((res && res.message) || "已保存并回读确认", 2200);
        renderConsoleUI();
      });
    };
    head.appendChild(toggle);
    box.appendChild(head);

    if (freqs.length) {
      var fSel = makeEl("select", "fh-select");
      fSel.disabled = !!busy;
      for (var j = 0; j < freqs.length; j++) {
        var fOpt = makeEl("option", "", "主动插话频率：" + (freqs[j].name || freqs[j].id));
        fOpt.value = freqs[j].id;
        if (freqs[j].id === conv.frequency) fOpt.selected = true;
        fSel.appendChild(fOpt);
      }
      fSel.onchange = function () {
        apiAdapter.updateConversation(conv.platform, conv.id, "frequency", fSel.value, function (err, res) {
          if (err) showToast(err.message, 3000);
          else showToast((res && res.message) || "频率设置已回读确认", 2200);
          renderConsoleUI();
        });
      };
      box.appendChild(fSel);
    }

    if (styles.length) {
      var sSel = makeEl("select", "fh-select");
      sSel.style.marginBottom = "0";
      sSel.disabled = !!busy;
      for (var k = 0; k < styles.length; k++) {
        var sOpt = makeEl("option", "", "群组人格：" + (styles[k].name || styles[k].id));
        sOpt.value = styles[k].id;
        if (styles[k].id === conv.style) sOpt.selected = true;
        sSel.appendChild(sOpt);
      }
      sSel.onchange = function () {
        apiAdapter.updateConversation(conv.platform, conv.id, "style", sSel.value, function (err, res) {
          if (err) showToast(err.message, 3000);
          else showToast((res && res.message) || "群组人格已回读确认", 2200);
          renderConsoleUI();
        });
      };
      box.appendChild(sSel);
    }

    return box;
  }

  function refreshConsoleData() {
    apiAdapter.probeSession(function (err, st) {
      if (!err && st.consoleMode === "connected") {
        apiAdapter.fetchOverview(function () {
          renderConsoleUI();
        });
        apiAdapter.fetchAllExtensions(function () {
          if (drawerOpen && activeTab === "console") renderExtensionPanels();
        });
      } else {
        renderConsoleUI();
      }
    });
  }

  // ---- Event Inspector Log & Diagnostics ----
  function refreshEventLogUI() {
    var box = $("fh-event-log");
    if (!box) return;
    var logs = eventAdapter.getEventLog();
    clearEl(box);
    if (!logs.length) {
      box.textContent = "尚无事件记录。";
      return;
    }
    for (var i = 0; i < Math.min(logs.length, 16); i++) {
      var row = logs[i];
      var line = formatClock(row.at) + " [" + row.kind + "] " + row.type +
                 " (" + (row.accepted ? "OK" : "SKIP") + ": " + row.reason + ")";
      box.appendChild(makeEl("div", "fh-log-line", line));
    }
  }

  function buildMetricsSnapshot() {
    var rm = renderer.getMetrics();
    var em = eventEngine.getState();
    var pm = packLoader.getMetrics();
    var conn = eventAdapter.getConnectionState();
    return {
      version: "2.2.0-r5.2",
      form: currentForm,
      themePref: currentTheme,
      theme: rm.theme,
      perfPref: prefPerf,
      webglPref: prefWebgl,
      defaultHomePref: prefDefaultHome,
      state: em.state,
      source: em.source,
      verified: em.verified,
      eventStats: em.stats,
      connection: conn,
      renderer: rm,
      pack: activePack ? {
        id: activePack.manifest.id,
        version: activePack.manifest.version,
        bytes: activePack.bytes,
        loadMs: activePack.loadMs
      } : null,
      packLoader: pm,
      consoleMode: apiAdapter.state.consoleMode,
      fixtureDemo: apiAdapter.state.fixtureDemo,
      formSwitchCount: formSwitchCount,
      ua: String(navigator.userAgent || "").slice(0, 140)
    };
  }

  function refreshDiagUI() {
    var m = buildMetricsSnapshot();
    var rm = m.renderer;
    var conn = m.connection;
    $("fh-d-renderer").textContent = rm.renderer.toUpperCase() + " · " + prefPerf + " (" + rm.perfMode + ")";
    $("fh-d-transport").textContent = conn.transport.toUpperCase() + (conn.connected ? " 已连" : " 离线") + " (重连 " + conn.reconnectAttempts + ")";
    $("fh-d-viewport").textContent = rm.viewport + " @ " + rm.dpr + "x (" + rm.orientation + ")";
    $("fh-d-fps").textContent = (rm.fps || "--") + " / " + (rm.p10Fps || "--") + " FPS";
    $("fh-d-first").textContent = (rm.firstPaintMs || "--") + "ms / " + (rm.firstCharMs || "--") + "ms";
    $("fh-d-morph").textContent = rm.morphCount
      ? (rm.lastMorphMs + "ms · 空帧 " + rm.morphBlankFrames)
      : "未触发";
    $("fh-d-mem").textContent = (rm.memoryMb !== null) ? (rm.memoryMb + " MB") : "浏览器未暴露";
    $("fh-d-drops").textContent = rm.frameDrops + " 次 / " + rm.pauseCount + " 次";
    $("fh-d-events").textContent =
      m.eventStats.deduped + " / " + m.eventStats.priorityBlocked + " / " + m.eventStats.expired;
    $("fh-d-pack").textContent = m.pack ? (m.pack.id + " v" + m.pack.version) : "未加载";
    $("fh-d-caps").textContent = (m.pack ? Math.round(m.pack.bytes / 1024) + "KB" : "--") +
      " · W:" + (m.packLoader.webpSupported ? "Y" : "N") +
      " H264:" + (m.packLoader.h264Supported ? "Y" : "N") +
      " C:" + (m.packLoader.cacheApiAvailable ? "Y" : "N");
    var uaMatch = m.ua.match(/(Chrome|Firefox)\/[0-9.]+/);
    $("fh-d-ua").textContent = uaMatch ? uaMatch[0] : m.ua.slice(0, 32);
  }

  function sendDiagToServer() {
    var m = buildMetricsSnapshot();
    apiAdapter.postDiag({
      ua: m.ua,
      w: window.innerWidth,
      h: window.innerHeight,
      dpr: window.devicePixelRatio || 1,
      page: location.href,
      version: m.version,
      form: m.form,
      theme: m.theme,
      perfPref: m.perfPref,
      state: m.state,
      transport: m.connection.transport,
      renderer: m.renderer.renderer,
      fps: m.renderer.fps,
      p10Fps: m.renderer.p10Fps,
      firstPaintMs: m.renderer.firstPaintMs,
      firstCharMs: m.renderer.firstCharMs,
      lastMorphMs: m.renderer.lastMorphMs,
      morphBlankFrames: m.renderer.morphBlankFrames,
      memoryMb: m.renderer.memoryMb,
      errors: m.packLoader.errors.slice(0, 10)
    });
  }

  // ---- Wire DOM Listeners & Mobile Touch Gestures ----
  eventEngine.subscribe(function (snap) {
    renderer.setState(snap.state);
    syncTopHud(snap);
    applyLiveStatusDots(apiAdapter.state.liveStatus, snap);
    if (drawerOpen && activeTab === "console") renderQQOverview(snap);
    wakeHud();
    if (snap.bubble) showBubble(snap.bubble, 3600);
    else showBubble("", 0);
    if (drawerOpen && activeTab === "events") refreshEventLogUI();
    if (drawerOpen && activeTab === "diag") refreshDiagUI();
  });

  $("fh-dock-morph").onclick = function () {
    switchForm(currentForm === "adult" ? "chibi" : "adult", { animate: true });
  };
  $("fh-dock-demo").onclick = function () {
    eventAdapter.runReplayScenario("chat_cycle");
    showToast("开始演练完整消息动画链", 1800);
  };
  $("fh-dock-video").onclick = selectVideoScene;
  $("fh-btn-theme-quick").onclick = function () {
    var idx = (THEME_ORDER.indexOf(currentTheme) + 1) % THEME_ORDER.length;
    applyTheme(THEME_ORDER[idx]);
  };
  $("fh-dock-console").href = consoleHomeUrl();
  $("fh-btn-drawer-top").onclick = function () { openDrawer("console"); };
  $("fh-drawer-close").onclick = closeDrawer;
  $("fh-drawer-mask").onclick = closeDrawer;
  $("fh-net-retry").onclick = function () {
    eventAdapter.retryNow();
    refreshConsoleData();
    showToast("正在重新连接服务…", 1600);
  };

  // Interactive tap on DaFeiYu character (gentle touch reaction; never overrides verified real chat states)
  $("fh-stage").onclick = function (ev) {
    wakeHud();
    if (ev && renderer.hitTestCharacter(ev.clientX, ev.clientY)) {
      renderer.spawnTapBurst(ev.clientX, ev.clientY);
      var touchRes = eventEngine.pushEvent({
        type: "happy",
        source: "touch",
        verified: false,
        text: currentForm === "chibi" ? "诶嘿～戳到小肥鱼啦！" : "嗯？找大肥鱼有什么事呀～"
      });
      if (touchRes.accepted) {
        refreshEventLogUI();
      }
    }
  };

  // Mobile bottom-sheet swipe-down gesture to close drawer
  var touchStartY = 0;
  var headEl = $("fh-drawer-head");
  if (headEl) {
    headEl.addEventListener("touchstart", function (ev) {
      if (ev.touches && ev.touches.length) touchStartY = ev.touches[0].clientY;
    }, false);
    headEl.addEventListener("touchend", function (ev) {
      if (ev.changedTouches && ev.changedTouches.length && touchStartY > 0) {
        var dy = ev.changedTouches[0].clientY - touchStartY;
        if (dy > 48 && window.innerWidth <= 640) closeDrawer();
      }
      touchStartY = 0;
    }, false);
  }

  var tabBtns = document.querySelectorAll(".fh-tab");
  for (var ti = 0; ti < tabBtns.length; ti++) {
    (function (btn) {
      btn.onclick = function () { selectTab(btn.getAttribute("data-tab")); };
    })(tabBtns[ti]);
  }

  function wireSeg(containerId, attr, handler) {
    var box = $(containerId);
    if (!box) return;
    var btns = box.querySelectorAll("button");
    for (var i = 0; i < btns.length; i++) {
      (function (b) {
        b.onclick = function () { handler(b.getAttribute(attr)); };
      })(btns[i]);
    }
  }

  wireSeg("fh-seg-form", "data-form", function (val) { switchForm(val, { animate: true }); });
  wireSeg("fh-seg-theme", "data-theme", function (val) {
    if (val === "video") selectVideoScene();
    else applyTheme(val);
  });
  wireSeg("fh-seg-perf", "data-perf", function (val) { applyPerfMode(val, false); });
  wireSeg("fh-seg-home", "data-home", function (val) {
    applyDefaultHome(val, false);
    showToast("默认首页已更新并持久化", 2000);
  });
  wireSeg("fh-seg-events", "data-ev", function (evName) {
    eventAdapter.clearReplay();
    eventAdapter.setDemoHold(true);
    eventAdapter.ingest({ type: evName, source: "demo", verified: false, force: true }, "dev");
    refreshEventLogUI();
  });

  $("fh-btn-webgl-toggle").onclick = function () { setWebglExperimental(!prefWebgl, false); };
  $("fh-btn-cycle").onclick = function () {
    eventAdapter.runReplayScenario("chat_cycle");
    showToast("开始回放完整聊天周期", 1800);
  };
  $("fh-btn-codex-replay").onclick = function () {
    eventAdapter.runReplayScenario("codex_bridge");
    showToast("开始回放 Codex Bridge 真实协议", 1800);
  };
  $("fh-btn-err-replay").onclick = function () {
    eventAdapter.runReplayScenario("error_recovery");
    showToast("开始回放异常自愈流程", 1800);
  };
  $("fh-btn-resume-live").onclick = function () {
    eventAdapter.clearReplay();
    eventAdapter.setDemoHold(false);
    eventAdapter.retryNow();
    showToast("已恢复实时事件监听", 1800);
  };
  $("fh-btn-hotreload").onclick = function () {
    packLoader.invalidate(currentForm);
    switchForm(currentForm, { animate: false, force: true, forceReload: true });
    showToast("正在热刷新角色素材包…", 1800);
  };
  $("fh-btn-send-diag").onclick = function () {
    sendDiagToServer();
    refreshDiagUI();
    showToast("已上报当前渲染诊断数据", 2000);
  };

  // Console Auth & Model Handlers
  $("fh-login-btn").onclick = function () {
    var pwd = $("fh-login-pwd").value || "";
    var errEl = $("fh-login-err");
    if (!pwd) {
      errEl.textContent = "请输入控制台密码";
      errEl.style.display = "block";
      return;
    }
    errEl.style.display = "none";
    $("fh-login-btn").disabled = true;
    apiAdapter.login(pwd, function (err) {
      $("fh-login-btn").disabled = false;
      $("fh-login-pwd").value = "";
      if (err) {
        errEl.textContent = err.message;
        errEl.style.display = "block";
      } else {
        showToast("已登录统一控制台", 2200);
        apiAdapter.fetchAllExtensions(function () {
          renderConsoleUI();
        });
      }
      renderConsoleUI();
    });
  };

  $("fh-logout-btn").onclick = function () {
    apiAdapter.logout(function () {
      resetQqAdminModules();
      showToast("已退出控制台登录", 2000);
      renderConsoleUI();
    });
  };

  $("fh-qq-admin-retry").onclick = function () { ensureQqAdminLoaded(true, false); };

  document.addEventListener("fh-qq-admin-unauthorized", function () {
    if (apiAdapter.state.consoleMode !== "connected") return;
    apiAdapter.state.consoleMode = "auth_required";
    apiAdapter.state.overview = null;
    renderConsoleUI();
  });

  $("fh-refresh-btn").onclick = function () {
    refreshConsoleData();
    eventAdapter.pollOnce();
    showToast("正在刷新状态…", 1400);
  };

  $("fh-select-qq-model").onchange = function () {
    var val = $("fh-select-qq-model").value;
    renderSaveBar();
    apiAdapter.setModel("qq", val, function (err, res) {
      if (err) showToast(err.message, 3200);
      else showToast((res && res.message) || "QQ 模型已保存", 2400);
      renderConsoleUI();
    });
  };

  $("fh-select-wx-model").onchange = function () {
    var val = $("fh-select-wx-model").value;
    renderSaveBar();
    apiAdapter.setModel("wechat", val, function (err, res) {
      if (err) showToast(err.message, 3200);
      else showToast((res && res.message) || "微信模型已保存", 2400);
      renderConsoleUI();
    });
  };

  // Viewport & Visibility Lifecycle
  function syncViewportChrome() {
    var ih = window.innerHeight || 533;
    var ch = (document.documentElement && document.documentElement.clientHeight) ? document.documentElement.clientHeight : ih;
    var visH = Math.min(ih, ch);
    var ua = String(navigator.userAgent || "");
    var isCompactWebView = (/\bwv\b/.test(ua) && window.innerWidth <= 360);
    var dock = $("fh-dock");
    if (dock) {
      var dockH = dock.offsetHeight || 42;
      var margin = isCompactWebView ? 42 : ((window.innerWidth <= 640) ? 10 : 16);
      dock.style.bottom = "auto";
      dock.style.top = Math.max(80, visH - dockH - margin) + "px";
    }
    var mask = $("fh-drawer-mask");
    if (mask) mask.style.height = visH + "px";
    var drawer = $("fh-drawer");
    if (drawer && window.innerWidth <= 640) {
      var dh = Math.round(visH * (document.body.className.indexOf("console-home") >= 0 ? 0.92 : 0.80));
      drawer.style.height = dh + "px";
      drawer.style.top = "auto";
      drawer.style.bottom = "0px";
    } else if (drawer) {
      drawer.style.height = visH + "px";
      drawer.style.top = "0px";
      drawer.style.bottom = "auto";
    }
  }

  document.addEventListener("visibilitychange", function () {
    syncLiveScene();
    if (!document.hidden) eventAdapter.pollOnce();
  });
  if (motionPreference && motionPreference.addEventListener) {
    motionPreference.addEventListener("change", syncLiveScene);
  } else if (motionPreference && motionPreference.addListener) {
    motionPreference.addListener(syncLiveScene);
  }
  if (dataConnection && dataConnection.addEventListener) {
    dataConnection.addEventListener("change", syncLiveScene);
  }

  window.addEventListener("online", function () {
    if (liveVideoUnavailable || failedStillSource) {
      liveVideoUnavailable = false;
      failedStillSource = "";
      syncLiveScene();
    }
    eventAdapter.retryNow();
    showToast("网络已恢复，正在同步最新状态", 2000);
  });
  window.addEventListener("offline", function () {
    renderNetworkBanner({ offline: true, lastError: "设备离线" });
  });

  window.addEventListener("resize", function () {
    renderer.resize();
    syncLiveScene();
    syncViewportChrome();
  });
  window.addEventListener("orientationchange", function () {
    setTimeout(function () {
      renderer.resize();
      syncLiveScene();
      syncViewportChrome();
    }, 180);
  });

  // ---- Start Application ----
  applyDefaultHome(prefDefaultHome, true);
  applyPerfMode(prefPerf, true);
  if (prefWebgl) setWebglExperimental(true, true);
  applyTheme(currentTheme);
  renderer.start();
  syncLiveScene();
  syncViewportChrome();
  setTimeout(function () { renderer.resize(); syncViewportChrome(); }, 350);
  setTimeout(function () { renderer.resize(); syncViewportChrome(); }, 1200);
  switchForm(currentForm, { animate: false });
  syncTopHud(eventEngine.getState());
  wakeHud();

  eventAdapter.start();

  // Periodic ticks
  setInterval(function () { eventEngine.tick(); }, 1000);
  setInterval(function () {
    if (drawerOpen && activeTab === "diag") refreshDiagUI();
    if (drawerOpen && activeTab === "events") refreshEventLogUI();
  }, 1500);
  setTimeout(refreshConsoleData, 500);
  setTimeout(evaluateMeasuredAdaptive, 4200);
  setTimeout(sendDiagToServer, 3500);
  setInterval(function () {
    if (!document.hidden) sendDiagToServer();
  }, 15000);

  // ---- Public Integration Contract (for Codex & Automated Tests) ----
  root.DaFeiYuHome = {
    version: "2.2.0-r5.2",
    pushEvent: function (ev) {
      if (ev && ev.source === "demo") eventAdapter.setDemoHold(true);
      return eventEngine.pushEvent(ev);
    },
    ingestRaw: function (payload) {
      return eventAdapter.ingest(payload, "api");
    },
    exitDemoMode: function () {
      eventAdapter.clearReplay();
      eventAdapter.setDemoHold(false);
      eventAdapter.retryNow();
    },
    setForm: function (form, animate) { switchForm(form, { animate: animate !== false }); },
    setTheme: function (theme) { applyTheme(theme); },
    setPerfMode: function (mode) {
      if (mode === "webgl") setWebglExperimental(true, true);
      else {
        setWebglExperimental(false, true);
        applyPerfMode(mode, true);
      }
    },
    setWebgl: function (on) { setWebglExperimental(on, true); },
    setDefaultHome: function (mode) { applyDefaultHome(mode, false); },
    openDrawer: openDrawer,
    closeDrawer: closeDrawer,
    runReplayScenario: function (id) { eventAdapter.runReplayScenario(id); },
    getState: function () { return eventEngine.getState(); },
    getHistory: function () { return eventEngine.getHistory(); },
    getEventLog: function () { return eventAdapter.getEventLog(); },
    getMetrics: buildMetricsSnapshot,
    subscribe: function (fn) { return eventEngine.subscribe(fn); },
    sendDiag: sendDiagToServer
  };
})(typeof self !== "undefined" ? self : this);
