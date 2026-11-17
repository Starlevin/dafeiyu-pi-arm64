#!/usr/bin/env python3
"""group_routes.py — 微信桥「每群选择旧 core / Swift 试点」的可回滚路由（默认旧路）。

设计要点（对应任务书）：
- 19301 既有转发层**完全不动**：`old` 路由原样走 `http://127.0.0.1:19301`。
- 默认 `old`：配置文件缺失、解析失败、未列出该群、值非法 → 一律回落 19301。
- 只有显式 `{"default":"old","groups":{"<群名>":"swift"}}` 且 `enabled:true` 的群才改走
  `swift` 端点（独立本机监听，端口由 `unified-pilot.json` 提供）。
- 「发送不明勿盲重试」：本模块只做**路由选择**，不新增任何发送重试；Swift 端点的
  `ask_core` 仍复用 `pi_bridge.ask_core` 的既有语义（仅 ConnectionRefused 重试，
  超时/HTTP/解析错立即返回），`deliver_ready` 的 `unknown` 不重试语义完全保留。
- 「不重复发送」：路由不缓存、不重放；幂等由 Swift 核心的 msg_id 去重 + 响应回放保证。
- 「记录不含消息原文」：本模块只记 route/core 两个离散值，绝不记聊正文本身。

配置文件位置（优先级）：
  1. 环境变量 `DAFEIYU_UNIFIED_ROUTES` 指向的路径
  2. `--routes-file` 参数
  3. 默认使用本机配置目录
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Dict, Optional, Tuple
from runtime_paths import config_home, runtime_home
import threading

OLD_CORE_URL = "http://127.0.0.1:19301"   # 既有契约，保留
DEFAULT_ROUTE = "old"                       # 默认仍旧路

_lock = threading.Lock()
_cache: Dict[str, Any] = {"path": None, "mtime": 0.0, "data": None}


def default_routes_file() -> Path:
    override = os.environ.get("DAFEIYU_UNIFIED_ROUTES")
    if override:
        return Path(override)
    return config_home() / "unified-routes.json"


def _load(path: Optional[Path] = None) -> Dict[str, Any]:
    """fail-closed 读取：任何异常都返回空配置（= 全部走 old）。"""
    p = path or default_routes_file()
    try:
        stat = p.stat()
    except OSError:
        return {}
    with _lock:
        c = _cache
        if c["path"] == str(p) and c["mtime"] == stat.st_mtime and isinstance(c["data"], dict):
            return c["data"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    with _lock:
        _cache.update({"path": str(p), "mtime": stat.st_mtime, "data": data})
    return data


def route_for(chat_id: str, *, config_path: Optional[Path] = None,
              swift_url: Optional[str] = None) -> Tuple[str, str]:
    """返回 (core 名, 端点 URL)。永远返回合法值，绝不抛异常。

    core ∈ {"old", "swift"}；默认与任何异常都是 ("old", OLD_CORE_URL)。
    `swift_url` 由桥从 `unified-pilot.json` 读取；读不到就用内置默认端口 19311，
    但只有 `enabled:true` 且该群显式标为 swift 时才会返回 swift。
    """
    cfg = _load(config_path)
    if not cfg.get("enabled", False):
        return DEFAULT_ROUTE, OLD_CORE_URL

    groups = cfg.get("groups")
    choice = DEFAULT_ROUTE   # 未显式列出的群永远走 old；default 字段仅作文档/兼容，不捕获未列群
    if isinstance(groups, dict):
        value = groups.get(str(chat_id))
        if isinstance(value, str) and value.lower() == "swift":
            # 「先只支持测试群」：可选 testGroups 白名单；配了就要求该群在其中。
            # 与 Swift 核心 DAFEIYU_UNIFIED_ALLOWED_CHATS 是两道独立白名单，
            # 任一不通过都回落到 old（不会把消息改道后静默丢弃）。
            test = cfg.get("testGroups")
            if test is None or (isinstance(test, list) and str(chat_id) in {str(x) for x in test}):
                choice = "swift"
    if choice == "swift":
        endpoint = swift_url or str(cfg.get("swift_url") or "http://127.0.0.1:19311")
        # 端点必须是本机 loopback，禁止把聊天路由到任意外部地址。
        if not _is_loopback(endpoint):
            return DEFAULT_ROUTE, OLD_CORE_URL
        return "swift", endpoint.rstrip("/")
    return DEFAULT_ROUTE, OLD_CORE_URL


def _is_loopback(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
                and parsed.port is not None and 1 <= parsed.port <= 65535
                and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
                and parsed.path in ("", "/"))
    except ValueError:
        return False


def swift_pilot_url() -> Optional[Tuple[str, str]]:
    """从统一试点运行时文件读取 (独立监听地址, 本机 token)；不读取任何登录凭证。

    返回 None 表示试点未运行 —— 调用方应回落 old，绝不盲连。
    """
    candidates = []
    override = os.environ.get("DAFEIYU_DATA_DIR")
    if override:
        candidates.append(Path(override) / "unified-pilot.json")
    candidates.append(runtime_home() / "config" / "unified-pilot.json")
    for p in candidates:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            port = int(data["port"])
            token = str(data["token"])
            low = str(data.get("controlURL") or "").lower()
            if not low.startswith("http://127.0.0.1:"):
                continue
            if 1 <= port <= 65535 and token:
                return f"http://127.0.0.1:{port}", token
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None
