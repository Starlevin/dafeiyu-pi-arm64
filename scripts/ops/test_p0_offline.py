#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线单测：微信窗口分类 + 日志轮转（不接触生产环境）。"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
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


def test_restarts_keep_recent_funnel_without_copying_private_logs():
    rot = load("logrotate_archive_test", "dafeiyu-logrotate.py")
    with tempfile.TemporaryDirectory() as directory:
        log = Path(directory) / "qq-bot.log"
        stamp = int(time.time()) - 10
        current = f"[qq_proactive_funnel] at={stamp} scope=0123456789abcdef event=confirmed reason=-\n".encode()
        old = current.replace(str(stamp).encode(), str(stamp - 86400).encode())
        private = b"QQ_CONTROL_URL=http://127.0.0.1:11111/#fixture-secret\n"
        for sample in (current + old + private, b"start 2\n", b"start 3\n", b"start 4\n"):
            log.write_bytes(sample)
            assert rot.rotate(log, starting=True).startswith("rotated_startup")
            assert not log.exists()
            assert log.with_suffix(".log.1").read_bytes() == sample
        archive = log.with_suffix(".log.3")
        assert archive.read_bytes() == current
        if os.name != "nt":
            assert archive.stat().st_mode & 0o777 == 0o600
        # A stopped rotation could have left the just-archived source in .2.
        log.with_suffix(".log.2").write_bytes(current + private)
        log.write_bytes(b"scheduled rotation\n")
        rot.MAX_BYTES = 1
        assert rot.rotate(log).startswith("rotated")
        assert archive.read_bytes() == current
        assert log.read_bytes() == b""
        print("funnel history across 4 startup rotations and timer: PASS")


def test_archive_recovers_confirmed_reply_before_original_tail_window():
    rot = load("logrotate_wide_archive_test", "dafeiyu-logrotate.py")
    rot.MAX_BYTES = 256
    rot.HISTORY_BYTES = 144
    with tempfile.TemporaryDirectory() as directory:
        log = Path(directory) / "qq-bot.log"
        stamp = int(time.time()) - 1
        receipt = f"[qq_proactive_funnel] at={stamp} scope=0123456789abcdef event=confirmed reason=-\n".encode()
        unrelated = b"private fixture unrelated to the receipt\n" * 5
        unfinished = receipt.replace(b"confirmed", b"candidate").rstrip(b"\n")
        payload = receipt + unrelated
        # Force the bounded scan to begin immediately after a newline.
        padding = b"x" * len(unfinished) + b"\n"
        width = rot.MAX_BYTES + rot.HISTORY_BYTES
        aligned_prefix = b"p" * (width - len(payload) - len(unfinished) - 1) + b"\n"
        log.with_suffix(".log.2").write_bytes(padding + payload + aligned_prefix + unfinished)
        log.write_bytes(b"restart\n")

        assert rot.rotate(log, starting=True).startswith("rotated_startup")
        assert log.with_suffix(".log.3").read_bytes() == receipt
        print("standalone receipt survives a verbose log and an aligned scan: PASS")


def test_launcher_retains_only_the_current_panel_address_in_active_log():
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        assets = root / "Resources/QQControl"
        assets.mkdir(parents=True)
        for name in ("index.html", "app.js"):
            (assets / name).write_text("fixture", encoding="utf-8")
        binary = root / "engine"
        binary.touch()
        binary.chmod(0o700)
        profile = root / "runtime-profile.sh"
        profile.write_text(
            'DAFEIYU_BASE="$HOME"\nDAFEIYU_BIN="$HOME/engine"\n',
            encoding="utf-8")
        profile.chmod(0o600)
        log = root / "run/qq-bot.log"
        env = {"PATH": os.environ["PATH"], "HOME": str(root),
               "DAFEIYU_RUNTIME_PROFILE": str(profile)}
        for n in range(4):
            binary.write_text(
                f'#!/bin/sh\nprintf "%s\\n" "QQ_CONTROL_URL=http://127.0.0.1:12345/#fixture-{n}"\n',
                encoding="utf-8")
            subprocess.run(["bash", str(HERE / "dafeiyu-launch.sh")],
                           env=env, check=True, timeout=10)
            assert log.read_text(encoding="utf-8").splitlines() == [
                f"QQ_CONTROL_URL=http://127.0.0.1:12345/#fixture-{n}"
            ]
        assert log.stat().st_mode & 0o777 == 0o600
        print("launcher keeps an isolated current panel address across restarts: PASS")


if __name__ == "__main__":
    test_classify()
    test_rotate()
    test_restarts_keep_recent_funnel_without_copying_private_logs()
    test_archive_recovers_confirmed_reply_before_original_tail_window()
    test_launcher_retains_only_the_current_panel_address_in_active_log()
