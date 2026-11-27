#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ocgo_common.py - shared brain for the OpenCode Go relay and its control panel.

Everything that touches disk or the network lives here, so there is exactly one
writer for .env and models.json:

  * .env read/write (comments preserved, atomic replace)
  * upstream HTTP proxy: parse / auto-detect / CONNECT validation
  * HTTPS requests with optional CONNECT tunnel
  * model catalog (10 minute cache), free-tier detection
  * per-model reasoning mapping -> WorkBuddy models.json entries
  * models.json: full reset, key refresh, backup + atomic write
  * quota parsing, key masking

Standard library only. Windows-first, but degrades to no-proxy elsewhere.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "2.2.1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
# Runtime artefacts belong to the project, not to a machine-wide cache folder:
# logs are part of the feature set and must survive independently of any global cache.
CACHE_DIR = os.path.join(BASE_DIR, "cache")
LOG_DIR = os.path.join(CACHE_DIR, "logs")
BACKUP_DIR = os.path.join(CACHE_DIR, "backup")
MODELS_PATH = os.path.join(os.path.expanduser("~"), ".workbuddy", "models.json")
DEFAULT_NAME_PREFIX = "[OC] "

HOST = "opencode.ai"
GO_BASE = "/inference/go"
CATALOG_URL = f"https://{HOST}{GO_BASE}/v1/models"
USAGE_URL = f"https://{HOST}{GO_BASE}/v1/usage"
CHAT_URL = f"https://{HOST}{GO_BASE}/openai/v1/chat/completions"
DEFAULT_PORT = 8787
DEFAULT_BASE_URL = f"http://127.0.0.1:{DEFAULT_PORT}/v1"
USER_AGENT = "workbuddy-opencode-proxy/2.0 (+windows; local)"
VENDOR = "OpenCode"
FALLBACK_SESSION = "ses_wb_fallback"

# Local mixed/http proxies worth trying when mode is "auto", in order.
DEFAULT_PROXY_URL = "http://127.0.0.1:7897"


# WorkBuddy's own effort vocabulary, ascending.
WB_EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "max"]

CATALOG_TTL = 600.0
PROBE_DELAY = 0.3
PROBE_TIMEOUT = 8.0

_catalog_lock = threading.Lock()
_catalog_cache: dict[str, tuple[float, list]] = {}
_models_lock = threading.RLock()


# --------------------------------------------------------------------------- #
# small utilities
# --------------------------------------------------------------------------- #

def ensure_cache_dir() -> str:
    for path in (CACHE_DIR, LOG_DIR, BACKUP_DIR):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass
    return CACHE_DIR


def mask_key(key: str) -> str:
    key = (key or "").strip()
    if len(key) <= 12:
        return "***" if key else ""
    return f"{key[:10]}…{key[-4:]}"


def stable_only(values) -> list[str]:
    return [v for v in dict.fromkeys(values) if v]


def local_base_url(port: int = DEFAULT_PORT) -> str:
    """Base URL WorkBuddy uses to reach a relay listening on ``port``."""
    return f"http://127.0.0.1:{int(port)}/v1"


# --------------------------------------------------------------------------- #
# .env
# --------------------------------------------------------------------------- #

