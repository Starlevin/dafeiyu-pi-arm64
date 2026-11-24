"""Read-only JSON commands for a local DSH bili-duty installation."""

import argparse
import asyncio
from collections.abc import Mapping
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import json
import os
from pathlib import Path
import re
import sys


REQUEST_TIMEOUT = 12
MAX_RECOMMENDATIONS = 5
BVID = re.compile(r"BV[A-Za-z0-9]{10}")


def load_plugin(directory):
    module_path = directory / "mcp_server.py"
    if not module_path.is_file():
        raise FileNotFoundError
    # These functions are the only tools the imported server may register.
    # The bridge calls neither the server transport nor any mutating function.
    os.environ["BILI_MCP_TOOLS"] = "bili_check_credential,bili_recommend"
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("dsh_bili_duty", module_path)
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    return plugin


def login_required(command):
    if command == "status":
        return {"ok": True, "authenticated": False}
    return {"ok": False, "authenticated": False, "error": "login_required"}


def recommendations(payload, limit):
    if isinstance(payload, Mapping):
        payload = payload.get("item", payload.get("data"))
    if not isinstance(payload, list):
        return None

    videos = {}
    for item in payload:
        if not isinstance(item, Mapping) or item.get("goto") not in (None, "", "av"):
            continue
        bvid, title = item.get("bvid"), item.get("title")
        if not isinstance(bvid, str) or not BVID.fullmatch(bvid) or not isinstance(title, str):
            continue
        title = " ".join(title[:512].split())[:160]
        if not title:
            continue
        videos.setdefault(bvid, {"bvid": bvid, "title": title})
        if len(videos) == limit:
            break
    return list(videos.values())


async def read(plugin, command, limit):
    credential = plugin.load_credential()
    if credential is None or not credential.has_sessdata():
        return login_required(command)
    expected_id = str(credential.dedeuserid)
    if not re.fullmatch(r"[1-9][0-9]*", expected_id):
        return login_required(command)
    profile = await plugin.user.get_self_info(credential)
    if not isinstance(profile, Mapping):
        return {"ok": False, "error": "upstream_response"}
    if str(profile.get("mid")) != expected_id or profile.get("isLogin") is False:
        return login_required(command)
    if command == "status":
        return {"ok": True, "authenticated": True}

    # Keep using the verified object even if the plugin rotates its file.
    payload = await plugin.homepage.get_videos(credential=credential)
    videos = recommendations(payload, limit)
    if videos is None:
        return {"ok": False, "error": "upstream_response"}
    return {"ok": True, "authenticated": True, "count": len(videos), "videos": videos}


async def run_read(plugin, command, limit):
    try:
        return await asyncio.wait_for(read(plugin, command, limit), timeout=REQUEST_TIMEOUT)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timeout"}
    except Exception as exc:
        if getattr(exc, "code", None) == -101:
            return login_required(command)
        return {"ok": False, "error": "upstream_unavailable"}


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError


def main(argv=None):
    parser = ArgumentParser(prog="bilibili-duty")
    parser.add_argument(
        "--plugin-dir",
        type=Path,
        default=Path.home() / ".dsh" / "bili-duty",
        help="existing local bili-duty installation",
    )
    commands = parser.add_subparsers(required=True, dest="command")
    commands.add_parser("status", help="verify the saved login")
    recommend = commands.add_parser("recommend", help="read a small recommendation sample")
    recommend.add_argument("--limit", type=int, choices=range(1, MAX_RECOMMENDATIONS + 1), default=3)
    try:
        args = parser.parse_args(argv)
    except ValueError:
        print('{"ok":false,"error":"invalid_arguments"}')
        return 2

    # Dependency diagnostics may contain paths, credential fields or raw
    # responses. Keep only the bridge's explicitly shaped JSON on stdout.
    with open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink), redirect_stderr(sink):
        try:
            plugin = load_plugin(args.plugin_dir)
        except Exception:
            result = {"ok": False, "error": "plugin_unavailable"}
        else:
            result = asyncio.run(run_read(plugin, args.command, getattr(args, "limit", 0)))
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
