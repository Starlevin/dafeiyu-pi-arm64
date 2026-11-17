#!/usr/bin/env python3
"""在本机（Pi）生成短时、单次的 pair / reset 令牌。

由 Windows 一键启动器通过 SSH 调用；令牌只落在 Pi 的服务状态目录里
（state/pair-token.json，权限 0600），绝不在网络上传播明文之外的东西，
也不会打印到任何日志。服务端 unified_console.py 读取同一份文件校验并
消费令牌。

TokenSave 语义（「尽量先用 TokenSave」）：
  * 若状态目录里已有一枚未过期、未消费、类型匹配的令牌，直接复用它，
    只返回带 #pair= 的链接，不重新铸造；
  * 仅当没有可复用的令牌时才铸造新令牌。
这样避免每次点启动器都把上一枚还没用的令牌作废。

用法（在 Pi 上，由启动器经 ssh 调用）：
  python3 pair_token.py [--reset] [--state-dir DIR] [--ttl 180] [--port 18444]
输出：一行本机浏览器链接 http://127.0.0.1:<port>/#pair=...[&reset=1]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="生成密码恢复令牌")
    ap.add_argument("--state-dir", default=str(HERE / "state"))
    ap.add_argument("--ttl", type=int, default=180)
    ap.add_argument("--port", type=int, default=18444)
    ap.add_argument("--force", action="store_true",
                    help="忽略 TokenSave 复用，强制铸造新令牌")
    args = ap.parse_args(argv)

    path = Path(args.state_dir) / "pair-token.json"
    rec = read(path)
    now = time.time()
    reusable = (
        not args.force
        and isinstance(rec, dict)
        and rec.get("token")
        and isinstance(rec.get("expires"), (int, float))
        and rec["expires"] > now
        and not rec.get("consumed")
        and bool(rec.get("reset")) == bool(args.reset)
    )
    if reusable:
        token = str(rec["token"])
    else:
        import secrets
        token = secrets.token_urlsafe(24)
        write(path, {
            "token": token,
            "expires": now + args.ttl,
            "reset": bool(args.reset),
            "consumed": False,
            "created": now,
        })

    fragment = "#pair=" + token + ("&reset=1" if args.reset else "")
    print("http://127.0.0.1:%d/%s" % (args.port, fragment))
    return 0


if __name__ == "__main__":
    sys.exit(main())
