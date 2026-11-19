#!/usr/bin/env python3
"""Read-only metadata-change hint from the *active* WeChat's WAL files.

No SQL queries, decryption, process-memory access, message bodies or keys.
This detector only says that a local encrypted database file changed.
"""
from __future__ import annotations
import os
import re
import subprocess
import time
from pathlib import Path

DB_ROLE = re.compile(r"^(?:session|message_[0-9]+|biz_message_[0-9]+)[.]db(?:-wal)?$")
ROOT = Path.home().resolve()

def find_active_wal_files():
    result = set()
    try:
        lines = subprocess.check_output(
            ["ps", "-eo", "pid=,comm="], text=True, timeout=4).splitlines()
    except (OSError, subprocess.SubprocessError):
        return []
    for line in lines:
        parts = line.split()
        if len(parts) != 2 or "wechat" not in parts[1].lower() and "weixin" not in parts[1].lower():
            continue
        try:
            fd_dir = Path("/proc") / parts[0] / "fd"
            for fd in fd_dir.iterdir():
                try:
                    name = os.readlink(fd)
                    filename = Path(name).name
                    if not DB_ROLE.fullmatch(filename):
                        continue
                    path = Path(name)
                    if path.is_file() and path.resolve().is_relative_to(ROOT):
                        if filename.endswith("-wal"):
                            result.add(path)
                        else:
                            wal = Path(str(path) + "-wal")
                            if wal.is_file():
                                result.add(wal)
                except (OSError, ValueError):
                    continue
        except (OSError, PermissionError):
            continue
    return sorted(result)

def fingerprint(file):
    """Only filesystem metadata, no file opens or content reads."""
    try:
        s = Path(file).stat()
        return s.st_ino, s.st_size, s.st_mtime_ns
    except (OSError, ValueError):
        return None

class WalHint:
    def __init__(self, paths):
        self.paths = tuple(paths)
        self.baseline = {p: fingerprint(p) for p in self.paths}

    def changed(self):
        events = set()
        for p in self.paths:
            curr = fingerprint(p)
            if curr != self.baseline.get(p):
                self.baseline[p] = curr
                events.add("session" if Path(p).name.startswith("session") else "message")
        return events

if __name__ == "__main__":
    watched = find_active_wal_files()
    print("ACTIVE_ENCRYPTED_WAL_FILES", len(watched), flush=True)
    print("WATCH_ROLES", sorted({("session" if p.name.startswith("session") else "message") for p in watched}), flush=True)
    hint = WalHint(watched)
    counts = {"session": 0, "message": 0}
    begin = time.monotonic()
    while time.monotonic() - begin < 12:
        for role in hint.changed():
            counts[role] += 1
        time.sleep(0.2)
    print("READ_ONLY_MONITOR_DURATION_S", round(time.monotonic() - begin, 1))
    print("METADATA_CHANGES_ONLY", counts)
    print("NO_DECRYPTION_NO_PROCESS_MEMORY_NO_CONTENT", True)
