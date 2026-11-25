'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { loadConfig } = require('../src/infra/config');
const { Bot } = require('../src/pipeline/chat');
const { getSettings, applySettings } = require('../src/services/settings');

const GROUPS = ['试点群A', '试点群B'];

/** 每个测试一份独立临时根目录；配置直接写成权威 config.json。 */
function fixture(t, { enabled = true, groups = GROUPS, cooldowns } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dafeiyu-pqc-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const proactiveJudge = { enabled, groups: [...groups] };
  if (cooldowns !== undefined) proactiveJudge.publicQuestionCooldowns = { ...cooldowns };
  const onDisk = {
    group: {
      proactiveJudge,
      requireAt: true,
      freeChat: false,
      cooldownSec: 7,
      maxRepliesPerMinute: 15,
    },
  };
  fs.mkdirSync(root, { recursive: true });
  fs.writeFileSync(path.join(root, 'config.json'), JSON.stringify(onDisk, null, 2) + '\n', 'utf8');
  const config = loadConfig({ root, env: {} });
  // applySettings 末尾要算 statusPayload，给它最小可用的桩；configPatch 要 llm.reload。
  const runtime = {
    config,
    logger: { info() {}, debug() {}, warn() {}, error() {} },
    llm: {
      isMock: true,
      provider: 'deepseek',
      providerLabel: 'fixture',
      needsKey: false,
      model: 'fixture',
      baseUrl: 'http://127.0.0.1:1/v1',
      apiKey: '',
      temperature: 0.95,
      reload() {},
    },
    store: { summary: () => ({ sessions: 0, messages: 0, notes: 0, totalMessages: 0, totalReplies: 0 }) },
    gateways: new Map(),
    bot: null,
    startedAt: Date.now(),
    version: 'fixture',
    stickerService: { reconcile() {} },
    watchConfig: false,
  };
  return { root, config, runtime, configFile: path.join(root, 'config.json') };
}

function readConfig(configFile) {
  return JSON.parse(fs.readFileSync(configFile, 'utf8'));
}

function assertOtherGroupSettingsUnchanged(configFile) {
  const group = readConfig(configFile).group;
  assert.equal(group.requireAt, true);
  assert.equal(group.freeChat, false);
  assert.equal(group.cooldownSec, 7);
  assert.equal(group.maxRepliesPerMinute, 15);
}

test('cooldown map persists to config.json and preserves other group settings', async (t) => {
  const { runtime, configFile } = fixture(t);
  await applySettings(runtime, { group: { publicQuestionCooldowns: { '试点群A': 90 } } });
  const raw = readConfig(configFile);
  assert.deepEqual(raw.group.proactiveJudge.publicQuestionCooldowns, { '试点群A': 90 });
  assert.deepEqual(raw.group.proactiveJudge.groups, GROUPS);
  assert.equal(raw.group.proactiveJudge.enabled, true);
  assertOtherGroupSettingsUnchanged(configFile);
  // 绝不写 settings.json 影子（这个字段只归 config.json）
  const saved = JSON.parse(fs.readFileSync(runtime.config.paths.settingsFile, 'utf8'));
  assert.equal(Object.prototype.hasOwnProperty.call(saved, 'publicQuestionCooldowns'), false);
  assert.equal(saved.group && saved.group.publicQuestionCooldowns, undefined);
});

test('second group write preserves the first group value', async (t) => {
  const { runtime, configFile } = fixture(t);
  await applySettings(runtime, { group: { publicQuestionCooldowns: { '试点群A': 90 } } });
  await applySettings(runtime, { group: { publicQuestionCooldowns: { '试点群B': 300 } } });
  assert.deepEqual(readConfig(configFile).group.proactiveJudge.publicQuestionCooldowns, {
    '试点群A': 90,
    '试点群B': 300,
  });
  assertOtherGroupSettingsUnchanged(configFile);
});

test('unknown group is rejected and the file is unchanged', async (t) => {
  const { runtime, configFile } = fixture(t);
  const before = fs.readFileSync(configFile, 'utf8');
  await assert.rejects(
    applySettings(runtime, { group: { publicQuestionCooldowns: { '未授权群': 90 } } }),
    /不在主动搭话白名单/
  );
  assert.equal(fs.readFileSync(configFile, 'utf8'), before);
  assert.deepEqual(readConfig(configFile).group.proactiveJudge.groups, GROUPS);
});

