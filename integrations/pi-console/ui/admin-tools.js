/* 大肥鱼统一控制台 —— 原版工具面板（同页静态模块）
 *
 * 同页模块，不是第二前端：
 *   - 没有自己的登录页、不读 #pair、不发起第二次认证；
 *   - 复用本页已有的会话 Cookie（fetch credentials:'same-origin'）；
 *   - 渲染进 #toolsMount；
 *   - 所有服务端文本一律经 textContent 写入，绝不拼 HTML 字符串。
 *
 * 后端合同（固定路由 + 固定动作/参数，见 server/tool_routes.py；按实测
 * NapCat 4.18.42 核对：POST /api/auth/login 取会话 → Bearer 调固定动作）：
 *
 *   GET  /api/tools/status        登录态 / 版本 / 观测到的后台
 *   GET  /api/tools/qrcode        二维码；已在线时原版返回 code:-1，这里如实说明
 *   GET  /api/tools/login         登录态明细
 *   GET  /api/tools/onebot        OneBot 端口（令牌只报是否配置）
 *   GET  /api/tools/plugins       插件清单（含 pluginManagerNotFound 原因）
 *   GET  /api/tools/logs?source=&lines=
 *   GET  /api/tools/config        白名单字段 + 可编辑字段 + 客户端 IP + SSL 状态
 *   GET  /api/tools/files?root=&path=|&file=   只读文件浏览（固定根目录）
 *   GET  /api/tools/export        导出白名单配置 JSON
 *   GET  /api/tools/restart/plan  重启前的影响说明
 *   GET  /api/tools/capabilities  能力表（含"本版确实缺失+原因"）
 *   GET  /api/tools/original      原站入口（仅显示地址文本）
 *   POST /api/tools/refresh       {}                清缓存
 *   POST /api/tools/restart       {action,confirm:true}
 *   POST /api/tools/config        {action:'config.write',confirm:true,patch}
 *   POST /api/tools/import        {action:'config.import',confirm:true,patch}
 *
 * 危险动作：页面内一次确认（confirm:true），不二次登录。
 * 尚未接入的功能会显示当前状态。
 */
