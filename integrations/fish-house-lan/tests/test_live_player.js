"use strict";
// Isolated real-Chrome test: serves only the workspace UI and an in-memory /ws fixture.
const fs = require("fs"), os = require("os"), path = require("path"), { spawn, spawnSync } = require("child_process");
const ROOT = path.resolve(__dirname, "../web");
const OUT = process.env.FISH_HOUSE_TEST_REPORT || path.join(os.tmpdir(), "fish-house-live-player-report.json");
const PORT = Number(process.env.FISH_HOUSE_TEST_PORT || 18931), CDP = Number(process.env.FISH_HOUSE_TEST_CDP || 9239);
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const sleep = ms => new Promise(r => setTimeout(r, ms));
const py = String.raw`import asyncio,json,os
from aiohttp import web
root=os.environ['LIVE_WEB_ROOT']; clients=set(); quiet_reconnect=[False]
with open(os.path.join(root,'fish_house/themes/default/theme.json'),encoding='utf-8') as f: clone=json.load(f)
clone['meta']['id']='live-test-clone'
retry_theme=json.loads(json.dumps(clone)); retry_theme['meta']['id']='live-test-retry'
retry_node=next(n for n in retry_theme['config']['flow']['nodes'] if n['id']=='phone_pickup')
retry_source=retry_node['video']; retry_node['video']='videos/preload-once.mp4'; retry_asset_count=[0]
poison=json.loads(json.dumps(clone)); poison['meta']['id']='live-test-poison'
wait_node=next((n for n in poison['config']['flow']['nodes'] if n.get('role')=='wait' and n.get('loop')),None)
wait_node['video']='https://evil.example/hidden.mp4'
rekeyed=json.loads(json.dumps(clone)); rekeyed['meta']['id']='live-test-rekeyed'
idmap={n['id']:('graph_node_%02d'%i) for i,n in enumerate(rekeyed['config']['flow']['nodes'])}
for n in rekeyed['config']['flow']['nodes']: n['id']=idmap[n['id']]
for e in rekeyed['config']['flow']['edges']: e['from']=idmap[e['from']]; e['to']=idmap[e['to']]
for item in rekeyed['config']['actions']: item['key']=idmap.get(item.get('key'),item.get('key'))
for key in ('videos','audio','durations'): rekeyed['config'][key]={idmap.get(k,k):v for k,v in rekeyed['config'][key].items()}
async def live(req): raise web.HTTPFound('/fish_house/live.html'+(('?'+req.query_string) if req.query_string else ''))
async def ws_route(req):
 w=web.WebSocketResponse(); await w.prepare(req); clients.add(w)
 if not quiet_reconnect[0]:
  await w.send_json({'type':'state','seq':1,'ts':int(__import__('time').time()),'source':'qq','phase':'idle','node_id':'rest','bubble_text':'','ttl_ms':0})
 try:
  while not w.closed:
   msg=await w.receive()
   if msg.type in (web.WSMsgType.CLOSE,web.WSMsgType.CLOSED,web.WSMsgType.ERROR): break
 finally: clients.discard(w)
 return w
async def inject(req):
 item=await req.json()
 for client in list(clients):
  if not client.closed: await client.send_json(item)
 return web.json_response({'queued':True})
async def disconnect(req):
 quiet_reconnect[0]=req.query.get('quiet')=='1'
 for w in list(clients): await w.close()
 return web.json_response({'closed':True})
async def ws_stats(req): return web.json_response({'clients':len(clients)})
async def favicon(req): return web.Response(status=204)
async def theme_clone(req): return web.json_response(clone)
async def theme_retry(req): return web.json_response(retry_theme)
async def retry_asset(req):
 retry_asset_count[0]+=1
 if retry_asset_count[0]==1: return web.Response(status=503)
 return web.FileResponse(os.path.join(root,'fish_house/themes/default',retry_source))
async def retry_count(req): return web.json_response({'count':retry_asset_count[0]})
async def theme_poison(req): return web.json_response(poison)
async def theme_rekeyed(req): return web.json_response(rekeyed)
async def theme_selection(req): return web.json_response({'theme_url':'/fish_house/themes/default/theme.json'})
async def clone_asset(req):
 name=req.match_info['name']; file=os.path.realpath(os.path.join(root,'fish_house/themes/default',name))
 base=os.path.realpath(os.path.join(root,'fish_house/themes/default'))
 if not file.startswith(base+os.sep) or not os.path.isfile(file): raise web.HTTPNotFound()
 return web.FileResponse(file)
app=web.Application(); app.router.add_get('/live',live); app.router.add_get('/ws',ws_route); app.router.add_get('/ws-stats',ws_stats); app.router.add_get('/retry-count',retry_count); app.router.add_get('/favicon.ico',favicon); app.router.add_post('/inject',inject); app.router.add_post('/disconnect',disconnect); app.router.add_get('/fish_house/live.theme.json',theme_selection); app.router.add_get('/fish_house/themes/cloned/theme.json',theme_clone); app.router.add_get('/fish_house/themes/retry/theme.json',theme_retry); app.router.add_get('/fish_house/themes/retry/videos/preload-once.mp4',retry_asset); app.router.add_get('/fish_house/themes/poison/theme.json',theme_poison); app.router.add_get('/fish_house/themes/rekeyed/theme.json',theme_rekeyed); app.router.add_get('/fish_house/themes/cloned/{name:.*}',clone_asset); app.router.add_get('/fish_house/themes/retry/{name:.*}',clone_asset); app.router.add_get('/fish_house/themes/poison/{name:.*}',clone_asset); app.router.add_get('/fish_house/themes/rekeyed/{name:.*}',clone_asset); app.router.add_static('/',root,show_index=False)
web.run_app(app,host='127.0.0.1',port=int(os.environ['LIVE_PORT']),access_log=None)`;
class CDPClient {
  constructor(url) { this.ws = new WebSocket(url); this.pending = new Map(); this.id = 0; this.errors = []; }
  async connect() { await new Promise((ok, no) => { this.ws.onopen = ok; this.ws.onerror = no; }); this.ws.onmessage = e => { const m = JSON.parse(String(e.data)); if (m.id && this.pending.has(m.id)) { const p = this.pending.get(m.id); this.pending.delete(m.id); m.error ? p.reject(Error(m.error.message)) : p.resolve(m.result); } if (m.method === "Runtime.exceptionThrown" || (m.method === "Log.entryAdded" && m.params.entry.level === "error")) this.errors.push(m); }; }
  send(method, params = {}) { return new Promise((resolve, reject) => { const id = ++this.id; this.pending.set(id, { resolve, reject }); this.ws.send(JSON.stringify({ id, method, params })); setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); reject(Error("CDP timeout " + method)); } }, 12000); }); }
  async js(expression) { const r = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }); if (r.exceptionDetails) throw Error(JSON.stringify(r.exceptionDetails)); return r.result && r.result.value; }
  close() { try { this.ws.close(); } catch (_) {} }
}
async function until(test, label, timeout = 12000) { const end = Date.now() + timeout; while (Date.now() < end) { const v = await test(); if (v) return v; await sleep(100); } throw Error("timeout: " + label); }
async function main() {
  const env = Object.assign({}, process.env, { LIVE_WEB_ROOT: ROOT, LIVE_PORT: String(PORT) });
  const server = spawn("python", ["-c", py], { env, stdio: "ignore" });
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "fh-live-chrome-"));
  const chrome = spawn(CHROME, ["--headless=new", "--no-sandbox", "--autoplay-policy=no-user-gesture-required", `--remote-debugging-port=${CDP}`, `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });
  const report = { status: "FAIL", assertions: [], runtimeConsoleErrors: 0, blackFrameSamples: 0 };
  const check = (name, ok, details) => { report.assertions.push({ name, pass: !!ok, details }); if (!ok) throw Error(name + ": " + details); };
  let cdp;
  try {
    await until(async () => { try { return (await fetch(`http://127.0.0.1:${CDP}/json/version`)).ok; } catch (_) { return false; } }, "Chrome CDP");
    const targets = await (await fetch(`http://127.0.0.1:${CDP}/json`)).json();
    cdp = new CDPClient(targets.find(t => t.type === "page").webSocketDebuggerUrl); await cdp.connect();
    await cdp.send("Page.enable"); await cdp.send("Runtime.enable"); await cdp.send("Log.enable");
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: `
      window.fhHoldFrame = !location.search;
      window.fhHeldFrames = [];
      const nativeFrame = HTMLVideoElement.prototype.requestVideoFrameCallback;
      HTMLVideoElement.prototype.requestVideoFrameCallback = function (callback) {
        return nativeFrame.call(this, (time, metadata) => {
          if (window.fhHoldFrame) window.fhHeldFrames.push(() => callback(time, metadata));
          else callback(time, metadata);
        });
      };
      window.fhControlledFrameCallback = HTMLVideoElement.prototype.requestVideoFrameCallback;
    ` });
    await cdp.send("Page.navigate", { url: `http://127.0.0.1:${PORT}/live` });
    await until(() => cdp.js("[...document.querySelectorAll('video')].some(v => !v.paused && v.currentTime > .1)"),
      "initial video started before the first-frame notification");
    await sleep(1300);
    const delayedStart = await cdp.js(`({
      node: window.__fishHouseLive.getState().currentNode,
      cover: document.querySelector('#cover').classList.contains('cover-hidden'),
      active: [...document.querySelectorAll('video')].some(v => v.style.opacity === '1'),
    })`);
    check("startup retains cover until a video frame is presented",
      !delayedStart.node && !delayedStart.cover && !delayedStart.active, JSON.stringify(delayedStart));
    await cdp.js("window.fhHoldFrame=false; window.fhHeldFrames.splice(0).forEach(release => release())");
    await until(() => cdp.js("window.__fishHouseLive && window.__fishHouseLive.getState().currentNode === 'rest'"), "idle theme playback");
    const defaultTheme = await (await fetch(`http://127.0.0.1:${PORT}/fish_house/themes/default/theme.json`)).json();
    const route = (from, to, theme = defaultTheme) => { const q=[[from]], seen={}; while(q.length){const p=q.shift(),tail=p[p.length-1];if(tail===to)return p;if(seen[tail])continue;seen[tail]=true;(theme.config.flow.edges||[]).filter(e=>e.from===tail).forEach(e=>q.push(p.concat([e.to])));}return []; };
    const defaultIdle = defaultTheme.config.flow.nodes.find(n=>n.role==="wait"&&n.loop) || defaultTheme.config.flow.nodes.find(n=>n.role==="wait");
    const defaultPhone = defaultTheme.config.flow.nodes.find(n=>["phone","phone_only","both"].includes(n.role)&&n.loop) || defaultTheme.config.flow.nodes.find(n=>["phone","phone_only","both"].includes(n.role));
    const defaultPickup = defaultTheme.config.flow.nodes.find(n=>n.id===route(defaultIdle.id,defaultPhone.id)[1]);
    const defaultPutdown = route(defaultPhone.id, defaultIdle.id)[1];
    const putdownVideo = defaultTheme.config.flow.nodes.find(n => n.id === defaultPutdown).video;
    const defaultPickupUrl = `http://127.0.0.1:${PORT}/fish_house/themes/default/${defaultPickup.video}`;
    await until(() => cdp.js(`(function(){var v=[...document.querySelectorAll('video')].find(x=>x.currentSrc===${JSON.stringify(defaultPickupUrl)});return v&&v.readyState>=2&&v.paused&&v.style.opacity==='0'?true:null})()`), "default theme's first pickup video preloads while idle");
    await sleep(500);
    const defaultPreload = await cdp.js(`(function(){var v=[...document.querySelectorAll('video')].find(x=>x.currentSrc===${JSON.stringify(defaultPickupUrl)});return v&&{ready:v.readyState,paused:v.paused,preload:v.preload}})()`);
    check("idle playback preloads exactly the first graph node into the hidden inactive video", defaultPreload.ready>=2 && defaultPreload.paused && defaultPreload.preload==="auto", JSON.stringify(defaultPreload));
    let m = await cdp.js("({id:window.__fishHouseLive.getState().themeId,current:window.__fishHouseLive.getState().currentNode,videos:[...document.querySelectorAll('video')].map(v=>({paused:v.paused,ready:v.readyState,src:v.currentSrc,opacity:getComputedStyle(v).opacity,time:v.currentTime,muted:v.muted}))})");
    check("/live route, source theme ID and actual muted video playback", m.id === "7ce22364d7" && m.videos.some(v => !v.paused && v.ready >= 2 && v.src.includes("themes/default/videos/") && v.muted), JSON.stringify(m));
    const frames = await until(async () => { const f = await cdp.js("(function(){var v=[...document.querySelectorAll('video')].filter(x=>!x.paused&&x.readyState>=2)[0];if(!v)return null;var c=document.createElement('canvas');c.width=64;c.height=64;var x=c.getContext('2d');x.drawImage(v,0,0,64,64);var d=x.getImageData(0,0,64,64).data,n=0;for(var i=0;i<d.length;i+=4)if(d[i]+d[i+1]+d[i+2]>24)n++;return {n:n,time:v.currentTime,muted:v.muted}})()"); return f && f.time > .45 ? f : false; }, "video frame advancement");
    report.blackFrameSamples++; check("decoded frame is non-black and autoplay is muted", frames.n > 100 && frames.muted, JSON.stringify(frames));
    check("same-origin theme cover is loaded and replaced after video frame", await until(() =>
      cdp.js("document.getElementById('cover').naturalWidth > 0 && getComputedStyle(document.getElementById('cover')).opacity === '0'"),
      "cover fades once the video is visible"), "cover fallback is removed after first decoded frame");
    const send = async (seq, phase, source = "qq", type = "state", node = "", ttl = 0, bubbleText = "") => {
      const msg = { type, seq, ts: Math.floor(Date.now()/1000), source, phase, node_id: node, bubble_text: bubbleText, ttl_ms: ttl };
      await fetch(`http://127.0.0.1:${PORT}/inject`, { method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(msg) });
    };
    let seq = 2;
    await cdp.js("window.__fhLiveNodes=[];window.__fhLivePrevious='';window.__fhLiveTraceTimer=setInterval(function(){var n=window.__fishHouseLive.getState().currentNode;if(n&&n!==window.__fhLivePrevious){window.__fhLiveNodes.push(n);window.__fhLivePrevious=n;}},40)");
    const preloadResourceCount = await cdp.js(`performance.getEntriesByType('resource').filter(e=>e.name===${JSON.stringify(defaultPickupUrl)}).length`);
    await cdp.js(`(function () {
      const play = HTMLMediaElement.prototype.play;
      window.fhOriginalPlay = play;
      HTMLMediaElement.prototype.play = function () {
        if (this.src.endsWith('/' + ${JSON.stringify(defaultPhone.video)})) {
          this.playbackRate = 2;
          window.fhQueuedLoop = { start: performance.now(), duration: this.duration };
        }
        if (window.fhQueuedLoop && this.src.endsWith('/' + ${JSON.stringify(putdownVideo)})) {
          window.fhQueuedLoop.visibleMs = performance.now() - window.fhQueuedLoop.start;
        }
        return play.call(this);
      };
    })()`);
    await cdp.js("window.fhHoldFrame=true");
    const fastReceived = seq++; await send(fastReceived, "received");
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().lastSeq")) === fastReceived, "fast received accepted");
    const delayedPickup = await until(() => cdp.js(`(function () {
      const incoming = [...document.querySelectorAll('video')].find(v => v.currentSrc === ${JSON.stringify(defaultPickupUrl)});
      if (!incoming || incoming.paused || incoming.currentTime <= .05) return null;
      const outgoing = [...document.querySelectorAll('video')].find(v => v !== incoming);
      return {node:window.__fishHouseLive.getState().currentNode, incoming:incoming.style.opacity,
        outgoing:outgoing.style.opacity};
    })()`), "incoming pickup runs while frame notification is held");
    check("transition retains the outgoing frame until incoming presentation",
      delayedPickup.node === defaultIdle.id && delayedPickup.incoming === "0" && delayedPickup.outgoing === "1",
      JSON.stringify(delayedPickup));
    await cdp.js("window.fhHoldFrame=false; window.fhHeldFrames.splice(0).forEach(release => release())");
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().currentNode")) === defaultPickup.id, "preloaded pickup becomes active");
    const pickupActivation = await cdp.js(`(function(){var v=[...document.querySelectorAll('video')].find(x=>x.currentSrc===${JSON.stringify(defaultPickupUrl)});return {found:!!v,paused:v&&v.paused,opacity:v&&v.style.opacity,requests:performance.getEntriesByType('resource').filter(e=>e.name===${JSON.stringify(defaultPickupUrl)}).length}})()`);
    check("first pickup activation reuses the preloaded video request", pickupActivation.found && !pickupActivation.paused && pickupActivation.opacity === "1" && pickupActivation.requests === preloadResourceCount, JSON.stringify({active:pickupActivation.found&&!pickupActivation.paused, opacity:pickupActivation.opacity, requestsBefore:preloadResourceCount, requestsAfter:pickupActivation.requests}));
    const sentSeq = seq++;
    await send(sentSeq, "sent", "qq", "state", "", 20000, "夹具已发送");
    await until(() => cdp.js(`window.__fishHouseLive.getState().lastSeq === ${sentSeq}
        && document.getElementById('bubble').textContent === '夹具已发送'
        && !document.getElementById('bubble').classList.contains('hidden')`), "fast sent feedback while the animation finishes");
    const samples = [], sampleDir = fs.mkdtempSync(path.join(os.tmpdir(), "fh-live-transition-")), sampleEnd = Date.now() + 25000;
    let completed = false;
    while (Date.now() < sampleEnd) {
      const shot = await cdp.send("Page.captureScreenshot", { format:"png" }), file = path.join(sampleDir, `frame-${samples.length}.png`);
      fs.writeFileSync(file, Buffer.from(shot.data,"base64")); samples.push(file);
      const trace = await cdp.js("window.__fhLiveNodes");
      if (trace.includes("phone_pickup") && trace.includes("phone_loop") && trace.includes("phone_putdown") && trace[trace.length-1] === "rest") { completed = true; break; }
      await sleep(350);
    }
    check("Chromium samples captured across fast transition", samples.length >= 5, `samples=${samples.length}`);
    const scan = spawnSync("python", ["-c", "import json,sys; from PIL import Image; out=[]; \nfor f in sys.argv[1:]:\n im=Image.open(f).convert('RGB'); c=im.crop((im.width//2-32,im.height//2-32,im.width//2+32,im.height//2+32)); n=sum(1 for r,g,b in c.getdata() if r+g+b>24); out.append({'file':f,'lit_pixels':n,'total':4096,'black':n<100});\nprint(json.dumps({'samples':len(out),'black_frames':sum(1 for x in out if x['black']),'frames':out}))", ...samples], { encoding:"utf8" });
    if (scan.status !== 0) throw Error("transition sample pixel scan failed: " + scan.stderr);
    const pixelScan = JSON.parse(scan.stdout.trim()); report.blackFrameSamples = pixelScan.samples; report.sampledBlackFrames = pixelScan.black_frames;
    fs.rmSync(sampleDir, {recursive:true,force:true});
    check("no black transition frames sampled in Chromium", pixelScan.black_frames === 0, `sampled=${pixelScan.samples}; black=${pixelScan.black_frames}`);
    check("fast received then sent shows pickup, phone pose, putdown, and idle", completed, JSON.stringify(await cdp.js("window.__fhLiveNodes")));
    const queuedLoop = await cdp.js("window.fhQueuedLoop");
    check("a fast queued reply shows the phone pose without waiting for a full cycle",
      queuedLoop && queuedLoop.visibleMs >= 600 && queuedLoop.visibleMs < 2000,
      JSON.stringify(queuedLoop));
    await cdp.js("HTMLMediaElement.prototype.play = window.fhOriginalPlay");
    const sequences = [["received","phone_pickup"],["thinking","phone_pickup"],["replying","phone_putdown"],["sent","phone_putdown"],["error","rest"],["idle","rest"]];
    for (const [phase, expected] of sequences) { await send(seq++, phase, phase === "thinking" ? "wechat" : "qq"); if (phase === "thinking") await sleep(1000); else await until(() => cdp.js(`window.__fishHouseLive.getState().currentNode === ${JSON.stringify(expected)}`), phase + " mapped node"); }
    check("all phase mappings accepted in order (received/thinking to phone, reply/sent to putdown, idle/error)", true, sequences.map(s=>s.join("→")).join(", "));
    const dedupeSeq = seq++; await send(dedupeSeq, "received"); await until(async () => (await cdp.js("window.__fishHouseLive.getState().lastSeq")) === dedupeSeq, "received seq"); await send(dedupeSeq, "thinking", "wechat"); await sleep(300); const afterDupSeq = await cdp.js("window.__fishHouseLive.getState().lastSeq");
    check("duplicate sequence ignored", afterDupSeq === dedupeSeq, `accepted seq remains ${afterDupSeq}`);
    await fetch(`http://127.0.0.1:${PORT}/inject`, { method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({type:"bubble",seq:seq++,ts:Math.floor(Date.now()/1000),source:"qq",phase:"received",node_id:"",bubble_text:"正在处理",ttl_ms:1200}) });
    await until(async () => !(await cdp.js("document.getElementById('bubble').classList.contains('hidden')")), "bubble appears");
    check("default temporary event bubble appears", true, "bubble text is capped; ttl_ms is limited to three seconds");
    await until(async () => (await cdp.js("document.getElementById('bubble').classList.contains('hidden')")), "bubble ttl expires", 4000);
    const configFrame = (n, url, show) => ({type:"config",seq:n,ts:Math.floor(Date.now()/1000),source:"qq",phase:"idle",node_id:"",bubble_text:"",ttl_ms:0,config:{theme_url:url,show_bubble:show}});
    const invalidConfigSeq = seq++;
    await fetch(`http://127.0.0.1:${PORT}/inject`, { method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(configFrame(invalidConfigSeq,"https://evil.example/theme.json",true)) });
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().lastSeq")) === invalidConfigSeq, "config sequence accepted");
    await fetch(`http://127.0.0.1:${PORT}/inject`, { method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(configFrame(invalidConfigSeq,"/fish_house/themes/cloned/theme.json",true)) });
    await sleep(250);
    check("duplicate config seq ignored; external theme URL rejected", await cdp.js("window.__fishHouseLive.getState().themeId === '7ce22364d7'"), "same-origin allowlist and config de-duplication");
    const cloneTheme = await (await fetch(`http://127.0.0.1:${PORT}/fish_house/themes/cloned/theme.json`)).json();
    const cloneCmp = JSON.parse(JSON.stringify(cloneTheme)), defaultCmp = JSON.parse(JSON.stringify(defaultTheme));
    cloneCmp.meta.id = defaultCmp.meta.id;
    check("test theme clone differs only by theme ID", JSON.stringify(cloneCmp) === JSON.stringify(defaultCmp), "videos, node roles, IDs and graph edges are byte-equivalent JSON");
    const graphPath = (from, to) => { const q=[[from]], seen={}; while(q.length){const p=q.shift(),tail=p[p.length-1];if(tail===to)return p;if(seen[tail])continue;seen[tail]=true;(cloneTheme.config.flow.edges||[]).filter(e=>e.from===tail).forEach(e=>q.push(p.concat([e.to])));}return []; };
    const nodes = cloneTheme.config.flow.nodes, waitNode = nodes.find(n=>n.role==="wait"&&n.loop) || nodes.find(n=>n.role==="wait");
    const phoneNode = nodes.find(n=>["phone","phone_only","both"].includes(n.role)&&n.loop) || nodes.find(n=>["phone","phone_only","both"].includes(n.role));
    const pickupNode = nodes.find(n=>n.id===graphPath(waitNode.id,phoneNode.id)[1]);
    const putdownNode = nodes.find(n=>n.id===graphPath(phoneNode.id,waitNode.id)[1]);
    const cloneConfigSeq = seq++;
    await fetch(`http://127.0.0.1:${PORT}/inject`, { method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(configFrame(cloneConfigSeq,"/fish_house/themes/cloned/theme.json",false)) });
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().themeId")) === "live-test-clone", "same-origin cloned theme load");
    await until(async () => (await cdp.js("[...document.querySelectorAll('video')].some(v=>!v.paused&&v.readyState>=2&&v.currentSrc.includes('/themes/cloned/videos/'))")), "cloned theme local video playback");
    const clonePickupUrl = `http://127.0.0.1:${PORT}/fish_house/themes/cloned/${pickupNode.video}`;
    const clonePreload = await until(() => cdp.js(`(function(){var v=[...document.querySelectorAll('video')].find(x=>x.currentSrc===${JSON.stringify(clonePickupUrl)});return v&&v.readyState>=2&&v.paused&&v.style.opacity==='0'?{ready:v.readyState,paused:v.paused}:null})()`), "new theme preloads its own first pickup video");
    check("theme switch clears old buffers and preloads the selected theme's pickup", clonePreload.ready>=2 && clonePreload.paused, JSON.stringify(clonePreload));
    check("renamed theme ID uses theme graph roles and same-origin video paths", pickupNode && putdownNode && await cdp.js("window.__fishHouseLive.getState().idleNode") === waitNode.id && await cdp.js("window.__fishHouseLive.getState().phoneNode") === phoneNode.id, `idle=${waitNode.id}, phone=${phoneNode.id}, pickup=${pickupNode.id}, putdown=${putdownNode.id}`);
    await cdp.js("window.__fhLiveNodes=[];window.__fhLivePrevious='';");
    const cloneReceivedSeq = seq++;
    await send(cloneReceivedSeq,"received","qq","flow_goto",pickupNode.id);
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().lastSeq")) === cloneReceivedSeq, "flow_goto pickup accepted");
    await send(seq++,"sent","qq","flow_goto",putdownNode.id);
    await until(async () => { const t=await cdp.js("window.__fhLiveNodes"); return t.includes(pickupNode.id)&&t.includes(phoneNode.id)&&t.includes(putdownNode.id)&&t[t.length-1]===waitNode.id; }, "fast flow_goto pickup→phone→putdown→idle", 25000);
    check("fast received-pickup then sent-putdown flow_goto completes clone graph path", true, JSON.stringify(await cdp.js("window.__fhLiveNodes")));
    const orderedSeq = seq++;
    const orderedFrame = {type:"flow_goto",seq:orderedSeq,ts:Math.floor(Date.now()/1000),event_at:Date.now()/1000,source:"qq",phase:"sent",node_id:pickupNode.id,bubble_text:"",ttl_ms:0,orders:[waitNode.id,pickupNode.id,phoneNode.id,putdownNode.id,waitNode.id].map(node_id=>({node_id}))};
    await cdp.js(`window.__fishHouseLive.apply(${JSON.stringify(orderedFrame)}, false)`);
    await cdp.js(`window.__fishHouseLive.apply(${JSON.stringify(Object.assign({},orderedFrame,{type:"state",seq:seq++}))}, false)`);
    const orderedState = await cdp.js("window.__fishHouseLive.getState()");
    check("ordered graph orders play adjacent nodes without expanding or duplicating a fast repeat", orderedState.pendingNode === pickupNode.id && JSON.stringify(orderedState.queued) === JSON.stringify([phoneNode.id,putdownNode.id,waitNode.id]), JSON.stringify(orderedState));
    const secondReceipt = { ...orderedFrame, source: "wechat", seq: seq++ };
    await cdp.js(`window.__fishHouseLive.apply(${JSON.stringify(secondReceipt)}, false)`);
    await cdp.js(`window.__fishHouseLive.apply(${JSON.stringify({ ...secondReceipt, type: "state", seq: seq++ })}, false)`);
    const twoReceipts = await cdp.js("window.__fishHouseLive.getState()");
    const secondPath = [pickupNode.id, phoneNode.id, putdownNode.id, waitNode.id];
    check("confirmed receipts from both platforms queue separately even with the same timestamp",
      JSON.stringify(twoReceipts.queued.slice(-4)) === JSON.stringify(secondPath)
      && twoReceipts.queued.filter(id => id === putdownNode.id).length === 2, JSON.stringify(twoReceipts));
    await until(async () => {
      const state = await cdp.js("window.__fishHouseLive.getState()");
      return state.currentNode === waitNode.id && !state.pendingNode && !state.queued.length;
    }, "both ordered action cycles complete before reconnect", 40000);
    check("bubble can be disabled and cloned config remains live", await cdp.js("window.__fishHouseLive.getState().themeId === 'live-test-clone' && getComputedStyle(document.getElementById('bubble')).opacity === '0'"), "theme loaded, bubble disabled");
    const beforeHome = await (await fetch(`http://127.0.0.1:${PORT}/fish_house/home.html`)).status;
    check("existing home.html remains available", beforeHome === 200, "HTTP " + beforeHome);
    await fetch(`http://127.0.0.1:${PORT}/disconnect`, { method:"POST" });
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().lastSeq")) === 1, "reconnect snapshot resets sequence");
    await send(2, "idle", "wechat");
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().currentNode")) === "rest", "post-reconnect idle event");
    await send(3, "thinking", "qq", "flow_goto", pickupNode.id);
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().currentNode")) === pickupNode.id, "flow_goto real theme node");
    check("WS reconnect resets sequence; flow_goto follows a real theme edge", true, "new socket snapshot seq=1, then state seq=2 and graph edge seq=3");
    await cdp.send("Page.navigate", { url:`http://127.0.0.1:${PORT}/live?bubble=0` });
    await until(async () => (await cdp.js("window.__fishHouseLive && window.__fishHouseLive.getState().themeId")) === "7ce22364d7", "query bubble override boot");
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().lastSeq")) === 1, "query page snapshot");
    await until(async () => (await (await fetch(`http://127.0.0.1:${PORT}/ws-stats`)).json()).clients >= 1, "active query-page websocket");
    await fetch(`http://127.0.0.1:${PORT}/inject`, { method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(configFrame(2,"/fish_house/themes/cloned/theme.json",true)) });
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().lastSeq")) === 2, "query page config sequence");
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().themeId")) === "live-test-clone", "query page cloned config");
    await fetch(`http://127.0.0.1:${PORT}/inject`, { method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({type:"bubble",seq:3,ts:Math.floor(Date.now()/1000),source:"qq",phase:"received",node_id:"",bubble_text:"隐藏测试",ttl_ms:1200}) });
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().lastSeq")) === 3, "bubble query frame accepted");
    check("/live?bubble=0 keeps bubbles hidden despite config enable and positive event TTL", await cdp.js("document.getElementById('bubble').classList.contains('hidden') && getComputedStyle(document.getElementById('bubble')).opacity === '0'"), "query override survives cloned theme config and event");
    await fetch(`http://127.0.0.1:${PORT}/inject`, { method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(configFrame(4,"/fish_house/themes/poison/theme.json",true)) });
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().themeId")) === "live-test-poison", "poison theme metadata load");
    await sleep(300);
    const poisonMetrics = await cdp.js("({state:window.__fishHouseLive.getState(),videos:[...document.querySelectorAll('video')].map(v=>v.currentSrc),coverOpacity:Number(getComputedStyle(document.getElementById('cover')).opacity),coverWidth:document.getElementById('cover').naturalWidth})");
    check("theme with external MP4 is blocked and same-origin cover remains as fallback", poisonMetrics.state.currentNode === "" && poisonMetrics.state.pendingNode === "" && poisonMetrics.videos.every(src=>!src.includes('evil.example')) && poisonMetrics.coverOpacity > .9 && poisonMetrics.coverWidth > 0, JSON.stringify(poisonMetrics));
    const rekeyTheme = await (await fetch(`http://127.0.0.1:${PORT}/fish_house/themes/rekeyed/theme.json`)).json();
    const rekeyNodes = rekeyTheme.config.flow.nodes, rekeyIdle = rekeyNodes.find(n=>n.role==="wait"&&n.loop) || rekeyNodes.find(n=>n.role==="wait");
    const rekeyPhone = rekeyNodes.find(n=>["phone","phone_only","both"].includes(n.role)&&n.loop) || rekeyNodes.find(n=>["phone","phone_only","both"].includes(n.role));
    const rekeyPath = (from,to) => { const q=[[from]], seen={}; while(q.length){const p=q.shift(),tail=p[p.length-1];if(tail===to)return p;if(seen[tail])continue;seen[tail]=true;(rekeyTheme.config.flow.edges||[]).filter(e=>e.from===tail).forEach(e=>q.push(p.concat([e.to])));}return []; };
    const rekeyPickup = rekeyNodes.find(n=>n.id===rekeyPath(rekeyIdle.id,rekeyPhone.id)[1]);
    const rekeyPutdown = rekeyNodes.find(n=>n.id===rekeyPath(rekeyPhone.id,rekeyIdle.id)[1]);
    const originalIds = defaultTheme.config.flow.nodes.map(n=>n.id);
    check("rekeyed fixture changes node and edge IDs but preserves roles and video assets", rekeyNodes.every(n=>!originalIds.includes(n.id)&&/^graph_node_/.test(n.id)) && rekeyNodes.every((n,i)=>n.role===defaultTheme.config.flow.nodes[i].role&&rekeyTheme.config.videos[n.id]===defaultTheme.config.videos[defaultTheme.config.flow.nodes[i].id]), `theme=${rekeyTheme.meta.id}; nodes=${rekeyNodes.map(n=>n.id).join(',')}`);
    await cdp.js("Object.defineProperty(navigator,'connection',{configurable:true,value:{saveData:true}})");
    await fetch(`http://127.0.0.1:${PORT}/inject`, { method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(configFrame(5,"/fish_house/themes/rekeyed/theme.json",false)) });
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().themeId")) === "live-test-rekeyed", "renamed-node theme loaded");
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().currentNode")) === rekeyIdle.id, "renamed-node idle video plays");
    await sleep(400);
    check("Save-Data mode suppresses inactive pickup preload after theme switch", await cdp.js(`![...document.querySelectorAll('video')].some(v=>v.currentSrc.includes(${JSON.stringify('/themes/rekeyed/' + rekeyPickup.video)}))`), "selected theme idle video plays; pickup remains unfetched");
    await cdp.js("Object.defineProperty(navigator,'connection',{configurable:true,value:{saveData:false}})");
    check("renamed node IDs are chosen from graph roles", rekeyIdle && rekeyPhone && rekeyPickup && rekeyPutdown && await cdp.js("window.__fishHouseLive.getState().idleNode") === rekeyIdle.id && await cdp.js("window.__fishHouseLive.getState().phoneNode") === rekeyPhone.id, `idle=${rekeyIdle.id},phone=${rekeyPhone.id},pickup=${rekeyPickup.id},putdown=${rekeyPutdown.id}`);
    await cdp.js("window.__fhRekeyTrace=[];window.__fhRekeyLast='';window.__fhRekeyTimer=setInterval(function(){var s=window.__fishHouseLive.getState(),n=s.currentNode;if(n&&n!==window.__fhRekeyLast){window.__fhRekeyLast=n;var v=[...document.querySelectorAll('video')].find(x=>!x.paused&&x.style.zIndex==='2');window.__fhRekeyTrace.push({node:n,src:v&&v.currentSrc,ready:v&&v.readyState,time:v&&v.currentTime});}},40)");
    await send(6,"received","qq","flow_goto",rekeyPickup.id);
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().lastSeq")) === 6, "rekey received flow_goto accepted");
    await send(7,"sent","qq","flow_goto",rekeyPutdown.id);
    await until(async () => {const t=await cdp.js("window.__fhRekeyTrace");return t.some(x=>x.node===rekeyPickup.id)&&t.some(x=>x.node===rekeyPhone.id)&&t.some(x=>x.node===rekeyPutdown.id)&&t[t.length-1].node===rekeyIdle.id;}, "renamed-node flow_goto pickup→phone→putdown→idle", 25000);
    const rekeyMetrics = await cdp.js("window.__fhRekeyTrace");
    check("non-rest/non-phone node names drive real idle and fast flow_goto playback", rekeyMetrics.filter(x=>["graph_node_00","graph_node_01","graph_node_02","graph_node_03","graph_node_04","graph_node_05","graph_node_06"].includes(x.node)).length>=4&&rekeyMetrics.filter(x=>x.ready>=2&&x.time>0&&x.src&&x.src.includes('/themes/rekeyed/videos/')).length>=4, JSON.stringify(rekeyMetrics));
    // Regression: a one-shot action can end while the queue is empty. A later
    // real ordered WS event must start its route immediately; there will be no
    // second `ended` callback to drain the new queue.
    await cdp.js(`window.__fishHouseLive.playNode(${JSON.stringify(rekeyPutdown.id)})`);
    await until(async () => cdp.js(`window.__fishHouseLive.getState().currentNode === ${JSON.stringify(rekeyPutdown.id)} && [...document.querySelectorAll('video')].some(v=>!v.paused && v.readyState>=2)`), "late-order one-shot playback starts");
    await until(async () => cdp.js(` [...document.querySelectorAll('video')].some(v=>v.currentSrc.includes(${JSON.stringify('/themes/rekeyed/' + rekeyPutdown.video)}) && v.ended)`), "late-order one-shot reaches ended", 12000);
    const lateSeq = 8;
    const lateFrame = {type:"flow_goto",seq:lateSeq,ts:Math.floor(Date.now()/1000),event_at:Date.now()/1000,source:"wechat",phase:"idle",node_id:rekeyIdle.id,bubble_text:"",ttl_ms:0,orders:[{node_id:rekeyIdle.id}]};
    await fetch(`http://127.0.0.1:${PORT}/inject`, { method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(lateFrame) });
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().lastSeq")) === lateSeq, "late ordered event accepted");
    const lateSamples = [], lateDir = fs.mkdtempSync(path.join(os.tmpdir(), "fh-live-late-tail-"));
    let lateComplete = false;
    const lateEnd = Date.now() + 16000;
    while (Date.now() < lateEnd) {
      const shot = await cdp.send("Page.captureScreenshot", { format:"png" });
      const file = path.join(lateDir, `frame-${lateSamples.length}.png`);
      fs.writeFileSync(file, Buffer.from(shot.data,"base64")); lateSamples.push(file);
      if (await cdp.js(`window.__fishHouseLive.getState().currentNode === ${JSON.stringify(rekeyIdle.id)} && !window.__fishHouseLive.getState().queued.length`)) { lateComplete = true; break; }
      await sleep(220);
    }
    const lateScan = spawnSync("python", ["-c", "from PIL import Image; import sys; print(sum(1 for f in sys.argv[1:] if Image.open(f).convert('L').crop((Image.open(f).width//3,Image.open(f).height//3,Image.open(f).width*2//3,Image.open(f).height*2//3)).getextrema()[1] < 12))", ...lateSamples], { encoding:"utf8" });
    if (lateScan.status !== 0) throw Error("late tail frame scan failed: " + lateScan.stderr);
    const lateBlack = Number(lateScan.stdout.trim()); fs.rmSync(lateDir, {recursive:true,force:true});
    check("late ordered WS event after one-shot ended reaches wait", lateComplete, JSON.stringify(await cdp.js("window.__fishHouseLive.getState()")));
    check("late ended-to-wait tail has no sampled black frames", lateSamples.length >= 1 && lateBlack === 0, `samples=${lateSamples.length}; black=${lateBlack}`);
    await cdp.js(`window.__fishHouseLive.playNode(${JSON.stringify(rekeyPutdown.id)})`);
    await until(async () => cdp.js(`window.__fishHouseLive.getState().currentNode === ${JSON.stringify(rekeyPutdown.id)} && [...document.querySelectorAll('video')].some(v=>!v.paused && v.readyState>=2)`), "late fallback one-shot playback starts");
    await until(async () => cdp.js(` [...document.querySelectorAll('video')].some(v=>v.currentSrc.includes(${JSON.stringify('/themes/rekeyed/' + rekeyPutdown.video)}) && v.ended)`), "late fallback one-shot reaches ended", 12000);
    const fallbackSeq = 9;
    await fetch(`http://127.0.0.1:${PORT}/inject`, { method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({type:"state",seq:fallbackSeq,ts:Math.floor(Date.now()/1000),source:"wechat",phase:"idle",node_id:rekeyIdle.id,bubble_text:"",ttl_ms:0}) });
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().lastSeq")) === fallbackSeq, "late fallback state accepted");
    await until(async () => cdp.js(`window.__fishHouseLive.getState().currentNode === ${JSON.stringify(rekeyIdle.id)} && !window.__fishHouseLive.getState().queued.length`), "late fallback path reaches wait", 12000);
    check("late state fallback after one-shot ended reaches wait", true, `node=${rekeyIdle.id}`);
    const retryThemeSeq = 10;
    await fetch(`http://127.0.0.1:${PORT}/inject`, { method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(configFrame(retryThemeSeq,"/fish_house/themes/retry/theme.json",false)) });
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().themeId")) === "live-test-retry", "one-shot retry theme loaded");
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().currentNode")) === "rest", "retry theme idle playback");
    const retryUrl = `http://127.0.0.1:${PORT}/fish_house/themes/retry/videos/preload-once.mp4`;
    await until(async () => cdp.js(`(function(){var v=[...document.querySelectorAll('video')].find(x=>x.currentSrc===${JSON.stringify(retryUrl)});return v&&v.error?{code:v.error.code,paused:v.paused,opacity:v.style.opacity}:null})()`), "hidden first pickup preload receives the fixture's one-time 503");
    const failedPreloadState = await cdp.js("({node:window.__fishHouseLive.getState().currentNode,active:[...document.querySelectorAll('video')].filter(v=>!v.paused&&v.style.opacity==='1').length})");
    check("failed hidden preload leaves the existing idle video visible", failedPreloadState.node === "rest" && failedPreloadState.active === 1, JSON.stringify(failedPreloadState));
    await send(11,"received","qq","flow_goto","phone_pickup");
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().lastSeq")) === 11, "failed-preload pickup event accepted");
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().currentNode")) === "phone_pickup", "failed hidden preload reloads and plays on entry", 12000);
    const retryStats = await (await fetch(`http://127.0.0.1:${PORT}/retry-count`)).json();
    check("failed hidden preload is retried once when its node becomes active", retryStats.count >= 2 && await cdp.js(`([...document.querySelectorAll('video')].some(v=>v.currentSrc===${JSON.stringify(retryUrl)}&&!v.paused&&v.style.opacity==='1'))`), JSON.stringify(retryStats));
    await cdp.js("window.__fhNativePlay=HTMLMediaElement.prototype.play;HTMLMediaElement.prototype.play=function(){return Promise.reject(new DOMException('fixture rejection','NotAllowedError'))}");
    await fetch(`http://127.0.0.1:${PORT}/inject`, { method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(configFrame(12,"/fish_house/themes/cloned/theme.json",true)) });
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().themeId")) === "live-test-clone", "rejected play clone theme");
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().currentNode")) === "", "rejected play starts before checking pending");
    await until(async () => (await cdp.js("window.__fishHouseLive.getState().pendingNode")) === "", "rejected play clears pending");
    await until(async () => (await cdp.js("Number(getComputedStyle(document.getElementById('cover')).opacity) > .9")), "same-origin cover restored after rejection");
    const rejectedMetrics = await cdp.js("({state:window.__fishHouseLive.getState(),opacity:Number(getComputedStyle(document.getElementById('cover')).opacity),coverWidth:document.getElementById('cover').naturalWidth})");
    check("play rejection clears pending state and leaves same-origin cover visible", rejectedMetrics.state.currentNode === "" && rejectedMetrics.state.pendingNode === "" && rejectedMetrics.opacity > .9 && rejectedMetrics.coverWidth > 0, JSON.stringify(rejectedMetrics));
    await cdp.js("HTMLMediaElement.prototype.play=window.__fhNativePlay");
    await cdp.js("window.fhHoldFrame=true; window.__fishHouseLive.playNode('rest'); true");
    await until(() => cdp.js("window.__fishHouseLive.getState().pendingNode === 'rest'"), "stalled startup becomes pending");
    await until(() => cdp.js("window.__fishHouseLive.getState().pendingNode === ''"), "stalled frame reaches its deadline", 10000);
    await cdp.js("window.fhHoldFrame=false; window.fhHeldFrames.splice(0).forEach(release => release())");
    check("frame deadline keeps cover and ignores late notifications",
      await cdp.js("!document.querySelector('#cover').classList.contains('cover-hidden') && !window.__fishHouseLive.getState().currentNode && [...document.querySelectorAll('video')].every(v => !v.getAttribute('src'))"),
      "the cover stays visible and the failed media is released");
    await cdp.js("HTMLVideoElement.prototype.requestVideoFrameCallback=undefined; window.__fishHouseLive.playNode('rest'); true");
    await until(() => cdp.js("window.__fishHouseLive.getState().currentNode === 'rest'"), "legacy playback readiness");
    check("legacy frame readiness requires advancing real playback",
      await cdp.js("[...document.querySelectorAll('video')].some(v => v.style.opacity === '1' && v.currentTime > 0 && v.videoWidth > 0)"),
      "playback clock and decoded dimensions are both available");
    const themeUrl = name => `/fish_house/themes/${name}/theme.json`;
    const injectFrame = msg => fetch(`http://127.0.0.1:${PORT}/inject`, {
      method: "POST", headers: {"content-type":"application/json"}, body: JSON.stringify(msg)
    });
    await cdp.js(`(function () {
      var fetchTheme = window.fetch;
      window.fhHeldTheme = {};
      window.fhThemeToHold = 'rekeyed';
      window.fetch = function (url, options) {
        var name = window.fhThemeToHold;
        if (!name || !String(url).endsWith('/themes/' + name + '/theme.json')) return fetchTheme(url, options);
        return new Promise(function (resolve) {
          window.fhHeldTheme[name] = function () {
            var result = fetchTheme(url, options);
            delete window.fhHeldTheme[name];
            resolve(result);
            return result;
          };
        });
      };
    })()`);
    await injectFrame(configFrame(13, themeUrl("rekeyed"), true));
    await until(() => cdp.js("Boolean(window.fhHeldTheme.rekeyed)"), "theme request paused");
    await send(14, "thinking", "wechat", "state", rekeyPhone.id);
    await until(() => cdp.js("window.__fishHouseLive.getState().lastSeq === 14"), "state arrives before delayed theme");
    check("the existing theme remains visible during the request",
      await cdp.js("window.__fishHouseLive.getState().themeId === 'live-test-clone' && window.__fishHouseLive.getState().currentNode === 'rest'"),
      "the new graph is still pending");
    await cdp.js("HTMLVideoElement.prototype.requestVideoFrameCallback=window.fhControlledFrameCallback; window.fhHoldFrame=true; window.fhHeldTheme.rekeyed(); true");
    await until(() => cdp.js("window.__fishHouseLive.getState().themeId === 'live-test-rekeyed' && window.fhHeldFrames.length > 0"),
      "new theme's first presented frame paused");
    check("theme change retains the old video while the new frame is pending",
      await cdp.js("[...document.querySelectorAll('video')].some(v => v.currentSrc.includes('/themes/cloned/videos/') && v.style.opacity === '1' && v.readyState >= 2)"),
      "visible old-theme video remains available after new metadata loads");
    await cdp.js("window.fhHoldFrame=false; window.fhHeldFrames.splice(0).forEach(release => release())");
    await until(() => cdp.js(`window.__fishHouseLive.getState().currentNode === ${JSON.stringify(rekeyPhone.id)}`),
      "the state received during a theme load is played by the new graph");
    check("the retained state selects a decoded video in the new theme",
      await cdp.js("[...document.querySelectorAll('video')].some(v => v.currentSrc.includes('/themes/rekeyed/videos/') && !v.paused && v.readyState >= 2)"),
      `node=${rekeyPhone.id}`);

    await cdp.js("window.fhThemeToHold='cloned'");
    await injectFrame(configFrame(15, themeUrl("cloned"), true));
    await until(() => cdp.js("Boolean(window.fhHeldTheme.cloned)"), "obsolete theme request paused");
    await injectFrame(configFrame(16, themeUrl("retry"), true));
    await until(() => cdp.js("window.__fishHouseLive.getState().themeId === 'live-test-retry'"), "newer theme loads first");
    await cdp.js("window.fhHeldTheme.cloned().then(function () { return true; })");
    await sleep(400);
    check("a late response cannot replace the newer theme",
      await cdp.js("window.__fishHouseLive.getState().themeId === 'live-test-retry'"), "latest selection remains active");

    await injectFrame(configFrame(17, themeUrl("cloned"), true));
    await until(() => cdp.js("Boolean(window.fhHeldTheme.cloned)"), "theme request paused for expiry");
    await injectFrame({type:"state", seq:18, ts:Math.floor(Date.now()/1000), source:"qq", phase:"thinking",
      node_id:defaultPhone.id, bubble_text:"", ttl_ms:80});
    await until(() => cdp.js("window.__fishHouseLive.getState().lastSeq === 18"), "short-lived state delivered");
    await sleep(200);
    await cdp.js("window.fhHeldTheme.cloned(); true");
    await until(() => cdp.js("window.__fishHouseLive.getState().themeId === 'live-test-clone' && window.__fishHouseLive.getState().currentNode === 'rest'"),
      "cloned theme remains at idle after retained state expires");
    check("expired state cannot trigger an animation after a slow theme load",
      await cdp.js("(function(){var s=window.__fishHouseLive.getState(); return s.currentNode==='rest' && !s.pendingNode && !s.queued.length;})()"),
      "expired thinking event did not enter the queue");
    await cdp.js("window.__fishHouseLive.playNode(" + JSON.stringify(defaultPhone.id) + ")");
    await cdp.js(`new Promise(resolve => {
      const video = [...document.querySelectorAll('video')].find(v => v.style.zIndex === '2' && v.style.opacity === '1');
      window.fhActiveLoop = { ended: false, duration: video.duration };
      video.addEventListener('ended', () => { window.fhActiveLoop.ended = true; window.fhActiveLoop.mediaEnd = video.currentTime; }, { once: true });
      video.addEventListener('seeked', () => { window.fhActiveLoop.start = video.currentTime; resolve(); }, { once: true });
      video.playbackRate = 1;
      video.currentTime = video.duration - 0.8;
    })`);
    const nearStartedAt = Date.now();
    await injectFrame({type:"state", seq:19, ts:Math.floor(Date.now()/1000), source:"qq", phase:"sent",
      node_id:defaultPutdown, orders:[defaultPutdown,defaultIdle.id], bubble_text:"", ttl_ms:0});
    await until(() => cdp.js("window.__fishHouseLive.getState().currentNode === " + JSON.stringify(defaultPutdown)),
      "putdown begins after the active loop ends");
    const activeLoop = await cdp.js("window.fhActiveLoop");
    check("a phone loop near its tail uses the real media end pose",
      activeLoop.ended && activeLoop.duration - activeLoop.start < 1 && Math.abs(activeLoop.mediaEnd - activeLoop.duration) < 0.05,
      JSON.stringify({...activeLoop, putdownMs:Date.now()-nearStartedAt}));
    await until(() => cdp.js("window.__fishHouseLive.getState().currentNode === " + JSON.stringify(defaultIdle.id)),
      "ordered reply returns from putdown to idle");

    await cdp.js("window.__fishHouseLive.playNode(" + JSON.stringify(defaultPhone.id) + ")");
    await cdp.js(`new Promise(resolve => {
      const video = [...document.querySelectorAll('video')].find(v => v.style.zIndex === '2' && v.style.opacity === '1');
      window.fhFarPhone = video;
      video.addEventListener('seeked', resolve, { once: true });
      video.playbackRate = 1;
      video.currentTime = 0.75;
    })`);
    const farStartedAt = Date.now();
    await injectFrame({type:"state", seq:20, ts:Math.floor(Date.now()/1000), source:"qq", phase:"sent",
      node_id:defaultPutdown, orders:[defaultPutdown,defaultIdle.id], bubble_text:"", ttl_ms:0});
    await until(() => cdp.js("window.__fishHouseLive.getState().currentNode === " + JSON.stringify(defaultPutdown)),
      "a phone loop far from its tail starts putdown promptly", 2000);
    const farCut = await cdp.js(`(function () {
      const outgoing = window.fhFarPhone;
      const incoming = [...document.querySelectorAll('video')].find(v => v !== outgoing);
      return {outgoingTime:outgoing.currentTime, outgoingOpacity:Number(getComputedStyle(outgoing).opacity),
        duration:outgoing.duration, ended:outgoing.ended, fade:getComputedStyle(incoming).transitionDuration};
    })()`);
    check("fast mid-cycle exit overlaps two decoded videos before the end of the loop",
      farCut.outgoingOpacity === 1 && farCut.fade === "0.65s"
        && !farCut.ended && farCut.duration - farCut.outgoingTime > 3,
      JSON.stringify({...farCut, putdownMs:Date.now()-farStartedAt}));
    await until(() => cdp.js("window.__fishHouseLive.getState().currentNode === " + JSON.stringify(defaultIdle.id)),
      "mid-cycle crossfade returns to idle");

    await cdp.js("window.__fishHouseLive.playNode(" + JSON.stringify(defaultPhone.id) + ")");
    await cdp.js(`new Promise(resolve => {
      const video = [...document.querySelectorAll('video')].find(v => v.style.zIndex === '2' && v.style.opacity === '1');
      video.onended = null;
      window.fhSuppressedEnd = false;
      video.addEventListener('ended', () => { window.fhSuppressedEnd = true; }, { once: true });
      video.addEventListener('seeked', resolve, { once: true });
      video.playbackRate = 2;
      video.currentTime = video.duration - 0.6;
    })`);
    const recoveryStartedAt = Date.now();
    await injectFrame({type:"state", seq:21, ts:Math.floor(Date.now()/1000), source:"qq", phase:"sent",
      node_id:defaultPutdown, orders:[defaultPutdown,defaultIdle.id], bubble_text:"", ttl_ms:0});
    await until(() => cdp.js("window.__fishHouseLive.getState().currentNode === " + JSON.stringify(defaultPutdown)),
      "putdown recovers even when the native ended handler is unavailable", 2800);
    check("bounded recovery works when the ended callback is missing", await cdp.js("window.fhSuppressedEnd"),
      `putdown presented after ${Date.now()-recoveryStartedAt}ms`);
    await until(() => cdp.js("window.__fishHouseLive.getState().currentNode === " + JSON.stringify(defaultIdle.id)),
      "watchdog recovery returns to idle");

    await cdp.js("window.__fishHouseLive.playNode(" + JSON.stringify(defaultPhone.id) + ")");
    const errorFrame = {type:"state", seq:22, ts:Math.floor(Date.now()/1000), source:"qq", phase:"sent",
      node_id:defaultPutdown, orders:[defaultPutdown,defaultIdle.id], bubble_text:"", ttl_ms:0};
    await cdp.js(`(function () {
      window.__fishHouseLive.apply(${JSON.stringify(errorFrame)}, false);
      const video = [...document.querySelectorAll('video')].find(v => v.style.zIndex === '2' && v.style.opacity === '1');
      video.dispatchEvent(new Event('error'));
    })()`);
    await until(() => cdp.js("window.__fishHouseLive.getState().currentNode === " + JSON.stringify(defaultPutdown)),
      "media loss advances immediately to the next queued clip", 2200);
    check("a media error in the active phone cycle skips the wait and keeps the route", true, "the putdown video presented");
    await until(() => cdp.js("window.__fishHouseLive.getState().currentNode === " + JSON.stringify(defaultIdle.id)),
      "media error recovery returns to idle");

    const computerLoop = defaultTheme.config.flow.nodes.find(n => n.loop && n.role === "both");
    const fromComputer = route(computerLoop.id, defaultIdle.id).slice(1);
    await cdp.js("window.__fishHouseLive.playNode(" + JSON.stringify(computerLoop.id) + ")");
    const computerStartedAt = Date.now();
    await injectFrame({type:"state", seq:23, ts:Math.floor(Date.now()/1000), source:"qq", phase:"idle",
      node_id:fromComputer[0], orders:fromComputer, bubble_text:"", ttl_ms:0});
    await until(() => cdp.js("window.__fishHouseLive.getState().currentNode === " + JSON.stringify(fromComputer[0])),
      "computer transition does not wait for the long loop", 2200);
    check("the longer both-role loop keeps immediate transitions", true,
      `next video presented after ${Date.now()-computerStartedAt}ms`);
    await until(() => cdp.js("window.__fishHouseLive.getState().currentNode === " + JSON.stringify(defaultIdle.id)),
      "idle before stream outage");
    await send(24, "thinking", "wechat", "state", defaultPhone.id, 100000);
    await until(() => cdp.js("window.__fishHouseLive.getState().currentNode === " + JSON.stringify(defaultPhone.id)),
      "long generation reaches the phone loop");
    await fetch(`http://127.0.0.1:${PORT}/disconnect?quiet=1`, {method:"POST"});
    await until(() => cdp.js("window.__fishHouseLive.getState().currentNode === " + JSON.stringify(defaultPutdown)),
      "missing activity stream releases long generation", 7200);
    check("a silent reconnect ends a long activity using its exit clip", true,
      "the putdown clip presented before the 100-second activity timeout");
    const s = await cdp.send("Page.captureScreenshot", { format:"png" }); fs.writeFileSync(path.join(os.tmpdir(), "fish-house-live-frame.png"), Buffer.from(s.data,"base64"));
    const expectedRetryErrors = cdp.errors.filter(e => e.method === "Log.entryAdded" && e.params.entry.url === retryUrl && /503/.test(e.params.entry.text));
    const unexpectedBrowserErrors = cdp.errors.filter(e => !expectedRetryErrors.includes(e));
    check("the retry fixture records exactly its intentional first-request 503", expectedRetryErrors.length === 1, JSON.stringify(expectedRetryErrors));
    report.runtimeConsoleErrors = unexpectedBrowserErrors.length; report.browserErrors = cdp.errors; check("zero unexpected browser runtime/console errors", !unexpectedBrowserErrors.length, "count="+unexpectedBrowserErrors.length+" details="+JSON.stringify(unexpectedBrowserErrors));
    report.status = "PASS";
  } finally {
    report.finishedAt = new Date().toISOString(); fs.writeFileSync(OUT, JSON.stringify(report,null,2));
    if (cdp) cdp.close(); server.kill(); chrome.kill();
    try { fs.rmSync(profile,{recursive:true,force:true}); } catch (_) {}
  }
  console.log(JSON.stringify({report:OUT,...report},null,2));
}
main().catch(e=>{console.error(e.stack||e);process.exitCode=1;});
