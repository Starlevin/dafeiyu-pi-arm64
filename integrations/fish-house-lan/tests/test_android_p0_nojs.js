"use strict";
/* Regression: Android WebView P0 blank-screen contract for fish-house index.html.
 *
 * Background (candidate codex/house-p0-20261121, commit 3c198714):
 *   Old page rendered the first scene ONLY via persona.js (adding .frame.on /
 *   .layer.show) and used Chrome-66-incompatible CSS (aspect-ratio/inset/min()/
 *   env()/backdrop-filter). If the script stalled or failed, a portrait Android
 *   WebView showed a full-screen blank gray/blue panel (the documented P0).
 *   The fix makes the first scene pure CSS/markup, so it paints before/without JS.
 *
 * This test serves the REAL index.html over HTTP in an isolated headless Chrome,
 * emulates an Android 9 Chrome 66 UA + 420x820 portrait viewport, and asserts:
 *   1) fixed + JS blocked  -> first scene visible          (the P0 must-pass)
 *   2) fixed + JS enabled  -> first scene visible
 *   3) pre-fix + JS blocked -> first scene NOT visible      (sensitivity check)
 *   4) fixed + PC landscape -> landscape scene visible     (preserve PC path)
 *
 * Test layer: real HTTP + real Chromium with Android UA/viewport emulation.
 * NOT a physical-device / real Chrome 66 renderer test (no such device here).
 * Zero external packages; uses Node 24 native WebSocket + child_process.
 */
const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");
const net = require("net");
const { spawn, execFileSync } = require("child_process");

const REPO_INDEX = path.resolve(__dirname, "..", "web", "fish_house", "index.html");
const CANDIDATE = "3c198714"; // codex/house-p0-20261121
// CHROME_BIN (or CHROME_PATH) overrides the binary; otherwise try common legal
// install locations. Both ports are OS-assigned at runtime so this test can
// never attach to a pre-existing debuggable browser (no fixed 9241/18893).
let CHROME = null;
let CDP_PORT = 0;
let HTTP_PORT = 0;
const UA_ANDROID = "Mozilla/5.0 (Linux; Android 9; Archimedes) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Version/4.0 Chrome/66.0.3359.158 Mobile Safari/537.36";
const UA_PC = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// Created only AFTER pre-flight checks pass, so a failed startup leaves no temp dir.
let tmpRoot = null;

// Resolve a Chrome/Chromium binary without a hardcoded path. Env override wins;
// then a small list of common legal defaults. Missing binary = clear diagnostic.
function resolveChrome() {
  const env = process.env.CHROME_BIN || process.env.CHROME_PATH;
  // An explicit override that points nowhere is a config mistake: fail loudly
  // instead of silently falling back to a different binary.
  if (env && !fs.existsSync(env)) {
    throw new Error("CHROME_BIN/CHROME_PATH is set but not found: " + env);
  }
  const candidates = [];
  if (env) candidates.push(env);
  if (process.platform === "win32") {
    for (const pf of ["ProgramFiles", "ProgramFiles(x86)", "LocalAppData"]) {
      const base = process.env[pf];
      if (base) {
        candidates.push(path.join(base, "Google", "Chrome", "Application", "chrome.exe"));
        candidates.push(path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"));
      }
    }
  } else if (process.platform === "darwin") {
    candidates.push("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome");
    candidates.push("/Applications/Chromium.app/Contents/MacOS/Chromium");
  } else {
    candidates.push("/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser");
  }
  for (const c of candidates) {
    if (c && fs.existsSync(c)) return c;
  }
  throw new Error("Chrome not found. Set CHROME_BIN to a Chrome/Chromium executable. Tried:\n  " +
    candidates.filter(Boolean).join("\n  "));
}

// Ask the OS for a free loopback port (bind :0, read assigned port, release).
function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.once("error", reject);
    s.listen(0, "127.0.0.1", () => {
      const p = s.address().port;
      s.close(() => resolve(p));
    });
  });
}

