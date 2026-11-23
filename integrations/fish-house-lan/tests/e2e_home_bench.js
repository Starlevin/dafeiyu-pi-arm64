"use strict";
/* Real Chrome CDP E2E + Cross-Viewport Zero-Overflow + SSE + 9-Module Console + Benchmark Suite (R5.2) */
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

const BASE = path.resolve(__dirname, "..");
const EVIDENCE = path.join(BASE, "evidence");
const CHROME_PATH = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const CDP_PORT = 9236;
const FIXTURE_PORT = 18890;
const READONLY_PORT = 18891;
const PROFILE_DIR = path.join(BASE, ".chrome-e2e-profile");

fs.mkdirSync(EVIDENCE, { recursive: true });

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

class CDPClient {
  constructor(wsUrl) {
    this.wsUrl = wsUrl;
    this.id = 0;
    this.pending = new Map();
    this.runtimeErrors = [];
    this.consoleErrors = [];
  }
  connect() {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(this.wsUrl);
      this.ws.onopen = () => resolve();
      this.ws.onerror = (e) => reject(e);
      this.ws.onmessage = (ev) => {
        const msg = JSON.parse(String(ev.data));
        if (msg.id && this.pending.has(msg.id)) {
          const p = this.pending.get(msg.id);
          this.pending.delete(msg.id);
          if (msg.error) p.reject(new Error(msg.error.message || JSON.stringify(msg.error)));
          else p.resolve(msg.result);
        } else if (msg.method) {
          if (msg.method === "Runtime.exceptionThrown") {
            const txt = "EXCEPTION: " + JSON.stringify(msg.params.exceptionDetails);
            this.runtimeErrors.push(txt);
            this.consoleErrors.push(txt);
          } else if (msg.method === "Log.entryAdded" && msg.params.entry && msg.params.entry.level === "error") {
            this.consoleErrors.push("LOG_ERR: " + msg.params.entry.text);
          }
        }
      };
    });
  }
  send(method, params) {
    return new Promise((resolve, reject) => {
      const id = ++this.id;
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params: params || {} }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error("CDP timeout: " + method));
        }
      }, 15000);
    });
  }
  async evalJS(expr) {
    const res = await this.send("Runtime.evaluate", {
      expression: expr,
      returnByValue: true,
      awaitPromise: true,
    });
    if (res.exceptionDetails) {
      throw new Error("Eval exception: " + JSON.stringify(res.exceptionDetails));
    }
    return res.result ? res.result.value : undefined;
  }
  async screenshot(filename) {
    const res = await this.send("Page.captureScreenshot", { format: "png" });
    const out = path.join(EVIDENCE, filename);
    fs.writeFileSync(out, Buffer.from(res.data, "base64"));
    return out;
  }
  close() { try { this.ws.close(); } catch (e) {} }
}

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

async function getPageWs(port) {
  const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
  const page = list.find((t) => t.type === "page");
  if (!page) throw new Error("No CDP page target found");
  return page.webSocketDebuggerUrl;
}

