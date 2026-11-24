"use strict";
/* Unit Test Suite for DaFeiYu Home R5.2 Priority Event State Machine (event-engine.js) */
var assert = require("assert");
var path = require("path");
var EE = require(path.join(__dirname, "..", "web", "fish_house", "home", "js", "event-engine.js"));

var fakeNow = 1795200000000;
function clock() { return fakeNow; }
function advance(ms) { fakeNow += ms; }

var passed = 0;
function test(name, fn) {
  fakeNow = 1795200000000;
  fn();
  passed += 1;
  console.log("  PASS:", name);
}

console.log("=== DaFeiYu R5.2 Event Engine Unit Tests ===");

test("1. All 9 states (idle/received/thinking/replying/sent/happy/confused/rest/error) transition cleanly", function () {
  var eng = EE.createEngine({ nowFn: clock });
  assert.strictEqual(eng.getState().state, "idle");

  var seq = 1;
  var list = ["received", "thinking", "replying", "sent", "happy", "confused", "error", "rest", "idle"];
  for (var i = 0; i < list.length; i++) {
    var r = eng.pushEvent({ type: list[i], source: "qq", verified: true, seq: seq++, at: fakeNow / 1000, force: true });
    assert.strictEqual(r.accepted, true, "state " + list[i]);
    assert.strictEqual(eng.getState().state, list[i]);
  }
});

test("2. Legacy fish-house phase aliases normalize cleanly", function () {
  var eng = EE.createEngine({ nowFn: clock });
  eng.pushEvent({ phase: "notice", source: "wechat", verified: true, seq: 1 });
  assert.strictEqual(eng.getState().state, "received");
  eng.pushEvent({ phase: "work", source: "qq", verified: true, seq: 2 });
  assert.strictEqual(eng.getState().state, "thinking");
  eng.pushEvent({ phase: "reply", source: "wechat", verified: true, seq: 3 });
  assert.strictEqual(eng.getState().state, "replying");
});

test("3. Priority matrix blocks lower-priority concurrent same-seq/unsequenced event", function () {
  var eng = EE.createEngine({ nowFn: clock });
  eng.pushEvent({ type: "replying", source: "qq", verified: true, seq: 10, at: fakeNow });
  assert.strictEqual(eng.getState().state, "replying");

  // Same seq lower-priority received should NOT interrupt replying
  var blocked = eng.pushEvent({ type: "received", source: "qq", verified: true, seq: 10, at: fakeNow });
  assert.strictEqual(blocked.accepted, false);
  assert.strictEqual(blocked.reason, "lower_priority_blocked");
  assert.strictEqual(eng.getState().state, "replying");

  // Higher-priority error IMMEDIATELY preempts replying
  var err = eng.pushEvent({ type: "error", source: "qq", verified: true, seq: 10, at: fakeNow });
  assert.strictEqual(err.accepted, true);
  assert.strictEqual(eng.getState().state, "error");
});

test("4. Local touch interaction NEVER preempts active real bot state, works when idle/rest", function () {
  var eng = EE.createEngine({ nowFn: clock });
  // When idle, touch works cleanly
  var t1 = eng.pushEvent({ type: "happy", source: "touch", verified: false });
  assert.strictEqual(t1.accepted, true);
  assert.strictEqual(eng.getState().source, "touch");

  // Real verified event immediately preempts touch
  var r1 = eng.pushEvent({ type: "thinking", source: "wechat", verified: true, seq: 5, at: fakeNow });
  assert.strictEqual(r1.accepted, true);
  assert.strictEqual(eng.getState().state, "thinking");
  assert.strictEqual(eng.getState().source, "real");

  // Touch while real thinking is active MUST be blocked
  var t2 = eng.pushEvent({ type: "happy", source: "touch", verified: false });
  assert.strictEqual(t2.accepted, false);
  assert.strictEqual(t2.reason, "blocked_by_real_state");
  assert.strictEqual(eng.getState().state, "thinking");
});

test("5. Interrupt & Recovery Stack restores fresh interrupted real state after error expires", function () {
  var eng = EE.createEngine({ nowFn: clock });
  // Active thinking (TTL 90s)
  eng.pushEvent({ type: "thinking", source: "qq", verified: true, seq: 1, at: fakeNow });
  assert.strictEqual(eng.getState().state, "thinking");

  // Transient error interrupts thinking (TTL 8.5s)
  advance(2000);
  eng.pushEvent({ type: "error", source: "qq", verified: true, seq: 2, at: fakeNow });
  assert.strictEqual(eng.getState().state, "error");
  assert.strictEqual(eng.getState().hasInterrupted, true);

  // After 9s error expires, thinking is only 11s old (< 90s TTL) -> restores cleanly!
  advance(9000);
  eng.tick();
  assert.strictEqual(eng.getState().state, "thinking");
  assert.strictEqual(eng.getState().stats.interruptedRestored, 1);
});

