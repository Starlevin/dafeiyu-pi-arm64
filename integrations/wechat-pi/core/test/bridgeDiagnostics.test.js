'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const register = require('../src/http/routes/status');

test('local status preserves fractional bridge time and rejects invalid diagnostics', () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'wechat-bridge-status-'));
  const oldStateHome = process.env.DAFEIYU_WECHAT_STATE_HOME;
  process.env.DAFEIYU_WECHAT_STATE_HOME = folder;
  try {
    const routes = new Map();
    register({ get: (url, handler) => routes.set(url, handler), post() {} });
    const route = routes.get('/api/status');
    const runtime = {
      config: { persona: { name: 'fixture' }, group: {} },
      llm: {}, gateways: new Map(), startedAt: Date.now(),
      store: { summary: () => ({}) },
    };
    const now = Math.floor(Date.now() / 1000) + 0.125;
    const stateFile = path.join(folder, 'bridge-health.json');
    const readStatus = (remoteAddress = '127.0.0.1') => {
      let payload;
      route(runtime, { socket: { remoteAddress } }, {
        writeHead: (code) => assert.equal(code, 200),
        end: (body) => { payload = JSON.parse(body); },
      });
      return payload.bridgeDiagnostics;
    };
    const client = { time: now, phase: 'client', reason: 'window_available' };
    const received = { time: now + 0.25, phase: 'received', reason: 'group' };
    const confirmed = { time: now + 0.5, phase: 'confirmation', reason: 'visible_outgoing' };
    fs.writeFileSync(stateFile, JSON.stringify({
      client, last_event: { ...received, text: 'never exposed' }, last_success: confirmed,
      last_failure: { time: String(now), phase: 'received', reason: 'failed' },
      last_unknown: { time: now, phase: [], reason: 'unknown' },
    }));
    assert.deepEqual(readStatus(), { client, last_event: received, last_success: confirmed, fresh: true });
    assert.equal(readStatus('192.0.2.10'), undefined);

    const stale = { ...client, time: now - 120 };
    fs.writeFileSync(stateFile, JSON.stringify({ client: stale }));
    assert.deepEqual(readStatus(), { client: stale, fresh: false });
  } finally {
    if (oldStateHome === undefined) delete process.env.DAFEIYU_WECHAT_STATE_HOME;
    else process.env.DAFEIYU_WECHAT_STATE_HOME = oldStateHome;
    fs.rmSync(folder, { recursive: true, force: true });
  }
});
