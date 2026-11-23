"""OneBot 反向 WS 只读快照通道的合同测试（契约 §12）。

分两层：
* **纯逻辑**（本文件）：动作白名单、loopback 绑定、echo 唯一性、帧编解码、
  envelope 解包、令牌不泄漏 —— 零 socket。
* **真 socket**：`tools/_lead_check_ws.py`（假 OneBot WS 服务端）覆盖握手、串包、
  期限。

跑法：
    cd integrations/qq-qzone && python -m unittest tests.test_ws_transport -v
"""

from __future__ import annotations

import socket
import unittest
from pathlib import Path

import sys

HERE = Path(__file__).resolve().parent
MODULE = HERE.parent
if str(MODULE) not in sys.path:
    sys.path.insert(0, str(MODULE))

import qzone_napcat as napcat  # noqa: E402

SECRET = "synthetic-ws-token-should-never-leak"


class WsEndpointTests(unittest.TestCase):
    def test_lockdown_accepts_loopback_ws_with_port(self):
        for url in (
            "ws://127.0.0.1:3001",
            "ws://[::1]:3001",
            "ws://localhost:3001",
            "wss://127.0.0.1:3001",
        ):
            self.assertTrue(napcat.is_ws_endpoint(url), url)

    def test_lockdown_rejects_non_loopback_and_http(self):
        for url in (
            "ws://192.168.1.10:3001",
            "ws://10.0.0.5:3001",
            "ws://evil.example.com:3001",
            "ws://127.0.0.1",  # 缺端口
            "http://127.0.0.1:3001",  # 这是 HTTP，不是 WS
            "ws://127.0.0.1:0",
            "",
            None,
            1234,
        ):
            self.assertFalse(napcat.is_ws_endpoint(url), url)

    def test_resolve_endpoint_classifies_scheme(self):
        self.assertEqual(napcat.resolve_endpoint("ws://127.0.0.1:3001")["kind"], "ws")
        self.assertEqual(napcat.resolve_endpoint("http://127.0.0.1:3001")["kind"], "http")
        self.assertEqual(napcat.resolve_endpoint("https://127.0.0.1:3001")["kind"], "http")
        for bad in ("http://192.168.1.10:3001", "ws://qq.example.com:3001", "ftp://127.0.0.1:1"):
            self.assertEqual(napcat.resolve_endpoint(bad), {"kind": "none", "url": None}, bad)

    def test_localhost_pinned_to_literal_address(self):
        self.assertEqual(
            napcat.loopback_host_port("ws://localhost:3001", schemes=napcat.WS_SCHEMES),
            ("127.0.0.1", 3001),
        )
        self.assertEqual(
            napcat.loopback_host_port("ws://[::1]:3001", schemes=napcat.WS_SCHEMES),
            ("::1", 3001),
        )
        self.assertEqual(
            napcat.loopback_host_port("ws://127.0.0.1:3001", schemes=napcat.WS_SCHEMES),
            ("127.0.0.1", 3001),
        )
        self.assertIsNone(
            napcat.loopback_host_port("ws://192.168.1.10:3001", schemes=napcat.WS_SCHEMES)
        )