def read_env(path: str = ENV_PATH) -> dict:
    values: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def write_env(updates: dict, path: str = ENV_PATH) -> None:
    """Update keys in place, keep comments/order, append what is missing."""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    except OSError:
        lines = []

    pending = {k: str(v) for k, v in updates.items()}
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in pending:
                out.append(f"{key}={pending.pop(key)}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in pending.items():
        if key not in seen:
            out.append(f"{key}={value}")

    data = "\n".join(out).rstrip("\n") + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# upstream proxy
# --------------------------------------------------------------------------- #

def parse_proxy(value: str | None) -> tuple[str, int, str | None] | None:
    """'http://127.0.0.1:7897' -> ('127.0.0.1', 7897, None)."""
    if not value:
        return None
    value = value.strip()
    if not value or value.lower() in ("off", "none", "direct"):
        return None
    if "://" not in value:
        value = "http://" + value
    try:
        parts = urllib.parse.urlsplit(value)
        if not parts.hostname:
            return None
        port = parts.port or (443 if parts.scheme == "https" else 8080)
        auth = None
        if parts.username:
            raw = f"{parts.username}:{parts.password or ''}".encode("utf-8")
            auth = "Basic " + base64.b64encode(raw).decode("ascii")
        return parts.hostname, int(port), auth
    except ValueError:
        return None


def test_proxy(url: str, timeout: float = 3.0) -> bool:
    """One CONNECT to the gateway through the proxy. No request is sent."""
    parsed = parse_proxy(url)
    if not parsed:
        return False
    host, port, auth = parsed
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        headers = [f"CONNECT {HOST}:443 HTTP/1.1", f"Host: {HOST}:443"]
        if auth:
            headers.append(f"Proxy-Authorization: {auth}")
        sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
        sock.settimeout(timeout)
        reply = sock.recv(200)
        first = reply.split(b"\r\n", 1)[0]
        return re.match(rb"HTTP/1\.[01] 200", first) is not None
    except OSError:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def normalize_upstream(mode: str | None, url: str | None = "") -> tuple[str, str]:
    """Migrate legacy .env values into (off|auto|manual, proxy_url)."""
    raw = (mode or "off").strip()
    low = raw.lower()
    fallback = (url or "").strip()
    if low in ("off", "none", "direct"):
        return "off", fallback
    if low in ("auto", "manual"):
        return low, fallback or DEFAULT_PROXY_URL
    if "://" in raw or ":" in raw:
        return "manual", raw
    return "auto", fallback or DEFAULT_PROXY_URL


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def request(method: str, url: str, headers: dict | None = None,
            body: bytes | None = None, proxy: str | None = None,
            timeout: float = 30.0) -> tuple[int, dict, bytes]:
    """One-shot HTTPS request, optionally through an HTTP CONNECT proxy."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or HOST
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)

    tunnel = parse_proxy(proxy) if proxy else None
    if tunnel:
        phost, pport, pauth = tunnel
        conn = http.client.HTTPSConnection(phost, pport, timeout=timeout)
        tunnel_headers = {}
        if pauth:
            tunnel_headers["Proxy-Authorization"] = pauth
        conn.set_tunnel(host, port, headers=tunnel_headers)
    elif parts.scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)

    try:
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    finally:
        try:
            conn.close()
        except Exception:
            pass


def request_json(method: str, url: str, key: str = "", payload: dict | None = None,
                 proxy: str | None = None, timeout: float = 30.0,
                 session: str = "ses_ocgo_tool") -> tuple[int, dict, object]:
    headers = {"Content-Type": "application/json", "x-opencode-session": session}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    status, hdrs, data = request(method, url, headers, body, proxy, timeout)
    try:
        parsed = json.loads(data.decode("utf-8", "replace")) if data else None
    except ValueError:
        parsed = None
    return status, hdrs, parsed


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #

def fetch_catalog(key: str, proxy: str | None = None, timeout: float = 30.0,
                  force: bool = False) -> list[dict]:
    cache_key = (key or "")[-8:] + "|" + (proxy or "direct")
    now = time.time()
    with _catalog_lock:
        hit = _catalog_cache.get(cache_key)
        if hit and not force and now - hit[0] < CATALOG_TTL:
            return hit[1]

    status, _, payload = request_json("GET", CATALOG_URL, key=key, proxy=proxy,
                                      timeout=timeout)
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"catalog HTTP {status}")
    models = payload.get("data")
    if not isinstance(models, list):
        raise RuntimeError("catalog shape changed: no data[]")

    with _catalog_lock:
        _catalog_cache[cache_key] = (now, models)
    return models


def clear_catalog_cache() -> None:
    with _catalog_lock:
        _catalog_cache.clear()


def is_chat(model: dict) -> bool:
    """Only chat models belong in WorkBuddy; decision/embedding ones 400 there."""
    return (model.get("type") or "chat") == "chat"


def _price(node) -> float | None:
    if isinstance(node, dict):
        node = node.get("price")
    if node is None or isinstance(node, bool):
        return None
    try:
        return float(node)
    except (TypeError, ValueError):
        return None


def is_free(model: dict) -> bool:
    text = (model.get("cost") or {}).get("text") or {}
    inp = text.get("input") or {}
    out = text.get("output") or {}
    pin, pout = _price(inp), _price(out)
    pcache = _price((inp.get("cache") or {}).get("read")) if isinstance(inp, dict) else None
    known = [p for p in (pin, pout, pcache) if p is not None]
    if any(p > 0 for p in known):
        return False
    if pin == 0 and pout == 0 and (pcache is None or pcache == 0):
        return True
    return str(model.get("id") or "").endswith("-free")


def name_prefix(explicit: str | None = None, env: dict | None = None,
                free: bool = False) -> str:
    """Base prefix, or its free-model variant: "[OC] " -> "[OC-FREE] ".

    Derived automatically so there is no second setting to keep in sync.
    """
    if explicit is not None:
        base = explicit
    else:
        env = env if env is not None else read_env()
        base = env.get("OPENCODE_NAME_PREFIX")
        if base is None:
            base = DEFAULT_NAME_PREFIX
    if not free:
        return base
    cut = base.find("]")
    if cut >= 0:
        return base[:cut] + "-FREE" + base[cut:]
    trimmed = base.rstrip()
    return f"{trimmed}-FREE " if trimmed else "FREE "


def clean_name(model: dict) -> str:
    return (model.get("name") or model.get("id") or "").strip()


def reasoning_config(model: dict) -> dict | None:
    caps = (model.get("capabilities") or {}).get("reasoning") or {}
    if not caps.get("supported"):
        return None
    raw = [e for e in (caps.get("effort") or []) if isinstance(e, str)]
    efforts = [e for e in WB_EFFORTS if e in raw]
    toggle = caps.get("toggle")
    if any(e in ("none", "off") for e in raw) or toggle is True:
        can_disable = True
    elif toggle is False:
        can_disable = False
    elif not raw:
        # No effort levels means the model can only think on/off; without an
        # explicit toggle it is treated as always-on.
        can_disable = False
    else:
        # The catalog has effort levels but says nothing about toggling.
        # Leave the field unknown so WorkBuddy uses its default switch.
        can_disable = None
    result = {"supportedEfforts": efforts}
    if can_disable is not None:
        result["canDisableThinking"] = can_disable
    if efforts:
        result["defaultEffort"] = efforts[-1]
    return result


def build_entry(model: dict, key: str, base_url: str = DEFAULT_BASE_URL,
                name_prefix: str = DEFAULT_NAME_PREFIX) -> dict:
    mid = model["id"]
    caps = model.get("capabilities") or {}
    limits = model.get("limit") or {}
    modalities = (model.get("modalities") or {}).get("input") or []
    reasoning = reasoning_config(model)

    entry = {
        "id": mid,
        "name": f"{name_prefix}{clean_name(model)}",
        "vendor": VENDOR,
        "url": base_url,
        "apiKey": key,
        "supportsToolCall": bool((caps.get("tools") or {}).get("supported")),
        "supportsImages": any(str(m).startswith("image/") for m in modalities),
        "supportsReasoning": reasoning is not None,
        "maxInputTokens": limits.get("context"),
        "maxOutputTokens": limits.get("output"),
    }
    if reasoning is not None:
        entry["reasoning"] = reasoning
        if reasoning.get("canDisableThinking") is False:
            entry["onlyReasoning"] = True
    return {k: v for k, v in entry.items() if v is not None}


def model_row(model: dict) -> dict:
    """Compact view used by the API and the panel."""
    caps = (model.get("capabilities") or {}).get("reasoning") or {}
    reasoning = reasoning_config(model)
    if not caps.get("supported"):
        think = "no"
    elif reasoning and reasoning.get("canDisableThinking") is False:
        think = "always"
    else:
        think = "toggle"
    return {
        "id": model.get("id", ""),
        "name": clean_name(model),
        "ctx": (lambda v: -1 if v is None else int(v))((model.get("limit") or {}).get("context")),
        "out": (lambda v: -1 if v is None else int(v))((model.get("limit") or {}).get("output")),
        "think": think,
        "efforts": ",".join((reasoning or {}).get("supportedEfforts", [])),
        "free": is_free(model),
        "training": bool((model.get("go") or {}).get("training")),
        "limited": bool((model.get("go") or {}).get("limited_regions")),
        "zdr": bool((model.get("go") or {}).get("zdr")),
    }


# --------------------------------------------------------------------------- #
# models.json
# --------------------------------------------------------------------------- #

MODELS_LOAD_STATE = {"recovered": False, "backup": None, "error": None}


class ModelsRecoveryRequired(RuntimeError):
    """Raised when models.json is corrupt and a backup was loaded read-only."""


def models_load_state() -> dict:
    return dict(MODELS_LOAD_STATE)


def _models_backups() -> list[tuple[float, str]]:
    try:
        entries = []
        for ent in os.scandir(BACKUP_DIR):
            if ent.is_file() and ent.name.startswith("models.json.bak."):
                try:
                    entries.append((ent.stat().st_mtime, ent.path))
                except OSError:
                    continue
        entries.sort(key=lambda item: item[0], reverse=True)
        return entries
    except OSError:
        return []


def _read_models_file(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            raw = fh.read().strip()
    except FileNotFoundError:
        return []
    if not raw:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise RuntimeError(f"{path} is not a JSON array")
    return data


def load_models(path: str = MODELS_PATH) -> list:
    global MODELS_LOAD_STATE
    try:
        data = _read_models_file(path)
    except Exception as exc:  # noqa: BLE001
        for _, candidate in _models_backups():
            try:
                data = _read_models_file(candidate)
            except Exception:
                continue
            MODELS_LOAD_STATE = {"recovered": True, "backup": candidate,
                                 "error": f"{type(exc).__name__}: {exc}"}
            return data
        MODELS_LOAD_STATE = {"recovered": True, "backup": None,
                             "error": f"{type(exc).__name__}: {exc}"}
        raise
    MODELS_LOAD_STATE = {"recovered": False, "backup": None, "error": None}
    return data


def require_models_writable(models_path: str = MODELS_PATH) -> None:
    load_models(models_path)
    if MODELS_LOAD_STATE["recovered"]:
        raise ModelsRecoveryRequired("models.json 已从备份只读加载，请先确认恢复")


def _prune_models_backups(limit: int = 20) -> None:
    for _, path in _models_backups()[limit:]:
        try:
            os.remove(path)
        except OSError:
            pass


def restore_models_from_backup(models_path: str = MODELS_PATH) -> dict:
    """Write the newest valid backup back; keep the corrupt file for inspection."""
    with _models_lock:
        backups = _models_backups()
        if not backups:
            raise RuntimeError("没有可用的 models.json 备份")
        last_error = None
        for _, candidate in backups:
            try:
                entries = _read_models_file(candidate)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
            tmp = models_path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(entries, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            corrupt = None
            if os.path.exists(models_path):
                stamp = time.strftime("%Y%m%d-%H%M%S")
                corrupt = f"{models_path}.corrupt.{stamp}"
                n = 1
                while os.path.exists(corrupt):
                    corrupt = f"{models_path}.corrupt.{stamp}-{n}"
                    n += 1
                shutil.copy2(models_path, corrupt)
            os.replace(tmp, models_path)
            global MODELS_LOAD_STATE
            MODELS_LOAD_STATE = {"recovered": False, "backup": None, "error": None}
            return {"restored": len(entries), "backup": candidate, "corrupt": corrupt}
        raise RuntimeError(f"备份文件均不可用: {last_error}")


LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


def _loopback_v1(url: object) -> tuple[bool, int]:
    """True + port when ``url`` is exactly this tool's loopback /v1 shape.

    Only the parsed host and path count: a loopback fragment inside an
    external URL (path, query or userinfo) must not claim the row.
    """
    text = str(url or "").strip()
    if not text:
        return False, 0
    try:
        parts = urllib.parse.urlsplit(text)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return False, 0
    if (parts.scheme not in ("http", "https") or host not in LOOPBACK_HOSTS
            or parts.username or parts.password
            or parts.path.rstrip("/") != "/v1"
            or parts.query or parts.fragment):
        return False, 0
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return True, port


def managed_entry(entry: dict) -> bool:
    if not isinstance(entry, dict):
        return False
    own_url, port = _loopback_v1(entry.get("url"))
    if not own_url:
        return False
    # This tool stamps its vendor; keep accepting the initial release's
    # URL-only rows on the default port for a seamless upgrade.
    return entry.get("vendor") == VENDOR or port == DEFAULT_PORT


def _reserve_backup_path() -> str:
    """Reserve a unique backup name; seconds alone collide on quick writes."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(BACKUP_DIR, f"models.json.bak.{stamp}")
    for index in range(1000):
        candidate = base if index == 0 else f"{base}-{index}"
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise OSError(f"cannot allocate a models.json backup name under {BACKUP_DIR}")


def save_models(entries: list, path: str = MODELS_PATH, backup: bool = True) -> str | None:
    """Atomic write. Returns the backup path when one was made."""
    if MODELS_LOAD_STATE["recovered"]:
        raise ModelsRecoveryRequired("models.json 已从备份只读加载，请先确认恢复")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    backup_path = None
    if backup and os.path.exists(path):
        ensure_cache_dir()
        backup_path = _reserve_backup_path()
        try:
            shutil.copy2(path, backup_path)
        except OSError:
            # The backup is the only rollback copy; never risk the original.
            try:
                os.remove(backup_path)
            except OSError:
                pass
            raise
        _prune_models_backups()

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(entries, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return backup_path


def _normalise_free(include_free, free_by_id: dict[str, dict]) -> list[str]:
    """'none' | 'all' | [ids] -> list of free chat model ids."""
    if include_free is None:
        return []
    if isinstance(include_free, str):
        value = include_free.strip().lower()
        if value in ("", "none", "0", "false"):
            return []
        if value in ("all", "*", "yes", "true"):
            return list(free_by_id)
        return [s.strip() for s in include_free.split(",") if s.strip()]
    return [str(s) for s in include_free]


def purge_models(models_path: str = MODELS_PATH) -> dict:
    """Delete every managed row, keep user-owned ones. Returns counts + backup."""
    with _models_lock:
        existing = load_models(models_path)
        if MODELS_LOAD_STATE["recovered"]:
            raise ModelsRecoveryRequired("models.json 已从备份只读加载，请先确认恢复")
        kept: list = []
        purged = 0
        for row in existing:
            if isinstance(row, dict) and managed_entry(row):
                purged += 1
                continue
            kept.append(row)
        backup = save_models(kept, models_path) if purged else None
        return {"purged": purged, "kept": len(kept), "rows": len(kept), "backup": backup}


def sync_models(key: str, base_url: str = DEFAULT_BASE_URL, reset: bool = False,
                include_free="none", only: list[str] | None = None,
                models_path: str = MODELS_PATH, proxy: str | None = None,
                force: bool = False, prefix: str | None = None) -> dict:
    """Rewrite WorkBuddy's custom models.

    reset=True drops every managed row first (user rows survive).
    include_free accepts "none" | "all" | a comma string | a list.
    prefix defaults to .env OPENCODE_NAME_PREFIX, then "[OC] ".
    """
    models = [m for m in fetch_catalog(key, proxy=proxy) if is_chat(m)]
    free_by_id = {m["id"]: m for m in models if is_free(m)}
    paid = [m for m in models if not is_free(m)]

    if only:
        paid = [m for m in paid if m["id"] in only]
    paid.sort(key=lambda m: -(((m.get("go") or {}).get("monthly_usage") or {}).get("go") or 0))

    prefix_value = name_prefix(prefix)
    free_prefix_value = name_prefix(prefix, free=True)
    entries = [build_entry(m, key, base_url, prefix_value) for m in paid]
    wanted_free = _normalise_free(include_free, free_by_id)
    for mid in wanted_free:
        model = free_by_id.get(mid)
        if model:
            entries.append(build_entry(model, key, base_url, free_prefix_value))

    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["name"]] = counts.get(entry["name"], 0) + 1
    for entry in entries:
        if counts[entry["name"]] > 1:
            entry["name"] = f"{entry['name']}（{entry['id']}）"

    with _models_lock:
        existing = load_models(models_path)
        if MODELS_LOAD_STATE["recovered"]:
            raise ModelsRecoveryRequired("models.json 已从备份只读加载，请先确认恢复")
        managed_ids = {e["id"] for e in entries}
        kept: list = []
        removed = 0
        conflicts: list[str] = []
        for row in existing:
            if not isinstance(row, dict):
                kept.append(row)
                continue
            rid = row.get("id")
            if reset:
                if managed_entry(row):
                    removed += 1
                    continue
                if rid in managed_ids:
                    conflicts.append(str(rid))
                kept.append(row)
                continue
            if rid in managed_ids:
                if managed_entry(row) or force:
                    continue
                conflicts.append(str(rid))
            kept.append(row)

        old_ids = {r.get("id") for r in kept if isinstance(r, dict)}
        added = 0
        updated = 0
        written = 0
        for entry in entries:
            if entry["id"] in old_ids:
                continue
            kept.append(entry)
            written += 1
            if entry["id"] in {r.get("id") for r in existing if isinstance(r, dict)}:
                updated += 1
            else:
                added += 1

        backup = save_models(kept, models_path)

    if reset:
        added = written
        updated = 0

    return {
        "entries": entries,
        "rows": len(kept),
        "added": added,
        "updated": updated,
        "written": written,
        "removed": removed,
        "kept": sum(1 for r in kept if isinstance(r, dict) and not managed_entry(r)),
        "preserved": sum(1 for r in kept if isinstance(r, dict) and not managed_entry(r)),
        "conflicts": conflicts,
        "backup": backup,
        "prefix": prefix_value,
        "free_prefix": free_prefix_value,
        "skipped_free": [mid for mid in free_by_id if mid not in wanted_free],
    }


def update_models_key(key: str, models_path: str = MODELS_PATH) -> int:
    """Rewrite apiKey on every managed row. Returns the number changed."""
    with _models_lock:
        entries = load_models(models_path)
        if MODELS_LOAD_STATE["recovered"]:
            raise ModelsRecoveryRequired("models.json 已从备份只读加载，请先确认恢复")
        changed = 0
        for row in entries:
            if isinstance(row, dict) and managed_entry(row) and row.get("apiKey") != key:
                row["apiKey"] = key
                changed += 1
        if changed:
            save_models(entries, models_path)
        return changed


def managed_count(models_path: str = MODELS_PATH) -> int:
    try:
        return sum(1 for r in load_models(models_path) if isinstance(r, dict) and managed_entry(r))
    except Exception:
        return 0


# --------------------------------------------------------------------------- #
# quota
# --------------------------------------------------------------------------- #

def fetch_usage(key: str, proxy: str | None = None, timeout: float = 20.0) -> dict:
    last: Exception | None = None
    for attempt in (0, 1):
        try:
            status, _, payload = request_json("GET", USAGE_URL, key=key, proxy=proxy, timeout=timeout)
            if status != 200 or not isinstance(payload, dict):
                raise RuntimeError(f"usage HTTP {status}")
            return payload.get("usage") or {}
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == 0:
                time.sleep(0.3)
    raise last if last else RuntimeError("usage failed")


def format_reset(value: str | None) -> str:
    if not value:
        return "-"
    try:
        import calendar

        stamp = calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))
        return time.strftime("%m-%d %H:%M", time.localtime(stamp))
    except Exception:
        return value[:16]


