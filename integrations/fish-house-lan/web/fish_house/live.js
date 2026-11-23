(function () {
  "use strict";
  var videos = [document.getElementById("videoA"), document.getElementById("videoB")];
  var bubble = document.getElementById("bubble"), cover = document.getElementById("cover");
  var active = 0, theme, currentNode = "", pendingNode = "", queue = [], socket = null, reconnect = 0;
  var lastSeq = -1, firstStateOnSocket = true, lastTransition = "", lastTransitionAt = 0, lastOrders = "", lastOrdersAt = 0;
  var lastOrderEvent = "";
  var expiry = 0, bubbleExpiry = 0, transitionTimer = 0, startedAt = 0;
  var idleDelay = 0, showBubble = true, bubbleOverride = null, themeBase = "", idleNode = "", phoneNode = "";
  var pickupPreload = null, themeEpoch = 0;
  var embedded = new URLSearchParams(location.search).get("mode") === "home";
  var allowedSources = { qq: true, wechat: true };
  var allowedPhases = { idle: true, received: true, thinking: true, replying: true, sent: true, error: true };

  function edges() {
    var list = theme && theme.config && theme.config.flow && theme.config.flow.edges || [];
    var out = {};
    list.forEach(function (edge) { if (edge && edge.from && edge.to) (out[edge.from] || (out[edge.from] = {}))[edge.to] = true; });
    return out;
  }
  function node(id) {
    var list = theme && theme.config && theme.config.flow && theme.config.flow.nodes || [];
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
    return null;
  }
  function safeThemeUrl(raw) {
    if (typeof raw !== "string" || !raw || /[\\%]/.test(raw)) return null;
    try {
      var url = new URL(raw, location.href);
      if (url.origin !== location.origin || url.search || url.hash || !/^\/fish_house\/themes\/[A-Za-z0-9_-]+\/theme\.json$/.test(url.pathname)) return null;
      return url;
    } catch (_) { return null; }
  }
  function assetUrl(raw, kind) {
    var pattern = kind === "cover" ? /^cover\.[A-Za-z0-9]+$/i : /^videos\/[A-Za-z0-9_.-]+\.mp4$/i;
    if (typeof raw !== "string" || !pattern.test(raw)) return "";
    var resolved = new URL(raw, themeBase);
    return resolved.origin === location.origin && resolved.pathname.indexOf(themeBase.pathname) === 0 ? resolved.href : "";
  }
  function srcFor(n) {
    return n ? assetUrl(n.video, "video") : "";
  }
  function validNode(id) { return Boolean(node(id)); }
  function route(from, to) {
    if (!from || !to) return [];
    if (from === to) return [from];
    var graph = edges(), todo = [[from]], seen = {};
    while (todo.length) {
      var path = todo.shift(), tail = path[path.length - 1];
      if (tail === to) return path;
      if (seen[tail]) continue; seen[tail] = true;
      Object.keys(graph[tail] || {}).forEach(function (next) { if (!seen[next]) todo.push(path.concat([next])); });
    }
    return [];
  }
  function chooseThemeNodes() {
    var list = theme && theme.config && theme.config.flow && theme.config.flow.nodes || [];
    var waits = list.filter(function (n) { return String(n.role || "").toLowerCase() === "wait"; });
    var phoneRoles = { phone: true, phone_only: true, both: true };
    var phones = list.filter(function (n) { return phoneRoles[String(n.role || "").toLowerCase()]; });
    idleNode = (waits.filter(function (n) { return !!n.loop; })[0] || waits[0] || list.filter(function (n) { return !!n.loop; })[0] || list[0] || {}).id || "";
    phoneNode = (phones.filter(function (n) { return !!n.loop; })[0] || phones[0] || {}).id || "";
    if (!phoneNode && idleNode) {
      var possible = list.filter(function (n) { return n.id !== idleNode && route(idleNode, n.id).length > 1; });
      phoneNode = (possible.filter(function (n) { return !!n.loop; })[0] || possible[0] || {}).id || "";
    }
  }
  function phoneSequence(pickupId) {
    if (!phoneNode || !idleNode) return [];
    if (pickupId && validNode(pickupId)) {
      var fromPickup = route(pickupId, phoneNode);
      if (fromPickup.length) return fromPickup;
    }
    var path = route(idleNode, phoneNode);
    return path.length > 1 ? path.slice(1) : path;
  }
  function idleSequence(putdownId) {
    if (!idleNode) return [];
    if (putdownId && validNode(putdownId)) {
      var toIdle = route(putdownId, idleNode);
      if (toIdle.length) return toIdle;
    }
    if (phoneNode) {
      var path = route(phoneNode, idleNode);
      if (path.length > 1) return path.slice(1);
    }
    return [idleNode];
  }
  function phaseWaypoints(phase, requested, flowGoto) {
    if (phase === "received" || phase === "thinking") {
      return requested ? [requested].concat(phoneSequence(requested).slice(1)) : phoneSequence();
    }
    if (phase === "replying" || phase === "sent") return requested ? [requested].concat(idleSequence(requested).slice(1)) : idleSequence();
    if (phase === "idle" || phase === "error") return requested ? [requested].concat(idleSequence(requested).slice(1)) : [idleNode];
    return flowGoto && requested ? [requested] : (requested ? [requested] : [idleNode]);
  }
  function showText(message) {
    var text = typeof message.bubble_text === "string" ? message.bubble_text.trim().slice(0, 160) : "";
    var ttl = Math.min(3000, Math.max(0, Number(message.ttl_ms) || 0));
    if (!showBubble || !text || !ttl) { bubble.classList.add("hidden"); clearTimeout(bubbleExpiry); return; }
    bubble.textContent = text;
    bubble.classList.remove("hidden");
    clearTimeout(bubbleExpiry);
    bubbleExpiry = setTimeout(function () { bubble.classList.add("hidden"); }, ttl);
  }
  function pause(video) { video.onended = null; video.onerror = null; try { video.pause(); } catch (_) {} }
  function clearPickupPreload(resetVideo) {
    var preload = pickupPreload;
    pickupPreload = null;
    if (!resetVideo || !preload || preload.video === videos[active]) return;
    pause(preload.video);
    preload.video.removeAttribute("src");
    preload.video.load();
  }
  function preloadPickup() {
    var connection = navigator.connection;
    if (connection && connection.saveData) { clearPickupPreload(true); return; }
    if (!idleNode || !phoneNode || currentNode !== idleNode || pendingNode || queue.length) return;
    var path = route(idleNode, phoneNode), firstPickup = path.length > 1 ? path[1] : "";
    var target = node(firstPickup), file = srcFor(target);
    if (!target || !file) return;
    var video = videos[1 - active];
    if (pickupPreload && pickupPreload.video === video && pickupPreload.nodeId === firstPickup
        && pickupPreload.file === file && pickupPreload.themeEpoch === themeEpoch
        && video.src === file) return;
    clearPickupPreload(true);
    pause(video);
    video.style.opacity = "0";
    video.preload = "auto";
    video.muted = true;
    video.playsInline = true;
    video.loop = Boolean(target.loop);
    video.src = file;
    video.load();
    pickupPreload = { video: video, nodeId: firstPickup, file: file, themeEpoch: themeEpoch };
  }
  function queueAfterLoop(id) {
    var n = node(id), seconds = Number(n && n.duration || theme && theme.config && theme.config.durations && theme.config.durations[id]) || 8;
    clearTimeout(transitionTimer);
    transitionTimer = setTimeout(advance, Math.max(250, seconds * 1000));
  }
  function playNode(id) {
    var n = node(id), file = srcFor(n);
    if (!n || !file) return Promise.resolve(false);
    if (id === currentNode && !videos[active].paused && videos[active].readyState >= 2) return Promise.resolve(true);
    if (pendingNode === id) return Promise.resolve(true);
    pendingNode = id;
    var nextIndex = 1 - active, next = videos[nextIndex], prev = videos[active];
    var reusePreload = Boolean(pickupPreload && pickupPreload.video === next && pickupPreload.nodeId === id
      && pickupPreload.file === file && pickupPreload.themeEpoch === themeEpoch && next.src === file
      && !next.error && next.networkState !== HTMLMediaElement.NETWORK_NO_SOURCE);
    if (pickupPreload) {
      var previousPreload = pickupPreload;
      pickupPreload = null;
      if (!reusePreload && previousPreload.video === next) {
        pause(next); next.removeAttribute("src"); next.load();
      }
    }
    pause(next); next.style.opacity = "0"; next.loop = Boolean(n.loop); next.muted = true; next.playsInline = true;
    next.preload = "auto";
    if (!reusePreload) next.src = file;
    var generation = ++startedAt;
    return new Promise(function (resolve) {
      var started = false, settled = false, readyTimer = 0;
      function fail() {
        if (settled) return; settled = true; clearTimeout(readyTimer);
        next.removeEventListener("canplay", ready); next.removeEventListener("loadeddata", ready);
        if (generation === startedAt) {
          if (pendingNode === id) pendingNode = "";
          next.onerror = null;
          if (next !== videos[active]) pause(next);
        }
        resolve(false);
      }
      function ready() {
        if (started || settled) return;
        if (generation !== startedAt) { fail(); return; }
        started = true;
        next.removeEventListener("canplay", ready); next.removeEventListener("loadeddata", ready);
        next.play().then(function () {
          if (settled) return;
          if (generation !== startedAt) { fail(); return; }
          settled = true; clearTimeout(readyTimer); next.onerror = null;
          next.style.opacity = "1"; prev.style.opacity = "0"; active = nextIndex; currentNode = id; pendingNode = "";
          setTimeout(function () {
            if (generation === startedAt && prev !== videos[active]) {
              pause(prev); prev.removeAttribute("src"); prev.load();
              if (id === idleNode) preloadPickup();
            }
          }, 450);
          next.onended = function () { advance(); };
          if (cover && !cover.classList.contains("cover-hidden")) {
            var revealed = false, showVideo = function () {
              if (revealed || generation !== startedAt) return;
              revealed = true; cover.classList.add("cover-hidden");
              if (embedded && window.parent !== window) {
                window.parent.postMessage({ type: "dafeiyu-live-ready" }, location.origin);
              }
            };
            if (next.requestVideoFrameCallback) next.requestVideoFrameCallback(showVideo); else setTimeout(showVideo, 250);
            setTimeout(showVideo, 1200);
          }
          resolve(true);
        }).catch(fail);
      }
      next.addEventListener("canplay", ready); next.addEventListener("loadeddata", ready); next.onerror = fail;
      readyTimer = setTimeout(ready, 5000);
      if (reusePreload && next.readyState >= 2) Promise.resolve().then(ready);
      else if (!reusePreload) next.load();
    });
  }
  function advance() {
    if (!queue.length) return;
    var next = queue.shift(), graph = edges();
    if (!graph[currentNode] || !graph[currentNode][next]) { queue = []; return; }
    playNode(next).then(function (ok) { if (!ok) { queue = []; return; } if (queue.length && node(next).loop) queueAfterLoop(next); });
  }
  function enqueuePath(waypoints) {
    if (!waypoints || !waypoints.length || waypoints.some(function (id) { return !validNode(id); })) return false;
    var hadQueue = queue.length > 0;
    var anchor = hadQueue ? queue[queue.length - 1] : (pendingNode || currentNode || idleNode);
    if (!anchor) anchor = waypoints[0];
    var additions = [], cursor = anchor;
    for (var i = 0; i < waypoints.length; i++) {
      var segment = route(cursor, waypoints[i]);
      if (!segment.length) return false;
      additions = additions.concat(segment.slice(1)); cursor = waypoints[i];
    }
    if (!additions.length) return true;
    queue = (hadQueue ? queue : []).concat(additions);
    clearTimeout(transitionTimer);
    if (pendingNode) return true;
    if (!currentNode) {
      queue.shift();
      playNode(queue.length ? queue[0] : waypoints[0]).then(function (ok) { if (!ok) queue = []; });
      return true;
    }
    if (currentNode && node(currentNode) && node(currentNode).loop && currentNode !== idleNode) {
      advance();
      return true;
    }
    if (currentNode && node(currentNode) && !node(currentNode).loop) {
      if (videos[active].ended) advance();
      return true;
    }
    advance();
    return true;
  }
  function enqueueOrders(orders, eventKey) {
    if (!Array.isArray(orders) || !orders.length) return false;
    orders = orders.map(function (order) { return typeof order === "string" ? order : order && order.node_id; });
    if (orders.some(function (id) { return typeof id !== "string" || !validNode(id); })) return false;
    var signature = orders.join(","), now = Date.now();
    if (eventKey ? eventKey === lastOrderEvent : signature === lastOrders && now - lastOrdersAt < 1500) return true;
    var hadQueue = queue.length > 0;
    var anchor = hadQueue ? queue[queue.length - 1] : (pendingNode || currentNode || idleNode);
    var path = orders.slice();
    if (path[0] === anchor) path.shift();
    var cursor = anchor;
    for (var i = 0; i < path.length; i++) {
      if (!edges()[cursor] || !edges()[cursor][path[i]]) return false;
      cursor = path[i];
    }
    if (!path.length) return true;
    lastOrders = signature; lastOrdersAt = now; lastOrderEvent = eventKey;
    queue = (hadQueue ? queue : []).concat(path);
    clearTimeout(transitionTimer);
    if (pendingNode) return true;
    if (!currentNode) {
      var initial = queue.shift();
      playNode(initial).then(function (ok) { if (!ok) queue = []; });
    } else if (node(currentNode) && node(currentNode).loop) advance();
    else if (node(currentNode) && videos[active].ended) advance();
    return true;
  }
  function apply(message, isSnapshot) {
    if (!message || typeof message !== "object") return;
    if (message.type !== "state" && message.type !== "flow_goto" && message.type !== "bubble" && message.type !== "config") return;
    if (!Number.isInteger(message.seq) || message.seq < 0 || !Number.isFinite(Number(message.ts)) || !allowedSources[message.source] || !allowedPhases[message.phase] || typeof message.node_id !== "string" || !Number.isFinite(Number(message.ttl_ms)) || Number(message.ttl_ms) < 0 || typeof message.bubble_text !== "string") return;
    if (!(isSnapshot && firstStateOnSocket) && message.seq <= lastSeq) return;
    lastSeq = message.seq; firstStateOnSocket = false;
    if (message.type === "config") {
      var config = message.config || message;
      if (typeof config.theme_url === "string" && config.theme_url) loadTheme(config.theme_url);
      if (bubbleOverride === null && typeof config.show_bubble === "boolean") { showBubble = config.show_bubble; if (!showBubble) bubble.classList.add("hidden"); }
      if (Number.isFinite(Number(config.idle_timeout_ms))) idleDelay = Math.max(0, Number(config.idle_timeout_ms));
      return;
    }
    if (message.type === "bubble") { showText(message); return; }
    showText(message);
    var phase = message.phase;
    var requested = message.node_id && validNode(message.node_id) ? message.node_id : "";
    if (message.node_id && !requested) return;
    if (Array.isArray(message.orders)) {
      var eventKey = Number.isFinite(message.event_at) && message.event_at > 0
        ? [message.source, phase, message.event_at].join(":") : "";
      if (enqueueOrders(message.orders, eventKey)) {
        clearTimeout(expiry);
        if (phase !== "idle" && Number(message.ttl_ms) > 0) expiry = setTimeout(function () { queue = []; enqueuePath([idleNode]); }, Number(message.ttl_ms));
        return;
      }
    }
    var targetPath = phaseWaypoints(phase, requested, message.type === "flow_goto");
    if (!targetPath.length || targetPath.some(function (id) { return !validNode(id); })) return;
    var signature = phase + ":" + targetPath.join(",");
    var now = Date.now();
    if (signature === lastTransition && now - lastTransitionAt < 900) return;
    lastTransition = signature; lastTransitionAt = now;
    clearTimeout(expiry);
    if (phase !== "idle" && Number(message.ttl_ms) > 0) expiry = setTimeout(function () { queue = []; enqueuePath([idleNode]); }, Number(message.ttl_ms));
    enqueuePath(targetPath);
  }
  function connect() {
    if (embedded) {
      if (socket || !window.EventSource) return;
      var events = new EventSource("/api/house/live-events");
      socket = events;
      events.onopen = function () { firstStateOnSocket = true; lastSeq = -1; };
      events.onmessage = receive;
      return;
    }
    if (socket || !window.WebSocket) return;
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    var ws;
    try { ws = new WebSocket(proto + "//" + location.host + "/ws"); } catch (_) { scheduleReconnect(); return; }
    socket = ws; firstStateOnSocket = true; lastSeq = -1;
    ws.onmessage = receive;
    ws.onclose = function () { if (socket === ws) socket = null; scheduleReconnect(); };
    ws.onerror = function () {};
  }
  function receive(event) {
    var msg;
    try { msg = JSON.parse(event.data); } catch (_) { return; }
    if (msg && typeof msg === "object") apply(msg, msg.type === "state" && firstStateOnSocket);
  }
  function scheduleReconnect() { clearTimeout(reconnect); reconnect = setTimeout(connect, 1500); }
  function loadTheme(url) {
    var absolute = safeThemeUrl(url);
    if (!absolute) return Promise.resolve(false);
    if (theme && themeBase && new URL("theme.json", themeBase).href === absolute.href) return Promise.resolve(true);
    return fetch(absolute.href, { cache: "no-store" }).then(function (r) { if (!r.ok) throw new Error("theme HTTP " + r.status); return r.json(); }).then(function (data) {
      if (!data || !data.config || !data.config.videos || !data.config.flow || !Array.isArray(data.config.flow.nodes) || !Array.isArray(data.config.flow.edges)) throw new Error("unsupported theme");
      startedAt++;
      clearTimeout(expiry);
      clearTimeout(transitionTimer);
      themeEpoch++;
      clearPickupPreload(false);
      theme = data; themeBase = new URL("./", absolute.href); chooseThemeNodes();
      var coverFile = assetUrl(data.meta && data.meta.cover, "cover");
      if (cover && coverFile) { cover.src = coverFile; cover.classList.remove("cover-hidden"); }
      videos.forEach(function (video) { pause(video); video.removeAttribute("src"); video.load(); video.style.opacity = "0"; });
      active = 0; currentNode = ""; pendingNode = ""; queue = [];
      lastOrders = ""; lastOrdersAt = 0; lastOrderEvent = "";
      if (idleNode) playNode(idleNode).then(function (ok) { if (ok && queue.length) { if (node(idleNode).loop) queueAfterLoop(idleNode); else advance(); } });
      return true;
    }).catch(function (error) { console.error("Fish House theme load failed", error); return false; });
  }
  bubbleOverride = new URLSearchParams(location.search).get("bubble");
  if (bubbleOverride === "0") showBubble = false;
  if (bubbleOverride === "1") showBubble = true;
  window.__fishHouseLive = { apply: apply, playNode: playNode, getState: function () { return { currentNode: currentNode, pendingNode: pendingNode, lastSeq: lastSeq, queued: queue.slice(), themeId: theme && theme.meta && theme.meta.id, idleNode: idleNode, phoneNode: phoneNode }; } };
  var defaultThemeUrl = "/fish_house/themes/default/theme.json";
  fetch("/fish_house/live.theme.json", { cache: "no-store" }).then(function (r) {
    if (!r.ok) throw new Error("live theme selector HTTP " + r.status);
    return r.json();
  }).then(function (selector) {
    var chosen = selector && safeThemeUrl(selector.theme_url);
    return loadTheme(chosen ? chosen.href : defaultThemeUrl).then(function (ok) { return ok || loadTheme(defaultThemeUrl); });
  }).catch(function () { return loadTheme(defaultThemeUrl); }).then(connect);
})();