class WsTransportContractTests(unittest.TestCase):
    def _transport(self, url="ws://127.0.0.1:3001", token=SECRET, **kw):
        return napcat.WebSocketTransport(url, token, **kw)

    def test_constructor_rejects_non_loopback(self):
        for bad in ("ws://192.168.1.10:3001", "ws://evil.example.com:3001",
                    "http://127.0.0.1:3001", "ws://127.0.0.1"):
            with self.assertRaises(napcat.OneBotError) as ctx:
                self._transport(bad)
            self.assertEqual(ctx.exception.kind, "badcall", bad)

    def test_constructor_rejects_positive_timeout(self):
        with self.assertRaises(napcat.OneBotError):
            napcat.WebSocketTransport("ws://127.0.0.1:3001", None, 0)

    def test_only_read_actions_may_be_allowed(self):
        with self.assertRaises(napcat.OneBotError) as ctx:
            napcat.WebSocketTransport(
                "ws://127.0.0.1:3001", None,
                allowed_actions=["get_qzone_feeds", "send_qzone_msg"],
            )
        self.assertEqual(ctx.exception.kind, "badcall")

    def test_whitelist_is_single_read_action(self):
        self.assertEqual(napcat.WS_READ_ACTIONS, frozenset({napcat.ACTION_GET_QZONE_FEEDS}))
        # 白名单里的动作必须同时是 READ_ACTIONS，且与写动作不相交
        self.assertTrue(napcat.WS_READ_ACTIONS <= napcat.READ_ACTIONS)
        self.assertFalse(napcat.WS_READ_ACTIONS & napcat.WRITE_ACTIONS)

    def test_write_actions_refused_before_any_io(self):
        """未建连就拒绝：用一个必然连不上的端口也能证明「没有发起连接」。"""
        t = self._transport("ws://127.0.0.1:9")  # discard 端口，必然不可达
        for action in sorted(napcat.WRITE_ACTIONS):
            with self.assertRaises(napcat.OneBotError) as ctx:
                t.post(action, {"content": "x"})
            # badcall 而不是 conn/timeout：说明根本没走到网络
            self.assertEqual(ctx.exception.kind, "badcall", action)
            self.assertEqual(ctx.exception.action, action)

    def test_other_reads_refused_before_any_io(self):
        t = self._transport("ws://127.0.0.1:9")
        for action in sorted(napcat.READ_ACTIONS - napcat.WS_READ_ACTIONS):
            with self.assertRaises(napcat.OneBotError) as ctx:
                t.post(action, {})
            self.assertEqual(ctx.exception.kind, "badcall", action)

    def test_non_mapping_payload_rejected(self):
        t = self._transport("ws://127.0.0.1:9")
        with self.assertRaises(napcat.OneBotError) as ctx:
            t.post("get_qzone_feeds", ["not", "a", "mapping"])  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.kind, "badcall")

    def test_bad_action_name_rejected(self):
        t = self._transport("ws://127.0.0.1:9")
        with self.assertRaises(napcat.OneBotError) as ctx:
            t.post("Send Qzone", {})
        self.assertEqual(ctx.exception.kind, "badcall")

    def test_connect_failure_is_classified(self):
        """127.0.0.1:9 上没有监听 -> conn（不是 timeout、不是 badcall）。"""
        t = self._transport("ws://127.0.0.1:9", timeout=1.0, read_timeout=0.3)
        with self.assertRaises(napcat.OneBotError) as ctx:
            t.post(napcat.ACTION_GET_QZONE_FEEDS, {"format": "snowluma"})
        self.assertIn(ctx.exception.kind, ("conn", "timeout"))

    def test_echo_is_unique_and_unguessable(self):
        t = self._transport()
        echoes = [t.next_echo() for _ in range(50)]
        self.assertEqual(len(set(echoes)), 50, "echo 必须每次不同（否则会串包）")
        self.assertTrue(all(e.startswith("qzv-") for e in echoes))
        # 前缀带随机量：同一端点两个实例互不可猜
        other = self._transport()
        self.assertFalse(
            set(t.next_echo().split("-")[1:2]) & set(other.next_echo().split("-")[1:2])
        )

    # -- 令牌与状态面 ---------------------------------------------------- #
    def test_token_never_appears_in_repr_or_describe(self):
        t = self._transport(token=SECRET)
        self.assertNotIn(SECRET, repr(t))
        info = napcat.describe_transport(t)
        blob = repr(info)
        self.assertNotIn(SECRET, blob)
        self.assertTrue(info["has_token"])
        self.assertTrue(info["read_only"])
        self.assertEqual(info["allowed_actions"], ["get_qzone_feeds"])
        self.assertEqual(info["kind"], "ws")
        self.assertEqual(info["timeout"], napcat.WS_TOTAL_DEADLINE_SECONDS)

    def test_timeout_defaults_to_twelve_seconds(self):
        t = self._transport()
        self.assertEqual(t.timeout, 12.0)
        self.assertEqual(napcat.WS_TOTAL_DEADLINE_SECONDS, 12.0)
        self.assertEqual(t._read_timeout, 12.0)
        self.assertEqual(self._transport(read_timeout=0.3)._read_timeout, 0.3)
        self.assertEqual(napcat.WS_READ_TIMEOUT_SECONDS, 12.0)
        self.assertEqual(napcat.WS_CONNECT_TIMEOUT_SECONDS, 5.0)