test('invalid seconds are rejected and the file is unchanged', async (t) => {
  for (const bad of [29, 1201, 60.5, '60', true]) {
    await t.test('invalid value ' + JSON.stringify(bad), async (sub) => {
      const { runtime, configFile } = fixture(sub);
      const before = fs.readFileSync(configFile, 'utf8');
      await assert.rejects(
        applySettings(runtime, { group: { publicQuestionCooldowns: { '试点群A': bad } } }),
        /整数秒/
      );
      assert.equal(fs.readFileSync(configFile, 'utf8'), before);
    });
  }
});

test('getSettings exposes enabled groups, cooldown map, and default', async (t) => {
  const { runtime } = fixture(t, { cooldowns: { '试点群A': 90 } });
  const group = getSettings(runtime).group;
  assert.equal(group.proactiveJudge.enabled, true);
  assert.deepEqual(group.proactiveJudge.groups, GROUPS);
  assert.deepEqual(group.proactiveJudge.publicQuestionCooldowns, { '试点群A': 90 });
  assert.equal(group.proactiveJudge.publicQuestionCooldownDefault, 60);
  // 原有字段不能被移除
  assert.equal(group.requireAt, true);
  assert.equal(group.cooldownSec, 7);
});

test('disabled judge rejects writes and leaves the file unchanged', async (t) => {
  const { runtime, configFile } = fixture(t, { enabled: false });
  const before = fs.readFileSync(configFile, 'utf8');
  await assert.rejects(
    applySettings(runtime, { group: { publicQuestionCooldowns: { '试点群A': 90 } } }),
    /总开关没开/
  );
  assert.equal(fs.readFileSync(configFile, 'utf8'), before);
});

test('pause is reflected immediately in the reply path, survives reload, and can be reverted', async (t) => {
  const { runtime, root, configFile } = fixture(t);
  const chat = { config: runtime.config, groupAutoReplyGroups: Bot.prototype.groupAutoReplyGroups };
  const msg = { platform: 'wechat', chatType: 'group', chatTitle: GROUPS[0], groupAutoReply: true };
  const normalize = () => Bot.prototype.normalize.call(chat, msg);
  assert.equal(normalize().groupAutoReply, true);
  await applySettings(runtime, { group: {
    proactiveGroupEnabled: { [GROUPS[0]]: false },
    publicQuestionCooldowns: { [GROUPS[0]]: 90 },
  } });
  assert.equal(normalize().groupAutoReply, false);
  assert.equal(Bot.prototype.normalize.call(chat, { ...msg, atMe: true }).atMe, true);
  assert.deepEqual(chat.groupAutoReplyGroups(), [GROUPS[1]]);
  assert.deepEqual(getSettings(runtime).group.proactiveJudge.pausedGroups, [GROUPS[0]]);
  assert.deepEqual(readConfig(configFile).group.proactiveJudge.groups, GROUPS);
  chat.config = loadConfig({ root, env: {} });
  assert.deepEqual(chat.groupAutoReplyGroups(), [GROUPS[1]]);
  await applySettings(runtime, { group: { proactiveGroupEnabled: { [GROUPS[0]]: true } } });
  chat.config = runtime.config;
  assert.equal(normalize().groupAutoReply, true);
  assert.equal(getSettings(runtime).group.proactiveJudge.publicQuestionCooldowns[GROUPS[0]], 90);
});

test('invalid toggle rejects the entire combined write', async (t) => {
  const { runtime, configFile } = fixture(t);
  const before = fs.readFileSync(configFile, 'utf8');
  for (const toggles of [{ '未授权群': true }, { [GROUPS[0]]: 0 }]) {
    await assert.rejects(applySettings(runtime, { group: {
      proactiveGroupEnabled: toggles, publicQuestionCooldowns: { [GROUPS[0]]: 90 },
    } }));
    assert.equal(fs.readFileSync(configFile, 'utf8'), before);
  }
});
