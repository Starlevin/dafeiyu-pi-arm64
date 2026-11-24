'use strict';

/**
 * 群长期记忆隔离的集成回归（真实 Gateway → Bot → Store 路径）。
 *
 * 场景：两个群、同一个 senderId、同一个平台 msgId，全部落在临时数据目录。
 * 这里只断言「公开行为」：网关去重、人物卡/摘要/检索、磁盘目录、冷重建。
 * Store 模块级的正向隔离由 memoryIsolation.test.js 覆盖，本文件补它测不到的层：
 *   - Bot 的 sessionKey 是否把群 id 带进去（同 sender/同 msgId 不能跨群去重）；
 *   - 压缩后的摘要是否还在本群；
 *   - 冷重建后是否仍然隔离；
 *   - 跨群写入是否直接被 MEMORY_CHAT_MISMATCH 拒绝（隔离红线）。
 *
 * 全程离线：llm 是本地 stub，Gateway 没有 outboundUrl，不读写真实微信数据/凭据。
 */

const assert = require('node:assert/strict');
const fs = require('node:fs');
const { EventEmitter } = require('node:events');
const registerBridge = require('../src/http/routes/bridge');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { Gateway } = require('../src/adapters/gateway');
const { Bot } = require('../src/pipeline/chat');
const { Store } = require('../src/memory/store');
const { chatHash } = require('../src/memory/hash');
const personCard = require('../src/memory/personCard');
const relations = require('../src/memory/relations');
const { loadConfig } = require('../src/infra/config');

const SENDER_ID = 'fixture-same-sender';
// 平台消息号相同，且是合法 trace id，覆盖带 MessageTiming 的本地桥接路径。
const MSG_ID = '11111111-1111-4111-8111-111111111111';
const GROUP_A = 'fixture-group-alpha';
const GROUP_B = 'fixture-group-beta';
const KEY_A = `wechat:g:${GROUP_A}`;
const KEY_B = `wechat:g:${GROUP_B}`;

function tempStore(t, { limits = {} } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dafeiyu-memory-isolation-'));
  const dir = path.join(root, 'store');
  const store = new Store({ dir, limits });
  t.after(() => {
    try { store.close(); } catch { /* already closed */ }
    fs.rmSync(root, { recursive: true, force: true, maxRetries: 3, retryDelay: 40 });
  });
  return { root, dir, store };
}

function gatewayFixture(t, { owner, moments, complete } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dafeiyu-memory-isolation-'));
  const config = loadConfig({ root, env: {}, readOnly: true });
  config.websearch = { enabled: false };
  config.group.proactiveJudge = { enabled: false, groups: [] };
  if (owner) config.persona.master = owner;
  const logger = { info() {}, debug() {}, warn() {}, error() {} };
  const dir = path.join(root, 'store');
  const store = new Store({ dir });
  const llm = { isMock: false, model: 'fixture-model', complete: complete || (async () => '隔离回归回复。') };
  const bot = new Bot({ config, store, llm, logger, moments });
  const gateway = new Gateway({ platform: 'wechat', bot, logger });
  t.after(() => {
    try { store.close(); } catch { /* already closed */ }
    fs.rmSync(root, { recursive: true, force: true, maxRetries: 3, retryDelay: 40 });
  });
  return { dir, store, gateway };
}

function inbound(groupId, text) {
  return {
    chat_id: groupId,
    chat_title: groupId,
    chat_type: 'group',
    sender_id: SENDER_ID,
    sender_name: '同一测试用户',
    at_me: true,
    text,
    msg_id: MSG_ID,
  };
}

function oneMessageWithId(store, key) {
  return store.sessions.get(key).messages.filter((m) => m.msgId === MSG_ID);
}

function assertGroupOnly(store, key, ownNeedle, foreignNeedle) {
  const messages = store.sessions.get(key).messages;
  assert.equal(messages.some((m) => String(m.text).includes(ownNeedle)), true, `${key} 应保留本群消息`);
  assert.equal(messages.some((m) => String(m.text).includes(foreignNeedle)), false, `${key} 不应出现别群消息`);
}

