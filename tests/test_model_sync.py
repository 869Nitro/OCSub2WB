#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for the model one-click sync and its config safety.

The panel's "one-click configure" button posts to ``/api/refresh`` (the tree
has no ``/api/refresh_oneclick`` route; ``/api/refresh`` is the one-click
entry).  That handler rebuilds WorkBuddy's custom-model table through
``ocgo_common.sync_models``.  Every managed row must point at the port the
relay was told to listen on via ``--port``, because WorkBuddy calls exactly
that URL.

How the tests stay honest and isolated:
  * the real ``RelayServer`` / ``RelayHandler`` serve a real loopback socket
    bound to an ephemeral port, so the live 8787 is never bound or called;
  * the route, ``sync_models``, ``build_entry``, ``managed_entry`` and the
    atomic models.json writer all run as in production;
  * only the upstream catalog transport (``request_json``) is faked, and the
    destination models.json path, the ``.env`` reader and the cache dirs are
    redirected into a TemporaryDirectory.  No real upstream, no credentials,
    no WorkBuddy configuration is read or written.
"""

from __future__ import annotations

import contextlib
import copy
import http.client
import importlib
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PROXY_SOURCE = PROJECT_ROOT / "opencode-go-proxy.py"
PROXY_MODULE_NAME = "ocgo_proxy_under_test"

# Syntactically plausible but entirely fake key; never a real credential.
FAKE_KEY = "oc_sk_unittest_fake_key_000000000000"
API_TOKEN = "unittest-control-token"
CLI_PORT = 56820
DEFAULT_PORT = 8787
CHAT_MODEL_ID = "ocgo-unittest-chat"

# One paid chat model: the default one-click sync (free=none/all with no free
# rows) still imports it, which is all the URL assertion needs.
FAKE_CATALOG = [
    {
        "id": CHAT_MODEL_ID,
        "name": "Unit Test Chat",
        "type": "chat",
        "limit": {"context": 32768, "output": 4096},
        "capabilities": {
            "tools": {"supported": True},
            "reasoning": {"supported": False},
        },
        "cost": {"text": {"input": 1.0, "output": 2.0}},
        "modalities": {"input": ["text/plain"]},
    }
]

CONFIG_ATTRS = (
    "port",
    "api_token",
    "log",
    "models_count",
    "fallback_key",
    "upstream_mode",
    "proxy_url",
)

_MISSING = object()


def _load_proxy_module():
    spec = importlib.util.spec_from_file_location(PROXY_MODULE_NAME, PROXY_SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {PROXY_SOURCE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PROXY_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


class _ModelSyncTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PROXY_SOURCE.is_file():
            raise unittest.SkipTest(f"missing {PROXY_SOURCE}")
        cls.oc = importlib.import_module("ocgo_common")
        cls.proxy = _load_proxy_module()

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="ocgo-model-sync-")
        self.addCleanup(self.tmpdir.cleanup)
        root = Path(self.tmpdir.name)
        self.models_path = root / "models.json"
        cache_dir = root / "cache"

        oc = self.oc
        self.addCleanup(oc.clear_catalog_cache)
        oc.clear_catalog_cache()

        def reset_state():
            oc.MODELS_LOAD_STATE = {"recovered": False, "backup": None, "error": None}

        reset_state()
        self.addCleanup(reset_state)

        self.catalog_calls = []
        real_sync = oc.sync_models
        real_count = oc.managed_count

        def sync_into_temp(key, **kwargs):
            # /api/refresh calls sync_models(key, base_url=..., reset=...,
            # include_free=..., proxy=None, force=False).  Only the
            # destination file changes; base_url stays production-owned.
            kwargs["models_path"] = str(self.models_path)
            return real_sync(key, **kwargs)

        def count_temp(*_args, **_kwargs):
            return real_count(str(self.models_path))

        def fake_request_json(method, url, key="", payload=None, proxy=None,
                              timeout=30.0, session="ses_ocgo_tool"):
            # Simulated model catalog; no real upstream is contacted.
            if method == "GET" and url == oc.CATALOG_URL:
                self.catalog_calls.append({"key": key, "proxy": proxy})
                return 200, {}, {"data": copy.deepcopy(FAKE_CATALOG)}
            raise AssertionError(f"unexpected upstream call: {method} {url}")

        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        self._stack.enter_context(
            mock.patch.object(oc, "request_json", fake_request_json))
        self._stack.enter_context(mock.patch.object(oc, "sync_models", sync_into_temp))
        self._stack.enter_context(mock.patch.object(oc, "managed_count", count_temp))
        self._stack.enter_context(
            mock.patch.object(oc, "read_env", lambda *_a, **_k: {}))
        self._stack.enter_context(mock.patch.object(oc, "CACHE_DIR", str(cache_dir)))
        self._stack.enter_context(
            mock.patch.object(oc, "LOG_DIR", str(cache_dir / "logs")))
        self._stack.enter_context(
            mock.patch.object(oc, "BACKUP_DIR", str(cache_dir / "backup")))

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
        config.log = self.proxy.RequestLog(str(cache_dir / "logs"))

    def _read_rows(self):
        return json.loads(self.models_path.read_text(encoding="utf-8"))

    def _write_rows(self, rows):
        self.models_path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _backup_files(self):
        return sorted(Path(self.oc.BACKUP_DIR).glob("models.json.bak.*"))

    def _start_relay(self):
        server = self.proxy.RelayServer(("127.0.0.1", 0), self.proxy.RelayHandler)
        relay_port = server.server_address[1]
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
        # Port 0 is always an ephemeral port; this guards the live relay.
        self.assertNotEqual(relay_port, DEFAULT_PORT)
        return relay_port

    def _post_oneclick_refresh(self, argv):
        config = self.proxy.Config
        args = self.proxy.parse_args(argv)
        # Same assignments main() makes from the CLI arguments.
        config.port = args.port
        config.fallback_key = (args.key or "").strip()
        config.api_token = API_TOKEN
        config.upstream_mode = "off"
        config.proxy_url = None

        relay_port = self._start_relay()
        conn = http.client.HTTPConnection("127.0.0.1", relay_port, timeout=15.0)
        try:
            conn.request(
                "POST",
                "/api/refresh?reset=1",
                body=b"free=all",
                headers={
                    "x-ocgo-token": config.api_token,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            response = conn.getresponse()
            status = response.status
            text = response.read().decode("utf-8", "replace")
        finally:
            conn.close()
        return args.port, status, text


class OneClickSyncPortTest(_ModelSyncTestBase):
    def _assert_oneclick_url(self, argv, expected_port):
        port, status, text = self._post_oneclick_refresh(argv)
        self.assertEqual(port, expected_port)
        self.assertEqual(status, 200, f"one-click refresh failed: {text}")
        self.assertTrue(text.startswith("R ok=1"), text)
        self.assertEqual(
            self.catalog_calls,
            [{"key": FAKE_KEY, "proxy": None}],
            "one-click sync must read the (simulated) catalog with the CLI key",
        )

        rows = json.loads(self.models_path.read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 1, rows)
        entry = rows[0]
        self.assertIsInstance(entry, dict)
        self.assertEqual(entry.get("id"), CHAT_MODEL_ID)
        self.assertEqual(entry.get("apiKey"), FAKE_KEY)
        self.assertEqual(
            entry.get("url"),
            f"http://127.0.0.1:{expected_port}/v1",
            f"one-click sync wrote {entry.get('url')!r} for --port {expected_port}",
        )

    def test_oneclick_sync_uses_cli_port(self):
        """--port 56820 must produce 127.0.0.1:56820 URLs."""
        self.assertNotEqual(CLI_PORT, DEFAULT_PORT)
        self._assert_oneclick_url(
            ["--port", str(CLI_PORT), "--key", FAKE_KEY], CLI_PORT
        )

    def test_oneclick_sync_uses_default_port(self):
        """Without --port the same entry must still write the 8787 default."""
        self.assertEqual(self.proxy.DEFAULT_PORT, DEFAULT_PORT)
        self._assert_oneclick_url(["--key", FAKE_KEY], DEFAULT_PORT)


class ModelsConfigSafetyTest(_ModelSyncTestBase):
    def test_managed_entry_keeps_foreign_urls_and_own_loopback(self):
        managed = self.oc.managed_entry
        owned = [
            {"vendor": "OpenCode", "url": "http://127.0.0.1:8787/v1"},
            {"vendor": "OpenCode", "url": "http://localhost:8787/v1/"},
            {"vendor": "OpenCode", "url": "http://127.0.0.1:56820/v1"},
            {"vendor": "something-else", "url": "http://127.0.0.1:8787/v1"},
        ]
        foreign = [
            {"vendor": "OpenCode", "url": "https://api.example.com/v1"},
            {"vendor": "OpenCode",
             "url": "https://api.example.com/v1?peer=127.0.0.1:8787"},
            {"vendor": "OpenCode",
             "url": "https://api.example.com/127.0.0.1:8787/v1"},
            {"vendor": "OpenCode", "url": "http://127.0.0.1:56820/v1?peer=1"},
            {"vendor": "Ollama", "url": "http://127.0.0.1:11434/v1"},
            {"vendor": "OpenCode", "url": "http://127.0.0.1:56820/chat"},
            "not a dict",
            {},
        ]
        for entry in owned:
            self.assertTrue(managed(entry), entry)
        for entry in foreign:
            self.assertFalse(managed(entry), entry)

    def test_repeated_oneclick_keeps_single_row_and_backups(self):
        with mock.patch.object(self.oc.time, "strftime",
                               return_value="20260102-030405"):
            for _ in range(3):
                port, status, text = self._post_oneclick_refresh(
                    ["--port", str(CLI_PORT), "--key", FAKE_KEY])
                self.assertEqual(port, CLI_PORT)
                self.assertEqual(status, 200, text)
                self.assertTrue(text.startswith("R ok=1"), text)

        rows = self._read_rows()
        self.assertEqual([row.get("id") for row in rows], [CHAT_MODEL_ID])
        self.assertEqual(rows[0].get("url"), f"http://127.0.0.1:{CLI_PORT}/v1")
        self.assertEqual(self.oc.managed_count(), 1)

        backups = self._backup_files()
        self.assertEqual(len(backups), 2, backups)
        self.assertEqual(len({path.name for path in backups}), 2)

    def test_sync_preserves_foreign_row_with_same_model_id(self):
        custom = {
            "id": CHAT_MODEL_ID,
            "name": "My external route",
            "vendor": "OpenCode",
            "url": "https://gateway.example.com/v1?fallback=127.0.0.1:8787",
            "apiKey": "custom-key",
            "supportsToolCall": True,
        }
        self._write_rows([custom])
        stats = self.oc.sync_models(
            FAKE_KEY, base_url=self.oc.local_base_url(CLI_PORT), reset=True,
            include_free="all", models_path=str(self.models_path),
        )
        self.assertEqual(stats["conflicts"], [CHAT_MODEL_ID])
        self.assertEqual(stats["removed"], 0)
        self.assertEqual(stats["written"], 0)
        self.assertEqual(self._read_rows(), [custom])
        self.assertEqual(self.oc.managed_count(), 0)

    def test_oneclick_reports_same_id_conflict_and_keeps_custom(self):
        custom = {
            "id": CHAT_MODEL_ID,
            "name": "My external route",
            "vendor": "OpenCode",
            "url": "https://gateway.example.com/v1?fallback=127.0.0.1:8787",
            "apiKey": "custom-key",
        }
        self._write_rows([custom])
        port, status, text = self._post_oneclick_refresh(
            ["--port", str(CLI_PORT), "--key", FAKE_KEY])
        self.assertEqual(port, CLI_PORT)
        self.assertEqual(status, 200, text)
        self.assertTrue(text.startswith("R ok=1"), text)
        self.assertIn("conflicts=1", text)
        self.assertIn(f"ids={CHAT_MODEL_ID}", text)
        self.assertEqual(self._read_rows(), [custom])
        self.assertEqual(self.oc.managed_count(), 0)

    def test_sync_replaces_previous_nondefault_port_row(self):
        previous = {
            "id": CHAT_MODEL_ID,
            "vendor": "OpenCode",
            "url": "http://127.0.0.1:56820/v1",
            "apiKey": "old-key",
        }
        self._write_rows([previous])
        stats = self.oc.sync_models(
            FAKE_KEY, base_url=self.oc.local_base_url(56821), reset=True,
            include_free="all", models_path=str(self.models_path),
        )
        self.assertEqual(stats["removed"], 1)
        self.assertEqual(stats["conflicts"], [])
        rows = self._read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], CHAT_MODEL_ID)
        self.assertEqual(rows[0]["url"], "http://127.0.0.1:56821/v1")
        self.assertEqual(rows[0]["apiKey"], FAKE_KEY)
        self.assertEqual(self.oc.managed_count(), 1)

    def test_update_key_touches_only_managed_rows(self):
        managed = {
            "id": "ocgo-cli",
            "vendor": "OpenCode",
            "url": "http://127.0.0.1:56820/v1",
            "apiKey": "old-key",
        }
        custom = {
            "id": "custom",
            "vendor": "OpenCode",
            "url": "https://gateway.example.com/v1",
            "apiKey": "custom-key",
        }
        self._write_rows([managed, custom])
        changed = self.oc.update_models_key("oc_sk_new_key", str(self.models_path))
        self.assertEqual(changed, 1)
        rows = self._read_rows()
        self.assertEqual(rows[0]["apiKey"], "oc_sk_new_key")
        self.assertEqual(rows[1]["apiKey"], "custom-key")

    def test_purge_removes_managed_rows_and_keeps_custom(self):
        custom_external = {
            "id": "custom-external",
            "vendor": "OpenCode",
            "url": "https://api.example.com/v1?peer=127.0.0.1:8787",
        }
        custom_local_other = {
            "id": "custom-local",
            "vendor": "Ollama",
            "url": "http://127.0.0.1:11434/v1",
        }
        managed_default = {
            "id": "ocgo-default",
            "vendor": "OpenCode",
            "url": "http://127.0.0.1:8787/v1",
        }
        managed_cli = {
            "id": "ocgo-cli",
            "vendor": "OpenCode",
            "url": "http://127.0.0.1:56820/v1",
        }
        self._write_rows(
            [custom_external, managed_default, custom_local_other, managed_cli])
        stats = self.oc.purge_models(str(self.models_path))
        self.assertEqual(stats["purged"], 2)
        self.assertEqual(stats["kept"], 2)
        self.assertEqual(stats["rows"], 2)
        self.assertTrue(stats["backup"])
        self.assertEqual(
            self._read_rows(), [custom_external, custom_local_other])
        self.assertEqual(self.oc.managed_count(), 0)

    def test_empty_models_file_is_rebuilt(self):
        self.models_path.write_text("", encoding="utf-8")
        stats = self.oc.sync_models(
            FAKE_KEY, base_url=self.oc.local_base_url(CLI_PORT), reset=True,
            include_free="all", models_path=str(self.models_path),
        )
        self.assertFalse(self.oc.models_load_state()["recovered"])
        self.assertEqual(stats["added"], 1)
        rows = self._read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], f"http://127.0.0.1:{CLI_PORT}/v1")

    def test_same_second_backups_do_not_overwrite_each_other(self):
        self._write_rows([{"id": "first"}])
        with mock.patch.object(self.oc.time, "strftime",
                               return_value="20260102-030405"):
            self.oc.save_models([{"id": "second"}], str(self.models_path))
            self.oc.save_models([{"id": "third"}], str(self.models_path))

        backups = self._backup_files()
        self.assertEqual(len(backups), 2, backups)
        contents = sorted(
            json.loads(path.read_text(encoding="utf-8"))[0]["id"]
            for path in backups
        )
        self.assertEqual(contents, ["first", "second"])
        self.assertEqual(self._read_rows(), [{"id": "third"}])

    def test_backup_failure_leaves_user_file_untouched(self):
        original = json.dumps([{"id": "keep-me"}], ensure_ascii=False, indent=2) + "\n"
        self.models_path.write_text(original, encoding="utf-8")
        with mock.patch.object(self.oc.shutil, "copy2",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.oc.save_models([{"id": "replacement"}], str(self.models_path))
        self.assertEqual(self.models_path.read_text(encoding="utf-8"), original)
        self.assertFalse(Path(str(self.models_path) + ".tmp").exists())
        self.assertEqual(self._backup_files(), [])

    def test_corrupt_models_blocks_write_until_restore(self):
        valid = [
            {"id": "user-row", "vendor": "Other",
             "url": "https://api.example.com/v1"},
            {"id": "ocgo-old", "vendor": "OpenCode",
             "url": "http://127.0.0.1:8787/v1"},
        ]
        self._write_rows(valid)
        saved = self.oc.save_models(valid, str(self.models_path))
        self.assertTrue(saved)
        corrupt = "{not json"
        self.models_path.write_text(corrupt, encoding="utf-8")

        self.assertEqual(self.oc.load_models(str(self.models_path)), valid)
        state = self.oc.models_load_state()
        self.assertTrue(state["recovered"])
        self.assertEqual(state["backup"], saved)
        with self.assertRaises(self.oc.ModelsRecoveryRequired):
            self.oc.require_models_writable(str(self.models_path))

        with self.assertRaises(self.oc.ModelsRecoveryRequired):
            self.oc.sync_models(
                FAKE_KEY, base_url=self.oc.local_base_url(CLI_PORT), reset=True,
                include_free="all", models_path=str(self.models_path),
            )
        self.assertEqual(self.models_path.read_text(encoding="utf-8"), corrupt)

        stats = self.oc.restore_models_from_backup(str(self.models_path))
        self.assertEqual(stats["restored"], len(valid))
        self.assertEqual(stats["backup"], saved)
        self.assertTrue(Path(stats["corrupt"]).is_file())
        self.assertEqual(
            Path(stats["corrupt"]).read_text(encoding="utf-8"), corrupt)
        self.assertEqual(self._read_rows(), valid)
        self.assertFalse(self.oc.models_load_state()["recovered"])

        stats = self.oc.sync_models(
            FAKE_KEY, base_url=self.oc.local_base_url(CLI_PORT), reset=True,
            include_free="all", models_path=str(self.models_path),
        )
        self.assertEqual(stats["removed"], 1)
        rows = self._read_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], valid[0])
        self.assertEqual(rows[1]["url"], f"http://127.0.0.1:{CLI_PORT}/v1")
        self.assertEqual(self.oc.managed_count(), 1)

    def test_corrupt_models_without_backup_reports_error(self):
        corrupt = "{broken"
        self.models_path.write_text(corrupt, encoding="utf-8")
        with self.assertRaises(RuntimeError) as caught:
            self.oc.restore_models_from_backup(str(self.models_path))
        self.assertIn("备份", str(caught.exception))
        self.assertEqual(self.models_path.read_text(encoding="utf-8"), corrupt)
        self.assertEqual(self._backup_files(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