def quota_rows(usage: dict) -> list[dict]:
    rows = []
    for label, key in (("5小时", "rolling"), ("本周", "weekly"), ("本月", "monthly")):
        item = usage.get(key) or {}
        percent = item.get("percent")
        try:
            pct = -1 if percent is None else int(percent)
        except (TypeError, ValueError):
            pct = -1
        rows.append({
            "key": key,
            "label": label,
            "percent": pct,
            "status": item.get("status") or "-",
            "reset": format_reset(item.get("resetsAt")),
        })
    return rows


# --------------------------------------------------------------------------- #
# free model probing
# --------------------------------------------------------------------------- #

def classify_probe(status: int, headers: dict, body: str) -> str:
    body_low = (body or "").lower()
    if status == 200:
        return "ok"
    # cf-ray is present on every Cloudflare-fronted response, so only the body
    # tells us this was actually a WAF block (1010 / browser_signature_banned).
    if "cloudflare_error" in body_low or "browser_signature" in body_low or "error_code\":1010" in body_low:
        return "waf"
    if "trains on request data" in body_low:
        if "paid endpoints" in body_low:
            return "opt-in paid"
        if "free endpoints" in body_low:
            return "opt-in free"
        return "opt-in"
    if status == 401:
        return "401 key"
    if status == 429:
        return "429 rate"
    if status == 451:
        return "451 region"
    if status == 403:
        if "freetier" in body_low or "free tier" in body_low or "free-tier" in body_low:
            return "403 free"
        if "global" in body_low:
            return "403 global"
        if any(w in body_low for w in ("region", "country", "territor", "region_not_allowed")):
            return "403 region"
        if "entitlement" in body_low:
            return "403 plan"
        return "403 other"
    if status == 400 and "session" in body_low:
        return "400 session"
    if status == 400 and "global" in body_low:
        return "400 global"
    if status == 400 and any(w in body_low for w in ("region", "country", "territor")):
        return "400 region"
    if status == 400 and "invalid_request" in body_low:
        return "400"
    return f"{status} err"