test('verified accounts isolate the same group, private chat, message ID and old unbound history', async (t) => {
  const accountA = 'a'.repeat(64), accountB = 'b'.repeat(64);
  const prompts = [];
  const { dir, store, gateway } = gatewayFixture(t, {
    complete: async ({ turns }) => {
      prompts.push(JSON.stringify(turns));
      return '收到这次隔离回归的消息。';
    },
  });
  store.ensureSession(KEY_A, { platform: 'wechat', chatType: 'group' });
  store.appendMessage(KEY_A, { role: 'user', uid: SENDER_ID, text: '无主旧记录夹具' });
  gateway.bot.config.wechat = { memoryAccountBinding: accountA };
  const base = inbound(GROUP_A, '甲号群记录夹具');
  const untrusted = await gateway.handleInbound({ ...base, memory_account: accountA }, { local: true });
  assert.equal(untrusted.reason, 'memory_account_unverified');
  const verify = { local: true, trustedLocal: true };
  for (const [account, type, text] of [[accountA, 'group', '甲号群记录夹具'],
                                       [accountB, 'group', '乙号群记录夹具'],
                                       [accountA, 'private', '甲号私聊记录夹具']]) {
    const reply = await gateway.handleInbound(
      { ...base, memory_account: account, chat_type: type, text }, verify);
    assert.equal(reply.action, 'reply');
  }
  assert.equal(prompts.length, 3);
  assert.ok(prompts.every(p => !p.includes('无主旧记录夹具')));
  assert.ok(!prompts[1].includes('甲号群记录夹具') && !prompts[2].includes('甲号群记录夹具'));
  store.close();
  const cold = new Store({ dir });
  t.after(() => cold.close());
  const scopeA = `wechat:a:${accountA}:g:${GROUP_A}`;
  const scopeB = `wechat:a:${accountB}:g:${GROUP_A}`;
  const privateA = `wechat:a:${accountA}:u:${GROUP_A}`;
  assertGroupOnly(cold, scopeA, '甲号群记录夹具', '乙号群记录夹具');
  assertGroupOnly(cold, scopeB, '乙号群记录夹具', '甲号群记录夹具');
  assertGroupOnly(cold, privateA, '甲号私聊记录夹具', '甲号群记录夹具');
  assert.equal(cold.isDuplicate(scopeA, MSG_ID), true);
  assert.equal(cold.isDuplicate(scopeB, MSG_ID), true);
  assert.equal(cold.messages(KEY_A, 2)[0].text, '无主旧记录夹具');
});

test('managed WeChat stores assistant text only after the signed, matching UI receipt', async (t) => {
  const account = 'a'.repeat(64);
  const token = 'local-fixture-secret';
  const { dir, store, gateway } = gatewayFixture(t);
  gateway.options.inboundToken = token;
  gateway.bot.config.wechat = { memoryAccountBinding: account };
  const send = (group) => gateway.handleInbound(
    { ...inbound(group, '这是一条入站测试消息'), memory_account: account },
    { local: true, trustedLocal: true },
  );
  const accepted = await send(GROUP_A);
  const abandoned = await send(GROUP_B);
  assert.ok(accepted.compatMemoryReceipt);
  assert.ok(abandoned.compatMemoryReceipt);
  assert.notEqual(accepted.compatMemoryReceipt, abandoned.compatMemoryReceipt);
  const scopeA = `wechat:a:${account}:g:${GROUP_A}`;
  const scopeB = `wechat:a:${account}:g:${GROUP_B}`;
  const roles = (db, scope) => db.messages(scope, 20).map((row) => row.role);
  assert.deepEqual(roles(store, scopeA), ['user']);
  assert.deepEqual(roles(store, scopeB), ['user']);

  const routes = new Map();
  registerBridge({ post: (path, handler) => routes.set(path, handler), pattern() {} });
  const ack = routes.get('/api/bridge/memory/confirmed');
  async function request(body, auth = token, address = '127.0.0.1') {
    const req = Object.assign(new EventEmitter(), {
      socket: { remoteAddress: address },
      headers: { 'x-whale-bridge': '1', 'x-bot-token': auth },
    });
    let status, response;
    const res = {
      writeHead(value) { status = value; },
      end(value) { response = JSON.parse(value); },
    };
    const run = ack({ gateways: new Map([['wechat', gateway]]) }, req, res);
    req.emit('data', Buffer.from(JSON.stringify(body)));
    req.emit('end');
    await run;
    return [status, response];
  }
  const receipt = { receipt: accepted.compatMemoryReceipt, memory_account: account, trace: MSG_ID };
  assert.equal((await request(receipt, 'bad-token'))[0], 403);
  assert.equal((await request(receipt, token, '192.0.2.15'))[0], 403);
  assert.deepEqual(await request({ ...receipt, memory_account: 'b'.repeat(64) }), [200, { ok: true, confirmed: false }]);
  assert.deepEqual(await request({ ...receipt, trace: 'missing' }), [200, { ok: true, confirmed: false }]);
  assert.deepEqual(roles(store, scopeA), ['user']);
  const passed = [200, { ok: true, confirmed: true }];
  assert.deepEqual(await request(receipt), passed);
  assert.deepEqual(await request(receipt), passed);
  assert.deepEqual(roles(store, scopeA), ['user', 'assistant']);

  store.close();
  const cold = new Store({ dir });
  t.after(() => cold.close());
  const restarted = new Gateway({ platform: 'wechat', bot: { store: cold } });
  assert.equal(restarted.confirmMemory({ ...receipt, receipt: abandoned.compatMemoryReceipt }), false);
  assert.deepEqual(roles(cold, scopeA), ['user', 'assistant']);
  assert.deepEqual(roles(cold, scopeB), ['user']);
});

