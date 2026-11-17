#!/usr/bin/env python3
"""pi_memory_sync.py — 把 QQ 侧「大肥鱼」记住的事实，单向同步进微信侧记忆

为什么是单向：
  QQ 的 memoryBooks 是 Swift 结构（per-chat events + compaction），
  微信 memory/ 是 Node 结构（chats + 压缩/检索），schema 完全不同。
  双向写同一份数据必然互相破坏，所以只做
      QQ 事实 --(只读导出)--> /remember --> 微信记忆
  微信学到的新事实不回写 QQ。

实现要点：
  * 从本机配置的 QQ 状态文件只读导出 memoryBooks[*].items[*].text
  * 只同步 importance >= MIN_IMPORTANCE 且是 fact/preference/event 的条目
  * 用 item.id 去重（本地 state 记录已同步的 id）
  * 通过 core 的 /remember 命令写入，带 no_reply=true 避免触发回复
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from runtime_paths import config_home, configured_path, state_home, runtime_home, configured_group

QQ_STATE = str(configured_path("DAFEIYU_QQ_STATE_FILE", runtime_home() / "data-qq" / "qq-state.json"))
WECHAT_CFG = str(configured_path("DAFEIYU_WECHAT_CONFIG_FILE", config_home() / "config.json"))
CORE = "http://127.0.0.1:19301/webhook/wechat"
STATE = str(state_home() / "memory-sync-state.json")

MIN_IMPORTANCE = 1
MAX_PER_RUN = 8
KINDS = ("fact", "preference", "event")


def _wechat_token() -> str:
    d = json.loads(open(WECHAT_CFG, encoding="utf-8").read())
    return d["gateway"]["wechat"]["inboundToken"]


def _load_state() -> set[str]:
    try:
        d = json.load(open(STATE, encoding="utf-8"))
        return set(d.get("synced") or [])
    except Exception:
        return set()


def _save_state(ids: set[str]):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"synced": sorted(ids)[-2000:]}, fh)
        os.replace(tmp, STATE)
    except Exception:
        pass


def collect_facts(limit=MAX_PER_RUN, synced: set[str] | None = None):
    """从 QQ 记忆里挑出要同步的事实，按 updatedAt 从新到旧。"""
    synced = synced or set()
    try:
        q = json.loads(open(QQ_STATE, encoding="utf-8").read())
    except Exception:
        return []
    rows = []
    for chat_key, book in (q.get("memoryBooks") or {}).items():
        for it in (book.get("items") or []):
            if not isinstance(it, dict):
                continue
            iid = str(it.get("id") or "")
            text = str(it.get("text") or "").strip()
            if not iid or not text or iid in synced:
                continue
            if str(it.get("kind") or "") not in KINDS:
                continue
            try:
                imp = int(it.get("importance") or 0)
            except Exception:
                imp = 0
            if imp < MIN_IMPORTANCE:
                continue
            try:
                upd = float(it.get("updatedAt") or 0)
            except Exception:
                upd = 0
            rows.append((upd, iid, text, chat_key, imp))
    rows.sort(reverse=True)
    return rows[:limit]


def remember(text: str, token: str, admin_id=None, admin_name=None) -> bool:
    """用 /remember 命令写进微信记忆；no_reply 避免触发一条回复。"""
    admin_id = admin_id or configured_group("ADMIN_ID")
    admin_name = admin_name or configured_group("ADMIN_NAME")
    if not admin_id or not admin_name:
        return False
    payload = {
        "chat_type": "private",
        "chat_id": admin_id,
        "sender_id": admin_id,
        "sender_name": admin_name,
        "text": f"/remember {text}",
        "at_me": True,
        "no_reply": False,   # True 会让 core 整条跳过，命令根本不执行
        "msg_id": f"memsync-{int(time.time() * 1000)}",
    }
    req = urllib.request.Request(
        CORE, data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
        headers={"content-type": "application/json", "x-bot-token": token})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
        return bool(d.get("ok"))
    except Exception:
        return False


def sync_once(log) -> int:
    token = _wechat_token()
    synced = _load_state()
    facts = collect_facts(synced=synced)
    if not facts:
        log("  [memory-sync] 没有新的可同步事实")
        return 0
    ok_n = 0
    for upd, iid, text, chat_key, imp in facts:
        # 打个来源标记，方便在微信记忆里溯源
        marked = f"[来自QQ] {text}"
        if remember(marked, token):
            synced.add(iid)
            ok_n += 1
            log(f"  [memory-sync] OK imp={imp} {text[:50]!r}")
        else:
            log(f"  [memory-sync] FAIL {text[:50]!r}")
        time.sleep(0.4)
    _save_state(synced)
    log(f"  [memory-sync] 本轮同步 {ok_n}/{len(facts)} 条，累计 {len(synced)} 条")
    return ok_n


def start(log, interval_sec=1800):
    """后台线程：每 30 分钟同步一次。"""
    import threading

    def loop():
        log(f"  [memory-sync] 启动，每 {interval_sec}s 同步一次 QQ 记忆")
        while True:
            try:
                sync_once(log)
            except Exception as exc:
                log(f"  [memory-sync] 异常：{exc!r}")
            time.sleep(interval_sec)

    t = threading.Thread(target=loop, name="memory-sync", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    import sys
    sync_once(lambda m: print(m, flush=True))
