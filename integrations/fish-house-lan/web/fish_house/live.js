(function () {
  "use strict";
  var videos = [document.getElementById("videoA"), document.getElementById("videoB")];
  var bubble = document.getElementById("bubble"), cover = document.getElementById("cover");
  var active = 0, theme, currentNode = "", pendingNode = "", queue = [], socket = null, reconnect = 0;
  var lastSeq = -1, firstStateOnSocket = true, lastTransition = "", lastTransitionAt = 0, lastOrders = "", lastOrdersAt = 0;
  var lastOrderEvent = "";
  var expiry = 0, bubbleExpiry = 0, transitionTimer = 0, phoneExitTimer = 0, startedAt = 0, presentedAt = 0;
  var streamExpiry = 0;
  var serverClock = null;
  var idleDelay = 0, showBubble = true, bubbleOverride = null, themeBase = "", idleNode = "", phoneNode = "";
  var pickupPreload = null, themeEpoch = 0, pendingTheme = null;
  var fadeMs = 450, maxPhoneLoopSeconds = 9, phoneEndWindowMs = 1000, mediaEndGraceMs = 200;
  var phoneMinVisibleMs = 650, phoneCrossfadeMs = 650;
  var embedded = new URLSearchParams(location.search).get("mode") === "home";
  var allowedSources = { qq: true, wechat: true };
  var allowedPhases = { idle: true, received: true, thinking: true, tool: true, replying: true, sent: true, error: true };

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
      return requested ? [requested].concat(phoneSequence(requested).slice(1)) : [phoneNode || idleNode];
    }
    if (phase === "replying") return [phoneNode || idleNode];
    if (phase === "sent") return requested ? [requested].concat(idleSequence(requested).slice(1)) : idleSequence();
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
  function clearPhoneExitTimer() {
    clearTimeout(phoneExitTimer);
    phoneExitTimer = 0;
  }
  function driveQueue() {
    if (!currentNode || pendingNode) return;
    var clip = node(currentNode), video = videos[active];
    if (!clip) return;
    if (!queue.length) {
      clearPhoneExitTimer();
      if (clip.loop && !video.loop) {
        video.loop = true;
        if (video.ended) video.play().catch(function () {});
      }
      return;
    }
    var role = String(clip.role || "").toLowerCase();
    var finishPhoneLoop = currentNode !== idleNode && currentNode === phoneNode
      && (role === "phone" || role === "phone_only")
      && video.duration > 0 && video.duration <= maxPhoneLoopSeconds;
    if (video.ended || (clip.loop && !finishPhoneLoop)) {
      advance();
      return;
    }
    if (!clip.loop || phoneExitTimer) return;
    var remainingMs = Math.max(0, video.duration - video.currentTime) * 1000 / Math.max(0.1, video.playbackRate);
    if (remainingMs <= phoneEndWindowMs) {
      // Use the natural last frame when it is close. The watchdog bounds a
      // stalled decoder or missing ended event to 1.2 seconds.
      video.loop = false;
      phoneExitTimer = setTimeout(function () {
        phoneExitTimer = 0;
        advance();
      }, remainingMs + mediaEndGraceMs);
      return;
    }
    // A reply already queued at pickup still shows the phone pose briefly.
    // A phone clip that has been visible for longer can crossfade at once.
    var visibleDelay = Math.max(0, phoneMinVisibleMs - (performance.now() - presentedAt));
    var connection = navigator.connection;
    var crossfade = connection && connection.saveData ? 0 : phoneCrossfadeMs;
    if (!visibleDelay) {
      advance(crossfade);
      return;
    }
    phoneExitTimer = setTimeout(function () {
      phoneExitTimer = 0;
      advance(crossfade);
    }, visibleDelay);
  }
  function playNode(id, hasPreviousFrame, crossfadeMs) {
    var n = node(id), file = srcFor(n);
    if (!n || !file) return Promise.resolve(false);
    if (id === currentNode && !videos[active].paused && videos[active].readyState >= 2) return Promise.resolve(true);
    if (pendingNode === id) return Promise.resolve(true);
    clearPhoneExitTimer();
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
    if (currentNode || hasPreviousFrame) {
      // A new request can reuse the back layer before the previous fade ends.
      // Finish revealing the last presented frame before reloading that layer.
      prev.style.transition = "none";
      prev.style.opacity = "1";
    }
    pause(next);
    next.style.transition = "none";
    next.style.opacity = "0";
    next.style.zIndex = "1";
    next.loop = Boolean(n.loop); next.muted = true; next.playsInline = true;
    next.preload = "auto";
    if (!reusePreload) next.src = file;
    var generation = ++startedAt;
    return new Promise(function (resolve) {
      var started = false, settled = false, deadline = 0, videoFrame = null, initialTime = 0;
      function cleanup() {
        clearTimeout(deadline);
        next.removeEventListener("canplay", ready);
        next.removeEventListener("loadeddata", ready);
        next.removeEventListener("timeupdate", advanced);
        if (videoFrame !== null && next.cancelVideoFrameCallback) next.cancelVideoFrameCallback(videoFrame);
      }
      function fail() {
        if (settled) return;
        settled = true;
        cleanup();
        if (generation === startedAt) {
          if (pendingNode === id) pendingNode = "";
          pause(next);
          next.removeAttribute("src");
          next.load();
        }
        resolve(false);
      }
      function presented() {
        if (settled) return;
        if (generation !== startedAt) { fail(); return; }
        settled = true;
        cleanup();
        next.onerror = function () {
          if (generation === startedAt && videos[active] === next) advance();
        };
        var firstFrame = !currentNode;
        prev.style.zIndex = "1";
        next.style.zIndex = "2";
        next.style.transition = crossfadeMs ? "opacity " + crossfadeMs + "ms ease-in-out" : "";
        next.style.opacity = "1";
        active = nextIndex; currentNode = id; pendingNode = "";
        presentedAt = performance.now();
        // Keep the cover or previous image beneath the incoming video for the full fade.
        setTimeout(function () {
          if (generation !== startedAt) return;
          prev.style.opacity = "0";
          pause(prev); prev.removeAttribute("src"); prev.load();
          if (cover) cover.classList.add("cover-hidden");
          if (id === idleNode) preloadPickup();
        }, crossfadeMs ? crossfadeMs + 50 : fadeMs);
        next.onended = driveQueue;
        if (firstFrame && embedded && window.parent !== window) {
          window.parent.postMessage({ type: "dafeiyu-live-ready" }, location.origin);
        }
        resolve(true);
      }
      function advanced() {
        // Chrome 66 and older WebViews lack the compositor callback.
        if (!next.paused && next.readyState >= 2 && next.videoWidth > 0 && next.currentTime > initialTime) presented();
      }
      function ready() {
        if (started || settled) return;
        if (generation !== startedAt) { fail(); return; }
        started = true;
        initialTime = next.currentTime;
        next.removeEventListener("canplay", ready); next.removeEventListener("loadeddata", ready);
        next.play().then(function () {
          if (settled) return;
          if (generation !== startedAt) { fail(); return; }
          if (next.requestVideoFrameCallback) {
            videoFrame = next.requestVideoFrameCallback(presented);
          } else {
            next.addEventListener("timeupdate", advanced);
            advanced();
          }
        }).catch(fail);
      }
      next.addEventListener("canplay", ready); next.addEventListener("loadeddata", ready); next.onerror = fail;
      deadline = setTimeout(fail, 8000);
      if (reusePreload && next.readyState >= 2) Promise.resolve().then(ready);
      else if (!reusePreload) next.load();
    });
  }
  function advance(crossfadeMs) {
    if (!queue.length || pendingNode) return;
    var next = queue.shift(), graph = edges();
    if (!graph[currentNode] || !graph[currentNode][next]) { queue = []; driveQueue(); return; }
    var epoch = themeEpoch;
    playNode(next, false, crossfadeMs).then(function (ok) {
      if (epoch !== themeEpoch) return;
      if (!ok) queue = [];
      driveQueue();
    });
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
      var epoch = themeEpoch;
      queue.shift();
      playNode(queue.length ? queue[0] : waypoints[0]).then(function (ok) { if (!ok && epoch === themeEpoch) queue = []; });
      return true;
    }
    driveQueue();
    return true;
  }
  function enqueueOrders(orders, eventKey) {
    if (!Array.isArray(orders) || !orders.length) return false;
    orders = orders.map(function (order) { return typeof order === "string" ? order : order && order.node_id; });
    if (orders.some(function (id) { return typeof id !== "string" || !validNode(id); })) return false;
    var signature = orders.join(","), now = performance.now();
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
      var epoch = themeEpoch;
      var initial = queue.shift();
      playNode(initial).then(function (ok) { if (!ok && epoch === themeEpoch) queue = []; });
    } else driveQueue();
    return true;
  }
  function observeServerClock(header, requestAt) {
    var serverDate = Date.parse(header), observedAt = performance.now();
    var roundTrip = observedAt - requestAt;
    if (!Number.isFinite(serverDate) || roundTrip > 5000) return;
    // HTTP Date has whole-second precision. Keep the local reading if it
    // falls within the server's possible time range; otherwise use its middle.
    var latest = serverDate + 1000 + roundTrip;
    var local = Date.now();
    serverClock = {
      observedAt: observedAt,
      unixMs: local >= serverDate && local <= latest ? local : (serverDate + latest) / 2
    };
  }
  function serverTimeAt(receivedAt, frameTime) {
    if (!serverClock) return Date.now();
    var estimate = serverClock.unixMs + receivedAt - serverClock.observedAt;
    return Math.max(estimate, Number(frameTime) * 1000);
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
    var receivedAt = performance.now();
    if (Number.isFinite(message.expires_at) && message.expires_at > 0) {
      var remaining = Math.min(Number(message.ttl_ms), message.expires_at * 1000 - serverTimeAt(receivedAt, message.ts));
      if (remaining <= 0) return;
      message = Object.assign({}, message, { ttl_ms: remaining });
    }
    showText(message);
    if (message.type === "bubble") return;
    clearTimeout(streamExpiry);
    streamExpiry = 0;
    if (pendingTheme) {
      // The next server frame may already refer to the requested theme.
      // Keep only the latest playback state and preserve its original lifetime.
      pendingTheme.frame = message;
      pendingTheme.frameAt = receivedAt;
      return;
    }
    applyPlayback(message);
  }
  function expireActivity() {
    queue = [];
    enqueuePath([idleNode]);
    driveQueue();
  }
  function applyPlayback(message) {
    var phase = message.phase;
    var requested = message.node_id && validNode(message.node_id) ? message.node_id : "";
    if (message.node_id && !requested) return;
    if (Array.isArray(message.orders)) {
      var eventKey = Number.isFinite(message.event_at) && message.event_at > 0
        ? [message.source, phase, message.event_at].join(":") : "";
      if (enqueueOrders(message.orders, eventKey)) {
        clearTimeout(expiry);
        if (phase !== "idle" && Number(message.ttl_ms) > 0) expiry = setTimeout(expireActivity, Number(message.ttl_ms));
        return;
      }
    }
    var targetPath = phaseWaypoints(phase, requested, message.type === "flow_goto");
    if (!targetPath.length || targetPath.some(function (id) { return !validNode(id); })) return;
    var signature = phase + ":" + targetPath.join(",");
    var now = performance.now();
    if (signature === lastTransition && now - lastTransitionAt < 900) return;
    lastTransition = signature; lastTransitionAt = now;
    clearTimeout(expiry);
    if (phase !== "idle" && Number(message.ttl_ms) > 0) expiry = setTimeout(expireActivity, Number(message.ttl_ms));
    enqueuePath(targetPath);
  }
  function resetStream() {
    firstStateOnSocket = true;
    lastSeq = -1;
    if (pendingTheme) pendingTheme.frame = null;
  }
  function streamUnavailable() {
    if (pendingTheme) pendingTheme.frame = null;
    if (!streamExpiry) streamExpiry = setTimeout(function () {
      streamExpiry = 0;
      clearTimeout(expiry);
      expireActivity();
    }, 4500);
  }
  function connect() {
    if (embedded) {
      if (socket || !window.EventSource) return;
      var events = new EventSource("/api/house/live-events");
      socket = events;
      events.onopen = resetStream;
      events.onerror = streamUnavailable;
      events.onmessage = receive;
      return;
    }
    if (socket || !window.WebSocket) return;
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    var ws;
    try { ws = new WebSocket(proto + "//" + location.host + "/ws"); } catch (_) { scheduleReconnect(); return; }
    socket = ws; resetStream();
    ws.onmessage = receive;
    ws.onclose = function () {
      if (socket !== ws) return;
      socket = null;
      streamUnavailable();
      scheduleReconnect();
    };
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
    if (pendingTheme && pendingTheme.url === absolute.href) return pendingTheme.promise;
    pendingTheme = null;
    if (theme && themeBase && new URL("theme.json", themeBase).href === absolute.href) return Promise.resolve(true);
    var request = { url: absolute.href, frame: null, frameAt: 0, promise: null };
    pendingTheme = request;
    request.promise = fetch(absolute.href, { cache: "no-store" }).then(function (r) { if (!r.ok) throw new Error("theme HTTP " + r.status); return r.json(); }).then(function (data) {
      if (pendingTheme !== request) return false;
      if (!data || !data.config || !data.config.videos || !data.config.flow || !Array.isArray(data.config.flow.nodes) || !Array.isArray(data.config.flow.edges)) throw new Error("unsupported theme");
      pendingTheme = null;
      startedAt++;
      clearTimeout(expiry);
      clearTimeout(transitionTimer);
      themeEpoch++;
      clearPhoneExitTimer();
      clearPickupPreload(false);
      theme = data; themeBase = new URL("./", absolute.href); chooseThemeNodes();
      var previous = videos[active];
      var keepFrame = previous.style.opacity === "1" && previous.readyState >= 2
        && previous.videoWidth > 0 && Boolean(srcFor(node(idleNode)));
      var coverFile = assetUrl(data.meta && data.meta.cover, "cover");
      if (cover && coverFile) {
        cover.src = coverFile;
        if (!keepFrame) cover.classList.remove("cover-hidden");
      }
      videos.forEach(function (video) {
        if (keepFrame && video === previous) {
          video.onended = video.onerror = null;
          return;
        }
        pause(video); video.removeAttribute("src"); video.load(); video.style.opacity = "0";
      });
      if (!keepFrame) active = 0;
      currentNode = ""; pendingNode = ""; queue = [];
      lastOrders = ""; lastOrdersAt = 0; lastOrderEvent = "";
      lastTransition = ""; lastTransitionAt = 0;
      var frame = request.frame, ttl = frame ? Number(frame.ttl_ms) : 0;
      var remaining = ttl - (performance.now() - request.frameAt);
      var resume = frame && (ttl === 0 || remaining > 0);
      var epoch = themeEpoch;
      if (idleNode) playNode(idleNode, keepFrame).then(function (ok) {
        if (epoch !== themeEpoch) return;
        if (!ok) {
          if (keepFrame) {
            if (cover) cover.classList.remove("cover-hidden");
            pause(previous); previous.removeAttribute("src"); previous.load(); previous.style.opacity = "0";
          }
          return;
        }
        if (!queue.length) return;
        if (keepFrame && resume) transitionTimer = setTimeout(driveQueue, fadeMs);
        else driveQueue();
      });
      if (resume) applyPlayback(Object.assign({}, frame, { ttl_ms: Math.max(0, remaining) }));
      return true;
    }).catch(function (error) {
      if (pendingTheme === request) {
        pendingTheme = null;
        console.error("Fish House theme load failed", error);
      }
      return false;
    });
    return request.promise;
  }
  bubbleOverride = new URLSearchParams(location.search).get("bubble");
  if (bubbleOverride === "0") showBubble = false;
  if (bubbleOverride === "1") showBubble = true;
  window.__fishHouseLive = { apply: apply, playNode: playNode, getState: function () { return { currentNode: currentNode, pendingNode: pendingNode, lastSeq: lastSeq, queued: queue.slice(), themeId: theme && theme.meta && theme.meta.id, idleNode: idleNode, phoneNode: phoneNode }; } };
  var defaultThemeUrl = "/fish_house/themes/default/theme.json";
  var selectorRequestedAt = performance.now();
  fetch("/fish_house/live.theme.json", { cache: "no-store" }).then(function (r) {
    if (!r.ok) throw new Error("live theme selector HTTP " + r.status);
    observeServerClock(r.headers.get("Date"), selectorRequestedAt);
    return r.json();
  }).then(function (selector) {
    var chosen = selector && safeThemeUrl(selector.theme_url);
    return loadTheme(chosen ? chosen.href : defaultThemeUrl).then(function (ok) { return ok || loadTheme(defaultThemeUrl); });
  }).catch(function () { return loadTheme(defaultThemeUrl); }).then(connect);
})();
