#!/usr/bin/env python3
"""Verify the Linux Swift QQ intake checkpoint with an isolated OneBot peer."""

import argparse
import ctypes
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import select
import struct
import subprocess
import sys
import tempfile
import time
import uuid


ACCOUNT = 12345
GROUPS = (99999, 88888)
FIRST_ID, SECOND_ID = 701, 702
RENAMED_INTO_DIRECTORY = 0x80
# Include companion events so rapid replacements of the same name cannot
# be coalesced before the watcher reads the queue.
FILE_UPDATE_EVENTS = RENAMED_INTO_DIRECTORY | 0x40 | 0x100 | 0x8


def await_state(check, description, seconds=9):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.03)
    raise AssertionError(description)


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=4)


def inbound(group, message_id):
    return {"post_type": "message", "message_type": "group", "sub_type": "normal",
            "self_id": ACCOUNT, "group_id": group, "message_id": message_id, "user_id": 54321,
            "sender": {"user_id": 54321}, "message": [{"type": "text", "data": {"text": "合成入站夹具"}}]}


def initial_state():
    targets = [{"id": str(uuid.uuid4()), "number": str(group), "name": "fixture",
                "group": True, "enabled": True} for group in GROUPS]
    ai = {"model": "workbuddy-deepseek", "prompt": "fixture", "maxTokens": 160,
          "dailyLimit": 200, "cooldownSeconds": 5, "workHoursEnabled": False,
          "workStart": 0, "workEnd": 23, "chats": []}
    return {"config": {"expectedSelfID": str(ACCOUNT), "targets": targets, "ai": ai,
                       "groupParticipationEnabled": False, "memoryEnabled": True},
            "seen": {}, "logs": [],
            "sends": {"day": "", "attempts": 0, "confirmed": 0, "uncertain": 0,
                      "byChat": {}, "pending": [], "lastByChat": {}},
            "usage": {"day": "", "calls": 0, "tokens": 0}}


def atomic_replacements(descriptor, name):
    total = 0
    while True:
        try:
            batch = os.read(descriptor, 4096)
        except BlockingIOError:
            return total
        offset = 0
        while offset < len(batch):
            _, mask, _, length = struct.unpack_from("iIII", batch, offset)
            offset += 16
            written = batch[offset:offset + length].rstrip(b"\0")
            if mask & RENAMED_INTO_DIRECTORY and written == name:
                total += 1
            offset += length


def online(log):
    match = re.search(r"QQ_CONTROL_URL=http://127\.0\.0\.1:(\d+)/#([0-9A-Za-z-]+)", log.read_text())
    if not match:
        return False
    port, token = match.groups()
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=2)
    try:
        conn.request("GET", "/api/status", headers={
            "x-qq-control": token, "Origin": f"http://127.0.0.1:{port}",
        })
        response = conn.getresponse()
        status = json.load(response)
        return response.status == 200 and status["connected"] and status["running"]
    finally:
        conn.close()