// Wait for a child to actually exit (poll exitCode/signalCode; no external deps).
async function waitForExit(child, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) return true;
    await sleep(120);
  }
  return false;
}

// A tiny but valid PNG (2x2 opaque blue) — enough for naturalWidth>0 + geometry.
const PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFElEQVR42mP8z8Dwn4GBgYGJAQoAAD//AAP+2QnKAAAAAElFTkSuQmCC";
const PROBE = `(() => {
  function vis(el){if(!el)return false;var n=el;while(n&&n.nodeType===1){var s=getComputedStyle(n);
    if(s.display==='none'||s.visibility==='hidden'||parseFloat(s.opacity)<=0.01)return false;n=n.parentElement}
    var r=el.getBoundingClientRect();return r.width>1&&r.height>1}
  var active=[].slice.call(document.querySelectorAll('.frame')).filter(vis)
    .map(function(f){return f.getAttribute('data-orient')+':'+Math.round(f.getBoundingClientRect().width)+'x'+Math.round(f.getBoundingClientRect().height)});
  var imgs=[].slice.call(document.querySelectorAll('.layer img')).filter(function(im){return vis(im)&&im.complete&&im.naturalWidth>0})
    .map(function(im){return im.getAttribute('src')});
  return JSON.stringify({viewport:window.innerWidth+'x'+window.innerHeight,portrait:!!(window.matchMedia&&window.matchMedia('(orientation: portrait)').matches),
    activeFrames:active,decodedImgs:imgs,firstSceneVisible:active.length>0&&imgs.length>0});
})()`;

function buildFixtures() {
  const fixed = path.join(tmpRoot, "fixed");
  const prefix = path.join(tmpRoot, "prefixed");
  for (const d of [fixed, prefix]) {
    fs.mkdirSync(path.join(d, "characters"), { recursive: true });
    for (const n of ["whale-pc.png", "whale-android.png", "cat-pc.png", "cat-android.png"]) {
      fs.writeFileSync(path.join(d, "characters", n), Buffer.from(PNG_B64, "base64"));
    }
  }
  const html = fs.readFileSync(REPO_INDEX, "utf8");
  fs.writeFileSync(path.join(fixed, "index.html"), html);
  fs.copyFileSync(path.join(path.dirname(REPO_INDEX), "home.html"), path.join(fixed, "home.html"));
  const homeCss = path.join("home", "css", "home.css");
  fs.mkdirSync(path.dirname(path.join(fixed, homeCss)), { recursive: true });
  fs.copyFileSync(path.join(path.dirname(REPO_INDEX), homeCss), path.join(fixed, homeCss));
  // Derive the pre-fix page from the candidate's parent commit (sensitivity check).
  let ok = false;
  try {
    const rel = "integrations/fish-house-lan/web/fish_house/index.html";
    const old = execFileSync("git", ["show", CANDIDATE + "^:" + rel], { cwd: __dirname, encoding: "utf8" });
    fs.writeFileSync(path.join(prefix, "index.html"), old);
    ok = true;
  } catch (e) { /* git history unavailable: skip sensitivity check only */ }
  return { fixed, prefix, prefixAvailable: ok };
}

function startServer() {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      const requestPath = decodeURIComponent(req.url.split("?")[0]);
      if (requestPath === "/") {
        res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
        res.end('<main id="console-fixture">Console</main>');
        return;
      }
      const p = path.join(tmpRoot, requestPath);
      fs.readFile(p, (err, data) => {
        if (err) { res.writeHead(404); res.end("nf"); return; }
        const contentType = p.endsWith(".png") ? "image/png" :
          p.endsWith(".css") ? "text/css; charset=utf-8" : "text/html; charset=utf-8";
        res.writeHead(200, { "Content-Type": contentType });
        res.end(data);
      });
    });
    srv.listen(HTTP_PORT, "127.0.0.1", () => resolve(srv));
  });
}

