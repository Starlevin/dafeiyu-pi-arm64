"""Synthetic origin box for tests: fakes every NapCat action the bridge uses.

This is the ONLY place write actions are exercised. It emulates the verified
4.18.42 contract (two-step session, {code,data} envelope, unsupported actions
raising the same error the real server produces) so the bridge's success and
failure paths can both be tested without touching the Pi.

It also records what was called, so tests can assert "no shell was spawned",
"no restart happened", etc.
"""

from __future__ import annotations

import json


class FakeBox:
    def __init__(self, version="4.18.42", logged_in=True, plugins=None,
                 qr_error=True, update_hangs=False):
        self.version = version
        self.logged_in = logged_in
        self.plugins = plugins or []
        self.qr_error = qr_error          # logged in -> GetQQLoginQrcode returns -1
        self.update_hangs = update_hangs
        self.onebot_token = True
        self.calls = []                   # [(action, params)]
        self.restarts = 0
        self.config = {
            "host": "127.0.0.1",
            "port": 6099,
            "loginRate": 10,
            "disableWebUI": False,
            "enableXForwardedFor": False,
            "accessControlMode": "none",
            "token": "synthetic-token-should-never-leave",
            "sslCert": "/synthetic/path",
        }
        self.logs = {"2026-11-11_23-00-00.000": "2026-11-11 23:00:00 ok\n"
                                                "2026-11-11 23:00:01 token=abc hidden\n"}

    # ---- NapCat surface -------------------------------------------------- #

    def action(self, action, params):
        self.calls.append((action, dict(params or {})))
        if action == "base/GetNapCatVersion":
            return {"version": self.version}
        if action == "base/QQVersion":
            return "3.2.15-40139"
        if action == "QQLogin/CheckLoginStatus":
            return {"isLogin": self.logged_in, "isOffline": not self.logged_in,
                    "coreReady": True, "loginPhase": "ready" if self.logged_in else "waiting",
                    "qrLoginAccepted": False, "qrcodeurl": "", "loginError": ""}
        if action == "QQLogin/GetQQLoginInfo":
            if not self.logged_in:
                raise _unsupported()
            return {"uin": "1234567890", "nick": "夹具账号", "online": True, "uid": "u1"}
        if action == "QQLogin/GetQQLoginQrcode":
            if self.qr_error or self.logged_in:
                raise _unsupported()
            return {"pngbase64": "iVBORw0KGgo=", "qrcode": "opaque-token"}
        if action == "QQLogin/GetQuickLoginList":
            return ["1234567890"]
        if action == "QQLogin/SetQuickLogin":
            raise _unsupported()
        if action == "WebUIConfig/GetConfig":
            return dict(self.config)
        if action == "WebUIConfig/GetClientIP":
            return {"ip": "203.0.113.9"}
        if action == "WebUIConfig/GetSSLStatus":
            return {"certExists": False, "keyExists": False, "enabled": False,
                    "certContent": "", "keyContent": ""}
        if action == "WebUIConfig/UpdateConfig":
            allowed = ("host", "port", "loginRate", "disableWebUI",
                       "enableXForwardedFor", "accessControlMode")
            changed = []
            for k, v in params.items():
                if k in allowed:
                    self.config[k] = v
                    changed.append(k)
            return {"changed": changed}
        if action == "WebUIConfig/UpdateDisableWebUI":
            self.config["disableWebUI"] = bool(params.get("disable"))
            return {}
        if action == "Log/GetLogList":
            return sorted(self.logs)
        if action == "Log/GetLog":
            name = str(params.get("name") or "")
            for key, text in self.logs.items():
                if key in name:
                    return {"content": text}
            raise _unsupported()
        if action == "Plugin/List":
            return {"pluginManagerNotFound": not bool(self.plugins),
                    "plugins": list(self.plugins), "extensionPages": []}
        if action == "Process/Restart":
            self.restarts += 1
            return {"message": "restarting"}
        raise _unsupported()

    def nap_log(self, name):
        for key, text in self.logs.items():
            if key == name or key + ".log" == name:
                return text
        raise _unsupported()

    def log(self, name):
        return ("engine started\n"
                "ws token=synthetic-secret-token connected\n"
                "ok reply sent\n")

    def watchdog(self):
        return {"engine_count": 1, "onebot_alive": True, "qq_alive": True,
                "unit_state": "active"}


def _unsupported():
    from tool_common import ToolError
    return ToolError("后台没有完成这个动作，可能是版本不支持该接口", 502, "unsupported_action")
