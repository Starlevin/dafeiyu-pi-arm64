"""控制台侧的用量读取器（stdlib only）。

契约：**只读**。它只调用 model-hub 现有的 loopback 管理面
`GET http://127.0.0.1:18090/api/usage`，用现有 admin.key 授权；
从不写 model-hub 的任何文件，也从不触发任何上游模型调用。

降级策略（绝不假装有数据）：
  * 网关不可达 / 密钥不可读 / 无权限 → available=false 且带上中文原因；
  * 网关返回结构不认识 → 同样降级，不让 console 500。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

COUNTER_KEYS = ("in", "out", "total", "calls", "missing")


def _int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number >= 0 else 0


def normalize(raw):
    """把网关响应归一成 UI 合同；任何异常形状都按「不可用」处理。"""
    if not isinstance(raw, dict):
        return None
    if raw.get("available") is not True:
        return {
            "available": False,
            "reason": str(raw.get("reason") or "网关尚未启用用量统计"),
            "startedAt": None,
            "today": None,
            "days7": None,
            "since": None,
            "dayCount": 0,
            "coverage": raw.get("coverage") or {},
        }

    def window(node):
        if not isinstance(node, dict):
            return None
        providers = []
        for item in node.get("providers") or []:
            if not isinstance(item, dict):
                continue
            providers.append({
                "provider": str(item.get("provider") or "未知 provider"),
                "model": str(item.get("model") or "未知模型"),
                **{k: _int(item.get(k)) for k in COUNTER_KEYS},
            })
        totals = node.get("totals") if isinstance(node.get("totals"), dict) else {}
        return {
            "totals": {k: _int(totals.get(k)) for k in COUNTER_KEYS},
            "providers": providers,
            "empty": bool(node.get("empty", not providers)),
        }

    return {
        "available": True,
        "reason": "",
        "startedAt": _int(raw.get("startedAt")) or None,
        "today": window(raw.get("today")),
        "days7": window(raw.get("days7")),
        "since": window(raw.get("since")),
        "dayCount": _int(raw.get("dayCount")),
        "coverage": raw.get("coverage") if isinstance(raw.get("coverage"), dict) else {},
    }


class UsageAdapter:
    """从 model-hub 的 loopback 管理面读用量。"""

    def __init__(self, cfg, fixture=None):
        self.cfg = cfg or {}
        self.fixture = fixture
        self.base = str(self.cfg.get("qqHubUrl") or "http://127.0.0.1:18090").rstrip("/")
        root = self.cfg.get("qqRoot") or os.environ.get("DAFEIYU_QQ_ROOT")
        self.key_path = (Path(str(root)).expanduser() / "secrets/model-hub/admin.key"
                         if root else None)
        self.timeout = 6

    # ---- fixture（合成夹具，只在测试里用） ---------------------------------- #
    def _fixture(self):
        if not isinstance(self.fixture, dict):
            return None
        node = self.fixture.get("usage")
        return node if isinstance(node, dict) else None

    def snapshot(self):
        fixed = self._fixture()
        if fixed is not None:
            normalized = normalize(fixed)
            return normalized or {"available": False, "reason": "夹具格式不可识别"}
        if self.fixture is not None:
            return self._unavailable("夹具未提供模型用量")
        if self.key_path is None:
            return self._unavailable("尚未配置 QQ 数据目录，未读取管理密钥")
        try:
            key = self.key_path.read_text(encoding="utf-8").strip()
        except OSError:
            return self._unavailable("用量统计尚未在网关上启用（读不到管理密钥）")
        if not key:
            return self._unavailable("用量统计尚未在网关上启用（管理密钥为空）")
        request = urllib.request.Request(
            self.base + "/api/usage",
            headers={"x-model-control": key, "Origin": self.base,
                     "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read(2_000_000) or b"{}")
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                return self._unavailable("网关拒绝了用量读取（管理密钥或来源不匹配）")
            if error.code == 404:
                return self._unavailable("网关上还没有启用用量统计")
            return self._unavailable("网关暂时无法返回用量")
        except (urllib.error.URLError, OSError, ValueError):
            return self._unavailable("连不上网关，暂时读不到用量")
        normalized = normalize(body)
        return normalized or self._unavailable("网关返回的用量结构无法识别")

    @staticmethod
    def _unavailable(reason):
        return {"available": False, "reason": reason, "startedAt": None,
                "today": None, "days7": None, "since": None,
                "dayCount": 0, "coverage": {}}

    # ---- 合并到 overview ---------------------------------------------------- #
    def section(self):
        """给 /api/overview 的 usage 段：失败也返回结构，不抛异常。"""
        try:
            data = self.snapshot()
        except Exception:
            data = self._unavailable("用量统计暂不可读")
        data.setdefault("source", "model-hub /v1/chat/completions")
        return data