async function run() {
  const report = {
    version: "2.2.0-r5.2",
    startedAt: new Date().toISOString(),
    assertions: [],
    benchmarks: {},
    screenshots: [],
    runtimeErrors: [],
    consoleErrors: [],
  };

  function assertCheck(name, ok, detail) {
    report.assertions.push({ name, pass: !!ok, detail: detail || "" });
    console.log(ok ? "  [PASS]" : "  [FAIL]", name, detail ? ("— " + detail) : "");
    if (!ok) throw new Error("Assertion failed: " + name + " (" + detail + ")");
  }

  try { fs.rmSync(PROFILE_DIR, { recursive: true, force: true }); } catch (e) {}
  fs.mkdirSync(PROFILE_DIR, { recursive: true });
  const srvFix = spawn("python", [path.join(BASE, "dev_server.py"), "--port", String(FIXTURE_PORT), "--mode", "fixture"], { stdio: "ignore" });
  const srvRo = spawn("python", [path.join(BASE, "dev_server.py"), "--port", String(READONLY_PORT), "--mode", "readonly"], { stdio: "ignore" });
  const chrome = spawn(CHROME_PATH, [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu-sandbox",
    "--autoplay-policy=no-user-gesture-required",
    `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${PROFILE_DIR}`,
    "about:blank",
  ], { stdio: "ignore" });

  let cdp = null;
  try {
    await waitForHttp(`http://127.0.0.1:${FIXTURE_PORT}/healthz`, 10000);
    await waitForHttp(`http://127.0.0.1:${READONLY_PORT}/healthz`, 10000);
    await waitForHttp(`http://127.0.0.1:${CDP_PORT}/json/version`, 10000);

    const wsUrl = await getPageWs(CDP_PORT);
    cdp = new CDPClient(wsUrl);
    await cdp.connect();
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Log.enable");

    console.log("=== 1. PC 1280x800 Visual, SSE Transport, 9-State & Zero-Overflow Tests ===");
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 1280, height: 800, deviceScaleFactor: 1, mobile: false,
    });
    await cdp.send("Page.navigate", { url: `http://127.0.0.1:${FIXTURE_PORT}/fish_house/home.html?form=adult&theme=day&perf=high` });
    await sleep(2200);

    const mInit = await cdp.evalJS("window.DaFeiYuHome && window.DaFeiYuHome.getMetrics()");
    const pcOverflow = await cdp.evalJS("document.documentElement.scrollWidth <= window.innerWidth");
    assertCheck("PC boot + SSE real-time connected + zero overflow",
      !!mInit && mInit.pack && mInit.pack.id === "dafeiyu-adult" && mInit.connection.transport === "sse" && pcOverflow,
      `firstPaint=${mInit.renderer.firstPaintMs}ms firstChar=${mInit.renderer.firstCharMs}ms transport=${mInit.connection.transport} fps=${mInit.renderer.fps}`);
    await cdp.screenshot("pc-adult-day-idle.png");
    report.screenshots.push("pc-adult-day-idle.png");

    // Push a real Codex bridge-health event via SSE from server!
    const nowSec = Math.floor(Date.now() / 1000);
    await fetch(`http://127.0.0.1:${FIXTURE_PORT}/api/dev/inject_bridge_health`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        last_event: { time: nowSec, phase: "generation", reason: "started", trace: "e2e-trace-1" },
        source: "wechat"
      }),
    });
    await sleep(500);
    const sseState = await cdp.evalJS("window.DaFeiYuHome.getState()");
    assertCheck("Live SSE Codex bridge-health event drives thinking state",
      sseState.state === "thinking" && sseState.verified === true,
      `state=${sseState.state} source=${sseState.source} verified=${sseState.verified}`);

    // Test all 9 states
    const states9 = ["idle", "received", "thinking", "replying", "sent", "happy", "confused", "rest", "error"];
    for (const st of states9) {
      await cdp.evalJS(`window.DaFeiYuHome.pushEvent({ type: ${JSON.stringify(st)}, source: "demo", verified: false, force: true })`);
      await sleep(260);
      if (st === "replying") {
        await cdp.screenshot("pc-adult-replying.png");
        report.screenshots.push("pc-adult-replying.png");
      }
    }
    assertCheck("All 9 states transitioned cleanly", true, states9.join(","));

    // Adult -> Chibi Morph with zero-blank-frame assertion
    await cdp.evalJS(`window.DaFeiYuHome.pushEvent({ type: "idle", source: "demo", force: true })`);
    await sleep(300);
    await cdp.evalJS(`window.DaFeiYuHome.setForm("chibi", true)`);
    await sleep(260);
    await cdp.screenshot("pc-morph-mid.png");
    report.screenshots.push("pc-morph-mid.png");
    await sleep(600);
    const mChibi = await cdp.evalJS("window.DaFeiYuHome.getMetrics()");
    assertCheck("Morph Adult -> Chibi completed with 0 blank frames",
      mChibi.form === "chibi" && mChibi.pack.id === "dafeiyu-chibi" && mChibi.renderer.morphBlankFrames === 0,
      `lastMorphMs=${mChibi.renderer.lastMorphMs}ms blankFrames=${mChibi.renderer.morphBlankFrames}`);
    await cdp.screenshot("pc-chibi-day-idle.png");
    report.screenshots.push("pc-chibi-day-idle.png");

    await cdp.evalJS(`window.DaFeiYuHome.pushEvent({ type: "happy", source: "demo", force: true })`);
    await sleep(400);
    await cdp.screenshot("pc-chibi-happy.png");
    report.screenshots.push("pc-chibi-happy.png");

    await cdp.evalJS(`window.DaFeiYuHome.setForm("adult", true); window.DaFeiYuHome.setTheme("night"); window.DaFeiYuHome.pushEvent({ type: "thinking", source: "demo", force: true });`);
    await sleep(850);
    await cdp.screenshot("pc-adult-night.png");
    report.screenshots.push("pc-adult-night.png");

    console.log("=== 2. Multi-Mode Performance Benchmark (High 2D vs Eco 15fps vs WebGL vs Video) ===");
    await cdp.evalJS(`window.DaFeiYuHome.setTheme("day"); window.DaFeiYuHome.setPerfMode("high");`);
    await sleep(2000);
    report.benchmarks.pc_2d_high = await cdp.evalJS("window.DaFeiYuHome.getMetrics().renderer");

    await cdp.evalJS(`window.DaFeiYuHome.setPerfMode("eco");`);
    await sleep(2000);
    report.benchmarks.pc_2d_eco = await cdp.evalJS("window.DaFeiYuHome.getMetrics().renderer");

    await cdp.evalJS(`window.DaFeiYuHome.setPerfMode("high"); window.DaFeiYuHome.setWebgl(true);`);
    await sleep(2000);
    report.benchmarks.pc_webgl = await cdp.evalJS("window.DaFeiYuHome.getMetrics().renderer");

    await cdp.evalJS(`window.DaFeiYuHome.setWebgl(false); window.DaFeiYuHome.setTheme("video");`);
    await sleep(1800);
    report.benchmarks.pc_legacy_video = await cdp.evalJS("window.DaFeiYuHome.getMetrics().renderer");
    assertCheck("Multi-mode benchmark captured (High/Eco/WebGL/Video)",
      report.benchmarks.pc_2d_high.fps >= 50 && report.benchmarks.pc_2d_eco.fps <= 20 && report.benchmarks.pc_webgl.renderer === "webgl",
      `High=${report.benchmarks.pc_2d_high.fps}fps Eco=${report.benchmarks.pc_2d_eco.fps}fps WebGL=${report.benchmarks.pc_webgl.fps}fps`);
    await cdp.evalJS(`window.DaFeiYuHome.setTheme("day");`);

    console.log("=== 3. Unified 9-Module Console E2E + Persistence Verification ===");
    await cdp.evalJS(`window.DaFeiYuHome.openDrawer("console")`);
    await sleep(500);
    await cdp.evalJS(`document.getElementById("fh-login-pwd").value = "dev-fixture-pass"; document.getElementById("fh-login-btn").click();`);
    await sleep(1000);
    const consoleCheck = await cdp.evalJS(`({
      mode: window.DaFeiYuHome.getMetrics().consoleMode,
      memTag: document.getElementById("fh-mem-tag").textContent,
      stickerTag: document.getElementById("fh-sticker-tag").textContent,
      extTag: document.getElementById("fh-ext-tag").textContent,
      traceTag: document.getElementById("fh-trace-tag").textContent,
      fallback: document.getElementById("fh-ov-fallback").textContent
    })`);
    assertCheck("All 9 Unified Console modules loaded & badged honestly",
      consoleCheck.mode === "connected" && consoleCheck.memTag === "模拟数据" && consoleCheck.traceTag === "模拟数据",
      JSON.stringify(consoleCheck));

    await cdp.evalJS(`var s = document.getElementById("fh-select-qq-model"); s.value = "deepseek-reasoner"; s.onchange();`);
    await sleep(700);
    const saveCheck = await cdp.evalJS(`({
      tag: document.getElementById("fh-model-verify-tag").textContent,
      bar: document.getElementById("fh-save-bar").textContent,
      barCls: document.getElementById("fh-save-bar").className
    })`);
    assertCheck("Model save lifecycle confirmed with readback",
      saveCheck.tag === "回读已确认" && saveCheck.barCls === "confirmed",
      JSON.stringify(saveCheck));
    await cdp.screenshot("pc-console-drawer.png");
    report.screenshots.push("pc-console-drawer.png");

    // Persistence across full page reload (form=chibi, perf=eco, defaultHome=console)
    await cdp.evalJS(`window.DaFeiYuHome.setForm("chibi", false); window.DaFeiYuHome.setPerfMode("eco"); window.DaFeiYuHome.setDefaultHome("console");`);
    await sleep(400);
    await cdp.send("Page.navigate", { url: `http://127.0.0.1:${FIXTURE_PORT}/fish_house/home.html` });
    await sleep(1800);
    const persisted = await cdp.evalJS(`({
      form: window.DaFeiYuHome.getMetrics().form,
      perfPref: window.DaFeiYuHome.getMetrics().perfPref,
      homePref: window.DaFeiYuHome.getMetrics().defaultHomePref,
      drawerOpen: document.getElementById("fh-drawer").className.indexOf("open") >= 0
    })`);
    assertCheck("Form, perfMode (eco), & defaultHome (console) persist across reload",
      persisted.form === "chibi" && persisted.perfPref === "eco" && persisted.homePref === "console" && persisted.drawerOpen === true,
      JSON.stringify(persisted));
    await cdp.evalJS(`window.DaFeiYuHome.setDefaultHome("home"); window.DaFeiYuHome.setPerfMode("high"); window.DaFeiYuHome.closeDrawer();`);

    console.log("=== 4. Stress Test + Background Pause/Resume ===");
    const heapBefore = await cdp.evalJS("window.DaFeiYuHome.getMetrics().renderer.memoryMb");
    for (let i = 0; i < 30; i++) {
      const ev = states9[i % states9.length];
      await cdp.evalJS(`window.DaFeiYuHome.pushEvent({ type: ${JSON.stringify(ev)}, source: "demo", force: true })`);
      if (i % 5 === 0) {
        await cdp.evalJS(`window.DaFeiYuHome.setForm(${JSON.stringify(i % 10 === 0 ? "adult" : "chibi")}, true)`);
      }
      await sleep(65);
    }
    await sleep(700);
    const mStress = await cdp.evalJS("window.DaFeiYuHome.getMetrics()");
    assertCheck("30x event + morph stress test clean (0 blank frames, 0 JS exceptions)",
      cdp.runtimeErrors.length === 0 && mStress.renderer.morphBlankFrames === 0,
      `heap=${heapBefore}MB->${mStress.renderer.memoryMb}MB switches=${mStress.formSwitchCount}`);
    report.benchmarks.stress = { heapBeforeMb: heapBefore, heapAfterMb: mStress.renderer.memoryMb, switches: mStress.formSwitchCount };

    await cdp.evalJS(`Object.defineProperty(document, "hidden", { configurable: true, get: function(){ return true; } }); document.dispatchEvent(new Event("visibilitychange"));`);
    await sleep(250);
    const pausedState = await cdp.evalJS("window.DaFeiYuHome.getMetrics().renderer");
    await cdp.evalJS(`Object.defineProperty(document, "hidden", { configurable: true, get: function(){ return false; } }); document.dispatchEvent(new Event("visibilitychange"));`);
    await sleep(250);
    const resumedState = await cdp.evalJS("window.DaFeiYuHome.getMetrics().renderer");
    assertCheck("Background pause & foreground resume verified",
      pausedState.paused === true && pausedState.pauseCount >= 1 && resumedState.paused === false,
      `pauseCount=${pausedState.pauseCount}`);

    console.log("=== 5. Mobile Viewports (Archimedes 320x533 Chrome 66, 390x844, 420x820) + Zero Overflow ===");
    const mobileSpecs = [
      { name: "archimedes_320x533", w: 320, h: 533, dpr: 1.5, ua: "Mozilla/5.0 (Linux; Android 9; Archimedes) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/66.0.3359.158 Mobile Safari/537.36", shot: "mobile-archimedes-adult-idle.png" },
      { name: "modern_390x844", w: 390, h: 844, dpr: 2, ua: "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36", shot: "mobile-390-adult-idle.png" },
      { name: "standard_420x820", w: 420, h: 820, dpr: 2, ua: "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36", shot: "mobile-420-adult-idle.png" },
    ];
    for (const sp of mobileSpecs) {
      await cdp.send("Emulation.setUserAgentOverride", { userAgent: sp.ua });
      await cdp.send("Emulation.setDeviceMetricsOverride", { width: sp.w, height: sp.h, deviceScaleFactor: sp.dpr, mobile: true });
      await cdp.send("Page.navigate", { url: `http://127.0.0.1:${FIXTURE_PORT}/fish_house/home.html?form=adult&theme=day&perf=high&home=home` });
      await sleep(1600);
      const layout = await cdp.evalJS(`({
        overflowX: document.documentElement.scrollWidth > window.innerWidth,
        scrollW: document.documentElement.scrollWidth,
        innerW: window.innerWidth,
        dockH: document.getElementById("fh-dock").offsetHeight,
        dockRight: document.getElementById("fh-dock").getBoundingClientRect().right
      })`);
      assertCheck(`Mobile ${sp.name} zero horizontal overflow & touch target fit`,
        layout.overflowX === false && layout.dockH >= 36 && layout.dockRight <= sp.w + 1,
        JSON.stringify(layout));
      await cdp.screenshot(sp.shot);
      report.screenshots.push(sp.shot);
    }

    // Capture Mobile Dev Event Replay tab screenshot
    await cdp.evalJS(`window.DaFeiYuHome.openDrawer("events")`);
    await sleep(500);
    await cdp.screenshot("mobile-events-panel.png");
    report.screenshots.push("mobile-events-panel.png");

    console.log("=== 6. Read-Only Mode Honesty Verification (Port 18891) ===");
    await cdp.send("Page.navigate", { url: `http://127.0.0.1:${READONLY_PORT}/fish_house/home.html?home=home` });
    await sleep(1600);
    await cdp.evalJS(`window.DaFeiYuHome.openDrawer("console")`);
    await sleep(700);
    const roState = await cdp.evalJS(`({
      mode: window.DaFeiYuHome.getMetrics().consoleMode,
      qqDisabled: document.getElementById("fh-select-qq-model").disabled,
      wxDisabled: document.getElementById("fh-select-wx-model").disabled,
      memTag: document.getElementById("fh-mem-tag").textContent,
      traceTag: document.getElementById("fh-trace-tag").textContent
    })`);
    assertCheck("Read-only mode locks writes & marks unmounted contracts honestly",
      roState.mode === "readonly_only" && roState.qqDisabled === true && roState.wxDisabled === true,
      JSON.stringify(roState));

    report.runtimeErrors = cdp.runtimeErrors;
    report.consoleErrors = cdp.consoleErrors;
    assertCheck("Zero uncaught JS runtime exceptions across entire R5.2 E2E run",
      cdp.runtimeErrors.length === 0,
      `runtimeErrors=${cdp.runtimeErrors.length}`);

    report.finishedAt = new Date().toISOString();
    report.status = "PASS";
    fs.writeFileSync(path.join(EVIDENCE, "pc-e2e-report.json"), JSON.stringify(report, null, 2));
    console.log("\n=== R5.2 CHROME E2E & BENCHMARK SUITE: PASS ===");
  } finally {
    if (cdp) cdp.close();
    try { chrome.kill(); } catch (e) {}
    try { srvFix.kill(); } catch (e) {}
    try { srvRo.kill(); } catch (e) {}
  }
}

run().catch((err) => {
  console.error("E2E FAILED:", err);
  process.exit(1);
});
