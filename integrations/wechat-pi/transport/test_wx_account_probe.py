from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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

    def test_login_can_offer_opening_scan_page_without_reading_pixels(self):
        switch = Node("button", "Switch Account", bounds=(25, 310, 114, 25))
        window = Node("frame", children=[switch], bounds=(20, 20, 280, 380))
        login = self.inspection(window)
        self.assertEqual(login.switch, switch.bounds)
        self.assertFalse(self.inspection(window, Node("dialog")).switch)
        disabled = probe.inspect(Node("application", children=[window]),
                                 lambda node: node.showing, lambda node: node.bounds,
                                 lambda _node: False)
        self.assertFalse(disabled.switch)
        open_page, capture = Mock(), Mock()
        self.assertEqual(probe.run("status", lambda: login, capture, open_page=open_page),
                         {"state": "login", "canScan": False, "canOpenQr": True})
        open_page.assert_not_called()
        capture.assert_not_called()

    def test_qr_request_opens_scan_page_once_then_validates_capture(self):
        login = probe.Login("login", 123, (20, 20, 280, 380), switch=(25, 310, 114, 25))
        scan = probe.Login("scan", 123, (20, 20, 280, 380), (32, 65, 180, 180))
        inspect_fn = Mock(side_effect=[login, login, scan, scan])
        capture, decode, open_page = Mock(return_value=b"pixels"), Mock(return_value="fixture"), Mock(return_value=True)
        result = probe.run("qr", inspect_fn, capture, decode, open_page, sleep=Mock())
        self.assertEqual(result, {"state": "scan", "canScan": True, "qr": "fixture"})
        open_page.assert_called_once_with(login)
        capture.assert_called_once_with(scan)

    def test_window_mapping_handles_measured_offsets_and_rejects_clipping(self):
        accessibility = (351, 178, 280, 380)
        x_window = (366, 185, 292, 396)
        self.assertTrue(probe._matching_geometry(accessibility, x_window))
        self.assertFalse(probe._matching_geometry(accessibility, (366, 185, 880, 640)))
        state = probe.Login("login", 123, accessibility, switch=(354, 513, 116, 22))
        self.assertEqual(probe._drawable_region(state, x_window, state.switch), (3, 335, 116, 22))
        self.assertIsNone(probe._drawable_region(state, (366, 185, 292, 350), state.switch))

    def test_open_page_requires_same_window_under_pointer_and_fresh_login(self):
        state = probe.Login("login", 123, (351, 178, 280, 380), switch=(354, 513, 116, 22))
        pointer = (427, 531, 777)
        fake_input = SimpleNamespace(move=Mock(), click=Mock(), pointer=Mock(return_value=pointer))
        with patch.dict("os.environ", {"DISPLAY": ":99"}), patch.dict("sys.modules", {"pi_xinput": fake_input}), \
             patch.object(probe, "_x_display", return_value=nullcontext((object(), object()))), \
             patch.object(probe, "_login_drawable", return_value=(777, (366, 185, 292, 396))), \
             patch.object(probe, "native_status", return_value=probe.Login("online")) as native:
            self.assertFalse(probe.open_scan_page(state))
            native.return_value = state
            fake_input.pointer.return_value = (427, 531, 999)
            self.assertFalse(probe.open_scan_page(state))
            fake_input.click.assert_not_called()
            fake_input.pointer.return_value = pointer
            self.assertTrue(probe.open_scan_page(state))
            fake_input.click.assert_called_once_with(427, 531, settle=0.1)


if __name__ == "__main__":
    unittest.main()
