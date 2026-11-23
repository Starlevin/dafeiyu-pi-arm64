"use strict";
/* Real Chromium check for the authenticated QQ-management QZone lifecycle. */
const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const net = require("net");
const { spawn } = require("child_process");

const BASE = path.resolve(__dirname, "..");
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

function port() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.once("error", reject);
    s.listen(0, "127.0.0.1", () => {
      const p = s.address().port;
      s.close(() => resolve(p));
    });
  });
}
function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }
async function waitHttp(url, ms = 10000) {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    try { if ((await fetch(url)).ok) return; } catch (_) { /* server starting */ }
    await sleep(150);
  }
  throw new Error("Timed out waiting for " + url);
}
class Cdp {
  constructor(ws) { this.wsUrl = ws; this.id = 0; this.pending = new Map(); }
  connect() {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(this.wsUrl);
      this.ws.onopen = resolve;
      this.ws.onerror = reject;
      this.ws.onmessage = (event) => {
        const message = JSON.parse(String(event.data));
        if (!message.id || !this.pending.has(message.id)) return;
        const pending = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message));
        else pending.resolve(message.result);
      };
    });
  }
  send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++this.id;
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async eval(expression) {
    const response = await this.send("Runtime.evaluate", {
      expression, returnByValue: true, awaitPromise: true
    });
    if (response.exceptionDetails) throw new Error(JSON.stringify(response.exceptionDetails));
    return response.result && response.result.value;
  }
  close() { try { this.ws.close(); } catch (_) {} }
}

