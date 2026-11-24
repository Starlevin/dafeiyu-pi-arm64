from pathlib import Path
from types import SimpleNamespace
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import read_bridge


class ReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.credential = SimpleNamespace(dedeuserid="123", has_sessdata=lambda: True)
        self.plugin = SimpleNamespace(
            load_credential=Mock(return_value=self.credential),
            user=SimpleNamespace(get_self_info=AsyncMock(return_value={"mid": 123, "name": "private"})),
            homepage=SimpleNamespace(get_videos=AsyncMock()),
        )

    async def test_status_checks_the_users_own_session(self):
        result = await read_bridge.run_read(self.plugin, "status", 0)
        self.assertEqual(result, {"ok": True, "authenticated": True})
        self.plugin.user.get_self_info.assert_awaited_once_with(self.credential)
        self.plugin.homepage.get_videos.assert_not_awaited()

    async def test_status_without_credentials_does_not_call_upstream(self):
        self.plugin.load_credential.return_value = None
        self.assertEqual(
            await read_bridge.run_read(self.plugin, "status", 0),
            {"ok": True, "authenticated": False},
        )
        self.plugin.user.get_self_info.assert_not_awaited()

    async def test_missing_cookie_or_account_rejects_before_request(self):
        for credential in (
            SimpleNamespace(dedeuserid="123", has_sessdata=lambda: False),
            SimpleNamespace(dedeuserid=None, has_sessdata=lambda: True),
        ):
            with self.subTest(credential=credential):
                self.plugin.load_credential.return_value = credential
                self.assertEqual(
                    await read_bridge.run_read(self.plugin, "recommend", 3),
                    {"ok": False, "authenticated": False, "error": "login_required"},
                )
        self.plugin.user.get_self_info.assert_not_awaited()
        self.plugin.homepage.get_videos.assert_not_awaited()

    async def test_different_account_or_logged_out_rejects_recommendation(self):
        for response in ({"mid": 789}, {"mid": 123, "isLogin": False}):
            with self.subTest(response=response):
                self.plugin.user.get_self_info.return_value = response
                result = await read_bridge.run_read(self.plugin, "recommend", 3)
                self.assertEqual(result, {"ok": False, "authenticated": False, "error": "login_required"})
        self.plugin.homepage.get_videos.assert_not_awaited()

    async def test_expired_session_has_a_safe_status(self):
        error = RuntimeError("SESSDATA=example-private")
        error.code = -101
        self.plugin.user.get_self_info.side_effect = error
        self.assertEqual(
            await read_bridge.run_read(self.plugin, "status", 0),
            {"ok": True, "authenticated": False},
        )
        self.plugin.homepage.get_videos.assert_not_awaited()

    async def test_recommendations_keep_only_public_fields_and_enforce_limit(self):
        self.plugin.homepage.get_videos.return_value = {"item": [
            {"bvid": "BV1234567890", "goto": "live", "title": "ignore"},
            {"bvid": "bad", "title": "ignore"},
            {"bvid": "BV1234567890", "goto": "av", "title": "  第一段\n公开标题 ",
             "owner": {"mid": 984, "name": "ignored"}, "rcmd_reason": {"content": "ignored"}},
            {"bvid": "BV1234567890", "title": "duplicate"},
            {"bvid": "BV1234567891", "title": " "},
            {"bvid": "BV1234567892", "title": "字" * 400, "cookie": "example-private"},
            {"bvid": "BV1234567893", "title": "over limit"},
        ]}
        result = await read_bridge.run_read(self.plugin, "recommend", 2)
        self.assertEqual(result, {
            "ok": True,
            "authenticated": True,
            "count": 2,
            "videos": [
                {"bvid": "BV1234567890", "title": "第一段 公开标题"},
                {"bvid": "BV1234567892", "title": "字" * 160},
            ],
        })
        self.plugin.load_credential.assert_called_once_with()
        self.plugin.homepage.get_videos.assert_awaited_once_with(credential=self.credential)

    async def test_empty_recommendations_are_distinct_from_a_malformed_response(self):
        self.plugin.homepage.get_videos.return_value = {"item": []}
        self.assertEqual(
            await read_bridge.run_read(self.plugin, "recommend", 3),
            {"ok": True, "authenticated": True, "count": 0, "videos": []},
        )
        self.plugin.homepage.get_videos.return_value = {"item": "example-private"}
        self.assertEqual(
            await read_bridge.run_read(self.plugin, "recommend", 3),
            {"ok": False, "error": "upstream_response"},
        )

    async def test_upstream_failure_is_redacted(self):
        self.plugin.homepage.get_videos.side_effect = RuntimeError("SESSDATA=example-private")
        self.assertEqual(
            await read_bridge.run_read(self.plugin, "recommend", 3),
            {"ok": False, "error": "upstream_unavailable"},
        )

    async def test_timeout_is_bounded(self):
        async def slow(_credential):
            await asyncio.sleep(30)

        self.plugin.user.get_self_info.side_effect = slow
        with patch.object(read_bridge, "REQUEST_TIMEOUT", 0.01):
            self.assertEqual(
                await read_bridge.run_read(self.plugin, "status", 0),
                {"ok": False, "error": "timeout"},
            )


class CommandTests(unittest.TestCase):
    def invoke(self, *args):
        return subprocess.run(
            [sys.executable, str(Path(read_bridge.__file__)), *args],
            capture_output=True, text=True, timeout=10,
        )

    def test_unavailable_plugin_outputs_only_safe_json(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke("--plugin-dir", directory, "status")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {"ok": False, "error": "plugin_unavailable"})
        self.assertEqual(result.stderr, "")

    def test_dependency_diagnostics_do_not_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin_path = Path(directory) / "mcp_server.py"
            plugin_path.write_text(
                'import os, sys\n'
                'assert os.environ["BILI_MCP_TOOLS"] == "bili_check_credential,bili_recommend"\n'
                'print("SESSDATA=example-private")\n'
                'def load_credential():\n'
                '    print("raw-qr-example", file=sys.stderr)\n'
                '    raise RuntimeError("example-private")\n',
                encoding="utf-8",
            )
            result = self.invoke("--plugin-dir", directory, "status")
            self.assertEqual(os.listdir(directory), ["mcp_server.py"])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {"ok": False, "error": "upstream_unavailable"})
        self.assertEqual(result.stderr, "")

    def test_rejects_write_commands_and_oversized_reads(self):
        for arguments in (("reply",), ("recommend", "--limit", "6")):
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stdout), {"ok": False, "error": "invalid_arguments"})
                self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
