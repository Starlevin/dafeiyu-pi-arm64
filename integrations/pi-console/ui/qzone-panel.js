/* Explicitly mounted read-only QQ Zone panel for the authenticated QQ home tab. */
(() => {
  'use strict';
  let root = null;
  let active = false;
  let epoch = 0;
  let busy = false;

  const el = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = String(text);
    return n;
  };

  async function request(path, method, body, requestEpoch) {
    const response = await fetch(path, {
      method, credentials: 'same-origin', cache: 'no-store',
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined
    });
    let value = {};
    try { value = await response.json(); } catch (_) { /* stable message below */ }
    if (!active || requestEpoch !== epoch) return null;
    if (!response.ok) {
      const error = new Error(value.error || '请求未完成');
      error.status = response.status;
      if (response.status === 401) {
        document.dispatchEvent(new Event('fh-qq-admin-unauthorized'));
      }
      throw error;
    }
    return value;
  }

  function render(snapshot, status, list) {
    while (list.firstChild) list.removeChild(list.firstChild);
    const rows = Array.isArray(snapshot && snapshot.items) ? snapshot.items : [];
    status.textContent = snapshot && snapshot.refreshed_ts
      ? `快照时间戳：${snapshot.refreshed_ts}；${rows.length} 条动态`
      : '暂无已保存的好友动态快照。';
    for (const row of rows) {
      const card = el('article', undefined, 'tools-card qzone-read-card');
      const author = typeof row.author === 'string' ? row.author : '好友';
      const content = typeof row.text === 'string' ? row.text : '（无文字内容）';
      card.append(el('h4', author), el('p', content));
      if (row.ts) card.append(el('small', row.day || row.ts));
      list.appendChild(card);
    }
  }

  async function loadSnapshot(status, list) {
    const requestEpoch = epoch;
    const value = await request('/api/qzone/latest?limit=20', 'GET', null, requestEpoch);
    if (value && active && requestEpoch === epoch) render(value.data || value, status, list);
  }

  function mount(container) {
    if (!container || !document.documentElement.contains(container)) return false;
    if (root && root.parentNode) return true;
    active = true;
    busy = false;
    const requestEpoch = ++epoch;
    root = el('section', undefined, 'qzone-read-panel');
    root.setAttribute('aria-label', 'QQ 空间好友动态，只读');
    const title = el('h3', 'QQ 空间好友动态', 'section-title');
    const note = el('p', '动态读取仅访问好友动态接口，不支持发布、点赞或评论。', 'section-lead');
    const button = el('button', '读取好友动态', 'btn');
    button.type = 'button';
    const status = el('p', '正在读取已保存快照…', 'qzone-read-status');
    const list = el('div', undefined, 'qzone-read-list');
    root.append(title, note, button, status, list);
    container.appendChild(root);

    button.addEventListener('click', async () => {
      if (!active || busy) return;
      busy = true;
      const operationEpoch = epoch;
      button.disabled = true;
      status.textContent = '正在读取好友动态…';
      try {
        const value = await request('/api/qzone/refresh', 'POST',
          { action: 'qzone.refresh', confirm: true }, operationEpoch);
        if (value && active && operationEpoch === epoch) {
          await loadSnapshot(status, list);
        }
      } catch (error) {
        if (active && operationEpoch === epoch) {
          status.textContent = error && error.message ? error.message : '读取失败';
        }
      } finally {
        if (active && operationEpoch === epoch) {
          busy = false;
          button.disabled = false;
        }
      }
    });

    loadSnapshot(status, list).catch((error) => {
      if (active && requestEpoch === epoch) {
        status.textContent = error && error.status === 401
          ? '登录状态已失效，请重新登录。'
          : (error && error.message ? error.message : '需要登录后才能查看已保存快照。');
      }
    });
    return true;
  }

  function refreshSnapshot() {
    if (!active || !root) return;
    const status = root.querySelector('.qzone-read-status');
    const list = root.querySelector('.qzone-read-list');
    if (status && list) loadSnapshot(status, list).catch((error) => {
      if (active && status.isConnected) status.textContent = error.message || '读取失败';
    });
  }

  function reset() {
    active = false;
    busy = false;
    epoch += 1;
    if (root) {
      while (root.firstChild) root.removeChild(root.firstChild);
      if (root.parentNode) root.parentNode.removeChild(root);
    }
    root = null;
  }

  window.DaFeiYuQzone = { mount, reset, refresh: refreshSnapshot };
})();