test('a reply derived from an owner Moments snapshot never enters cold chat memory', async (t) => {
  const ownerId = 'wxid_fixtureowner';
  const key = `wechat:u:${ownerId}`;
  const snapshot = '社交快照专用样本字段';
  const externalReply = '我看到了合成发布活动的摘要';
  const normalReply = '今天是普通对话回归';
  const prompts = [];
  const { dir, store, gateway } = gatewayFixture(t, {
    owner: { names: ['测试主人'], wxids: [ownerId] },
    moments: { contextBlock: () => snapshot },
    complete: async ({ turns }) => {
      const prompt = JSON.stringify(turns);
      prompts.push(prompt);
      return prompt.includes(snapshot) ? externalReply : normalReply;
    },
  });
  const request = {
    ...inbound(ownerId, '请查看朋友圈最近有什么动态'),
    chat_type: 'private',
    sender_id: ownerId,
    sender_name: '测试主人',
  };
  const reply = await gateway.handleInbound(request, { local: true });
  assert.equal(reply.reply, externalReply);
  assert.ok(prompts[0].includes(snapshot));
  assert.equal(store.isDuplicate(key, MSG_ID), true);
  assert.deepEqual(store.messages(key, 20).map((row) => row.role), ['user']);

  const ordinary = await gateway.handleInbound({
    ...request,
    text: '我们换个话题聊今天的事',
    msg_id: '22222222-2222-4222-8222-222222222222',
  }, { local: true });
  assert.equal(ordinary.reply, normalReply);
  await gateway.handleInbound({
    ...inbound(GROUP_A, '也请看看朋友圈最近的动态'),
    sender_id: ownerId,
    sender_name: '测试主人',
  }, { local: true });
  assert.ok(prompts.slice(1).every((prompt) => !prompt.includes(snapshot) && !prompt.includes(externalReply)));

  store.close();
  const cold = new Store({ dir });
  t.after(() => cold.close());
  const journal = fs.readFileSync(path.join(dir, 'chats', chatHash(key), 'messages.jsonl'), 'utf8');
  assert.ok(journal.includes(normalReply));
  assert.ok(!journal.includes(snapshot) && !journal.includes(externalReply));
  const recent = cold.buildContext({ key, uid: ownerId, query: '朋友圈' }).recent;
  assert.deepEqual(recent.filter((row) => row.role === 'assistant').map((row) => row.text), [normalReply]);
  assert.equal(cold.isDuplicate(key, MSG_ID), true);
});

test('Moments reads require a verified owner in a private WeChat chat', async (t) => {
  let accesses = 0;
  const { gateway } = gatewayFixture(t, {
    owner: { names: ['同一测试用户'], wxids: [] },
    moments: {
      ensureFresh: async () => { accesses += 1; },
      contextBlock: () => { accesses += 1; return 'fixture snapshot'; },
    },
  });
  const request = inbound(SENDER_ID, '请查看朋友圈最近有什么动态');
  const privateReply = await gateway.handleInbound({ ...request, chat_type: 'private' }, { local: true });
  assert.equal(privateReply.action, 'reply');
  const groupReply = await gateway.handleInbound(request, { local: true });
  assert.equal(groupReply.action, 'reply');
  assert.equal(accesses, 0);
});

