#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线单测：微信窗口分类 + 日志轮转（不接触生产环境）。"""
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(HERE / filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses 需要模块已登记
    spec.loader.exec_module(mod)
    return mod


def test_classify():
    rec = load("qq_recovery_under_test", "qq-recovery.py")
    assert rec.classify_wechat_window(None) == "unknown"
    assert rec.classify_wechat_window({"ok": False}) == "unknown"
    assert rec.classify_wechat_window({"ok": True, "windows": []}) == "unknown"
    assert rec.classify_wechat_window(
        {"ok": True, "windows": [{"w": 880, "h": 640}]}) == "logged_in"
    assert rec.classify_wechat_window(
        {"ok": True, "windows": [{"w": 280, "h": 380}]}) == "login_window"
    assert rec.classify_wechat_window(
        {"ok": True, "windows": [{"w": 880, "h": 640}, {"w": 280, "h": 380}]}) == "logged_in"
    assert rec.classify_wechat_window(
        {"ok": True, "windows": [{"w": 800, "h": 300}]}) == "unknown"
    assert rec.classify_wechat_window(
        {"ok": True, "windows": [{"w": "x", "h": 380}]}) == "unknown"
    print("classify: 8/8 PASS")


def test_rotate():
    rot = load("logrotate_under_test", "dafeiyu-logrotate.py")
    tmp = Path(tempfile.mkdtemp())
    try:
        log = tmp / "qq-bot.log"
        log.write_bytes(b"A" * 200)
        os.chmod(log, 0o600)
        rot.LOG = log
        rot.MAX_BYTES = 100
        assert rot.rotate(log) .startswith("rotated")
        assert log.exists() and log.stat().st_size == 0
        assert (tmp / "qq-bot.log.1").read_bytes() == b"A" * 200
        if os.name != "nt":
            assert oct((tmp / "qq-bot.log.1").stat().st_mode & 0o777) == "0o600"
        assert rot.rotate(log).startswith("below_limit")
        log.write_bytes(b"B" * 200)
        assert rot.rotate(log).startswith("rotated")
        assert (tmp / "qq-bot.log.1").read_bytes() == b"B" * 200
        assert (tmp / "qq-bot.log.2").read_bytes() == b"A" * 200
        print("rotate: PASS")
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    test_classify()
    test_rotate()