test("A terminal send failure cannot revive a finished model animation", function () {
  var eng = EE.createEngine({ nowFn: clock });
  eng.pushEvent({ type: "thinking", source: "qq", verified: true, seq: 1, at: fakeNow });
  advance(1000);
  eng.pushEvent({ type: "error", source: "qq", verified: true, terminal: true, seq: 2, at: fakeNow });
  assert.strictEqual(eng.getState().state, "error");
  assert.strictEqual(eng.getState().hasInterrupted, false);
  advance(9000);
  eng.tick();
  assert.strictEqual(eng.getState().state, "confused");
});

test("6. Event deduplication by ID and polling signature prevents flicker", function () {
  var eng = EE.createEngine({ nowFn: clock });
  var a = eng.pushEvent({ id: "msg-101", type: "received", source: "qq", verified: true, seq: 10, at: fakeNow });
  var b = eng.pushEvent({ id: "msg-101", type: "received", source: "qq", verified: true, seq: 10, at: fakeNow });
  assert.strictEqual(a.accepted, true);
  assert.strictEqual(b.accepted, false);
  assert.strictEqual(b.reason, "duplicate_id");

  var c = eng.pushEvent({ type: "thinking", source: "qq", verified: true, seq: 11, at: fakeNow });
  var d = eng.pushEvent({ type: "thinking", source: "qq", verified: true, seq: 11, at: fakeNow });
  assert.strictEqual(c.accepted, true);
  assert.strictEqual(d.accepted, false);
  assert.strictEqual(d.reason, "duplicate_signature");
});

test("7. Out-of-order seq and future timestamps fail closed", function () {
  var eng = EE.createEngine({ nowFn: clock });
  eng.pushEvent({ type: "replying", source: "qq", verified: true, seq: 20, at: fakeNow });
  var stale = eng.pushEvent({ type: "received", source: "qq", verified: true, seq: 19, at: fakeNow });
  assert.strictEqual(stale.accepted, false);
  assert.strictEqual(stale.reason, "out_of_order_seq");

  var future = eng.pushEvent({ type: "sent", source: "qq", verified: true, seq: 21, at: fakeNow + 60000 });
  assert.strictEqual(future.accepted, false);
  assert.strictEqual(future.reason, "future_timestamp");
});

test("8. Expired real events (TTL) are rejected and fall back to idle", function () {
  var eng = EE.createEngine({ nowFn: clock });
  var oldSec = Math.floor((fakeNow - 120000) / 1000);
  var res = eng.pushEvent({ type: "received", source: "wechat", verified: true, seq: 5, at: oldSec });
  assert.strictEqual(res.accepted, false);
  assert.strictEqual(res.reason, "expired_ttl");
  assert.strictEqual(eng.getState().state, "idle");
});

test("9. Deterministic recovery chains: sent->idle, error->confused->idle, idle->rest", function () {
  var eng = EE.createEngine({ nowFn: clock });

  eng.pushEvent({ type: "sent", source: "qq", verified: true, seq: 1, at: fakeNow });
  assert.strictEqual(eng.getState().state, "sent");
  advance(5200);
  eng.tick();
  assert.strictEqual(eng.getState().state, "idle");

  eng.pushEvent({ type: "error", source: "qq", verified: true, seq: 2, at: fakeNow });
  assert.strictEqual(eng.getState().state, "error");
  advance(9000);
  eng.tick();
  assert.strictEqual(eng.getState().state, "confused");
  advance(5500);
  eng.tick();
  assert.strictEqual(eng.getState().state, "idle");

  advance(46000);
  eng.tick();
  assert.strictEqual(eng.getState().state, "rest");

  var wake = eng.pushEvent({ type: "received", source: "wechat", verified: true, seq: 3, at: fakeNow });
  assert.strictEqual(wake.accepted, true);
  assert.strictEqual(eng.getState().state, "received");
});

test("10. Unverified qq/wechat source is strictly stratified as demo", function () {
  var eng = EE.createEngine({ nowFn: clock });
  eng.pushEvent({ type: "thinking", source: "qq", verified: false });
  assert.strictEqual(eng.getState().source, "demo");
  assert.strictEqual(eng.getState().verified, false);
});

console.log("ALL R5.2 EVENT ENGINE TESTS PASSED:", passed + "/" + passed);