async function run() {
  const appPort = await port();
  const cdpPort = 9238;
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "qzone-home-cdp-"));
  const owner = spawn("python", [path.join(BASE, "dev_server.py"), "--host", "127.0.0.1",
    "--port", String(appPort), "--mode", "owner_fixture"], { stdio: "ignore" });
  const readonly = spawn("python", [path.join(BASE, "dev_server.py"), "--host", "127.0.0.1",
    "--port", String(appPort + 1), "--mode", "readonly"], { stdio: "ignore" });
  const chrome = spawn(CHROME, ["--headless=new", "--no-sandbox", "--disable-gpu",
    `--remote-debugging-port=${cdpPort}`, `--user-data-dir=${profile}`, "about:blank"],
  { stdio: "ignore" });
  let cdp;
  try {
    await waitHttp(`http://127.0.0.1:${appPort}/healthz`);
    await waitHttp(`http://127.0.0.1:${appPort + 1}/healthz`);
    await waitHttp(`http://127.0.0.1:${cdpPort}/json/version`);
    const targets = await (await fetch(`http://127.0.0.1:${cdpPort}/json`)).json();
    cdp = new Cdp(targets.find((target) => target.type === "page").webSocketDebuggerUrl);
    await cdp.connect();
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source:
      "window.__qzoneApiCalls=[];const f=window.fetch;window.fetch=function(u,...a){if(String(u).includes('/api/qzone/'))window.__qzoneApiCalls.push(String(u));return f.call(this,u,...a)};" });

    await cdp.send("Page.navigate", { url: `http://127.0.0.1:${appPort}/fish_house/home.html` });
    await sleep(1000);
    await cdp.eval("document.querySelector('[data-tab=\"qq-admin\"]').click()");
    await sleep(300);
    assert.strictEqual(await cdp.eval("fetch('/api/test/qzone_counts').then(r=>r.json()).then(x=>x.latest_reads)"), 0,
      "unauthenticated QQ tab must not call the QZone API");
    assert.strictEqual(await cdp.eval("!!document.getElementById('fh-qzone-script')"), false,
      "unauthenticated QQ tab must not load the QZone module");

    await cdp.eval("window.DaFeiYuHome.openDrawer('console')");
    await cdp.eval("document.getElementById('fh-login-pwd').value='dev-fixture-pass';document.getElementById('fh-login-btn').click()");
    await sleep(600);
    await cdp.eval("document.querySelector('[data-tab=\"qq-admin\"]').click()");
    const untilPanel = Date.now() + 8000;
    while (Date.now() < untilPanel && !(await cdp.eval("document.querySelector('.qzone-read-panel') !== null"))) await sleep(100);
    assert.strictEqual(await cdp.eval("document.querySelector('.qzone-read-panel') !== null"), true,
      "authenticated QQ management page should mount the QZone reader");
    assert.strictEqual(await cdp.eval("fetch('/api/test/qzone_counts').then(r=>r.json()).then(x=>x.latest_reads)"), 1,
      "authenticated mount should read only the persisted snapshot");
    assert.strictEqual(await cdp.eval("document.querySelector('.qzone-read-list b') === null && document.querySelector('.qzone-read-list').textContent.includes('<b>安全文本</b>')"), true,
      "feed text must be rendered as text, not HTML");

    await cdp.eval("document.querySelector('.qzone-read-panel button').click()");
    const untilRefresh = Date.now() + 5000;
    while (Date.now() < untilRefresh && !(await cdp.eval("fetch('/api/test/qzone_counts').then(r=>r.json()).then(x=>x.refreshes === 1)"))) await sleep(100);
    assert.strictEqual(await cdp.eval("fetch('/api/test/qzone_counts').then(r=>r.json()).then(x=>x.refreshes)"), 1,
      "manual refresh should call the read-refresh route");

    await cdp.eval("fetch('/api/test/expire_session',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(()=>window.DaFeiYuQzone.refresh())");
    const untilUnauthorized = Date.now() + 5000;
    while (Date.now() < untilUnauthorized && (await cdp.eval("window.DaFeiYuHome.getMetrics().consoleMode")) !== "auth_required") await sleep(100);
    assert.strictEqual(await cdp.eval("document.querySelector('.qzone-read-panel') === null"), true,
      "a stale-session 401 must invalidate the page session and remove the panel");

    await cdp.eval("window.DaFeiYuHome.openDrawer('console');document.getElementById('fh-login-pwd').value='dev-fixture-pass';document.getElementById('fh-login-btn').click()");
    await sleep(500);
    await cdp.eval("document.querySelector('[data-tab=\"qq-admin\"]').click()");
    const untilRemount = Date.now() + 8000;
    while (Date.now() < untilRemount && !(await cdp.eval("document.querySelector('.qzone-read-panel') !== null"))) await sleep(100);
    assert.strictEqual(await cdp.eval("document.querySelector('.qzone-read-panel') !== null"), true,
      "a fresh owner session should remount the panel");

    await cdp.eval("fetch('/api/test/qzone_delay',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({delay_ms:1400})}).then(()=>{window.DaFeiYuQzone.refresh();document.getElementById('fh-logout-btn').click()})");
    const untilLogout = Date.now() + 5000;
    while (Date.now() < untilLogout && (await cdp.eval("window.DaFeiYuHome.getMetrics().consoleMode")) !== "auth_required") await sleep(100);
    assert.strictEqual(await cdp.eval("document.querySelector('.qzone-read-panel') === null"), true,
      "logout should synchronously remove QZone DOM");
    await sleep(1700);
    assert.strictEqual(await cdp.eval("document.querySelector('.qzone-read-panel') === null"), true,
      "a late snapshot response after logout must not restore sensitive DOM");

    await cdp.eval("fetch('/api/test/qzone_asset_failure',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:true})}).then(()=>{window.DaFeiYuQzone.reset();delete window.DaFeiYuQzone;document.getElementById('fh-qzone-script').remove()})");
    await cdp.eval("window.DaFeiYuHome.openDrawer('console');document.getElementById('fh-login-pwd').value='dev-fixture-pass';document.getElementById('fh-login-btn').click()");
    await sleep(500);
    await cdp.eval("document.querySelector('[data-tab=\"qq-admin\"]').click()");
    const untilAssetFailure = Date.now() + 8000;
    while (Date.now() < untilAssetFailure && await cdp.eval("!document.getElementById('fh-qzone-script')?.dataset.loadFailed")) await sleep(100);
    assert.strictEqual(await cdp.eval("!!window.DaFeiYuAdminTools && document.getElementById('fh-qq-admin-gate').hidden && !document.getElementById('toolsMount').hidden"), true,
      "QZone asset failure must not fail the existing QQ admin module");
    assert.strictEqual(await cdp.eval("document.querySelector('.qzone-read-panel') === null"), true,
      "failed optional QZone asset should leave the existing admin page available");
    await cdp.eval("fetch('/api/test/qzone_asset_failure',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:false})})");
    await cdp.eval("document.querySelector('[data-tab=\"appearance\"]').click();document.querySelector('[data-tab=\"qq-admin\"]').click()");
    const untilAssetRecovery = Date.now() + 8000;
    while (Date.now() < untilAssetRecovery && !(await cdp.eval("document.querySelector('.qzone-read-panel') !== null"))) await sleep(100);
    assert.strictEqual(await cdp.eval("document.querySelector('.qzone-read-panel') !== null"), true,
      "re-entering QQ management should retry and recover the optional QZone viewer");

    await cdp.send("Page.navigate", { url: `http://127.0.0.1:${appPort + 1}/fish_house/home.html` });
    await sleep(1200);
    await cdp.eval("document.querySelector('[data-tab=\"qq-admin\"]').click()");
    await sleep(300);
    assert.strictEqual(await cdp.eval("window.DaFeiYuHome.getMetrics().consoleMode"), "readonly_only");
    assert.deepStrictEqual(await cdp.eval("window.__qzoneApiCalls"), [],
      "read-only home must not issue authenticated QZone requests");
    assert.strictEqual(await cdp.eval("!!document.getElementById('fh-qzone-script')"), false,
      "read-only home must not load the QZone module");
    console.log("PASS: Chromium QZone home lifecycle (auth gate, mount, snapshot, refresh, logout, stale response, read-only)");
  } finally {
    if (cdp) cdp.close();
    for (const proc of [chrome, owner, readonly]) { try { proc.kill(); } catch (_) {} }
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (_) {}
  }
}
run().catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