async function cdpTargets() {
  const r = await fetch("http://127.0.0.1:" + CDP_PORT + "/json/list");
  return r.json();
}
class CDP {
  constructor(u) { this.ws = new WebSocket(u); this.id = 0; this.pending = new Map(); }
  open() { return new Promise((res, rej) => { this.ws.onopen = res; this.ws.onerror = rej; }); }
  call(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  listen() {
    this.ws.onmessage = (ev) => {
      const m = JSON.parse(String(ev.data));
      if (m.id && this.pending.has(m.id)) {
        const p = this.pending.get(m.id); this.pending.delete(m.id);
        m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result);
      }
    };
  }
}

async function probe(cdp, dir, jsOn, ua, w, h) {
  await cdp.call("Emulation.setScriptExecutionDisabled", { value: !jsOn });
  await cdp.call("Emulation.setUserAgentOverride", { userAgent: ua });
  await cdp.call("Emulation.setDeviceMetricsOverride", {
    width: w, height: h, deviceScaleFactor: 1, mobile: w < 600, screenWidth: w, screenHeight: h
  });
  await cdp.call("Page.navigate", { url: "http://127.0.0.1:" + HTTP_PORT + "/" + dir + "/index.html" });
  await sleep(2400);
  const r = await cdp.call("Runtime.evaluate", { expression: PROBE, returnByValue: true });
  return JSON.parse(r.result.value);
}

async function consoleNavigationWithoutScripts(cdp) {
  const root = "http://127.0.0.1:" + HTTP_PORT;
  await probe(cdp, "fixed", false, UA_ANDROID, 390, 740);
  await cdp.call("Page.navigate", { url: root + "/fixed/home.html" });
  const evaluate = async (expression) => (await cdp.call("Runtime.evaluate", {
    expression, returnByValue: true
  })).result.value;
  let control;
  for (let i = 0; i < 20 && !control; i++) {
    control = await evaluate(`(() => {
      const link = document.querySelector("#fh-dock-console");
      if (!link || location.pathname !== "/fixed/home.html" || document.readyState !== "complete") return null;
      const rect = link.getBoundingClientRect();
      const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
      if (document.elementFromPoint(x, y) !== link) return null;
      return { x, y, tag: link.tagName, href: link.getAttribute("href") };
    })()`);
    if (!control) await sleep(100);
  }
  if (!control || control.tag !== "A" || control.href !== "/") return { ok: false, control };
  await cdp.call("Input.dispatchMouseEvent", { type: "mousePressed", button: "left",
    clickCount: 1, x: control.x, y: control.y });
  await cdp.call("Input.dispatchMouseEvent", { type: "mouseReleased", button: "left",
    clickCount: 1, x: control.x, y: control.y });
  for (let i = 0; i < 25; i++) {
    if (await evaluate('location.pathname === "/" && !!document.querySelector("#console-fixture")')) return { ok: true };
    await sleep(100);
  }
  return { ok: false, control };
}

