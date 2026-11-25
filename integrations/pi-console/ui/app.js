/* 大肥鱼统一控制台 —— 前端 API 合同（简要）
 * 统一约定：fetch(..., {credentials:'same-origin'}) + JSON；
 *           POST 成功返回 {ok:true}，失败为非 2xx 且 {error:"中文原因"}。
 * GET  /api/session      -> {authenticated, setupAvailable, secure, lanUrl}
 * POST /api/setup        {pair, password[, reset:true]}  首次设置 / 重置密码（pair 仅来自 #pair= 片段，重置带 reset:true）
 * POST /api/login        {password}
 * POST /api/logout       {}                之后重新 GET /api/session 并显示登录
 * POST /api/password     {current, password}
 * GET  /api/overview     -> qq/wechat 状态、models、conversations、
 *                            diagnostics、styleOptions/frequencyOptions/limitOptions（均为 [{id,name,platforms?}]）
 *   qq      {connected, running, model, client, clientSource, lastRead}
 *           client 为空 = 后台没有报告客户端（客户端那一行显示「未报告」，不猜名字）
 *   wechat  {connected, running, model, client, clientState, clientReason, lastRead,
 *            enabled, bridge:{online,listener,accountOk,lastSeenSec},
 *            diagnostics:{fresh,phase,reason,time}, capabilities:{model}}
 *           clientState: ok|qr|offline|disabled|unknown —— enabled 只表示「网关开启」，
 *           绝不当作「已连接」；读不到就是 unknown，界面上显示「未确认」。
 * POST /api/model        {platform, id}      -> {ok, verification:{state,observed,lastRead}}
 *                           state: confirmed 回读到目标 / mismatch 回读到别的 /
 *                                  unconfirmed 读不到（界面必须显示「未确认」）
 * POST /api/conversation {platform, id, field, value}   field: enabled|style|frequency|limit
 * GET  /api/ca           证书仅在本机（localhost）提供；按 loopback 主机名决定是否展示下载链接。
 * 规则：任一 POST 成功后重新 GET /api/overview（POST then re-GET）；
 *       失败只显示 error 字段的中文文本，绝不输出原始 JSON；
 *       所有服务端字符串一律通过 textContent 写入 DOM。
 * 状态行：成功/失败都带上「最近读取时间」，读取失败时保留旧数据但标明时间与失败原因。
 * 安全：#pair 读到后立即 history.replaceState 移除片段；令牌只存内存（不持久化、不写 sessionStorage），
 *       绝不进入 URL 查询、DOM 文本或控制台日志。
 * 主题：app.js 在 <head> 阻塞加载，首帧前同步写入 data-theme（跟随系统/浅色/深色，
 *       偏好仅存 localStorage 的非敏感键 dafeiyu.theme），无 inline script、无闪烁。
 * 布局：桌面 = 平台卡 + 会话表格；≤760px = 平台卡 + 平台筛选 + 可展开会话卡（390px 宽实测）。
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);

  // ---- 主题：跟随系统 / 浅色 / 深色 -----------------------------------------
  // app.js 在 <head> 里以阻塞式外部脚本加载（不用 inline script，兼容 CSP），
  // 因此在首帧绘制前就能同步写好 data-theme，避免浅色闪一下。
  // 只持久化非敏感的显示偏好；不含账户、令牌或密码。
  const THEME_KEY = 'dafeiyu.theme';
  const THEME_VALUES = ['system', 'light', 'dark'];
  let themePref = 'system';

  function readThemePref() {
    try {
      const v = window.localStorage.getItem(THEME_KEY);
      return THEME_VALUES.indexOf(v) >= 0 ? v : 'system';
    } catch (e) { return 'system'; } // 隐私模式 / 存储被禁用时跟随系统
  }
  function storeThemePref(v) {
    try { window.localStorage.setItem(THEME_KEY, v); } catch (e) { /* 忽略：本轮仍即时生效 */ }
  }
  function systemDark() {
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  }
  function effectiveTheme(pref) {
    return pref === 'system' ? (systemDark() ? 'dark' : 'light') : pref;
  }
  function applyTheme(pref) {
    themePref = THEME_VALUES.indexOf(pref) >= 0 ? pref : 'system';
    const root = document.documentElement;
    // data-theme 写「解析后的实际主题」：CSS 只需按 light/dark 两套变量切换；
    // 用户的选择另存 data-theme-pref，供跟随系统时监听 OS 变化使用。
    const eff = effectiveTheme(themePref);
    root.setAttribute('data-theme', eff);
    root.setAttribute('data-theme-pref', themePref);
    root.style.colorScheme = eff;
    const metas = document.querySelectorAll('meta[name="theme-color"]');
    for (let i = 0; i < metas.length; i++) {
      metas[i].setAttribute('content', eff === 'dark' ? '#151b26' : '#f7f6f3');
    }
  }

  // 首帧前同步应用（此时 <body> 尚未解析，只操作 <html> 与 <head> 里的 meta）。
  themePref = readThemePref();
  applyTheme(themePref);
  // 跟随系统时监听 OS 主题变化（含旧版 Safari 的 addListener 回退）。
  if (window.matchMedia) {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const onOsChange = () => { if (themePref === 'system') applyTheme('system'); };
    if (mq.addEventListener) mq.addEventListener('change', onOsChange);
    else if (mq.addListener) mq.addListener(onOsChange);
  }

  const state = {
    session: null,      // 归一化后的 /api/session
    overview: null,     // 归一化后的 /api/overview
    busy: false,        // 是否有请求在途（用于禁用按钮）
    pair: '',           // 配对令牌，仅内存，绝不持久化
    reset: false,       // #reset=1
    lastLoadedAt: null, // 最近一次成功读取 overview 的时间（Date）
    overviewStale: false,
    proactive: null,
    proactiveStale: true,
    proactiveRead: 0,
    lastVerify: {},     // platform -> {state, observed} 模型切换回读结果
    filter: 'all',      // 会话平台筛选：all | qq | wechat
    expanded: {},       // 手机卡片展开状态：conversation id -> true
  };

  const PLATFORM_NAME = { qq: 'QQ', wechat: '微信', system: '系统' };
  const FIELD_NAME = { enabled: '自动回复', style: '性格', frequency: '主动频率', limit: '每日上限' };
  const FREQ_LABEL = { quiet: '低频', natural: '正常', active: '活跃' };

  // ---- 小工具：所有服务端文本都经 textContent --------------------------------
  function node(tag, cls, txt) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (txt !== undefined && txt !== null) n.textContent = String(txt);
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  function str(v) { return (typeof v === 'string' && v.length > 0) ? v : null; }
  function num(v) { return (typeof v === 'number' && Number.isFinite(v)) ? v : null; }
  function bool(v) { return (typeof v === 'boolean') ? v : null; }
  function arr(v) { return Array.isArray(v) ? v : []; }
  function badge(text, cls) { return node('span', 'badge ' + cls, text); }

  // ---- 网络：同源凭证、只暴露中文 error --------------------------------------
  function apiError(message, status) {
    const e = new Error(message);
    e.status = status;
    return e;
  }
  async function request(path, method, body, signal) {
    const opts = { method: method, credentials: 'same-origin', cache: 'no-store', headers: {} };
    if (signal) opts.signal = signal;
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      throw apiError('无法连接控制台服务，请检查网络后重试。', 0);
    }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      const reason = (data && typeof data.error === 'string' && data.error.trim())
        ? data.error.trim()
        : '请求未完成，请稍后重试。';
      throw apiError(reason, res.status);
    }
    if (method === 'POST' && !(data && data.ok === true)) {
      throw apiError('服务未确认本次操作，请刷新后重试。', res.status);
    }
    return data;
  }
  const apiGet = (path) => request(path, 'GET', undefined);
  const apiPost = (path, body) => request(path, 'POST', body);

  // ---- 状态条 ----------------------------------------------------------------
  // 「最近读取时间」始终可见：成功显示读到的时刻，失败保留上一次成功时刻并标明失败。
  function pad2(n) { return n < 10 ? '0' + n : String(n); }
  function clock(d) {
    if (!(d instanceof Date) || isNaN(d.getTime())) return '';
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }
  function setStatus(text, kind, opts) {
    $('statusText').textContent = text || '';
    $('statusBar').setAttribute('data-kind', kind || '');
    const meta = $('statusMeta');
    const when = opts && opts.at ? clock(opts.at) : '';
    const stale = opts && opts.stale;
    meta.textContent = when
      ? ((stale ? '上次读取 ' : '最近读取 ') + when)
      : '';
    meta.hidden = !meta.textContent;
  }
  // 只更新「最近读取时间」，不动状态文字（用于 POST 成功后的轻提示）。
  function setReadTime(d, stale) {
    const meta = $('statusMeta');
    const when = clock(d);
    meta.textContent = when ? ((stale ? '上次读取 ' : '最近读取 ') + when) : '';
    meta.hidden = !meta.textContent;
  }
  function renderSessionMeta(s) {
    s = s || {};
    $('statusMeta').textContent = s.lanUrl ? ('局域网：' + s.lanUrl) : '';
    $('secureText').textContent = s.secure === true ? 'HTTPS' : 'HTTP';
    $('lanUrl').textContent = s.lanUrl || '未配置';
    // CA 证书只在本机（loopback）提供：按主机名判断，而非 secure。
    // 本机 HTTP 下 secure=false 但 /api/ca 可用；LAN HTTPS 下 secure=true 却取不到证书。
    $('caRow').hidden = !isLoopbackHost() || !str(s.lanUrl).startsWith('https://');
  }
  function isLoopbackHost() {
    const h = (window.location.hostname || '').toLowerCase();
    return h === 'localhost' || h === '127.0.0.1' || h === '::1' || h === '[::1]';
  }

  // ---- 在途状态：请求期间禁用所有按钮 / 可编辑控件 -----------------------------
  function blockOverviewEdit(el) {
    return state.overviewStale && el.closest('#platformGrid,#convBody,#convCards,#globalCard');
  }
  function enable(el, on) {
    if (!el) return;
    el.setAttribute('data-enabled', on ? '1' : '0');
    el.disabled = state.busy || !on || !!blockOverviewEdit(el);
  }
  function syncDisabled() {
    const list = document.querySelectorAll('[data-enabled]');
    for (let i = 0; i < list.length; i++) {
      const n = list[i];
      n.disabled = state.busy || n.getAttribute('data-enabled') !== '1' || !!blockOverviewEdit(n);
    }
  }
  function setBusy(busy) {
    state.busy = busy;
    document.body.classList.toggle('is-busy', busy);
    syncDisabled();
  }
  // 统一包裹：显示状态行 + 在途禁用；成功与否由 fn 内部决定最终状态文本。
  async function run(label, fn) {
    if (state.busy) return false;
    setBusy(true);
    setStatus(label, 'busy');
    try { await fn(); return true; }
    finally { setBusy(false); }
  }
  function showFailure(e, errId) {
    if (e.status === 401) { handleUnauthorized(); return; }
    if (errId) setFormError(errId, e.message);
    setStatus(e.message, 'error');
  }
  function setFormError(id, msg) {
    const n = $(id);
    if (!n) return;
    n.textContent = msg || '';
    n.hidden = !msg;
  }
  function handleUnauthorized() {
    state.overview = null;
    if (state.session) state.session.authenticated = false;
    clearPair();
    showAuth('login');
    setFormError('loginError', '登录已失效，请重新输入密码。');
    setStatus('登录已失效，请重新输入密码。', 'error');
  }

  // ---- 配对片段：读到即移除，绝不进入 URL 查询 --------------------------------
  function captureFragment() {
    const hash = window.location.hash || '';
    let token = '', reset = false;
    if (hash) {
      try {
        const p = new URLSearchParams(hash.replace(/^#/, ''));
        token = p.get('pair') || '';
        reset = p.get('reset') === '1';
      } catch (e) { /* 片段格式异常时忽略 */ }
    }
    // Only fixed navigation fragments survive; pairing tokens never do.
    const workspaceLink = Array.from(document.querySelectorAll('[data-workspace-view]'))
      .some((link) => link.hash === hash);
    if (hash && !workspaceLink) {
      window.history.replaceState(null, '', window.location.pathname + window.location.search);
    }
    return { token: token, reset: reset };
  }
  function storePair(token, reset) {
    state.pair = token;
    state.reset = !!reset;
  }
  function clearPair() {
    state.pair = '';
    state.reset = false;
  }
  function clearSecrets() {
    ['setupPassword', 'setupConfirm', 'loginPassword', 'pwdCurrent', 'pwdNew', 'pwdConfirm']
      .forEach((id) => { const n = $(id); if (n) n.value = ''; });
    clearPair();
  }

  // ---- 会话（认证）判定 ------------------------------------------------------
  function normalizeSession(s) {
    s = (s && typeof s === 'object') ? s : {};
    return {
      authenticated: s.authenticated === true,
      setupAvailable: s.setupAvailable === true,
      secure: s.secure === true,
      lanUrl: str(s.lanUrl),
    };
  }
  function showAuth(mode, opts) {
    $('authCard').hidden = false;
    $('console').hidden = true;
    resetHostStatus();
    clearWeChatMemory();
    clearMemoryStorage();
    clearWeChatGroupPersonas();
    closeWeChatAccount();
    const isSetup = (mode === 'setup');
    $('setupForm').hidden = !isSetup;
    $('loginForm').hidden = isSetup;
    $('setupHint').hidden = !(opts && opts.hint);
    if (opts && opts.hint) {
      $('setupHint').textContent = '首次设置 / 恢复密码需要在本机浏览器打开带 #pair=... 的链接。';
    }
    setFormError('setupError', '');
    setFormError('loginError', '');
    if (isSetup) {
      const reset = state.reset;
      $('authTitle').textContent = reset ? '重置密码' : '设置密码';
      $('authDesc').textContent = reset
        ? '请输入新的密码以恢复控制台访问。'
        : '请为控制台设置一个密码，之后用它登录。';
      $('setupSubmit').textContent = reset ? '重置密码' : '设置密码';
      $('authFoot').textContent = '配对凭证来自本机链接，不会在页面中显示。';
    } else {
      $('authTitle').textContent = '登录';
      $('authDesc').textContent = '输入密码以继续使用控制台。';
      $('loginSubmit').textContent = '登录';
      $('authFoot').textContent = '忘记密码？请在本机浏览器打开恢复链接。';
    }
  }
  function showConsole() {
    $('authCard').hidden = true;
    $('console').hidden = false;
    syncHostStatus();
    if (window.location.hash === '#hSettings') refreshChannels();
    if (window.location.hash === '#hDiagnostics') refreshDelivery();
    if (window.location.hash === '#hConversations') refreshWeChatMemory(true);
  }
  function applySession(s) {
    renderSessionMeta(s);
    if (s.authenticated) {
      clearPair(); // 已登录：不再持有任何配对令牌
      showConsole();
      loadOverview();
      return;
    }
    if (s.setupAvailable && state.pair) {
      showAuth('setup');
      setStatus(state.reset ? '请重设密码' : '请设置访问密码', 'warn');
      return;
    }
    clearPair();
    if (s.setupAvailable) {
      showAuth('login', { hint: true });
      setStatus('等待首次设置：请在本机浏览器打开配对链接', 'warn');
    } else {
      showAuth('login');
      setStatus('请输入密码登录', '');
    }
  }
  async function loadSession(note) {
    setStatus('正在连接控制台…', 'busy');
    let s;
    try {
      s = await apiGet('/api/session');
    } catch (e) {
      showAuth('login');
      setFormError('loginError', e.message);
      setStatus('无法连接：' + e.message, 'error');
      return;
    }
    state.session = normalizeSession(s);
    applySession(state.session);
    if (note) setStatus(note, 'ok', { at: state.lastLoadedAt });
  }

  // ---- 认证表单 --------------------------------------------------------------
  async function onSetupSubmit(ev) {
    ev.preventDefault();
    if (state.busy) return;
    const password = $('setupPassword').value;
    const confirm = $('setupConfirm').value;
    setFormError('setupError', '');
    if (!password) { setFormError('setupError', '请输入新密码。'); return; }
    if (password !== confirm) { setFormError('setupError', '两次输入的密码不一致。'); return; }
    if (!state.pair) {
      setFormError('setupError', '缺少配对凭证，请在本机浏览器重新打开设置链接。');
      return;
    }
    await run('正在设置…', async () => {
      try {
        // pair 只放进 JSON 请求体，绝不拼到 URL 查询里。
        // 重置密码时带 reset:true，与链接的 &reset=1 / 令牌类型保持一致。
        const body = { pair: state.pair, password: password };
        if (state.reset) body.reset = true;
        await apiPost('/api/setup', body);
      } catch (e) { showFailure(e, 'setupError'); return; }
      clearSecrets();
      state.session.authenticated = true;
      state.session.setupAvailable = false;
      showConsole();
      const ok = await loadOverview({ silent: true }); // POST 成功后重新 GET overview
      setStatus(ok ? '设置完成，控制台已就绪' : '设置完成', 'ok',
                { at: state.lastLoadedAt });
    });
  }
  async function onLoginSubmit(ev) {
    ev.preventDefault();
    if (state.busy) return;
    const password = $('loginPassword').value;
    setFormError('loginError', '');
    if (!password) { setFormError('loginError', '请输入密码。'); return; }
    await run('正在登录…', async () => {
      try {
        await apiPost('/api/login', { password: password });
      } catch (e) { showFailure(e, 'loginError'); return; }
      clearSecrets();
      state.session.authenticated = true;
      showConsole();
      const ok = await loadOverview({ silent: true }); // POST 成功后重新 GET overview
      setStatus('登录成功', 'ok', { at: state.lastLoadedAt });
    });
  }
  async function onPasswordSubmit(ev) {
    ev.preventDefault();
    if (state.busy) return;
    const current = $('pwdCurrent').value;
    const password = $('pwdNew').value;
    const confirm = $('pwdConfirm').value;
    setFormError('pwdError', '');
    if (!current || !password) { setFormError('pwdError', '请填写当前密码与新密码。'); return; }
    if (password !== confirm) { setFormError('pwdError', '两次输入的新密码不一致。'); return; }
    await run('正在保存新密码…', async () => {
      try {
        await apiPost('/api/password', { current: current, password: password });
        $('pwdCurrent').value = ''; $('pwdNew').value = ''; $('pwdConfirm').value = '';
        const ok = await loadOverview({ silent: true }); // POST 成功后重新 GET overview
        if (ok) setStatus('密码已更新', 'ok', { at: state.lastLoadedAt });
      } catch (e) { showFailure(e, 'pwdError'); }
    });
  }
  async function onLogout() {
    if (state.busy) return;
    await run('正在退出…', async () => {
      hostStatus.signingOut = true;
      stopHostStatus();
      try {
        await apiPost('/api/logout', {});
      } catch (e) {
        hostStatus.signingOut = false;
        showFailure(e);
        syncHostStatus();
        return;
      }
      clearSecrets();
      state.overview = null;
      if (state.session) state.session.authenticated = false;
      resetHostStatus();
      hostStatus.signingOut = false;
      await loadSession('已退出登录'); // 合同：logout 后重新 GET /api/session 并显示登录
    });
  }

  // ---- 概览数据归一化：null 一律安全降级 --------------------------------------
  const PLATFORM_KEYS = ['qq', 'wechat'];
  // 微信客户端可达状态：服务端给 ok/qr/offline/disabled/unknown；
  // 读不到（缺字段）时按 connected 兜底，绝不默认成「已连接」。
  const CLIENT_STATES = ['ok', 'qr', 'offline', 'disabled', 'unknown'];
  function deriveClientState(p) {
    const s = str(p.clientState);
    if (s && CLIENT_STATES.indexOf(s) >= 0) return s;
    if (p.connected === true) return 'ok';
    if (p.connected === false) return 'offline';
    return 'unknown';
  }
  function normBridge(b) {
    if (!b || typeof b !== 'object') return null;
    return {
      online: bool(b.online),
      listener: bool(b.listener),
      accountOk: bool(b.accountOk),
      lastSeenSec: num(b.lastSeenSec),
    };
  }
  // 服务端 lastRead 是 epoch 秒；只用于显示，不参与任何判定。
  function normEpoch(v) {
    const n = num(v);
    if (n === null || n <= 0) return null;
    const d = new Date(n * 1000);
    return isNaN(d.getTime()) ? null : d;
  }
  function normPlatform(p) {
    p = (p && typeof p === 'object') ? p : {};
    const diag = (p.diagnostics && typeof p.diagnostics === 'object') ? p.diagnostics : null;
    return {
      connected: bool(p.connected),
      running: bool(p.running),
      model: str(p.model),
      client: str(p.client),
      clientSource: (p.clientSource === 'reported' || p.clientSource === 'config')
        ? p.clientSource : 'unknown',
      clientState: deriveClientState(p),
      clientReason: str(p.clientReason),
      enabled: bool(p.enabled),
      bridge: normBridge(p.bridge),
      bridgeStatus: str(p.bridgeStatus),
      lastRead: normEpoch(p.lastRead),
      diagFresh: diag ? diag.fresh === true : null,
      diagPhase: diag ? str(diag.phase) : null,
      diagReason: diag ? str(diag.reason) : null,
      diagTime: diag ? normEpoch(diag.time) : null,
      caps: {
        model: (p.capabilities && p.capabilities.model === true) || str(p.model) !== null,
      },
    };
  }
  // 选项统一为 {id, name, platforms}；platforms 是服务端给的附加提示字段。
  // 兼容字符串/数字形式的旧数据，但绝不用 safeStr 把对象过滤掉。
  function normOptionList(list) {
    return arr(list).map((x) => {
      let base;
      if (typeof x === 'string') base = { id: x, name: null };
      else if (typeof x === 'number' && Number.isFinite(x)) base = { id: String(x), name: null };
      else if (x && typeof x === 'object' && str(x.id) !== null) {
        base = {
          id: String(x.id),
          name: str(x.name),
          platforms: arr(x.platforms).filter((p) => p === 'qq' || p === 'wechat'),
        };
      } else return null;
      if (!base.platforms) base.platforms = [];
      return base;
    }).filter(Boolean);
  }
  // 按当前会话的平台过滤可选项：没有 platforms 提示的选项对所有平台开放。
  function optionsForPlatform(list, platform) {
    return list.filter((op) => !op.platforms.length || op.platforms.indexOf(platform) >= 0);
  }
  function normOverview(o) {
    o = (o && typeof o === 'object') ? o : {};
    const models = arr(o.models).filter((m) => m && str(m.id) !== null).map((m) => ({
      id: String(m.id),
      name: str(m.name) || String(m.id),
      platforms: arr(m.platforms).filter((p) => p === 'qq' || p === 'wechat'),
    }));
    const conversations = arr(o.conversations)
      .filter((c) => c && (c.platform === 'qq' || c.platform === 'wechat') && str(c.id) !== null)
      .map((c) => {
        const caps = (c.capabilities && typeof c.capabilities === 'object') ? c.capabilities : {};
        return {
          platform: c.platform,
          id: String(c.id),
          name: str(c.name) || String(c.id),
          kind: (c.kind === 'group' || c.kind === 'private') ? c.kind : null,
          enabled: bool(c.enabled),
          enabledLabel: str(c.enabledLabel),
          enabledHint: str(c.enabledHint),
          style: str(c.style),
          frequency: str(c.frequency),
          limit: num(c.limit),
          capabilities: {
            enabled: caps.enabled === true, style: caps.style === true,
            frequency: caps.frequency === true, limit: caps.limit === true,
          },
        };
      });
    const diagnostics = arr(o.diagnostics).filter((d) => d && typeof d === 'object').map((d) => ({
      platform: (d.platform === 'qq' || d.platform === 'wechat' || d.platform === 'system') ? d.platform : 'system',
      level: (d.level === 'info' || d.level === 'warning' || d.level === 'error') ? d.level : 'info',
      message: str(d.message) || '',
    }));
    return {
      qq: normPlatform(o.qq),
      wechat: normPlatform(o.wechat),
      models: models,
      conversations: conversations,
      diagnostics: diagnostics,
      styleOptions: normOptionList(o.styleOptions),
      frequencyOptions: normOptionList(o.frequencyOptions),
      limitOptions: normOptionList(o.limitOptions),
      globalSettings: arr(o.globalSettings).filter((g) => g && typeof g === 'object').map((g) => ({
        scope: str(g.scope) || '',
        field: str(g.field) || '',
        value: g.value === null || g.value === undefined ? null : g.value,
        platform: (g.platform === 'qq' || g.platform === 'wechat' || g.platform === 'system') ? g.platform : 'system',
        label: str(g.label) || '全局设置',
        capabilities: {
          style: !!(g.capabilities && g.capabilities.style === true),
        },
        styleOptions: normOptionList(g.styleOptions),
      })),
    };
  }
  // 频率档位以服务端 frequencyOptions 为准（[{id,name}]，quiet/natural/active，无 off）。
  // 服务端未提供时才回退到基线三档；显示名一律用服务端给的 name。
  function frequencyOptions(o, platform) {
    const server = optionsForPlatform(o.frequencyOptions, platform);
    if (server.length) return server;
    return [
      { id: 'quiet', name: FREQ_LABEL.quiet },
      { id: 'natural', name: FREQ_LABEL.natural },
      { id: 'active', name: FREQ_LABEL.active },
    ];
  }
  function optionName(op) { return op.name || FREQ_LABEL[op.id] || op.id; }

  // ---- 读取概览 --------------------------------------------------------------
  async function loadOverview(opts) {
    const silent = !!(opts && opts.silent);
    // 读取中也要保留上一次的时间，用户才知道现在看的是多旧的数据。
    if (!silent) setStatus('正在读取状态…', 'busy', { at: state.lastLoadedAt, stale: true });
    let o;
    try {
      o = await apiGet('/api/overview');
    } catch (e) {
      if (e.status === 401) { handleUnauthorized(); return false; }
      // 失败不清空已有数据：保留上次读到的内容，但把时间标成「上次读取」。
      setStatus('读取状态失败：' + e.message, 'error',
                { at: state.lastLoadedAt, stale: true });
      state.overviewStale = true;
      syncDisabled();
      return false;
    }
    state.overviewStale = false;
    state.overview = normOverview(o);
    state.lastLoadedAt = new Date();
    renderAll();
    loadQQOrdinaryConfig();
    if (!silent) {
      const partial = ['qq', 'wechat'].some((key) => state.overview[key].lastRead === null);
      setStatus(partial ? '部分状态未读到' : '状态已更新', partial ? 'warning' : 'ok',
                { at: state.lastLoadedAt });
    }
    return true;
  }

  async function loadQQOrdinaryConfig() {
    const read = ++state.proactiveRead;
    try {
      const raw = await apiGet('/api/proactive/config');
      if (read !== state.proactiveRead) return null;
      if (!raw || raw.ok !== true) throw new Error('策略响应无效');
      state.proactive = raw;
      if (state.proactiveStale) {
        $('qqOrdinaryStatus').textContent = '';
        $('wechatActivityStatus').textContent = '';
      }
      state.proactiveStale = false;
      renderQQOrdinaryConfig();
      renderWeChatActivity();
      return raw;
    } catch (e) {
      if (read !== state.proactiveRead) return null;
      state.proactiveStale = true;
      if (e.status === 401) { handleUnauthorized(); return null; }
      renderQQOrdinaryConfig();
      renderWeChatActivity();
      for (const id of ['qqOrdinaryStatus', 'wechatActivityStatus']) {
        $(id).textContent = '读取失败，当前显示上次读取的设置。';
      }
      return null;
    }
  }

  function renderQQOrdinaryConfig() {
    const host = $('qqOrdinaryRows');
    if (!host) return;
    clear(host);
    const data = state.proactive || {};
    const groups = data.qqGroups && typeof data.qqGroups === 'object' ? data.qqGroups : {};
    const ordinary = data.qqOrdinary && typeof data.qqOrdinary === 'object' ? data.qqOrdinary : {};
    const runtime = data.qqRuntime || {};
    const editable = !state.proactiveStale && data.qqConfigState === 'available';
    const names = Object.keys(groups).filter((id) => /^group:\d{1,20}$/.test(id)).sort();
    if (!names.length) {
      host.appendChild(node('p', 'card-note', '暂无已登记的 QQ 群策略。'));
      return;
    }
    names.forEach((id) => {
      const row = groups[id] && typeof groups[id] === 'object' ? groups[id] : {};
      const effective = ordinary[id] && ordinary[id].enabled === true;
      const line = node('div', 'memory-toolbar');
      line.appendChild(node('span', 'cell-name', id));
      const applied = runtime.verification === 'applied' && !state.proactiveStale
        ? (runtime.groups || {})[id] : null;
      const runtimeLabel = !applied ? '核心状态待确认'
        : !runtime.running ? '核心已读入 · 回复未运行'
        : applied.ordinary ? '普通接话已生效'
        : applied.publicQuestion ? '仅公开提问自动回复' : '普通接话关闭';
      line.appendChild(node('span', 'cell-sub', runtimeLabel));
      const toggleLabel = node('label', 'switch');
      const toggle = document.createElement('input');
      toggle.type = 'checkbox'; toggle.checked = effective;
      toggle.setAttribute('aria-label', id + ' 普通聊天');
      enable(toggle, editable);
      toggleLabel.appendChild(toggle); toggleLabel.appendChild(node('span', 'track'));
      toggleLabel.appendChild(node('span', 'state', effective ? '开' : '关'));
      line.appendChild(toggleLabel);
      const threshold = document.createElement('input');
      threshold.type = 'number'; threshold.className = 'input compact';
      threshold.min = '0.30'; threshold.max = '0.95'; threshold.step = '0.01';
      const originalThreshold = typeof row.threshold === 'number' && Number.isFinite(row.threshold)
        ? row.threshold : 0.70;
      const displayedThreshold = originalThreshold.toFixed(2);
      threshold.value = displayedThreshold;
      threshold.setAttribute('aria-label', id + ' 普通聊天阈值');
      enable(threshold, editable);
      line.appendChild(threshold);
      const save = node('button', 'btn btn-primary btn-sm', '保存');
      save.type = 'button';
      enable(save, editable);
      save.addEventListener('click', () => {
        if (state.busy || state.proactiveStale) return;
        const enabled = toggle.checked;
        const value = Number(threshold.value);
        const changedThreshold = value !== Number(displayedThreshold);
        if (changedThreshold && (!Number.isFinite(value) || value < 0.30 || value > 0.95)) {
          $('qqOrdinaryStatus').textContent = '阈值须在 0.30 到 0.95 之间。'; return;
        }
        const changes = {};
        const expected = {};
        if (enabled !== effective) {
          changes.ordinaryReplyEnabled = enabled;
          expected.ordinaryReplyEnabled = effective;
        }
        if (changedThreshold) {
          changes.threshold = value;
          expected.threshold = originalThreshold;
        }
        const keys = Object.keys(changes);
        if (!keys.length) return;
        const matches = (values) => values && keys.every((key) => values[key] === changes[key]);
        run('正在保存 QQ 普通聊天设置…', async () => {
          const result = await apiPost('/api/proactive/config', { platform: 'qq', group: id, ...changes, expected });
          if (!result || result.verification !== 'persisted' || result.group !== id || !matches(result.observed)) {
            throw new Error('保存后的磁盘值未确认。');
          }
          const loaded = await loadQQOrdinaryConfig();
          const saved = loaded && (loaded.qqGroups || {})[id];
          const live = loaded && loaded.qqRuntime;
          const liveRow = live && live.verification === 'applied' && (live.groups || {})[id];
          const status = $('qqOrdinaryStatus');
          if (!matches(saved)) {
            status.textContent = id + ' 已写入，页面回读未确认。';
          } else if (!liveRow || !matches({ threshold: liveRow.threshold, ordinaryReplyEnabled: liveRow.ordinary })) {
            status.textContent = id + ' 已保存，核心生效未确认。';
          } else {
            status.textContent = id + (live.running ? ' 已保存，核心生效已确认。' : ' 已保存，核心已读入。');
          }
        }).catch(async (e) => {
          if (e.status === 401) { handleUnauthorized(); return; }
          if (e.status === 409) await loadQQOrdinaryConfig();
          $('qqOrdinaryStatus').textContent = e.message;
        });
      });
      line.appendChild(save);
      host.appendChild(line);
    });
  }

  function renderWeChatActivity() {
    const host = $('wechatActivityRows');
    const status = $('wechatActivityStatus');
    const data = state.proactive || {};
    const enabled = new Set(arr(data.wechatEnabledGroups));
    const groups = data.wechatAuthorizedGroups || [...enabled];
    clear(host);
    if (!groups.length) {
      host.appendChild(node('p', 'card-note', '暂无已授权的微信群。'));
      return;
    }
    const cooldowns = (data.publicQuestionCooldowns || {}).wechat || {};
    groups.forEach((name) => {
      const row = node('form', 'memory-toolbar');
      const field = node('label', 'field');
      field.appendChild(node('span', 'cell-name', name + ' · 冷却秒数'));
      const input = document.createElement('input');
      input.className = 'input compact';
      input.type = 'number';
      input.min = '30'; input.max = '1200'; input.step = '1'; input.required = true;
      const originalCooldown = cooldowns[name] || data.publicQuestionCooldownDefault || 60;
      input.value = String(originalCooldown);
      input.disabled = state.proactiveStale;
      field.appendChild(input);

      const toggleField = node('label', 'switch');
      const toggle = document.createElement('input');
      toggle.type = 'checkbox';
      toggle.checked = enabled.has(name);
      toggle.disabled = state.proactiveStale;
      toggle.setAttribute('aria-label', name + ' 自动接话');
      toggleField.append(toggle, node('span', 'track'), node('span', 'state', '自动接话'));
      const save = node('button', 'btn btn-primary btn-sm', '保存');
      save.type = 'submit';
      save.disabled = state.proactiveStale;
      row.append(field, toggleField, save);
      row.addEventListener('submit', (event) => {
        event.preventDefault();
        const value = Number(input.value);
        const active = toggle.checked;
        if (!Number.isInteger(value) || value < 30 || value > 1200 || state.busy || state.proactiveStale) return;
        const expected = {};
        const patch = { platform: 'wechat', group: name, expected };
        if (value !== originalCooldown) {
          patch.publicQuestionCooldownSeconds = value;
          expected.publicQuestionCooldownSeconds = originalCooldown;
        }
        if (active !== enabled.has(name)) {
          patch.ordinaryReplyEnabled = active;
          expected.ordinaryReplyEnabled = enabled.has(name);
        }
        if (!Object.keys(expected).length) return;
        run('正在保存微信群接话设置…', async () => {
          const result = await apiPost('/api/proactive/config', patch);
          if (result.verification !== 'persisted' || result.group !== name ||
              ('publicQuestionCooldownSeconds' in patch && result.runtimeObserved !== value) ||
              ('ordinaryReplyEnabled' in patch && result.ordinaryReplyEnabled !== active)) {
            throw new Error('保存后未确认运行值');
          }
          const loaded = await loadQQOrdinaryConfig();
          const current = loaded && (loaded.publicQuestionCooldowns || {}).wechat;
          if (!loaded || !arr(loaded.wechatAuthorizedGroups).includes(name) ||
              ('ordinaryReplyEnabled' in patch && arr(loaded.wechatEnabledGroups).includes(name) !== active) ||
              ('publicQuestionCooldownSeconds' in patch && (!current || current[name] !== value))) {
            status.textContent = '已写入，页面回读未确认。';
            return;
          }
          status.textContent = '已保存并核对本次修改。';
        }).catch(async (error) => {
          if (error.status === 401) { handleUnauthorized(); return; }
          if (error.status === 409) await loadQQOrdinaryConfig();
          status.textContent = error.message;
        });
      });
      host.appendChild(row);
    });
  }

  // ---- 机器采样：只在已认证、可见的总览读取 ----------------------------------
  const HOST_MISSING = '未采集';
  const HOST_MIN_INTERVAL = 60000;
  const HOST_SERVICES = [
    ['qq', 'QQ'], ['wechatClient', '微信客户端'], ['wechatBridge', '微信桥接'],
    ['core', '核心'], ['modelHub', '模型网关'], ['house', '家园'],
  ];
  const HOST_SERVICE_LABELS = new Map([
    ['active', ['运行中', 'on']], ['inactive', ['已停止', 'off']],
    ['failed', ['故障', 'error']], ['activating', ['启动中', 'warning']],
    ['deactivating', ['停止中', 'warning']],
  ]);
  const HOST_CLOCK = new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
  const hostStatus = {
    timer: null, request: null, signingOut: false, nextReadAt: 0,
    current: null, history: [], loaded: false,
  };

  function isHostSample(sample) {
    return sample && num(sample.at) !== null && sample.at > 0 && sample.at <= 8640000000000;
  }
  function hostSampleTime(sample) { return HOST_CLOCK.format(new Date(sample.at * 1000)); }
  function hostBytes(bytes) {
    let value = bytes;
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index++;
    }
    return value.toFixed(index === 0 ? 0 : 1) + ' ' + units[index];
  }
  function hostUsage(label, capacity) {
    const used = capacity && num(capacity.usedBytes);
    const total = capacity && num(capacity.totalBytes);
    if (capacity && used === 0 && total === 0 && label === 'Swap') return [label, '未配置'];
    if (!capacity || used === null || total === null || used < 0 || total <= 0 || used > total) {
      return [label, HOST_MISSING];
    }
    const percent = used / total * 100;
    return [label, percent.toFixed(1) + '%', hostBytes(used) + ' / ' + hostBytes(total), percent];
  }
  function hostUptime(seconds) {
    if (seconds === null || seconds < 0) return HOST_MISSING;
    if (seconds < 60) return Math.floor(seconds) + ' 秒';
    const minutes = Math.floor(seconds / 60);
    const days = Math.floor(minutes / 1440);
    const hours = Math.floor(minutes % 1440 / 60);
    const rest = minutes % 60;
    return (days ? days + ' 天 ' : '') + (hours || days ? hours + ' 小时 ' : '') + rest + ' 分钟';
  }
  function renderHostStatus() {
    const last = hostStatus.current;
    const history = $('hostHistory');
    const selected = hostStatus.history.find((sample) => String(sample.at) === history.value);
    const sample = selected || last || {};
    $('hostLastSample').textContent = '最近采样：' + (last ? hostSampleTime(last) : HOST_MISSING);
    const cpu = num(sample.cpuPercent);
    const percent = cpu !== null && cpu >= 0 && cpu <= 100 ? cpu : null;
    const load = num(sample.load1);
    const temp = num(sample.tempC);
    const metrics = [
      ['CPU', percent !== null ? percent.toFixed(1) + '%' : HOST_MISSING,
        '1 分钟负载 ' + (load !== null && load >= 0 ? load.toFixed(2) : HOST_MISSING), percent],
      hostUsage('内存', sample.memory),
      ['温度', temp !== null ? temp.toFixed(1) + ' °C' : HOST_MISSING],
      hostUsage('磁盘', sample.disk),
      hostUsage('Swap', sample.swap),
      ['开机时长', hostUptime(num(sample.uptimeSeconds))],
    ];
    const grid = $('hostMetrics');
    clear(grid);
    for (const [label, value, detail, fill] of metrics) {
      const valueCell = node('dd');
      valueCell.appendChild(node('span', 'host-metric-value', value));
      if (detail) valueCell.appendChild(node('span', 'host-metric-detail', detail));
      if (num(fill) !== null) {
        const meter = node('progress');
        meter.max = 100;
        meter.value = fill;
        meter.setAttribute('aria-hidden', 'true');
        valueCell.appendChild(meter);
      }
      const metric = node('div', 'host-metric');
      metric.append(node('dt', '', label), valueCell);
      grid.appendChild(metric);
    }
    const services = $('hostServices');
    clear(services);
    for (const [key, title] of HOST_SERVICES) {
      const value = sample.services && sample.services[key];
      const [label, kind] = HOST_SERVICE_LABELS.get(value) || ['未确认', 'unknown'];
      const item = node('li', 'host-service');
      item.dataset.kind = kind;
      const dot = node('span', 'host-service-dot');
      dot.setAttribute('aria-hidden', 'true');
      item.append(dot, node('span', '', title), node('span', 'host-service-state', label));
      services.appendChild(item);
    }
  }
  function renderHostHistory() {
    const select = $('hostHistory');
    const selectedAt = select.value;
    const latest = node('option', '', '最新采样');
    latest.value = '';
    select.replaceChildren(latest);
    const entries = document.createDocumentFragment();
    for (const sample of hostStatus.history.slice().reverse()) {
      const option = node('option', '', hostSampleTime(sample));
      option.value = String(sample.at);
      entries.appendChild(option);
    }
    select.appendChild(entries);
    select.value = hostStatus.history.some((sample) => String(sample.at) === selectedAt) ? selectedAt : '';
    select.disabled = hostStatus.history.length === 0;
  }
  function hostNotice(text) {
    const message = $('hostStatusMessage');
    message.textContent = text;
    message.hidden = !text;
  }
  function canReadHostStatus() {
    return state.session && state.session.authenticated && !hostStatus.signingOut &&
      document.visibilityState === 'visible' && !$('console').hidden &&
      !$('hostStatusCard').closest('[data-workspace-pane]').hidden;
  }
  function stopHostStatus() {
    clearTimeout(hostStatus.timer);
    hostStatus.timer = null;
    const request = hostStatus.request;
    if (request) {
      hostStatus.request = null;
      if (request.controller) request.controller.abort();
      if (!hostStatus.loaded) hostStatus.nextReadAt = 0;
    }
  }
  function resetHostStatus() {
    stopHostStatus();
    hostStatus.current = null;
    hostStatus.history = [];
    hostStatus.loaded = false;
    hostStatus.nextReadAt = 0;
    hostNotice('');
    renderHostHistory();
    renderHostStatus();
  }
  function syncHostStatus(refreshNow = false) {
    if (!canReadHostStatus()) { stopHostStatus(); return; }
    if (hostStatus.request) return;
    clearTimeout(hostStatus.timer);
    hostStatus.timer = null;
    const wait = refreshNow ? 0 : hostStatus.nextReadAt - Date.now();
    if (wait > 0) {
      hostStatus.timer = setTimeout(() => syncHostStatus(), wait);
      return;
    }
    readHostStatus();
  }
  async function readHostStatus() {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const reading = { controller: controller };
    const deadline = controller ? setTimeout(() => controller.abort(), 12000) : null;
    hostStatus.request = reading;
    hostStatus.nextReadAt = Date.now() + HOST_MIN_INTERVAL;
    if (!hostStatus.loaded) hostNotice('正在读取机器状态…');
    try {
      const data = await request('/api/host/status', 'GET', undefined, controller && controller.signal);
      if (hostStatus.request !== reading || !canReadHostStatus()) return;
      if (!data || data.ok !== true) throw apiError('读取未完成', 0);
      hostStatus.current = isHostSample(data.current) ? data.current : null;
      hostStatus.history = arr(data.history).slice(-1440).filter(isHostSample);
      hostStatus.loaded = true;
      const seconds = num(data.sampleSeconds);
      hostStatus.nextReadAt = Date.now() + (seconds === null
        ? HOST_MIN_INTERVAL : Math.max(HOST_MIN_INTERVAL, Math.min(3600, seconds) * 1000));
      renderHostHistory();
      renderHostStatus();
      hostNotice(hostStatus.current ? '' : '暂无最新采样。');
    } catch (error) {
      if (hostStatus.request !== reading || !canReadHostStatus()) return;
      if (error.status === 401) { handleUnauthorized(); return; }
      hostNotice(hostStatus.current || hostStatus.history.length
        ? '读取失败，已保留上次采样。' : '暂时无法读取机器状态，可用总览刷新重试。');
    } finally {
      clearTimeout(deadline);
      if (hostStatus.request === reading) {
        hostStatus.request = null;
        syncHostStatus();
      }
    }
  }

  // ---- 渲染：平台卡片（连接 / 自动回复 / 客户端 / 模型 四项分开显示） -----------
  // 徽章只陈述读到的事实：true=已连接 false=未连接 null=未确认（读不到）。
  // 待扫码 / 已停用 是比「未连接」更具体的状态，优先显示（避免说成「未连接」误导）。
  function connectionBadge(p) {
    if (p.clientState === 'qr') return badge('待扫码', 'warning');
    if (p.clientState === 'disabled') return badge('已停用', 'off');
    if (p.bridgeStatus === 'listener_paused') return badge('监听暂停', 'warning');
    if (p.bridgeStatus === 'window_pending') return badge('窗口待恢复', 'warning');
    if (p.bridgeStatus === 'account_blocked') return badge('账号校验未通过', 'warning');
    if (p.bridgeStatus === 'offline') return badge('心跳中断', 'off');
    if (p.connected === true) return badge('已连接', 'on');
    if (p.connected === false) return badge('未连接', 'off');
    return badge('未确认', 'unknown'); // 读不到 ≠ 不可用
  }
  function runningBadge(v) {
    if (v === true) return badge('运行中', 'on');
    if (v === false) return badge('已停止', 'warning');
    return badge('未确认', 'unknown');
  }
  // 客户端那一行：「客户端」不是连接状态本身，而是「接的是谁」。
  // QQ：后台没报告 -> 显示「未报告」；配置里写了 qqClientName 才显示名字。
  function clientText(key, p) {
    if (p.client) return p.client;
    return key === 'qq' ? '未报告' : '未确认';
  }
  function clientHint(key, p) {
    if (key === 'qq' && !p.client) {
      return '后台没有报告客户端名称；如需显示，可在配置里设置 qqClientName';
    }
    if (key === 'wechat') {
      if (p.clientState === 'qr') return '微信客户端在等待扫码或手机确认';
      if (p.clientState === 'disabled') return '微信网关已停用';
      if (p.bridgeStatus === 'offline') return '当前未收到桥接心跳，微信登录状态可在账号页单独确认';
      if (p.bridgeStatus === 'listener_paused') return '桥接仍有心跳，消息监听暂未运行';
      if (p.bridgeStatus === 'window_pending') return '等待微信窗口恢复后继续收发';
      if (p.bridgeStatus === 'account_blocked') return '桥接未通过当前账号的校验';
      if (p.clientState === 'ok') return p.bridge && p.bridge.accountOk === true
        ? '桥接在线且账号已通过校验' : '桥接监听正常，客户端登录状态可在账号页确认';
      return '桥接状态读不到，先不判断';
    }
    return '';
  }
  // 微信的桥接三要素（online / listener / accountOk）逐项展示，不合成一个笼统的「在线」。
  function bridgeBits(p) {
    const wrap = node('span', 'bridge-bits');
    if (!p.bridge) {
      wrap.appendChild(node('span', 'bit unknown', '桥接 未确认'));
      return wrap;
    }
    const tri = (v) => (v === true ? 'on' : (v === false ? 'off' : 'unknown'));
    const mark = (v) => (v === true ? '是' : (v === false ? '否' : '未确认'));
    wrap.appendChild(node('span', 'bit ' + tri(p.bridge.online), '心跳 ' + mark(p.bridge.online)));
    wrap.appendChild(node('span', 'bit ' + tri(p.bridge.listener), '监听 ' + mark(p.bridge.listener)));
    wrap.appendChild(node('span', 'bit ' + tri(p.bridge.accountOk), '账号 ' + mark(p.bridge.accountOk)));
    return wrap;
  }
  // 桥接诊断的「新鲜度」：超过 60 秒的旧记录不能用来判断当前状态。
  function bridgeNote(p) {
    if (!p.diagReason && p.diagFresh === null) return '';
    const parts = [];
    if (p.diagFresh === true) parts.push('诊断状态新鲜');
    else if (p.diagFresh === false) parts.push('诊断记录已过期');
    else parts.push('无诊断记录');
    if (p.diagReason) parts.push(p.diagReason);
    if (p.diagTime) parts.push(clock(p.diagTime));
    return parts.join(' · ');
  }
  function kvRow(label, valueNode, unset) {
    const row = node('div', 'kv');
    row.appendChild(node('span', 'k', label));
    const v = node('span', 'v' + (unset ? ' unset' : ''));
    if (typeof valueNode === 'string') v.textContent = valueNode;
    else if (valueNode) v.appendChild(valueNode);
    else v.textContent = '未配置';
    row.appendChild(v);
    return row;
  }
  // 模型：当前值 + 可选候选。候选就是下拉里除当前值以外的项，并明确标注「（当前）」。
  // 点击切换后必须验证回读：confirmed / mismatch / unconfirmed 三种说法分开显示。
  function modelDisplay(m) {
    const avail = arr(m);
    return avail.length ? (avail.length + ' 个可选') : '无候选';
  }
  function applyVerification(platform, verification) {
    const v = verification && typeof verification === 'object' ? verification : null;
    const stateName = v && (v.state === 'confirmed' || v.state === 'mismatch' ||
      v.state === 'unconfirmed') ? v.state : 'unconfirmed';
    state.lastVerify[platform] = {
      state: stateName,
      observed: v && str(v.observed) !== null ? String(v.observed) : null,
      requested: v && str(v.requested) !== null ? String(v.requested) : null,
    };
  }
  function verificationNode(platform) {
    const v = state.lastVerify[platform];
    if (!v) return null;
    if (v.state === 'confirmed') {
      const n = node('span', 'verify ok', '已回读确认：' + (v.observed || ''));
      return n;
    }
    if (v.state === 'mismatch') {
      return node('span', 'verify warn',
        '回读到的是 ' + (v.observed || '未知') + '，切换未生效');
    }
    return node('span', 'verify unknown', '未确认（回读不到当前模型）');
  }
  function buildModelSelect(platform, p, o) {
    const sel = document.createElement('select');
    sel.className = 'select';
    const avail = o.models.filter((m) => m.platforms.indexOf(platform) >= 0);
    if (!avail.length) {
      const op = document.createElement('option');
      op.value = '';
      op.textContent = '暂无可选模型';
      sel.appendChild(op);
      enable(sel, false);
      return sel;
    }
    if (p.model === null || !avail.some((m) => m.id === p.model)) {
      const ph = document.createElement('option');
      ph.value = p.model === null ? '' : p.model;
      ph.textContent = p.model === null ? '未配置' : (p.model + '（当前）');
      sel.appendChild(ph);
    }
    avail.forEach((m) => {
      const op = document.createElement('option');
      op.value = m.id;
      op.textContent = m.name + (m.id === p.model ? '（当前）' : '');
      if (p.model === m.id) op.selected = true;
      sel.appendChild(op);
    });
    if (p.model !== null && avail.some((m) => m.id === p.model)) sel.value = p.model;
    enable(sel, p.caps.model !== false);
    const oldValue = sel.value;
    sel.addEventListener('change', () => {
      const id = sel.value;
      if (!id) { sel.value = oldValue; return; }
      if (state.busy) { sel.value = oldValue; return; }
      run('正在切换模型…', async () => {
        let verification = null;
        try {
          const res = await apiPost('/api/model', { platform: platform, id: id });
          verification = res && res.verification;
        } catch (e) {
          sel.value = oldValue;
          showFailure(e);
          return;
        }
        // 无论 POST 是否带 verification，都要重新 GET overview 让界面回到真实状态。
        const ok = await loadOverview({ silent: true });
        applyVerification(platform, verification);
        renderPlatformGrid();               // 立即显示回读结果
        const v = state.lastVerify[platform];
        if (ok && v && v.state === 'confirmed') {
          setStatus('模型已切换并回读确认', 'ok', { at: state.lastLoadedAt });
        } else if (ok && v && v.state === 'mismatch') {
          setStatus('模型切换后回读到的是 ' + (v.observed || '其他模型') + '，请刷新确认', 'warn',
                    { at: state.lastLoadedAt });
        } else {
          setStatus('模型切换已提交，但回读不到当前模型，状态未确认', 'warn',
                    { at: state.lastLoadedAt });
        }
      });
    });
    return sel;
  }
  function renderPlatformGrid() {
    const grid = $('platformGrid');
    clear(grid);
    const o = state.overview;
    if (!o) { $('overviewLead').textContent = '正在读取状态…'; return; }
    [['qq', 'QQ'], ['wechat', '微信']].forEach((pair) => {
      const key = pair[0], label = pair[1], p = o[key];
      const card = node('div', 'platform-card');
      const head = node('div', 'platform-head');
      head.appendChild(node('div', 'platform-mark ' + key, key === 'qq' ? 'Q' : '微'));
      const copy = node('div');
      copy.appendChild(node('div', 'platform-name', label));
      const sub = node('div', 'platform-sub', PLATFORM_NAME[key]);
      copy.appendChild(sub);
      head.appendChild(copy);
      head.appendChild(connectionBadge(p));
      card.appendChild(head);

      // 1) 连接：上面徽章给结论，这里给依据（微信：心跳/监听/账号三项）。
      if (key === 'wechat') card.appendChild(kvRow('桥接', bridgeBits(p)));
      // 2) 自动回复：独立于连接。
      card.appendChild(kvRow('自动回复', runningBadge(p.running)));
      // 3) 客户端：是谁接进来的，不是连接状态。
      card.appendChild(kvRow('客户端', clientText(key, p), !p.client));
      const hint = clientHint(key, p);
      if (hint) card.appendChild(node('p', 'card-hint', hint));
      if (key === 'wechat') {
        const note = bridgeNote(p);
        if (note) card.appendChild(node('p', 'card-hint mono-hint', note));
      }
      // 4) 模型：当前 + 候选 + 回读结果。
      const modelBox = node('div', 'model-box');
      const curLine = node('div', 'kv');
      curLine.appendChild(node('span', 'k', '当前模型'));
      const curVal = node('span', 'v' + (p.model ? ' mono' : ' unset'));
      curVal.textContent = p.model || '未确认';
      curLine.appendChild(curVal);
      modelBox.appendChild(curLine);
      const pickerLine = node('div', 'kv');
      pickerLine.appendChild(node('span', 'k', '切换模型'));
      const pv = node('span', 'v model-pick');
      pv.appendChild(buildModelSelect(key, p, o));
      pickerLine.appendChild(pv);
      modelBox.appendChild(pickerLine);
      const candLine = node('div', 'kv');
      candLine.appendChild(node('span', 'k', '候选'));
      candLine.appendChild(node('span', 'v unset',
        modelDisplay(o.models.filter((m) => m.platforms.indexOf(key) >= 0))));
      modelBox.appendChild(candLine);
      const vn = verificationNode(key);
      if (vn) {
        const vline = node('div', 'kv verify-line');
        vline.appendChild(node('span', 'k', '回读'));
        const vv = node('span', 'v');
        vv.appendChild(vn);
        vline.appendChild(vv);
        modelBox.appendChild(vline);
      }
      card.appendChild(modelBox);
      grid.appendChild(card);
    });
    const unknown = PLATFORM_KEYS.filter((k) => o[k].connected === null);
    const down = PLATFORM_KEYS.filter((k) => o[k].connected === false);
    if (unknown.length) {
      $('overviewLead').textContent = unknown.map((key) => PLATFORM_NAME[key]).join('、') + '状态待确认';
    } else if (down.length) {
      $('overviewLead').textContent = down.map((key) => PLATFORM_NAME[key]).join('、') + '连接待处理';
    } else {
      $('overviewLead').textContent = 'QQ 和微信均已连接';
    }
  }

  // ---- 渲染：会话表格 --------------------------------------------------------
  function showCellError(errNode, msg) {
    errNode.textContent = msg || '';
    errNode.hidden = !msg;
  }
  function buildSelectControl(options, current, placeholder) {
    const sel = document.createElement('select');
    sel.className = 'select compact';
    if (current === null) {
      const ph = document.createElement('option');
      ph.value = '';
      ph.textContent = placeholder || '未配置';
      sel.appendChild(ph);
    }
    options.forEach((op) => {
      const o = document.createElement('option');
      o.value = op.id;
      o.textContent = optionName(op);
      if (current === op.id) o.selected = true;
      sel.appendChild(o);
    });
    if (current !== null && !options.some((op) => op.id === current)) {
      // 当前值不属于该平台可选项：如只读展示，禁用且 value 为空，避免误提交。
      const o = document.createElement('option');
      o.value = '';
      o.textContent = current + '（当前值 · 本平台不适用）';
      o.disabled = true;
      o.selected = true;
      sel.appendChild(o);
    }
    if (current !== null && options.some((op) => op.id === current)) sel.value = current;
    return sel;
  }
  function buildEditableCell(c, field, o) {
    const td = node('td', 'cell-ctl');
    const err = node('span', 'cell-error');
    err.hidden = true;

    let control, get, revert, validate, invalidMsg, hintText = '';

    if (field === 'enabled') {
      const label = node('label', 'switch' + (c.enabled === null ? ' is-unknown' : ''));
      if (c.enabledHint) label.title = c.enabledHint;
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = (c.enabled === true);
      const track = node('span', 'track');
      const st = node('span', 'state', c.enabled === true ? '开' : (c.enabled === false ? '关' : '未配置'));
      label.appendChild(input); label.appendChild(track); label.appendChild(st);
      control = label;
      enable(input, true);
      get = () => input.checked;
      revert = () => {
        input.checked = (c.enabled === true);
        st.textContent = c.enabled === true ? '开' : (c.enabled === false ? '关' : '未配置');
        label.classList.toggle('is-unknown', c.enabled === null);
      };
      input.addEventListener('change', () => {
        st.textContent = input.checked ? '开' : '关';
        label.classList.remove('is-unknown');
        onChange();
      });
      validate = (v) => typeof v === 'boolean';
      invalidMsg = '开关值无效。';
      hintText = c.enabledHint || '';
    } else if (field === 'style' || field === 'frequency') {
      const source = field === 'style' ? o.styleOptions : frequencyOptions(o, c.platform);
      // 按当前会话平台过滤，避免把微信人设 id 提交给 QQ 会话（服务端会拒绝）。
      const options = optionsForPlatform(source, c.platform);
      const sel = buildSelectControl(options, c[field], '未配置');
      control = sel;
      enable(sel, options.length > 0 || c[field] !== null);
      get = () => sel.value;
      revert = () => { sel.value = (c[field] === null ? '' : String(c[field])); };
      sel.addEventListener('change', onChange);
      validate = (v) => typeof v === 'string' && v.length > 0;
      invalidMsg = '请先选择一个有效选项。';
      if (!options.length) hintText = '暂无可选项';
      if (field === 'frequency') hintText = '主动接话的频繁程度';
    } else { // limit
      const input = document.createElement('input');
      input.type = 'number';
      input.className = 'input compact';
      input.min = '0';
      input.max = '10000';   // 服务端固定 0–10000，不依赖 limitMax 字段
      input.step = '1';
      input.placeholder = '未配置';
      input.value = (c.limit === null ? '' : String(c.limit));
      control = input;
      enable(input, true);
      get = () => {
        const raw = input.value.trim();
        if (raw === '') return null;
        const n = Number(raw);
        return (Number.isInteger(n) && n >= 0 && n <= 10000) ? n : NaN;
      };
      revert = () => { input.value = (c.limit === null ? '' : String(c.limit)); };
      input.addEventListener('input', onChange);
      validate = (v) => typeof v === 'number' && Number.isInteger(v) && v >= 0 && v <= 10000;
      invalidMsg = '请输入 0–10000 的整数；0 表示不限制。';
      hintText = '0 表示不限制';
      // limitOptions 作为数字输入的预设建议（合同仍以数字提交）。
      const numeric = optionsForPlatform(o.limitOptions, c.platform).filter((op) => {
        const n = Number(op.id);
        return Number.isInteger(n) && n >= 0 && n <= 10000;
      });
      if (numeric.length) {
        const dl = document.createElement('datalist');
        dl.id = 'limit-suggest-' + c.platform + '-' + c.id.replace(/[^A-Za-z0-9_-]/g, '');
        numeric.forEach((op) => {
          const option = document.createElement('option');
          option.value = op.id;
          if (op.name) option.setAttribute('label', op.name);
          dl.appendChild(option);
        });
        input.setAttribute('list', dl.id);
        td.appendChild(dl);
      }
    }

    const saveBtn = node('button', 'btn-mini', '保存');
    saveBtn.type = 'button';
    enable(saveBtn, false);
    function onChange() {
      enable(saveBtn, true);
      showCellError(err, '');
    }
    saveBtn.addEventListener('click', () => {
      if (state.busy || saveBtn.disabled) return;
      const value = get();
      if (!validate(value)) { showCellError(err, invalidMsg); return; }
      run('正在保存…', async () => {
        try {
          await saveConversationSetting(c.platform, c.id, field, value, false);
        } catch (e) {
          revert();
          enable(saveBtn, false);
          showCellError(err, e.message);
          showFailure(e);
        }
      });
    });

    td.insertBefore(control, td.firstChild);
    td.appendChild(saveBtn);
    if (hintText) td.appendChild(node('span', 'cell-hint', hintText));
    td.appendChild(err);
    return td;
  }
  async function saveConversationSetting(platform, id, field, value, isGlobal) {
    await apiPost('/api/conversation', { platform, id, field, value });
    if (!await loadOverview({ silent: true })) return;
    const overview = state.overview;
    const saved = isGlobal
      ? overview.globalSettings.find((g) => g.platform === platform && g.scope === id && g.field === field)
      : overview.conversations.find((c) => c.platform === platform && c.id === id);
    const observed = saved ? saved[isGlobal ? 'value' : field] : null;
    const stamp = { at: state.lastLoadedAt };
    if (observed === value) {
      setStatus('已保存并回读确认', 'ok', stamp);
    } else if (observed == null) {
      setStatus('已提交，未能确认当前设置值', 'warning', stamp);
    } else {
      setStatus('提交后读到的设置值与选择不一致', 'warning', stamp);
    }
  }

  // 微信人设是全局预设：一个控件管所有会话。
  function renderGlobalSettings() {
    const card = $('globalCard');
    const body = $('globalBody');
    clear(body);
    const o = state.overview;
    const list = o ? o.globalSettings : [];
    const writable = list.filter((g) => g.scope && g.capabilities.style);
    card.hidden = writable.length === 0;
    if (!writable.length) return;
    writable.forEach((g) => {
      const row = node('div', 'global-row');
      row.appendChild(node('div', 'global-label', g.label || '全局设置'));
      const options = g.styleOptions.length ? g.styleOptions : o.styleOptions;
      const sel = buildSelectControl(options, g.value, '未配置');
      enable(sel, options.length > 0);
      const saveBtn = node('button', 'btn-mini', '保存');
      saveBtn.type = 'button';
      enable(saveBtn, false);
      const err = node('span', 'cell-error');
      err.hidden = true;
      sel.addEventListener('change', () => { enable(saveBtn, true); showCellError(err, ''); });
      saveBtn.addEventListener('click', () => {
        if (state.busy || saveBtn.disabled) return;
        const value = sel.value;
        if (typeof value !== 'string' || !value) { showCellError(err, '请先选择一个有效选项。'); return; }
        run('正在保存…', async () => {
          try {
            await saveConversationSetting(g.platform, g.scope, 'style', value, true);
          } catch (e) {
            sel.value = (g.value === null ? '' : String(g.value));
            enable(saveBtn, false);
            showCellError(err, e.message);
            showFailure(e);
          }
        });
      });
      row.appendChild(sel);
      row.appendChild(saveBtn);
      row.appendChild(err);
      body.appendChild(row);
    });
  }
  // 只读项的显示值（真实值照实显示，绝不假装成逐会话可改）。
  function readOnlyText(c, field, o) {
    const raw = c[field];
    if (raw === null || raw === undefined) return '—';
    if (field === 'style') {
      const op = o.styleOptions.find((x) => x.id === raw);
      return op ? op.name : String(raw);
    }
    if (field === 'frequency') return FREQ_LABEL[raw] || String(raw);
    if (field === 'limit') return (raw === 0 ? '不限' : String(raw) + ' 条/天');
    return (raw === true ? '开' : raw === false ? '关' : String(raw));
  }
  // 为什么这一项不能改：三种不同的原因必须分开说，不能都写成「不支持」。
  // capabilities 表示「这个平台到底有没有这一项」；值为 null 表示「这次没读到」。
  //   wechat-global  微信全局（改一次影响所有会话，去「微信全局人设」改）
  //   unknown        平台有这一项，但没读到值 -> 未确认
  //   unsupported    平台本来就没有这一项（例如微信没有每日限额）
  function readOnlyReason(c, field) {
    if (c.platform === 'wechat' && (field === 'style' ||
        (field === 'frequency' && c.kind === 'group'))) return 'wechat-global';
    if (!c.capabilities[field] && c[field] === null) return 'unsupported';
    if (c[field] === null) return 'unknown';
    return 'unsupported';
  }
  function readOnlyChip(c, field, o) {
    const reason = readOnlyReason(c, field);
    const label = reason === 'wechat-global' ? '微信全局'
      : (reason === 'unknown' ? '未确认' : '不支持');
    const span = node('span', 'ro-chip ' + reason, label);
    span.setAttribute('title', reason === 'wechat-global'
      ? (field === 'style' ? '在下方设置微信群人设或兼容链路的默认人设' : '此频率适用于全部微信群')
      : (reason === 'unknown'
        ? '没有读到该会话的这一项，界面不做猜测'
        : '该平台不支持按会话修改此项'));
    return span;
  }
  // 每行（表格）与每张卡（手机）共用同一套字段渲染：桌面和手机看到的数据一致。
  function appendFields(host, c, o, asCells) {
    ['enabled', 'style', 'frequency', 'limit'].forEach((field) => {
      let target;
      if (asCells) {
        target = document.createElement('td');
      } else {
        target = node('div', 'field-row');
        target.appendChild(node('span', 'field-key',
          field === 'enabled' && c.enabledLabel ? c.enabledLabel : FIELD_NAME[field]));
      }
      if (c.capabilities[field]) {
        const ctl = buildEditableCell(c, field, o);
        if (asCells) {
          target = ctl;
        } else {
          // 手机卡片里不想要 td 的样式类，把控件搬进 div 行。
          target.appendChild(node('div', 'field-ctl'));
          const box = target.lastChild;
          while (ctl.firstChild) box.appendChild(ctl.firstChild);
        }
        host.appendChild(target);
      } else {
        target.appendChild(node('span', 'cell-unset', readOnlyText(c, field, o)));
        target.appendChild(readOnlyChip(c, field, o));
        host.appendChild(target);
      }
    });
  }
  function filteredConversations() {
    const o = state.overview;
    const list = o ? o.conversations : [];
    if (state.filter === 'all') return list;
    return list.filter((c) => c.platform === state.filter);
  }
  function renderFilterChips(all) {
    const counts = { all: all.length, qq: 0, wechat: 0 };
    all.forEach((c) => { counts[c.platform] = (counts[c.platform] || 0) + 1; });
    const chips = document.querySelectorAll('#convFilters [data-filter]');
    for (let i = 0; i < chips.length; i++) {
      const key = chips[i].getAttribute('data-filter');
      const on = key === state.filter;
      chips[i].classList.toggle('is-on', on);
      chips[i].setAttribute('aria-pressed', on ? 'true' : 'false');
      const n = chips[i].querySelector('[data-count]');
      if (n) n.textContent = String(counts[key] || 0);
    }
  }
  function conversationMeta(c) {
    const bits = [];
    if (c.kind === 'group') bits.push('群聊');
    else if (c.kind === 'private') bits.push('私聊');
    bits.push('ID ' + c.id);
    return bits.join(' · ');
  }
  function renderConversationCards(o, list) {
    const box = $('convCards');
    clear(box);
    list.forEach((c) => {
      const open = state.expanded[c.id] === true;
      const card = node('div', 'conv-card' + (open ? ' is-open' : ''));
      card.setAttribute('data-platform', c.platform);
      const headBtn = document.createElement('button');
      headBtn.type = 'button';
      headBtn.className = 'conv-card-head';
      headBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
      headBtn.appendChild(node('span', 'platform-pill ' + c.platform,
        PLATFORM_NAME[c.platform] || c.platform));
      const title = node('span', 'conv-card-title', c.name);
      headBtn.appendChild(title);
      headBtn.appendChild(node('span', 'conv-card-meta', conversationMeta(c)));
      headBtn.appendChild(node('span', 'chevron', open ? '收起' : '展开'));
      headBtn.addEventListener('click', () => {
        state.expanded[c.id] = !open;
        renderConversations();
      });
      card.appendChild(headBtn);
      if (open) {
        const body = node('div', 'conv-card-body');
        appendFields(body, c, o, false);
        card.appendChild(body);
      }
      box.appendChild(card);
    });
  }
  function renderConversations() {
    const body = $('convBody');
    clear(body);
    const o = state.overview;
    const all = o ? o.conversations : [];
    const list = filteredConversations();
    renderFilterChips(all);
    $('convCount').textContent = list.length
      ? (list.length + ' 个会话' + (state.filter === 'all' ? '' : '（已筛选）'))
      : '';
    $('convEmpty').hidden = all.length > 0;
    $('convFilteredEmpty').hidden = !(all.length > 0 && list.length === 0);
    if (list.length === 0) {
      clear($('convCards'));
      return;
    }
    list.forEach((c) => {
      const tr = document.createElement('tr');
      const pcell = document.createElement('td');
      pcell.appendChild(node('span', 'platform-pill ' + c.platform, PLATFORM_NAME[c.platform] || c.platform));
      tr.appendChild(pcell);
      const ncell = document.createElement('td');
      ncell.appendChild(node('div', 'cell-name', c.name));
      ncell.appendChild(node('div', 'cell-meta', conversationMeta(c)));
      tr.appendChild(ncell);
      appendFields(tr, c, o, true);
      body.appendChild(tr);
    });
    renderConversationCards(o, list);
  }
  function setFilter(key) {
    state.filter = (key === 'qq' || key === 'wechat') ? key : 'all';
    renderConversations();
    syncDisabled();
  }

  // ---- 渲染：诊断列表 --------------------------------------------------------
  function renderDiagnostics() {
    const list = $('diagList');
    clear(list);
    const o = state.overview;
    const items = o ? o.diagnostics : [];
    $('diagCount').textContent = items.length ? (items.length + ' 条') : '';
    $('diagEmpty').hidden = items.length > 0;
    const levelBadge = { info: ['提示', 'info'], warning: ['警告', 'warning'], error: ['错误', 'error'] };
    items.forEach((d) => {
      const li = node('li', 'diag-item ' + d.level);
      const lb = levelBadge[d.level];
      li.appendChild(badge(lb[0], lb[1]));
      li.appendChild(node('span', 'who', PLATFORM_NAME[d.platform] || '系统'));
      li.appendChild(node('span', 'msg', d.message || '（无说明）'));
      list.appendChild(li);
    });
  }

  function renderAll() {
    renderPlatformGrid();
    renderGlobalSettings();
    renderConversations();
    renderDiagnostics();
    syncDisabled();
  }

  // ---- 启动 / 事件绑定 ------------------------------------------------------
  function wireAvatar() {
    const img = document.querySelector('[data-avatar]');
    const fallback = document.querySelector('[data-avatar-fallback]');
    if (!img) return;
    const fail = () => { img.hidden = true; if (fallback) fallback.hidden = false; };
    img.addEventListener('error', fail);
    if (img.complete && img.naturalWidth === 0) fail();
  }
  function wireThemePicker() {
    const sel = document.querySelector('[data-theme-select]');
    if (!sel) return;
    sel.value = themePref;
    const note = $('themeNote');
    const syncNote = () => {
      if (!note) return;
      const eff = effectiveTheme(themePref);
      note.textContent = themePref === 'system'
        ? ('正在跟随系统：当前为' + (eff === 'dark' ? '深色' : '浅色') + '。')
        : ('当前固定为' + (eff === 'dark' ? '深色' : '浅色') + '，不随系统变化。');
    };
    syncNote();
    sel.addEventListener('change', () => {
      const v = THEME_VALUES.indexOf(sel.value) >= 0 ? sel.value : 'system';
      storeThemePref(v);     // 只存非敏感显示偏好
      applyTheme(v);         // 即时切换，无需刷新
      syncNote();
    });
  }
  // 平台筛选：只改显示，不发请求；手机和桌面用同一份状态。
  function wireFilters() {
    const chips = document.querySelectorAll('#convFilters [data-filter]');
    for (let i = 0; i < chips.length; i++) {
      chips[i].addEventListener('click', () => {
        setFilter(chips[i].getAttribute('data-filter'));
      });
    }
  }

  async function refreshChannels() {
    const mount = $('channelInventory');
    const button = $('channelRefresh');
    if (!mount || !button || button.disabled || !state.session || !state.session.authenticated) return;
    button.disabled = true;
    try {
      const data = await apiGet('/api/channels');
      const channels = Array.isArray(data.channels) ? data.channels : [];
      const labels = { ok: '在线', offline: '离线', qr: '待扫码', disabled: '已停用', unknown: '未确认' };
      clear(mount);
      channels.forEach((channel) => {
        if (!channel || !['qq', 'wechat'].includes(channel.platform)) return;
        const title = node('strong', '', PLATFORM_NAME[channel.platform]);
        const meta = node('span', 'channel-meta', channel.client || '客户端未报告');
        const label = node('span', 'channel-state', labels[channel.state] || labels.unknown);
        label.dataset.state = ['ok', 'offline', 'qr', 'disabled', 'unknown'].includes(channel.state)
          ? channel.state : 'unknown';
        const row = node('div', 'channel-row');
        row.append(title, meta, label);
        mount.appendChild(row);
      });
      if (!mount.firstChild) mount.textContent = '暂未读到平台连接状态。';
    } catch (error) {
      if (error.status === 401) { handleUnauthorized(); return; }
      mount.textContent = '连接状态读取失败，稍后可重试。';
    } finally {
      button.disabled = false;
    }
  }

  async function refreshDelivery() {
    const mount = $('deliverySummary');
    const button = $('deliveryRefresh');
    if (!mount || !button || button.disabled || !state.session || !state.session.authenticated) return;
    button.disabled = true;
    try {
      const report = await apiGet('/api/delivery/summary');
      clear(mount);
      const observation = { partial: '部分数据可读', unavailable: '暂未读到回执' };
      const rows = [
        ['qq', 'QQ 核心当前计数', [
          ['当前待处理', 'queueDepth'], ['OneBot 已确认', 'confirmed'], ['结果未知', 'unknown'],
        ]],
        ['wechat', '微信桥接最近 24 小时', [
          ['会话内确认', 'confirmed'], ['结果未知', 'unknown'], ['提交待核验', 'pending'],
        ]],
      ];
      rows.forEach(([platform, scope, metrics]) => {
        const data = report[platform] || {};
        const card = node('article', 'card');
        const list = node('dl', 'meta-list');
        metrics.forEach(([label, key]) => {
          const row = node('div', 'meta-row');
          const count = Number.isSafeInteger(data[key]) && data[key] >= 0 ? String(data[key]) : '—';
          row.append(node('dt', '', label), node('dd', '', count));
          list.appendChild(row);
        });
        card.append(node('h4', 'card-title', PLATFORM_NAME[platform]), node('p', 'card-note', scope), list);
        if (observation[data.status]) card.appendChild(node('p', 'card-note', observation[data.status]));
        mount.appendChild(card);
      });
    } catch (error) {
      if (error.status === 401) { handleUnauthorized(); return; }
      mount.textContent = '回执读取失败，稍后可重试。';
    } finally {
      button.disabled = false;
    }
  }

  let memoryStorageRequest = 0;

  function clearMemoryStorage() {
    ++memoryStorageRequest;
    $('memoryStorageRows').replaceChildren();
    $('memoryStorageStatus').textContent = '';
    $('memoryStorageRefresh').disabled = false;
  }

  async function refreshMemoryStorage() {
    if (!$('memoryStorage').open || !state.session || !state.session.authenticated) return;
    const request = ++memoryStorageRequest;
    const button = $('memoryStorageRefresh');
    const status = $('memoryStorageStatus');
    const mount = $('memoryStorageRows');
    button.disabled = true;
    status.textContent = '正在读取存储状态…';
    mount.replaceChildren();
    try {
      const result = await apiGet('/api/memory/storage');
      if (request !== memoryStorageRequest) return;
      const labels = {
        available: '已建立', empty: '尚未建立会话存储',
        partial: '扫描不完整', unavailable: '无法读取',
      };
      const count = (value) => Number.isSafeInteger(value) && value >= 0 ? String(value) : '—';
      const bytes = (value) => {
        if (!Number.isSafeInteger(value) || value < 0) return '—';
        if (value < 1024) return value + ' B';
        return value < 1024 ** 2 ? (value / 1024).toFixed(1) + ' KiB'
          : (value / (1024 ** 2)).toFixed(2) + ' MiB';
      };
      for (const [key, title] of [
        ['qqReliable', 'QQ 可靠记忆'], ['wechatSwift', '微信群长期记忆'],
        ['wechatCompat', '微信兼容链记忆'],
      ]) {
        const store = result.stores[key] || {};
        const list = node('dl', 'meta-list');
        const values = [
          ['状态', labels[store.state] || labels.unavailable],
          ['会话目录', count(store.scopes)], ['文件大小', bytes(store.bytes)],
        ];
        if (key !== 'wechatCompat') values.push(['状态备份', count(store.stateBackups)]);
        for (const [label, value] of values) {
          const row = node('div', 'meta-row');
          row.append(node('dt', '', label), node('dd', '', value));
          list.appendChild(row);
        }
        const card = node('article', 'memory-record');
        card.append(node('h4', '', title), list);
        mount.appendChild(card);
      }
      status.textContent = '';
    } catch (error) {
      if (request !== memoryStorageRequest) return;
      if (error.status === 401) { handleUnauthorized(); return; }
      status.textContent = '存储状态读取失败，请刷新重试。';
    } finally {
      if (request === memoryStorageRequest) button.disabled = false;
    }
  }

  let memoryRequest = 0;
  let memoryEdit = null;
  let savingMemory = false;
  const memoryRoute = '/api/memory/wechat';
  const wechatAccountPath = '/api/account/wechat';
  const wechatBridgeLabels = {
    ready: ['桥接监听正常', 'ok'],
    window_pending: ['桥接等待微信窗口就绪', 'scan'],
    listener_paused: ['桥接在线，消息监听暂停', 'scan'],
    login_pending: ['桥接在线，等待客户端登录', 'scan'],
    account_blocked: ['桥接在线，账号校验未通过', 'offline'],
    offline: ['桥接心跳未收到', 'offline'],
    disabled: ['桥接已停用', 'unknown'],
    unknown: ['桥接状态待确认', 'unknown'],
  };
  let wechatAccountRequest = 0;
  let wechatAccountBusy = false;
  let wechatAccountPoll = 0;
  let wechatQrExpires = 0;
  let wechatQrDeadline = 0;
  const WECHAT_QR_LIFETIME_MS = 30_000;

  function hideWeChatQr() {
    clearTimeout(wechatQrDeadline);
    wechatQrDeadline = 0;
    wechatQrExpires = 0;
    const image = $('wechatAccountQr');
    image.hidden = true;
    image.removeAttribute('src');
  }

  function showWeChatBridge(stateName) {
    const label = wechatBridgeLabels[stateName] || wechatBridgeLabels.unknown;
    const field = $('wechatBridgeStatus');
    field.textContent = label[0];
    field.dataset.state = label[1];
  }

  function closeWeChatAccount() {
    ++wechatAccountRequest;
    wechatAccountBusy = false;
    clearTimeout(wechatAccountPoll);
    hideWeChatQr();
    $('wechatAccountStatus').textContent = '';
    showWeChatBridge('unknown');
    $('wechatAccountRefresh').disabled = false;
    $('wechatAccountGetQr').disabled = true;
    $('wechatAccountReconnect').disabled = true;
  }

  async function requestWeChatAccount(path, method, body, timeoutMs) {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    let timer;
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => {
        reject(apiError('微信账号请求超时，请刷新状态确认。', 0));
        if (controller) controller.abort();
      }, timeoutMs);
    });
    try {
      return await Promise.race([request(path, method, body, controller && controller.signal), deadline]);
    } finally {
      clearTimeout(timer);
    }
  }

  async function readWeChatAccount(action) {
    if (wechatQrExpires && Date.now() >= wechatQrExpires) hideWeChatQr();
    if (wechatAccountBusy || !$('wechatAccount').open || !state.session || !state.session.authenticated) return;
    if (action === 'reconnect' && !window.confirm('重连微信桥接会短暂暂停微信消息处理。确认重连吗？')) return;
    const request = ++wechatAccountRequest;
    const status = $('wechatAccountStatus');
    const image = $('wechatAccountQr');
    const scan = $('wechatAccountGetQr');
    const reconnect = $('wechatAccountReconnect');
    const refresh = $('wechatAccountRefresh');
    clearTimeout(wechatAccountPoll);
    if (action) hideWeChatQr();
    wechatAccountBusy = true;
    refresh.disabled = scan.disabled = reconnect.disabled = true;
    status.textContent = action === 'reconnect' ? '正在重连微信桥接…' : '正在读取微信窗口…';
    let result;
    try {
      if (action === 'reconnect') {
        await requestWeChatAccount(wechatAccountPath + '/reconnect', 'POST', { confirmed: true }, 30_000);
      }
      result = action === 'qr'
        ? await requestWeChatAccount(wechatAccountPath + '/qr', 'POST', {}, 12_000)
        : await requestWeChatAccount(wechatAccountPath, 'GET', undefined, 10_000);
      if (request !== wechatAccountRequest) return;
      status.textContent = (action === 'reconnect' ? '桥接已重连。' : '') + result.label;
      status.dataset.state = result.state;
      if (action !== 'qr') showWeChatBridge(result.bridgeStatus);
      if (result.state === 'scan' && typeof result.image === 'string'
          && result.image.startsWith('data:image/png;base64,')) {
        image.src = result.image;
        image.hidden = false;
        wechatQrExpires = Date.now() + WECHAT_QR_LIFETIME_MS;
        wechatQrDeadline = setTimeout(hideWeChatQr, WECHAT_QR_LIFETIME_MS);
      }
      if (result.state !== 'scan' || Date.now() >= wechatQrExpires) hideWeChatQr();
      if (!image.hidden) wechatAccountPoll = setTimeout(() => readWeChatAccount(), 6000);
    } catch (error) {
      if (request !== wechatAccountRequest) return;
      if (error.status === 401) { handleUnauthorized(); return; }
      hideWeChatQr();
      status.textContent = error.message;
      if (action !== 'qr') showWeChatBridge('unknown');
    } finally {
      if (request === wechatAccountRequest) {
        wechatAccountBusy = false;
        refresh.disabled = false;
        scan.textContent = result && result.canOpenQr ? '打开微信扫码页' : '获取登录二维码';
        scan.disabled = !result || !(result.canScan === true || result.canOpenQr === true);
        reconnect.disabled = !result || result.canReconnect !== true;
      }
    }
  }
  const groupPersonaRoute = '/api/persona/wechat';
  let personaGroups = [];
  let personaRequest = 0;
  let personaBusy = false;

  function selectedWeChatPersonaGroup() {
    return personaGroups.find((group) => group.id === $('wechatPersonaGroup').value);
  }

  function updateWeChatPersonaControls(useCurrent) {
    const group = selectedWeChatPersonaGroup();
    const menu = $('wechatPersonaPreset');
    if (useCurrent) menu.value = group ? group.preset : '';
    $('wechatPersonaGroup').disabled = personaBusy || !personaGroups.length;
    $('wechatPersonaRefresh').disabled = personaBusy;
    menu.disabled = personaBusy || !group;
    $('wechatPersonaSave').disabled = personaBusy || !group || menu.value === group.preset;
  }

  function clearWeChatGroupPersonas() {
    ++personaRequest;
    personaGroups = [];
    personaBusy = false;
    $('wechatPersonaGroup').replaceChildren();
    $('wechatPersonaPreset').replaceChildren();
    $('wechatPersonaStatus').textContent = '';
    updateWeChatPersonaControls(true);
  }

  async function syncWeChatGroupPersonas(save) {
    if (personaBusy || !$('wechatGroupPersona').open || !state.session || !state.session.authenticated) return;
    const group = selectedWeChatPersonaGroup();
    const value = $('wechatPersonaPreset').value;
    if (save && (!group || group.preset === value)) return;
    const request = ++personaRequest;
    const status = $('wechatPersonaStatus');
    const menu = $('wechatPersonaGroup');
    const selection = menu.value;
    let accepted = false;
    personaBusy = true;
    updateWeChatPersonaControls(false);
    status.textContent = save ? '正在保存并回读…' : '正在读取微信群人设…';
    try {
      if (save) {
        await apiPost(groupPersonaRoute + '/' + group.id, { preset: value, previous: group.preset });
        accepted = true;
      }
      const result = await apiGet(groupPersonaRoute);
      if (request !== personaRequest) return;
      personaGroups = arr(result.groups);
      const counts = new Map();
      personaGroups.forEach((item) => counts.set(item.label, (counts.get(item.label) || 0) + 1));
      menu.replaceChildren(...personaGroups.map((item) => {
        const label = counts.get(item.label) > 1 ? item.label + ' · ' + item.accountCode : item.label;
        const option = node('option', '', label);
        option.value = item.id;
        return option;
      }));
      if (personaGroups.some((item) => item.id === selection)) menu.value = selection;
      const inherited = node('option', '', '沿用当前核心的默认人设');
      inherited.value = '';
      $('wechatPersonaPreset').replaceChildren(inherited, ...arr(result.styles).map((name) => node('option', '', name)));
      const current = selectedWeChatPersonaGroup();
      if (save) {
        status.textContent = current && current.id === group.id && current.preset === value
          ? '已保存并回读确认。' : '保存后的回读值已变化，请核对当前设置。';
      } else {
        status.textContent = current ? '适用于本群的 Swift 文本及兼容链路回复。' : '暂无可管理的微信群。';
      }
    } catch (error) {
      if (request !== personaRequest) return;
      if (error.status === 401) { handleUnauthorized(); return; }
      status.textContent = accepted ? '已提交，请刷新核对是否保存。' : error.message;
    } finally {
      if (request === personaRequest) {
        personaBusy = false;
        updateWeChatPersonaControls(true);
      }
    }
  }

  function stopMemoryEdit() {
    memoryEdit = null;
    $('wechatMemoryEdit').hidden = true;
    $('wechatMemoryValue').value = '';
  }

  function clearWeChatMemory() {
    ++memoryRequest;
    savingMemory = false;
    const menu = $('wechatMemoryGroup');
    menu.replaceChildren();
    menu.disabled = true;
    $('wechatMemoryRefresh').disabled = false;
    $('wechatMemorySave').disabled = false;
    $('wechatMemoryCancel').disabled = false;
    $('wechatMemoryValue').readOnly = false;
    $('wechatMemoryRows').replaceChildren();
    $('wechatMemoryStatus').textContent = '';
    stopMemoryEdit();
  }

  function renderWeChatMemory(entries, scope) {
    const mount = $('wechatMemoryRows');
    mount.replaceChildren();
    const sources = { conversation: '群聊陈述 · 待核实', manual: '群内手工保存', operator: '控制台更正' };
    const kinds = { task: '事项', fact: '事实', preference: '偏好', event: '活动' };
    for (const record of entries) {
      const item = node('article', 'memory-record');
      const text = node('div', '');
      const end = new Date(record.expiresAt * 1000);
      const expiry = Number.isFinite(end.getTime()) ? end.toLocaleString('zh-CN') : '到期时间待确认';
      text.append(node('h4', '', record.label), node('p', '', record.value),
        node('p', 'memory-meta', [kinds[record.kind], sources[record.source],
          record.paused ? '已暂停长期引用' : '', '到期 ' + expiry].filter(Boolean).join(' · ')));
      const actions = node('div', 'memory-actions');
      const edit = node('button', 'btn btn-quiet btn-sm', '更正');
      edit.type = 'button';
      edit.addEventListener('click', () => {
        if (savingMemory || $('wechatMemoryGroup').value !== scope) return;
        memoryEdit = { scope, id: record.id };
        $('wechatMemoryLabel').textContent = '更正 ' + record.label;
        $('wechatMemoryValue').value = record.value;
        $('wechatMemoryEdit').hidden = false;
        $('wechatMemoryValue').focus();
      });
      actions.append(edit);
      if (typeof record.paused === 'boolean') {
        const pause = node('button', 'btn btn-quiet btn-sm', record.paused ? '恢复引用' : '暂停引用');
        pause.type = 'button';
        pause.addEventListener('click', () => {
          if ($('wechatMemoryGroup').value !== scope) return;
          updateWeChatMemory(scope, { id: record.id, paused: !record.paused, previousPaused: record.paused });
        });
        actions.append(pause);
      }
      item.append(text, actions);
      mount.append(item);
    }
  }

  async function refreshWeChatMemory(loadGroups) {
    if (savingMemory || !$('wechatMemory').open || !state.session || !state.session.authenticated) return;
    const requestID = ++memoryRequest;
    const menu = $('wechatMemoryGroup');
    const refresh = $('wechatMemoryRefresh');
    const status = $('wechatMemoryStatus');
    stopMemoryEdit();
    $('wechatMemoryRows').replaceChildren();
    menu.disabled = true;
    refresh.disabled = true;
    status.textContent = '正在读取群记忆…';
    try {
      if (loadGroups) {
        const previous = menu.value;
        const data = await apiGet(memoryRoute);
        if (requestID !== memoryRequest) return;
        const groups = arr(data && data.groups);
        const labels = new Map();
        groups.forEach((group) => labels.set(group.label, (labels.get(group.label) || 0) + 1));
        menu.replaceChildren(...groups.map((group) => {
          const label = labels.get(group.label) > 1 ? group.label + ' · ' + group.accountCode : group.label;
          const option = node('option', '', label);
          option.value = group.id;
          return option;
        }));
        if (groups.some((group) => group.id === previous)) menu.value = previous;
      }
      const group = menu.value;
      if (!group) {
        status.textContent = '暂无可管理的微信群。';
        return;
      }
      const data = await apiGet(memoryRoute + '/' + group);
      if (requestID !== memoryRequest) return;
      const entries = arr(data && data.entries);
      renderWeChatMemory(entries, group);
      status.textContent = entries.length ? '已读取 ' + entries.length + ' 条当前记忆。' : '该群暂无当前记忆。';
    } catch (error) {
      if (requestID !== memoryRequest) return;
      if (error.status === 401) { handleUnauthorized(); return; }
      status.textContent = error.message;
    } finally {
      if (requestID === memoryRequest) {
        menu.disabled = !menu.options.length;
        refresh.disabled = false;
      }
    }
  }

  function saveWeChatMemory(event) {
    event.preventDefault();
    if (memoryEdit) {
      updateWeChatMemory(memoryEdit.scope, { id: memoryEdit.id, value: $('wechatMemoryValue').value.trim() });
    }
  }

  async function updateWeChatMemory(scope, change) {
    if (savingMemory || !$('wechatMemory').open || !state.session || !state.session.authenticated) return;
    const action = typeof change.paused !== 'boolean' ? '更正' : change.paused ? '暂停引用' : '恢复引用';
    const input = $('wechatMemoryValue');
    const status = $('wechatMemoryStatus');
    const requestID = memoryRequest;
    let accepted = false;
    savingMemory = true;
    const locked = ['wechatMemoryGroup', 'wechatMemoryRefresh', 'wechatMemorySave', 'wechatMemoryCancel'].map($);
    locked.push(...$('wechatMemoryRows').querySelectorAll('button'));
    locked.forEach((control) => { control.disabled = true; });
    input.readOnly = true;
    status.textContent = '正在保存并回读…';
    try {
      const path = memoryRoute + '/' + scope;
      const saved = await apiPost(path, change);
      accepted = true;
      const current = await apiGet(path);
      if (requestID !== memoryRequest) return;
      const entries = arr(current && current.entries);
      if (!saved.entry || (typeof change.paused === 'boolean' && saved.entry.paused !== change.paused)
          || !entries.some((entry) => entry.id === saved.entry.id && entry.value === saved.entry.value
                            && entry.paused === saved.entry.paused)) {
        throw new Error(action + '已提交，回读未确认。请刷新核对。');
      }
      stopMemoryEdit();
      renderWeChatMemory(entries, scope);
      status.textContent = action + '已保存并回读确认。';
    } catch (error) {
      if (requestID !== memoryRequest) return;
      if (error.status === 401) { handleUnauthorized(); return; }
      status.textContent = accepted ? action + '已提交，回读未确认。请刷新核对。' : error.message;
    } finally {
      if (requestID === memoryRequest) {
        savingMemory = false;
        locked.forEach((control) => { control.disabled = false; });
        input.readOnly = false;
      }
    }
  }

  function wireWorkspace() {
    const links = Array.from(document.querySelectorAll('[data-workspace-view]'));
    const panes = Array.from(document.querySelectorAll('[data-workspace-pane]'));
    function displayView() {
      const requested = window.location.hash.slice(1);
      const target = links.some((link) => link.dataset.workspaceView === requested)
        ? requested : 'hOverview';
      links.forEach((link) => {
        if (link.dataset.workspaceView === target) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
      });
      panes.forEach((pane) => { pane.hidden = pane.dataset.workspacePane !== target; });
      syncHostStatus();
      if (target === 'hSettings') refreshChannels();
      else if ($('wechatAccount').open) {
        $('wechatAccount').open = false;
        closeWeChatAccount();
      }
      if (target === 'hDiagnostics') refreshDelivery();
      if (target === 'hConversations') refreshWeChatMemory(true);
    }
    window.addEventListener('hashchange', displayView);
    links.forEach((link) => link.addEventListener('click', () => {
      if (window.location.hash === link.hash) displayView();
    }));
    const refreshButton = $('channelRefresh');
    if (refreshButton) refreshButton.addEventListener('click', refreshChannels);
    const deliveryButton = $('deliveryRefresh');
    if (deliveryButton) deliveryButton.addEventListener('click', refreshDelivery);
    displayView();
  }

  function init() {
    wireAvatar();
    wireThemePicker();
    wireFilters();
    renderHostStatus();
    const actions = document.querySelectorAll('button[data-action]');
    for (let i = 0; i < actions.length; i++) enable(actions[i], true);

    $('setupForm').addEventListener('submit', onSetupSubmit);
    $('loginForm').addEventListener('submit', onLoginSubmit);
    $('passwordForm').addEventListener('submit', onPasswordSubmit);
    $('refreshBtn').addEventListener('click', () => {
      if (state.busy) return;
      loadOverview();
      syncHostStatus(true);
    });
    $('logoutBtn').addEventListener('click', onLogout);
    $('hostHistory').addEventListener('change', renderHostStatus);
    document.addEventListener('visibilitychange', () => syncHostStatus());
    $('wechatMemory').addEventListener('toggle', () => refreshWeChatMemory(true));
    $('memoryStorage').addEventListener('toggle', () => {
      if ($('memoryStorage').open) refreshMemoryStorage();
      else clearMemoryStorage();
    });
    $('memoryStorageRefresh').addEventListener('click', refreshMemoryStorage);
    $('wechatMemoryGroup').addEventListener('change', () => refreshWeChatMemory(false));
    $('wechatMemoryRefresh').addEventListener('click', () => refreshWeChatMemory(true));
    $('wechatMemoryEdit').addEventListener('submit', saveWeChatMemory);
    $('wechatMemoryCancel').addEventListener('click', stopMemoryEdit);
    $('wechatGroupPersona').addEventListener('toggle', () => syncWeChatGroupPersonas(false));
    $('wechatPersonaRefresh').addEventListener('click', () => syncWeChatGroupPersonas(false));
    $('wechatPersonaGroup').addEventListener('change', () => updateWeChatPersonaControls(true));
    $('wechatPersonaPreset').addEventListener('change', () => updateWeChatPersonaControls(false));
    $('wechatGroupPersonaForm').addEventListener('submit', (event) => {
      event.preventDefault();
      syncWeChatGroupPersonas(true);
    });
    $('wechatAccount').addEventListener('toggle', () => {
      if ($('wechatAccount').open) readWeChatAccount();
      else closeWeChatAccount();
    });
    $('wechatAccountRefresh').addEventListener('click', () => readWeChatAccount());
    $('wechatAccountGetQr').addEventListener('click', () => readWeChatAccount('qr'));
    $('wechatAccountReconnect').addEventListener('click', () => readWeChatAccount('reconnect'));

    // 先捕获并立刻移除 #pair= 片段，再决定屏幕。令牌只留在内存里。
    const frag = captureFragment();
    if (frag.token) storePair(frag.token, frag.reset);
    wireWorkspace();
    loadSession();
  }

  // app.js 现在是 <head> 里的阻塞式外部脚本（无 inline script，兼容 CSP）：
  // 主题已在上方同步应用，其余初始化等 DOM 就绪后再做。
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
