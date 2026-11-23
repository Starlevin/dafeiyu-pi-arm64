"""Offline contract checks for the explicitly enabled local multimodal route."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import pi_vision


class FakeResponse:
    def __init__(self, value, status=200):
        self.value = value
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        return self.value[:limit]


class PiVisionModelHubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key_file = Path(self.tmp.name) / "gateway.key"
        self.key_file.write_text("fixture-gateway-key\n", encoding="utf-8")
        self.env = patch.dict(os.environ, {
            "DAFEIYU_WECHAT_VISION_BACKEND": "model-hub",
            "DAFEIYU_WECHAT_VISION_HUB_MODEL": "workbuddy-deepseek",
            "DAFEIYU_WECHAT_VISION_HUB_KEY_FILE": str(self.key_file),
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.png = pi_vision._png(1, 1, [b"\x00\x11\x22\x33"])

    def test_model_hub_sends_openai_multimodal_payload_to_fixed_loopback(self):
        captured = {}

        def open_request(req, timeout):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            captured["timeout"] = timeout
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse(json.dumps({"choices": [{
                "finish_reason": "stop",
                "message": {"content": "合成图片中有蓝色方块。"},
            }]}).encode("utf-8"))

        with patch.object(pi_vision.urllib.request, "build_opener") as build_opener:
            build_opener.return_value.open.side_effect = open_request
            result = pi_vision.describe(
                self.png, "只说颜色", timeout=9,
                trace_id="123e4567-e89b-12d3-a456-426614174000")

        self.assertEqual(result, "合成图片中有蓝色方块。")
        self.assertEqual(captured["url"], "http://127.0.0.1:18090/v1/chat/completions")
        self.assertEqual(captured["headers"]["authorization"], "Bearer fixture-gateway-key")
        self.assertEqual(captured["headers"]["x-dafeiyu-trace-id"],
                         "123e4567-e89b-12d3-a456-426614174000")
        self.assertEqual(captured["timeout"], 9)
        body = captured["body"]
        self.assertEqual(body["model"], "workbuddy-deepseek")
        self.assertFalse(body["stream"])
        self.assertEqual(body["max_tokens"], 1000)
        content = body["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "image_url")
        self.assertTrue(content[0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(content[0]["image_url"]["url"].split(",", 1)[1]), self.png)
        self.assertEqual(content[1], {"type": "text", "text": "只说颜色"})

    def test_trace_validator_matches_gateway_accepted_shapes(self):
        self.assertTrue(pi_vision.TRACE_ID_RE.fullmatch(
            "123e4567-e89b-12d3-a456-426614174000"))
        self.assertTrue(pi_vision.TRACE_ID_RE.fullmatch("a" * 64))
        self.assertFalse(pi_vision.TRACE_ID_RE.fullmatch("not-valid"))

    def test_model_hub_redirect_never_sends_credentials_to_location(self):
        redirected_requests = []
        initial_auth = []
        sink = ThreadingHTTPServer(("127.0.0.1", 0), type(
            "SinkHandler", (BaseHTTPRequestHandler,), {
                "do_POST": lambda handler: redirected_requests.append(
                    handler.headers.get("Authorization")),
                "log_message": lambda *_args: None,
            }))
        sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
        sink_thread.start()

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(handler):
                initial_auth.append(handler.headers.get("Authorization"))
                handler.send_response(302)
                handler.send_header("Location", f"http://127.0.0.1:{sink.server_port}/collect")
                handler.send_header("Content-Length", "0")
                handler.end_headers()

            def log_message(self, *_args):
                pass

        source = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        source_thread = threading.Thread(target=source.serve_forever, daemon=True)
        source_thread.start()
        try:
            with patch.object(pi_vision, "MODEL_HUB",
                              f"http://127.0.0.1:{source.server_port}/v1/chat/completions"):
                self.assertIsNone(pi_vision.describe(self.png, "prompt", timeout=3))
            self.assertEqual(initial_auth, ["Bearer fixture-gateway-key"])
            self.assertEqual(redirected_requests, [])
        finally:
            source.shutdown()
            sink.shutdown()
            source.server_close()
            sink.server_close()
            source_thread.join(timeout=2)
            sink_thread.join(timeout=2)

    def test_missing_or_invalid_model_never_sends_image(self):
        with patch.dict(os.environ, {"DAFEIYU_WECHAT_VISION_HUB_MODEL": ""}), \
             patch.object(pi_vision.urllib.request, "build_opener") as build_opener:
            self.assertIsNone(pi_vision.describe(self.png, "prompt"))
            build_opener.assert_not_called()
        with patch.dict(os.environ, {"DAFEIYU_WECHAT_VISION_HUB_MODEL": "bad model"}), \
             patch.object(pi_vision.urllib.request, "build_opener") as build_opener:
            self.assertIsNone(pi_vision.describe(self.png, "prompt"))
            build_opener.assert_not_called()

    def test_missing_gateway_key_fails_closed(self):
        with patch.dict(os.environ, {"DAFEIYU_WECHAT_VISION_HUB_KEY_FILE":
                                     str(Path(self.tmp.name) / "missing")}), \
             patch.object(pi_vision.urllib.request, "build_opener") as build_opener:
            self.assertIsNone(pi_vision.describe(self.png, "prompt"))
            build_opener.assert_not_called()

    def test_shared_judge_key_file_is_a_compatible_key_source(self):
        with patch.dict(os.environ, {
                "DAFEIYU_WECHAT_VISION_HUB_KEY_FILE": "",
                "DAFEIYU_WECHAT_JUDGE_KEY_FILE": str(self.key_file)}), \
             patch.object(pi_vision.urllib.request, "build_opener") as build_opener:
            build_opener.return_value.open.return_value = FakeResponse(json.dumps({
                "choices": [{"message": {"content": "fixture"}}],
            }).encode("utf-8"))
            self.assertEqual(pi_vision.describe(self.png, "prompt"), "fixture")
            req = build_opener.return_value.open.call_args.args[0]
            self.assertEqual(req.get_header("Authorization"), "Bearer fixture-gateway-key")

    def test_upstream_error_or_malformed_response_is_not_description(self):
        for response in (
                FakeResponse(b'{"error":{"message":"not a description"}}'),
                FakeResponse(b'{"choices":[{"message":{"content":"bad"}}]}', status=503),
                FakeResponse(b'{"choices":[{"finish_reason":"length","message":{"content":"partial"}}]}'),
                FakeResponse(b'{"choices":[{"message":{"content":[{"type":"text","text":"ambiguous"}]}}]}'),
                FakeResponse(b"not json"),
                FakeResponse(b"x" * (pi_vision.MODEL_HUB_MAX_RESPONSE + 1))):
            with self.subTest(response=response.value[:40]), \
                 patch.object(pi_vision.urllib.request, "build_opener") as build_opener:
                build_opener.return_value.open.return_value = response
                self.assertIsNone(pi_vision.describe(self.png, "prompt"))

    def test_default_route_preserves_legacy_core_compatibility(self):
        with patch.dict(os.environ, {"DAFEIYU_WECHAT_VISION_BACKEND": ""}), \
             patch.object(pi_vision.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"ok":true,"text":"legacy"}'
            self.assertEqual(pi_vision.describe(self.png, "prompt"), "legacy")
            request = urlopen.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:19301/api/vision")
            sent = json.loads(request.data.decode("utf-8"))
            self.assertEqual(base64.b64decode(sent["image"]), self.png)

    def test_unknown_backend_never_falls_back_to_either_upstream(self):
        with patch.dict(os.environ, {"DAFEIYU_WECHAT_VISION_BACKEND": "remote-api"}), \
             patch.object(pi_vision.urllib.request, "urlopen") as old_urlopen, \
             patch.object(pi_vision.urllib.request, "build_opener") as hub_opener:
            self.assertIsNone(pi_vision.describe(self.png, "prompt"))
            old_urlopen.assert_not_called()
            hub_opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