(() => {
  'use strict';

  const MOUNT_ID = 'toolsMount';
  const PREFIX = '/api/tools';

  const node = (tag, cls, txt) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (txt !== undefined && txt !== null) n.textContent = String(txt);
    return n;
  };
  const clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); };
  const arr = (v) => (Array.isArray(v) ? v : []);
  const yn = (v) => (v === true ? '是' : (v === false ? '否' : '未知'));
  const badge = (text, cls) => node('span', 'badge ' + cls, text);

  let mounted = false;
  let busy = false;
  let resetEpoch = 0;
  let data = {
    status: null, qr: null, login: null, onebot: null, recovery: null, logs: null,
    config: null, plugins: null, capabilities: null, original: null, files: null,
    restartPlan: null,
  };
  let logSource = 'qq-engine';
  let fileRoot = 'config';
  let filePath = '';
  const el = {};

  // ---- 网络 ---------------------------------------------------------------- //
  async function api(path, method, body) {
    const epoch = resetEpoch;
    const stale = () => {
      const error = new Error('会话已变更，已忽略旧请求结果。');
      error.stale = true;
      return error;
    };
    const opts = { method: method || 'GET', credentials: 'same-origin',
      cache: 'no-store', headers: {} };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      if (epoch !== resetEpoch) throw stale();
      throw new Error('无法连接控制台服务，请检查网络后重试。');
    }
    let payload = null;
    try { payload = await res.json(); } catch (e) { payload = null; }
    if (epoch !== resetEpoch) throw stale();
    if (!res.ok) {
      const reason = payload && typeof payload.error === 'string' && payload.error.trim()
        ? payload.error.trim()
        : '请求未完成，请稍后重试。';
      const err = new Error(reason);
      err.status = res.status;
      throw err;
    }
    return payload;
  }

  // ---- 局部状态 ------------------------------------------------------------ //
  function setNote(text, kind) {
    if (!el.note) return;
    el.note.textContent = text || '';
    el.note.setAttribute('data-kind', kind || '');
  }
  function syncDisabled() {
    const list = document.querySelectorAll('[data-tools-action]');
    for (let i = 0; i < list.length; i++) list[i].disabled = busy;
  }
  async function withBusy(label, fn) {
    if (busy) return;
    const epoch = resetEpoch;
    busy = true;
    syncDisabled();
    setNote(label, 'busy');
    try {
      await fn();
    } catch (e) {
      if (e.stale || epoch !== resetEpoch) return;
      if (e.status === 401) {
        setNote('登录已失效，请在本页重新登录后再使用工具面板。', 'error');
        const event = document.createEvent('Event');
        event.initEvent('fh-qq-admin-unauthorized', true, false);
        document.dispatchEvent(event);
        return;
      }
      setNote(e.message, 'error');
    } finally {
      if (epoch === resetEpoch) {
        busy = false;
        syncDisabled();
      }
    }
  }
  // 危险动作：只在本页确认一次（不弹第二次密码，也不跳转登录）。
  function confirmOnce(question) {
    try { return window.confirm(question); } catch (e) { return false; }
  }

  function metaList(pairs) {
    const dl = node('dl', 'meta-list');
    pairs.forEach((p) => {
      const row = node('div', 'meta-row');
      row.appendChild(node('dt', null, p[0]));
      row.appendChild(node('dd', p[2] ? 'mono' : null,
        p[1] === null || p[1] === undefined ? '未知' : String(p[1])));
      dl.appendChild(row);
    });
    return dl;
  }
  function table(headers, rows, opts) {
    const wrap = node('div', 'table-wrap');
    const t = node('table', 'data-table tools-table');
    const thead = node('thead');
    const hr = node('tr');
    headers.forEach((h) => hr.appendChild(node('th', null, h)));
    thead.appendChild(hr);
    t.appendChild(thead);
    const tb = node('tbody');
    rows.forEach((cells) => {
      const tr = node('tr');
      cells.forEach((c) => {
        const td = node('td');
        if (c && typeof c === 'object' && c.badge) td.appendChild(badge(c.text, c.badge));
        else if (c && typeof c === 'object' && c.node) td.appendChild(c.node);
        else td.textContent = (c === null || c === undefined) ? '—' : String(c);
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    wrap.appendChild(t);
    if (opts && opts.minWidth === false) t.classList.add('tools-table-narrow');
    return wrap;
  }

  // ---- 状态 ---------------------------------------------------------------- //
  function renderStatus() {
    const box = el.status;
    clear(box);
    const st = data.status;
    if (!st) { box.appendChild(node('p', 'empty', '尚未读取状态。')); return; }
    const nap = st.napcat || {};
    const gate = st.gate || {};
    const rows = [
      ['NapCat 接口', { badge: nap.reachable ? 'on' : 'warning',
        text: nap.reachable ? '可达' : '不可达' },
        nap.reachable ? '本机回环 6099，由运行中的 qq 进程持有' : (nap.error || '')],
      ['登录', { badge: nap.loggedIn ? 'on' : 'warning',
        text: nap.loggedIn ? '已登录' : (nap.loggedIn === false ? '未登录' : '未知') },
        nap.loggedIn ? '账号已在线' : '可在「扫码」里获取二维码'],
      ['离线', nap.offline === null || nap.offline === undefined ? '未知'
        : (nap.offline ? '是' : '否'), '掉线状态'],
      ['账号', nap.account || '—', '已掩码，完整账号不显示'],
      ['昵称', nap.nickname || '—', ''],
      ['NapCat 版本', nap.version || '未读到',
        nap.version ? '仅适配范围内放开写入' : '版本未知 → 写入已关闭'],
      ['QQ 版本', nap.qqVersion || '—', ''],
      ['WebUI 令牌', nap.tokenConfigured ? '已配置' : '未配置', '只报是否配置'],
      ['OneBot 令牌', nap.onebotTokenConfigured ? '已配置' : '未配置', '只报是否配置'],
    ];
    box.appendChild(table(['项目', '值', '说明'], rows, { minWidth: false }));
    if (gate.message) {
      box.appendChild(node('p', gate.adapted ? 'tools-ok' : 'tools-warn', gate.message));
    }
    const boxes = arr(st.boxes);
    if (boxes.length) {
      const chips = node('div', 'tools-chips');
      boxes.forEach((b) => {
        const chip = node('span', 'tools-chip');
        if (b.id === 'wechat') {
          chip.appendChild(node('span', null, '微信后台'));
          chip.appendChild(node('span', 'muted', ' 模型 ' + (b.model || '未知')));
          chip.appendChild(badge(b.bridgeOnline ? '在线' : '离线/未知',
            b.bridgeOnline ? 'on' : 'warning'));
        } else if (b.unit) {
          chip.appendChild(node('span', null, b.unit));
        }
        chips.appendChild(chip);
      });
      box.appendChild(node('p', 'card-note', '观测到的后台（只读）'));
      box.appendChild(chips);
    }
  }

  // ---- 扫码 ---------------------------------------------------------------- //
  function renderQr() {
    const box = el.qr;
    clear(box);
    const current = data.login && data.login.state;
    const d = (current && current.loggedIn === true)
      ? { loggedIn: true, canRefresh: false }
      : (data.qr || { loggedIn: current ? current.loggedIn : null,
        canRefresh: !!(data.login && data.login.canRefresh) });
    if (d.loggedIn) {
      box.appendChild(node('p', 'tools-ok', d.reason || '当前账号已在线，无需扫码。'));
    } else if (d.qr && d.qr.image) {
      const img = node('img', 'tools-qr');
      img.src = d.qr.image;
      img.alt = 'QQ 登录二维码';
      img.width = 220;
      img.height = 220;
      box.appendChild(img);
      box.appendChild(node('p', 'card-note',
        '用手机 QQ 扫码并在手机上确认；成功后点「重新读取」查看登录状态。'));
    } else {
      box.appendChild(node('p', 'tools-warn', d.reason || '二维码暂不可用。'));
    }
    const bar = node('div', 'tools-bar');
    const get = node('button', 'btn btn-sm', '获取二维码');
    get.type = 'button';
    get.setAttribute('data-tools-action', 'qr-get');
    get.addEventListener('click', () => withBusy('正在获取二维码…', async () => {
      data.qr = await api(PREFIX + '/qrcode');
      renderQr();
      setNote(data.qr.loggedIn ? 'QQ 已在线' : (data.qr.qr ? '二维码已获取' : data.qr.reason),
        data.qr.loggedIn || data.qr.qr ? 'ok' : 'warning');
    }));
    bar.appendChild(get);
    const refresh = node('button', 'btn btn-quiet btn-sm', '刷新二维码');
    refresh.type = 'button';
    refresh.setAttribute('data-tools-action', 'qr-refresh');
    refresh.disabled = !d.canRefresh || d.loggedIn !== false;
    refresh.addEventListener('click', () => withBusy('正在刷新二维码…', async () => {
      data.qr = await api(PREFIX + '/qrcode/refresh', 'POST', {action: 'qr.refresh', confirm: true});
      renderQr();
    }));
    bar.appendChild(refresh);
    box.appendChild(bar);
    if (data.login && data.login.state) {
      const s = data.login.state;
      box.appendChild(metaList([
        ['登录阶段', s.loginPhase || '未知'],
        ['内核就绪', yn(s.coreReady)],
        ['扫码已接受', yn(s.qrAccepted)],
        ['离线', yn(s.offline)],
      ]));
    }
  }

  // ---- OneBot -------------------------------------------------------------- //
  function renderOnebot() {
    const box = el.onebot;
    clear(box);
    const d = data.onebot;
    if (!d) { box.appendChild(node('p', 'empty', '尚未读取。')); return; }
    box.appendChild(metaList([
      ['OneBot 令牌', d.tokenConfigured ? '已配置' : '未配置'],
      ['配置文件', d.configPath || '未找到'],
    ]));
    const nets = arr(d.networks);
    if (nets.length) {
      box.appendChild(table(['类型', '名称', '地址', '启用', '令牌'], nets.map((n) => [
        n.label, n.name || '—',
        (n.host || '') + (n.port ? ':' + n.port : ''),
        { badge: n.enable ? 'on' : 'off', text: n.enable ? '是' : '否' },
        n.hasToken ? '已配置' : '无',
      ]), { minWidth: false }));
    } else {
      box.appendChild(node('p', 'empty', d.reason || '没有读到 OneBot 网络配置。'));
    }
    // 下游端口（引擎连的 3001）单独列出，便于排查。
    arr(d.endpoints).forEach((e) => box.appendChild(node('p', 'card-note',
      e.label + '：' + e.addr + '（' + (e.note || '') + '）')));
    if (d.writableReason) box.appendChild(node('p', 'card-note', d.writableReason));
  }

  // ---- 插件 ---------------------------------------------------------------- //
  function renderPlugins() {
    const box = el.plugins;
    clear(box);
    const d = data.plugins;
    if (!d) { box.appendChild(node('p', 'empty', '尚未读取。')); return; }
    if (!d.available) {
      box.appendChild(node('p', 'tools-warn', d.reason || '插件管理器不可用。'));
    }
    const list = arr(d.plugins);
    if (list.length) {
      box.appendChild(table(['插件', '版本', '启用'], list.map((p) => [
        p.name || p.id, p.version || '—',
        { badge: p.enabled ? 'on' : 'off', text: p.enabled ? '是' : '否' },
      ]), { minWidth: false }));
    } else if (d.available) {
      box.appendChild(node('p', 'empty', '当前没有已安装插件。'));
    }
    if (d.writableReason) box.appendChild(node('p', 'card-note', d.writableReason));
  }

  // ---- 配置（可写，需确认） ------------------------------------------------- //
  function renderConfig() {
    const box = el.config;
    clear(box);
    const d = data.config;
    if (!d) { box.appendChild(node('p', 'empty', '尚未读取。')); return; }
    const cfg = d.config || {};
    const keys = Object.keys(cfg);
    if (!keys.length) {
      box.appendChild(node('p', 'empty', d.reason || '没有可显示的配置字段。'));
    } else {
      box.appendChild(metaList(keys.map((k) => [k, String(cfg[k]), true])));
    }
    if (d.clientIp) box.appendChild(node('p', 'card-note', '客户端 IP：' + d.clientIp));
    if (d.ssl) {
      box.appendChild(node('p', 'card-note', 'SSL：证书 '
        + (d.ssl.certExists ? '已存在' : '不存在') + '，密钥 '
        + (d.ssl.keyExists ? '已存在' : '不存在') + '，'
        + (d.ssl.enabled ? '已启用' : '未启用')));
    }

    const editable = arr(d.editable).length ? arr(d.editable) : keys;
    const form = node('div', 'tools-form');
    const inputs = {};
    editable.forEach((k) => {
      const row = node('div', 'field');
      row.appendChild(node('label', null, k));
      const v = cfg[k];
      let input;
      if (typeof v === 'boolean') {
        input = node('select', 'select compact');
        [['true', 'true'], ['false', 'false']].forEach((o) => {
          const opt = node('option', null, o[1]);
          opt.value = o[0];
          if (String(v) === o[0]) opt.selected = true;
          input.appendChild(opt);
        });
      } else {
        input = node('input', 'input compact');
        input.type = 'text';
        input.value = v === undefined ? '' : String(v);
      }
      inputs[k] = input;
      row.appendChild(input);
      form.appendChild(row);
    });
    const save = node('button', 'btn btn-sm', '保存（需确认）');
    save.type = 'button';
    save.setAttribute('data-tools-action', 'config-save');
    save.addEventListener('click', () => withBusy('正在保存…', async () => {
      const patch = {};
      editable.forEach((k) => {
        const v = inputs[k].value;
        if (typeof cfg[k] === 'boolean') patch[k] = (v === 'true');
        else if (typeof cfg[k] === 'number') {
          const n = parseInt(v, 10);
          if (!Number.isFinite(n)) return;
          patch[k] = n;
        } else patch[k] = v;
      });
      const changed = Object.keys(patch).filter((k) => String(cfg[k]) !== String(patch[k]));
      if (!changed.length) { setNote('没有改动。', 'ok'); return; }
      if (!confirmOnce('将修改以下字段：' + changed.join('、')
        + '\n\n这会写入原版 WebUI 配置，确定执行一次吗？')) {
        setNote('已取消。', '');
        return;
      }
      await api(PREFIX + '/config', 'POST',
        { action: 'config.write', confirm: true, patch: patch });
      await reload(['config', 'status']);
      setNote('配置已保存。', 'ok');
    }));
    form.appendChild(save);
    box.appendChild(form);

    // 导出 / 导入
    const io = node('div', 'tools-bar');
    const exp = node('button', 'btn btn-quiet btn-sm', '导出配置');
    exp.type = 'button';
    exp.setAttribute('data-tools-action', 'export');
    exp.addEventListener('click', () => withBusy('正在导出…', async () => {
      const d2 = await api(PREFIX + '/export');
      const pre = node('pre', 'tools-log');
      pre.textContent = JSON.stringify(d2.config, null, 2);
      clear(el.exportBox);
      el.exportBox.appendChild(node('p', 'card-note',
        '导出内容（' + (d2.note || '') + '）：'));
      el.exportBox.appendChild(pre);
      setNote('已导出到页面（不含令牌）。', 'ok');
    }));
    io.appendChild(exp);
    const imp = node('button', 'btn btn-quiet btn-sm', '导入配置（需确认）');
    imp.type = 'button';
    imp.setAttribute('data-tools-action', 'import');
    imp.addEventListener('click', () => withBusy('正在导入…', async () => {
      const raw = window.prompt('粘贴要导入的 JSON（只允许白名单字段）：', '{}');
      if (raw === null) { setNote('已取消。', ''); return; }
      let patch;
      try { patch = JSON.parse(raw); } catch (e) {
        throw new Error('JSON 无法解析。');
      }
      if (!confirmOnce('将把这份配置写入原版 WebUI，确定执行一次吗？')) {
        setNote('已取消。', '');
        return;
      }
      await api(PREFIX + '/import', 'POST',
        { action: 'config.import', confirm: true, patch: patch });
      await reload(['config', 'status']);
      setNote('导入完成。', 'ok');
    }));
    io.appendChild(imp);
    box.appendChild(io);
    el.exportBox = node('div', 'tools-export');
    box.appendChild(el.exportBox);
    if (d.writableReason) box.appendChild(node('p', 'card-note', d.writableReason));
  }

  // ---- 重启 ---------------------------------------------------------------- //
  function renderRestart() {
    const box = el.restart;
    clear(box);
    const d = data.restartPlan;
    if (d) {
      box.appendChild(node('p', 'tools-mode', '原生重启：调用原版 Process/Restart'));
      const ul = node('ul', 'tools-list');
      arr(d.willDo).forEach((t) => ul.appendChild(node('li', null, '会做：' + t)));
      arr(d.willNotDo).forEach((t) => ul.appendChild(node('li', null, '不会：' + t)));
      box.appendChild(ul);
      if (d.ownerNote) box.appendChild(node('p', 'card-note', d.ownerNote));
    }
    const btn = node('button', 'btn btn-sm', '重启 QQ（需确认）');
    btn.type = 'button';
    btn.setAttribute('data-tools-action', 'restart');
    btn.addEventListener('click', () => withBusy('正在请求重启…', async () => {
      if (!confirmOnce('这会真的重启 QQ 客户端，期间自动回复会中断。'
        + '\n\n确定执行一次吗？')) {
        setNote('已取消。', '');
        return;
      }
      await api(PREFIX + '/restart', 'POST', { action: 'restart', confirm: true });
      setNote('已请求原版重启；稍后点「重新读取」查看状态。', 'ok');
    }));
    box.appendChild(btn);
  }

  // ---- 日志 ---------------------------------------------------------------- //
  function renderLogs() {
    const box = el.logs;
    clear(box);
    const d = data.logs;
    const bar = node('div', 'tools-row');
    const select = node('select', 'select compact');
    arr(d && d.sources).forEach((s) => {
      const opt = node('option', null, s);
      opt.value = s;
      if (s === logSource) opt.selected = true;
      select.appendChild(opt);
    });
    select.setAttribute('data-tools-action', 'log-source');
    select.addEventListener('change', () => {
      logSource = select.value;
      withBusy('正在读取日志…', loadLogs);
    });
    bar.appendChild(select);
    box.appendChild(bar);
    if (!d) { box.appendChild(node('p', 'empty', '尚未读取。')); return; }
    if (d.redactedLines) {
      box.appendChild(node('p', 'tools-warn',
        '有 ' + d.redactedLines + ' 行含敏感内容，已整体隐去。'));
    }
    const pre = node('pre', 'tools-log');
    pre.textContent = arr(d.lines).join('\n');
    box.appendChild(pre);
    if (d.truncated) box.appendChild(node('p', 'card-note', '仅显示最后若干行。'));
  }

  // ---- 文件浏览（只读） ----------------------------------------------------- //
  function renderFiles() {
    const box = el.files;
    clear(box);
    const d = data.files;
    const bar = node('div', 'tools-row');
    const sel = node('select', 'select compact');
    arr(d && d.roots).forEach((r) => {
      const opt = node('option', null, r);
      opt.value = r;
      if (r === fileRoot) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.setAttribute('data-tools-action', 'file-root');
    sel.addEventListener('change', () => {
      fileRoot = sel.value;
      filePath = '';
      withBusy('正在读取目录…', loadFiles);
    });
    bar.appendChild(sel);
    box.appendChild(bar);
    if (!d) { box.appendChild(node('p', 'empty', '尚未读取。')); return; }
    if (d.file) {
      const pre = node('pre', 'tools-log');
      pre.textContent = d.file.text || '';
      box.appendChild(node('p', 'card-note', '文件：' + d.file.path
        + (d.file.truncated ? '（已截断）' : '')));
      box.appendChild(pre);
      return;
    }
    const rows = arr(d.entries).map((e) => {
      const nameNode = node('button', 'tools-link', e.name);
      nameNode.type = 'button';
      if (e.browsable) {
        nameNode.addEventListener('click', () => {
          if (e.dir) {
            filePath = (d.path ? d.path + '/' : '') + e.name;
            withBusy('正在读取目录…', loadFiles);
          } else {
            withBusy('正在读取文件…', async () => {
              const enc = encodeURIComponent((d.path ? d.path + '/' : '') + e.name);
              data.files = await api(PREFIX + '/files?root=' + encodeURIComponent(fileRoot)
                + '&file=' + enc);
              renderFiles();
              setNote('已读取。', 'ok');
            });
          }
        });
      } else {
        nameNode.disabled = true;
      }
      return [nameNode, e.dir ? '目录' : '文件',
        e.size === null ? '—' : String(e.size)];
    });
    box.appendChild(table(['名称', '类型', '大小'], rows, { minWidth: false }));
    box.appendChild(node('p', 'card-note',
      '只读浏览固定目录（配置 / 日志 / 运行目录）。JSON 里的令牌一律隐去。'));
  }

  // ---- 能力表 / 原站入口 ---------------------------------------------------- //
  function renderCapabilities() {
    const box = el.capabilities;
    clear(box);
    const d = data.capabilities;
    if (!d) { box.appendChild(node('p', 'empty', '尚未读取。')); return; }
    const gate = d.gate || {};
    box.appendChild(node('p', gate.adapted ? 'tools-ok' : 'tools-warn',
      (gate.version ? '当前版本 ' + gate.version + '：' : '') + (gate.message || '')));
    const rows = arr(d.items).map((it) => {
      let b;
      if (it.level === 'denied') b = { badge: 'off', text: '不可用' };
      else if (!it.available) b = { badge: 'warning', text: '本版缺失' };
      else if (it.adapted === false) b = { badge: 'warning', text: '受限' };
      else b = { badge: 'info', text: '可用' };
      return [it.title, b, it.writable ? (it.confirm ? '可写·需确认' : '可写') : '只读',
        it.available ? (it.note || '') : (it.missingReason || it.note || '')];
    });
    box.appendChild(table(['能力', '状态', '权限', '说明'], rows));
  }

  function renderOriginal() {
    const box = el.original;
    clear(box);
    const d = data.original;
    if (!d) { box.appendChild(node('p', 'empty', '尚未读取。')); return; }
    const ul = node('ul', 'tools-list');
    arr(d.items).forEach((it) => {
      const li = node('li');
      li.appendChild(node('span', 'tools-cap-title', it.label));
      // 只显示地址文本，不生成可点击外链（避免变成通用反向代理/跳板）。
      li.appendChild(node('code', 'mono', it.addr || ''));
      li.appendChild(node('span', 'muted', it.note || ''));
      ul.appendChild(li);
    });
    box.appendChild(ul);
  }

  // ---- 数据流 -------------------------------------------------------------- //
  async function reload(what) {
    const epoch = resetEpoch;
    const partialFailures = [];
    const need = what || null;
    const get = (name, path) => {
      if (need && need.indexOf(name) === -1) return Promise.resolve(null);
      return api(path).catch((e) => {
        if (e.status === 401 || e.stale) throw e;
        partialFailures.push(name);
        return { __toolReadFailed: true };
      });
    };
    const res = await Promise.all([
      get('status', PREFIX + '/status'),
      get('login', PREFIX + '/login'),
      get('onebot', PREFIX + '/onebot'),
      get('recovery', PREFIX + '/recovery'),
      get('config', PREFIX + '/config'),
      get('plugins', PREFIX + '/plugins'),
      get('capabilities', PREFIX + '/capabilities'),
      get('files', PREFIX + '/files?root=' + encodeURIComponent(fileRoot)
        + (filePath ? '&path=' + encodeURIComponent(filePath) : '')),
      get('restart', PREFIX + '/restart/plan'),
    ]);
    if (epoch !== resetEpoch) return;
    const names = ['status', 'login', 'onebot', 'recovery', 'config',
      'plugins', 'capabilities', 'files', 'restartPlan'];
    names.forEach((n, i) => {
      if (res[i] !== null) data[n] = res[i] && res[i].__toolReadFailed ? null : res[i];
    });
    try {
      await loadLogs();
    } catch (e) {
      if (e.status === 401 || e.stale) throw e;
      data.logs = null;
      partialFailures.push('logs');
    }
    if (epoch !== resetEpoch) return;
    renderAll();
    if (partialFailures.length) setNote('部分 QQ 管理信息暂不可用，可重新读取。', 'error');
    return partialFailures.length > 0;
  }

  async function loadAll() {
    const partial = await reload(null);
    if (!partial) setNote('已更新。', 'ok');
  }
  async function loadLogs() {
    const epoch = resetEpoch;
    const result = await api(PREFIX + '/logs?source=' + encodeURIComponent(logSource) + '&lines=80');
    if (epoch !== resetEpoch) return;
    data.logs = result;
    renderLogs();
  }
  async function loadFiles() {
    const epoch = resetEpoch;
    const result = await api(PREFIX + '/files?root=' + encodeURIComponent(fileRoot)
      + (filePath ? '&path=' + encodeURIComponent(filePath) : ''));
    if (epoch !== resetEpoch) return;
    data.files = result;
    renderFiles();
  }

  function renderAll() {
    renderStatus(); renderQr(); renderOnebot(); renderPlugins(); renderConfig();
    renderRestart(); renderLogs(); renderFiles(); renderCapabilities();
  }

  // ---- 结构 ---------------------------------------------------------------- //
  function card(id, title, note) {
    const sec = node('div', 'card tools-card');
    sec.appendChild(node('h3', 'card-title', title));
    if (note) sec.appendChild(node('p', 'card-note', note));
    const body = node('div', 'tools-body');
    sec.appendChild(body);
    el[id] = body;
    return sec;
  }

  function build(mount) {
    clear(mount);
    const head = node('div', 'tools-head');
    head.appendChild(node('p', 'eyebrow', '统一控制台'));
    head.appendChild(node('h2', 'section-title', 'QQ 管理'));
    mount.appendChild(head);
    mount.appendChild(node('p', 'section-lead',
      '与统一控制台共用当前登录会话。危险操作（重启、改写配置、导入）'
      + '需要在本页确认一次。终端、更新等尚未接入的功能，在「能力与限制」'
      + '里写明原因。'));

    const bar = node('div', 'tools-bar');
    const reloadBtn = node('button', 'btn btn-quiet btn-sm', '重新读取');
    reloadBtn.type = 'button';
    reloadBtn.setAttribute('data-tools-action', 'reload');
    reloadBtn.addEventListener('click', () => withBusy('正在重新读取…', loadAll));
    bar.appendChild(reloadBtn);
    el.note = node('p', 'tools-note');
    el.note.setAttribute('role', 'status');
    el.note.setAttribute('aria-live', 'polite');
    bar.appendChild(el.note);
    mount.appendChild(bar);

    mount.appendChild(card('status', 'QQ 状态', '账号已掩码；令牌只报是否配置。'));
    mount.appendChild(card('qr', '扫码登录', 'QQ 离线时可在这里扫码登录。'));
    mount.appendChild(card('onebot', 'OneBot', '端口与令牌配置状态。'));
    mount.appendChild(card('plugins', '插件', '原版插件管理器状态。'));
    mount.appendChild(card('config', '设备配置', '白名单字段可改，提交需确认一次。'));
    mount.appendChild(card('restart', '原生重启', '走原版 Process/Restart，不另起客户端。'));
    mount.appendChild(card('logs', '运行日志', '敏感行整体隐去。'));
    mount.appendChild(card('files', '文件', '只读浏览固定目录。'));
    mount.appendChild(card('capabilities', '能力与限制', '含本版确实缺失的项与原因。'));
  }

  function mount() {
    if (mounted) return;
    const host = document.getElementById(MOUNT_ID);
    if (!host) return;
    mounted = true;
    build(host);
    withBusy('正在读取工具状态…', loadAll);
  }

  window.DaFeiYuAdminTools = {
    mount: mount,
    refresh: () => withBusy('正在重新读取…', loadAll),
    reset: () => {
      resetEpoch += 1;
      const host = document.getElementById(MOUNT_ID);
      if (host) clear(host);
      mounted = false;
      busy = false;
      data = { status: null, qr: null, login: null, onebot: null, recovery: null,
        logs: null, config: null, plugins: null, capabilities: null, original: null,
        files: null, restartPlan: null };
      filePath = '';
    },
  };

  function boot() {
    const details = document.getElementById('maintenanceTools');
    if (!details) return;
    details.addEventListener('toggle', () => { if (details.open) mount(); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
