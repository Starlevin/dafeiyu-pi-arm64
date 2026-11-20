#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通过本机回环 QQ 管理中继（127.0.0.1:13001）发送一条管理员私聊告警。

只读取本机 0600 凭据文件用于连接，绝不打印 Token、账号或聊天原文。
用法：qq-admin-alert.py "告警正文"
输出：ALERT_SENT=true|false（不包含任何敏感信息）
"""
from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path

ROOT = Path.home() / "dafeiyu-test"
sys.path.insert(0, str(ROOT / "qq-admin" / "vendor"))

from websockets.asyncio.client import connect  # noqa: E402

RELAY = "ws://127.0.0.1:13001"


async def rpc(ws, action: str, params: dict, timeout: float = 15.0) -> dict:
    echo = "alert:" + secrets.token_hex(8)
    await ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("rpc_timeout")
        raw = await asyncio.wait_for(ws.recv(), remaining)
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(event, dict) and event.get("echo") == echo:
            return event


async def send(message: str, check_only: bool = False) -> bool:
    cfg = json.loads((ROOT / "secrets" / "qq-admin.json").read_text(encoding="utf-8"))
    admins = sorted(str(x) for x in (cfg.get("admins") or []) if str(x).isdigit())
    if not admins:
        return False
    token = (Path.home() / "napcat-test" / "secrets" / "onebot.token").read_text(
        encoding="utf-8").strip()
    if not token:
        return False
    headers = {"Authorization": "Bearer " + token}
    async with connect(RELAY, additional_headers=headers, proxy=None,
                       max_size=16_000_000, compression=None,
                       open_timeout=8, close_timeout=3) as ws:
        login = await rpc(ws, "get_login_info", {})
        if str((login.get("data") or {}).get("user_id")) != str(cfg.get("account")):
            return False
        if check_only:
            return True
        result = await rpc(ws, "send_private_msg", {
            "user_id": int(admins[0]),
            "message": [{"type": "text", "data": {"text": message[:500]}}],
        })
        return result.get("status") == "ok" and result.get("retcode") == 0


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    text = "" if check_only else (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not check_only and not text:
        print("ALERT_SENT=false reason=empty")
        return 2
    try:
        ok = asyncio.run(send(text, check_only))
    except Exception as exc:  # noqa: BLE001 - 只输出异常类名，避免泄露内部细节
        print("ALERT_SENT=false reason=%s" % type(exc).__name__)
        return 1
    if check_only:
        print("ALERT_CHANNEL_OK=%s" % ("true" if ok else "false"))
        return 0 if ok else 1
    print("ALERT_SENT=%s" % ("true" if ok else "false"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
