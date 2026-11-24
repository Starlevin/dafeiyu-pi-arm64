import ctypes
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

import pi_vision


class AvatarSamplingTests(unittest.TestCase):
    def setUp(self):
        def geometry(_display, _root, _root_out, _x, _y, width, height, *_rest):
            ctypes.cast(width, ctypes.POINTER(ctypes.c_uint)).contents.value = 60
            ctypes.cast(height, ctypes.POINTER(ctypes.c_uint)).contents.value = 30
            return 1

        self.x11 = SimpleNamespace(
            od=Mock(return_value=1), cd=Mock(), rr=Mock(return_value=2),
            geo=Mock(side_effect=geometry), di=Mock(),
            gi=Mock(side_effect=lambda _d, _r, x, *_args: x + 1),
            gp=Mock(side_effect=lambda image, x, y: (x + 31 * y) if image == 1 else 99),
        )
        patcher = patch.object(pi_vision, "_lib", return_value=vars(self.x11))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pair_reuses_connection_and_matches_png_based_hashes(self):
        left, right = (0, 0, 24, 23), (35, 0, 16, 23)
        expected = []
        for rect, varied in ((left, True), (right, False)):
            png = pi_vision.crop(*rect)
            expected.append((varied, hashlib.sha256(zlib.decompress(png[41:-16])).digest()))
        self.x11.od.reset_mock()
        self.x11.cd.reset_mock()
        self.x11.gi.reset_mock()
        self.x11.di.reset_mock()
        with patch.object(pi_vision, "_png", side_effect=AssertionError("unexpected PNG")):
            self.assertEqual(pi_vision.sample_avatars(left, right), tuple(expected))
        self.x11.od.assert_called_once_with(None)
        self.x11.cd.assert_called_once_with(1)
        self.assertEqual(self.x11.gi.call_count, 2)
        self.assertEqual(self.x11.di.call_count, 2)

    def test_fails_closed_when_a_strip_would_need_clipping(self):
        self.assertEqual(pi_vision.sample_avatars((-1, 0, 16, 23), (46, 0, 16, 23)),
                         (None, None))
        self.x11.gi.assert_not_called()
        self.x11.cd.assert_called_once_with(1)

    def test_releases_images_and_connection_when_pixel_read_raises(self):
        self.x11.gp.side_effect = OSError("synthetic pixel failure")
        with self.assertRaises(OSError):
            pi_vision.sample_avatars((0, 0, 24, 23), (35, 0, 16, 23))
        self.x11.di.assert_called_once_with(1)
        self.x11.cd.assert_called_once_with(1)

    def test_missing_display_or_geometry_yields_no_evidence(self):
        rects = ((0, 0, 24, 23), (35, 0, 16, 23))
        self.x11.od.return_value = None
        self.assertEqual(pi_vision.sample_avatars(*rects), (None, None))
        self.x11.cd.assert_not_called()
        self.x11.od.return_value = 1
        self.x11.geo.side_effect = None
        self.x11.geo.return_value = 0
        self.assertEqual(pi_vision.sample_avatars(*rects), (None, None))
        self.x11.gi.assert_not_called()
        self.x11.cd.assert_called_once_with(1)