test('same sender and platform message id are deduplicated per group and stay isolated after cold rebuild', async (t) => {
  const { dir, store, gateway } = gatewayFixture(t);

  const alpha = await gateway.handleInbound(inbound(GROUP_A, 'alpha 群独有的记忆'), { local: true });
  assert.equal(alpha.action, 'reply');
  // 同一条 msgId 到了另一个群：必须还能收，不能被 A 群的 seenIds 挡掉。
  assert.equal(store.isDuplicate(KEY_B, MSG_ID), false);
  const beta = await gateway.handleInbound(inbound(GROUP_B, 'beta 群独有的记忆'), { local: true });
  assert.equal(beta.action, 'reply');

  assert.deepEqual([...store.sessions.keys()].sort(), [KEY_A, KEY_B].sort());
  assert.equal(store.isDuplicate(KEY_A, MSG_ID), true);
  assert.equal(store.isDuplicate(KEY_B, MSG_ID), true);
  assert.equal(oneMessageWithId(store, KEY_A).length, 1);
  assert.equal(oneMessageWithId(store, KEY_B).length, 1);

  // 各自重投一次：都只按本群去重，另一群的消息数不受影响。
  const replayA = await gateway.handleInbound(inbound(GROUP_A, 'alpha 群独有的记忆'), { local: true });
  const replayB = await gateway.handleInbound(inbound(GROUP_B, 'beta 群独有的记忆'), { local: true });
  assert.equal(replayA.action, 'ignore');
  assert.equal(replayA.reason, 'duplicate');
  assert.equal(replayB.action, 'ignore');
  assert.equal(replayB.reason, 'duplicate');
  assert.equal(oneMessageWithId(store, KEY_A).length, 1);
  assert.equal(oneMessageWithId(store, KEY_B).length, 1);
  assertGroupOnly(store, KEY_A, 'alpha 群独有', 'beta 群独有');
  assertGroupOnly(store, KEY_B, 'beta 群独有', 'alpha 群独有');

  // 人物卡 / 摘要 / 检索都按群各存一份。
  store.addNote(KEY_A, SENDER_ID, 'alpha 群专属偏好');
  store.addNote(KEY_B, SENDER_ID, 'beta 群专属偏好');
  store.setSummary(KEY_A, 'alpha 群摘要');
  store.setSummary(KEY_B, 'beta 群摘要');
  const chatA = store.chatStore.ensureChat(KEY_A);
  const chatB = store.chatStore.ensureChat(KEY_B);
  const cardA = store.chatStore.getCard(chatA, SENDER_ID);
  const cardB = store.chatStore.getCard(chatB, SENDER_ID);
  assert.ok(cardA && cardB);
  for (const [chat, card, marker] of [[chatA, cardA, 'alpha'], [chatB, cardB, 'beta']]) {
    personCard.addEvent(card, {
      id: `event-${marker}`,
      date: '2026-11-21',
      title: `${marker} 专属事件`,
      summary: `${marker} 专属事件详情`,
      keywords: [`${marker}-event`],
      participants: [SENDER_ID],
      importance: 1,
      confidence: 1,
      source: { chatId: chat.chatId, messageIds: [MSG_ID] },
    }, { chatId: chat.chatId, now: Date.UTC(2026, 10, 21) });
    store.chatStore.saveCard(chat, card);
  }

  const now = Date.UTC(2026, 10, 21);
  const ctxA = store.buildContext({ key: KEY_A, uid: SENDER_ID, query: 'alpha-event', now });
  const ctxB = store.buildContext({ key: KEY_B, uid: SENDER_ID, query: 'beta-event', now });
  assert.equal(ctxA.summary, 'alpha 群摘要');
  assert.equal(ctxB.summary, 'beta 群摘要');
  assert.deepEqual(store.listNotes(KEY_A).find((row) => row.uid === SENDER_ID).notes, ['alpha 群专属偏好']);
  assert.deepEqual(store.listNotes(KEY_B).find((row) => row.uid === SENDER_ID).notes, ['beta 群专属偏好']);
  assert.equal(ctxA.events.some((e) => e.title === 'alpha 专属事件'), true);
  assert.equal(ctxA.events.some((e) => e.title === 'beta 专属事件'), false);
  assert.equal(ctxB.events.some((e) => e.title === 'beta 专属事件'), true);
  assert.equal(ctxB.events.some((e) => e.title === 'alpha 专属事件'), false);
  assert.equal(ctxA.recent.some((m) => String(m.text).includes('beta 群独有')), false);
  assert.equal(ctxB.recent.some((m) => String(m.text).includes('alpha 群独有')), false);

  // 物理隔离：两个 chat 目录，原文各写各的文件。
  const chatDirs = fs.readdirSync(path.join(dir, 'chats')).filter((n) => /^[0-9a-f]{16}$/.test(n));
  assert.equal(chatDirs.length, 2);
  for (const [key, ownNeedle, foreignNeedle] of [[KEY_A, 'alpha 群独有', 'beta 群独有'], [KEY_B, 'beta 群独有', 'alpha 群独有']]) {
    const body = fs.readFileSync(path.join(dir, 'chats', chatHash(key), 'messages.jsonl'), 'utf8');
    assert.equal(body.includes(ownNeedle), true);
    assert.equal(body.includes(foreignNeedle), false);
  }

  // 冷重建：新 Store 从磁盘恢复后，去重、卡片、摘要、检索仍互不串群。
  store.close();
  const reloaded = new Store({ dir });
  t.after(() => reloaded.close());
  assert.equal(reloaded.isDuplicate(KEY_A, MSG_ID), true);
  assert.equal(reloaded.isDuplicate(KEY_B, MSG_ID), true);
  assert.equal(reloaded.isDuplicate('wechat:g:fixture-group-gamma', MSG_ID), false);
  assert.equal(oneMessageWithId(reloaded, KEY_A).length, 1);
  assert.equal(oneMessageWithId(reloaded, KEY_B).length, 1);
  assert.deepEqual(reloaded.listNotes(KEY_A).find((row) => row.uid === SENDER_ID).notes, ['alpha 群专属偏好']);
  assert.deepEqual(reloaded.listNotes(KEY_B).find((row) => row.uid === SENDER_ID).notes, ['beta 群专属偏好']);
  const reloadedA = reloaded.buildContext({ key: KEY_A, uid: SENDER_ID, query: 'alpha-event', now });
  const reloadedB = reloaded.buildContext({ key: KEY_B, uid: SENDER_ID, query: 'beta-event', now });
  assert.equal(reloadedA.summary, 'alpha 群摘要');
  assert.equal(reloadedB.summary, 'beta 群摘要');
  assert.equal(reloadedA.events.some((e) => e.title === 'alpha 专属事件'), true);
  assert.equal(reloadedA.events.some((e) => e.title === 'beta 专属事件'), false);
  assert.equal(reloadedB.events.some((e) => e.title === 'beta 专属事件'), true);
  assert.equal(reloadedB.events.some((e) => e.title === 'alpha 专属事件'), false);
  assertGroupOnly(reloaded, KEY_A, 'alpha 群独有', 'beta 群独有');
  assertGroupOnly(reloaded, KEY_B, 'beta 群独有', 'alpha 群独有');
  reloaded.close();
});

