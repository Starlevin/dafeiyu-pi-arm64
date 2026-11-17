#!/usr/bin/env python3
"""pi_care.py — 主动搭话调度器（按 core 的约定实现）

core 的设计约定（见 src/http/routes/care.js 头注释）：
  「判定只在这里做一次；桥接只负责『按点问一次 → 发 → 记账』」

所以本模块负责：
  1. 定期 GET /api/care/due?last=&sentToday=&active=   问「现在该发吗」
  2. due=true 时 GET /api/care?chat=<target>            取那句话
  3. 调发送函数写进微信输入框
  4. 自己记 last / sentToday（按上海日期跨天清零）

注意：时间窗、频率、每日上限全部由 core 判定，这里不重复实现。
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from runtime_paths import state_home

CORE = "http://127.0.0.1:19301"
POLL_SEC = 300          # 每 5 分钟问一次
STATE = str(state_home() / "care-state.json")


def _today() -> str:
    # core 用上海日期做每日上限
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.datetime.now().strftime("%Y-%m-%d")


class CareLedger:
    def __init__(self, path: str = STATE):
        self.path = path
        self.last = 0
        self.sent_today = 0
        self.day = _today()

    def load(self):
        try:
            d = json.load(open(self.path, encoding="utf-8"))
            self.last = int(d.get("last") or 0)
            self.sent_today = int(d.get("sentToday") or 0)
            self.day = str(d.get("day") or _today())
        except Exception:
            pass
        self._roll()

    def _roll(self):
        if self.day != _today():
            self.day = _today()
            self.sent_today = 0

    def record(self):
        self._roll()
        self.last = int(time.time() * 1000)
        self.sent_today += 1
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"last": self.last, "sentToday": self.sent_today,
                           "day": self.day}, fh)
            os.replace(tmp, self.path)
        except Exception:
            pass


def _get(path: str, timeout=60):
    try:
        with urllib.request.urlopen(CORE + path, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"http{e.code}"}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:120]}


def start(send_fn, chat_id: str, log, active_ids=("wx_group_member",),
          poll_sec: int = POLL_SEC):
    """起后台线程做主动搭话调度。返回 thread。"""
    ledger = CareLedger()
    ledger.load()

    def loop():
        log(f"  [care] 调度启动：每 {poll_sec}s 问一次 core，目标={chat_id!r}")
        while True:
            try:
                q = urllib.parse.urlencode({
                    "last": ledger.last,
                    "sentToday": ledger.sent_today,
                    "active": ",".join(active_ids),
                })
                due = _get(f"/api/care/due?{q}", 30)
                if due.get("due"):
                    # core 会按它的 contacts 白名单挑 target（实测会挑到「文件传输助手」）。
                    # 但本桥接只能往「当前打开的那个会话」里打字，所以一律用 chat_id，
                    # 并把 core 选的 target 记进日志便于排查。
                    core_tgt = due.get("target")
                    tgt = chat_id
                    if core_tgt and str(core_tgt.get("wxid") or "") not in ("", chat_id):
                        log(f"  [care] core 建议 target={core_tgt.get('name')!r}，"
                            f"本桥接固定发往 {chat_id!r}")
                    log(f"  [care] 到点了（reason={due.get('reason')}），生成中…")
                    got = _get("/api/care?" + urllib.parse.urlencode(
                        {"chat": str(tgt)}), 150)
                    text = str(got.get("text") or "").strip()
                    if not text:
                        log(f"  [care] 生成失败或为空：{got.get('error') or got}")
                    else:
                        ok = send_fn(text)
                        log(f"  [care] 已发送={'OK' if ok else 'FAIL'}: {text[:50]!r}")
                        if ok:
                            ledger.record()
                else:
                    log(f"  [care] 暂不发（{due.get('reason')}，窗口={due.get('windows')}，"
                        f"今日已发 {due.get('sentToday')}/{due.get('maxPerDay')}）")
            except Exception as exc:
                log(f"  [care] 调度异常：{exc!r}")
            time.sleep(poll_sec)

    t = threading.Thread(target=loop, name="care", daemon=True)
    t.start()
    return t
