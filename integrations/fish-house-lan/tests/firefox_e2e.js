"use strict";
/* Real Mozilla Firefox WebDriver BiDi Cross-Browser E2E & Screenshot Suite (R5.2)
 * Uses Firefox's built-in --remote-debugging-port (W3C WebDriver BiDi) + Node built-in WebSocket.
 * Verifies:
 *   1. PC 1280x800 boot, character WebP atlas render, SSE transport, zero horizontal overflow
 *   2. Adult -> Chibi smooth morph (0 blank frames)
 *   3. Mobile 390x844 portrait layout, Chibi character render, zero horizontal overflow
 *   4. Zero uncaught JS errors + real rendered screenshots (PC & Mobile)
 */
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

const BASE = path.resolve(__dirname, "..");
const EVIDENCE = path.join(BASE, "evidence");
const FIREFOX_PATH = "C:\\Program Files\\Mozilla Firefox\\firefox.exe";
const BIDI_PORT = 9245;
const SERVER_PORT = 18892;
const PROFILE_DIR = path.join(BASE, ".firefox-e2e-profile");

fs.mkdirSync(EVIDENCE, { recursive: true });

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

async function waitForHttp(url, timeoutMs) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const r = await fetch(url);
      if (r.ok) return true;
    } catch (e) {}
    await sleep(200);
  }
  throw new Error("Timed out waiting for " + url);
}

class BiDiClient {
  constructor(wsUrl) {
    this.wsUrl = wsUrl;
    this.id = 0;
    this.pending = new Map();
    this.jsErrors = [];
  }
  async connectWithRetry(timeoutMs) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      try {
        await new Promise((resolve, reject) => {
          const ws = new WebSocket(this.wsUrl);
          ws.onopen = () => { this.ws = ws; resolve(); };
          ws.onerror = (e) => reject(e);
          ws.onmessage = (ev) => {
            const msg = JSON.parse(String(ev.data));
            if (msg.id && this.pending.has(msg.id)) {
              const p = this.pending.get(msg.id);
              this.pending.delete(msg.id);
              if (msg.type === "error") p.reject(new Error(msg.error + ": " + msg.message));
              else p.resolve(msg.result);
            } else if (msg.type === "event" && msg.method === "log.entryAdded") {
              if (msg.params && msg.params.type === "javascript" && msg.params.level === "error") {
                this.jsErrors.push(msg.params.text || JSON.stringify(msg.params));
              }
            }
          };
        });
        return;
      } catch (e) {
        await sleep(250);
      }
    }
    throw new Error("Timed out connecting to Firefox BiDi at " + this.wsUrl);
  }
  send(method, params) {
    return new Promise((resolve, reject) => {
      const id = ++this.id;
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params: params || {} }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error("BiDi timeout: " + method));
        }
      }, 15000);
    });
  }
  async evalJson(context, expr) {
    const res = await this.send("script.evaluate", {
      expression: "JSON.stringify(" + expr + ")",
      target: { context },
      awaitPromise: false,
    });
    if (res.type === "exception") {
      throw new Error("Firefox eval exception: " + JSON.stringify(res.exceptionDetails));
    }
    return res.result && res.result.value ? JSON.parse(res.result.value) : null;
  }
  async screenshot(context, filename) {
    const res = await this.send("browsingContext.captureScreenshot", { context });
    const out = path.join(EVIDENCE, filename);
    fs.writeFileSync(out, Buffer.from(res.data, "base64"));
    return out;
  }
  close() { try { if (this.ws) this.ws.close(); } catch (e) {} }
}