def run_round(binary, fixture, state_file, run_dir, messages, expected):
    run_dir.mkdir()
    (run_dir / "message-shape").write_text("groupParticipation")
    peer = subprocess.Popen([sys.executable, "-B", str(fixture), str(run_dir)],
                            stdout=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([peer.stdout], [], [], 6)
        assert ready, "OneBot fixture did not start"
        port = int(peer.stdout.readline())
        state = json.loads(state_file.read_bytes()) if state_file.exists() else initial_state()
        state["config"]["endpoint"] = f"ws://127.0.0.1:{port}"
        state_file.write_text(json.dumps(state))
        environment = {"PATH": os.environ["PATH"], "HOME": str(run_dir),
                       "LD_LIBRARY_PATH": os.environ.get("LD_LIBRARY_PATH", ""),
                       "DAFEIYU_DATA_DIR": str(state_file.parent), "DAFEIYU_AUTO_RESUME": "1",
                       "DAFEIYU_ONEBOT_TOKEN": "synthetic-test-token",
                       "DAFEIYU_LLM_API_KEY": "synthetic-model-key",
                       "DAFEIYU_MEMORY_RELIABLE": "authoritative",
                       "DAFEIYU_LLM_PROVIDER": "custom",
                       "DAFEIYU_LLM_BASE_URL": "http://127.0.0.1:9/v1"}
        with (run_dir / "swift.log").open("wb") as log:
            engine = subprocess.Popen([str(binary)], cwd=run_dir, env=environment, stdout=log, stderr=log)
        try:
            actions = run_dir / "actions"
            await_state(lambda: actions.exists() and "get_group_list" in actions.read_text(),
                        "QQ fixture did not finish login")
            await_state(lambda: online(run_dir / "swift.log"),
                        "QQ fixture did not begin receiving")
            libc = ctypes.CDLL(None, use_errno=True)
            descriptor = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            assert descriptor >= 0
            try:
                target = os.fsencode(state_file.parent)
                assert libc.inotify_add_watch(descriptor, target, FILE_UPDATE_EVENTS) >= 0
                pending = run_dir / "events.tmp"
                pending.write_text(json.dumps(messages))
                pending.replace(run_dir / "event-batch")

                def saved():
                    snapshot = json.loads(state_file.read_bytes())
                    books = snapshot.get("memoryBooks") or {}
                    return all(
                        {row["id"] for row in books.get(f"{ACCOUNT}:group:{group}", {}).get("recent", [])}
                        == {str(message_id) for message_id in ids}
                        and all(f"group:{group}:{message_id}" in snapshot.get("seen", {}) for message_id in ids)
                        for group, ids in expected.items()
                    )

                await_state(saved, "The QQ state did not persist both the scoped ID and memory")
                assert state_file.stat().st_mode & 0o777 == 0o600
                replacements = atomic_replacements(descriptor, os.fsencode(state_file.name))
            finally:
                os.close(descriptor)
            snapshot = json.loads(state_file.read_bytes())
            for group, ids in expected.items():
                scope = f"{ACCOUNT}:group:{group}"
                book = snapshot["memoryBooks"][scope]
                assert len(book["recent"]) == len(ids)
                digest = hashlib.sha256(scope.encode()).hexdigest()[:16]
                journal = state_file.parent / "QQMemoryReliable" / f"scope-{digest}" / "journal"
                raw_records = [json.loads(line) for segment in journal.glob("seg-*.log")
                               for line in segment.read_text().splitlines()]
                assert len(raw_records) == len(ids), (group, len(raw_records), len(ids))
            assert not re.search(r"send_group_msg|send_private_msg", actions.read_text())
            return replacements
        finally:
            stop(engine)
    finally:
        stop(peer)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=lambda p: Path(p).resolve(strict=True))
    parser.add_argument("fixture", type=lambda p: Path(p).resolve(strict=True))
    parser.add_argument("--writes-per-input", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="qq-ingress-checkpoint-") as scratch:
        root = Path(scratch)
        state = root / "qq-state.json"
        messages = [inbound(group, FIRST_ID) for group in GROUPS]
        first = run_round(args.binary, args.fixture, state, root / "first", messages,
                          {group: [FIRST_ID] for group in GROUPS})
        second = run_round(args.binary, args.fixture, state, root / "second",
                           messages + [inbound(GROUPS[0], SECOND_ID)],
                           {GROUPS[0]: [FIRST_ID, SECOND_ID], GROUPS[1]: [FIRST_ID]})
        assert (first, second) == (2 * args.writes_per_input, args.writes_per_input), (first, second)
        print(f"QQ_INGRESS_CHECKPOINT=PASS writes_first={first} writes_after_restart={second} "
              "cross_group_same_id=PASS replayed_writes=0 outbound_sends=0", flush=True)


if __name__ == "__main__":
    main()
