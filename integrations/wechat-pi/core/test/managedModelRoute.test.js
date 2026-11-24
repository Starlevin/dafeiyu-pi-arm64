'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { loadConfig, resolveRuntime, resolveVisionConfig } = require('../src/infra/config');
const { applyConfigPatch, publicConfig } = require('../src/infra/configPatch');
const { DeepSeekClient } = require('../src/llm/client');
const { Bot } = require('../src/pipeline/chat');
const { applySettings } = require('../src/services/settings');
const { applyPersonaPatch } = require('../src/services/persona');
const vision = require('../src/adapters/vision');

const MODEL_HUB = 'http://127.0.0.1:18090/v1';
const OLD_PROVIDER = 'https://legacy-provider.invalid/v1';
const MODEL = 'workbuddy-deepseek';
const ROUTE_MANAGED = /统一控制台管理/;

function fixture(t, { baseUrl = MODEL_HUB } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dafeiyu-managed-route-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const configFile = path.join(root, 'config.json');
  fs.writeFileSync(configFile, JSON.stringify({
    provider: 'custom',
    llm: { baseUrl, model: MODEL, apiKey: 'fixture-local-chat-key' },
    vision: { baseUrl, model: MODEL, apiKey: 'fixture-local-vision-key' },
    admin: { allowAll: true },
  }));
  const config = loadConfig({ root, env: {} });
  fs.mkdirSync(config.paths.dataDir);
  fs.writeFileSync(config.paths.settingsFile, JSON.stringify({
    provider: 'deepseek', baseUrl: OLD_PROVIDER, model: 'legacy-model',
    apiKey: 'fixture-old-key', visionProvider: 'deepseek',
    visionBaseUrl: OLD_PROVIDER, visionModel: 'old-vision-model',
    keys: { deepseek: 'fixture-old-key', zhipu: 'fixture-backup-key' },
  }));
  const logger = { info() {}, debug() {}, warn() {}, error() {} };
  const llm = new DeepSeekClient({ config, logger });
  const runtime = {
    config, llm, logger,
    store: { summary: () => ({ sessions: 0, messages: 0, notes: 0 }) },
    gateways: new Map(), startedAt: Date.now(),
  };
  return { root, runtime };
}

test('managed chat and images ignore saved routes and keep configured credentials', (t) => {
  const { runtime: { config, llm } } = fixture(t);
  const rt = resolveRuntime(config);
  assert.equal(rt.managed, true);
  assert.equal(llm.endpoint(), MODEL_HUB + '/chat/completions');
  assert.equal(llm.model, MODEL);
  assert.equal(rt.apiKey, 'fixture-local-chat-key');
  assert.equal(resolveVisionConfig(config).baseUrl, MODEL_HUB);
  assert.equal(resolveVisionConfig(config).model, MODEL);
  assert.equal(vision.resolveKey(config), 'fixture-local-vision-key');
  assert.equal(llm.switchFallback(), null);
  assert.equal(llm.endpoint(), MODEL_HUB + '/chat/completions');
});

test('managed admin commands leave the provider route unchanged', (t) => {
  const { runtime } = fixture(t);
  const bot = new Bot(runtime);
  const before = fs.readFileSync(runtime.config.paths.settingsFile, 'utf8');
  for (const text of ['改模型 deepseek-flash', '用 deepseek', '演示模式 开']) {
    const reply = bot.handleAdmin({
      msg: { platform: 'wechat', chatType: 'private', senderId: 'fixture-owner' },
      clean: { text }, key: 'fixture', state: {}, nowMs: 1,
    });
    assert.match(reply.text, ROUTE_MANAGED);
  }
  assert.equal(fs.readFileSync(runtime.config.paths.settingsFile, 'utf8'), before);
});

test('model settings and persona requests reject route edits while normal settings persist', async (t) => {
  const { root, runtime } = fixture(t);
  const { config } = runtime;
  const before = fs.readFileSync(config.paths.configFile, 'utf8');
  for (const body of [{ model: 'another-model' }, { baseUrl: OLD_PROVIDER },
                      { visionBaseUrl: OLD_PROVIDER }, { visionCopyFromChat: true }]) {
    await assert.rejects(applySettings(runtime, body), ROUTE_MANAGED);
  }
  assert.throws(() => applyConfigPatch(runtime, { 'llm.baseUrl': OLD_PROVIDER }), ROUTE_MANAGED);
  assert.throws(() => applyConfigPatch(runtime, { 'vision.model': 'another-vision-model' }), ROUTE_MANAGED);
  assert.equal(fs.readFileSync(config.paths.configFile, 'utf8'), before);
  assert.equal((await applyPersonaPatch(runtime, { model: 'another-model' })).ok, false);
  assert.equal(publicConfig(config).config.llm.apiKey, '');
  assert.equal(publicConfig(config).secrets['llm.apiKey'], true);
  await applySettings(runtime, { provider: 'custom', model: MODEL, baseUrl: MODEL_HUB,
                                 temperature: 0.82, group: { cooldownSec: 42 } });
  const reloaded = loadConfig({ root, env: {} });
  assert.equal(reloaded.group.cooldownSec, 42);
  assert.equal(resolveRuntime(reloaded).temperature, 0.82);
  assert.equal(resolveRuntime(reloaded).baseUrl, MODEL_HUB);
});

test('a standalone installation still reads its explicitly selected provider', (t) => {
  const { runtime } = fixture(t, { baseUrl: 'http://127.0.0.1:19100/v1' });
  assert.equal(runtime.llm.runtime.managed, false);
  assert.equal(runtime.llm.model, 'legacy-model');
});
