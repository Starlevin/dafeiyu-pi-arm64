'use strict';

const { statusPayload, recentMessages } = require('../../services/status');
const { json } = require('../util');
const { isLoopbackRequest } = require('../security');
const memoryBackup = require('../../memory/backup');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

function bridgeDiagnostics() {
  try {
    const stateHome = process.env.DAFEIYU_WECHAT_STATE_HOME || path.join(
      process.env.XDG_STATE_HOME || path.join(os.homedir(), '.local', 'state'), 'dafeiyu-wechat');
    const file = path.join(stateHome, 'bridge-health.json');
    const data = JSON.parse(fs.readFileSync(file, 'utf8'));
    const result = {};
    for (const key of ['client', 'last_event', 'last_failure', 'last_success', 'last_unknown']) {
      const value = data[key];
      if (!Number.isFinite(value?.time) || value.time <= 0 ||
          typeof value.phase !== 'string' || !/^[a-z_]{1,32}$/.test(value.phase) ||
          typeof value.reason !== 'string' || !/^[A-Za-z0-9_:-]{1,80}$/.test(value.reason)) continue;
      result[key] = { time: value.time, phase: value.phase, reason: value.reason };
      if (/^[a-f0-9-]{36}$/.test(value.trace)) result[key].trace = value.trace;
    }
    result.fresh = !!result.client && Math.abs(Date.now() / 1000 - result.client.time) <= 60;
    return result;
  } catch {
    return { fresh: false };
  }
}

module.exports = function register(router) {
  router.get('/api/status', (runtime, req, res) => {
    const status = statusPayload(runtime);
    if (isLoopbackRequest(req)) status.bridgeDiagnostics = bridgeDiagnostics();
    return json(res, 200, status);
  });
  router.get('/bridge-status', (runtime, req, res) => {
    if (!isLoopbackRequest(req)) return json(res, 403, { ok: false });
    const state = bridgeDiagnostics();
    const labels = { client: '客户端', last_event: '最近处理', last_failure: '最近失败',
      last_success: '最近确认', last_unknown: '最近发送待核对' };
    const rows = Object.entries(labels).map(([key, label]) => {
      const entry = state[key];
      const detail = entry ? `${entry.phase} / ${entry.reason}` : '暂无记录';
      const when = entry ? new Date(entry.time * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '';
      return `<tr><th scope="row">${label}</th><td>${detail}</td><td>${when}</td><td>${entry?.trace || ''}</td></tr>`;
    }).join('');
    const html = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta http-equiv="refresh" content="10"><title>大肥鱼微信状态</title>
<body><h1>微信桥接状态</h1><p>${state.fresh ? '状态已刷新' : '桥接状态超过 60 秒未更新'}</p>
<table><thead><tr><th>项目</th><th>结果</th><th>北京时间</th><th>诊断编号</th></tr></thead><tbody>${rows}</tbody></table>
</body></html>`;
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
    res.end(html);
  });
  // 启动器状态页的「最近动态」：只读内存快照，本机可读（与 /api/status 同级信息）。
  router.get('/api/recent', (runtime, req, res, info) => {
    const raw = Number(info.url.searchParams.get('limit') || 10);
    const limit = Number.isFinite(raw) ? Math.min(50, Math.max(1, Math.round(raw))) : 10;
    return json(res, 200, { ok: true, items: recentMessages(runtime.store, limit) });
  });
  // 本机运维控制，只回成功/失败；不开放任何记忆正文、导出或恢复接口。
  router.post('/api/runtime/prepare-stop', (runtime, req, res) => {
    const gateway = runtime.gateways.get('wechat');
    if (!isLoopbackRequest(req) || !gateway || !gateway.verifyToken(req, null)) {
      return json(res, 403, { ok: false, error: '停止准备仅允许通过本机令牌调用' });
    }
    runtime.store.flush();
    const pending = [...runtime.store.chatStore.chats.values()].some(chat => chat.dirtyMeta || chat.dirtySummary);
    if (pending) return json(res, 503, { ok: false, error: '记忆尚未保存，已取消停止准备' });
    memoryBackup.writeSnapshot(runtime.config.root, 'before-stop');
    return json(res, 200, { ok: true });
  });
};
