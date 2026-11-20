'use strict';
// ReplyDecision Protocol v1 — CommonJS mirror of qq-core/Sources/BotCore/ReplyDecisionProtocol.swift.
// Contract: ../../PROTOCOL_REPLY_DECISION_V1.md
// The scorer only returns a score. Any scorer failure means no proactive speech.

const ACTIONS = Object.freeze({ SPEAK: 'SPEAK', WAIT: 'WAIT', SKIP: 'SKIP' });
const REASONS = Object.freeze({
  MUST_ANSWER: 'must_answer',
  QUIET_HOURS: 'quiet_hours',
  DAILY_BUDGET: 'daily_budget',
  ADDRESSED_TO_OTHER: 'addressed_to_other',
  REPEAT_SUPPRESSED: 'repeat_suppressed',
  RECENTLY_SPOKE: 'recently_spoke',
  HIGH_INTERRUPT_COST: 'high_interrupt_cost',
  EMPTY_CONTEXT: 'empty_context',
  HEURISTIC_LOW: 'heuristic_low',
  HEURISTIC_HOLD: 'heuristic_hold',
  SCORER_UNAVAILABLE: 'scorer_unavailable',
  BELOW_THRESHOLD: 'below_threshold',
  MODEL_PASS: 'model_pass',
  HEURISTIC_PASS: 'heuristic_pass',
});
const SOURCES = Object.freeze({
  MUST_ANSWER: 'must_answer', HEURISTIC: 'heuristic', MODEL: 'model', FAIL_CLOSED: 'fail_closed',
});

const DEFAULTS = Object.freeze({
  threshold: 0.7, waitBand: 0.12, dailyLimit: 30, recentSpeakSuppressSeconds: 90,
  highInterruptCost: 0.7, waitTTLSeconds: 45, maxWindow: 8, maxCharactersPerMessage: 125,
});

function clamp01(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 0;
  return Math.min(1, Math.max(0, n));
}
function round4(value) {
  if (!Number.isFinite(value)) return 0;
  return Math.round(value * 10000) / 10000;
}
function chars(text) { return Array.from(String(text)); }
function dayKeyFor(timestamp) { return Math.floor((Number(timestamp) + 28800) / 86400); }

function normalizeInput(raw, limits = DEFAULTS) {
  const input = Object.assign({}, raw);
  input.scope = input.scope === 'private' ? 'private' : 'group';
  input.chatAlias = String(input.chatAlias || '');
  input.mention = ['none', 'self', 'reply_to_bot', 'other'].includes(input.mention) ? input.mention : 'none';
  input.unansweredQuestion = Boolean(input.unansweredQuestion);
  input.questionToAll = Boolean(input.questionToAll);
  input.invited = Boolean(input.invited);
  input.addressedToOther = Boolean(input.addressedToOther);
  input.botSpokeRecently = Boolean(input.botSpokeRecently);
  input.secondsSinceBotReply = Math.max(0, Number(input.secondsSinceBotReply || 0));
  input.secondsSinceTrigger = Math.max(0, Number(input.secondsSinceTrigger || 0));
  input.repeatedTopic = Boolean(input.repeatedTopic);
  input.interruptCost = clamp01(input.interruptCost);
  input.topicRelevance = clamp01(input.topicRelevance);
  input.isOwner = Boolean(input.isOwner);
  input.quietHours = Boolean(input.quietHours);
  input.dailyRemaining = Math.max(0, Number(input.dailyRemaining == null ? 30 : input.dailyRemaining));
  const window = (Array.isArray(input.window) ? input.window : [])
    .map((line) => chars(String(line).trim()).slice(0, limits.maxCharactersPerMessage || DEFAULTS.maxCharactersPerMessage).join(''))
    .filter((line) => line.length > 0);
  input.window = window.slice(-(limits.maxWindow || DEFAULTS.maxWindow));
  return input;
}

function isMustAnswer(input) {
  return input.scope === 'private' || input.mention === 'self' || input.mention === 'reply_to_bot' || input.invited;
}

function heuristicScore(input) {
  let value = 0.1;
  if (input.unansweredQuestion) value += 0.28;
  if (input.questionToAll) value += 0.1;
  value += 0.22 * input.topicRelevance;
  value -= 0.3 * input.interruptCost;
  if (input.botSpokeRecently) value -= 0.12;
  if (input.repeatedTopic) value -= 0.3;
  if ((input.window || []).length <= 1) value -= 0.05;
  return round4(clamp01(value));
}

function makeResult(action, score, reason, ttlSeconds, source, heuristic, scorerCalled) {
  return {
    action, score: round4(score), reason, ttlSeconds, source,
    heuristicScore: heuristic == null ? null : round4(heuristic),
    scorerCalled: Boolean(scorerCalled),
  };
}
function skip(reason, heuristic) {
  return makeResult(ACTIONS.SKIP, 0, reason, 0, SOURCES.HEURISTIC, heuristic, false);
}

