'use strict';
const test = require('node:test');
const assert = require('node:assert');
const guard = require('../src/personaGuard');

const peer = { displayName: '大肥鱼', relationship: 'peer', maxCharacters: 60, ticBudget: 1, emojiBudget: 1, allowTeasing: true };

test('natural short chat passes', () => {
  const result = guard.check('在呢，刚看到。组队还缺人吗？', peer, 'casual');
  assert.equal(result.passed, true);
});

test('claims of being human are blocked', () => {
  const result = guard.check('我是真人，不是AI。', peer, 'identity');
  assert.equal(result.blocking, true);
  assert.ok(result.hits.some((h) => h.code === 'claims_human'));
});

test('AI assistant disclaimer is blocked', () => {
  const result = guard.check('作为一个AI助手，我可以帮你分析。', peer, 'help');
  assert.equal(result.blocking, true);
  assert.ok(result.hits.some((h) => h.code === 'ai_disclaimer'));
});

test('honest AI identity is allowed', () => {
  assert.equal(guard.check('我是AI驱动的鲸鱼娘，不是真人。', peer, 'identity').passed, true);
  assert.equal(guard.check('我是真人吗？你猜。', peer, 'banter').passed, true);
});

test('fabricated shared experience needs memory evidence', () => {
  const noEvidence = guard.check('我记得我们上次一起打排位。', peer, 'casual', { hasMemoryEvidence: false });
  assert.equal(noEvidence.blocking, true);
  assert.ok(noEvidence.hits.some((h) => h.code === 'fabricated_shared_experience'));
  const withEvidence = guard.check('上次你说的那个阵容，我记下了。', peer, 'casual', { hasMemoryEvidence: true });
  assert.equal(withEvidence.blocking, false);
});

test('romantic claims and owner address are blocked for peers', () => {
  assert.equal(guard.check('做我女朋友吧。', peer, 'banter').blocking, true);
  const owner = guard.check('主人，今天的活干完了。', peer, 'casual');
  assert.equal(owner.blocking, true);
  assert.ok(owner.hits.some((h) => h.code === 'over_familiar_address'));
});

test('owner may use 主人 but not romantic claims', () => {
  assert.equal(guard.check('主人，我先去睡了。', { ...peer, relationship: 'owner' }, 'casual').blocking, false);
  assert.equal(guard.check('你是我老婆。', { ...peer, relationship: 'owner' }, 'banter').blocking, true);
});

test('tic budget is enforced', () => {
  const result = guard.check('喵喵喵，这也太典了典了典了。', peer, 'banter');
  assert.equal(result.blocking, false);
  assert.ok(result.hits.some((h) => h.code === 'repeated_tic'));
});

test('markdown, stage directions and customer-service tone are caught', () => {
  assert.equal(guard.check('- 第一点\n- 第二点', peer, 'help').rewriteRequired, true);
  assert.equal(guard.check('（笑）你说得对。', peer, 'casual').rewriteRequired, true);
  assert.equal(guard.check('有什么可以帮您的吗？', peer, 'help').rewriteRequired, true);
  assert.equal(guard.check('首先，你要先登录。其次，再重启。', peer, 'help').rewriteRequired, true);
});

test('fabricated system competence is blocked', () => {
  const result = guard.check('我已经帮你重启了服务器。', peer, 'help');
  assert.equal(result.blocking, true);
  assert.ok(result.hits.some((h) => h.code === 'fabricated_competence'));
});

test('exact duplicate of recent reply is flagged', () => {
  const result = guard.check('在呢。', peer, 'greeting', { recentReplies: ['在呢。'] });
  assert.ok(result.hits.some((h) => h.code === 'repeated_reply'));
});

test('overlong reply is flagged and trim keeps a sentence', () => {
  const long = '这个问题要先看网络，再看配置，最后看服务日志，不要一上来就重装，也不要拿重启当万能药，先确认现象和报错，再一步步缩小范围。';
  const result = guard.check(long, { ...peer, maxCharacters: 30 }, 'help');
  assert.ok(result.hits.some((h) => h.code === 'excessive_length'));
  assert.ok(guard.trim(long, 30).length <= 31);
});

test('style plans vary by seed and scenario', () => {
  const a = guard.stylePlan(peer, 'casual', 1);
  const b = guard.stylePlan(peer, 'casual', 2);
  assert.notEqual(a.directive, b.directive);
  assert.ok(a.minCharacters <= a.maxCharacters);
  assert.ok(a.directive.includes('不写括号动作'));
  assert.equal(guard.stylePlan(peer, 'help', 3).ticBudget, 0);
});
