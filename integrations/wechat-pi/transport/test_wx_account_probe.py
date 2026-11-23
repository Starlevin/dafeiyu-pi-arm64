import unittest
from unittest.mock import Mock

import wx_account_probe as probe


class Node:
    def __init__(self, role, name="", children=(), bounds=None, showing=True):
        self.role = role
        self.name = name
        self.children = children
        self.bounds = bounds
        self.showing = showing

    def get_name(self):
        return self.name

    def get_role_name(self):
        return self.role

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_process_id(self):
        return 123


class AccountProbeTests(unittest.TestCase):
    def inspection(self, *windows):
        return probe.inspect(Node("application", "wechat", windows),
                             lambda node: node.showing, lambda node: node.bounds)

    def test_logged_in_evidence_prevents_reading_any_pixels(self):
        chats = Node("list", "Chats", [Node("list item", "scan to log in")])
        online = self.inspection(Node("frame", children=[chats]),
                                 Node("dialog", children=[Node("label", "scan to log in")]))
        self.assertEqual(online.state, "online")
        capture = Mock()
        self.assertEqual(probe.run("qr", lambda: online, capture), {"state": "online", "canScan": False})
        capture.assert_not_called()

    def test_only_positive_bounded_scan_window_can_be_cropped(self):
        label = Node("label", "扫码登录")
        qr = Node("image", "登录二维码", bounds=(60, 94, 160, 160))
        scan = Node("frame", children=[label, qr], bounds=(20, 20, 288, 344))
        verdict = self.inspection(scan)
        self.assertEqual(verdict, probe.Login("scan", 123, (20, 20, 288, 344), (60, 94, 160, 160)))
        capture = Mock(return_value=b"fixture pixels")
        decode = Mock(return_value="https://login.weixin.qq.com/l/fixture")
        self.assertNotIn("qr", probe.run("status", lambda: verdict, capture, decode))
        capture.assert_not_called()
        self.assertIn("qr", probe.run("qr", lambda: verdict, capture, decode))
        capture.assert_called_once_with(verdict)
        self.assertFalse(self.inspection(scan, Node("frame")).region)
        self.assertFalse(self.inspection(Node("frame", children=[label], bounds=(0, 0, 800, 800))).region)

    def test_phone_confirmation_or_changed_window_never_returns_code(self):
        scan = probe.Login("scan", 123, (0, 0, 250, 300), (30, 60, 150, 150))
        confirm = self.inspection(Node("frame", children=[Node("label", "请在手机上确认登录")]))
        self.assertEqual(confirm.state, "confirm")
        source = Mock(side_effect=[scan, confirm])
        result = probe.run("qr", source, Mock(return_value=b"pixels"), Mock(return_value="synthetic"))
        self.assertEqual(result, {"state": "confirm", "canScan": False})

    def test_logged_in_navigation_is_recognized_with_another_panel_open(self):
        frame = Node("frame", children=[Node("button", "Contacts"), Node("button", "Moments")])
        self.assertEqual(self.inspection(frame), probe.Login("online"))

    def test_hidden_qr_label_and_chat_text_do_not_authorize_capture(self):
        frames = [Node("frame", children=[Node("label", "扫码登录", showing=False),
                                          Node("text", "扫码登录")], bounds=(0, 0, 280, 320))]
        self.assertEqual(self.inspection(*frames), probe.Login("unknown"))


if __name__ == "__main__":
    unittest.main()