function normalizeState(state, now) {
  const base = Object.assign({
    dayKey: dayKeyFor(now), decisions: 0, speaks: 0, scorerCalls: 0,
    lastSpeakAt: 0, lastDecisionAt: 0, recentSpeakTexts: [],
  }, state || {});
  const key = dayKeyFor(now);
  if (base.dayKey !== key) {
    return {
      dayKey: key, decisions: 0, speaks: 0, scorerCalls: 0,
      lastSpeakAt: 0, lastDecisionAt: 0, recentSpeakTexts: [],
    };
  }
  return base;
}

/**
 * Decide whether to speak.
 * @param {object} rawInput
 * @param {object} rawState
 * @param {number} now epoch seconds
 * @param {function|null} scorer async (input) => number|undefined
 * @param {object} rawLimits
 * @returns {Promise<{result: object, state: object}>}
 */
async function decide(rawInput, rawState, now, scorer = null, rawLimits = {}) {
  const limits = Object.assign({}, DEFAULTS, rawLimits);
  limits.threshold = clamp01(limits.threshold);
  limits.waitBand = clamp01(limits.waitBand);
  limits.highInterruptCost = clamp01(limits.highInterruptCost);
  const input = normalizeInput(rawInput, limits);
  const state = normalizeState(rawState, now);
  state.decisions += 1;
  state.lastDecisionAt = Number(now);

  if (isMustAnswer(input)) {
    return { result: makeResult(ACTIONS.SPEAK, 1, REASONS.MUST_ANSWER, 0, SOURCES.MUST_ANSWER, null, false), state };
  }
  if (input.quietHours) return { result: skip(REASONS.QUIET_HOURS), state };
  if (input.dailyRemaining <= 0 || state.speaks >= limits.dailyLimit) return { result: skip(REASONS.DAILY_BUDGET), state };
  if (input.addressedToOther) return { result: skip(REASONS.ADDRESSED_TO_OTHER), state };
  if (input.repeatedTopic && input.botSpokeRecently) return { result: skip(REASONS.REPEAT_SUPPRESSED), state };
  if (input.botSpokeRecently && input.secondsSinceBotReply < limits.recentSpeakSuppressSeconds && !input.unansweredQuestion) {
    return { result: skip(REASONS.RECENTLY_SPOKE), state };
  }
  if (input.interruptCost >= limits.highInterruptCost) return { result: skip(REASONS.HIGH_INTERRUPT_COST), state };
  if (!input.window.length) return { result: skip(REASONS.EMPTY_CONTEXT), state };

  const heuristic = heuristicScore(input);
  const gate = round4(limits.threshold - limits.waitBand);
  if (heuristic < gate) return { result: skip(REASONS.HEURISTIC_LOW, heuristic), state };

  if (typeof scorer === 'function') {
    state.scorerCalls += 1;
    let raw;
    try {
      raw = await scorer(input);
    } catch (err) {
      return { result: makeResult(ACTIONS.SKIP, 0, REASONS.SCORER_UNAVAILABLE, 0, SOURCES.FAIL_CLOSED, heuristic, true), state };
    }
    if (typeof raw !== 'number' || !Number.isFinite(raw) || raw < 0 || raw > 1) {
      return { result: makeResult(ACTIONS.SKIP, 0, REASONS.SCORER_UNAVAILABLE, 0, SOURCES.FAIL_CLOSED, heuristic, true), state };
    }
    const score = round4(raw);
    if (score >= limits.threshold) {
      return { result: makeResult(ACTIONS.SPEAK, score, REASONS.MODEL_PASS, 0, SOURCES.MODEL, heuristic, true), state };
    }
    return { result: makeResult(ACTIONS.WAIT, score, REASONS.BELOW_THRESHOLD, limits.waitTTLSeconds, SOURCES.MODEL, heuristic, true), state };
  }

  if (heuristic >= limits.threshold) {
    return { result: makeResult(ACTIONS.SPEAK, heuristic, REASONS.HEURISTIC_PASS, 0, SOURCES.HEURISTIC, heuristic, false), state };
  }
  return { result: makeResult(ACTIONS.WAIT, heuristic, REASONS.HEURISTIC_HOLD, limits.waitTTLSeconds, SOURCES.HEURISTIC, heuristic, false), state };
}

function normalizeText(text) {
  return String(text).toLowerCase().replace(/[\s\p{P}\p{S}]/gu, '');
}

function recordSpeak(state, text, now) {
  const next = normalizeState(state, now);
  next.speaks += 1;
  next.lastSpeakAt = Number(now);
  const normalized = normalizeText(text);
  if (!normalized) return next;
  const existing = next.recentSpeakTexts.indexOf(normalized);
  if (existing >= 0) next.recentSpeakTexts.splice(existing, 1);
  next.recentSpeakTexts.push(normalized);
  if (next.recentSpeakTexts.length > 8) next.recentSpeakTexts.splice(0, next.recentSpeakTexts.length - 8);
  return next;
}

module.exports = {
  ACTIONS, REASONS, SOURCES, DEFAULTS,
  clamp01, round4, dayKeyFor, normalizeInput, isMustAnswer, heuristicScore,
  decide, recordSpeak, normalizeText, normalizeState,
};
