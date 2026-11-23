'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { test } = require('node:test');
const { Gateway } = require('../src/adapters/gateway');
const { Bot } = require('../src/pipeline/chat');
const { Store } = require('../src/memory/store');
const { loadConfig } = require('../src/infra/config');

test('a group preset reaches the compatibility model without changing the shared persona', async (t) => {
  const base = fs.realpathSync(os.tmpdir());
  const root = fs.mkdtempSync(path.join(base, 'dafeiyu-group-persona-'));
  t.after(() => {
    if (path.dirname(fs.realpathSync(root)) === base) fs.rmSync(root, { recursive: true, force: true });
  });
  const config = loadConfig({ root, env: {}, readOnly: true });
  config.websearch = { enabled: false };
  config.persona.customPrompt = '你是独立的合成角色。';
  const inherited = config.persona.stylePreset;
  const prompts = [];
  const llm = {
    isMock: false, model: 'fixture-model',
    async complete(options) {
      prompts.push(options.system);
      return '这是一次独立的语气测试回复。';
    },
  };
  const logger = { info() {}, debug() {}, warn() {}, error() {} };
  const store = new Store({ dir: path.join(root, 'store') });
  const bot = new Bot({ config, store, llm, logger });
  const gateway = new Gateway({ platform: 'wechat', bot, logger });
  const baseMessage = {
    chat_type: 'group', sender_id: 'fixture-user', sender_name: 'fixture member',
    at_me: true, text: '你好，请说一句问候语。',
  };
  const primary = { ...baseMessage, chat_id: 'fixture-room', chat_title: 'fixture room',
    msg_id: randomUUID(), bridge_style: '温柔' };
  const other = { ...baseMessage, chat_id: 'other-room', chat_title: 'other room', msg_id: randomUUID() };
  assert.equal((await gateway.handleInbound(primary, { local: true })).action, 'reply');
  assert.equal((await gateway.handleInbound(other, { local: true })).action, 'reply');
  assert.ok(prompts[0].includes('温和耐心，先理解'), 'preset must appear in the selected group prompt');
  assert.ok(!prompts[1].includes('温和耐心，先理解'), 'an independent group inherits its default');
  assert.ok(prompts[0].includes(config.persona.customPrompt), 'the custom role must remain in the prompt');
  assert.equal(config.persona.stylePreset, inherited);
});
