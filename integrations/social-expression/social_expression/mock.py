from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .channels import OutboundError, check_receipt, normalize_target


@dataclass
class MockOneBot:
    """In-memory OneBot v11 recorder. No socket, no credentials, no real account."""

    expected_group_id: Optional[int] = None
    expected_user_id: Optional[int] = None
    sticker_visible: bool = True
    sent: List[Dict[str, object]] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)

    def deliver(self, payload: Dict[str, object]) -> Dict[str, object]:
        try:
            action = payload.get("action")
            params = payload.get("params")
            if not isinstance(params, dict):
                raise OutboundError("missing_params")
            if action == "send_group_msg":
                group_id = params.get("group_id")
                if not isinstance(group_id, int) or isinstance(group_id, bool) or group_id <= 0:
                    raise OutboundError("invalid_group_id")
                if self.expected_group_id is not None and group_id != self.expected_group_id:
                    raise OutboundError("wrong_group")
            elif action == "send_private_msg":
                user_id = params.get("user_id")
                if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
                    raise OutboundError("invalid_user_id")
                if self.expected_user_id is not None and user_id != self.expected_user_id:
                    raise OutboundError("wrong_user")
            else:
                raise OutboundError("unexpected_action")
            segments = params.get("message")
            if not isinstance(segments, list) or not segments:
                raise OutboundError("empty_segments")
            text_seen = False
            sticker_seen = False
            for segment in segments:
                if not isinstance(segment, dict):
                    raise OutboundError("invalid_segment")
                kind = segment.get("type")
                data = segment.get("data")
                if not isinstance(data, dict):
                    raise OutboundError("invalid_segment_data")
                if kind == "text":
                    if not str(data.get("text") or "").strip():
                        raise OutboundError("empty_text_segment")
                    text_seen = True
                elif kind == "at":
                    if not str(data.get("qq") or "").isdigit():
                        raise OutboundError("invalid_at_segment")
                elif kind == "image":
                    if not str(data.get("file") or "").startswith("sticker-id:"):
                        raise OutboundError("sticker_path_not_opaque")
                    sticker_seen = True
            if not text_seen:
                raise OutboundError("missing_text_segment")
            receipt_status = "visible_outgoing" if sticker_seen and self.sticker_visible else "confirmed"
            if sticker_seen and not self.sticker_visible:
                receipt_status = "unconfirmed"
            self.sent.append(payload)
            return {"ok": True, "reason": "accepted", "receipt": receipt_status, "sticker": sticker_seen,
                    "delivered": True,
                    "sticker_delivered": self.sticker_visible if sticker_seen else None}
        except OutboundError as error:
            self.rejected.append(error.reason)
            return {"ok": False, "reason": error.reason, "receipt": "failed", "delivered": False}


@dataclass
class MockWeChatBridge:
    """In-memory WeChat bridge recorder; mirrors tx_guard's exact-target rule."""

    expected_chat: Optional[str] = None
    sticker_visible: bool = True
    sent: List[Dict[str, object]] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)

    def deliver(self, payload: Dict[str, object]) -> Dict[str, object]:
        operations = payload.get("messages")
        if not isinstance(operations, list) or not operations:
            self.rejected.append("empty_operations")
            return {"ok": False, "reason": "empty_operations", "receipt": "failed", "delivered": False}
        delivered = False
        sticker = False
        statuses: List[str] = []
        for operation in operations:
            if not isinstance(operation, dict):
                self.rejected.append("invalid_operation")
                return {"ok": False, "reason": "invalid_operation", "receipt": "failed", "delivered": False}
            chat = operation.get("chat")
            if not isinstance(chat, str) or not chat:
                self.rejected.append("empty_chat")
                return {"ok": False, "reason": "empty_chat", "receipt": "failed", "delivered": False}
            if self.expected_chat is not None and normalize_target(chat) != normalize_target(self.expected_chat):
                self.rejected.append("wrong_chat")
                return {"ok": False, "reason": "wrong_chat", "receipt": "failed", "delivered": False}
            action = operation.get("action")
            if action == "send_text":
                if not str(operation.get("text") or "").strip():
                    self.rejected.append("empty_text")
                    return {"ok": False, "reason": "empty_text", "receipt": "failed", "delivered": False}
                statuses.append("confirmed")
                delivered = True
            elif action == "send_sticker":
                sticker = True
                if self.sticker_visible:
                    statuses.append("visible_outgoing")
                    delivered = True
                else:
                    statuses.append("unconfirmed")
            else:
                self.rejected.append("unexpected_action")
                return {"ok": False, "reason": "unexpected_action", "receipt": "failed", "delivered": False}
        self.sent.append(payload)
        return {"ok": True, "reason": "accepted", "receipt": ";".join(statuses), "sticker": sticker,
                "delivered": delivered, "sticker_delivered": self.sticker_visible if sticker else None}
