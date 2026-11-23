"use strict";
/* Unit tests for public-question cooldown save in api-adapter.js.
 * Loads the browser adapter into a fake global with a synchronous-ish stub XHR:
 *   GET  /api/proactive/config -> in-memory store
 *   POST /api/proactive/config -> validates incrementally and updates the store
 * No network, no real timers, no external dependencies.
 */
var assert = require("assert");
var path = require("path");
var vm = require("vm");

var ADAPTER_PATH = path.join(__dirname, "..", "web", "fish_house", "home", "js", "api-adapter.js");

function deepCopy(value) {
  return JSON.parse(JSON.stringify(value));
}

function FakeXHR() {
  this.readyState = 0;
  this.status = 0;
  this.responseText = "";
  this._method = "";
  this._url = "";
  this._body = null;
}

FakeXHR.prototype.open = function (method, url) {
  this._method = String(method).toUpperCase();
  this._url = String(url);
};
FakeXHR.prototype.setRequestHeader = function () {};
FakeXHR.prototype.abort = function () {};
FakeXHR.prototype.send = function (body) {
  var env = FakeXHR.env;
  this._body = body;
  var parsedBody = null;
  if (body) {
    try { parsedBody = JSON.parse(body); } catch (e) { parsedBody = null; }
  }
  var result = env.handle(this._method, this._url, parsedBody);
  this.status = result.status;
  this.responseText = result.body === undefined ? "" : JSON.stringify(result.body);
  this.readyState = 4;
  if (typeof this.onreadystatechange === "function") this.onreadystatechange();
};

function loadAdapter(env) {
  var source = require("fs").readFileSync(ADAPTER_PATH, "utf8");
  var sandbox = {
    XMLHttpRequest: FakeXHR,
    setTimeout: function () { return 1; },
    clearTimeout: function () {},
    Date: Date,
    JSON: JSON,
    console: console
  };
  sandbox.self = sandbox;
  FakeXHR.env = env;
  vm.runInNewContext(source, sandbox, { filename: ADAPTER_PATH });
  return sandbox.DaFeiYuApiAdapter.createApiAdapter({ consoleBase: "" });
}

function freshStore() {
  return {
    publicQuestionCooldownDefault: 60,
    publicQuestionCooldowns: {
      qq: { "group:12345": 60, "group:99999": 45 },
      wechat: { "试点群A": 60, "试点群B": 75 }
    },
    wechatEnabledGroups: ["试点群A", "试点群B"],
    qqGroups: {
      "group:12345": { threshold: 0.7, freshnessSeconds: 3 },
      "group:99999": { threshold: 0.65, freshnessSeconds: 2 }
    }
  };
}

// When staleGet is true, GET deliberately returns the pre-POST snapshot (mismatch simulation).
function makeEnv(store, staleGet) {
  var snapshot = deepCopy(store);
  return {
    calls: [],
    handle: function (method, url, body) {
      this.calls.push({ method: method, url: url, body: body });
      if (method === "GET" && /\/api\/proactive\/config$/.test(url)) {
        return { status: 200, body: deepCopy(staleGet ? snapshot : store) };
      }
      if (method === "POST" && /\/api\/proactive\/config$/.test(url)) {
        if (!body || typeof body !== "object") {
          return { status: 400, body: { ok: false, error: "invalid body" } };
        }
        // Incremental shape
        if (body.platform === "qq" || body.platform === "wechat") {
          var sec = body.publicQuestionCooldownSeconds;
          if (typeof body.group !== "string" || !body.group ||
              typeof sec !== "number" || Math.floor(sec) !== sec || sec < 30 || sec > 1200) {
            return { status: 400, body: { ok: false, error: "invalid cooldown" } };
          }
          if (!store.publicQuestionCooldowns[body.platform]) {
            store.publicQuestionCooldowns[body.platform] = {};
          }
          store.publicQuestionCooldowns[body.platform][body.group] = sec;
          return { status: 200, body: { ok: true } };
        }
        // Backward-compatible old QQ shape
        if (typeof body.group === "string" && typeof body.threshold === "number") {
          var row = store.qqGroups[body.group] || {};
          row.threshold = body.threshold;
          if (typeof body.freshnessSeconds === "number") row.freshnessSeconds = body.freshnessSeconds;
          if (typeof body.publicQuestionCooldownSeconds === "number") {
            store.publicQuestionCooldowns.qq[body.group] = body.publicQuestionCooldownSeconds;
          }
          store.qqGroups[body.group] = row;
          return { status: 200, body: { ok: true } };
        }
        return { status: 400, body: { ok: false, error: "unsupported shape" } };
      }
      return { status: 404, body: { error: "unknown endpoint" } };
    }
  };
}

function connected(api) {
  api.state.consoleMode = "connected";
  api.state.busy = false;
  api.state.fixtureDemo = false;
}

var passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  console.log("  PASS:", name);
}

console.log("=== Public Question Cooldown Adapter Tests ===");