async function run() {
  try { fs.rmSync(PROFILE_DIR, { recursive: true, force: true }); } catch (e) {}
  fs.mkdirSync(PROFILE_DIR, { recursive: true });

  const report = {
    version: "2.2.0-r5.2",
    browser: "Mozilla Firefox",
    startedAt: new Date().toISOString(),
    assertions: [],
    metrics: {},
    screenshots: [],
    jsErrors: [],
    status: "RUNNING",
  };

  function assertCheck(name, ok, detail) {
    report.assertions.push({ name, pass: !!ok, detail: detail || "" });
    console.log(ok ? "  [PASS]" : "  [FAIL]", name, detail ? ("— " + detail) : "");
    if (!ok) throw new Error("Assertion failed: " + name + " (" + detail + ")");
  }

  const srv = spawn("python", [path.join(BASE, "dev_server.py"), "--port", String(SERVER_PORT), "--mode", "fixture"], { stdio: "ignore" });
  const ff = spawn(FIREFOX_PATH, [
    "-headless",
    "-no-remote",
    "-profile", PROFILE_DIR,
    "--remote-debugging-port", String(BIDI_PORT),
  ], {
    stdio: "ignore",
    env: Object.assign({}, process.env, { MOZ_REMOTE_ALLOW_SYSTEM_ACCESS: "1" }),
  });

  let bidi = null;
  try {
    await waitForHttp(`http://127.0.0.1:${SERVER_PORT}/healthz`, 10000);
    bidi = new BiDiClient(`ws://127.0.0.1:${BIDI_PORT}/session`);
    await bidi.connectWithRetry(15000);

    const sess = await bidi.send("session.new", { capabilities: {} });
    if (sess && sess.capabilities && sess.capabilities.browserVersion) {
      report.browser = "Mozilla Firefox " + sess.capabilities.browserVersion;
    }
    await bidi.send("session.subscribe", { events: ["log.entryAdded"] });
    const created = await bidi.send("browsingContext.create", { type: "tab" });
    const ctx = created.context;

    console.log(`=== Firefox Cross-Browser E2E (${report.browser}) ===`);
    await bidi.send("browsingContext.navigate", {
      context: ctx,
      url: `http://127.0.0.1:${SERVER_PORT}/fish_house/home.html?form=adult&theme=day&perf=high&home=home`,
      wait: "complete",
    });
    await bidi.send("browsingContext.setViewport", {
      context: ctx,
      viewport: { width: 1280, height: 800 },
    });
    await sleep(2400);

    const pcMetrics = await bidi.evalJson(ctx, "window.DaFeiYuHome && window.DaFeiYuHome.getMetrics()");
    const pcLayout = await bidi.evalJson(ctx, "({ scrollW: document.documentElement.scrollWidth, innerW: window.innerWidth })");
    report.metrics.pc = pcMetrics;
    assertCheck("Firefox PC 1280x800 boot + Adult WebP atlas + SSE + zero overflow",
      !!pcMetrics && pcMetrics.pack && pcMetrics.pack.id === "dafeiyu-adult" &&
      pcMetrics.renderer.firstCharMs >= 0 && pcMetrics.connection.transport === "sse" &&
      pcLayout.scrollW <= pcLayout.innerW,
      `firstPaint=${pcMetrics.renderer.firstPaintMs}ms firstChar=${pcMetrics.renderer.firstCharMs}ms fps=${pcMetrics.renderer.fps} transport=${pcMetrics.connection.transport}`);

    await bidi.screenshot(ctx, "firefox-pc-1280x800.png");
    report.screenshots.push("firefox-pc-1280x800.png");

    // Morph Adult -> Chibi and verify 0 blank frames in Firefox
    await bidi.evalJson(ctx, `(window.DaFeiYuHome.setForm("chibi", true), true)`);
    await sleep(1400);
    const morphM = await bidi.evalJson(ctx, "window.DaFeiYuHome.getMetrics()");
    assertCheck("Firefox Adult -> Chibi morph clean (0 blank frames)",
      morphM.form === "chibi" && morphM.renderer.morphBlankFrames === 0,
      `form=${morphM.form} lastMorphMs=${morphM.renderer.lastMorphMs}ms blankFrames=${morphM.renderer.morphBlankFrames}`);

    // Mobile portrait 390x844 in Firefox
    await bidi.send("browsingContext.setViewport", {
      context: ctx,
      viewport: { width: 390, height: 844 },
    });
    await sleep(900);
    const mobLayout = await bidi.evalJson(ctx, `({
      scrollW: document.documentElement.scrollWidth,
      innerW: window.innerWidth,
      dockRight: Math.round(document.getElementById("fh-dock").getBoundingClientRect().right),
      form: window.DaFeiYuHome.getMetrics().form,
      fps: window.DaFeiYuHome.getMetrics().renderer.fps
    })`);
    report.metrics.mobile = mobLayout;
    assertCheck("Firefox Mobile 390x844 Chibi render + zero horizontal overflow",
      mobLayout.scrollW <= mobLayout.innerW && mobLayout.dockRight <= mobLayout.innerW && mobLayout.form === "chibi",
      JSON.stringify(mobLayout));

    await bidi.screenshot(ctx, "firefox-mobile-390x844.png");
    report.screenshots.push("firefox-mobile-390x844.png");

    report.jsErrors = bidi.jsErrors;
    assertCheck("Firefox zero uncaught JS errors", bidi.jsErrors.length === 0, `jsErrors=${bidi.jsErrors.length}`);

    report.status = "PASS";
    report.finishedAt = new Date().toISOString();
    fs.writeFileSync(path.join(EVIDENCE, "firefox-report.json"), JSON.stringify(report, null, 2));
    console.log("=== FIREFOX WEBDRIVER BIDI E2E SUITE: PASS ===");
  } finally {
    try { if (bidi) await bidi.send("session.end", {}); } catch (e) {}
    if (bidi) bidi.close();
    try { ff.kill(); } catch (e) {}
    try { srv.kill(); } catch (e) {}
    await sleep(400);
    try { fs.rmSync(PROFILE_DIR, { recursive: true, force: true }); } catch (e) {}
  }
}

run().catch((err) => {
  console.error("FIREFOX E2E FAILED:", err);
  process.exit(1);
});