def probe_model(key: str, model_id: str, proxy: str | None, timeout: float = PROBE_TIMEOUT) -> dict:
    payload = {"model": model_id, "messages": [{"role": "user", "content": "hi"}],
               "max_tokens": 1, "stream": False}
    try:
        status, headers, data = request(
            "POST", CHAT_URL,
            {"Content-Type": "application/json",
             "Accept": "application/json",
             "Authorization": f"Bearer {key}",
             "x-opencode-session": "ses_ocgo_probe"},
            json.dumps(payload).encode("utf-8"), proxy, timeout,
        )
        body = data[:400].decode("utf-8", "replace")
        return {"status": status, "kind": classify_probe(status, headers, body), "body": body[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"status": 0, "kind": f"error {type(exc).__name__}", "body": str(exc)[:160]}


def probe_free_models(key: str, mode: str = "off", proxy: str | None = None,
                      limited: dict[str, bool] | None = None,
                      progress=None) -> list[dict]:
    limited = limited or {}
    models = [m for m in fetch_catalog(key) if is_chat(m) and is_free(m)]
    results = []
    total = len(models)
    for index, model in enumerate(models, start=1):
        model_id = model["id"]
        if mode == "manual":
            route = "proxy"
        elif mode == "auto" and limited.get(model_id):
            route = "proxy"
        else:
            route = "direct"
        if route == "proxy" and not proxy:
            result = {"status": 0, "kind": "需要代理", "body": ""}
        else:
            result = probe_model(key, model_id, proxy if route == "proxy" else None)
        results.append({"id": model_id, "name": clean_name(model),
                        "route": route, "result": result})
        if progress:
            progress(index, total, model_id)
        time.sleep(PROBE_DELAY)
    return results


def probe_working_ids(results: list[dict]) -> list[str]:
    return [r["id"] for r in results if (r.get("result") or {}).get("status") == 200]


def _selftest_detail(text: object) -> str:
    return str(text).replace("|", "/").replace("\r", " ").replace("\n", " ")[:160]


def selftest_reasoning(caps: dict | None) -> tuple[dict, str]:
    """Lowest thinking setting allowed by the catalog.

    Returns (payload fragment, label). Omit means "do not send reasoning_effort".
    """
    reasoning = ((caps or {}).get("reasoning") or {})
    if not reasoning.get("supported"):
        return {}, "无"
    raw = [e for e in (reasoning.get("effort") or []) if isinstance(e, str)]
    if any(e in ("none", "off") for e in raw):
        return {"reasoning_effort": "none"}, "none"
    if reasoning.get("toggle"):
        return {}, "omit"
    lowest = next((e for e in WB_EFFORTS if e in raw), None)
    if lowest:
        return {"reasoning_effort": lowest}, lowest
    return {}, "默认（无档位）"


def _selftest_local(port: int, method: str, path: str, body: dict | None,
                    key: str, timeout: float) -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Accept": "application/json", "Connection": "close", "X-OCGO-Selftest": "1"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, raw
    finally:
        conn.close()


def _selftest_reason(resp_status: int, raw: bytes) -> str:
    try:
        body = json.loads(raw)
        err = body.get("error") or {}
        if isinstance(err, dict) and err.get("message"):
            return f"HTTP {resp_status}: {err['message']}"
    except (ValueError, UnicodeDecodeError):
        pass
    text = raw[:200].decode("utf-8", "replace").strip()
    return f"HTTP {resp_status}: {text}" if text else f"HTTP {resp_status}"


def selftest_model(key: str, model: str, port: int = 8787,
                   timeout: float = 60.0) -> list[dict]:
    """Five checks for one model. Never raises; always returns five items."""
    items: list[dict] = []

    def add(item: str, ok: bool, detail: object) -> None:
        items.append({"item": item, "ok": bool(ok), "detail": _selftest_detail(detail)})

    caps = None
    models: list[dict] = []

    # relay
    try:
        status, _, raw = _selftest_local(port, "GET", "/healthz", None, key, 4.0)
        info = json.loads(raw)
        ok = status == 200 and bool(info.get("ok"))
        detail = (f"已就绪 · {info.get('upstream_mode', '?')} · pid {info.get('pid', '?')}"
                  if ok else _selftest_reason(status, raw))
        add("relay", ok, detail)
    except Exception as exc:  # noqa: BLE001
        add("relay", False, f"{type(exc).__name__}: {exc}")

    # catalog (also supplies reasoning capabilities)
    try:
        status, _, raw = _selftest_local(port, "GET", "/v1/models", None, key, 15.0)
        body = json.loads(raw) if raw else {}
        models = body.get("data") or []
        ok = status == 200 and bool(models)
        add("catalog", ok, f"{len(models)} 个模型" if ok else _selftest_reason(status, raw))
        if ok:
            caps = next((m for m in models if m.get("id") == model), None)
            caps = (caps or {}).get("capabilities")
    except Exception as exc:  # noqa: BLE001
        add("catalog", False, f"{type(exc).__name__}: {exc}")

    reasoning, reasoning_label = selftest_reasoning(caps)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 64,
        "stream": False,
    }
    payload.update(reasoning)

    chat_ok = False
    billing = ""
    # chat
    try:
        status, headers, raw = _selftest_local(
            port, "POST", "/v1/chat/completions", payload, key, timeout)
        body = json.loads(raw) if raw else {}
        choices = body.get("choices") or []
        chat_ok = status == 200 and bool(choices)
        if chat_ok:
            add("chat", True, f"通了 · 思考 {reasoning_label}")
            usage = body.get("usage") or {}
            billing = (body.get("billing_source")
                       or (body.get("opencode") or {}).get("billing_source")
                       or (usage.get("opencode") or {}).get("billing_source")
                       or headers.get("x-opencode-billing-source") or "")
        else:
            add("chat", False, _selftest_reason(status, raw))
    except Exception as exc:  # noqa: BLE001
        add("chat", False, f"{type(exc).__name__}: {exc}")

    # stream
    if not chat_ok:
        add("stream", False, "跳过（chat 未通过）")
    else:
        conn = None
        try:
            body = dict(payload)
            body["stream"] = True
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30.0)
            headers = {"Accept": "text/event-stream", "Content-Type": "application/json",
                       "Connection": "close", "X-OCGO-Selftest": "1"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            conn.request("POST", "/v1/chat/completions",
                         body=json.dumps(body).encode("utf-8"), headers=headers)
            resp = conn.getresponse()
            head = b""
            if resp.status == 200:
                while len(head) < 2048:
                    chunk = resp.read1(512)
                    if not chunk:
                        break
                    head += chunk
                    if b"data:" in head:
                        break
            conn.close()
            conn = None
            ok = resp.status == 200 and b"data:" in head
            add("stream", ok, "收到 SSE 首块" if ok else _selftest_reason(resp.status, head))
        except Exception as exc:  # noqa: BLE001
            add("stream", False, f"{type(exc).__name__}: {exc}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # billing (from the non-stream chat response)
    if not chat_ok:
        add("billing", False, "跳过（chat 未通过）")
    else:
        add("billing", bool(billing), billing or "响应里没有 billing_source")

    return items
