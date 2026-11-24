"""The native chooser and X11 may appear on different observation passes."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pi_media_reply as media


class MediaPickerTests(unittest.TestCase):
    def test_waits_for_accessibility_to_gain_its_own_x11_window(self):
        app, chooser = object(), object()
        bridge = SimpleNamespace(get_app=lambda: app)
        before = {10: (0, 0, 800, 600)}
        rect = (40, 50, 600, 400)
        pf = SimpleNamespace(find_new=Mock(side_effect=[[], [(21, rect), (22, rect)]]))
        with patch.object(media, "_visible_file_picker", return_value=(chooser, rect)), \
             patch.object(media, "_owned_picker_window", side_effect=lambda _p, w, _m: w == 22), \
             patch.object(media.time, "monotonic", side_effect=[0, .4]), \
             patch.object(media.time, "sleep") as delay:
            self.assertEqual(media._wait_for_file_picker(bridge, app, pf, 10, before, lambda box: box),
                             (22, chooser))
            delay.assert_called_once_with(.25)

    def test_unverified_or_replaced_chooser_never_supplies_an_x11_target(self):
        app, first, second = object(), object(), object()
        bridge = SimpleNamespace(get_app=lambda: app)
        rect = (40, 50, 600, 400)
        pf = SimpleNamespace(find_new=Mock(return_value=[(91, rect)]))
        with patch.object(media, "_visible_file_picker", side_effect=[(first, rect), (second, rect)]), \
             patch.object(media, "_owned_picker_window", return_value=False), \
             patch.object(media.time, "sleep"):
            self.assertEqual(media._wait_for_file_picker(bridge, app, pf, 10, {}, lambda box: box),
                             (None, None))
        with patch.object(media, "_visible_file_picker", return_value=(first, rect)), \
             patch.object(media, "_owned_picker_window", return_value=False), \
             patch.object(media.time, "monotonic", side_effect=[0, media.PICKER_WAIT_S]):
            self.assertEqual(media._wait_for_file_picker(bridge, app, pf, 10, {}, lambda box: box),
                             (None, first))

    def test_modal_name_changes_only_pass_for_the_original_composer_object(self):
        node, replacement = object(), object()
        geometry = (0, 0, 600, 100)
        original = (node, ("text", "", geometry))
        bridge = SimpleNamespace(
            _tx_composer_fp=Mock(return_value=("text", "temporarily renamed", geometry)),
        )
        self.assertTrue(media._same_composer(bridge, node, original))
        self.assertFalse(media._same_composer(bridge, replacement, original))
        bridge._tx_composer_fp.return_value = ("text", "", (9, 0, 600, 100))
        self.assertFalse(media._same_composer(bridge, node, original))

    def test_only_the_untouched_or_owned_dialog_can_be_cancelled(self):
        app, chooser, reader = object(), object(), object()
        path = Path("sticker-f6789abc/weixin-sticker.gif")
        action = SimpleNamespace(get_n_actions=lambda: 1, get_action_name=lambda _: "Press",
                                 do_action=Mock(return_value=True))
        field = SimpleNamespace(name="File name:", get_text_iface=lambda: reader)
        button = SimpleNamespace(
            name="Cancel", get_state_set=lambda: SimpleNamespace(contains=lambda _: True),
            get_action_iface=lambda: action,
        )
        bridge = SimpleNamespace(
            get_app=lambda: app, app_memory_account=Mock(return_value="same-account"),
            nm=lambda node: node.name,
            _walk_showing=lambda *_a, **_k: [(field, 0, "text"), (button, 0, "button")],
            Atspi=SimpleNamespace(Text=SimpleNamespace(get_text=Mock()),
                                  StateType=SimpleNamespace(ENABLED="enabled")),
        )
        cases = [
            ("", "ok", chooser, "same-account", True),
            ("sticker-01234567", "ok", chooser, "same-account", True),
            (str(path), "ok", chooser, "same-account", True),
            ("unrelated.pdf", "ok", chooser, "same-account", False),
            ("", "concurrent_draft", chooser, "same-account", False),
            ("", "ok", object(), "same-account", False),
            ("", "ok", chooser, "other-account", False),
        ]
        for filename, guard, current, account, expected in cases:
            with self.subTest(expected=expected, guard=guard):
                action.do_action.reset_mock()
                bridge.Atspi.Text.get_text.return_value = filename
                bridge.app_memory_account.return_value = account
                with patch.object(media, "_composer_pre_return_state", return_value=guard), \
                     patch.object(media, "_visible_file_picker", return_value=(current, (1, 2, 3, 4))):
                    self.assertEqual(media._cancel_owned_picker(
                        bridge, app, "fixture", (), chooser, path, "same-account"), expected)
                self.assertEqual(action.do_action.call_count, int(expected))
