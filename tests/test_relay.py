#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline regression tests for the relay data path, CLI upstream port and
``/api/key`` rollback.

Isolation rules:
  * every listener is an ephemeral port on a loopback address; the live 8787
    is never bound or dialled;
  * the real ``RelayServer`` / ``RelayHandler`` serve every forwarding test;
  * the only fake upstreams are small stdlib HTTP servers on 127.0.0.1 or
    127.0.0.2, selected explicitly through ``Config`` / ``--upstream``;
  * no external host, real key or WorkBuddy configuration is touched.  The
    ``/api/key`` test redirects .env, models.json and backup paths into a
    TemporaryDirectory and fakes only ``fetch_usage``.

The relay is not started through ``main`` except in the CLI-port test; that
test uses ``--port 0`` and a private token/pid/log directory.
"""

from __future__ import annotations

import contextlib
from concurrent.futures import ThreadPoolExecutor
import http.client
import importlib
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PROXY_SOURCE = PROJECT_ROOT / "opencode-go-proxy.py"
RELAY_MODULE_NAME = "ocgo_proxy_relay_under_test"

FAKE_KEY = "oc_sk_relay_fake_0000000000000000"
OLD_KEY = "oc_sk_relay_old_00000000000000000"
CLIENT_KEY = "wb-relay-client-fake"
API_TOKEN = "unittest-relay-token"
MODEL = "hy4-flash-free"
DEFAULT_PORT = 8787

JSON_BODY = json.dumps(
    {
        "id": "chatcmpl-json",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
    },
    separators=(",", ":"),
).encode("utf-8")

RATE_LIMIT_BODY = json.dumps(
    {"error": {"type": "rate_limit", "message": "slow down"}},
    separators=(",", ":"),
).encode("utf-8")

BREAK_CHUNK = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'


def _load_proxy_module():
    spec = importlib.util.spec_from_file_location(RELAY_MODULE_NAME, PROXY_SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {PROXY_SOURCE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[RELAY_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _sse_tool_chunks() -> list[bytes]:
    """SSE with one event split mid-JSON and tool arguments split by event."""
    events = [
        {
            "id": "chatcmpl-sse",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"ci',
                                },
                            }
                        ],
                    },
                }
            ],
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": 'ty":"Pa'}}
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": 'ris"}'}}
                        ]
                    },
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    chunks: list[bytes] = []
    for event in events:
        raw = ("data: " + json.dumps(event, separators=(",", ":")) + "\n\n").encode()
        if not chunks:
            cut = max(1, len(raw) // 2)
            chunks.extend([raw[:cut], raw[cut:]])
        else:
            chunks.append(raw)
    chunks.append(b"data: [DONE]\n\n")
    return chunks


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        owner = self.server.owner
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length)
        owner.record(self.command, self.path,
                     {k.lower(): v for k, v in self.headers.items()}, body)
        mode = owner.next_mode()
        if mode == "timeout":
            time.sleep(owner.hang_seconds)
            self.close_connection = True
            return
        if mode == "break":
            self._send_chunks([BREAK_CHUNK], terminate=False)
            return
        if mode == "delayed_json":
            owner.delayed_started.set()
            if not owner.release_delayed.wait(5):
                self.close_connection = True
                return
        if mode == "sse":
            self._send_chunks(owner.sse_chunks, terminate=True)
            return
        if mode == "429":
            self._send(429, RATE_LIMIT_BODY, "application/json",
                       {"Retry-After": "7"})
            return
        self._send(200, owner.json_body, "application/json")

    def _send(self, status, data, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()
        self.close_connection = True

    def _send_chunks(self, chunks, terminate):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(b"%x\r\n" % len(chunk))
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
        self.wfile.flush()
        if terminate:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        self.close_connection = True


class _UpstreamServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def __init__(self, address, owner):
        self.owner = owner
        super().__init__(address, _UpstreamHandler)


class _FakeUpstream:
    def __init__(self, address="127.0.0.1", modes=None, sse_chunks=None, json_body=JSON_BODY):
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.modes = list(modes or [])
        self.sse_chunks = list(sse_chunks or _sse_tool_chunks())
        self.json_body = json_body
        self.delayed_started = threading.Event()
        self.release_delayed = threading.Event()
        self.hang_seconds = 0.8
        self.server = _UpstreamServer((address, 0), self)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="ocgo-fake-upstream",
            daemon=True,
        )
        self.thread.start()

    def record(self, method, path, headers, body):
        with self.lock:
            self.requests.append(
                {"method": method, "path": path, "headers": headers, "body": body})

    def next_mode(self):
        with self.lock:
            if self.modes:
                return self.modes.pop(0)
        return "json"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


CONFIG_ATTRS = (
    "port",
    "scheme",
    "host",
    "upstream_port",
    "base_path",
    "session_mode",
    "fixed_session",
    "user_agent",
    "fallback_key",
    "close_delimited",
    "log",
    "api_token",
    "upstream_mode",
    "proxy_url",
    "models_cache",
    "models_count",
    "requests_total",
    "recent_responses",
)
_MISSING = object()


class _RelayTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PROXY_SOURCE.is_file():
            raise unittest.SkipTest(f"missing {PROXY_SOURCE}")
        cls.oc = importlib.import_module("ocgo_common")
        cls.proxy = _load_proxy_module()

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="ocgo-relay-test-")
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)

        config = self.proxy.Config
        self._saved_config = {
            name: config.__dict__.get(name, _MISSING) for name in CONFIG_ATTRS
        }

        def restore_config():
            for name, value in self._saved_config.items():
                if value is _MISSING:
                    if name in config.__dict__:
                        delattr(config, name)
                else:
                    setattr(config, name, value)

        self.addCleanup(restore_config)

        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        self._stack.enter_context(
            mock.patch.object(self.proxy.RequestLog, "_emit_console",
                              staticmethod(lambda text: None)))
        self._stack.enter_context(
            mock.patch.object(self.proxy, "safe_print", lambda *a, **k: None))
        config.log = self.proxy.RequestLog(str(self.root / "logs"))

    def _start_upstream(self, address="127.0.0.1", **kwargs):
        upstream = _FakeUpstream(address=address, **kwargs)
        self.addCleanup(upstream.stop)
        return upstream

    def _start_relay(self):
        server = self.proxy.RelayServer(("127.0.0.1", 0), self.proxy.RelayHandler)
        port = server.server_address[1]
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="ocgo-relay-test",
            daemon=True,
        )
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        # Ephemeral ports only; never touch the user's live relay.
        self.assertNotEqual(port, DEFAULT_PORT)
        self.relay_port = port
        self.proxy.Config.port = port
        return port

    def _serve(self, upstream=None):
        config = self.proxy.Config
        config.scheme = "http"
        config.host = upstream.host if upstream else "127.0.0.1"
        config.upstream_port = upstream.port if upstream else 1
        config.base_path = "/inference/go"
        config.session_mode = "auto"
        config.fixed_session = "ses_relay_fallback"
        config.user_agent = self.proxy.DEFAULT_UA
        config.fallback_key = FAKE_KEY
        config.close_delimited = False
        config.upstream_mode = "off"
        config.proxy_url = None
        config.api_token = API_TOKEN
        config.models_cache = None
        config.models_count = 0
        config.requests_total = 0
        config.recent_responses = ()
        return self._start_relay()

    def _request(self, method, path, body=None, headers=None, port=None,
                 timeout=5.0):
        conn = http.client.HTTPConnection(
            "127.0.0.1", port or self.relay_port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            data = response.read()
            return (
                response.status,
                {k.lower(): v for k, v in response.getheaders()},
                data,
            )
        finally:
            conn.close()

    def _post_chat(self, payload, headers=None, port=None, timeout=5.0):
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        merged = {"Content-Type": "application/json"}
        merged.update(headers or {})
        return self._request("POST", "/v1/chat/completions", data, merged,
                             port=port, timeout=timeout)


class RelayDataPathTest(_RelayTestBase):
    def test_json_response_is_forwarded_once(self):
        upstream = self._start_upstream()
        self._serve(upstream)
        payload = {
            "model": MODEL,
            "messages": [{"role": "user", "content": "hello json"}],
            "stream": False,
            "tools": [{"type": "function", "function": {"name": "get_weather"}}],
        }
        status, headers, data = self._post_chat(
            payload, {"Authorization": f"Bearer {CLIENT_KEY}"})

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "application/json")
        self.assertEqual(data, JSON_BODY)
        self.assertEqual(len(upstream.requests), 1,
                         "relay must not issue a hidden duplicate upstream call")

        request = upstream.requests[0]
        self.assertEqual(request["path"],
                         "/inference/go/openai/v1/chat/completions")
        self.assertEqual(request["body"],
                         json.dumps(payload, separators=(",", ":")).encode())
        self.assertEqual(request["headers"].get("authorization"),
                         f"Bearer {CLIENT_KEY}")
        self.assertEqual(request["headers"].get("host"),
                         f"{upstream.host}:{upstream.port}")
        self.assertEqual(request["headers"].get("user-agent"),
                         self.proxy.DEFAULT_UA)
        self.assertTrue(
            request["headers"].get("x-opencode-session", "").startswith("ses_"))

    def test_same_conversation_keeps_session_and_new_one_changes_it(self):
        upstream = self._start_upstream()
        self._serve(upstream)
        base = [{"role": "user", "content": "conversation alpha"}]
        same = base + [
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "follow up"},
        ]
        other = [{"role": "user", "content": "conversation beta"}]

        for messages in (base, same, other):
            status, _, _ = self._post_chat({"model": MODEL, "messages": messages})
            self.assertEqual(status, 200)
        status, _, _ = self._post_chat(
            {"model": MODEL, "messages": base},
            {"x-opencode-session": "ses_explicit_client"})
        self.assertEqual(status, 200)
        self.assertEqual(len(upstream.requests), 4)

        sessions = [item["headers"].get("x-opencode-session")
                    for item in upstream.requests]
        self.assertEqual(sessions[0], sessions[1])
        self.assertNotEqual(sessions[0], sessions[2])
        self.assertEqual(sessions[3], "ses_explicit_client")

    def test_sse_tool_call_fragments_and_done_are_passed_through(self):
        upstream = self._start_upstream(modes=["sse"])
        self._serve(upstream)
        status, headers, data = self._post_chat(
            {
                "model": MODEL,
                "messages": [{"role": "user", "content": "weather in Paris"}],
                "stream": True,
                "tools": [{"type": "function", "function": {"name": "get_weather"}}],
            },
            {"Accept": "text/event-stream"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "text/event-stream")
        self.assertEqual(headers.get("transfer-encoding"), "chunked")
        text = data.decode("utf-8")
        self.assertEqual(text, b"".join(upstream.sse_chunks).decode("utf-8"))
        self.assertEqual(len(upstream.requests), 1)

        events = [line[6:] for line in text.splitlines()
                  if line.startswith("data: ")]
        self.assertEqual(events[-1], "[DONE]")
        arguments = ""
        for event in events[:-1]:
            payload = json.loads(event)
            for choice in payload.get("choices", []):
                for call in choice.get("delta", {}).get("tool_calls", []):
                    arguments += call.get("function", {}).get("arguments", "")
        self.assertEqual(json.loads(arguments), {"city": "Paris"})

    def _recent(self):
        status, _, body = self._request(
            "GET", "/api/status", headers={"X-OCGO-Token": API_TOKEN})
        self.assertEqual(status, 200)
        fields = dict(field.split("=", 1) for field in body.decode().split() if "=" in field)
        return [(fields[f"{prefix}_model"], fields[f"{prefix}_tok_s"],
                 fields[f"{prefix}_ttft_s"], fields[f"{prefix}_receive_s"])
                for prefix in ("latest", "recent_2", "recent_3")
                if fields.get(f"{prefix}_model")]

    def _latest(self):
        recent = self._recent()
        return recent[0] if recent else ("", "", "", "")

    def _wait_latest(self, model):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            actual, rate, ttft, duration = self._latest()
            if actual == model:
                return rate, ttft, duration
            time.sleep(0.01)
        self.fail(f"latest completed model did not become {model!r}")

    def test_sse_reports_usage_without_changing_the_upstream_response(self):
        usage = [b'data: {"choices": [],\r\n', b'data: "usage": {"completion_tokens": 2',
                 b'4}}\r\n\r\n']
        chunks = _sse_tool_chunks()
        chunks[-1:-1] = usage
        upstream = self._start_upstream(modes=["sse", "sse"], sse_chunks=chunks)
        self._serve(upstream)
        self.assertEqual(self._latest(), ("", "", "", ""))

        request = {"model": MODEL, "messages": [], "stream": True,
                   "stream_options": {"include_obfuscation": False}}
        status, _, body = self._post_chat(request)
        self.assertEqual((status, body), (200, b"".join(chunks)))
        expected = {**request, "stream_options": {"include_obfuscation": False,
                                                  "include_usage": True}}
        self.assertEqual(json.loads(upstream.requests[0]["body"]), expected)
        rate, ttft, duration = self._wait_latest(MODEL)
        self.assertGreater(float(rate), 0.0)
        self.assertGreaterEqual(float(ttft), 0.0)
        self.assertGreater(float(duration), 0.0)
        self.assertAlmostEqual(float(rate), 23 / float(duration), delta=0.051)

        request["model"] = "explicit-options"
        request["stream_options"]["include_usage"] = False
        self._post_chat(request)
        self.assertEqual(upstream.requests[1]["body"],
                         json.dumps(request, separators=(",", ":")).encode())
        self.assertEqual(len(upstream.requests), 2)

    def test_without_usage_the_model_updates_and_selftest_or_errors_do_not_replace_it(self):
        upstream = self._start_upstream(modes=["sse", "429", "json", "sse"])
        self._serve(upstream)
        self._post_chat({"model": "no-usage", "stream": True})
        rate, ttft, duration = self._wait_latest("no-usage")
        self.assertEqual(rate, "")
        self.assertGreaterEqual(float(ttft), 0.0)
        self._post_chat({"model": "failed"})
        self._post_chat({"model": "selftest"}, {"X-OCGO-Selftest": "1"})
        self._post_chat({"model": "selftest", "stream": True}, {"X-OCGO-Selftest": "1"})
        self.assertEqual(self._latest(), ("no-usage", "", ttft, duration))
        self.assertNotIn("x-ocgo-selftest", upstream.requests[2]["headers"])
        self.assertNotIn("stream_options", json.loads(upstream.requests[3]["body"]))

    def test_concurrent_requests_use_completion_order(self):
        response = json.loads(JSON_BODY)
        response["usage"] = {"completion_tokens": 20}
        upstream = self._start_upstream(modes=["delayed_json", "json"],
                                        json_body=json.dumps(response).encode())
        self._serve(upstream)
        with ThreadPoolExecutor(max_workers=1) as worker:
            first = worker.submit(self._post_chat, {"model": "first-started"})
            try:
                self.assertTrue(upstream.delayed_started.wait(2))
                self.assertEqual(self._post_chat({"model": "second-started"})[0], 200)
                self.assertEqual(self._wait_latest("second-started"), ("", "", ""))
            finally:
                upstream.release_delayed.set()
            self.assertEqual(first.result()[0], 200)
        self.assertEqual(self._wait_latest("first-started"), ("", "", ""))
        self.assertEqual([model for model, *_ in self._recent()],
                         ["first-started", "second-started"])

    def test_recent_requests_keep_three_rows_including_repeated_models(self):
        upstream = self._start_upstream()
        self._serve(upstream)
        for model in ("oldest", "repeat", "middle", "repeat"):
            self.assertEqual(self._post_chat({"model": model})[0], 200)
            self.assertEqual(self._wait_latest(model), ("", "", ""))
        self.assertEqual(self._recent(), [("repeat", "", "", ""), ("middle", "", "", ""),
                                          ("repeat", "", "", "")])

    def test_reasoning_and_stream_trailers_do_not_reduce_output_rate(self):
        for usage in (
            {"completion_tokens": 1000, "completion_tokens_details": {"reasoning_tokens": 901}},
            {"completion_tokens": 1000, "opencode": {"tokens": {"output": 1000, "reasoning": 901}}},
        ):
            with self.subTest(usage=usage):
                stream = self.proxy.StreamUsage()
                stream.feed(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n', 1.0)
                stream.feed(b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\n', 2.0)
                self.assertEqual(stream.metrics(0.0), (None, None, None))
                stream.feed(b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n', 8.0)
                stream.feed(b'data: {"choices":[{"delta":{"content":"last"}}]}\n\n', 10.0)
                stream.feed(b'data: ' + json.dumps({"usage": usage}).encode() + b"\n\n", 12.0)
                stream.feed(b"data: [DONE]\n\n", 13.0)
                self.assertEqual(stream.metrics(0.0), (49.0, 8.0, 2.0))

    def test_receive_duration_is_available_without_separate_reasoning_counts(self):
        frames = [
            b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"last"}}]}\n\n',
        ]
        scenarios = [
            [(1.0, b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\n'),
             (2.0, frames[0]), (3.0, frames[1])],
            [(2.0, b"".join(frames))],
        ]
        for events, duration in zip(scenarios, (1.0, None)):
            with self.subTest(events=events):
                stream = self.proxy.StreamUsage()
                for timestamp, data in events:
                    stream.feed(data, timestamp)
                stream.feed(b'data: {"usage":{"completion_tokens":24}}\n\n', 4.0)
                self.assertEqual(stream.metrics(0.0), (None, 2.0, duration))

    def test_submillisecond_output_arrivals_keep_the_calculated_rate(self):
        stream = self.proxy.StreamUsage()
        stream.feed(b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n', 2.0)
        stream.feed(b'data: {"choices":[{"delta":{"content":"last"}}]}\n\n', 2.0 + 1 / 8192)
        usage = {"prompt_tokens": 2341, "completion_tokens": 10,
                 "completion_tokens_details": {"reasoning_tokens": 0}}
        stream.feed(b"data: " + json.dumps({"usage": usage}).encode() + b"\n\n", 2.5)
        self.assertEqual(stream.metrics(0.0), (73728.0, 2.0, 1 / 8192))

    def test_stream_metrics_for_messages_and_responses(self):
        fixtures = [
            ("/v1/messages", [
                {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "think"}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "first"}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "last"}},
                {"type": "message_delta", "usage": {"output_tokens": 19,
                                                    "output_tokens_details": {"thinking_tokens": 10}}},
                {"type": "message_stop"},
            ], 8.0),
            ("/v1/responses", [
                {"type": "response.reasoning_summary_text.delta", "delta": "think"},
                {"type": "response.output_text.delta", "delta": "first"},
                {"type": "response.output_text.delta", "delta": "last"},
                {"type": "response.completed", "response": {"usage": {"output_tokens": 22,
                    "output_tokens_details": {"reasoning_tokens": 9}}}},
            ], 12.0),
        ]
        for path, events, expected_rate in fixtures:
            with self.subTest(path=path):
                chunks = [b"data: " + json.dumps(event).encode() + b"\n\n" for event in events]
                parser = self.proxy.StreamUsage()
                for timestamp, chunk in enumerate(chunks, start=1):
                    parser.feed(chunk, timestamp)
                self.assertEqual(parser.metrics(0.0), (expected_rate, 2.0, 1.0))

                upstream = self._start_upstream(modes=["sse"], sse_chunks=chunks)
                self._serve(upstream)
                body = b'{"model":"native-stream","stream":true}'
                status, _, result = self._request(
                    "POST", path, body, {"Content-Type": "application/json"})
                self.assertEqual((status, result), (200, b"".join(chunks)))
                rate, ttft, duration = self._wait_latest("native-stream")
                self.assertGreater(float(duration), 0.0)
                self.assertGreater(float(rate), 0.0)
                self.assertGreaterEqual(float(ttft), 0.0)
                self.assertEqual(upstream.requests[0]["body"], body)

    def test_429_status_and_retry_after_survive(self):
        upstream = self._start_upstream(modes=["429"])
        self._serve(upstream)
        status, headers, data = self._post_chat(
            {"model": MODEL, "messages": [{"role": "user", "content": "429"}]})

        self.assertEqual(status, 429)
        self.assertEqual(headers.get("retry-after"), "7")
        self.assertEqual(data, RATE_LIMIT_BODY)
        self.assertEqual(len(upstream.requests), 1)

    def test_timeout_and_stream_break_then_next_request_succeeds(self):
        upstream = self._start_upstream(modes=["timeout", "break", "json"])
        self._serve(upstream)
        payload = {"model": MODEL, "messages": [{"role": "user", "content": "flaky"}]}

        with mock.patch.object(self.proxy, "READ_TIMEOUT", 0.25):
            status, _, data = self._post_chat(payload)
            self.assertEqual(status, 504)
            self.assertEqual(json.loads(data)["error"]["type"], "UpstreamTimeout")

            # A backend that dies mid-SSE yields a truncated stream, not a
            # silent retry.  The next client request must still work.
            conn = http.client.HTTPConnection(
                "127.0.0.1", self.relay_port, timeout=5.0)
            try:
                conn.request(
                    "POST", "/v1/chat/completions",
                    body=json.dumps(payload, separators=(",", ":")).encode(),
                    headers={"Content-Type": "application/json"})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                try:
                    partial = response.read()
                except http.client.IncompleteRead as exc:
                    partial = exc.partial
            finally:
                conn.close()
            self.assertNotIn(b"[DONE]", partial)

            status, _, data = self._post_chat(payload)
            self.assertEqual(status, 200)
            self.assertEqual(data, JSON_BODY)

        self.assertEqual(len(upstream.requests), 3,
                         "each client request must map to exactly one upstream call")


class UpstreamRoutingTest(_RelayTestBase):
    class _Conn:
        def __init__(self, host, port, timeout=None):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.sock = mock.Mock()
            self.tunnel = None
            self.closed = False

        def connect(self):
            return None

        def set_tunnel(self, host, port, headers=None):
            self.tunnel = (host, port, headers)

        def close(self):
            self.closed = True

    def _install_connections(self):
        created = []

        def factory(host, port, timeout=None):
            conn = self._Conn(host, port, timeout)
            created.append(conn)
            return conn

        self._stack.enter_context(
            mock.patch.object(http.client, "HTTPSConnection", side_effect=factory))
        self._stack.enter_context(
            mock.patch.object(http.client, "HTTPConnection", side_effect=factory))
        return created

    def test_https_default_port_and_explicit_http_port(self):
        created = self._install_connections()
        config = self.proxy.Config
        handler = self.proxy.RelayHandler

        config.scheme = "https"
        config.host = "opencode.ai"
        config.upstream_port = 0
        handler._open_upstream(None, None)
        self.assertEqual((created[-1].host, created[-1].port),
                         ("opencode.ai", 443))

        config.scheme = "http"
        config.host = "127.0.0.2"
        config.upstream_port = 45678
        handler._open_upstream(None, None)
        self.assertEqual((created[-1].host, created[-1].port),
                         ("127.0.0.2", 45678))

    def test_proxy_tunnel_keeps_default_upstream_port(self):
        created = self._install_connections()
        config = self.proxy.Config
        config.scheme = "https"
        config.host = "opencode.ai"
        config.upstream_port = 0

        self.proxy.RelayHandler._open_upstream(None, "http://127.0.0.1:7897")
        conn = created[-1]
        self.assertEqual((conn.host, conn.port), ("127.0.0.1", 7897))
        self.assertEqual(conn.tunnel, ("opencode.ai", 443, {}))

    def test_host_header_adds_only_nondefault_port(self):
        config = self.proxy.Config
        dummy = SimpleNamespace(headers={})

        config.scheme = "https"
        config.host = "opencode.ai"
        config.upstream_port = 0
        headers = self.proxy.RelayHandler._upstream_headers(dummy, "ses_test")
        self.assertEqual(headers["host"], "opencode.ai")

        config.scheme = "http"
        config.host = "127.0.0.2"
        config.upstream_port = 45678
        headers = self.proxy.RelayHandler._upstream_headers(dummy, "ses_test")
        self.assertEqual(headers["host"], "127.0.0.2:45678")


class CliUpstreamPortTest(_RelayTestBase):
    def setUp(self):
        super().setUp()
        self.sync_calls = []

        def fake_sync(key, **kwargs):
            self.sync_calls.append({"key": key, **kwargs})
            return {
                "added": 1, "updated": 0, "removed": 0, "rows": 1,
                "written": 1, "kept": 0, "prefix": "[OC] ",
                "free_prefix": "[OC-FREE] ", "conflicts": [],
            }

        self._stack.enter_context(
            mock.patch.object(self.oc, "managed_count", lambda *a, **k: 0))
        self._stack.enter_context(
            mock.patch.object(self.oc, "models_load_state",
                              lambda: {"recovered": False, "backup": None,
                                       "error": None}))
        self._stack.enter_context(mock.patch.object(self.oc, "sync_models", fake_sync))
        self._main_thread = None
        self._main_result = []
        self._main_port = None
        self._main_token = ""
        self.addCleanup(self._stop_main)

    def _start_main(self, upstream_url):
        token_file = self.root / "api.token"
        argv = [
            "--port", "0",
            "--upstream", upstream_url,
            "--env-file", str(self.root / ".env"),
            "--log-dir", str(self.root / "logs"),
            "--pid-file", str(self.root / "proxy.pid"),
            "--token-file", str(token_file),
            "--upstream-proxy", "off",
            "--key", FAKE_KEY,
            "--no-color",
        ]
        self.proxy.Config.port = 0
        self._main_thread = threading.Thread(
            target=lambda: self._main_result.append(self.proxy.main(argv)),
            name="ocgo-cli-main",
            daemon=True,
        )
        self._main_thread.start()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self._main_result:
                self.fail(f"main exited early with {self._main_result[0]}")
            token = ""
            try:
                token = token_file.read_text(encoding="ascii").strip()
            except OSError:
                pass
            port = self.proxy.Config.port
            if token and port and port != DEFAULT_PORT:
                try:
                    status, _, data = self._request(
                        "GET", "/healthz", port=port, timeout=0.5)
                except OSError:
                    status, data = 0, b""
                if status == 200 and json.loads(data)["port"] == port:
                    self._main_port = port
                    self._main_token = token
                    return port, token
            time.sleep(0.02)
        self.fail("CLI relay did not become ready on its OS-assigned port")

    def _stop_main(self):
        thread = self._main_thread
        if thread and thread.is_alive() and self._main_port:
            try:
                self._request(
                    "POST", "/api/shutdown", headers={
                        "x-ocgo-token": self._main_token},
                    port=self._main_port)
            except OSError:
                pass
            thread.join(timeout=5)
        if thread and thread.is_alive():
            self.fail("CLI relay did not stop")

    def _api(self, method, path, body=None):
        headers = {"x-ocgo-token": self._main_token}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        return self._request(method, path, body, headers, port=self._main_port)

    def test_explicit_upstream_port_and_os_assigned_relay_port(self):
        first = self._start_upstream(address="127.0.0.1")
        try:
            second = self._start_upstream(address="127.0.0.2")
        except OSError as exc:
            self.skipTest(f"127.0.0.2 loopback unavailable: {exc}")

        port, token = self._start_main(
            f"http://127.0.0.2:{second.port}/inference/go")
        self.assertNotEqual(port, DEFAULT_PORT)
        self.assertTrue(token)

        status, _, data = self._request("GET", "/healthz", port=port)
        self.assertEqual(status, 200)
        health = json.loads(data)
        self.assertEqual(health["port"], port)
        self.assertEqual(self.proxy.Config.host, "127.0.0.2")
        self.assertEqual(self.proxy.Config.upstream_port, second.port)

        status, _, body = self._api("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertIn(f"port={port}".encode(), body)

        status, _, body = self._api("POST", "/api/refresh?reset=1", b"free=none")
        self.assertEqual(status, 200, body)
        self.assertTrue(body.startswith(b"R ok=1"), body)
        self.assertEqual(self.sync_calls[-1]["key"], FAKE_KEY)
        self.assertEqual(self.sync_calls[-1]["base_url"],
                         f"http://127.0.0.1:{port}/v1")

        status, _, data = self._post_chat(
            {"model": MODEL, "messages": [{"role": "user", "content": "cli"}]},
            port=port)
        self.assertEqual(status, 200)
        self.assertEqual(data, JSON_BODY)
        self.assertEqual(len(first.requests), 0,
                         "relay used the wrong loopback upstream")
        self.assertEqual(len(second.requests), 1)
        self.assertEqual(second.requests[0]["headers"].get("host"),
                         f"127.0.0.2:{second.port}")
        self.assertEqual(second.requests[0]["headers"].get("authorization"),
                         f"Bearer {FAKE_KEY}")
        self._stop_main()


class ApiKeyRollbackTest(_RelayTestBase):
    def setUp(self):
        super().setUp()
        self.models_path = self.root / "models.json"
        self.env_path = self.root / ".env"
        self.backup_dir = self.root / "backup"
        self.usage_calls = []

        real_require = self.oc.require_models_writable
        real_write_env = self.oc.write_env
        real_update = self.oc.update_models_key

        def require_temp(*_args, **_kwargs):
            real_require(str(self.models_path))

        def write_env_temp(updates, path=None):
            real_write_env(updates, str(self.env_path))

        def update_temp(key, models_path=None):
            return real_update(key, str(self.models_path))

        def fake_usage(key, proxy=None, timeout=20.0):
            self.usage_calls.append({"key": key, "proxy": proxy})
            return {"monthly": {"percent": 12}, "rolling": {}, "weekly": {}}

        self._stack.enter_context(
            mock.patch.object(self.oc, "ENV_PATH", str(self.env_path)))
        self._stack.enter_context(
            mock.patch.object(self.oc, "BACKUP_DIR", str(self.backup_dir)))
        self._stack.enter_context(
            mock.patch.object(self.oc, "CACHE_DIR", str(self.root / "cache")))
        self._stack.enter_context(
            mock.patch.object(self.oc, "LOG_DIR", str(self.root / "logs")))
        self._stack.enter_context(
            mock.patch.object(self.oc, "require_models_writable", require_temp))
        self._stack.enter_context(
            mock.patch.object(self.oc, "write_env", write_env_temp))
        self._stack.enter_context(
            mock.patch.object(self.oc, "update_models_key", update_temp))
        self._stack.enter_context(
            mock.patch.object(self.oc, "fetch_usage", fake_usage))

        def reset_load_state():
            self.oc.MODELS_LOAD_STATE = {
                "recovered": False, "backup": None, "error": None}

        reset_load_state()
        self.addCleanup(reset_load_state)
        self._serve(None)
        self.proxy.Config.fallback_key = OLD_KEY

    def _write_models(self) -> bytes:
        rows = [
            {
                "id": "ocgo-relay",
                "vendor": "OpenCode",
                "url": "http://127.0.0.1:56820/v1",
                "apiKey": OLD_KEY,
            },
            {
                "id": "custom",
                "vendor": "Other",
                "url": "https://api.example.com/v1",
                "apiKey": "custom-key",
            },
        ]
        data = (json.dumps(rows, ensure_ascii=False, indent=2) + "\n").encode()
        self.models_path.write_bytes(data)
        return data

    def _post_key(self):
        return self._request(
            "POST", "/api/key", f"key={FAKE_KEY}".encode(),
            {"x-ocgo-token": API_TOKEN,
             "Content-Type": "application/x-www-form-urlencoded"})

    def test_backup_failure_restores_original_env(self):
        cases = (("missing", None), ("empty", b""),
                 ("existing", b"\xef\xbb\xbf# example\r\n"))
        for label, original_env in cases:
            with self.subTest(env=label):
                if self.env_path.exists():
                    self.env_path.unlink()
                if original_env is not None:
                    self.env_path.write_bytes(original_env)
                original_models = self._write_models()
                self.proxy.Config.fallback_key = OLD_KEY
                self.usage_calls.clear()

                with mock.patch.object(self.oc.shutil, "copy2",
                                       side_effect=OSError("disk full")):
                    status, _, body = self._post_key()

                self.assertEqual(status, 500, body)
                self.assertTrue(body.startswith(b"E key persist OSError"), body)
                self.assertNotIn(FAKE_KEY.encode(), body)
                if original_env is None:
                    self.assertFalse(self.env_path.exists(),
                                     "rollback must remove the .env it created")
                else:
                    self.assertEqual(self.env_path.read_bytes(), original_env)
                self.assertEqual(self.models_path.read_bytes(), original_models)
                self.assertEqual(self.proxy.Config.fallback_key, OLD_KEY)
                self.assertEqual(list(self.backup_dir.glob("models.json.bak.*")), [])
                self.assertEqual(len(self.usage_calls), 1,
                                 "key validation must not call usage twice")

    def test_key_update_stops_when_original_env_cannot_be_read(self):
        original_models = self._write_models()
        original_env = b"# configuration\n"
        self.env_path.write_bytes(original_env)
        real_open = open

        def open_fixture(path, mode="r", *args, **kwargs):
            if path == str(self.env_path) and mode == "rb":
                raise PermissionError("fixture cannot read original configuration")
            return real_open(path, mode, *args, **kwargs)

        with mock.patch("builtins.open", open_fixture):
            status, _, body = self._post_key()

        self.assertEqual(status, 500, body)
        self.assertEqual(self.env_path.read_bytes(), original_env)
        self.assertEqual(self.models_path.read_bytes(), original_models)
        self.assertEqual(self.proxy.Config.fallback_key, OLD_KEY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
