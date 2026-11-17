/* 大肥鱼统一控制台 · 内容阅读模块（社交平台只读阅读器）
 *
 * 定位：可拔插模块，不依赖 index.html 里预置的任何子元素。
 *   - 导出 window.DaFeiYuContent = { mount(rootEl), refresh(), version }
 *   - 页面加载后自动查找第一个 [data-content-root]；找到就挂载，找不到只注册、不报错。
 *   - 挂载后全部 DOM 在 rootEl 内部自建；类名统一 dc- 前缀，样式在 content.css。
 *
 * 只读：本模块不提供评论 / 转发 / 发布 / 点赞 / 私信等任何写操作入口，
 *       只有「读取 / 摘要 / 原文 / 问答 / 搜索 / 订阅开关 / 保存链接」。
 *
 * API 合同（字段名由服务端固定，本模块只读不猜）：
 *   GET  /api/content/capabilities
 *        -> {platforms:[{id,name,read,search,subscribe,reason,mode}], redirectMaxHops, limits:{...}, model:{configured,model}}
 *   GET  /api/content/feed?platform=all|<id>
 *        -> {items:[{id,platform,title,author,publishedAt,url,coverage,mode,excerpt,hasOriginal}], diagnostics:[{level,message}]}
 *   POST /api/content/read {url?|text?, platform?, title?, force?}
 *        -> {item, coverage, reason, trace:[{hop,url,allowed,reason}], degraded}
 *   POST /api/content/search {platform, keyword} -> {items:[...], limited:bool, reason}
 *   POST /api/content/subscribe {platform, keyword, enabled} -> {ok, subscription}
 *   POST /api/content/ask {id, question} -> {answer, cites:[{quote}], basis:summary|original}
 *
 * 约束：CSP 'self'（无 inline script / inline style / 外链资源）、无内联事件、
 *       服务端字符串一律 textContent 注入，禁止 innerHTML 拼接；
 *       fetch 一律 credentials:'same-origin' + cache:'no-store'；错误只显示中文原因。
 *
 * 可选服务端扩展字段（都做了防御式归一化，缺字段不影响渲染）：
 *   item.summary / item.keypoints / item.quotes / item.original / item.originalText
 *   item.reason / item.trace / item.degraded / item.publishedText
 *   服务端若另行下发 /api/content/item/{id} 提供全文，本模块也会在点开时尝试补齐。
 */