test('compressed summaries stay inside their own group and survive cold rebuild', (t) => {
  // 1,000 预算：recent 分片约 581 token，几条长消息就会触发同步压缩。
  const { dir, store } = tempStore(t, { limits: { dialogueBudget: 1000 } });
  const uid = SENDER_ID;
  for (let i = 0; i < 4; i += 1) {
    store.appendMessage(KEY_A, { role: 'user', uid, name: '同一测试用户', msgId: `alpha-${i}`, text: `ALPHA记忆标记${i}`.repeat(80) });
    store.appendMessage(KEY_B, { role: 'user', uid, name: '同一测试用户', msgId: `beta-${i}`, text: `BETA记忆标记${i}`.repeat(80) });
  }

  const now = Date.UTC(2026, 10, 21);
  const ctxA = store.buildContext({ key: KEY_A, uid, query: 'ALPHA记忆标记', now });
  const ctxB = store.buildContext({ key: KEY_B, uid, query: 'BETA记忆标记', now });
  assert.ok(ctxA.compressed.chunks >= 1, 'alpha 群应触发压缩');
  assert.ok(ctxB.compressed.chunks >= 1, 'beta 群应触发压缩');
  assert.equal(ctxA.summary.includes('ALPHA'), true);
  assert.equal(ctxA.summary.includes('BETA'), false);
  assert.equal(ctxB.summary.includes('BETA'), true);
  assert.equal(ctxB.summary.includes('ALPHA'), false);
  assert.equal(ctxA.recent.some((m) => String(m.text).includes('BETA')), false);
  assert.equal(ctxB.recent.some((m) => String(m.text).includes('ALPHA')), false);

  store.close();
  const reloaded = new Store({ dir, limits: { dialogueBudget: 1000 } });
  t.after(() => reloaded.close());
  assert.equal(reloaded.sessions.get(KEY_A).summary.includes('ALPHA'), true);
  assert.equal(reloaded.sessions.get(KEY_A).summary.includes('BETA'), false);
  assert.equal(reloaded.sessions.get(KEY_B).summary.includes('BETA'), true);
  assert.equal(reloaded.sessions.get(KEY_B).summary.includes('ALPHA'), false);
  assert.equal(reloaded.isDuplicate(KEY_A, 'alpha-1'), true);
  assert.equal(reloaded.isDuplicate(KEY_B, 'alpha-1'), false);
  assert.equal(reloaded.isDuplicate(KEY_B, 'beta-1'), true);
  reloaded.close();
});

