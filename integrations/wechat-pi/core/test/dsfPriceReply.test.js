'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { loadConfig } = require('../src/infra/config');
const { Store } = require('../src/memory/store');
const { Bot } = require('../src/pipeline/chat');

/**
 * 构建隔离的微信 Bot 测试环境（不触达真实群或外部网络）。
 */
function createIsolatedBot(completer) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dafeiyu-resilience-'));
  const config = loadConfig({ root, env: {} });
  config.group.allowlist = ['别野'];
  config.group.allowGroups = ['别野'];
  config.group.atReply = true;
  config.group.proactiveJudge = { enabled: false, groups: [] };
  config.stickers = { enabled: false };
  config.socialStyle = { enabled: false };
  config.search = { enabled: false };
  const store = new Store({ dir: path.join(root, 'store') });
  let callCount = 0;
  let fallbackSwitched = false;
  const llm = {
    provider: 'primary',
    model: 'primary-model',
    usageSeq: 0,
    usageSince() { return { calls: 0 }; },
    switchFallback() {
      if (fallbackSwitched) return null;
      fallbackSwitched = true;
      this.provider = 'backup';
      this.model = 'backup-model';
      return { label: 'backup-model' };
    },
    async complete(req) {
      callCount += 1;
      return completer(req, this, callCount);
    },
  };
  let nowMs = 1795240000000;
  const bot = new Bot({
    config,
    store,
    llm,
    now: () => new Date(nowMs),
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  return {
    bot,
    store,
    llm,
    getCallCount: () => callCount,
    didFallback: () => fallbackSwitched,
    advanceMs: (ms) => { nowMs += ms; },
    cleanup: () => {
      store.close();
      fs.rmSync(root, { recursive: true, force: true });
    },
  };
}

test('compatibility WeChat gives verified single-item prices without calling the model', async t => {
  const https = require('node:https');
  const { EventEmitter } = require('node:events');
  const price = require('../src/services/dsfPrice');
  const botEnv = createIsolatedBot(async (_req, _llm, count) => (count === 1
    ? '普通聊天仍调用模型' : '带着小队先从边路走一圈再说'));
  t.after(botEnv.cleanup);
  const previous = process.env.DAFEIYU_DSF_PRICE;
  process.env.DAFEIYU_DSF_PRICE = '1';
  t.after(() => {
    price.forget();
    if (previous === undefined) delete process.env.DAFEIYU_DSF_PRICE;
    else process.env.DAFEIYU_DSF_PRICE = previous;
  });
  let requests = 0;
  let marketAge = 7200000;
  let value = 987654321;
  const oldRequest = https.request;
  https.request = (options, onResponse) => {
    assert.equal(options.hostname, price.HOST);
    const req = new EventEmitter();
    req.destroy = error => { if (error) req.emit('error', error); };
    req.end = () => queueMicrotask(() => {
      requests += 1;
      const res = new EventEmitter();
      res.statusCode = 200;
      res.headers = {};
      res.resume = () => {};
      onResponse(res);
      const now = Date.now();
      const source = { fetchedAt: new Date(now - 1000).toISOString(),
        capturedFrom: new Date(now - marketAge - 1000).toISOString(),
        capturedTo: new Date(now - marketAge).toISOString() };
      const body = options.path === price.PATHS.market
        ? { schemaVersion: 2, prices: { 123456: value }, source }
        : { schemaVersion: 3, kind: 'itemLocalization', items: { 123456: '测试量子芯' } };
      res.emit('data', Buffer.from(JSON.stringify(body)));
      res.emit('end');
    });
    return req;
  };
  t.after(() => { https.request = oldRequest; });

  const ask = (text, key, options={}) => botEnv.bot.handle({
    platform: 'wechat', chatId: 'group_price_fixture', chatTitle: '别野', chatType: 'group',
    senderId: 'tester', senderName: '测试群友', text, atMe: true, msgId: key, ...options,
  }, { localWechat: true, stageAssistant() { assert.fail('a price snapshot must not enter assistant memory'); } });

  price.forget();
  const stale = await ask('三角洲 测试量子芯现在多少钱', 'test-old-price');
  assert.equal(stale.reason, 'dsf_price_direct');
  assert.match(stale.reply, /过期/);
  assert.doesNotMatch(stale.reply, /987654321/);
  assert.match(stale.reply, /dfttk\.com/);
  assert.equal(botEnv.getCallCount(), 0);
  assert.equal(requests, 2);

  botEnv.advanceMs(60000);
  marketAge = 10000;
  price.forget();
  const current = await ask('三角洲 123456 交易行价格', 'test-valid-price');
  assert.equal(current.reason, 'dsf_price_direct');
  assert.match(current.reply, /987654321/);
  assert.match(current.reply, /单件更新时间未知/);
  assert.match(current.reply, /非实时/);
  assert.deepEqual(current.bubbles, [current.reply]);
  assert.equal(botEnv.getCallCount(), 0);

  const previousCap = botEnv.bot.config.group.maxRepliesPerMinute;
  botEnv.bot.config.group.maxRepliesPerMinute = 1;
  const beforeLimited = requests;
  const blocked = await ask('三角洲 测试量子芯现在多少钱', 'test-burst-price');
  assert.equal(blocked.reason, 'burst_limit');
  assert.equal(requests, beforeLimited);
  assert.equal(botEnv.getCallCount(), 0);
  botEnv.bot.config.group.maxRepliesPerMinute = previousCap;

  botEnv.advanceMs(60000);
  value = Number.MAX_SAFE_INTEGER + 3;
  price.forget();
  const malformed = await ask('三角洲 测试量子芯价格', 'test-bad-price');
  assert.equal(malformed.reason, 'dsf_price_direct');
  assert.match(malformed.reply, /暂不可用/);
  assert.doesNotMatch(malformed.reply, /9007199254740994/);
  assert.equal(botEnv.getCallCount(), 0);

  botEnv.advanceMs(60000);
  price.forget();
  const beforeCasual = requests;
  const regular = await botEnv.bot.handle({
    platform: 'wechat', chatId: 'group_price_fixture', chatTitle: '别野', chatType: 'group',
    senderId: 'tester', senderName: '测试群友', text: '三角洲这张地图气氛挺好', atMe: true, msgId: 'chat-only',
  });
  assert.match(regular.reply, /普通聊天仍调用模型/);
  assert.equal(requests, beforeCasual);
  assert.equal(botEnv.getCallCount(), 1);

  botEnv.advanceMs(60000);
  value = 987654321;
  price.forget();
  const mixed = await botEnv.bot.handle({
    platform: 'wechat', chatId: 'group_price_fixture', chatTitle: '别野', chatType: 'group',
    senderId: 'tester', senderName: '测试群友', text: '三角洲 测试量子芯多少钱，还怎么获得',
    atMe: true, msgId: 'chat-mixed-price',
  });
  assert.notEqual(mixed.reason, 'dsf_price_direct');
  assert.equal(botEnv.getCallCount(), 2);

  const limited = { isGroup: true, platform: 'wechat', scope: 'standalone-budget-fixture',
    allowDirectReply: true, perChatDaily: 1 };
  assert.match((await price.lookupFor('三角洲 123456 价格', limited)).reply, /987654321/);
  assert.match((await price.lookupFor('三角洲 123456 价格', limited)).reply, /额度/);
});
