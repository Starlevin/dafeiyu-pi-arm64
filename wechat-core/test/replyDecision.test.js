'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { decide, recordSpeak, dayKeyFor, heuristicScore, normalizeText } = require('../src/replyDecision');

const base = {
  scope: 'group', chatAlias: 'group:A', window: ['今晚组队吗？', '缺一个'], mention: 'none',
  unansweredQuestion: true, questionToAll: true, invited: false, addressedToOther: false,
  botSpokeRecently: false, secondsSinceBotReply: 86400, secondsSinceTrigger: 0,
  repeatedTopic: false, interruptCost: 0.2, topicRelevance: 0.8, isOwner: false,
  quietHours: false, dailyRemaining: 30,
};
const emptyState = { dayKey: dayKeyFor(1_700_000_000), decisions: 0, speaks: 0, scorerCalls: 0, lastSpeakAt: 0, lastDecisionAt: 0, recentSpeakTexts: [] };

test('direct mention always speaks, never calls scorer', async () => {
  let calls = 0;
  const scorer = async () => { calls += 1; return 0.1; };
  const { result } = await decide({ ...base, mention: 'self', window: [] }, emptyState, 1_700_000_000, scorer);
  assert.equal(result.action, 'SPEAK');
  assert.equal(result.reason, 'must_answer');
  assert.equal(result.score, 1);
  assert.equal(result.source, 'must_answer');
  assert.equal(calls, 0);
});

test('private message always speaks', async () => {
  const { result } = await decide({ ...base, scope: 'private', chatAlias: 'private:X', quietHours: true, dailyRemaining: 0 }, emptyState, 1_700_000_000, null);
  assert.equal(result.action, 'SPEAK');
  assert.equal(result.reason, 'must_answer');
});

test('quiet hours and exhausted budget skip', async () => {
  const { result: quiet } = await decide({ ...base, quietHours: true }, emptyState, 1_700_000_000, null);
  assert.equal(quiet.reason, 'quiet_hours');
  const { result: budget } = await decide({ ...base, dailyRemaining: 0 }, emptyState, 1_700_000_000, null);
  assert.equal(budget.reason, 'daily_budget');
});

test('addressed to another member is never hijacked', async () => {
  const { result } = await decide({ ...base, addressedToOther: true, questionToAll: false }, emptyState, 1_700_000_000, null);
  assert.equal(result.action, 'SKIP');
  assert.equal(result.reason, 'addressed_to_other');
});

test('recent bot reply suppresses ambient chat without a pending question', async () => {
  const { result } = await decide({ ...base, unansweredQuestion: false, botSpokeRecently: true, secondsSinceBotReply: 30 }, emptyState, 1_700_000_000, null);
  assert.equal(result.reason, 'recently_spoke');
});

test('high interrupt cost skips before any scoring', async () => {
  let calls = 0;
  const scorer = async () => { calls += 1; return 0.99; };
  const { result } = await decide({ ...base, interruptCost: 0.8 }, emptyState, 1_700_000_000, scorer);
  assert.equal(result.reason, 'high_interrupt_cost');
  assert.equal(calls, 0);
});

test('low heuristic skips without model cost', async () => {
  let calls = 0;
  const scorer = async () => { calls += 1; return 0.99; };
  const low = { ...base, unansweredQuestion: false, questionToAll: false, topicRelevance: 0, interruptCost: 0.6, window: ['哈哈'] };
  const { result } = await decide(low, emptyState, 1_700_000_000, scorer);
  assert.equal(result.action, 'SKIP');
  assert.equal(result.reason, 'heuristic_low');
  assert.equal(calls, 0);
});

test('scorer failure fails closed', async () => {
  const { result } = await decide(base, emptyState, 1_700_000_000, async () => { throw new Error('timeout'); });
  assert.equal(result.action, 'SKIP');
  assert.equal(result.reason, 'scorer_unavailable');
  assert.equal(result.source, 'fail_closed');
});

test('out-of-range scorer value fails closed', async () => {
  const { result } = await decide(base, emptyState, 1_700_000_000, async () => 1.7);
  assert.equal(result.reason, 'scorer_unavailable');
});

test('scorer above/below threshold speaks or waits', async () => {
  const { result: pass } = await decide(base, emptyState, 1_700_000_000, async () => 0.82);
  assert.equal(pass.action, 'SPEAK');
  assert.equal(pass.reason, 'model_pass');
  const { result: hold } = await decide(base, emptyState, 1_700_000_000, async () => 0.61);
  assert.equal(hold.action, 'WAIT');
  assert.equal(hold.reason, 'below_threshold');
  assert.equal(hold.ttlSeconds, 45);
});

test('heuristic-only mode passes at or above threshold', async () => {
  const { result } = await decide({ ...base, topicRelevance: 1, unansweredQuestion: true, questionToAll: true, interruptCost: 0 }, emptyState, 1_700_000_000, null);
  assert.equal(result.action, 'SPEAK');
  assert.equal(result.reason, 'heuristic_pass');
});

test('daily state cap is enforced', async () => {
  const state = { ...emptyState, speaks: 30 };
  const { result } = await decide(base, state, 1_700_000_000, null, { dailyLimit: 30 });
  assert.equal(result.reason, 'daily_budget');
});

test('recordSpeak counts only confirmed sends and dedupes exact repeats', () => {
  let state = { ...emptyState };
  state = recordSpeak(state, '在呢。', 1_700_000_001);
  assert.equal(state.speaks, 1);
  state = recordSpeak(state, ' 在呢! ', 1_700_000_002);
  assert.equal(state.speaks, 2);
  assert.equal(state.recentSpeakTexts.length, 1);
  assert.equal(state.recentSpeakTexts[0], normalizeText('在呢。'));
});

test('day roll resets counters', () => {
  const state = { dayKey: dayKeyFor(1_700_000_000), speaks: 12, decisions: 40 };
  const next = require('../src/replyDecision').normalizeState(state, 1_700_100_000);
  assert.equal(next.speaks, 0);
  assert.equal(next.decisions, 0);
});

test('heuristic score matches the documented formula', () => {
  const expected = Math.round((0.1 + 0.28 + 0.1 + 0.22 * 0.8 - 0.3 * 0.2) * 10000) / 10000;
  assert.equal(heuristicScore({ ...base }), expected);
});
