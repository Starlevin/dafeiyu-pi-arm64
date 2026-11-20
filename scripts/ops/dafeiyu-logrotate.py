#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大肥鱼引擎日志按大小轮转（copytruncate）。

- 目标：~/dafeiyu-test/run/qq-bot.log（启动器以追加模式写入）
- 阈值：单文件 > 5 MiB 时轮转，保留 qq-bot.log.1 / qq-bot.log.2
- 权限：始终 0600（日志内含本机面板控制 URL，绝不外泄）
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
KEEP = 2
LOG = Path.home() / "dafeiyu-test" / "run" / "qq-bot.log"


def rotate(path: Path = LOG) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "absent"
    if size < MAX_BYTES:
        return "below_limit size=%d" % size

    for index in range(KEEP - 1, 0, -1):
        src = path.with_name(path.name + ".%d" % index)
        dst = path.with_name(path.name + ".%d" % (index + 1))
        if src.exists():
            os.replace(str(src), str(dst))

    staging = path.with_name(path.name + ".rotating")
    shutil.copy2(str(path), str(staging))
    os.chmod(str(staging), 0o600)
    os.replace(str(staging), str(path.with_name(path.name + ".1")))
    os.truncate(str(path), 0)
    return "rotated from=%d" % size


if __name__ == "__main__":
    print(rotate())
    sys.exit(0)
