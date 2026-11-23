/* DaFeiYu Home R5.2 — Character Pack Loader, Codec Probe & Cache Manager (Chrome 66 ES5)
 *
 * Responsibilities:
 *   - Fetch & validate pack registry and per-form manifest.json
 *   - Probe WebP alpha AND H.264 MP4 codec capabilities cleanly
 *   - Preload & verify decoded atlas bitmaps before committing any form switch
 *   - Best-effort Cache API warm caching (when secure context supports window.caches)
 *   - Lazy-prefetch secondary form during idle so Adult <-> Chibi morphs are instant
 *   - Graceful fallback to PNG poster or last good pack on network/decode error
 */
(function (root) {
  "use strict";

  var CACHE_NAME = "dafeiyu-home-assets-r52";

  function xhrJson(url, timeoutMs, cb) {
    var xhr = new XMLHttpRequest();
    var done = false;
    var timer = setTimeout(function () {
      if (done) return;
      done = true;
      try { xhr.abort(); } catch (e) {}
      cb(new Error("timeout:" + url), null);
    }, timeoutMs || 8000);

    xhr.open("GET", url, true);
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4 || done) return;
      done = true;
      clearTimeout(timer);
      if (xhr.status < 200 || xhr.status >= 300) {
        cb(new Error("http_" + xhr.status + ":" + url), null);
        return;
      }
      try {
        cb(null, JSON.parse(xhr.responseText));
      } catch (e) {
        cb(new Error("bad_json:" + url), null);
      }
    };
    xhr.onerror = function () {
      if (done) return;
      done = true;
      clearTimeout(timer);
      cb(new Error("network:" + url), null);
    };
    try { xhr.send(); } catch (e) {
      if (!done) {
        done = true;
        clearTimeout(timer);
        cb(e, null);
      }
    }
  }

  function warmCacheUrls(urls) {
    try {
      if (!window.caches || typeof window.caches.open !== "function" || !urls || !urls.length) return;
      window.caches.open(CACHE_NAME).then(function (cache) {
        for (var i = 0; i < urls.length; i++) {
          (function (u) {
            cache.match(u).then(function (hit) {
              if (!hit) {
                try { cache.add(u)["catch"](function () {}); } catch (e) {}
              }
            })["catch"](function () {});
          })(urls[i]);
        }
      })["catch"](function () {});
    } catch (e) {}
  }

  function loadImage(url, timeoutMs, cb) {
    var img = new Image();
    var done = false;
    var t0 = (window.performance && performance.now) ? performance.now() : Date.now();
    var timer = setTimeout(function () {
      if (done) return;
      done = true;
      img.onload = null;
      img.onerror = null;
      cb(new Error("img_timeout:" + url), null, 0);
    }, timeoutMs || 12000);

    img.onload = function () {
      if (done) return;
      done = true;
      clearTimeout(timer);
      var dt = Math.round(((window.performance && performance.now) ? performance.now() : Date.now()) - t0);
      if (img.naturalWidth > 0 && img.naturalHeight > 0) {
        cb(null, img, dt);
      } else {
        cb(new Error("img_empty:" + url), null, dt);
      }
    };
    img.onerror = function () {
      if (done) return;
      done = true;
      clearTimeout(timer);
      var dt = Math.round(((window.performance && performance.now) ? performance.now() : Date.now()) - t0);
      cb(new Error("img_decode_fail:" + url), null, dt);
    };
    img.src = url;
  }

  function detectWebpAlpha(cb) {
    var testImg = new Image();
    testImg.onload = function () {
      cb(testImg.width === 1 && testImg.height === 1);
    };
    testImg.onerror = function () { cb(false); };
    testImg.src = "data:image/webp;base64,UklGRkoAAABXRUJQVlA4WAoAAAAQAAAAAAAAAAAAQUxQSAwAAAARBxAR/Q9ERP8DAABWUDggGAAAABQBAJ0BKgEAAQAAAP4AAA3AAP7mtQAAAA==";
  }

  function detectH264Support() {
    try {
      var v = document.createElement("video");
      if (!v || typeof v.canPlayType !== "function") return false;
      var main = v.canPlayType('video/mp4; codecs="avc1.4D401E"');
      var base = v.canPlayType('video/mp4; codecs="avc1.42E01E"');
      return (main === "probably" || main === "maybe" || base === "probably" || base === "maybe");
    } catch (e) {
      return false;
    }
  }

  function createPackLoader(options) {
    var opts = options || {};
    var basePath = opts.basePath || "./";
    var registry = null;
    var packCache = {};
    var inFlight = {};
    var webpSupported = true;
    var h264Supported = detectH264Support();
    var metrics = {
      packLoads: {},
      errors: [],
      cacheHits: 0,
      webpSupported: true,
      h264Supported: h264Supported,
      cacheApiAvailable: !!(window.caches && typeof window.caches.open === "function")
    };

    detectWebpAlpha(function (ok) {
      webpSupported = ok;
      metrics.webpSupported = ok;
    });

    function recordErr(msg) {
      metrics.errors.push(String(msg).slice(0, 140));
      if (metrics.errors.length > 30) metrics.errors.shift();
    }

    function resolveUrl(rel, bust) {
      var u = rel;
      if (u.indexOf("http://") !== 0 && u.indexOf("https://") !== 0 && u.indexOf("/") !== 0) {
        u = basePath + u;
      }
      if (bust) {
        u += (u.indexOf("?") >= 0 ? "&" : "?") + "_v=" + encodeURIComponent(String(bust));
      }
      return u;
    }

    function loadRegistry(cb, forceReload) {
      if (registry && !forceReload) {
        cb(null, registry);
        return;
      }
      xhrJson(resolveUrl("home/packs/registry.json", forceReload ? Date.now() : ""), 6000, function (err, data) {
        if (err || !data || !data.packs) {
          recordErr(err ? err.message : "invalid_registry");
          cb(err || new Error("invalid_registry"), null);
          return;
        }
        registry = data;
        cb(null, registry);
      });
    }

    function loadAtlasSet(manifest, lowPerf, bust, cb) {
      var images = {};
      var atlases = manifest.atlases || {};
      var keys = [];
      var urlsToWarm = [];
      var k;
      for (k in atlases) {
        if (Object.prototype.hasOwnProperty.call(atlases, k)) keys.push(k);
      }

      if (!webpSupported || keys.length === 0) {
        loadImage(resolveUrl(manifest.poster, bust), 10000, function (pErr, pImg, pMs) {
          if (pErr) cb(pErr, null, null, pMs);
          else cb(null, {}, pImg, pMs);
        });
        return;
      }

      var pending = keys.length;
      var failed = false;
      var totalMs = 0;
      var loadedCount = 0;
      var posterImg = null;
      if (manifest.poster) {
        var pUrl = resolveUrl(manifest.poster, bust);
        urlsToWarm.push(pUrl);
        loadImage(pUrl, 8000, function (pe, pi) {
          if (!pe && pi) posterImg = pi;
        });
      }

      for (var i = 0; i < keys.length; i++) {
        (function (atlasKey) {
          var meta = atlases[atlasKey];
          var url = resolveUrl(meta.src, bust || manifest.version);
          urlsToWarm.push(url);
          loadImage(url, 14000, function (err, img, dt) {
            totalMs = Math.max(totalMs, dt);
            if (err) {
              recordErr("atlas:" + manifest.id + ":" + atlasKey + ":" + err.message);
              failed = true;
            } else {
              images[atlasKey] = img;
              loadedCount += 1;
            }
            pending -= 1;
            if (pending === 0) {
              if (loadedCount > 0) warmCacheUrls(urlsToWarm);
              if (loadedCount === 0) {
                if (posterImg) {
                  cb(null, images, posterImg, totalMs);
                } else if (manifest.poster) {
                  loadImage(resolveUrl(manifest.poster, bust), 8000, function (pErr, pImg) {
                    if (pErr) cb(new Error("all_atlases_failed:" + manifest.id), null, null, totalMs);
                    else cb(null, images, pImg, totalMs);
                  });
                } else {
                  cb(new Error("all_atlases_failed:" + manifest.id), null, null, totalMs);
                }
              } else {
                cb(null, images, posterImg, totalMs, failed);
              }
            }
          });
        })(keys[i]);
      }
    }

    function loadPack(formKey, cb, optsLoad) {
      var o = optsLoad || {};
      var forceReload = !!o.forceReload;
      if (packCache[formKey] && !forceReload) {
        metrics.cacheHits += 1;
        cb(null, packCache[formKey]);
        return;
      }
      if (inFlight[formKey] && !forceReload) {
        inFlight[formKey].push(cb);
        return;
      }
      inFlight[formKey] = [cb];

      function finish(err, pack) {
        var cbs = inFlight[formKey] || [];
        delete inFlight[formKey];
        for (var i = 0; i < cbs.length; i++) {
          try { cbs[i](err, pack); } catch (e) {}
        }
      }

      var tStart = (window.performance && performance.now) ? performance.now() : Date.now();
      loadRegistry(function (rErr, reg) {
        if (rErr || !reg.packs[formKey]) {
          finish(rErr || new Error("pack_not_found:" + formKey), null);
          return;
        }
        var entry = reg.packs[formKey];
        var bust = forceReload ? String(Date.now()) : "";
        xhrJson(resolveUrl(entry.manifest, bust), 8000, function (mErr, manifest) {
          if (mErr || !manifest || !manifest.id) {
            recordErr(mErr ? mErr.message : "bad_manifest:" + formKey);
            finish(mErr || new Error("bad_manifest:" + formKey), null);
            return;
          }

          if (manifest.format === "mp4-h264-video") {
            var vPack = {
              formKey: formKey,
              manifest: manifest,
              images: {},
              posterImg: null,
              h264Supported: h264Supported,
              loadedAt: Date.now(),
              loadMs: Math.round(((window.performance && performance.now) ? performance.now() : Date.now()) - tStart),
              bytes: 0,
              partial: false
            };
            packCache[formKey] = vPack;
            metrics.packLoads[formKey] = vPack.loadMs;
            finish(null, vPack);
            return;
          }

          loadAtlasSet(manifest, !!o.lowPerf, bust, function (aErr, images, posterImg, imgMs, partial) {
            if (aErr) {
              finish(aErr, null);
              return;
            }
            var totalMs = Math.round(((window.performance && performance.now) ? performance.now() : Date.now()) - tStart);
            var pack = {
              formKey: formKey,
              manifest: manifest,
              images: images,
              posterImg: posterImg,
              loadedAt: Date.now(),
              loadMs: totalMs,
              atlasMs: imgMs,
              bytes: entry.totalBytes || 0,
              partial: !!partial
            };
            packCache[formKey] = pack;
            metrics.packLoads[formKey] = totalMs;
            finish(null, pack);
          });
        });
      }, forceReload);
    }

    function prefetch(formKey) {
      if (packCache[formKey] || inFlight[formKey]) return;
      loadPack(formKey, function () {}, {});
    }

    function getCached(formKey) {
      return packCache[formKey] || null;
    }

    function invalidate(formKey) {
      if (formKey) {
        delete packCache[formKey];
      } else {
        packCache = {};
        registry = null;
      }
    }

    return {
      loadRegistry: loadRegistry,
      loadPack: loadPack,
      prefetch: prefetch,
      getCached: getCached,
      invalidate: invalidate,
      loadImage: loadImage,
      resolveUrl: resolveUrl,
      warmCacheUrls: warmCacheUrls,
      isH264Supported: function () { return h264Supported; },
      getMetrics: function () {
        return {
          webpSupported: webpSupported,
          h264Supported: h264Supported,
          cacheApiAvailable: metrics.cacheApiAvailable,
          cacheHits: metrics.cacheHits,
          packLoads: metrics.packLoads,
          errors: metrics.errors.slice()
        };
      }
    };
  }

  root.DaFeiYuPackLoader = {
    createPackLoader: createPackLoader,
    detectH264Support: detectH264Support
  };
})(typeof self !== "undefined" ? self : this);
