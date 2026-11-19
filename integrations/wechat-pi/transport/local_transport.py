#!/usr/bin/env python3
"""local_transport.py — DSH 工作区 → Codex managed bridge 的本机传输适配器

=============================================================================
职责边界（严格遵守）
=============================================================================
本适配器**只做一件事**：把已核验过的 AT-SPI 事件 / 发送回执，按
`wechat-codex/README.md` 的 NDJSON 契约写到 managed bridge 的 stdin；
并把 bridge 的 `send` 请求交给发送器，把回执写回。

它**不**登录、**不**扫码、**不**读写微信数据库、**不**提取密钥、
**不**部署、**不**重启微信、**不**触碰 QQ。

=============================================================================
契约（摘自 wechat-codex/README.md §传输适配契约）
=============================================================================
适配器 → 桥 (stdin, 每行一个 JSON)：
  {"type":"event","id":<str<=128>,"event":{...}}
  {"type":"ack","id":<request.id>,"receipt":{...}}
桥 → 适配器 (stdout)：
  {"type":"ready","running":bool}
  {"type":"send","request":{...}}
  {"type":"result","id":...,"status":...}
  {"type":"error","status":...}

event 必需字段（managed-bridge.js:105-109 强校验）：
  account_id, direction="incoming", identity="stable-id",
  chat_type in {private,group}, chat_id, sender_id, message_id,
  timestamp (安全整数毫秒), text
  群聊必须提供已核验的 mentioned_ids。真 @ 包含 account_id，普通群消息用 []。

receipt 必需字段（README L33）：
  account, target, preConfirm, postConfirm, ok, messageId(出站已观察到的)

=============================================================================
核心安全策略：只放行"真正稳定"的身份
=============================================================================
managed-bridge 只检查形状（正则 `^[\\w@.:-]{1,128}$` 且非 `ui:` 前缀），
它**无法判断**该 ID 到底是微信真实 ID 还是我们编的。因此本适配器必须
自己判断，规则：

  1. `ui:` 前缀 → 一律拒绝（README L29 明确拒绝）
  2. 其余 ID 必须来自"核验过的稳定来源"：
     - 房间：微信 chatroom id（形如 `<数字>@chatroom`）或已登记的稳定映射
       （`--room-map` 文件，由人在真机核实后手工记录）
     - 成员：wxid（形如 `wxid_xxx`）或已登记映射
     - 消息：微信服务端 msg id（纯数字或 `<数字>_<数字>` 形态）
  3. **仅凭 UI 窗口标题 / 昵称 / 占位符 / 合成 ID 的一律拒绝**，
     返回明确的拒绝理由，且**不写进 bridge**。
  4. 无真实 message_id 或无目标核验的发送回执，**不得**标为确认（ok=False）。

即：本适配器是"信任闸"，不是"格式转换器"。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, TextIO

# ----------------------------------------------------------------- 拒绝理由 --
R_UI_PREFIX = "rejected_ui_derived_id"          # ui: 派生
R_NOT_STABLE = "rejected_not_stable_identity"   # 形态不像微信真实 ID
R_NO_STABLE_IDENTITY = "rejected_no_stable_identity"  # identity != stable-id
R_MISSING = "rejected_missing_field"
R_BAD_TYPE = "rejected_bad_chat_type"
R_AMBIGUOUS = "rejected_ambiguous_room"         # 同名群无法唯一定位
R_BAD_TS = "rejected_bad_timestamp"
R_NO_RECEIPT_ID = "receipt_unconfirmed_no_message_id"
R_NO_TARGET_VERIFY = "receipt_unconfirmed_no_target_check"
R_WRONG_TARGET = "receipt_wrong_target"

# 微信真实 ID 形态（实测 + 官方习惯）
CHATROOM_RE = re.compile(r"^\d+@chatroom$")     # 群 chatroom id
WXID_RE = re.compile(r"^wxid_[A-Za-z0-9_-]{3,64}$")
# 服务端消息 id：纯数字 或 数字_数字（分片场景）
MSGID_RE = re.compile(r"^\d{1,20}(?:_\d{1,10})?$")

ACCOUNT_RE = re.compile(r"^wxid_[A-Za-z0-9_-]{3,64}$|^[A-Za-z0-9_.-]{4,64}$")


# ------------------------------------------------------------------- 数据类 --
@dataclass
class Verdict:
    ok: bool
    status: str = "ok"
    detail: str = ""
    event: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        d = {"ok": self.ok, "status": self.status}
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass
class RoomRegistry:
    """人工核实后登记的 显示名 → 稳定 ID 映射（room / member）。

    文件格式（JSON）：
      {"rooms": {"群显示名": "12345678@chatroom"},
       "members": {"昵称": "wxid_abcd1234"}}
    未登记的显示名视为**无稳定标识** → 拒绝。
    """
    rooms: Dict[str, str] = field(default_factory=dict)
    members: Dict[str, str] = field(default_factory=dict)
    # 反向：稳定 ID → 显示名（用于同名群歧义检测）
    rooms_by_id: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Optional[str]) -> "RoomRegistry":
        reg = cls()
        if not path:
            return reg
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        reg.rooms = dict(raw.get("rooms") or {})
        reg.members = dict(raw.get("members") or {})
        for name, rid in reg.rooms.items():
            reg.rooms_by_id[rid] = name
        return reg


# ------------------------------------------------------------------- 信任闸 --
class TrustGate:
    """决定一个 AT-SPI 事件能否变成 managed-bridge 可接受的稳定事件。"""

    def __init__(self, account_id: str, registry: RoomRegistry,
                 now_ms: Optional[Callable[[], int]] = None):
        self.account = account_id
        self.registry = registry
        self._now = now_ms or (lambda: int(time.time() * 1000))

    # -- 单个字段的稳定性判定 ------------------------------------------------
    @staticmethod
    def _looks_ui(value: str) -> bool:
        return isinstance(value, str) and value.startswith("ui:")

    def _stable_room(self, v: str) -> bool:
        return bool(CHATROOM_RE.match(v or ""))

    def _stable_member(self, v: str) -> bool:
        return bool(WXID_RE.match(v or ""))

    def _stable_msg(self, v: str) -> bool:
        return bool(MSGID_RE.match(v or ""))

    # -- 主入口 --------------------------------------------------------------
    def verify(self, raw: Dict[str, Any]) -> Verdict:
        """把一条 AT-SPI 派生的原始记录核验成契约事件，或拒绝。"""
        if not isinstance(raw, dict):
            return Verdict(False, R_MISSING, "event is not an object")

        # 0) 明确的非稳定身份标记（来自 atspi_source）
        if raw.get("identity") != "stable-id":
            return Verdict(False, R_NO_STABLE_IDENTITY,
                           f"identity={raw.get('identity')!r}; only 'stable-id' accepted")
        if raw.get("direction") != "incoming":
            return Verdict(False, R_NOT_STABLE, "只接收明确标记为入站的消息")

        # 1) 三个 ID 必须存在
        chat_id = str(raw.get("chat_id") or "")
        sender_id = str(raw.get("sender_id") or "")
        message_id = str(raw.get("message_id") or "")
        if not (chat_id and sender_id and message_id):
            return Verdict(False, R_MISSING,
                           "需要 chat_id / sender_id / message_id")

        # 2) ui: 前缀一律拒绝
        for name, val in (("chat_id", chat_id), ("sender_id", sender_id), ("message_id", message_id)):
            if self._looks_ui(val):
                return Verdict(False, R_UI_PREFIX, f"{name}={val!r} 以 ui: 开头")

        # 3) 逐个判定是否微信真实形态
        chat_type = str(raw.get("chat_type") or "")
        if chat_type not in ("private", "group"):
            return Verdict(False, R_BAD_TYPE, f"chat_type={chat_type!r}")

        # 群聊的 chat_id 是 chatroom id；私聊的 chat_id 就是对方 wxid（README L116 也这么要求）
        if chat_type == "group":
            if not self._stable_room(chat_id):
                return Verdict(False, R_NOT_STABLE,
                               f"群 chat_id 非 chatroom id（应形如 123@chatroom），实际 {chat_id!r}")
        else:
            if not self._stable_member(chat_id):
                return Verdict(False, R_NOT_STABLE,
                               f"私聊 chat_id 非 wxid 形态，实际 {chat_id!r}")
            if chat_id != sender_id:
                return Verdict(False, R_NOT_STABLE,
                               "私聊 chat_id 必须等于 sender_id")
        if not self._stable_member(sender_id):
            return Verdict(False, R_NOT_STABLE,
                           f"sender_id 非 wxid 形态，实际 {sender_id!r}")
        if not self._stable_msg(message_id):
            return Verdict(False, R_NOT_STABLE,
                           f"message_id 非服务端 id 形态，实际 {message_id!r}")

        # 4) 明确区分未取得提及元数据、普通群消息和真 @
        mentioned = raw.get("mentioned_ids")
        if chat_type == "group":
            if not isinstance(mentioned, list):
                return Verdict(False, R_NOT_STABLE,
                               "群消息缺少已核验的 mentioned_ids")
            bad = [m for m in mentioned if not isinstance(m, str) or not self._stable_member(m)]
            if bad:
                return Verdict(False, R_NOT_STABLE,
                               f"mentioned_ids 含非 wxid 项: {bad[:3]}")
            if mentioned and self.account not in mentioned:
                return Verdict(False, R_NOT_STABLE, "群消息只提及其他成员")

        # 5) 时间戳必须是安全整数毫秒
        ts = raw.get("timestamp")
        if not isinstance(ts, int) or isinstance(ts, bool) or ts <= 0:
            return Verdict(False, R_BAD_TS, f"timestamp={ts!r}")
        if ts > 2**53 - 1:
            return Verdict(False, R_BAD_TS, "timestamp 超出安全整数")

        # 6) 账号自身一致性
        if str(raw.get("account_id") or "") != self.account:
            return Verdict(False, R_NOT_STABLE,
                           "account_id 与适配器配置不一致")

        # 7) 同名群歧义：显示名对应的 room id 不唯一 → 拒绝
        title = str(raw.get("chat_title") or "")
        if title:
            mapped = self.registry.rooms.get(title)
            if mapped and mapped != chat_id:
                return Verdict(False, R_AMBIGUOUS,
                               f"显示名 {title!r} 已登记为 {mapped}，与事件 {chat_id} 不一致")
            same_name = [rid for rid, nm in self.registry.rooms_by_id.items() if nm == title]
            if len(same_name) > 1:
                return Verdict(False, R_AMBIGUOUS,
                               f"显示名 {title!r} 对应多个 room id: {same_name}")

        # 8) 构造契约事件（只带契约允许的字段）
        event = {
            "account_id": self.account,
            "direction": "incoming",
            "identity": "stable-id",
            "chat_type": chat_type,
            "chat_id": chat_id,
            "sender_id": sender_id,
            "message_id": message_id,
            "timestamp": ts,
            "text": str(raw.get("text") or ""),
        }
        if chat_type == "group":
            event["mentioned_ids"] = mentioned
        if raw.get("reply_to_id") is not None:
            reply_to_id = str(raw["reply_to_id"])
            if not self._stable_msg(reply_to_id):
                return Verdict(False, R_NOT_STABLE, "引用消息缺少稳定 ID")
            event["reply_to_id"] = reply_to_id
        if raw.get("chat_title"):
            event["chat_title"] = str(raw["chat_title"])[:60]
        if raw.get("image_base64"):
            event["image_base64"] = raw["image_base64"]
        return Verdict(True, "ok", "", event)

    # -- 回执核验 ------------------------------------------------------------
    def verify_receipt(self, receipt: Optional[Dict[str, Any]],
                       request: Dict[str, Any]) -> Dict[str, Any]:
        """把发送器回执做确认性判定；**没有真实 messageId 或目标核验失败一律不确认**。

        返回符合 README L33 的回执：
          {account, target, preConfirm, postConfirm, ok, messageId?}
        """
        observed = receipt if isinstance(receipt, dict) else {}
        base = {
            "account": str(observed.get("account") or ""),
            "target": str(observed.get("target") or ""),
            "preConfirm": observed.get("preConfirm") is True,
            "postConfirm": observed.get("postConfirm") is True,
            "ok": False,
        }

        if not observed:
            base["status"] = "unknown_timeout"        # 交给管理层记未知状态
            return base

        obs_id = observed.get("messageId") or observed.get("message_id") or ""
        obs_id = str(obs_id)

        # 1) 必须观察到真实出站 message id
        if not obs_id or not self._stable_msg(obs_id):
            base["status"] = R_NO_RECEIPT_ID
            base["ok"] = False
            return base

        # 2) 目标核验：pre/post 都要 true，且 target 必须等于请求目标
        want_target = str(request.get("target") or "")
        if base["target"] != want_target or base["account"] != self.account:
            base["status"] = R_WRONG_TARGET
            base["ok"] = False
            base["messageId"] = obs_id
            return base
        if not (base["preConfirm"] and base["postConfirm"]):
            base["status"] = R_NO_TARGET_VERIFY
            base["ok"] = False
            base["messageId"] = obs_id
            return base

        base["ok"] = True
        base["status"] = "confirmed"
        base["messageId"] = obs_id
        return base


# --------------------------------------------------------------- 适配器主体 --
class LocalTransport:
    """按 NDJSON 契约与 managed bridge 对话。

    设计为可注入 I/O，便于用合成 fixture 测试（不需要真微信）。
    """

    def __init__(self, account: str, registry: RoomRegistry,
                 stdin: TextIO, stdout: TextIO,
                 sender: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None,
                 now_ms: Optional[Callable[[], int]] = None,
                 next_id: Optional[Callable[[], str]] = None):
        self.account = account
        self.registry = registry
        self.gate = TrustGate(account, registry, now_ms=now_ms)
        self.stdin = stdin
        self.stdout = stdout
        self.sender = sender or (lambda req: None)     # 默认不发送（fail-closed）
        self._seq = 0
        self._next_id = next_id or self._default_id
        self.requests: Dict[str, Dict[str, Any]] = {}
        self.results: List[Dict[str, Any]] = []
        self.rejections: List[Dict[str, Any]] = []
        self.ready = threading.Event()

    def _default_id(self) -> str:
        self._seq += 1
        return f"ev{self._seq}"

    # -- 输出 ---------------------------------------------------------------
    def _emit(self, obj: Dict[str, Any]) -> None:
        self.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.stdout.flush()

    # -- 入站事件 -----------------------------------------------------------
    def submit_event(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """核验并投递给 bridge；被拒的事件不写入 bridge。"""
        verdict = self.gate.verify(raw)
        if not verdict.ok:
            rec = {"status": verdict.status, "detail": verdict.detail}
            self.rejections.append(rec)
            return {"sent": False, **verdict.as_dict()}
        eid = self._next_id()
        self._emit({"type": "event", "id": eid, "event": verdict.event})
        return {"sent": True, "id": eid, "status": "ok"}

    # -- 桥的 send 请求 -----------------------------------------------------
    def handle_send(self, request: Dict[str, Any]) -> None:
        rid = str(request.get("id") or "")
        kind, _, target_id = str(request.get("target") or "").partition(":")
        valid_target = ((kind == "group" and self.gate._stable_room(target_id)) or
                        (kind == "private" and self.gate._stable_member(target_id)))
        text = request.get("text")
        if (not rid or request.get("account") != self.account or not valid_target or
                not isinstance(text, str) or not text.strip()):
            self._emit({"type": "ack", "id": rid,
                        "receipt": {"ok": False, "status": "invalid_request"}})
            return
        self.requests[rid] = request
        receipt = None
        try:
            receipt = self.sender(request)
        except Exception:
            receipt = None
        verified = self.gate.verify_receipt(receipt, request)
        self._emit({"type": "ack", "id": rid, "receipt": verified})

    # -- 主循环 -------------------------------------------------------------
    def run(self) -> int:
        for line in self.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except Exception:
                continue
            t = frame.get("type")
            if t == "ready":
                self.ready.set()
            elif t == "send":
                self.handle_send(frame.get("request") or {})
            elif t == "result":
                self.results.append({"id": frame.get("id"), "status": frame.get("status")})
            # error / 其它忽略
        return 0


# ---------------------------------------------------------------------- CLI --
def build_registry_from_args(args) -> RoomRegistry:
    reg = RoomRegistry.load(args.room_map)
    # 命令行可覆盖/补充
    for name, rid in (args.room or []):
        reg.rooms[name] = rid
        reg.rooms_by_id[rid] = name
    for name, wid in (args.member or []):
        reg.members[name] = wid
    return reg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="DSH AT-SPI → wechat-codex managed bridge 本机传输适配")
    ap.add_argument("--account", required=True, help="本账号稳定 ID（wxid_...）")
    ap.add_argument("--room-map", help="显示名→稳定 ID 的 JSON 登记表")
    ap.add_argument("--room", action="append", nargs=2, metavar=("NAME", "CHATROOM_ID"),
                    help="补充房间映射，可重复")
    ap.add_argument("--member", action="append", nargs=2, metavar=("NAME", "WXID"),
                    help="补充成员映射，可重复")
    ap.add_argument("--events", help="从 NDJSON 文件读取原始事件（而非 stdin）")
    ap.add_argument("--echo-rejections", action="store_true",
                    help="把被拒绝的事件以 JSON 打到 stderr（便于排查）")
    a = ap.parse_args(argv)

    reg = build_registry_from_args(a)
    stdin = open(a.events, "r", encoding="utf-8") if a.events else sys.stdin
    try:
        transport = LocalTransport(a.account, reg, stdin, sys.stdout)
        rc = transport.run()
        if a.echo_rejections:
            for r in transport.rejections:
                print(json.dumps(r, ensure_ascii=False), file=sys.stderr)
        return rc
    finally:
        if a.events:
            stdin.close()


if __name__ == "__main__":
    sys.exit(main())