test('cross-group memory writes are rejected instead of merged', (t) => {
  const { store } = tempStore(t);
  const chatA = store.chatStore.ensureChat(KEY_A);
  const chatB = store.chatStore.ensureChat(KEY_B);
  const cardA = store.chatStore.ensurePerson(chatA, SENDER_ID, '同一测试用户');
  const cardB = store.chatStore.ensurePerson(chatB, SENDER_ID, '同一测试用户');
  const isMismatch = (error) => error && error.code === 'MEMORY_CHAT_MISMATCH';
  const now = Date.UTC(2026, 10, 21);

  // 卡属于 A 群，写入时却声称来自 B 群：任何入口都必须拒绝。
  assert.throws(() => personCard.addPreference(cardA, { text: 'beta 偏好' }, { chatId: KEY_B }), isMismatch);
  assert.throws(() => personCard.addTrait(cardA, { text: 'beta 性格', confidence: 1, explicit: true }, { chatId: KEY_B }), isMismatch);
  assert.throws(() => personCard.addThread(cardA, { text: 'beta 话题' }, { chatId: KEY_B }), isMismatch);
  assert.throws(() => personCard.addEvent(cardA, {
    id: 'beta-event', date: '2026-11-21', title: 'beta 事件', source: { chatId: KEY_B, messageIds: [MSG_ID] },
  }, { chatId: KEY_B }), isMismatch);
  // chatId 对得上、但事件 source 声称来自别群：同样拒绝。
  assert.throws(() => personCard.addEvent(cardA, {
    id: 'beta-event', date: '2026-11-21', title: 'beta 事件', source: { chatId: KEY_B, messageIds: [MSG_ID] },
  }, { chatId: KEY_A }), isMismatch);
  // 关系边走同一套红线：要写进 A 群，来源却声称是 B 群。
  assert.throws(() => relations.upsert(chatA.meta.relations, {
    a: SENDER_ID, b: 'fixture-other', text: '同事', source: { chatId: KEY_B, messageIds: [MSG_ID] },
  }, { chatId: KEY_A }), isMismatch);

  // 被拒绝的写入不能留下半条数据。
  assert.equal(cardA.preferences.length, 0);
  assert.equal(cardA.traits.length, 0);
  assert.equal(cardA.threads.length, 0);
  assert.equal(cardA.events.length, 0);
  assert.equal(chatA.meta.relations.length, 0);
  assert.equal(cardB.preferences.length, 0);
  assert.equal(cardB.events.length, 0);

  store.close();
  const reloaded = new Store({ dir: store.memoryDir });
  t.after(() => reloaded.close());
  const reloadedChatA = reloaded.chatStore.ensureChat(KEY_A);
  assert.equal(reloadedChatA.meta.relations.length, 0);
  assert.equal(reloaded.chatStore.getCard(reloadedChatA, SENDER_ID).preferences.length, 0);
  reloaded.close();
});
