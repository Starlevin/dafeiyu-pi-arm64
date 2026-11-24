/* DaFeiYu Home R5.2 — Dual-Backend Stage Renderer (2D Canvas Default + WebGL1 Benchmark + Legacy MIT Video)
 * Chrome 66 ES5 + Firefox + Edge compatible.
 *
 * Upgrades in R5.2:
 *   - 3 user-facing performance modes: auto / high / eco (+ opt-in WebGL1 benchmark)
 *   - Auto time-of-day room lighting (day 06-16 / sunset 17-19 / night 19:30-05:59) + manual themes
 *   - Subtle window sheer-curtain breeze shimmer + floating dust motes + state-reactive desk glow
 *   - Interactive stage hit-testing & tap sparkle bursts (head-pat / wave micro-interaction)
 *   - H.264 codec capability check & graceful degradation when video decoding is unsupported
 *   - Zero-blank-frame morph continuity verification counter
 */
(function (root) {
  "use strict";

  var THEMES = {
    auto: {
      id: "auto",
      label: "随时间自动"
    },
    day: {
      id: "day",
      label: "晴朗晨光",
      bgTint: "rgba(255, 252, 244, 0.0)",
      beamColor: "rgba(255, 250, 232, 0.16)",
      moteColor: "rgba(255, 253, 240, 0.72)",
      shadowColor: "rgba(42, 78, 118, 0.24)"
    },
    sunset: {
      id: "sunset",
      label: "暖阳午后",
      bgTint: "rgba(255, 178, 112, 0.17)",
      beamColor: "rgba(255, 210, 148, 0.22)",
      moteColor: "rgba(255, 235, 195, 0.78)",
      shadowColor: "rgba(104, 62, 52, 0.26)"
    },
    night: {
      id: "night",
      label: "星月夜色",
      bgTint: "rgba(24, 42, 86, 0.42)",
      beamColor: "rgba(165, 210, 255, 0.11)",
      moteColor: "rgba(205, 232, 255, 0.76)",
      shadowColor: "rgba(12, 24, 52, 0.42)"
    },
    video: {
      id: "video",
      label: "原版视频屋",
      bgTint: "rgba(0,0,0,0)",
      beamColor: "rgba(0,0,0,0)",
      moteColor: "rgba(0,0,0,0)",
      shadowColor: "rgba(0,0,0,0)"
    }
  };

  function nowMs() {
    return (window.performance && performance.now) ? performance.now() : Date.now();
  }

  function resolveClockTheme() {
    var d = new Date();
    var hm = d.getHours() * 60 + d.getMinutes();
    if (hm >= 360 && hm < 1020) return "day";     // 06:00 - 16:59
    if (hm >= 1020 && hm < 1170) return "sunset"; // 17:00 - 19:29
    return "night";                               // 19:30 - 05:59
  }

  function easeInOutCubic(t) {
    return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }

  function easeOutQuad(t) {
    return 1 - (1 - t) * (1 - t);
  }

  // ---- Optional WebGL1 Renderer Backend (Controlled Performance Experiment) ----
  function createWebGLBackend(canvas) {
    var gl = null;
    try {
      gl = canvas.getContext("webgl", { alpha: false, antialias: true, powerPreference: "default" }) ||
           canvas.getContext("experimental-webgl", { alpha: false, antialias: true });
    } catch (e) { gl = null; }
    if (!gl) return null;

    var vsSrc =
      "attribute vec2 a_pos;" +
      "attribute vec2 a_uv;" +
      "varying vec2 v_uv;" +
      "void main() {" +
      "  v_uv = a_uv;" +
      "  gl_Position = vec4(a_pos, 0.0, 1.0);" +
      "}";

    var fsSrc =
      "precision mediump float;" +
      "varying vec2 v_uv;" +
      "uniform sampler2D u_tex;" +
      "uniform float u_alpha;" +
      "uniform vec4 u_tint;" +
      "void main() {" +
      "  vec4 c = texture2D(u_tex, v_uv);" +
      "  vec3 rgb = mix(c.rgb, u_tint.rgb, u_tint.a);" +
      "  gl_FragColor = vec4(rgb, c.a * u_alpha);" +
      "}";

    function compile(type, src) {
      var sh = gl.createShader(type);
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
        gl.deleteShader(sh);
        return null;
      }
      return sh;
    }

    var vs = compile(gl.VERTEX_SHADER, vsSrc);
    var fs = compile(gl.FRAGMENT_SHADER, fsSrc);
    if (!vs || !fs) return null;

    var prog = gl.createProgram();
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return null;

    var posLoc = gl.getAttribLocation(prog, "a_pos");
    var uvLoc = gl.getAttribLocation(prog, "a_uv");
    var alphaLoc = gl.getUniformLocation(prog, "u_alpha");
    var tintLoc = gl.getUniformLocation(prog, "u_tint");
    var texLoc = gl.getUniformLocation(prog, "u_tex");

    var posBuf = gl.createBuffer();
    var uvBuf = gl.createBuffer();
    var texCache = [];

    function getTexture(img) {
      for (var i = 0; i < texCache.length; i++) {
        if (texCache[i].img === img) return texCache[i].tex;
      }
      var tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
      texCache.push({ img: img, tex: tex });
      if (texCache.length > 24) {
        var old = texCache.shift();
        try { gl.deleteTexture(old.tex); } catch (e) {}
      }
      return tex;
    }

    function drawQuad(img, sx, sy, sw, sh, dx, dy, dw, dh, cw, ch, alpha, tint) {
      if (!img || !img.naturalWidth || !img.naturalHeight) return;
      var tex = getTexture(img);
      var x1 = (dx / cw) * 2 - 1;
      var x2 = ((dx + dw) / cw) * 2 - 1;
      var y1 = 1 - (dy / ch) * 2;
      var y2 = 1 - ((dy + dh) / ch) * 2;

      var u1 = sx / img.naturalWidth;
      var u2 = (sx + sw) / img.naturalWidth;
      var v1 = sy / img.naturalHeight;
      var v2 = (sy + sh) / img.naturalHeight;

      gl.useProgram(prog);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.uniform1i(texLoc, 0);
      gl.uniform1f(alphaLoc, alpha);
      if (tint) gl.uniform4f(tintLoc, tint[0], tint[1], tint[2], tint[3]);
      else gl.uniform4f(tintLoc, 0, 0, 0, 0);

      gl.bindBuffer(gl.ARRAY_BUFFER, posBuf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
        x1, y1, x2, y1, x1, y2,
        x1, y2, x2, y1, x2, y2
      ]), gl.STREAM_DRAW);
      gl.enableVertexAttribArray(posLoc);
      gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);

      gl.bindBuffer(gl.ARRAY_BUFFER, uvBuf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
        u1, v1, u2, v1, u1, v2,
        u1, v2, u2, v1, u2, v2
      ]), gl.STREAM_DRAW);
      gl.enableVertexAttribArray(uvLoc);
      gl.vertexAttribPointer(uvLoc, 2, gl.FLOAT, false, 0, 0);

      gl.drawArrays(gl.TRIANGLES, 0, 6);
    }

    return {
      gl: gl,
      begin: function (cw, ch) {
        gl.viewport(0, 0, cw, ch);
        gl.clearColor(0.86, 0.93, 0.98, 1.0);
        gl.clear(gl.COLOR_BUFFER_BIT);
        gl.enable(gl.BLEND);
        gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      },
      drawQuad: drawQuad,
      dispose: function () {
        for (var i = 0; i < texCache.length; i++) {
          try { gl.deleteTexture(texCache[i].tex); } catch (e) {}
        }
        texCache = [];
      }
    };
  }

  function createStageRenderer(opts) {
    var container = opts.container;
    var canvas2d = opts.canvas2d;
    var canvasGl = opts.canvasGl;
    var videoLayerEl = opts.videoLayer;
    var videoA = opts.videoA;
    var videoB = opts.videoB;
    var packLoader = opts.packLoader;
    var onVideoFallback = opts.onVideoFallback || function () {};

    var ctx2d = canvas2d.getContext("2d");
    var webgl = null;
    var rendererMode = "2d"; // "2d" | "webgl"
    var themePref = "day";   // "auto" | "day" | "sunset" | "night" | "video"
    var perfMode = "high";   // "high" | "eco"
    var reducedMotion = false;
    try {
      if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        reducedMotion = true;
      }
    } catch (e) {}

    var currentPack = null;
    var currentState = "idle";
    var frameIndex = 0;
    var frameElapsedMs = 0;
    var lastCharRect = null; // CSS px bounding box for stage tap hit-testing

    var roomImgs = { pc: null, mobile: null };
    var roomLoaded = { pc: false, mobile: false };
    var roomLoadMs = { pc: 0, mobile: 0 };

    // Dual-form morph state
    var morph = null;
    var morphStats = { count: 0, lastMs: 0, maxMs: 0, blankFrames: 0 };

    // Tap sparkle burst pool
    var tapBursts = [];

    // Ambient dust motes (fixed pool)
    var motes = [];
    for (var m = 0; m < 14; m++) {
      motes.push({
        x: (m * 0.073 + 0.04) % 1,
        y: (m * 0.137 + 0.08) % 0.85,
        r: 1.4 + (m % 4) * 0.9,
        vx: 0.004 + (m % 3) * 0.002,
        vy: -0.003 - (m % 4) * 0.0018,
        phase: m * 1.1
      });
    }

    // Legacy video playback state
    var videoActiveIdx = 0;
    var videoToken = 0;
    var videoPack = null;
    var videoClip = "";
    var videoLayers = [videoA, videoB];
    var videoPrevPlaying = false;
    var videoDegraded = false;

    // Loop & telemetry
    var rafId = 0;
    var paused = false;
    var lastTickMs = nowMs();
    var lastDrawMs = 0;
    var viewW = 1280;
    var viewH = 720;
    var dpr = 1;
    var isPortrait = false;
    var firstPaintAt = 0;
    var firstCharAt = 0;
    var bootAt = nowMs();
    var frameTimes = [];
    var frameDrops = 0;
    var pauseCount = 0;
    var webglSupported = false;

    function effectiveThemeId() {
      if (themePref === "auto") return resolveClockTheme();
      return THEMES[themePref] ? themePref : "day";
    }

    function loadRoomImage(kind, useLow) {
      var suffix = useLow ? "-low.webp" : ".webp";
      var base = (kind === "mobile")
        ? "home/assets/rooms/room-mobile-day"
        : "home/assets/rooms/room-pc-day";
      var primaryUrl = packLoader.resolveUrl(base + suffix);
      var fallbackUrl = packLoader.resolveUrl(base + ".jpg");
      packLoader.loadImage(primaryUrl, 10000, function (err, img, dt) {
        if (!err && img) {
          roomImgs[kind] = img;
          roomLoaded[kind] = true;
          roomLoadMs[kind] = dt;
          if (packLoader.warmCacheUrls) packLoader.warmCacheUrls([primaryUrl]);
          if (!firstPaintAt) firstPaintAt = Math.round(nowMs() - bootAt);
        } else {
          packLoader.loadImage(fallbackUrl, 10000, function (err2, img2, dt2) {
            if (!err2 && img2) {
              roomImgs[kind] = img2;
              roomLoaded[kind] = true;
              roomLoadMs[kind] = dt2;
              if (!firstPaintAt) firstPaintAt = Math.round(nowMs() - bootAt);
            }
          });
        }
      });
    }

    function getVisibleViewport() {
      var iw = window.innerWidth || 320;
      var ih = window.innerHeight || 533;
      var cw = (document.documentElement && document.documentElement.clientWidth) ? document.documentElement.clientWidth : iw;
      var ch = (document.documentElement && document.documentElement.clientHeight) ? document.documentElement.clientHeight : ih;
      var w = Math.min(iw, cw);
      var h = Math.min(ih, ch);
      return { w: w, h: h };
    }

    function resize() {
      if (!container) return;
      var vp = getVisibleViewport();
      viewW = vp.w;
      viewH = vp.h;
      container.style.width = viewW + "px";
      container.style.height = viewH + "px";
      isPortrait = viewH > viewW;
      var rawDpr = window.devicePixelRatio || 1;
      if (perfMode === "eco") {
        dpr = 1;
      } else if (isPortrait) {
        dpr = Math.min(rawDpr, 1.5);
      } else {
        dpr = Math.min(rawDpr, 2.0);
      }
      var pw = Math.max(1, Math.round(viewW * dpr));
      var ph = Math.max(1, Math.round(viewH * dpr));

      if (canvas2d.width !== pw || canvas2d.height !== ph) {
        canvas2d.width = pw;
        canvas2d.height = ph;
      }
      if (canvasGl && (canvasGl.width !== pw || canvasGl.height !== ph)) {
        canvasGl.width = pw;
        canvasGl.height = ph;
      }

      var activeKind = isPortrait ? "mobile" : "pc";
      if (!roomImgs[activeKind]) loadRoomImage(activeKind, perfMode === "eco");
    }

    function setRendererMode(mode) {
      frameTimes = [];
      frameDrops = 0;
      lastDrawMs = 0;
      if (mode === "webgl") {
        if (!webgl && canvasGl) webgl = createWebGLBackend(canvasGl);
        if (webgl) {
          webglSupported = true;
          rendererMode = "webgl";
          canvas2d.style.display = "none";
          canvasGl.style.display = "block";
          return true;
        }
        rendererMode = "2d";
        canvas2d.style.display = "block";
        if (canvasGl) canvasGl.style.display = "none";
        return false;
      }
      rendererMode = "2d";
      canvas2d.style.display = "block";
      if (canvasGl) canvasGl.style.display = "none";
      return true;
    }

    function setPerfMode(mode) {
      perfMode = (mode === "eco" || mode === "low") ? "eco" : "high";
      frameTimes = [];
      frameDrops = 0;
      lastDrawMs = 0;
      resize();
      loadRoomImage(isPortrait ? "mobile" : "pc", perfMode === "eco");
    }

    function setTheme(nextTheme) {
      if (!THEMES[nextTheme]) nextTheme = "day";
      var wasVideo = (effectiveThemeId() === "video");
      themePref = nextTheme;
      videoDegraded = false;
      if (effectiveThemeId() === "video") {
        // Check H.264 codec capability BEFORE attempting video playback
        if (packLoader.isH264Supported && !packLoader.isH264Supported()) {
          videoDegraded = true;
          if (videoLayerEl) videoLayerEl.style.display = "none";
          onVideoFallback("当前浏览器不支持 H.264 视频解码，已自动降级为轻量 2D 动画屋");
          return;
        }
        if (videoLayerEl) videoLayerEl.style.display = "block";
        startVideoForState(currentState);
      } else {
        if (videoLayerEl) videoLayerEl.style.display = "none";
        if (wasVideo) stopVideos();
      }
    }

    function spawnTapBurst(cssX, cssY) {
      if (perfMode === "eco" || reducedMotion) return;
      var tNow = nowMs();
      for (var i = 0; i < 8; i++) {
        var ang = (Math.PI * 2 * i) / 8 + (i % 2) * 0.25;
        tapBursts.push({
          x: cssX * dpr,
          y: cssY * dpr,
          vx: Math.cos(ang) * (28 + (i % 3) * 14) * dpr,
          vy: (Math.sin(ang) * 26 - 28) * dpr,
          r: (2.8 + (i % 3) * 1.4) * dpr,
          born: tNow,
          life: 620,
          color: (i % 2 === 0) ? "#7cc9ff" : "#ffe28a"
        });
      }
      while (tapBursts.length > 24) tapBursts.shift();
    }

    function hitTestCharacter(clientX, clientY) {
      if (!lastCharRect || !container) return false;
      var rect = container.getBoundingClientRect();
      var x = clientX - rect.left;
      var y = clientY - rect.top;
      return (x >= lastCharRect.x && x <= lastCharRect.x + lastCharRect.w &&
              y >= lastCharRect.y && y <= lastCharRect.y + lastCharRect.h);
    }

    // ---- Legacy Video Mode (jiuchai/fish-house MIT 7 H.264 clips with fallback) ----
    function stopVideos() {
      videoToken += 1;
      for (var i = 0; i < videoLayers.length; i++) {
        var v = videoLayers[i];
        if (!v) continue;
        try { v.pause(); } catch (e) {}
        v.className = "fh-video";
        v.style.opacity = "0";
      }
    }

    function playVideoClip(url, loop, cb) {
      var nextIdx = 1 - videoActiveIdx;
      var prevIdx = videoActiveIdx;
      var next = videoLayers[nextIdx];
      var prev = videoLayers[prevIdx];
      if (!next || !prev) return;
      var myToken = videoToken;
      var settled = false;

      function done(ok) {
        if (settled) return;
        settled = true;
        next.onloadeddata = null;
        next.onplaying = null;
        next.onerror = null;
        if (!ok || myToken !== videoToken) {
          cb(false, next);
          return;
        }
        next.style.zIndex = "2";
        prev.style.zIndex = "1";
        next.style.opacity = "1";
        next.className = "fh-video show";
        videoActiveIdx = nextIdx;
        setTimeout(function () {
          if (videoActiveIdx !== nextIdx) return;
          prev.className = "fh-video";
          prev.style.opacity = "0";
          try { prev.pause(); } catch (e) {}
        }, 460);
        cb(true, next);
      }

      try { next.pause(); } catch (e) {}
      next.src = url;
      next.loop = !!loop;
      next.muted = true;
      next.playsInline = true;
      next.setAttribute("playsinline", "");
      next.preload = "auto";
      next.style.opacity = "0";
      next.onloadeddata = function () { done(true); };
      next.onplaying = function () { done(true); };
      next.onerror = function () { done(false); };
      next.load();
      try {
        var p = next.play();
        if (p && typeof p["catch"] === "function") {
          p["catch"](function () { done(false); });
        }
      } catch (e) { done(false); }
      setTimeout(function () { if (!settled) done(false); }, 6000);
    }

    function playVideoSeq(seqKeys, idx, myToken) {
      if (myToken !== videoToken || !videoPack || !seqKeys || idx >= seqKeys.length) return;
      var key = seqKeys[idx];
      var meta = videoPack.manifest.clips[key];
      if (!meta) return;
      videoClip = key;
      playVideoClip(packLoader.resolveUrl(meta.src), meta.loop, function (ok, vidEl) {
        if (myToken !== videoToken) return;
        if (!ok) {
          videoDegraded = true;
          if (videoLayerEl) {
            videoLayerEl.style.backgroundImage = "url(" + packLoader.resolveUrl("cover.jpg") + ")";
          }
          onVideoFallback("视频片段解码失败，已自动展示静态兼容海报（可切回蓝白 2D 房间）");
          return;
        }
        if (!meta.loop && vidEl) {
          vidEl.onended = function () {
            if (myToken === videoToken) playVideoSeq(seqKeys, idx + 1, myToken);
          };
        }
      });
    }

    function startVideoForState(st) {
      var myToken = ++videoToken;
      var mapped = (st === "sent") ? "happy" : st;
      if (videoPack) {
        var seq = videoPack.manifest.stateToSequence[mapped] || ["rest"];
        playVideoSeq(seq, 0, myToken);
        return;
      }
      packLoader.loadPack("video", function (err, pack) {
        if (err || !pack || myToken !== videoToken) return;
        videoPack = pack;
        var seq = pack.manifest.stateToSequence[mapped] || ["rest"];
        playVideoSeq(seq, 0, myToken);
      });
    }

    // ---- Character Pack & State Control ----
    function setPack(pack, withMorph) {
      if (!pack) return;
      if (!firstCharAt) firstCharAt = Math.round(nowMs() - bootAt);

      if (withMorph && currentPack && currentPack.manifest.id !== pack.manifest.id) {
        var dur = (perfMode === "eco" || reducedMotion) ? 220 : 620;
        var bubbles = [];
        var count = (perfMode === "eco" || reducedMotion) ? 0 : 16;
        for (var i = 0; i < count; i++) {
          bubbles.push({
            ox: (Math.sin(i * 2.31) * 0.42),
            oy: 0.1 + (i / count) * 0.78,
            r: 4 + (i % 5) * 3.2,
            speed: 0.45 + (i % 4) * 0.18,
            delay: (i % 6) * 0.045,
            hue: (i % 2 === 0) ? "rgba(182, 228, 255, " : "rgba(255, 255, 255, "
          });
        }
        morph = {
          fromPack: currentPack,
          toPack: pack,
          startMs: nowMs(),
          durationMs: dur,
          bubbles: bubbles
        };
        currentPack = pack;
        frameIndex = 0;
        frameElapsedMs = 0;
        morphStats.count += 1;
        return;
      }

      currentPack = pack;
      frameIndex = 0;
      frameElapsedMs = 0;
    }

    function setState(st) {
      if (st === currentState) return;
      currentState = st;
      frameIndex = 0;
      frameElapsedMs = 0;
      if (effectiveThemeId() === "video" && !videoDegraded) {
        startVideoForState(st);
      }
    }

    function getStateSpec(pack, st) {
      if (!pack || !pack.manifest || !pack.manifest.states) return null;
      return pack.manifest.states[st] || pack.manifest.states.happy || pack.manifest.states.idle || null;
    }

    function getFrameForPack(pack, st, fIdx) {
      var spec = getStateSpec(pack, st);
      if (!spec || !spec.frames || !spec.frames.length) return null;
      var fr = spec.frames[fIdx % spec.frames.length];
      var img = (pack.images && pack.images[fr.asset]) ? pack.images[fr.asset] : null;
      if (img && img.naturalWidth > 0) {
        return { img: img, rect: fr.rect, durationMs: fr.durationMs || 400, spec: spec };
      }
      if (pack.posterImg && pack.posterImg.naturalWidth > 0) {
        return {
          img: pack.posterImg,
          rect: [0, 0, pack.posterImg.naturalWidth, pack.posterImg.naturalHeight],
          durationMs: 1000,
          spec: spec
        };
      }
      return null;
    }

    function computeCoverRect(imgW, imgH, cw, ch, camScale) {
      var scale = Math.max(cw / imgW, ch / imgH) * (camScale || 1.0);
      var dw = imgW * scale;
      var dh = imgH * scale;
      return { dx: (cw - dw) * 0.5, dy: (ch - dh) * 0.5, dw: dw, dh: dh };
    }

    function computeCharPlacement(pack, fr, cw, ch, camScale, extraScale, bobY) {
      var anchor = (pack && pack.manifest && pack.manifest.anchor) ? pack.manifest.anchor : {};
      var cfg = isPortrait
        ? (anchor.mobile || { x: 0.5, feetY: 0.815, heightRatio: 0.52 })
        : (anchor.pc || { x: 0.5, feetY: 0.885, heightRatio: 0.64 });
      var rw = fr.rect[2];
      var rh = fr.rect[3];
      var targetH = ch * cfg.heightRatio * (camScale || 1.0) * (extraScale || 1.0);
      var targetW = targetH * (rw / rh);
      var maxW = cw * (isPortrait ? 0.88 : 0.46);
      if (targetW > maxW) {
        var k = maxW / targetW;
        targetW *= k;
        targetH *= k;
      }
      var cx = cw * cfg.x;
      var feetY = ch * cfg.feetY;
      var dx = cx - targetW * 0.5;
      var dy = feetY - targetH + (bobY || 0);
      lastCharRect = { x: dx / dpr, y: dy / dpr, w: targetW / dpr, h: targetH / dpr };
      return { cx: cx, feetY: feetY, dx: dx, dy: dy, dw: targetW, dh: targetH };
    }

    // ---- 2D Canvas Frame Draw ----
    function draw2D(tNow, dtMs) {
      var cw = canvas2d.width;
      var ch = canvas2d.height;
      var effTheme = effectiveThemeId();
      var th = THEMES[effTheme === "video" ? "day" : effTheme] || THEMES.day;

      var camScale = 1.0;
      var morphT = -1;
      if (morph) {
        morphT = (tNow - morph.startMs) / morph.durationMs;
        if (morphT >= 1.0) {
          var elapsedMorph = Math.round(tNow - morph.startMs);
          morphStats.lastMs = elapsedMorph;
          if (elapsedMorph > morphStats.maxMs) morphStats.maxMs = elapsedMorph;
          morph = null;
          morphT = -1;
        } else {
          var bell = Math.sin(morphT * Math.PI);
          camScale = 1.0 + ((perfMode === "eco" || reducedMotion) ? 0.015 : 0.045) * bell;
        }
      }

      ctx2d.clearRect(0, 0, cw, ch);
      var roomImg = roomImgs[isPortrait ? "mobile" : "pc"] || roomImgs.pc || roomImgs.mobile;
      if (roomImg && roomImg.naturalWidth > 0) {
        var rRect = computeCoverRect(roomImg.naturalWidth, roomImg.naturalHeight, cw, ch, camScale);
        ctx2d.drawImage(roomImg, rRect.dx, rRect.dy, rRect.dw, rRect.dh);
      } else {
        var bgGrad = ctx2d.createLinearGradient(0, 0, 0, ch);
        bgGrad.addColorStop(0, "#eaf5ff");
        bgGrad.addColorStop(0.55, "#d8ecfd");
        bgGrad.addColorStop(1, "#c7e1f7");
        ctx2d.fillStyle = bgGrad;
        ctx2d.fillRect(0, 0, cw, ch);
      }

      if (th.id !== "day") {
        ctx2d.fillStyle = th.bgTint;
        ctx2d.fillRect(0, 0, cw, ch);
      }

      if (perfMode !== "eco" && !reducedMotion) {
        drawEnvironment2D(ctx2d, cw, ch, tNow, dtMs, th);
      }

      if (morph && morphT >= 0) {
        drawMorphCharacter2D(ctx2d, cw, ch, tNow, camScale, morphT, th);
      } else if (currentPack) {
        var fr = getFrameForPack(currentPack, currentState, frameIndex);
        if (fr) {
          var bobAmp = (perfMode === "eco" || reducedMotion) ? 0 : (fr.spec.bobAmp || 2.0) * dpr;
          var bobSpeed = fr.spec.bobSpeed || 0.0022;
          var bobY = Math.sin(tNow * bobSpeed) * bobAmp;
          var pl = computeCharPlacement(currentPack, fr, cw, ch, camScale, 1.0, bobY);
          drawContactShadow2D(ctx2d, pl.cx, pl.feetY, pl.dw, bobY, th.shadowColor, 1.0);
          ctx2d.drawImage(fr.img, fr.rect[0], fr.rect[1], fr.rect[2], fr.rect[3],
                          pl.dx, pl.dy, pl.dw, pl.dh);
          if (perfMode !== "eco" && !reducedMotion) {
            drawStateAccents2D(ctx2d, pl, tNow, currentState);
          }
        }
      }

      // Draw interactive tap sparkle bursts
      if (tapBursts.length > 0 && perfMode !== "eco") {
        drawTapBursts2D(ctx2d, tNow);
      }
    }

    function drawTapBursts2D(ctx, tNow) {
      ctx.save();
      for (var i = tapBursts.length - 1; i >= 0; i--) {
        var p = tapBursts[i];
        var age = (tNow - p.born) / p.life;
        if (age >= 1) {
          tapBursts.splice(i, 1);
          continue;
        }
        var px = p.x + p.vx * age;
        var py = p.y + p.vy * age + 18 * dpr * age * age;
        ctx.globalAlpha = (1 - age) * 0.9;
        ctx.fillStyle = p.color;
        ctx.beginPath();
        ctx.arc(px, py, p.r * (1 - age * 0.4), 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
    }

    function drawEnvironment2D(ctx, cw, ch, tNow, dtMs, th) {
      // 1. Diagonal soft sunbeam / moonlight shafts from window
      var breathe = 0.85 + 0.15 * Math.sin(tNow * 0.0008);
      ctx.save();
      ctx.globalAlpha = breathe;
      var beamX = isPortrait ? cw * 0.65 : cw * 0.24;
      var grad = ctx.createLinearGradient(beamX, 0, isPortrait ? cw * 0.25 : cw * 0.72, ch * 0.85);
      grad.addColorStop(0, th.beamColor);
      grad.addColorStop(0.65, "rgba(255, 255, 255, 0.04)");
      grad.addColorStop(1, "rgba(255, 255, 255, 0.0)");
      ctx.fillStyle = grad;
      ctx.beginPath();
      if (isPortrait) {
        ctx.moveTo(cw * 0.42, 0);
        ctx.lineTo(cw * 0.96, 0);
        ctx.lineTo(cw * 0.62, ch * 0.88);
        ctx.lineTo(cw * 0.06, ch * 0.74);
      } else {
        ctx.moveTo(cw * 0.12, 0);
        ctx.lineTo(cw * 0.45, 0);
        ctx.lineTo(cw * 0.78, ch * 0.95);
        ctx.lineTo(cw * 0.34, ch * 0.95);
      }
      ctx.closePath();
      ctx.fill();

      // 2. Gentle sheer-curtain breeze shimmer near window edge
      var sway = Math.sin(tNow * 0.0014) * cw * 0.012;
      var cGrad = ctx.createLinearGradient(0, 0, cw * 0.3, ch * 0.35);
      cGrad.addColorStop(0, "rgba(255, 255, 255, 0.11)");
      cGrad.addColorStop(1, "rgba(255, 255, 255, 0)");
      ctx.fillStyle = cGrad;
      ctx.beginPath();
      if (isPortrait) {
        ctx.moveTo(cw * 0.48, 0);
        ctx.quadraticCurveTo(cw * 0.44 + sway, ch * 0.18, cw * 0.32 + sway * 1.4, ch * 0.34);
        ctx.lineTo(cw * 0.56 + sway, ch * 0.32);
        ctx.lineTo(cw * 0.62, 0);
      } else {
        ctx.moveTo(cw * 0.38, 0);
        ctx.quadraticCurveTo(cw * 0.41 + sway, ch * 0.22, cw * 0.45 + sway * 1.4, ch * 0.44);
        ctx.lineTo(cw * 0.51 + sway, ch * 0.42);
        ctx.lineTo(cw * 0.49, 0);
      }
      ctx.closePath();
      ctx.fill();
      ctx.restore();

      // 3. Night desk lamp warm cozy halo
      if (th.id === "night") {
        var lampX = isPortrait ? cw * 0.48 : cw * 0.74;
        var lampY = isPortrait ? ch * 0.34 : ch * 0.36;
        var lampR = Math.min(cw, ch) * 0.34;
        var lg = ctx.createRadialGradient(lampX, lampY, lampR * 0.05, lampX, lampY, lampR);
        lg.addColorStop(0, "rgba(255, 224, 152, 0.28)");
        lg.addColorStop(0.5, "rgba(255, 210, 130, 0.09)");
        lg.addColorStop(1, "rgba(255, 210, 130, 0.0)");
        ctx.fillStyle = lg;
        ctx.fillRect(lampX - lampR, lampY - lampR, lampR * 2, lampR * 2);
      }

      // 4. Reactive laptop glow during thinking/replying/error
      if (currentState === "thinking" || currentState === "replying" || currentState === "error") {
        var lapX = isPortrait ? cw * 0.64 : cw * 0.81;
        var lapY = isPortrait ? ch * 0.39 : ch * 0.42;
        var lapR = Math.min(cw, ch) * 0.16;
        var pulse = 0.55 + 0.45 * Math.sin(tNow * 0.005);
        var lapColor = currentState === "error"
          ? "rgba(255, 132, 108, "
          : "rgba(116, 198, 255, ";
        var g2 = ctx.createRadialGradient(lapX, lapY, 2, lapX, lapY, lapR);
        g2.addColorStop(0, lapColor + (0.28 * pulse).toFixed(3) + ")");
        g2.addColorStop(1, lapColor + "0)");
        ctx.fillStyle = g2;
        ctx.beginPath();
        ctx.arc(lapX, lapY, lapR, 0, Math.PI * 2);
        ctx.fill();
      }

      // 5. Floating sunlight dust motes / starlight glints
      var stepSec = Math.min(dtMs, 60) * 0.001;
      ctx.fillStyle = th.moteColor;
      for (var i = 0; i < motes.length; i++) {
        var pt = motes[i];
        pt.x += pt.vx * stepSec;
        pt.y += pt.vy * stepSec;
        if (pt.x > 1.02) pt.x = -0.02;
        if (pt.y < -0.02) pt.y = 0.86;
        var twinkle = 0.35 + 0.65 * Math.abs(Math.sin(tNow * 0.0015 + pt.phase));
        ctx.globalAlpha = twinkle * 0.65;
        ctx.beginPath();
        ctx.arc(pt.x * cw, pt.y * ch, pt.r * dpr, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalAlpha = 1.0;
    }

    function drawContactShadow2D(ctx, cx, feetY, dw, bobY, color, alpha) {
      var lift = Math.max(0, -bobY);
      var shrink = Math.max(0.84, 1.0 - lift * 0.015);
      var rx = dw * 0.24 * shrink;
      var ry = Math.max(5 * dpr, dw * 0.042 * shrink);
      ctx.save();
      ctx.globalAlpha = alpha * 0.85;
      var g = ctx.createRadialGradient(cx, feetY - ry * 0.2, rx * 0.08, cx, feetY - ry * 0.2, rx);
      g.addColorStop(0, color);
      g.addColorStop(0.55, color);
      g.addColorStop(1, "rgba(42, 78, 118, 0)");
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.ellipse(cx, feetY - ry * 0.2, rx, ry, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }

    function drawStateAccents2D(ctx, pl, tNow, st) {
      ctx.save();
      if (st === "received") {
        var p = (tNow % 1400) / 1400;
        var rx = pl.cx + pl.dw * 0.26;
        var ry = pl.dy + pl.dh * 0.14;
        ctx.strokeStyle = "rgba(74, 166, 248, " + ((1 - p) * 0.72).toFixed(3) + ")";
        ctx.lineWidth = 2.2 * dpr;
        ctx.beginPath();
        ctx.arc(rx, ry, (8 + p * 26) * dpr, 0, Math.PI * 2);
        ctx.stroke();
      } else if (st === "happy" || st === "sent") {
        for (var i = 0; i < 5; i++) {
          var k = ((tNow * 0.0006 + i * 0.21) % 1);
          var sx = pl.cx + Math.sin(i * 2.1 + k * 3.1) * pl.dw * 0.36;
          var sy = pl.dy + pl.dh * (0.65 - k * 0.62);
          ctx.globalAlpha = Math.sin(k * Math.PI) * 0.82;
          ctx.fillStyle = (i % 2 === 0) ? "#8fd3ff" : "#ffe08c";
          ctx.beginPath();
          ctx.arc(sx, sy, (2.6 + (i % 3)) * dpr, 0, Math.PI * 2);
          ctx.fill();
        }
      } else if (st === "rest") {
        for (var j = 0; j < 3; j++) {
          var q = ((tNow * 0.00032 + j * 0.33) % 1);
          var bx = pl.cx + pl.dw * 0.22 + Math.sin(q * 4 + j) * 7 * dpr;
          var by = pl.dy + pl.dh * 0.24 - q * 44 * dpr;
          ctx.globalAlpha = Math.sin(q * Math.PI) * 0.62;
          ctx.strokeStyle = "rgba(128, 192, 245, 0.85)";
          ctx.lineWidth = 1.6 * dpr;
          ctx.beginPath();
          ctx.arc(bx, by, (3.2 + q * 4.5) * dpr, 0, Math.PI * 2);
          ctx.stroke();
        }
      }
      ctx.restore();
    }

    function drawMorphCharacter2D(ctx, cw, ch, tNow, camScale, t, th) {
      var frFrom = getFrameForPack(morph.fromPack, currentState, 0);
      var frTo = getFrameForPack(morph.toPack, currentState, frameIndex);

      var blend = easeInOutCubic(Math.max(0, t));
      var outAlpha = frTo ? 1.0 - blend : 1.0;
      var outScale = 1.0 - 0.06 * blend;
      var inAlpha = frFrom ? blend : 1.0;
      var inScale = 0.92 + 0.08 * blend;

      var drawnAny = false;
      var refFr = frTo || frFrom;
      if (refFr) {
        var refPl = computeCharPlacement(morph.toPack, refFr, cw, ch, camScale, 1.0, 0);
        drawContactShadow2D(ctx, refPl.cx, refPl.feetY, refPl.dw, 0, th.shadowColor, 0.95);
      }

      if (frFrom && outAlpha > 0.01) {
        var plFrom = computeCharPlacement(morph.fromPack, frFrom, cw, ch, camScale, outScale, 0);
        ctx.save();
        ctx.globalAlpha = outAlpha;
        ctx.drawImage(frFrom.img, frFrom.rect[0], frFrom.rect[1], frFrom.rect[2], frFrom.rect[3],
                      plFrom.dx, plFrom.dy, plFrom.dw, plFrom.dh);
        ctx.restore();
        drawnAny = true;
      }

      if (frTo && inAlpha > 0.01) {
        var plTo = computeCharPlacement(morph.toPack, frTo, cw, ch, camScale, inScale, 0);
        ctx.save();
        ctx.globalAlpha = inAlpha;
        ctx.drawImage(frTo.img, frTo.rect[0], frTo.rect[1], frTo.rect[2], frTo.rect[3],
                      plTo.dx, plTo.dy, plTo.dw, plTo.dh);
        ctx.restore();
        drawnAny = true;
      }

      if (!drawnAny) morphStats.blankFrames += 1;

      if (morph.bubbles && morph.bubbles.length > 0 && refFr) {
        var haloPl = computeCharPlacement(morph.toPack, refFr, cw, ch, camScale, 1.0, 0);
        var haloStrength = Math.sin(t * Math.PI);
        var hx = haloPl.cx;
        var hy = haloPl.feetY - haloPl.dh * 0.46;
        var hr = Math.max(haloPl.dw, haloPl.dh) * (0.48 + 0.24 * haloStrength);
        ctx.save();
        var hg = ctx.createRadialGradient(hx, hy, hr * 0.06, hx, hy, hr);
        hg.addColorStop(0, "rgba(245, 252, 255, " + (0.62 * haloStrength).toFixed(3) + ")");
        hg.addColorStop(0.55, "rgba(155, 214, 255, " + (0.34 * haloStrength).toFixed(3) + ")");
        hg.addColorStop(1, "rgba(155, 214, 255, 0)");
        ctx.fillStyle = hg;
        ctx.beginPath();
        ctx.arc(hx, hy, hr, 0, Math.PI * 2);
        ctx.fill();

        for (var i = 0; i < morph.bubbles.length; i++) {
          var b = morph.bubbles[i];
          var bt = Math.max(0, Math.min(1, (t - b.delay) / Math.max(0.2, 1 - b.delay)));
          var bAlpha = Math.sin(bt * Math.PI) * 0.82;
          if (bAlpha <= 0.02) continue;
          var bx = hx + b.ox * haloPl.dw * (1 + bt * 0.35);
          var by = haloPl.feetY - (b.oy * haloPl.dh) - bt * haloPl.dh * 0.42 * b.speed;
          ctx.fillStyle = b.hue + bAlpha.toFixed(3) + ")";
          ctx.beginPath();
          ctx.arc(bx, by, b.r * dpr * (0.6 + 0.5 * bAlpha), 0, Math.PI * 2);
          ctx.fill();
        }
        ctx.restore();
      }
    }

    function drawWebGL(tNow) {
      if (!webgl) return;
      var cw = canvasGl.width;
      var ch = canvasGl.height;
      var effTheme = effectiveThemeId();
      var camScale = 1.0;
      var morphT = -1;
      if (morph) {
        morphT = (tNow - morph.startMs) / morph.durationMs;
        if (morphT >= 1.0) {
          var elapsedMorph = Math.round(tNow - morph.startMs);
          morphStats.lastMs = elapsedMorph;
          if (elapsedMorph > morphStats.maxMs) morphStats.maxMs = elapsedMorph;
          morph = null;
          morphT = -1;
        } else {
          camScale = 1.0 + 0.04 * Math.sin(morphT * Math.PI);
        }
      }

      var tint = null;
      if (effTheme === "sunset") tint = [1.0, 0.70, 0.44, 0.16];
      else if (effTheme === "night") tint = [0.10, 0.18, 0.38, 0.40];

      webgl.begin(cw, ch);
      var roomImg = roomImgs[isPortrait ? "mobile" : "pc"] || roomImgs.pc || roomImgs.mobile;
      if (roomImg && roomImg.naturalWidth > 0) {
        var rRect = computeCoverRect(roomImg.naturalWidth, roomImg.naturalHeight, cw, ch, camScale);
        webgl.drawQuad(roomImg, 0, 0, roomImg.naturalWidth, roomImg.naturalHeight,
                       rRect.dx, rRect.dy, rRect.dw, rRect.dh, cw, ch, 1.0, tint);
      }

      if (morph && morphT >= 0) {
        var frFrom = getFrameForPack(morph.fromPack, currentState, 0);
        var frTo = getFrameForPack(morph.toPack, currentState, frameIndex);
        var blend = easeInOutCubic(Math.max(0, morphT));
        var outA = frTo ? 1.0 - blend : 1.0;
        var inA = frFrom ? blend : 1.0;
        if (frFrom && outA > 0.01) {
          var plF = computeCharPlacement(morph.fromPack, frFrom, cw, ch, camScale, 1.0 - 0.05 * morphT, 0);
          webgl.drawQuad(frFrom.img, frFrom.rect[0], frFrom.rect[1], frFrom.rect[2], frFrom.rect[3],
                         plF.dx, plF.dy, plF.dw, plF.dh, cw, ch, outA, null);
        }
        if (frTo && inA > 0.01) {
          var plT = computeCharPlacement(morph.toPack, frTo, cw, ch, camScale, 0.93 + 0.07 * inA, 0);
          webgl.drawQuad(frTo.img, frTo.rect[0], frTo.rect[1], frTo.rect[2], frTo.rect[3],
                         plT.dx, plT.dy, plT.dw, plT.dh, cw, ch, inA, null);
        }
      } else if (currentPack) {
        var fr = getFrameForPack(currentPack, currentState, frameIndex);
        if (fr) {
          var bobAmp = (perfMode === "eco" || reducedMotion) ? 0 : (fr.spec.bobAmp || 2.0) * dpr;
          var bobY = Math.sin(tNow * (fr.spec.bobSpeed || 0.0022)) * bobAmp;
          var pl = computeCharPlacement(currentPack, fr, cw, ch, camScale, 1.0, bobY);
          var cTint = (effTheme === "night") ? [0.28, 0.42, 0.72, 0.14] : null;
          webgl.drawQuad(fr.img, fr.rect[0], fr.rect[1], fr.rect[2], fr.rect[3],
                         pl.dx, pl.dy, pl.dw, pl.dh, cw, ch, 1.0, cTint);
        }
      }
    }

    function step(tNow) {
      if (paused) return;
      rafId = requestAnimationFrame(step);

      var dtMs = tNow - lastTickMs;
      lastTickMs = tNow;
      if (dtMs < 0 || dtMs > 500) dtMs = 16.6;

      var minInterval = (perfMode === "eco") ? 64 : 0;
      if (minInterval > 0 && (tNow - lastDrawMs) < minInterval) return;

      var drawDelta = lastDrawMs ? (tNow - lastDrawMs) : 16.6;
      lastDrawMs = tNow;
      if (drawDelta > 0 && drawDelta < 1000) {
        frameTimes.push(drawDelta);
        if (frameTimes.length > 90) frameTimes.shift();
        if (perfMode !== "eco" && drawDelta > 55) frameDrops += 1;
      }

      var effTheme = effectiveThemeId();
      if (currentPack && (effTheme !== "video" || videoDegraded)) {
        var spec = getStateSpec(currentPack, currentState);
        if (spec && spec.frames && spec.frames.length > 1) {
          var dur = (spec.frames[frameIndex % spec.frames.length].durationMs || 400);
          frameElapsedMs += dtMs;
          if (frameElapsedMs >= dur) {
            frameElapsedMs -= dur;
            if (spec.loop !== false || frameIndex < spec.frames.length - 1) {
              frameIndex = (frameIndex + 1) % spec.frames.length;
            }
          }
        }
      }

      if (effTheme !== "video" || videoDegraded) {
        if (rendererMode === "webgl" && webgl) drawWebGL(tNow);
        else draw2D(tNow, dtMs);
      }
    }

    function pause() {
      if (paused) return;
      paused = true;
      pauseCount += 1;
      if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
      if (effectiveThemeId() === "video") {
        var v = videoLayers[videoActiveIdx];
        if (v && !v.paused) { videoPrevPlaying = true; try { v.pause(); } catch (e) {} }
      }
    }

    function resume() {
      if (!paused) return;
      paused = false;
      lastTickMs = nowMs();
      lastDrawMs = 0;
      if (effectiveThemeId() === "video" && videoPrevPlaying) {
        videoPrevPlaying = false;
        var v = videoLayers[videoActiveIdx];
        if (v) { try { v.play()["catch"](function () {}); } catch (e) {} }
      }
      rafId = requestAnimationFrame(step);
    }

    function start() {
      resize();
      loadRoomImage(isPortrait ? "mobile" : "pc", perfMode === "eco");
      setTimeout(function () {
        loadRoomImage(isPortrait ? "pc" : "mobile", perfMode === "eco");
      }, 1800);
      paused = false;
      lastTickMs = nowMs();
      rafId = requestAnimationFrame(step);
    }

    function getMetrics() {
      var sorted = frameTimes.slice().sort(function (a, b) { return a - b; });
      var avgDelta = 0;
      for (var i = 0; i < sorted.length; i++) avgDelta += sorted[i];
      avgDelta = sorted.length ? (avgDelta / sorted.length) : 0;
      var p90Delta = sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.9))] : 0;
      var avgFps = avgDelta > 0 ? Math.round(1000 / avgDelta) : 0;
      var p10Fps = p90Delta > 0 ? Math.round(1000 / p90Delta) : 0;
      return {
        renderer: rendererMode,
        webglSupported: webglSupported,
        themePref: themePref,
        theme: effectiveThemeId(),
        perfMode: perfMode,
        reducedMotion: reducedMotion,
        paused: paused,
        pauseCount: pauseCount,
        viewport: viewW + "x" + viewH,
        dpr: dpr,
        orientation: isPortrait ? "portrait" : "landscape",
        firstPaintMs: firstPaintAt,
        firstCharMs: firstCharAt,
        roomLoadMs: roomLoadMs,
        fps: avgFps,
        p10Fps: p10Fps,
        frameDrops: frameDrops,
        morphCount: morphStats.count,
        lastMorphMs: morphStats.lastMs,
        maxMorphMs: morphStats.maxMs,
        morphBlankFrames: morphStats.blankFrames,
        videoClip: videoClip,
        videoDegraded: videoDegraded,
        memoryMb: (window.performance && performance.memory)
          ? Math.round((performance.memory.usedJSHeapSize / (1024 * 1024)) * 10) / 10
          : null
      };
    }

    return {
      start: start,
      resize: resize,
      pause: pause,
      resume: resume,
      setPack: setPack,
      setState: setState,
      setTheme: setTheme,
      setPerfMode: setPerfMode,
      setRendererMode: setRendererMode,
      hitTestCharacter: hitTestCharacter,
      spawnTapBurst: spawnTapBurst,
      getEffectiveTheme: effectiveThemeId,
      getMetrics: getMetrics,
      THEMES: THEMES
    };
  }

  root.DaFeiYuStageRenderer = {
    createStageRenderer: createStageRenderer,
    THEMES: THEMES
  };
})(typeof self !== "undefined" ? self : this);
