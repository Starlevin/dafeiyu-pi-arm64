#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大肥鱼引擎日志按大小轮转（copytruncate）。

- 目标：${DAFEIYU_ROOT}/run/qq-bot.log（启动器以追加模式写入）
- 阈值：单文件 > 5 MiB 时轮转，保留 qq-bot.log.1 / qq-bot.log.2
- 权限：始终 0600（日志内含本机面板控制 URL，绝不外泄）
- .3 只留滚出完整日志的近 24 小时脱敏主动发言记录。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
KEEP = 2
LOG = Path.home() / "dafeiyu-test" / "run" / "qq-bot.log"
HISTORY_BYTES = 2_000_000
FUNNEL_LINE = re.compile(
    rb"\[qq_proactive_funnel\] at=([0-9]{1,12}(?:\.[0-9]{1,9})?) "
    rb"scope=[0-9a-f]{16} event=[a-z_]{1,48} reason=[a-z0-9_-]{1,48}"
)


def _preserve_funnel(path: Path, evicted: Path, *, now: float) -> None:
    """Keep only complete, already-deidentified records from the retiring log."""
    history = {}
    for source, scan_bytes in ((path, HISTORY_BYTES), (evicted, MAX_BYTES + HISTORY_BYTES)):
        try:
            log = source.open("rb")
        except FileNotFoundError:
            continue
        with log:
            # The retained archive is small, but an evicted full log can exceed
            # the rotation threshold between timer ticks.
            offset = max(0, log.seek(0, os.SEEK_END) - scan_bytes)
            if offset:
                log.seek(offset - 1)
                if log.read(1) != b"\n":
                    log.readline()
            else:
                log.seek(0)
            for raw in log:
                if not raw.endswith(b"\n"):
                    break
                raw = raw[:-1].removesuffix(b"\r")
                match = FUNNEL_LINE.fullmatch(raw)
                if not match:
                    continue
                stamp = float(match[1])
                if now - 86400 <= stamp <= now:
                    history[raw] = stamp
    size = 0
    records = []
    for raw in sorted(history, key=history.__getitem__, reverse=True):
        size += len(raw) + 1
        if size > HISTORY_BYTES:
            break
        records.append(raw)
    if not records and not path.exists():
        return
    with tempfile.NamedTemporaryFile(prefix=path.name + ".", dir=path.parent, delete=False) as out:
        staged = Path(out.name)
        try:
            for raw in reversed(records):
                out.write(raw + b"\n")
            out.close()
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)


def rotate(path: Path = LOG, *, starting: bool = False) -> str:
    """Serialize startup rename and the timer's in-place rotation."""
    if not path.exists():
        return "absent"
    if os.name == "nt":
        return _rotate(path, starting=starting)  # Offline fixture only.
    import fcntl
    lock_path = path.with_name(path.name + ".rotate.lock")
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600), "wb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _rotate(path, starting=starting)


def _rotate(path: Path, *, starting: bool) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "absent"
    if size < MAX_BYTES and not starting:
        return "below_limit size=%d" % size

    oldest = path.with_name(path.name + ".%d" % KEEP)
    if oldest.is_file():
        _preserve_funnel(path.with_name(path.name + ".3"), oldest, now=time.time())
    for index in range(KEEP - 1, 0, -1):
        src = path.with_name(path.name + ".%d" % index)
        dst = path.with_name(path.name + ".%d" % (index + 1))
        if src.exists():
            os.replace(str(src), str(dst))

    newest = path.with_name(path.name + ".1")
    if starting:
        os.replace(path, newest)
        return "rotated_startup from=%d" % size
    staging = path.with_name(path.name + ".rotating")
    shutil.copy2(str(path), str(staging))
    os.chmod(str(staging), 0o600)
    os.replace(str(staging), str(newest))
    os.truncate(str(path), 0)
    return "rotated from=%d" % size


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--startup", type=Path, help="Rotate the stopped engine's log before opening a new one")
    args = parser.parse_args()
    print(rotate(args.startup or LOG, starting=args.startup is not None))