(async () => {
  if (!fs.existsSync(REPO_INDEX)) { console.error("MISSING", REPO_INDEX); process.exit(2); }
  try { CHROME = resolveChrome(); }
  catch (e) { console.error("CHROME_RESOLVE_ERROR " + e.message); process.exit(2); }
  CDP_PORT = await freePort();
  HTTP_PORT = await freePort();
  tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "fh-p0-"));
  const fx = buildFixtures();
  const srv = await startServer();
  const profile = path.join(tmpRoot, "chrome-profile");
  const chrome = spawn(CHROME, [
    "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
    "--no-default-browser-check", "--remote-debugging-port=" + CDP_PORT,
    "--user-data-dir=" + profile, "about:blank"
  ], { stdio: "ignore", windowsHide: true });

  let cdp;
  try {
    for (let i = 0; i < 40; i++) {
      try { const ts = await cdpTargets(); if (ts.some(t => t.type === "page")) break; } catch (e) {}
      await sleep(250);
    }
    const ts = await cdpTargets();
    const page = ts.find(t => t.type === "page");
    if (!page) throw new Error("no CDP page target");
    cdp = new CDP(page.webSocketDebuggerUrl);
    await cdp.open(); cdp.listen();
    await cdp.call("Page.enable"); await cdp.call("Runtime.enable");

    const fixedJsOff = await probe(cdp, "fixed", false, UA_ANDROID, 420, 820);
    const fixedJsOn = await probe(cdp, "fixed", true, UA_ANDROID, 420, 820);
    const prefixJsOff = fx.prefixAvailable ? await probe(cdp, "prefixed", false, UA_ANDROID, 420, 820) : null;
    const fixedPc = await probe(cdp, "fixed", true, UA_PC, 1280, 800);
    const consoleWithoutScripts = await consoleNavigationWithoutScripts(cdp);

    const checks = [
      ["P0 first scene visible with JS BLOCKED (Android portrait)",
       fixedJsOff.firstSceneVisible === true, fixedJsOff],
      ["first scene visible with JS enabled (Android portrait)",
       fixedJsOn.firstSceneVisible === true, fixedJsOn],
      ["pre-fix page reproduces blank screen with JS blocked (sensitivity)",
       fx.prefixAvailable ? prefixJsOff.firstSceneVisible === false : null, prefixJsOff],
      ["house console remains tappable with JavaScript blocked (Android portrait)",
       consoleWithoutScripts.ok, consoleWithoutScripts],
      ["PC landscape input path preserved",
       fixedPc.activeFrames.some(f => f.indexOf("landscape:") === 0) && fixedPc.firstSceneVisible,
       fixedPc]
    ];

    let passed = 0;
    for (const [name, ok, detail] of checks) {
      if (ok === null) { console.log("SKIP " + name + " (git history unavailable)"); continue; }
      console.log((ok ? "PASS " : "FAIL ") + name);
      console.log("      " + JSON.stringify(detail));
      if (ok) passed++;
    }
    const total = checks.filter(c => c[2] !== null).length;
    console.log("RESULT " + passed + "/" + total + " passed");
    process.exitCode = passed === total ? 0 : 1;
  } catch (e) {
    console.error("HARNESS_ERROR", e); process.exitCode = 2;
  } finally {
    // Controlled teardown: close CDP, ask Chrome to exit gracefully, wait for the
    // WHOLE child process tree, then close the HTTP server and remove only this
    // run's own temp dir. Never recursively clean other dirs or existing browsers.
    try { if (cdp) cdp.close(); } catch (e) {}
    let exited = "n/a";
    if (typeof chrome !== "undefined" && chrome) {
      try { chrome.kill(); } catch (e) {}
      exited = await waitForExit(chrome, 5000);
      if (!exited) {
        try { chrome.kill("SIGKILL"); } catch (e) {}
        await waitForExit(chrome, 3000);
      }
    }
    if (typeof srv !== "undefined" && srv) {
      await new Promise((r) => srv.close(r));
    }
    // Windows can hold the profile dir briefly after Chrome's processes exit;
    // retry removal a few times and fail loudly if it never goes away.
    if (!tmpRoot) return;
    let removed = false, lastErr = null;
    for (let i = 0; i < 10; i++) {
      try { fs.rmSync(tmpRoot, { recursive: true, force: true }); removed = true; break; }
      catch (e) { lastErr = e; await sleep(200); }
    }
    if (!removed) {
      console.error("CLEANUP_FAILED " + tmpRoot + " :: " + (lastErr && lastErr.message));
      process.exitCode = 2;
    } else {
      console.log("CLEANUP_OK " + tmpRoot + " removed; chromeExit=" + exited);
    }
  }
})();