(function () {
  'use strict';

  /* ====================================================================== *
   * 常量
   * ====================================================================== */

  var API = {
    capabilities: '/api/content/capabilities',
    feed: '/api/content/feed',
    read: '/api/content/read',
    search: '/api/content/search',
    subscribe: '/api/content/subscribe',
    ask: '/api/content/ask',
    item: '/api/content/item/' // 可选：按 id 取全文（服务端未实现时静默忽略）
  };

  // 平台顺序与标记文字（服务端 capabilities 里也带 name，优先用服务的）
  var PLATFORMS = [
    { id: 'x', name: 'X', mark: 'X' },
    { id: 'douyin', name: '抖音', mark: '抖音' },
    { id: 'bilibili', name: 'B站', mark: 'B站' },
    { id: 'xiaohongshu', name: '小红书', mark: '小红书' }
  ];

  // coverage 八态：中文名 + 可读范围说明（服务端 reason 优先，这里只做兜底）
  var COVERAGE = {
    full: { name: '完整可见', scope: '已取得完整原文，可读全文。' },
    partial: { name: '部分可见', scope: '只取得部分正文，其余内容平台未提供。' },
    excerpt: { name: '仅摘录', scope: '只取得摘录片段，完整原文不可读。' },
    auth_required: { name: '需要登录', scope: '该内容需要登录授权才能读取，本工具不保存任何登录凭据。' },
    restricted: { name: '受限制', scope: '该内容受平台限制（地区 / 权限 / 转载限制），无法读取正文。' },
    expired: { name: '已失效', scope: '原链接已删除或过期，无法再读取。' },
    unavailable: { name: '暂时不可用', scope: '平台暂时无法访问，稍后再试。' },
    limited: { name: '已限流', scope: '读取过于频繁，被平台或限流策略拦下。' }
  };

  // 正文按 coverage 决定"实际能读到什么"
  var READABLE = { full: true, partial: true, excerpt: true };

  var DEFAULT_REASON = {
    full: '原文全文可读',
    partial: '仅取得部分正文',
    excerpt: '仅取得摘录，缺完整原文',
    auth_required: '需要登录才能读取正文',
    restricted: '内容受平台限制，无法读取正文',
    expired: '链接已失效',
    unavailable: '平台暂时无法访问',
    limited: '触发限流，稍后再试'
  };

  // 平台未配置时的降级说明
  var DEGRADE_TEXT = '该平台尚未配置读取权限（未授权 / 未配置），现在读不了它的页面上内容。' +
    '你仍然可以：把看到的正文直接粘贴到上面的输入框保存，或者只保存链接留档；' +
    '问答和摘要只能在有正文时使用。';

  var MAX_INPUT = 20000;

  /* ====================================================================== *
   * 归一化工具（字段缺失 / 类型不对一律降级，绝不把 undefined 打到 DOM）
   * ====================================================================== */

  function str(v) {
    return (typeof v === 'string' && v.trim()) ? v.trim() : '';
  }
  function num(v) {
    return (typeof v === 'number' && isFinite(v)) ? v : null;
  }
  function bool(v) {
    return (typeof v === 'boolean') ? v : null;
  }
  function arr(v) {
    return Array.isArray(v) ? v : [];
  }
  function obj(v) {
    return (v && typeof v === 'object' && !Array.isArray(v)) ? v : {};
  }
  function pickStr(o, keys) {
    for (var i = 0; i < keys.length; i++) {
      var v = str(o[keys[i]]);
      if (v) return v;
    }
    return '';
  }
  // 只允许 http(s) 链接进入 href，避免 javascript: / data: 之类
  function safeHref(v) {
    var s = str(v);
    if (!s) return '';
    try {
      var u = new URL(s, window.location.origin);
      if (u.protocol !== 'http:' && u.protocol !== 'https:') return '';
      return u.href;
    } catch (e) { return ''; }
  }
  function looksLikeUrl(v) {
    var s = str(v);
    if (!s || s.length > 2048 || /\s/.test(s)) return false;
    return /^https?:\/\/[^\s/]+\.[^\s/]+/i.test(s) ||
      /^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+\/[^\s]*$/i.test(s);
  }
  // 宽松识别平台（仅用于输入框提示；权威判定在服务端）
  function guessPlatform(v) {
    var s = str(v).toLowerCase();
    if (!s) return '';
    if (/(^|\/\/|\.)(x\.com|twitter\.com|t\.co)\b/.test(s) || /\/\/x\.com/.test(s)) return 'x';
    if (/douyin\.com|iesdouyin\.com|v\.douyin\.com/.test(s)) return 'douyin';
    if (/bilibili\.com|b23\.tv|bili2233\.cn|bili22\.cn|bili23\.cn/.test(s)) return 'bilibili';
    if (/xiaohongshu\.com|xhslink\.com|xhs\.link/.test(s)) return 'xiaohongshu';
    return '';
  }
  function platformMeta(id) {
    for (var i = 0; i < PLATFORMS.length; i++) {
      if (PLATFORMS[i].id === id) return PLATFORMS[i];
    }
    return null;
  }
  function platformName(id) {
    if (!id || id === 'all') return '全部';
    var cap = state.caps && state.caps.byId[id];
    if (cap && cap.name) return cap.name;
    var m = platformMeta(id);
    return m ? m.name : id;
  }
  function platformMark(id) {
    var m = platformMeta(id);
    return m ? m.mark : (id ? String(id).slice(0, 4) : '内容');
  }
  function coverageKey(v) {
    var s = str(v).toLowerCase();
    return COVERAGE[s] ? s : 'unavailable';
  }
  function coverageName(v) {
    return COVERAGE[coverageKey(v)].name;
  }
  function formatTime(v) {
    if (v === null || v === undefined || v === '') return '';
    if (typeof v === 'number' && isFinite(v)) {
      // 秒或毫秒时间戳
      var ms = v > 1e12 ? v : (v > 1e10 ? v : v * 1000);
      var d0 = new Date(ms);
      return isNaN(d0.getTime()) ? '' : fmtDate(d0);
    }
    var s = str(v);
    if (!s) return '';
    var d = new Date(s);
    return isNaN(d.getTime()) ? s.slice(0, 40) : fmtDate(d);
  }
  function fmtDate(d) {
    function p(n) { return n < 10 ? '0' + n : '' + n; }
    var now = new Date();
    var sameYear = d.getFullYear() === now.getFullYear();
    var base = sameYear
      ? (d.getMonth() + 1) + '月' + d.getDate() + '日'
      : d.getFullYear() + '年' + (d.getMonth() + 1) + '月' + d.getDate() + '日';
    return base + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function shorten(v, max) {
    var s = str(v);
    if (!s) return '';
    return s.length > max ? s.slice(0, max) + '…' : s;
  }

  /* ====================================================================== *
   * 归一化：capabilities / item / trace / diagnostics
   * ====================================================================== */

  function normalizeCapability(raw, index) {
    var o = obj(raw);
    var id = str(o.id) || (PLATFORMS[index] && PLATFORMS[index].id) || '';
    var fresh = { valid: true, fallback: false };
    var base = platformMeta(id);
    return {
      id: id,
      name: str(o.name) || (base ? base.name : id || '平台'),
      read: bool(o.read) === true,
      search: bool(o.search) === true,
      subscribe: bool(o.subscribe) === true,
      reason: str(o.reason),
      mode: str(o.mode) || (bool(o.read) === true ? 'direct' : 'paste'),
      _fallback: fresh.fallback,
      _valid: fresh.valid
    };
  }

  function normalizeCaps(raw) {
    var o = obj(raw);
    var list = arr(o.platforms).map(normalizeCapability).filter(function (c) { return !!c.id; });
    var byId = {};
    for (var i = 0; i < list.length; i++) byId[list[i].id] = list[i];
    var model = obj(o.model);
    var limits = obj(o.limits);
    var limitParts = [];
    var reads = num(limits.reads) !== null ? num(limits.reads) : num(limits.readCount);
    var maxReads = num(limits.maxReads) !== null ? num(limits.maxReads) : num(limits.dailyReads);
    if (reads !== null) {
      limitParts.push(maxReads !== null ? ('本次会话已读取 ' + reads + ' / ' + maxReads + ' 次') : ('本次会话已读取 ' + reads + ' 次'));
    }
    var windowSec = num(limits.windowSeconds);
    if (windowSec !== null) limitParts.push('限流窗口 ' + Math.round(windowSec) + ' 秒');
    var perMin = num(limits.perMinute);
    if (perMin !== null) limitParts.push('每分钟最多 ' + perMin + ' 次');
    return {
      platforms: list,
      byId: byId,
      redirectMaxHops: num(o.redirectMaxHops),
      limits: limits,
      limitsText: limitParts.join(' · '),
      model: {
        configured: model.configured === true,
        model: str(model.model),
        reason: str(model.reason)
      },
      ok: true
    };
  }

  function normalizeTrace(raw) {
    return arr(raw).map(function (h, i) {
      var o = obj(h);
      var hop = num(o.hop);
      return {
        hop: hop !== null ? hop : (i + 1),
        url: str(o.url),
        allowed: bool(o.allowed),
        reason: str(o.reason)
      };
    });
  }

  function normalizeItem(raw) {
    var o = obj(raw);
    var coverage = coverageKey(o.coverage);
    var original = pickStr(o, ['original', 'originalText', 'content', 'body', 'text']);
    return {
      id: str(o.id),
      platform: str(o.platform),
      title: str(o.title),
      author: str(o.author),
      publishedAt: (o.publishedAt === undefined ? '' : o.publishedAt),
      publishedText: str(o.publishedText),
      url: safeHref(o.url),
      coverage: coverage,
      mode: str(o.mode),
      excerpt: pickStr(o, ['excerpt', 'summary', 'snippet']),
      summary: pickStr(o, ['summary', 'abstract']),
      keypoints: arr(o.keypoints).map(function (k) { return str(k); }).filter(Boolean),
      quotes: arr(o.quotes).map(function (q) { return str(q); }).filter(Boolean),
      original: original,
      hasOriginal: o.hasOriginal === true || !!original,
      reason: pickStr(o, ['reason']),
      trace: normalizeTrace(o.trace),
      degraded: bool(o.degraded),
      limited: bool(o.limited) === true
    };
  }

  function normalizeDiagnostic(raw) {
    var o = obj(raw);
    var lv = str(o.level).toLowerCase();
    if (['info', 'warn', 'warning', 'error'].indexOf(lv) < 0) lv = 'info';
    if (lv === 'warning') lv = 'warn';
    var msg = str(o.message);
    if (!msg) return null;
    return { level: lv, message: msg };
  }

  /* ====================================================================== *
   * 网络：同源凭证 + no-store；错误只暴露服务端中文 error
   * ====================================================================== */

  function ApiError(message, status, limited) {
    var e = new Error(message);
    e.name = 'ApiError';
    e.status = status || 0;
    e.limited = !!limited;
    return e;
  }

  function readErrorPayload(data) {
    var o = obj(data);
    var msg = str(o.error) || str(o.reason) || str(o.message);
    return msg;
  }

  function request(path, method, body) {
    var opts = {
      method: method,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'Accept': 'application/json' }
    };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (res) {
      return res.json().then(function (data) {
        return { res: res, data: data };
      }, function () {
        return { res: res, data: null };
      });
    }).then(function (r) {
      var data = r.data;
      if (!r.res.ok) {
        var reason = readErrorPayload(data) || defaultHttpMessage(r.res.status);
        throw ApiError(reason, r.res.status, r.res.status === 429);
      }
      if (data === null || typeof data !== 'object') {
        throw ApiError('服务返回的内容无法解析，请稍后重试。', r.res.status);
      }
      if (method === 'POST' && obj(data).ok === false) {
        throw ApiError(readErrorPayload(data) || '服务未完成本次操作，请稍后重试。', r.res.status);
      }
      return data;
    }, function () {
      throw ApiError('无法连接内容服务，请确认控制台服务仍在运行。', 0);
    });
  }

  function defaultHttpMessage(status) {
    if (status === 401) return '登录状态已失效，请重新登录。';
    if (status === 403) return '当前账号无权执行该操作。';
    if (status === 404) return '内容接口暂不可用（服务端未实现）。';
    if (status === 429) return '读取过于频繁，请稍后再试。';
    if (status >= 500) return '服务暂时无法完成请求，请稍后再试。';
    return '请求未完成，请稍后重试。';
  }

  // 服务端 reason 常常已包含状态名（「需要登录…」），避免拼成「需要登录：需要登录…」
  function reasonWith(reason, key) {
    var msg = str(reason) || DEFAULT_REASON[coverageKey(key)];
    var name = coverageName(key);
    return msg.indexOf(name) === 0 ? msg : (name + '：' + msg);
  }

  var apiGet = function (p) { return request(p, 'GET', undefined); };
  var apiPost = function (p, b) { return request(p, 'POST', b); };

  /* ====================================================================== *
   * DOM 工具（只有 textContent，绝不 innerHTML）
   * ====================================================================== */

  function node(tag, cls, txt) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (txt !== undefined && txt !== null && txt !== '') n.textContent = String(txt);
    return n;
  }
  function clear(n) {
    while (n && n.firstChild) n.removeChild(n.firstChild);
  }
  function append(parent) {
    for (var i = 1; i < arguments.length; i++) {
      var c = arguments[i];
      if (c) parent.appendChild(c);
    }
    return parent;
  }
  function setText(n, txt) {
    if (n) n.textContent = (txt === undefined || txt === null) ? '' : String(txt);
  }
  function show(n, on) {
    if (n) n.hidden = !on;
  }

  function markEl(platformId) {
    var m = node('span', 'dc-mark', platformMark(platformId));
    if (platformId) m.setAttribute('data-platform', platformId);
    else m.setAttribute('data-platform', 'unknown');
    return m;
  }
  function covEl(coverage) {
    var key = coverageKey(coverage);
    var n = node('span', 'dc-cov', coverageName(key));
    n.setAttribute('data-cov', key);
    n.setAttribute('title', COVERAGE[key].scope);
    return n;
  }

  /* ====================================================================== *
   * 状态
   * ====================================================================== */

  var state = {
    mounted: false,
    root: null,
    refs: {},
    caps: null,          // normalizeCaps 结果
    capsError: '',       // 能力接口失败原因（单独提示，不阻塞其他功能）
    platform: 'all',     // 当前筛选
    items: [],
    diagnostics: [],
    feedLoading: false,
    feedError: '',
    selectedId: '',
    detail: null,        // 当前详情（含原文 / 摘要 / trace）
    detailLoading: false,
    tab: 'summary',
    ask: { question: '', answer: '', cites: [], basis: '', loading: false, error: '' },
    search: { items: [], loading: false, limited: false, reason: '', keyword: '', ran: false },
    subscribe: { enabled: false, loading: false, message: '', kind: '' },
    busy: {},            // 各类在途标记
    lastCapabilitiesAt: 0
  };

  /* ====================================================================== *
   * DOM 构建（全部在 rootEl 内部自建，不依赖 index.html 预置子元素）
   * ====================================================================== */

  function build(root) {
    clear(root);
    root.classList.add('dc');
    if (!root.hasAttribute('data-content-module')) root.setAttribute('data-content-module', 'content');

    var refs = state.refs;
    refs.root = root;

    /* ---- 1. 顶部状态条 ---- */
    var status = node('div', 'dc-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    refs.status = status;
    refs.statusDot = node('span', 'dc-dot');
    refs.statusDot.setAttribute('aria-hidden', 'true');
    refs.modelText = node('span', 'dc-model', '正在读取模型状态…');
    refs.usageText = node('span', 'dc-usage', '');
    refs.modeText = node('span', 'dc-mark dc-mode', '');
    append(status, refs.statusDot, refs.modelText, refs.usageText, refs.modeText);

    /* ---- 2. 粘贴区 ---- */
    var readPanel = node('section', 'dc-panel dc-read-panel');
    readPanel.setAttribute('aria-labelledby', 'dcReadTitle');
    var readHead = node('div', 'dc-subhead');
    var readTitleWrap = node('div');
    var eyebrow = node('p', 'dc-eyebrow', '读取');
    var readTitle = node('h3', 'dc-subtitle', '粘贴链接或正文');
    readTitle.id = 'dcReadTitle';
    append(readTitleWrap, eyebrow, readTitle,
      node('p', 'dc-note', '链接交给服务端按白名单逐跳校验；正文直接保存为可读内容。本模块只做读取，不提供任何写操作。'));
    append(readHead, readTitleWrap);

    var bar = node('div', 'dc-readbar');
    var input = node('input', 'dc-input');
    input.type = 'text';
    input.id = 'dcPasteInput';
    input.setAttribute('maxlength', String(MAX_INPUT));
    input.setAttribute('placeholder', '粘贴 X / 抖音 / B站 / 小红书 的链接，或直接粘贴正文…');
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('spellcheck', 'false');
    refs.input = input;

    var textarea = node('textarea', 'dc-textarea');
    textarea.id = 'dcPasteText';
    textarea.setAttribute('rows', '4');
    textarea.setAttribute('maxlength', String(MAX_INPUT));
    textarea.setAttribute('placeholder', '把正文粘贴到这里（平台未授权时的降级方式）…');
    textarea.hidden = true;
    refs.textarea = textarea;

    var readBtn = node('button', 'dc-btn', '读取');
    readBtn.type = 'button';
    refs.readBtn = readBtn;

    var toggleBtn = node('button', 'dc-btn dc-btn-quiet', '贴正文');
    toggleBtn.type = 'button';
    toggleBtn.setAttribute('aria-pressed', 'false');
    refs.toggleBtn = toggleBtn;

    append(bar, input, textarea, readBtn, toggleBtn);

    var detect = node('div', 'dc-detect');
    refs.detectKind = node('span', 'dc-detect-kind', '等待输入');
    refs.detectHint = node('span', 'dc-hint', '输入后自动识别是链接还是正文。');
    append(detect, refs.detectKind, refs.detectHint);

    var forceWrap = node('label', 'dc-check');
    var forceBox = node('input');
    forceBox.type = 'checkbox';
    forceBox.id = 'dcForce';
    refs.forceBox = forceBox;
    var forceLabel = node('span', '', '重新读取（忽略已有缓存）');
    append(forceWrap, forceBox, forceLabel);
    forceWrap.hidden = true;
    refs.forceWrap = forceWrap;

    var readMsg = node('p', 'dc-msg');
    readMsg.hidden = true;
    readMsg.setAttribute('role', 'alert');
    refs.readMsg = readMsg;

    append(readPanel, readHead, bar, detect, forceWrap, readMsg);

    /* ---- 3. 平台筛选 + 4. Feed ---- */
    var layout = node('div', 'dc-layout');

    var feedPanel = node('section', 'dc-panel dc-feed-panel');
    feedPanel.setAttribute('aria-labelledby', 'dcFeedTitle');
    var feedHead = node('div', 'dc-subhead');
    var feedTitle = node('h3', 'dc-subtitle', '已读内容');
    feedTitle.id = 'dcFeedTitle';
    refs.feedCount = node('span', 'count', '');
    append(feedHead, feedTitle, refs.feedCount);

    var chips = node('div', 'dc-chips');
    chips.setAttribute('role', 'group');
    chips.setAttribute('aria-label', '按平台筛选');
    refs.chips = chips;

    refs.degrade = node('div', 'dc-degrade');
    refs.degrade.titleNode = node('strong', 'dc-degrade-title', '该平台暂不可读');
    refs.degrade.body = node('p', '', '');
    append(refs.degrade, refs.degrade.titleNode, refs.degrade.body);
    refs.degrade.hidden = true;

    var feedMsg = node('p', 'dc-msg');
    feedMsg.hidden = true;
    refs.feedMsg = feedMsg;

    refs.feedList = node('ul', 'dc-feed');
    refs.feedEmpty = node('p', 'dc-empty', '还没有读取过内容。粘贴一个链接，或切换平台看看。');

    var diagWrap = node('div', 'dc-diag-wrap');
    refs.diagTitle = node('p', 'dc-eyebrow', '诊断');
    refs.diagList = node('ul', 'dc-diag');
    refs.diagEmpty = node('p', 'dc-empty', '暂无诊断信息。');
    append(diagWrap, refs.diagTitle, refs.diagList, refs.diagEmpty);
    diagWrap.hidden = true;
    refs.diagWrap = diagWrap;

    append(feedPanel, feedHead, chips, refs.degrade, feedMsg, refs.feedList, refs.feedEmpty, diagWrap);

    /* ---- 5. 详情：摘要 / 原文 / 问答 ---- */
    var detailPanel = node('section', 'dc-panel dc-detail-panel');
    detailPanel.setAttribute('aria-labelledby', 'dcDetailTitle');
    refs.detailPanel = detailPanel;

    refs.detailEmpty = node('div', 'dc-detail-empty');
    append(refs.detailEmpty,
      node('p', 'dc-eyebrow', '详情'),
      node('p', 'dc-subtitle', '选一条内容'),
      node('p', 'dc-note', '左侧列表里点任意一条，这里会显示摘要、原文与实际可读范围（逐跳白名单校验过程也会一并列出）。'));

    refs.detail = node('div', 'dc-detail');
    refs.detail.hidden = true;

    var dHead = node('div', 'dc-detail-head');
    var dTop = node('div', 'dc-item-top');
    refs.dMark = node('span');
    refs.dCov = node('span');
    refs.dLimited = node('span', 'dc-cov', '已限流');
    refs.dLimited.setAttribute('data-cov', 'limited');
    refs.dLimited.hidden = true;
    refs.dTime = node('span', 'dc-item-time', '');
    append(dTop, refs.dMark, refs.dCov, refs.dLimited, refs.dTime);

    refs.dTitle = node('h3', 'dc-detail-title', '未提供标题');
    refs.dTitle.id = 'dcDetailTitle';
    refs.dMeta = node('div', 'dc-detail-meta');
    refs.dAuthor = node('span', '', '');
    refs.dPub = node('span', '', '');
    refs.dMode = node('span', '', '');
    append(refs.dMeta, refs.dAuthor, refs.dPub, refs.dMode);

    refs.dScope = node('div', 'dc-scope');
    refs.dScopeLabel = node('span', 'dc-scope-label', '');
    refs.dScopeReason = node('span', 'dc-scope-reason', '');
    append(refs.dScope, refs.dScopeLabel, refs.dScopeReason);

    var dActions = node('div', 'dc-actions');
    refs.dLink = node('a', 'dc-link', '打开原链接');
    refs.dLink.target = '_blank';
    refs.dLink.rel = 'noopener noreferrer';
    refs.dLink.hidden = true;
    refs.dSave = node('button', 'dc-btn dc-btn-quiet', '保存链接');
    refs.dSave.type = 'button';
    refs.dSave.hidden = true;
    refs.dReload = node('button', 'dc-btn dc-btn-quiet', '重新读取');
    refs.dReload.type = 'button';
    refs.dReload.hidden = true;
    append(dActions, refs.dLink, refs.dSave, refs.dReload);

    var dMsg = node('p', 'dc-msg');
    dMsg.hidden = true;
    refs.detailMsg = dMsg;

    append(dHead, dTop, refs.dTitle, refs.dMeta, refs.dScope, dActions, dMsg);

    // tab 栏
    var tabs = node('div', 'dc-tabs');
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('aria-label', '内容视图');
    refs.tabs = {};
    var TAB_DEFS = [
      { id: 'summary', label: '摘要' },
      { id: 'original', label: '原文' },
      { id: 'ask', label: '问答' }
    ];
    var tabButtons = [];
    TAB_DEFS.forEach(function (def) {
      var b = node('button', 'dc-tab', def.label);
      b.type = 'button';
      b.setAttribute('role', 'tab');
      b.setAttribute('data-tab', def.id);
      b.setAttribute('id', 'dcTab-' + def.id);
      b.setAttribute('aria-controls', 'dcPane-' + def.id);
      b.setAttribute('aria-selected', 'false');
      refs.tabs[def.id] = b;
      tabButtons.push(b);
    });
    append.apply(null, [tabs].concat(tabButtons));

    /* 摘要 pane */
    var paneSummary = node('div', 'dc-pane');
    paneSummary.setAttribute('role', 'tabpanel');
    paneSummary.id = 'dcPane-summary';
    paneSummary.setAttribute('aria-labelledby', 'dcTab-summary');
    refs.summaryText = node('p', 'dc-summary-text', '');
    refs.summaryEmpty = node('p', 'dc-empty', '这条内容还没有摘要。');
    refs.keypointsWrap = node('div');
    refs.keypointsTitle = node('p', 'dc-eyebrow', '要点');
    refs.keypoints = node('ul', 'dc-keypoints');
    append(refs.keypointsWrap, refs.keypointsTitle, refs.keypoints);
    refs.quotesWrap = node('div');
    refs.quotesTitle = node('p', 'dc-eyebrow', '关键引文');
    refs.quotes = node('ul', 'dc-quotes');
    append(refs.quotesWrap, refs.quotesTitle, refs.quotes);
    refs.summaryDisclaimer = node('p', 'dc-disclaimer', '以上摘要与要点由模型生成，不是原文；请以「原文」标签页为准。');
    append(paneSummary, refs.summaryText, refs.summaryEmpty, refs.keypointsWrap, refs.quotesWrap, refs.summaryDisclaimer);

    /* 原文 pane */
    var paneOriginal = node('div', 'dc-pane');
    paneOriginal.setAttribute('role', 'tabpanel');
    paneOriginal.id = 'dcPane-original';
    paneOriginal.setAttribute('aria-labelledby', 'dcTab-original');
    refs.originalGate = node('div', 'dc-gate');
    refs.originalGate.hidden = true;
    refs.originalBody = node('p', 'dc-original', '');
    refs.originalEmpty = node('p', 'dc-empty', '拿不到原文。');
    var traceWrap = node('div', 'dc-trace-wrap');
    refs.traceTitle = node('p', 'dc-eyebrow', '逐跳白名单校验');
    refs.traceNote = node('p', 'dc-note', '服务端对每一跳都重新校验域名，短链只是其中一跳。');
    refs.trace = node('ul', 'dc-trace');
    refs.traceEmpty = node('p', 'dc-empty', '这次读取没有产生跳转校验记录（可能是直接粘贴的正文）。');
    append(traceWrap, refs.traceTitle, refs.traceNote, refs.trace, refs.traceEmpty);
    append(paneOriginal, refs.originalGate, refs.originalBody, refs.originalEmpty, traceWrap);

    /* 问答 pane */
    var paneAsk = node('div', 'dc-pane');
    paneAsk.setAttribute('role', 'tabpanel');
    paneAsk.id = 'dcPane-ask';
    paneAsk.setAttribute('aria-labelledby', 'dcTab-ask');
    refs.askBlocked = node('p', 'dc-ask-blocked', '');
    refs.askBlocked.hidden = true;
    var askRow = node('div', 'dc-ask');
    refs.askInput = node('input', 'dc-input');
    refs.askInput.type = 'text';
    refs.askInput.id = 'dcAskInput';
    refs.askInput.setAttribute('maxlength', '500');
    refs.askInput.setAttribute('autocomplete', 'off');
    refs.askInput.setAttribute('placeholder', '就这条内容提问，例如：作者的核心论据是什么？');
    refs.askBtn = node('button', 'dc-btn', '提问');
    refs.askBtn.type = 'button';
    append(askRow, refs.askInput, refs.askBtn);
    refs.askMsg = node('p', 'dc-msg');
    refs.askMsg.hidden = true;
    refs.answer = node('div', 'dc-answer');
    refs.answer.hidden = true;
    refs.answerHead = node('div', 'dc-answer-head');
    refs.answerBasis = node('span', 'dc-mark', '');
    refs.answerBody = node('p', 'dc-summary-text', '');
    append(refs.answer, refs.answerHead, refs.answerBody);
    refs.citesTitle = node('p', 'dc-eyebrow', '引用');
    refs.cites = node('ul', 'dc-cites');
    append(paneAsk, refs.askBlocked, askRow, refs.askMsg, refs.answer, refs.citesTitle, refs.cites);

    refs.panes = { summary: paneSummary, original: paneOriginal, ask: paneAsk };
    append(refs.detail, dHead, tabs, paneSummary, paneOriginal, paneAsk);

    append(detailPanel, refs.detailEmpty, refs.detail);

    append(layout, feedPanel, detailPanel);

    /* ---- 6. 搜索 / 订阅 ---- */
    var toolsPanel = node('section', 'dc-panel dc-tools-panel');
    toolsPanel.setAttribute('aria-labelledby', 'dcToolsTitle');
    var toolsHead = node('div', 'dc-subhead');
    var toolsTitle = node('h3', 'dc-subtitle', '搜索与订阅');
    toolsTitle.id = 'dcToolsTitle';
    append(toolsHead, toolsTitle,
      node('span', 'count', '仅在已授权范围内生效'));

    var tools = node('div', 'dc-tools');
    var row1 = node('div', 'dc-tools-row');
    refs.toolsPlatform = node('select', 'dc-select');
    refs.toolsPlatform.id = 'dcToolsPlatform';
    refs.toolsPlatform.setAttribute('aria-label', '搜索平台');
    refs.toolsKeyword = node('input', 'dc-input');
    refs.toolsKeyword.type = 'text';
    refs.toolsKeyword.id = 'dcToolsKeyword';
    refs.toolsKeyword.setAttribute('maxlength', '120');
    refs.toolsKeyword.setAttribute('autocomplete', 'off');
    refs.toolsKeyword.setAttribute('placeholder', '关键词');
    refs.searchBtn = node('button', 'dc-btn', '搜索');
    refs.searchBtn.type = 'button';
    append(row1, refs.toolsPlatform, refs.toolsKeyword, refs.searchBtn);

    var row2 = node('div', 'dc-tools-row');
    var sw = node('label', 'dc-switch');
    refs.subBox = node('input');
    refs.subBox.type = 'checkbox';
    refs.subBox.id = 'dcSubscribe';
    refs.subTrack = node('span', 'dc-track');
    refs.subTrack.setAttribute('aria-hidden', 'true');
    refs.subLabel = node('span', 'dc-switch-label', '订阅该关键词');
    append(sw, refs.subBox, refs.subTrack, refs.subLabel);
    refs.toolsNote = node('span', 'dc-note', '订阅只是让服务端在授权范围内留意新内容，不代表能绕过平台限制。');
    append(row2, sw, refs.toolsNote);

    refs.toolsMsg = node('p', 'dc-msg');
    refs.toolsMsg.hidden = true;
    refs.limitNote = node('p', 'dc-limit-note');
    refs.limitNote.hidden = true;
    refs.searchResultsTitle = node('p', 'dc-eyebrow', '搜索结果');
    refs.searchResults = node('ul', 'dc-feed');
    refs.searchResultsTitle.hidden = true;

    append(tools, row1, row2, refs.toolsMsg, refs.limitNote, refs.searchResultsTitle, refs.searchResults);
    append(toolsPanel, toolsHead, tools, node('p', 'dc-note',
      '平台未配置读取权限时，搜索与订阅会降级为不可用；此时仍可用「贴原文 / 保存链接」。'));

    append(root, status, readPanel, layout, toolsPanel);

    wire();
  }

  /* ====================================================================== *
   * 事件绑定（addEventListener，无内联事件；监听挂在 root 上做委托）
   * ====================================================================== */

  function wire() {
    var refs = state.refs;

    refs.input.addEventListener('input', onInputChange);
    refs.textarea.addEventListener('input', onInputChange);
    refs.input.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        doRead();
      }
    });
    refs.readBtn.addEventListener('click', doRead);
    refs.toggleBtn.addEventListener('click', togglePasteMode);

    refs.chips.addEventListener('click', function (ev) {
      var btn = closest(ev.target, '[data-platform-chip]');
      if (!btn) return;
      var id = btn.getAttribute('data-platform-chip') || 'all';
      selectPlatform(id);
    });
    refs.feedList.addEventListener('click', function (ev) {
      var btn = closest(ev.target, '[data-item-id]');
      if (!btn) return;
      openDetail(btn.getAttribute('data-item-id') || '');
    });
    refs.searchResults.addEventListener('click', function (ev) {
      var btn = closest(ev.target, '[data-item-id]');
      if (!btn) return;
      openDetail(btn.getAttribute('data-item-id') || '');
    });

    refs.tabs.summary.addEventListener('click', function () { switchTab('summary'); });
    refs.tabs.original.addEventListener('click', function () { switchTab('original'); });
    refs.tabs.ask.addEventListener('click', function () { switchTab('ask'); });
    // 方向键在 tab 间移动（无障碍惯例）
    var TAB_ORDER = ['summary', 'original', 'ask'];
    refs.root.addEventListener('keydown', function (ev) {
      var t = ev.target;
      if (!t || !t.getAttribute || t.getAttribute('role') !== 'tab') return;
      if (ev.key !== 'ArrowRight' && ev.key !== 'ArrowLeft') return;
      var cur = t.getAttribute('data-tab') || 'summary';
      var idx = TAB_ORDER.indexOf(cur);
      if (idx < 0) return;
      var next = TAB_ORDER[(idx + (ev.key === 'ArrowRight' ? 1 : TAB_ORDER.length - 1)) % TAB_ORDER.length];
      if (refs.tabs[next] && !refs.tabs[next].hidden) {
        ev.preventDefault();
        switchTab(next);
        refs.tabs[next].focus();
      }
    });

    refs.dReload.addEventListener('click', function () { reloadCurrent(); });
    refs.dSave.addEventListener('click', function () { saveLink(); });

    refs.askBtn.addEventListener('click', doAsk);
    refs.askInput.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        doAsk();
      }
    });

    refs.searchBtn.addEventListener('click', doSearch);
    refs.toolsKeyword.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !ev.isComposing) {
        ev.preventDefault();
        doSearch();
      }
    });
    refs.toolsPlatform.addEventListener('change', syncSubscribeState);
    refs.subBox.addEventListener('change', doSubscribe);
  }

  function closest(el, sel) {
    var n = el;
    while (n && n !== document) {
      if (n.nodeType === 1 && n.matches && n.matches(sel)) return n;
      n = n.parentNode;
    }
    return null;
  }

  /* ====================================================================== *
   * 提示 / 忙碌
   * ====================================================================== */

  function setMsg(el, text, kind) {
    if (!el) return;
    if (!text) {
      el.hidden = true;
      el.textContent = '';
      el.removeAttribute('data-kind');
      return;
    }
    el.textContent = text;
    el.hidden = false;
    if (kind) el.setAttribute('data-kind', kind);
    else el.removeAttribute('data-kind');
  }

  function setBusy(kind, on) {
    state.busy[kind] = !!on;
  }

  function syncControls() {
    var refs = state.refs;
    var busyRead = !!state.busy.read;
    var busySearch = !!state.busy.search;
    if (refs.readBtn) {
      refs.readBtn.disabled = busyRead;
      setText(refs.readBtn, busyRead ? '读取中…' : '读取');
    }
    if (refs.input) refs.input.disabled = busyRead;
    if (refs.textarea) refs.textarea.disabled = busyRead;
    if (refs.toggleBtn) refs.toggleBtn.disabled = busyRead;
    if (refs.searchBtn) {
      refs.searchBtn.disabled = busySearch || !canSearch();
      setText(refs.searchBtn, busySearch ? '搜索中…' : '搜索');
    }
    if (refs.dReload) refs.dReload.disabled = !!state.busy.read;
    if (refs.dSave) refs.dSave.disabled = !!state.busy.read;
    syncSubscribeState();
    syncAskState();
  }

  /* ====================================================================== *
   * capabilities
   * ====================================================================== */

  function loadCapabilities() {
    setText(state.refs.modelText, '正在读取模型状态…');
    setMsg(state.refs.feedMsg, '', '');
    return apiGet(API.capabilities).then(function (data) {
      state.caps = normalizeCaps(data);
      state.capsError = '';
    }, function (e) {
      state.caps = null;
      state.capsError = e.message;
    }).then(function () {
      state.lastCapabilitiesAt = Date.now();
      renderStatus();
      renderChips();
      renderToolsPlatforms();
      renderDegrade();
      renderFeed();
      syncControls();
    });
  }

  function capOf(id) {
    return (state.caps && state.caps.byId[id]) ? state.caps.byId[id] : null;
  }
  function capReadable(id) {
    var c = capOf(id);
    return !!(c && c.read);
  }
  function capSearchable(id) {
    var c = capOf(id);
    return !!(c && c.search);
  }

  function renderStatus() {
    var refs = state.refs;
    var model = state.caps ? state.caps.model : { configured: false, model: '', reason: '' };
    var box = refs.status;
    if (!state.caps) {
      box.setAttribute('data-model', 'off');
      setText(refs.modelText, '模型状态未知');
      setText(refs.usageText, state.capsError ? ('能力接口未就绪：' + state.capsError) : '正在读取能力…');
      setText(refs.modeText, '');
      refs.modeText.hidden = true;
      return;
    }
    if (model.configured) {
      box.setAttribute('data-model', 'ok');
      setText(refs.modelText, model.model ? ('模型：' + model.model) : '模型：已配置');
    } else {
      box.setAttribute('data-model', 'off');
      setText(refs.modelText, '模型未配置，问答不可用');
    }
    var usage = state.caps.limitsText || '';
    setText(refs.usageText, usage ? ('本次用量 · ' + usage) : '');
    refs.usageText.hidden = !usage;
    var hops = state.caps.redirectMaxHops;
    if (hops !== null) {
      setText(refs.modeText, '跳转上限 ' + hops + ' 跳');
      refs.modeText.hidden = false;
    } else {
      refs.modeText.hidden = true;
    }
  }

  /* ====================================================================== *
   * 平台筛选 chips
   * ====================================================================== */

  function renderChips() {
    var chips = state.refs.chips;
    clear(chips);
    var defs = [{ id: 'all', name: '全部' }];
    if (state.caps && state.caps.platforms.length) {
      state.caps.platforms.forEach(function (c) {
        defs.push({ id: c.id, name: c.name, cap: c });
      });
    } else {
      PLATFORMS.forEach(function (p) {
        defs.push({ id: p.id, name: p.name, cap: null });
      });
    }
    defs.forEach(function (d) {
      var btn = node('button', 'dc-chip');
      btn.type = 'button';
      btn.setAttribute('data-platform-chip', d.id);
      btn.setAttribute('aria-pressed', state.platform === d.id ? 'true' : 'false');
      append(btn, node('span', '', d.name));
      if (d.id !== 'all') {
        var cap = d.cap || capOf(d.id);
        if (cap && !cap.read) {
          append(btn, node('span', 'dc-chip-flag', '未配置'));
          btn.setAttribute('title', (d.name + ' 未配置读取权限，可贴原文或保存链接'));
        } else if (!cap && state.caps) {
          append(btn, node('span', 'dc-chip-flag', '未配置'));
        }
      }
      var count = (d.id === 'all') ? state.items.length
        : state.items.filter(function (it) { return it.platform === d.id; }).length;
      if (state.items.length) append(btn, node('span', 'dc-chip-count', String(count)));
      chips.appendChild(btn);
    });
  }

  function selectPlatform(id) {
    if (state.platform === id) return;
    state.platform = id;
    renderChips();
    renderDegrade();
    loadFeed();
  }

  function renderDegrade() {
    var box = state.refs.degrade;
    var id = state.platform;
    if (!id || id === 'all') { box.hidden = true; return; }
    var cap = capOf(id);
    if (cap && cap.read) { box.hidden = true; return; }
    box.hidden = false;
    setText(box.titleNode, platformName(id) + ' 未授权 / 未配置');
    var reason = cap && cap.reason ? cap.reason : (state.capsError || '服务端没有声明该平台的可读能力。');
    setText(box.body, reason + ' ' + DEGRADE_TEXT);
  }

  /* ====================================================================== *
   * 输入识别 / 粘贴模式
   * ====================================================================== */

  function currentText() {
    return state.refs.textarea && !state.refs.textarea.hidden
      ? state.refs.textarea.value
      : state.refs.input.value;
  }

  function onInputChange() {
    var v = str(currentText());
    var refs = state.refs;
    if (!v) {
      setText(refs.detectKind, '等待输入');
      setText(refs.detectHint, '输入后自动识别是链接还是正文。');
      refs.forceWrap.hidden = true;
      return;
    }
    if (looksLikeUrl(v) && !/\n/.test(v)) {
      var p = guessPlatform(v);
      setText(refs.detectKind, '识别为链接');
      setText(refs.detectHint, p ? ('疑似来自 ' + platformName(p) + '，读取时会逐跳校验域名。') : '暂未识别出平台，服务端会再校验一次域名白名单。');
      refs.forceWrap.hidden = !p;
      if (!p) refs.forceBox.checked = false;
    } else {
      setText(refs.detectKind, '识别为正文');
      setText(refs.detectHint, '按正文保存（' + v.length + ' 字），不发起任何外网读取。');
      refs.forceWrap.hidden = true;
      refs.forceBox.checked = false;
    }
  }

  function togglePasteMode() {
    var refs = state.refs;
    var toText = refs.textarea.hidden;
    refs.textarea.hidden = !toText;
    refs.input.hidden = toText;
    refs.toggleBtn.setAttribute('aria-pressed', toText ? 'true' : 'false');
    setText(refs.toggleBtn, toText ? '贴链接' : '贴正文');
    refs.input.setAttribute('placeholder', '粘贴 X / 抖音 / B站 / 小红书 的链接…');
    onInputChange();
    (toText ? refs.textarea : refs.input).focus();
  }

  /* ====================================================================== *
   * 读取
   * ====================================================================== */

  function doRead() {
    if (state.busy.read) return;
    var refs = state.refs;
    var raw = str(currentText());
    setMsg(refs.readMsg, '', '');
    if (!raw) {
      setMsg(refs.readMsg, '请先粘贴一个链接或一段正文。', 'warn');
      (refs.textarea.hidden ? refs.input : refs.textarea).focus();
      return;
    }
    if (raw.length > MAX_INPUT) {
      setMsg(refs.readMsg, '内容过长（超过 ' + MAX_INPUT + ' 字），请分段读取。', 'warn');
      return;
    }
    var body = {};
    var p = '';
    // 读取时顺带请服务端生成摘要（模型未配置时服务端会忽略，不会失败）
    body.summarize = true;
    if (looksLikeUrl(raw) && !/\n/.test(raw)) {
      body.url = raw;
      p = guessPlatform(raw);
      if (p) body.platform = p;
      if (refs.forceBox.checked) body.force = true;
    } else {
      body.text = raw;
      if (state.platform !== 'all') body.platform = state.platform;
    }

    setBusy('read', true);
    syncControls();
    setMsg(refs.readMsg, looksLikeUrl(raw) ? '正在按白名单逐跳读取…' : '正在保存正文…', 'busy');

    apiPost(API.read, body).then(function (data) {
      var o = obj(data);
      var item = normalizeItem(o.item);
      var coverage = coverageKey(o.coverage || item.coverage);
      item.coverage = coverage;
      if (!item.reason) item.reason = str(o.reason);
      if (!item.trace.length) item.trace = normalizeTrace(o.trace);
      var degraded = bool(o.degraded);
      if (degraded !== null) item.degraded = degraded;

      applyReadResult(item);

      var name = coverageName(coverage);
      if (READABLE[coverage]) {
        setMsg(refs.readMsg, '已读取：' + name + (item.reason ? '（' + item.reason + '）' : ''), 'ok');
      } else {
        setMsg(refs.readMsg, (item.reason ? reasonWith(item.reason, coverage) : name) +
          '；可改用「贴正文」保存内容，或只保存链接留档。', 'warn');
      }
      refs.input.value = '';
      refs.textarea.value = '';
      onInputChange();
      return loadFeed({ silent: true });
    }).catch(function (e) {
      setMsg(refs.readMsg, e.message, 'error');
      if (e.limited) {
        setMsg(refs.readMsg, e.message + '（本模块不会自动重试）', 'error');
      }
    }).then(function () {
      setBusy('read', false);
      syncControls();
    });
  }

  // 把读取结果并入列表与详情
  function applyReadResult(item) {
    if (!item.id) item.id = 'local-' + Date.now();
    var found = false;
    for (var i = 0; i < state.items.length; i++) {
      if (state.items[i].id === item.id) {
        state.items[i] = mergeItem(state.items[i], item);
        found = true;
        break;
      }
    }
    if (!found) state.items.unshift(item);
    state.selectedId = item.id;
    state.detail = mergeDetail(item);
    state.ask = { question: '', answer: '', cites: [], basis: '', loading: false, error: '' };
    state.tab = 'summary';
    renderChips();
    renderFeed();
    renderDetail();
  }

  function mergeItem(a, b) {
    a = a || {};
    b = b || {};
    var out = {};
    ['id', 'platform', 'title', 'author', 'publishedAt', 'publishedText', 'url', 'mode']
      .forEach(function (k) {
        var v = (b[k] === undefined || b[k] === null || b[k] === '') ? a[k] : b[k];
        out[k] = (v === undefined || v === null) ? '' : v;
      });
    out.coverage = coverageKey(b.coverage || a.coverage);
    out.excerpt = b.excerpt || a.excerpt || '';
    out.summary = b.summary || a.summary || '';
    out.keypoints = (b.keypoints && b.keypoints.length) ? b.keypoints : (a.keypoints || []);
    out.quotes = (b.quotes && b.quotes.length) ? b.quotes : (a.quotes || []);
    out.original = b.original || a.original || '';
    out.hasOriginal = !!(b.hasOriginal || a.hasOriginal || out.original);
    out.reason = b.reason || a.reason || '';
    out.trace = (b.trace && b.trace.length) ? b.trace : (a.trace || []);
    out.degraded = (b.degraded === true) ? true : (a.degraded === true);
    out.limited = (b.limited === true) ? true : (a.limited === true);
    return out;
  }

  // 详情对象：以 item 为底，保留可能的 service 扩展字段
  function mergeDetail(item) {
    return {
      item: item,
      loaded: true,
      source: ''
    };
  }

  /* ====================================================================== *
   * Feed
   * ====================================================================== */

  function loadFeed(opts) {
    var silent = !!(opts && opts.silent);
    if (state.feedLoading) return Promise.resolve();
    state.feedLoading = true;
    if (!silent) setMsg(state.refs.feedMsg, '正在读取内容列表…', 'busy');
    var q = '?platform=' + encodeURIComponent(state.platform || 'all');
    return apiGet(API.feed + q).then(function (data) {
      var o = obj(data);
      var items = arr(o.items).map(normalizeItem).filter(function (x) { return !!x.id; });
      state.items = items;
      state.diagnostics = arr(o.diagnostics).map(normalizeDiagnostic).filter(Boolean);
      state.feedError = '';
      return items;
    }, function (e) {
      // 失败不清空旧列表，只提示原因
      state.feedError = e.message;
      state.diagnostics = [];
      if (e.status === 404) {
        state.feedError = '内容列表接口暂不可用（服务端未实现 /api/content/feed）。';
      }
      return null;
    }).then(function () {
      state.feedLoading = false;
      if (state.feedError) setMsg(state.refs.feedMsg, state.feedError, 'error');
      else setMsg(state.refs.feedMsg, '', '');
      if (state.selectedId) {
        var keep = null;
        for (var i = 0; i < state.items.length; i++) {
          if (state.items[i].id === state.selectedId) { keep = state.items[i]; break; }
        }
        if (keep) {
          state.detail = mergeDetail(mergeItem((state.detail && state.detail.item) || keep, keep));
        } else if (!state.detail) {
          state.selectedId = '';
        }
      }
      renderChips();
      renderFeed();
      renderDiagnostics();
      renderDetail();
      syncControls();
    });
  }

  function renderFeed() {
    var refs = state.refs;
    var list = refs.feedList;
    clear(list);
    var items = state.items.filter(function (it) {
      return state.platform === 'all' || it.platform === state.platform;
    });
    setText(refs.feedCount, items.length ? (items.length + ' 条') : '');
    for (var i = 0; i < items.length; i++) {
      list.appendChild(feedRow(items[i]));
    }
    var emptyText;
    if (state.feedError) emptyText = '列表未取到，原因见上方提示。';
    else if (state.platform !== 'all' && capOf(state.platform) && !capOf(state.platform).read) {
      emptyText = platformName(state.platform) + ' 尚未配置读取权限，所以这里没有它的内容。可以贴原文或保存链接。';
    } else emptyText = '这个范围内还没有已读内容。粘贴一个链接试试。';
    setText(refs.feedEmpty, emptyText);
    show(refs.feedEmpty, items.length === 0);
  }

  function feedRow(item) {
    var li = node('li');
    var btn = node('button', 'dc-item');
    btn.type = 'button';
    btn.setAttribute('data-item-id', item.id);
    if (item.id === state.selectedId) btn.setAttribute('aria-current', 'true');

    var top = node('div', 'dc-item-top');
    append(top, markEl(item.platform), covEl(item.coverage));
    var time = formatTime(item.publishedAt) || item.publishedText;
    if (time) append(top, node('span', 'dc-item-time', time));

    append(btn, top);
    append(btn, node('p', 'dc-item-title', item.title || '(无标题)'));

    var meta = [];
    if (item.author) meta.push('作者：' + item.author);
    if (item.mode) meta.push('模式：' + item.mode);
    if (meta.length) append(btn, node('p', 'dc-item-meta', meta.join(' · ')));

    if (item.excerpt) append(btn, node('p', 'dc-item-excerpt', shorten(item.excerpt, 180)));

    li.appendChild(btn);
    return li;
  }

  function renderDiagnostics() {
    var refs = state.refs;
    var diags = state.diagnostics || [];
    clear(refs.diagList);
    for (var i = 0; i < diags.length; i++) {
      var li = node('li');
      li.setAttribute('data-level', diags[i].level);
      var lv = node('span', 'dc-diag-lv', diags[i].level === 'error' ? '错误' : (diags[i].level === 'warn' ? '提示' : '信息'));
      append(li, lv, node('span', 'dc-diag-msg', diags[i].message));
      refs.diagList.appendChild(li);
    }
    show(refs.diagEmpty, diags.length === 0);
    show(refs.diagWrap, diags.length > 0);
  }

  /* ====================================================================== *
   * 详情
   * ====================================================================== */

  function openDetail(id) {
    if (!id) return;
    if (state.selectedId === id && state.detail) {
      // 已选中：滚动到详情即可
      if (state.refs.detailPanel && state.refs.detailPanel.scrollIntoView) {
        state.refs.detailPanel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
      return;
    }
    var item = null;
    for (var i = 0; i < state.items.length; i++) {
      if (state.items[i].id === id) { item = state.items[i]; break; }
    }
    if (!item) return;
    state.selectedId = id;
    state.detail = mergeDetail(item);
    state.ask = { question: '', answer: '', cites: [], basis: '', loading: false, error: '' };
    state.tab = 'summary';
    renderFeed();
    renderDetail();
    maybeFetchFullText(item);
  }

  // 可选：服务端若实现了 /api/content/item/{id}，就补齐全文；未实现静默忽略
  function maybeFetchFullText(item) {
    if (!item || !item.id || item.original) return;
    if (!item.hasOriginal) return;
    var id = item.id;
    apiGet(API.item + encodeURIComponent(id)).then(function (data) {
      var full = normalizeItem(obj(data).item || data);
      if (!state.detail || !state.detail.item || state.detail.item.id !== id) return;
      var merged = mergeItem(state.detail.item, full);
      state.detail = mergeDetail(merged);
      for (var i = 0; i < state.items.length; i++) {
        if (state.items[i].id === id) state.items[i] = merged;
      }
      renderDetail();
    }, function () { /* 服务端未提供该接口：按已有内容渲染 */ });
  }

  function reloadCurrent() {
    var item = state.detail && state.detail.item;
    if (!item) return;
    if (!item.url) {
      setMsg(state.refs.detailMsg, '这条内容没有原链接（可能是直接粘贴的正文），无需重新读取。', 'warn');
      return;
    }
    if (state.busy.read) return;
    setBusy('read', true);
    syncControls();
    setMsg(state.refs.detailMsg, '正在重新读取…', 'busy');
    apiPost(API.read, { url: item.url, platform: item.platform || undefined, force: true }).then(function (data) {
      var o = obj(data);
      var fresh = normalizeItem(o.item);
      fresh.coverage = coverageKey(o.coverage || fresh.coverage);
      if (!fresh.reason) fresh.reason = str(o.reason);
      if (!fresh.trace.length) fresh.trace = normalizeTrace(o.trace);
      fresh.id = fresh.id || item.id;
      var merged = mergeItem(item, fresh);
      merged.id = item.id;
      state.detail = mergeDetail(merged);
      for (var i = 0; i < state.items.length; i++) {
        if (state.items[i].id === item.id) state.items[i] = merged;
      }
      setMsg(state.refs.detailMsg, '已刷新：' + coverageName(merged.coverage), 'ok');
      renderFeed();
      renderDetail();
    }).catch(function (e) {
      setMsg(state.refs.detailMsg, e.message, 'error');
    }).then(function () {
      setBusy('read', false);
      syncControls();
    });
  }

  function saveLink() {
    var item = state.detail && state.detail.item;
    if (!item || !item.url) return;
    if (state.busy.read) return;
    setBusy('read', true);
    syncControls();
    setMsg(state.refs.detailMsg, '正在保存链接…', 'busy');
    apiPost(API.read, { url: item.url, platform: item.platform || undefined }).then(function (data) {
      var o = obj(data);
      var fresh = normalizeItem(o.item);
      fresh.coverage = coverageKey(o.coverage || fresh.coverage);
      if (!fresh.reason) fresh.reason = str(o.reason);
      fresh.id = fresh.id || item.id;
      var merged = mergeItem(item, fresh);
      merged.id = item.id;
      state.detail = mergeDetail(merged);
      for (var i = 0; i < state.items.length; i++) {
        if (state.items[i].id === item.id) state.items[i] = merged;
      }
      setMsg(state.refs.detailMsg, '链接已保存留档（' + coverageName(merged.coverage) + '）。', 'ok');
      renderFeed();
      renderDetail();
    }).catch(function (e) {
      setMsg(state.refs.detailMsg, e.message, 'error');
    }).then(function () {
      setBusy('read', false);
      syncControls();
    });
  }

  function switchTab(id) {
    state.tab = id;
    renderTabs();
  }

  function renderTabs() {
    var refs = state.refs;
    var modelOK = !!(state.caps && state.caps.model.configured);
    ['summary', 'original', 'ask'].forEach(function (k) {
      var sel = state.tab === k;
      refs.tabs[k].setAttribute('aria-selected', sel ? 'true' : 'false');
      refs.tabs[k].setAttribute('tabindex', sel ? '0' : '-1');
      var pane = refs.panes[k];
      if (pane) pane.hidden = !sel;
    });
    refs.tabs.ask.setAttribute('title', modelOK ? '就这条内容提问' : '模型未配置，问答不可用');
  }

  function renderDetail() {
    var refs = state.refs;
    var d = state.detail;
    if (!d || !d.item) {
      show(refs.detail, false);
      show(refs.detailEmpty, true);
      return;
    }
    show(refs.detailEmpty, false);
    show(refs.detail, true);
    var item = d.item;

    // 头部
    clear(refs.dMark);
    refs.dMark.className = 'dc-mark';
    setText(refs.dMark, platformMark(item.platform));
    refs.dMark.setAttribute('data-platform', item.platform || 'unknown');

    setText(refs.dCov, coverageName(item.coverage));
    refs.dCov.className = 'dc-cov';
    refs.dCov.setAttribute('data-cov', coverageKey(item.coverage));
    refs.dCov.setAttribute('title', COVERAGE[coverageKey(item.coverage)].scope);
    show(refs.dLimited, item.limited);

    var time = formatTime(item.publishedAt) || item.publishedText;
    setText(refs.dTime, time);
    show(refs.dTime, !!time);

    setText(refs.dTitle, item.title || '(无标题)');

    setText(refs.dAuthor, item.author ? ('作者：' + item.author) : '作者：未提供');
    setText(refs.dPub, time ? ('时间：' + time) : '时间：未提供');
    var modeText = item.mode ? ('模式：' + item.mode) : '';
    setText(refs.dMode, modeText);
    show(refs.dMode, !!modeText);

    // 实际可读范围（显式展示）
    var key = coverageKey(item.coverage);
    refs.dScope.setAttribute('data-cov', key);
    setText(refs.dScopeLabel, '实际可读范围：' + coverageName(key));
    setText(refs.dScopeReason, item.reason || DEFAULT_REASON[key]);

    // 链接动作（只读）
    if (item.url) {
      refs.dLink.href = item.url;
      refs.dLink.title = item.url;
      show(refs.dLink, true);
    } else {
      refs.dLink.removeAttribute('href');
      show(refs.dLink, false);
    }
    show(refs.dSave, !!item.url);
    show(refs.dReload, !!item.url);

    if (item.degraded === true) {
      setMsg(refs.detailMsg, '该平台未配置读取权限，这条内容是降级保存的（正文或链接），能力有限。', 'warn');
    } else {
      setMsg(refs.detailMsg, '', '');
    }

    renderSummaryPane(item);
    renderOriginalPane(item);
    renderAskPane(item);
    renderTabs();
  }

  function renderSummaryPane(item) {
    var refs = state.refs;
    var summary = str(item.summary) || (item.coverage === 'excerpt' ? str(item.excerpt) : '');
    setText(refs.summaryText, summary);
    show(refs.summaryText, !!summary);
    setText(refs.summaryEmpty, item.coverage === 'excerpt'
      ? '只有摘录，没有生成摘要。'
      : '这条内容还没有摘要。');
    show(refs.summaryEmpty, !summary);

    clear(refs.keypoints);
    var kp = item.keypoints || [];
    for (var i = 0; i < kp.length; i++) refs.keypoints.appendChild(node('li', '', kp[i]));
    show(refs.keypointsWrap, kp.length > 0);

    clear(refs.quotes);
    var qs = item.quotes || [];
    for (var j = 0; j < qs.length; j++) {
      var li = node('li');
      var q = node('p', 'dc-quote', qs[j]);
      append(li, q, node('span', 'dc-quote-src', '模型生成，非原文'));
      refs.quotes.appendChild(li);
    }
    show(refs.quotesWrap, qs.length > 0);
    show(refs.summaryDisclaimer, !!(summary || kp.length || qs.length));
  }

  function renderOriginalPane(item) {
    var refs = state.refs;
    var key = coverageKey(item.coverage);

    // 拿不到原文时：明确写出状态与原因
    if (!READABLE[key]) {
      refs.originalGate.hidden = false;
      refs.originalGate.setAttribute('data-cov', key);
      clear(refs.originalGate);
      append(refs.originalGate, node('strong', '', coverageName(key)), node('span', '', '：' + (item.reason || DEFAULT_REASON[key]) + '。'));
      setText(refs.originalBody, '');
      show(refs.originalBody, false);
      setText(refs.originalEmpty, '原文不可读，状态：' + coverageName(key) + '。' + COVERAGE[key].scope +
        (item.url ? '你仍可以点上面的原链接自己查看，或把看到的内容贴进来保存。' : ''));
      show(refs.originalEmpty, true);
    } else {
      var body = str(item.original);
      if (key === 'partial' || key === 'excerpt') {
        refs.originalGate.hidden = false;
        refs.originalGate.setAttribute('data-cov', key);
        clear(refs.originalGate);
        append(refs.originalGate,
          node('strong', '', coverageName(key)),
          node('span', '', '：下面是服务端实际取到的部分，' + (item.reason || DEFAULT_REASON[key]) + '。'));
      } else {
        refs.originalGate.hidden = true;
      }
      if (body) {
        setText(refs.originalBody, body);
        show(refs.originalBody, true);
        show(refs.originalEmpty, false);
      } else {
        // 声明可读但没给正文时，明确说明拿到了什么
        show(refs.originalBody, false);
        refs.originalGate.hidden = false;
        refs.originalGate.setAttribute('data-cov', key);
        clear(refs.originalGate);
        append(refs.originalGate,
          node('strong', '', '未提供原文正文'),
          node('span', '', '：服务端标记为「' + coverageName(key) + '」，但未下发正文；详情接口也未补充。'));
        setText(refs.originalEmpty, item.excerpt ? ('目前只有摘录：' + item.excerpt) : '目前没有可显示的原文。');
        show(refs.originalEmpty, true);
      }
    }

    // 逐跳白名单 trace
    var trace = (item.trace && item.trace.length) ? item.trace : [];
    clear(refs.trace);
    for (var i = 0; i < trace.length; i++) {
      var h = trace[i];
      var li = node('li');
      if (h.allowed === false) li.setAttribute('data-allowed', 'false');
      else if (h.allowed === true) li.setAttribute('data-allowed', 'true');

      append(li, node('span', 'dc-trace-hop', '第 ' + h.hop + ' 跳'));

      var bodyWrap = node('div', 'dc-trace-body');
      var verdictText = h.allowed === true ? '放行' : (h.allowed === false ? '拒绝' : '未判定');
      var verdict = node('span', 'dc-verdict', verdictText);
      verdict.setAttribute('data-ok', h.allowed === true ? 'true' : (h.allowed === false ? 'false' : 'unknown'));

      var host = '';
      if (h.url) {
        try { host = new URL(h.url).host; } catch (e) { host = h.url; }
      }
      var hostLine = node('div', 'dc-trace-host');
      append(hostLine, verdict, node('span', '', host || '未提供地址'));

      append(bodyWrap, hostLine);
      if (h.reason) append(bodyWrap, node('div', 'dc-trace-reason', h.reason));
      append(li, bodyWrap);
      refs.trace.appendChild(li);
    }
    setText(refs.traceEmpty, item.url
      ? '服务端这次没有返回逐跳校验明细（可能直接命中缓存）。'
      : '这是直接粘贴的正文，不涉及任何跳转校验。');
    show(refs.traceTitle, trace.length > 0);
    show(refs.traceNote, trace.length > 0);
    show(refs.trace, trace.length > 0);
    show(refs.traceEmpty, trace.length === 0);
  }

  function syncAskState() {
    var refs = state.refs;
    var modelOK = !!(state.caps && state.caps.model.configured);
    var hasContent = !!(state.detail && state.detail.item &&
      (state.detail.item.original || state.detail.item.summary || state.detail.item.excerpt));
    var reason = '';
    if (!state.caps) reason = '还没拿到服务端能力信息，问答暂不可用。';
    else if (!modelOK) reason = state.caps.model.reason
      ? ('模型未配置：' + state.caps.model.reason)
      : '模型未配置，问答不可用。请先在控制台或服务端配置模型。';
    else if (!hasContent) reason = '这条内容没有可用的正文或摘要，问答没有依据。';

    var disabled = !!reason || !!state.busy.ask;
    if (refs.askInput) refs.askInput.disabled = disabled;
    if (refs.askBtn) {
      refs.askBtn.disabled = disabled;
      setText(refs.askBtn, state.busy.ask ? '回答中…' : '提问');
    }
    if (refs.askBlocked) {
      if (reason) {
        setText(refs.askBlocked, reason);
        refs.askBlocked.hidden = false;
      } else {
        refs.askBlocked.hidden = true;
      }
    }
  }

  function renderAskPane(item) {
    var refs = state.refs;
    syncAskState();
    setMsg(refs.askMsg, state.ask.error || '', state.ask.error ? 'error' : '');

    if (state.ask.answer) {
      clear(refs.answerHead);
      var basis = state.ask.basis === 'original' ? '依据原文' : (state.ask.basis === 'summary' ? '依据摘要' : '依据未知');
      var bnode = node('span', 'dc-mark', basis);
      append(refs.answerHead, bnode, node('span', '', '模型回答，可能出错，请对照原文核对。'));
      setText(refs.answerBody, state.ask.answer);
      refs.answer.hidden = false;
    } else {
      refs.answer.hidden = true;
    }

    clear(refs.cites);
    var cites = state.ask.cites || [];
    for (var i = 0; i < cites.length; i++) {
      refs.cites.appendChild(node('li', '', cites[i]));
    }
    show(refs.citesTitle, cites.length > 0);

    // 问答输入框失去焦点时保留问题
    if (refs.askInput && !refs.askInput.matches(':focus')) {
      refs.askInput.value = state.ask.question || '';
    }
  }

  function doAsk() {
    if (state.busy.ask) return;
    var refs = state.refs;
    var item = state.detail && state.detail.item;
    if (!item) return;
    var q = str(refs.askInput.value);
    state.ask.question = q;
    setMsg(refs.askMsg, '', '');
    if (!q) {
      state.ask.error = '请输入问题。';
      setMsg(refs.askMsg, state.ask.error, 'warn');
      return;
    }
    if (!state.caps || !state.caps.model.configured) {
      state.ask.error = '模型未配置，问答不可用。';
      setMsg(refs.askMsg, state.ask.error, 'warn');
      return;
    }
    setBusy('ask', true);
    syncAskState();
    setMsg(refs.askMsg, '正在回答…', 'busy');
    apiPost(API.ask, { id: item.id, question: q }).then(function (data) {
      var o = obj(data);
      var answer = pickStr(o, ['answer', 'text', 'content']);
      var cites = arr(o.cites).map(function (c) {
        if (typeof c === 'string') return str(c);
        return pickStr(obj(c), ['quote', 'text', 'content', 'excerpt']);
      }).filter(Boolean);
      state.ask.answer = answer;
      state.ask.cites = cites;
      state.ask.basis = str(o.basis);
      state.ask.error = answer ? '' : '服务未返回回答内容。';
      setMsg(refs.askMsg, state.ask.error, answer ? '' : 'error');
    }).catch(function (e) {
      state.ask.answer = '';
      state.ask.cites = [];
      state.ask.error = e.message;
      setMsg(refs.askMsg, e.message, 'error');
    }).then(function () {
      setBusy('ask', false);
      syncAskState();
      if (state.detail) renderAskPane(state.detail.item);
    });
  }

  /* ====================================================================== *
   * 搜索 / 订阅
   * ====================================================================== */

  function renderToolsPlatforms() {
    var sel = state.refs.toolsPlatform;
    var prev = sel.value;
    clear(sel);
    var list = (state.caps && state.caps.platforms.length) ? state.caps.platforms : PLATFORMS;
    for (var i = 0; i < list.length; i++) {
      var p = list[i];
      var opt = node('option');
      opt.value = p.id;
      var cap = capOf(p.id);
      var label = p.name;
      if (cap && !cap.search) label += '（未配置）';
      else if (!cap && state.caps) label += '（未配置）';
      opt.textContent = label;
      opt.disabled = !!(cap && !cap.search);
      sel.appendChild(opt);
    }
    if (prev) sel.value = prev;
    if (!sel.value && list.length) sel.value = list[0].id;
    if (state.platform !== 'all' && capSearchable(state.platform)) sel.value = state.platform;
    syncSubscribeState();
    syncControls();
  }

  function canSearch() {
    if (!state.caps) return false;
    var sel = state.refs.toolsPlatform;
    if (!sel || !sel.value) return false;
    return capSearchable(sel.value);
  }

  function syncSubscribeState() {
    var refs = state.refs;
    var sel = refs.toolsPlatform;
    if (!sel) return;
    var cap = capOf(sel.value);
    var ok = !!(cap && cap.subscribe);
    if (refs.subBox) {
      refs.subBox.disabled = !ok || !!state.busy.subscribe;
      if (!ok) refs.subBox.checked = false;
    }
    if (refs.toolsNote) {
      setText(refs.toolsNote, ok
        ? '订阅只是让服务端在授权范围内留意新内容，不代表能绕过平台限制。'
        : '该平台未开放订阅（未配置 / 未授权），这里只能搜索或贴原文。');
    }
    if (refs.searchBtn) refs.searchBtn.disabled = !!state.busy.search || !canSearch();
    if (refs.toolsKeyword) refs.toolsKeyword.disabled = !canSearch();
  }

  function doSearch() {
    if (state.busy.search) return;
    var refs = state.refs;
    var platform = refs.toolsPlatform.value;
    var keyword = str(refs.toolsKeyword.value);
    setMsg(refs.toolsMsg, '', '');
    refs.limitNote.hidden = true;
    if (!platform) {
      setMsg(refs.toolsMsg, '请先选择平台。', 'warn');
      return;
    }
    if (!capSearchable(platform)) {
      setMsg(refs.toolsMsg, platformName(platform) + ' 未配置搜索能力，无法搜索。可以改用「贴原文 / 保存链接」。', 'warn');
      return;
    }
    if (!keyword) {
      setMsg(refs.toolsMsg, '请输入关键词。', 'warn');
      refs.toolsKeyword.focus();
      return;
    }
    setBusy('search', true);
    syncControls();
    setMsg(refs.toolsMsg, '正在搜索…', 'busy');
    state.search.ran = true;
    state.search.keyword = keyword;
    apiPost(API.search, { platform: platform, keyword: keyword }).then(function (data) {
      var o = obj(data);
      var items = arr(o.items).map(normalizeItem).filter(function (x) { return !!x.id; });
      state.search.items = items;
      state.search.limited = o.limited === true;
      state.search.reason = str(o.reason);
      renderSearchResult(items);
      if (state.search.limited) {
        showLimit(state.search.reason, o);
        setMsg(refs.toolsMsg, '', '');
      } else {
        setMsg(refs.toolsMsg, items.length
          ? ('找到 ' + items.length + ' 条（' + platformName(platform) + '）。')
          : ('没有找到结果。' + (state.search.reason ? state.search.reason : '')), 'ok');
      }
    }).catch(function (e) {
      state.search.items = [];
      renderSearchResult([]);
      if (e.limited) {
        showLimit(e.message);
      } else {
        setMsg(refs.toolsMsg, e.message, 'error');
      }
    }).then(function () {
      setBusy('search', false);
      syncControls();
    });
  }

  function showLimit(reason, payload) {
    var refs = state.refs;
    var o = obj(payload);
    var limits = obj(o.limits);
    var secs = num(o.retryAfter);
    if (secs === null) secs = num(o.retryAfterSeconds);
    if (secs === null) secs = num(limits.retryAfter);
    if (secs === null) secs = num(limits.cooldownSeconds);
    var msg = '已限流';
    if (secs !== null && secs >= 0) msg += '，约 ' + Math.round(secs) + ' 秒后再试';
    if (reason) msg += '。' + reason;
    else msg += '。';
    msg += '（本模块不会自动重试）';
    setText(refs.limitNote, msg);
    refs.limitNote.hidden = false;
    setMsg(refs.toolsMsg, '', '');
  }

  function renderSearchResult(items) {
    var refs = state.refs;
    clear(refs.searchResults);
    for (var i = 0; i < items.length; i++) {
      refs.searchResults.appendChild(feedRow(items[i]));
    }
    show(refs.searchResultsTitle, items.length > 0);
  }

  function doSubscribe() {
    if (state.busy.subscribe) return;
    var refs = state.refs;
    var platform = refs.toolsPlatform.value;
    var keyword = str(refs.toolsKeyword.value);
    var enabled = refs.subBox.checked;
    setMsg(refs.toolsMsg, '', '');
    if (!keyword) {
      refs.subBox.checked = !enabled;
      setMsg(refs.toolsMsg, '请先填写关键词，再打开订阅。', 'warn');
      refs.toolsKeyword.focus();
      return;
    }
    if (!capOf(platform) || !capOf(platform).subscribe) {
      refs.subBox.checked = false;
      setMsg(refs.toolsMsg, platformName(platform) + ' 未开放订阅，操作未提交。', 'warn');
      return;
    }
    setBusy('subscribe', true);
    syncControls();
    setMsg(refs.toolsMsg, enabled ? '正在开启订阅…' : '正在关闭订阅…', 'busy');
    apiPost(API.subscribe, { platform: platform, keyword: keyword, enabled: enabled }).then(function (data) {
      var o = obj(data);
      var sub = obj(o.subscription);
      var active = bool(sub.enabled);
      if (active !== null) refs.subBox.checked = active;
      var name = str(sub.keyword) || keyword;
      setMsg(refs.toolsMsg, (refs.subBox.checked ? '已订阅：' : '已取消订阅：') + platformName(platform) + ' · ' + name, 'ok');
    }).catch(function (e) {
      refs.subBox.checked = !enabled;
      setMsg(refs.toolsMsg, e.message, 'error');
    }).then(function () {
      setBusy('subscribe', false);
      syncControls();
    });
  }

  /* ====================================================================== *
   * 对外接口
   * ====================================================================== */

  function mount(rootEl, opts) {
    var el = rootEl;
    if (typeof rootEl === 'string') el = document.getElementById(rootEl.replace(/^#/, '')) ||
      document.querySelector(rootEl);
    if (!el || el.nodeType !== 1) return false;

    // 已挂载过同一节点：不再重复绑定；是否拉数据由调用方决定
    if (state.mounted && state.root === el) {
      if (opts && opts.load) refresh();
      return true;
    }

    state.root = el;
    state.mounted = true;
    // 重置会话内状态（换容器重新挂载时不残留旧选择）
    state.caps = null;
    state.capsError = '';
    state.items = [];
    state.diagnostics = [];
    state.selectedId = '';
    state.detail = null;
    state.tab = 'summary';
    state.busy = {};
    state.search = { items: [], loading: false, limited: false, reason: '', keyword: '', ran: false };
    state.ask = { question: '', answer: '', cites: [], basis: '', loading: false, error: '' };

    build(el);
    renderStatus();
    renderChips();
    renderFeed();
    renderDiagnostics();
    renderDetail();
    renderToolsPlatforms();
    syncControls();
    // 默认只建 DOM、不发请求：未登录时不该预拉任何接口。
    if (opts && opts.load) refresh();
    return true;
  }

  function refresh() {
    if (!state.mounted) return Promise.resolve(false);
    // 只有真的在登录后的控制台里才发请求。
    if (!document.querySelector('#console:not([hidden])')) return Promise.resolve(false);
    state.lastCapabilitiesAt = Date.now();
    return loadCapabilities().then(function () {
      return loadFeed();
    }).then(function () {
      return true;
    }, function () {
      return false;
    });
  }

  /* 供 app.js 在“登录成功、控制台可见”之后显式调用：挂载 + 首次加载。 */
  function activate() {
    var el = document.querySelector('[data-content-root]');
    if (!el) return false;
    if (!state.mounted || state.root !== el) mount(el, { load: false });
    refresh();
    return true;
  }

  function autoMount() {
    var el = document.querySelector('[data-content-root]');
    if (!el) return false;
    // 首次登录后由 #console 显示触发 activate()；这里只建空壳，不拉数据。
    return mount(el, { load: false });
  }

  // 控制台（登录后才显示）一旦变得可见，就激活内容页并拉一次数据。
  function watchConsole() {
    var box = document.getElementById('console');
    if (!box || !window.MutationObserver) return;
    var wasHidden = box.hasAttribute('hidden');
    var obs = new MutationObserver(function () {
      var hidden = box.hasAttribute('hidden');
      if (wasHidden && !hidden) {
        wasHidden = false;
        activate();
      } else if (!wasHidden && hidden) {
        wasHidden = true;
      }
    });
    obs.observe(box, { attributes: true, attributeFilter: ['hidden'] });
    // 脚本加载时控制台可能已经可见（已登录直接进入）
    if (!box.hasAttribute('hidden')) activate();
  }

  window.DaFeiYuContent = {
    mount: mount,
    refresh: refresh,
    activate: activate,
    version: 2
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      autoMount();
      watchConsole();
    }, { once: true });
  } else {
    autoMount();
    watchConsole();
  }
})();
