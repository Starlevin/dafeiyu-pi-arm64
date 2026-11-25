#!/usr/bin/env python3
"""Verify the shipped QQ engine using loopback-only group and model fixtures.

Run on the Linux target from the repository root:
  unshare -Urn sh -c 'ip link set lo up && python3 -B scripts/test-qq-ordinary-loopback.py "$1"' -- /path/to/dafeiyu
"""

import argparse
from datetime import datetime, timedelta
import http.server
import importlib.util
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
from zoneinfo import ZoneInfo

CLOCK = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <sys/time.h>
#include <time.h>

/* Fixture process only. Deadlines based on the monotonic clock are unchanged. */
int clock_gettime(clockid_t clock, struct timespec *value) {
    int (*real)(clockid_t, struct timespec *) = dlsym(RTLD_NEXT, "clock_gettime");
    int result = real(clock, value);
    if (!result && (clock == CLOCK_REALTIME || clock == CLOCK_REALTIME_COARSE)) value->tv_sec += OFFSET_SECONDS;
    return result;
}
int gettimeofday(struct timeval *value, void *zone) {
    int (*real)(struct timeval *, void *) = dlsym(RTLD_NEXT, "gettimeofday");
    int result = real(value, zone);
    if (!result && value) value->tv_sec += OFFSET_SECONDS;
    return result;
}
time_t time(time_t *value) {
    time_t (*real)(time_t *) = dlsym(RTLD_NEXT, "time");
    time_t now = real(0) + OFFSET_SECONDS;
    if (value) *value = now;
    return now;
}
"""

STATEMENTS = (
    "周末走到海边才发现那边风挺凉快的",
    "海边那段路傍晚人少，走起来特别舒服",
    "海边散步比在商场里转圈舒服多了",
)
OTHER_GROUP = "隔离对话不能进主群"
REPLY = "海边那段有风又不挤，确实比在商场绕圈舒服。"


def load_runner(path):
    spec = importlib.util.spec_from_file_location("qq_ingress_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def model_server():
    requests = []

    class ChatModel(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self._write({"data": [{"id": "fixture-chat"}]})

        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            assert self.headers.get("Authorization") == "Bearer synthetic-model-key"
            length = int(self.headers["Content-Length"])
            assert length <= 128_000
            body = json.loads(self.rfile.read(length))
            scoring = body.get("max_tokens") == 12
            requests.append(("score" if scoring else "reply", body))
            content = ("0.68" if scoring else
                       json.dumps({"text": REPLY, "emotion": "neutral", "intensity": 0}, ensure_ascii=False))
            self._write({"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                         "usage": {"total_tokens": 7}})

        def _write(self, document):
            data = json.dumps(document).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 18090), ChatModel)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, requests


def replay(root, binary, fixture, runner, requests):
    clock_source = root / "clock.c"
    clock_library = root / "clock.so"
    clock_source.write_text(CLOCK, encoding="utf-8")
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    noon = (now + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    offset = int((noon - now).total_seconds())
    subprocess.run(["cc", "-shared", "-fPIC", f"-DOFFSET_SECONDS={offset}",
                    str(clock_source), "-o", str(clock_library), "-ldl"],
                   check=True, timeout=15, capture_output=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(root), "TZ": "Asia/Shanghai",
           "LD_PRELOAD": str(clock_library), "DAFEIYU_DATA_DIR": str(root),
           "DAFEIYU_AUTO_RESUME": "1", "DAFEIYU_ONEBOT_TOKEN": "synthetic-test-token",
           "DAFEIYU_LLM_API_KEY": "synthetic-model-key",
           "DAFEIYU_LLM_PROVIDER": "custom", "DAFEIYU_LLM_BASE_URL": "http://127.0.0.1:18090/v1",
           "DAFEIYU_LLM_MODEL": "fixture-chat", "DAFEIYU_REPLY_MODE": "fast",
           "DAFEIYU_PROACTIVE_GROUP_MODES": "99999=active",
           "DAFEIYU_REPLY_DECISION_V2": "enforce",
           "DAFEIYU_QQ_REPLY_TIMING": "1"}
    (root / "message-shape").write_text("groupParticipation", encoding="ascii")
    peer = subprocess.Popen([sys.executable, "-B", str(fixture), str(root)], env=env,
                            stdout=subprocess.PIPE, text=True)
    engine = None
    try:
        ready, _, _ = select.select([peer.stdout], [], [], 5)
        assert ready, "loopback OneBot fixture did not start"
        port = int(peer.stdout.readline())
        state = runner.initial_state()
        state["config"]["endpoint"] = f"ws://127.0.0.1:{port}"
        state["config"]["memoryEnabled"] = False
        (root / "qq-state.json").write_text(json.dumps(state), encoding="utf-8")
        policy = {"group:99999": {"mode": "enforce", "ordinaryReplyEnabled": True,
                                  "publicQuestionEnabled": True, "threshold": 0.45,
                                  "freshnessSeconds": 3, "dailyBudget": 30}}
        (root / "qq-decision-config.json").write_text(json.dumps(policy), encoding="utf-8")
        log = root / "swift.log"
        with log.open("wb") as output:
            engine = subprocess.Popen([str(binary)], cwd=root, env=env, stdout=output, stderr=output)
        runner.await_state(lambda: runner.online(log), "isolated engine did not connect", seconds=7)
        pending = root / "events.tmp"
        rows = []
        for n, statement in enumerate((OTHER_GROUP,) + STATEMENTS, start=1):
            row = runner.inbound(99999 if n > 1 else 88888, n)
            row["user_id"] = row["sender"]["user_id"] = 54320 + n % 2
            row["message"][0]["data"]["text"] = statement
            rows.append(row)
        def push(batch):
            pending.write_text(json.dumps(batch), encoding="utf-8")
            pending.replace(root / "event-batch")

        def snapshot():
            return json.loads((root / "qq-state.json").read_bytes())

        push(rows)
        capture = root / "payloads"
        try:
            runner.await_state(lambda: snapshot()["sends"]["confirmed"] == 1,
                               "sample did not produce a confirmed OneBot send", seconds=8)
        except AssertionError:
            diagnostics = log.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in diagnostics:
                if line.startswith(("[qq_proactive_funnel]", "[qq_reply_decision_v2]", "[qq_reply_decision_v2_veto]")):
                    print(line[:240])
            print("fixture_model_request_types=" + repr([kind for kind, _ in requests]))
            raise
        seen_after_first = len(snapshot()["seen"])
        followups = [runner.inbound(99999, n) for n in range(21, 24)]
        for row in followups:
            row["message"][0]["data"]["text"] = "海边那条散步路线我还想再去一趟"
        # Replay the same IDs, then reach the participation interval again
        # while the group is still under its production cooldown.
        push(rows[1:] + followups)
        runner.await_state(lambda: len(snapshot()["seen"]) == seen_after_first + len(followups),
                           "cooldown or replay fixture never reached the engine", seconds=4)
        time.sleep(.65)
        assert snapshot()["sends"]["confirmed"] == 1
        payloads = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
        assert len(payloads) == 1 and str(payloads[0].get("group_id")) == "99999"
        assert REPLY in json.dumps(payloads, ensure_ascii=False)
        assert [kind for kind, _ in requests] == ["score", "reply"], [kind for kind, _ in requests]
        bodies = json.dumps([body for _, body in requests], ensure_ascii=False)
        assert STATEMENTS[-1] in bodies and OTHER_GROUP not in bodies
        print("QQ_ORDINARY_ISOLATED=PASS ordinary_no_mention=true ordinary_no_question=true "
              "score_calls=1 generation_calls=1 onebot_confirmed=1 "
              "cross_group=isolated replay_no_send=true cooldown_no_send=true real_send=0")
    finally:
        if engine is not None:
            runner.stop(engine)
        runner.stop(peer)
        peer.stdout.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    if {name for _, name in socket.if_nameindex()} != {"lo"}:
        parser.error("run in an isolated loopback-only network namespace")
    os.umask(0o077)
    scripts = Path(__file__).resolve().parent
    peer = scripts.parent / "Tests/QQEngineTests/onebot_fixture.py"
    runner = load_runner(scripts / "test-qq-ingress-persistence.py")
    server, thread, requests = model_server()
    try:
        with tempfile.TemporaryDirectory(prefix="qq-ordinary-loopback-") as stage:
            replay(Path(stage), args.binary.resolve(strict=True), peer, runner, requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