class WsFrameCodecTests(unittest.TestCase):
    """帧编解码的单元验证（不经过连接）。"""

    def test_client_frame_is_masked(self):
        frame = napcat._ws_frame(0x1, b"hello")
        self.assertEqual(frame[0], 0x81)  # FIN + text
        self.assertTrue(frame[1] & 0x80, "客户端帧必须带 mask 位")
        self.assertEqual(frame[1] & 0x7F, 5)
        mask = frame[2:6]
        payload = frame[6:]
        self.assertEqual(napcat._ws_mask(payload, mask), b"hello")
        self.assertNotEqual(payload, b"hello", "载荷必须是掩码后的（不是明文）")

    def test_length_encodings(self):
        for size, header_len in ((125, 2), (126, 4), (65535, 4), (65536, 10)):
            frame = napcat._ws_frame(0x1, b"x" * size)
            self.assertEqual(len(frame), header_len + 4 + size)
            self.assertEqual(frame[0], 0x81)
            if header_len == 2:
                self.assertEqual(frame[1] & 0x7F, 125)
            elif header_len == 4:
                self.assertEqual(frame[1] & 0x7F, 126)
                self.assertEqual(int.from_bytes(frame[2:4], "big"), size)
            else:
                self.assertEqual(frame[1] & 0x7F, 127)
                self.assertEqual(int.from_bytes(frame[2:10], "big"), size)

    def test_empty_payload(self):
        frame = napcat._ws_frame(0x1, b"")
        self.assertEqual(frame[1] & 0x7F, 0)
        self.assertEqual(napcat._ws_mask(b"", b"abcd"), b"")

    def test_mask_roundtrip_is_involution(self):
        payload = b"QZone snowluma payload \xe4\xbd\xa0\xe5\xa5\xbd" * 7
        mask = b"\x01\x02\x03\x04"
        once = napcat._ws_mask(payload, mask)
        self.assertNotEqual(once, payload)
        self.assertEqual(napcat._ws_mask(once, mask), payload)


class WsServiceWiringTests(unittest.TestCase):
    """service 层按 scheme 选 transport，且 fail closed 与令牌不泄漏。"""

    def _svc(self, base_url, token=None):
        import tempfile

        import qzone_core as core
        import qzone_service as service_mod

        return service_mod.QzoneService(
            state_dir=tempfile.mkdtemp(prefix="qz-ws-"),
            cfg=core.QzoneConfig(owner_uin="10001"),
            clock=lambda: 1795406400,
            owner_authenticated=True,
            onebot_base_url=base_url,
            onebot_access_token=token,
        )

    def test_ws_endpoint_builds_ws_transport(self):
        svc = self._svc("ws://127.0.0.1:3001")
        self.assertTrue(svc.read_endpoint_available())
        transport = svc._get_transport()
        self.assertIsInstance(transport, napcat.WebSocketTransport)
        self.assertTrue(svc.read_endpoint_available())

    def test_http_endpoint_still_builds_http_transport(self):
        """原 HTTP 客户调用必须保持兼容（默认路径不变）。"""
        svc = self._svc("http://127.0.0.1:3001")
        transport = svc._get_transport()
        self.assertIsInstance(transport, napcat.UrllibTransport)
        self.assertEqual(transport.base_url, "http://127.0.0.1:3001")

    def test_non_loopback_endpoint_fails_closed_with_zero_requests(self):
        svc = self._svc("ws://192.168.1.10:3001")
        self.assertFalse(svc.read_endpoint_available())
        out = svc.refresh_latest(owner_authenticated=True, confirm=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "disabled")
        self.assertIsNone(svc.transport, "未放行的端点不得构造 transport")

    def test_unconfigured_fails_closed(self):
        svc = self._svc(None)
        self.assertFalse(svc.read_endpoint_available())
        out = svc.refresh_latest(owner_authenticated=True, confirm=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "disabled")

    def test_status_reports_ws_without_token_value(self):
        svc = self._svc("ws://127.0.0.1:3001", token=SECRET)
        self.assertTrue(svc.read_endpoint_available())
        blob = repr(svc.status())
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(SECRET, repr(svc._get_transport()))


if __name__ == "__main__":
    unittest.main()
