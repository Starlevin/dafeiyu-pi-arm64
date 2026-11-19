/* 大肥鱼统一控制台 —— 用量视图（独立可合并模块）
 *
 * 合并方式（不覆写别的 lane）：在 index.html 的 <head> 里加
 *   <link rel="stylesheet" href="usage.css">
 *   <script src="usage.js" defer></script>
 * 本文件自己找挂载点，找不到就安静退出；app.js 不需要任何改动。
 *
 * 合同：GET /api/usage（与 overview 同一会话鉴权）→
 *   {available, reason, startedAt, dayCount, coverage{inScope,notInScope,zeroToken},
 *    today|days7|since: {totals{in,out,total,calls,missing, callers},
 *                        providers:[{provider, model, in,out,total,calls,missing}],
 *                        empty}}
 * 规则：只用 textContent 写 DOM；数字全部来自 provider 返回的 usage，
 *       缺用量只显示「缺用量次数」，绝不估算或填充。
 */
(function () {
  'use strict';

  var MOUNT_ID = 'usagePane';
  var REFRESH_MS = 60000;
  var timer = null;
  var requestSeq = 0;     // 请求序号：过期的响应（含失败）一律丢弃，避免覆盖宿主数据
  var mounting = false;   // 渲染期间屏蔽轮询/自动加载，避免「外部注入的数据被 fetch 覆盖」
  var external = false;   // 宿主已自行推送数据：不再自动拉取
  var WINDOWS = [
    { key: 'today', label: '今日' },
    { key: 'days7', label: '近 7 日' },
    { key: 'since', label: '累计' }
  ];

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function fmt(n) {
    if (typeof n !== 'number' || !isFinite(n)) return '—';
    return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
  }

  function when(ts) {
    if (!ts || typeof ts !== 'number') return '尚未开始统计';
    var d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return '尚未开始统计';
    var p = function (v) { return (v < 10 ? '0' : '') + v; };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
      ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function findMount() {
    var found = document.getElementById(MOUNT_ID);
    if (found) return found;
    // 回退：放在「诊断」小节之前；再不行就放到 #console 末尾。
    var d = document.getElementById('hDiagnostics');
    var section = d && d.closest ? d.closest('section') : null;
    var host = section && section.parentNode;
    var consoleEl = document.getElementById('console');
    if (!host) host = consoleEl;
    if (!host) return null;
    var made = el('div', 'usage-embed');
    made.id = MOUNT_ID;
    if (section && section.parentNode === host) host.insertBefore(made, section);
    else host.appendChild(made);
    return made;
  }

  // 服务端合同是 {totals, providers, empty}；也兼容「扁平」写法
  // （providers 里有 in/out/total/calls/missing 而没有 totals 时，就地求和）。
  // 两者都缺就返回 null，让调用方显示空态，而不是编造数字。
  function windowOf(node) {
    if (!node || typeof node !== 'object') return null;
    var providers = Array.isArray(node.providers) ? node.providers : [];
    if (node.totals && typeof node.totals === 'object') {
      return { totals: node.totals, providers: providers, empty: !!node.empty };
    }
    if (!providers.length && !node.empty) {
      var flat = {};
      var any = false;
      ['in', 'out', 'total', 'calls', 'missing'].forEach(function (k) {
        if (typeof node[k] === 'number') { flat[k] = node[k]; any = true; }
      });
      if (any) return { totals: flat, providers: [], empty: !flat.calls };
    }
    var sum = { in: 0, out: 0, total: 0, calls: 0, missing: 0 };
    providers.forEach(function (row) {
      ['in', 'out', 'total', 'calls', 'missing'].forEach(function (k) {
        if (typeof row[k] === 'number') sum[k] += row[k];
      });
    });
    return { totals: sum, providers: providers, empty: !sum.calls && !providers.length };
  }

  function renderTiles(box, win) {
    var totals = (win && win.totals) || {};
    var tiles = [
      ['输入 Token', totals.in, '来自 provider usage 的输入计数'],
      ['输出 Token', totals.out, '来自 provider usage 的输出计数'],
      ['总量', totals.total, 'provider 给的 total_tokens；未给时用输入+输出'],
      ['调用数', totals.calls, '真正发到上游模型的请求次数（含 fallback 两端）'],
      ['缺用量', totals.missing, '上游没返回 usage 的次数：单独计数，不做估算']
    ];
    var grid = el('div', 'usage-tiles');
    tiles.forEach(function (item) {
      var tile = el('div', 'usage-tile' + (item[0] === '缺用量' ? ' warn' : ''));
      tile.appendChild(el('span', 'usage-tile-label', item[0]));
      tile.appendChild(el('strong', 'usage-tile-value',
        typeof item[1] === 'number' ? fmt(item[1]) : '—'));
      tile.appendChild(el('span', 'usage-tile-note', item[2]));
      tile.title = item[2];
      grid.appendChild(tile);
    });
    box.appendChild(grid);
  }

  function renderTable(box, win) {
    var list = (win && win.providers) || [];
    if (!list.length) {
      box.appendChild(el('p', 'usage-empty', '这个时间窗口还没有已记账的模型调用。'));
      return;
    }
    var wrap = el('div', 'usage-table-wrap');
    var table = el('table', 'usage-table');
    var head = el('thead');
    var hr = el('tr');
    ['Provider', '实际模型', '输入', '输出', '总量', '调用数', '缺用量'].forEach(function (t) {
      hr.appendChild(el('th', null, t));
    });
    head.appendChild(hr);
    table.appendChild(head);
    var body = el('tbody');
    list.forEach(function (row) {
      var tr = el('tr');
      tr.appendChild(el('td', 'usage-provider', row.provider));
      tr.appendChild(el('td', 'usage-model', row.model));
      ['in', 'out', 'total', 'calls', 'missing'].forEach(function (k, i) {
        var cell = el('td', 'usage-num' + (k === 'missing' && row.missing > 0 ? ' warn' : ''), fmt(row[k]));
        tr.appendChild(cell);
      });
      body.appendChild(tr);
    });
    table.appendChild(body);
    wrap.appendChild(table);
    box.appendChild(wrap);
  }

  function renderCoverage(box, coverage) {
    var cov = coverage || {};
    var det = el('details', 'usage-coverage');
    det.appendChild(el('summary', null, '覆盖边界（哪些调用不在统计内）'));
    var groups = [
      ['已覆盖', cov.inScope],
      ['不在统计内', cov.notInScope],
      ['不产生 token', cov.zeroToken]
    ];
    groups.forEach(function (g) {
      if (!Array.isArray(g[1]) || !g[1].length) return;
      det.appendChild(el('h4', 'usage-cov-title', g[0]));
      var ul = el('ul', 'usage-cov-list');
      g[1].forEach(function (line) { ul.appendChild(el('li', null, line)); });
      det.appendChild(ul);
    });
    box.appendChild(det);
  }

  function renderUnavailable(box, data) {
    var note = el('p', 'usage-unavailable');
    note.appendChild(el('strong', null, '用量统计暂不可用：'));
    note.appendChild(el('span', null, data.reason || '网关上还没有启用用量统计'));
    box.appendChild(note);
    box.appendChild(el('p', 'usage-hint',
      '启用方法见部署说明：在 model-hub 上加记账补丁后重启该服务即可，不影响聊天。'));
  }

  function render(input) {
    var mount = findMount();
    if (!mount) return;
    mounting = true;        // 渲染回调里不再触发自动加载
    var data = (input && typeof input === 'object') ? input : {};
    try {
      paint(mount, data);
    } catch (e) {
      // 渲染本身出问题也不能让页面白屏：退化成「不可用」提示。
      while (mount.firstChild) mount.removeChild(mount.firstChild);
      var fallback = el('div', 'card usage-card');
      renderUnavailable(fallback, { reason: '用量视图渲染失败，请刷新后重试' });
      mount.appendChild(fallback);
    } finally {
      mounting = false;
    }
  }

  function paint(mount, data) {
    while (mount.firstChild) mount.removeChild(mount.firstChild);

    var card = el('div', 'card usage-card');
    var head = el('div', 'usage-head');
    var title = el('h3', 'card-title', 'Token 用量');
    head.appendChild(title);
    var metaBits = [];
    if (data.available) {
      metaBits.push('统计起始 ' + when(data.startedAt));
      metaBits.push('按 provider + 实际模型汇总');
      if (data.dayCount) metaBits.push('已记录 ' + data.dayCount + ' 天');
    }
    head.appendChild(el('span', 'usage-meta', metaBits.join(' · ')));
    var refresh = el('button', 'btn btn-quiet btn-sm usage-refresh', '刷新用量');
    refresh.type = 'button';
    refresh.addEventListener('click', function () {
      external = false;     // 用户主动刷新：交回服务端数据源
      load(mount);
    });
    head.appendChild(refresh);
    card.appendChild(head);

    if (data.available !== true) {
      renderUnavailable(card, data);
      mount.appendChild(card);
      return;
    }

    WINDOWS.forEach(function (win) {
      var node = windowOf(data[win.key]);
      var block = el('div', 'usage-window');
      block.appendChild(el('h4', 'usage-window-title', win.label));
      renderTiles(block, node);
      renderTable(block, node);
      card.appendChild(block);
    });
    renderCoverage(card, data.coverage);
    mount.appendChild(card);
  }

  function load(mount) {
    if (mounting || external) return;   // 宿主接管期间不再自动拉取
    var card = mount.querySelector('.usage-card');
    if (card) card.setAttribute('data-loading', '1');
    var seq = ++requestSeq;
    fetch('/api/usage', { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } })
      .then(function (res) {
        if (!res.ok) throw new Error(String(res.status));
        return res.json();
      })
      .then(function (data) {
        // 宿主在等待期间接管了数据源：丢弃这次自动拉取的结果，不覆盖宿主数据。
        if (seq !== requestSeq || external || mounting) return;
        render(data);
      })
      .catch(function () {
        if (seq !== requestSeq || external || mounting) return;
        render({ available: false, reason: '无法读取用量（控制台服务未响应）' });
      });
  }

  function start() {
    var mount = findMount();
    var consoleEl = document.getElementById('console');
    // 登录前 #console 是 hidden 的：等它出现再挂，避免无意义请求。
    if (consoleEl && consoleEl.hidden) {
      var observer = new MutationObserver(function () {
        if (!consoleEl.hidden) {
          observer.disconnect();
          begin(mount);
        }
      });
      observer.observe(consoleEl, { attributes: true, attributeFilter: ['hidden'] });
      return;
    }
    begin(mount);
  }

  function begin(mount) {
    if (!mount) return;
    // 宿主页若已自行注入数据（window.DaFeiYuUsage.render 先被调用），不再抢着 fetch。
    if (mounting || external) return;
    if (mount.querySelector('.usage-card')) return;
    load(mount);
    if (timer) clearInterval(timer);
    timer = setInterval(function () {
      var consoleEl = document.getElementById('console');
      if (consoleEl && !consoleEl.hidden && !mounting) load(mount);
    }, REFRESH_MS);
  }

  // 宿主显式推送数据时，暂停自动拉取：否则一次失败的 fetch 会把好数据覆盖成
  // 「不可用」。之后只要用户点「刷新用量」，就会恢复自动刷新。
  function takeOver() {
    external = true;
    if (timer) { clearInterval(timer); timer = null; }
  }

  // 允许别的 lane 用 window.DaFeiYuUsage.render(payload) 直接推送数据。
  window.DaFeiYuUsage = {
    render: function (payload) { takeOver(); render(payload); },
    reload: function () { external = false; start(); }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
