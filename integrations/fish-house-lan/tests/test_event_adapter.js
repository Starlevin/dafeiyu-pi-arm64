"use strict";
/* Unit Test Suite for DaFeiYu R5.2 Event Adapter (event-adapter.js)
 * Verifies 100% parity with Codex's backend/status_adapter.py (commit f435016).
 */
var assert = require("assert");
var path = require("path");
var EA = require(path.join(__dirname, "..", "web", "fish_house", "home", "js", "event-adapter.js"));

var NOW = 1800000000;
function ev(phase, reason, extra) {
  var out = { time: NOW, phase: phase, reason: reason };
  if (extra) {
    for (var k in extra) {
      if (Object.prototype.hasOwnProperty.call(extra, k)) out[k] = extra[k];
    }
  }
  return out;
}

var passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  console.log("  PASS:", name);
}

console.log("=== DaFeiYu R5.2 Event Adapter & Codex Bridge Parity Tests ===");

test("1. Authoritative Codex input stages match backend/status_adapter.py", function () {
  assert.strictEqual(EA.normalizeCodexActivity(ev("received", "group"), NOW), "received");
  assert.strictEqual(EA.normalizeCodexActivity(ev("generation", "started"), NOW), "thinking");
  assert.strictEqual(EA.normalizeCodexActivity(ev("generation", "ready"), NOW), "replying");
  assert.strictEqual(EA.normalizeCodexActivity(ev("submit", "started"), NOW), "replying");
});

test("2. Local handoff is replying, NOT a send receipt; only visible_outgoing/confirmed with success=true is sent", function () {
  assert.strictEqual(EA.normalizeCodexActivity(ev("local_handoff", "accepted", { local_handoff: true }), NOW), "replying");
  assert.strictEqual(EA.normalizeCodexActivity(ev("confirmation", "accepted", { success: true }), NOW), "idle");
  assert.strictEqual(EA.normalizeCodexActivity(ev("confirmation", "visible_outgoing", { success: false }), NOW), "error");
  assert.strictEqual(EA.normalizeCodexActivity(ev("confirmation", "visible_outgoing", { success: true }), NOW), "sent");
  assert.strictEqual(EA.normalizeCodexActivity(ev("confirmation", "confirmed", { success: true }), NOW), "sent");
});

test("3. Errors, expired (>60s), future (<0), and malformed events fail closed", function () {
  assert.strictEqual(EA.normalizeCodexActivity(ev("submit", "failed", { failed: true }), NOW), "error");
  assert.strictEqual(EA.normalizeCodexActivity(ev("generation", "started"), NOW + 61), "idle");
  assert.strictEqual(EA.normalizeCodexActivity(ev("generation", "started"), NOW - 1), "idle");
  assert.strictEqual(EA.normalizeCodexActivity({ time: "bad", phase: "received", reason: "group" }, NOW), "idle");
  assert.strictEqual(EA.normalizeCodexActivity(ev("unknown", "received"), NOW), "idle");
  assert.strictEqual(EA.normalizeCodexActivity(null, NOW), "idle");
});

test("4. Bridge-health snapshot requires matching success marker and honors latest event", function () {
  var confirmEv = ev("confirmation", "visible_outgoing");
  assert.strictEqual(EA.normalizeCodexBridgeHealth({ last_event: confirmEv }, NOW), "idle");
  assert.strictEqual(EA.normalizeCodexBridgeHealth({ last_event: confirmEv, last_success: confirmEv }, NOW), "sent");

  var recvEv = ev("received", "group");
  assert.strictEqual(EA.normalizeCodexBridgeHealth({ last_success: confirmEv, last_event: recvEv }, NOW), "received");

  var failEv = ev("confirmation", "send_exception");
  var priorOk = ev("confirmation", "visible_outgoing");
  priorOk.time -= 1;
  assert.strictEqual(EA.normalizeCodexBridgeHealth({ last_event: failEv, last_success: priorOk, last_failure: failEv }, NOW), "error");
  assert.strictEqual(EA.normalizeCodexBridgeHealth({}, NOW), "idle");
});

test("5. parseIncomingPayload handles canonical, raw Codex activity, and bridge-health envelopes", function () {
  var p1 = EA.parseIncomingPayload({ seq: 8, phase: "thinking", source: "qq", verified: true,
                                    at: NOW, expires_at: NOW + 120 }, NOW * 1000);
  assert.strictEqual(p1.type, "thinking");
  assert.strictEqual(p1.verified, true);
  assert.strictEqual(p1.expiresAt, NOW + 120);

  var p2 = EA.parseIncomingPayload({
    last_event: { time: NOW, phase: "generation", reason: "started", trace: "abc" },
    source: "wechat"
  }, NOW * 1000);
  assert.strictEqual(p2.type, "thinking");
  assert.strictEqual(p2.verified, true);
  assert.strictEqual(p2.id, "abc:thinking");
});

test("6. Connection status cannot create an animation or refresh its timestamp", function () {
  var original = global.XMLHttpRequest;
  var eventSource = global.EventSource;
  var events = [], statusReads = 0;
  global.EventSource = undefined;
  global.XMLHttpRequest = function () {
    this.open = function (_, url) { this.url = url; };
    this.send = function () {
      this.readyState = 4;
      this.status = 200;
      this.responseText = JSON.stringify(this.url.indexOf("state.json") >= 0
        ? { phase: "idle", seq: 1, verified: true, source: "wechat", at: NOW }
        : { wechat: { activity_state: "thinking" }, timestamp: NOW });
      this.onreadystatechange();
    };
  };
  var adapter = EA.createEventAdapter({
    engine: { pushEvent: function (event) { events.push(event); return { accepted: true }; } },
    onStatusData: function () { statusReads += 1; }
  });
  try {
    adapter.start();
    assert.strictEqual(statusReads, 1);
    assert.deepStrictEqual(events.map(function (event) { return event.type; }), ["idle"]);
  } finally {
    adapter.stop();
    global.XMLHttpRequest = original;
    global.EventSource = eventSource;
  }
});

console.log("ALL R5.2 EVENT ADAPTER TESTS PASSED:", passed + "/" + passed);