test("1. QQ group 90s: verified true, read-back matches, saveState confirmed", function () {
  var store = freshStore();
  var env = makeEnv(store);
  var api = loadAdapter(env);
  connected(api);
  api.savePublicQuestionCooldown("qq", "group:12345", 90, function (error, res) {
    assert.strictEqual(error, null);
    assert.strictEqual(res.verified, true);
    assert.strictEqual(res.observed, 90);
    assert.strictEqual(res.seconds, 90);
    assert.strictEqual(api.state.saveState, "confirmed");
    assert.strictEqual(store.publicQuestionCooldowns.qq["group:12345"], 90);
    assert.strictEqual(api.state.extensions.proactive.data.publicQuestionCooldowns.qq["group:12345"], 90);
  });
  var posts = env.calls.filter(function (c) { return c.method === "POST"; });
  assert.strictEqual(posts.length, 1);
  assert.deepStrictEqual(posts[0].body, {
    platform: "qq", group: "group:12345", publicQuestionCooldownSeconds: 90
  });
  assert.strictEqual(env.calls[env.calls.length - 1].method, "GET");
});

test("2. WeChat exact group 试点群A 120s: verified true, other group unchanged", function () {
  var store = freshStore();
  var env = makeEnv(store);
  var api = loadAdapter(env);
  connected(api);
  api.savePublicQuestionCooldown("wechat", "试点群A", 120, function (error, res) {
    assert.strictEqual(error, null);
    assert.strictEqual(res.verified, true);
    assert.strictEqual(res.observed, 120);
    assert.strictEqual(api.state.saveState, "confirmed");
  });
  assert.strictEqual(store.publicQuestionCooldowns.wechat["试点群A"], 120);
  assert.strictEqual(store.publicQuestionCooldowns.wechat["试点群B"], 75);
});

test("3. Unconnected / fixtureDemo / busy are rejected without POST", function () {
  var cases = [
    function (api) { api.state.consoleMode = "auth_required"; },
    function (api) { api.state.fixtureDemo = true; },
    function (api) { api.state.busy = true; }
  ];
  for (var i = 0; i < cases.length; i++) {
    var store = freshStore();
    var env = makeEnv(store);
    var api = loadAdapter(env);
    connected(api);
    cases[i](api);
    api.savePublicQuestionCooldown("qq", "group:12345", 60, function (error, res) {
      assert.strictEqual(typeof error, "string");
      assert.ok(error.length > 0);
      assert.strictEqual(res, undefined);
    });
    assert.strictEqual(env.calls.length, 0, "case " + i + " must not issue a request");
    assert.strictEqual(store.publicQuestionCooldowns.qq["group:12345"], 60);
  }
});

test("4. Invalid seconds 29/1201/60.5/'60'/true and invalid platform/group rejected without POST", function () {
  var badValues = [29, 1201, 60.5, "60", true, NaN, 0];
  for (var i = 0; i < badValues.length; i++) {
    var store = freshStore();
    var env = makeEnv(store);
    var api = loadAdapter(env);
    connected(api);
    api.savePublicQuestionCooldown("qq", "group:12345", badValues[i], function (error, res) {
      assert.strictEqual(typeof error, "string");
      assert.strictEqual(res, undefined);
    });
    assert.strictEqual(env.calls.length, 0, "bad seconds " + badValues[i] + " must not POST");
  }
  var badTargets = [
    ["wechat-alias", "group:12345", 60],
    ["qq", "", 60],
    ["qq", 12345, 60]
  ];
  for (var j = 0; j < badTargets.length; j++) {
    var env2 = makeEnv(freshStore());
    var api2 = loadAdapter(env2);
    connected(api2);
    (function (target) {
      api2.savePublicQuestionCooldown(target[0], target[1], target[2], function (error, res) {
        assert.strictEqual(typeof error, "string");
        assert.strictEqual(res, undefined);
      });
    })(badTargets[j]);
    assert.strictEqual(env2.calls.length, 0, "bad target must not POST");
  }
});

test("5. POST ok but GET stale -> mismatch, error surfaced, saveState mismatch", function () {
  var store = freshStore();
  var env = makeEnv(store, true);
  var api = loadAdapter(env);
  connected(api);
  api.savePublicQuestionCooldown("qq", "group:12345", 90, function (error, res) {
    assert.strictEqual(error, null);
    assert.strictEqual(res.verified, false);
    assert.strictEqual(res.observed, 60);
    assert.strictEqual(res.saveState, "mismatch");
    assert.strictEqual(typeof res.error, "string");
    assert.ok(res.error.length > 0);
    assert.strictEqual(api.state.saveState, "mismatch");
    assert.ok(api.state.saveMessage.indexOf("回读") >= 0);
  });
  // Store updated but read-back deliberately stale
  assert.strictEqual(store.publicQuestionCooldowns.qq["group:12345"], 90);
});

test("6. Legacy saveProactive still works (threshold/freshness read-back)", function () {
  var store = freshStore();
  var env = makeEnv(store);
  var api = loadAdapter(env);
  connected(api);
  api.saveProactive("group:12345", 0.82, 2, function (error) {
    assert.strictEqual(error, "");
    assert.strictEqual(store.qqGroups["group:12345"].threshold, 0.82);
    assert.strictEqual(store.qqGroups["group:12345"].freshnessSeconds, 2);
  });
});

console.log("ALL PUBLIC QUESTION COOLDOWN TESTS PASSED:", passed + "/" + passed);
