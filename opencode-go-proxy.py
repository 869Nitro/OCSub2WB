#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
opencode-go-proxy 2.0

A tiny loopback relay that lets any OpenAI-Chat-Completions client (WorkBuddy,
Trae, Cline, ...) talk to the OpenCode Go / Console gateway.

Why it exists: the gateway requires a stable `x-opencode-session` header on the
Go route. WorkBuddy's custom-model dialog has no field for request headers, so
the header has to be added somewhere in the path. This relay adds it, sets a
proper user-agent, and otherwise forwards bytes verbatim.

Design rules:
  * standard library only, no dependencies
  * never rewrite the request body (only a read-only peek to derive a session id)
  * streaming responses are relayed chunk by chunk with an explicit flush
  * binds 127.0.0.1 only
  * one instance per port (Windows happily double-binds, so we probe first)

Usage:
  python opencode-go-proxy.py                 # 127.0.0.1:8787
  python opencode-go-proxy.py --port 8788
  python opencode-go-proxy.py --session fixed --fixed-session ses_workbuddy
  python opencode-go-proxy.py --close-delimited   # if a client rejects chunked
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import secrets
import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

import ocgo_common as oc

VERSION = "2.0"
API_VERSION = 2
DEFAULT_PORT = 8787
DEFAULT_UPSTREAM = "https://opencode.ai/inference/go"
DEFAULT_UA = "workbuddy-opencode-proxy/2.0 (+windows; local)"
FALLBACK_SESSION = "ses_wb_fallback"
MAX_STREAM_BYTES = 8 * 1024 * 1024  # don't parse bodies larger than this
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 180.0
LOG_KEEP_DAYS = 7
MODELS_TTL = 600.0
TOKEN_PATH = os.path.join(oc.CACHE_DIR, "api.token")

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "expect",
    "host",
    "content-length",
}

# Response headers worth passing through untouched.
PASS_RESPONSE_HEADERS = (
    "content-type",
    "content-encoding",
    "content-language",
    "cache-control",
    "x-request-id",
    "x-opencode-request-id",
    "retry-after",
    "opencode-processing-time",
    "x-opencode-billing-source",
)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def safe_print(text: str, err: bool = False) -> None:
    """print() that tolerates having no console at all (pythonw.exe)."""
    stream = sys.stderr if err else sys.stdout
    if stream is None:
        return
    try:
        stream.write(text + "\n")
        stream.flush()
    except Exception:
        pass


def write_pid_file(path: str) -> None:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="ascii") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass


def clear_pid_file(path: str) -> None:
    try:
        with open(path, "r", encoding="ascii") as fh:
            current = fh.read().strip()
        if current == str(os.getpid()):
            os.remove(path)
    except OSError:
        pass


def load_env_file(path: str) -> dict:
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


def derive_session(body: bytes, payload: dict | None = None) -> str | None:
    """Stable per-conversation id derived from the first user message.

    Same conversation -> same id -> better routing / prompt-cache reuse.
    Anything unexpected returns None so the caller can fall back.
    """
    if payload is None:
        if not body or len(body) > MAX_STREAM_BYTES:
            return None
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return None
    if not isinstance(payload, dict):
        return None
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    for item in messages:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        text: str | None = None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict)
                and p.get("type") in ("text", "input_text")
                and isinstance(p.get("text"), str)
            ]
            text = "\n".join(parts) if parts else None
        if text:
            digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
            return "ses_" + digest[:32]
    return None


class RequestLog:
    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        self._lock = threading.Lock()
        os.makedirs(log_dir, exist_ok=True)
        self._prune()

    def _prune(self) -> None:
        cutoff = time.time() - LOG_KEEP_DAYS * 86400
        try:
            for name in os.listdir(self.log_dir):
                if not (name.startswith("proxy-") and name.endswith(".log")):
                    continue
                full = os.path.join(self.log_dir, name)
                if os.path.getmtime(full) < cutoff:
                    os.remove(full)
        except OSError:
            pass

    def write(self, line: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        text = f"{stamp} {line}"
        with self._lock:
            self._emit_console(text)
            day = time.strftime("%Y%m%d")
            path = os.path.join(self.log_dir, f"proxy-{day}.log")
            try:
                if os.path.exists(path) and os.path.getsize(path) > 20 * 1024 * 1024:
                    os.replace(path, path + ".1")
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(text + "\n")
            except OSError:
                pass

    @staticmethod
    def _emit_console(text: str) -> None:
        try:
            print(text, flush=True)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# path mapping
# --------------------------------------------------------------------------- #

def map_path(raw_path: str) -> tuple[str | None, str]:
    """Return (path_on_gateway_host, query). None means 404.

    Accepts every shape a client might produce:
      /v1/chat/completions           -> /inference/go/openai/v1/chat/completions
      /chat/completions              -> same
      /v1/v1/chat/completions        -> same (double /v1 tolerated)
      /v1/messages                   -> /inference/go/anthropic/v1/messages
      /openai/v1/chat/completions    -> /inference/go/openai/v1/chat/completions
      /inference/go/openai/v1/...    -> passed through untouched
    """
    parts = urlsplit(raw_path)
    path = parts.path or "/"
    query = parts.query

    while path.startswith("/v1/v1/"):
        path = "/v1/" + path[len("/v1/v1/"):]

    if path.startswith("/inference/"):
        return path, query
    if path.startswith("/openai/") or path.startswith("/anthropic/"):
        return "/inference/go" + path, query

    if path in ("/v1/messages", "/v1/messages/"):
        return "/inference/go/anthropic/v1/messages", query
    if path in ("/v1/chat/completions", "/chat/completions"):
        return "/inference/go/openai/v1/chat/completions", query
    if path in ("/v1/responses", "/responses"):
        return "/inference/go/openai/v1/responses", query
    if path in ("/v1/models", "/models"):
        return "/inference/go/v1/models", query
    if path in ("/v1/usage", "/usage"):
        return "/inference/go/v1/usage", query
    return None, query


# --------------------------------------------------------------------------- #
# handler
# --------------------------------------------------------------------------- #

class Config:
    port = DEFAULT_PORT
    scheme = "https"
    host = "opencode.ai"
    base_path = "/inference/go"
    session_mode = "auto"
    fixed_session = FALLBACK_SESSION
    user_agent = DEFAULT_UA
    fallback_key = ""
    close_delimited = False
    log: RequestLog
    requests_total = 0
    started_at = time.time()

    # control panel state
    lock = threading.RLock()
    api_token = ""
    upstream_mode = "off"
    proxy_url: str | None = None
    limited: dict[str, bool] = {}
    limited_ts = 0.0
    limited_lock = threading.Lock()
    limited_loading = False
    models_count = 0
    models_cache: tuple[float, bytes] | None = None
    jobs: dict[str, dict] = {}
    job_seq = 0
    quota_cache: tuple[float, dict] | None = None


def _prune_jobs_locked(keep: int = 20) -> None:
    finished = sorted(
        (int(job_id), job_id)
        for job_id, job in Config.jobs.items()
        if job.get("state") != "running"
    )
    for _, job_id in finished[:-keep]:
        Config.jobs.pop(job_id, None)


def refresh_limited(force: bool = False) -> None:
    """Load the model -> limited_regions map; cheap TTL cache, single flight."""
    with Config.limited_lock:
        if Config.limited_loading:
            return
        if not force and time.time() - Config.limited_ts < 600:
            return
        Config.limited_loading = True
    try:
        models = oc.fetch_catalog(Config.fallback_key, proxy=None)
        mapping = {m.get("id", ""): bool((m.get("go") or {}).get("limited_regions"))
                   for m in models if m.get("id")}
        with Config.limited_lock:
            Config.limited = mapping
            Config.limited_ts = time.time()
        Config.log.write(f"limited_refresh count={len(mapping)}")
    except Exception as exc:  # noqa: BLE001
        Config.log.write(f"limited_refresh_failed {type(exc).__name__}: {exc}")
    finally:
        with Config.limited_lock:
            Config.limited_loading = False


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"opencode-go-proxy/{VERSION}"
    sys_version = ""
    _headers_sent = False

    # -- plumbing --------------------------------------------------------- #

    def log_message(self, fmt, *args):  # silence stdlib access log
        return

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_HEAD(self):
        self._dispatch("HEAD")

    # -- dispatch --------------------------------------------------------- #

    def _is_limited(self, model: str | None) -> bool:
        if not model:
            return False
        with Config.limited_lock:
            if model in Config.limited:
                return Config.limited[model]
            stale = time.time() - Config.limited_ts > 600
        if stale:
            refresh_limited(force=True)
        with Config.limited_lock:
            return Config.limited.get(model, False)

    def _route(self, model: str | None) -> tuple[str | None, str]:
        mode = Config.upstream_mode
        if mode == "manual":
            return (Config.proxy_url, "proxy") if Config.proxy_url else (None, "blocked")
        if mode == "auto" and self._is_limited(model):
            if Config.proxy_url:
                return Config.proxy_url, "proxy"
            return None, "blocked"
        return None, "direct"

    def _dispatch(self, method: str) -> None:
        started = time.monotonic()
        raw_path = self.path
        if raw_path in ("/healthz", "/health", "/"):
            self._json(200, {
                "ok": True,
                "name": "opencode-go-proxy",
                "version": VERSION,
                "api_version": API_VERSION,
                "pid": os.getpid(),
                "port": Config.port,
                "upstream": f"{Config.scheme}://{Config.host}{Config.base_path}",
                "session_mode": Config.session_mode,
                "close_delimited": Config.close_delimited,
                "upstream_mode": Config.upstream_mode,
                "proxy_url": Config.proxy_url or "",
                "models": Config.models_count,
                "requests": Config.requests_total,
                "uptime_sec": round(time.time() - Config.started_at, 1),
            })
            Config.log.write(f"GET {raw_path} 200 healthz")
            return

        if raw_path == "/api" or raw_path.startswith("/api/"):
            self._handle_api(method)
            return

        upstream_path, query = map_path(raw_path)
        if upstream_path is None:
            self._json(404, {
                "error": {
                    "type": "NotFound",
                    "message": f"proxy does not map path {raw_path}. "
                               f"Use /v1/chat/completions or /v1/messages.",
                }
            })
            Config.log.write(f"{method} {raw_path} 404 unmapped")
            return

        body = self._read_body()
        payload = None
        if body and len(body) <= MAX_STREAM_BYTES:
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    payload = parsed
            except (ValueError, UnicodeDecodeError):
                payload = None
        model = payload.get("model") if payload else None
        session = self._session_for(body, payload)
        is_chat = any(part in upstream_path for part in
                      ("/chat/completions", "/messages", "/responses"))
        proxy, route = self._route(model) if is_chat else (None, "direct")
        if route == "blocked":
            message = ("该模型锁区，需要代理；请在面板填写代理地址并应用"
                       if Config.upstream_mode == "auto"
                       else "手动模式需要代理地址；请在面板填写并应用")
            self._json(502, {"error": {"type": "ProxyRequired", "message": message}})
            Config.log.write(f"{method} {raw_path} 502 proxy_required model={model or '-'}")
            return

        # WorkBuddy polls GET /v1/models every 10 minutes for each custom model
        # base URL. Cache the catalog so those polls never reach the gateway.
        if method == "GET" and upstream_path.endswith("/v1/models"):
            cached = Config.models_cache
            if cached and time.time() - cached[0] < MODELS_TTL:
                self._raw(200, cached[1], "application/json; charset=utf-8")
                Config.requests_total += 1
                Config.log.write(f"GET {raw_path} 200 {len(cached[1])}B cached")
                return

        headers = self._upstream_headers(session)
        target = upstream_path + (("?" + query) if query else "")

        conn = None
        status = 0
        sent = 0
        try:
            conn = self._open_upstream(proxy)
            Config.log.write(f"{method} {raw_path} route={route} model={model or '-'} "
                             f"proxy={proxy or 'direct'}")

            conn.request(method, target, body=body or None, headers=headers)
            resp = conn.getresponse()
            status = resp.status

            ctype = (resp.getheader("content-type") or "").lower()
            streaming = ctype.startswith("text/event-stream")
            if not streaming and not Config.close_delimited:
                # Some gateways stream without the canonical content type.
                streaming = resp.getheader("transfer-encoding", "").lower() == "chunked" \
                    and method != "HEAD" and "application/json" not in ctype

            if method == "HEAD":
                self._write_headers(status, resp, length=0)
                self.close_connection = True
                sent = 0
            elif streaming:
                sent = self._relay_stream(resp, status)
            else:
                sent, payload = self._relay_buffered(resp, status)
                if method == "GET" and upstream_path.endswith("/v1/models") and status == 200:
                    Config.models_cache = (time.time(), payload)

        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            Config.log.write(f"{method} {raw_path} client_abort after {status}")
            self.close_connection = True
            return
        except socket.timeout:
            Config.log.write(f"{method} {raw_path} upstream_timeout")
            self._safe_error(504, "UpstreamTimeout", "gateway did not answer in time")
            Config.requests_total += 1
            return
        except Exception as exc:  # noqa: BLE001 - relay must never crash a thread
            Config.log.write(f"{method} {raw_path} proxy_error {type(exc).__name__}: {exc}")
            self._safe_error(502, "UpstreamUnreachable", f"{type(exc).__name__}: {exc}")
            Config.requests_total += 1
            return
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        elapsed = round((time.monotonic() - started) * 1000)
        Config.requests_total += 1
        Config.log.write(
            f"{method} {raw_path}->{target} {status} {sent}B {elapsed}ms session={session}"
        )

    # -- request pieces --------------------------------------------------- #

    def _open_upstream(self, proxy: str | None):
        """Open the upstream connection, retrying once on a handshake hiccup.

        Only the connect/TLS phase is retried: at that point no request bytes
        have been written, so a retry cannot double-charge a completion.
        When an upstream proxy is active the same TLS handshake happens through
        a CONNECT tunnel, so retry semantics are unchanged.
        """
        tunnel = oc.parse_proxy(proxy) if proxy else None
        last: Exception | None = None
        for attempt in (0, 1):
            if tunnel:
                phost, pport, pauth = tunnel
                conn = http.client.HTTPSConnection(phost, pport, timeout=CONNECT_TIMEOUT)
                tunnel_headers = {"Proxy-Authorization": pauth} if pauth else {}
                conn.set_tunnel(Config.host, 443, headers=tunnel_headers)
            elif Config.scheme == "https":
                conn = http.client.HTTPSConnection(Config.host, 443, timeout=CONNECT_TIMEOUT)
            else:
                conn = http.client.HTTPConnection(Config.host, 80, timeout=CONNECT_TIMEOUT)
            try:
                conn.connect()
            except (OSError, ssl.SSLError) as exc:
                last = exc
                try:
                    conn.close()
                except Exception:
                    pass
                if attempt == 0:
                    time.sleep(0.25)
                    continue
                raise
            try:
                conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except (OSError, AttributeError):
                pass
            conn.sock.settimeout(READ_TIMEOUT)
            return conn
        raise last if last else RuntimeError("cannot open upstream")

    def _read_body(self) -> bytes:
        length = self.headers.get("content-length")
        if not length:
            return b""
        try:
            n = int(length)
        except ValueError:
            return b""
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _session_for(self, body: bytes, payload: dict | None = None) -> str:
        provided = (self.headers.get("x-opencode-session") or "").strip()
        if provided:
            return provided
        if Config.session_mode == "fixed":
            return Config.fixed_session
        derived = derive_session(body, payload)
        return derived or Config.fixed_session

    def _upstream_headers(self, session: str) -> dict:
        # Header names are case-insensitive on the wire, but Python dicts are not.
        # Normalise everything to lower case once, then set our overrides, otherwise
        # a client-supplied "User-Agent" plus our "user-agent" go out as two headers
        # and Cloudflare rejects the request as a forged browser signature (1010).
        headers: dict[str, str] = {}
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP:
                continue
            if lower in ("user-agent", "accept-encoding", "x-opencode-session", "host"):
                continue
            headers[lower] = value

        headers["host"] = Config.host
        headers["x-opencode-session"] = session
        headers["user-agent"] = Config.user_agent
        headers["accept-encoding"] = "identity"
        if "authorization" not in headers and Config.fallback_key:
            headers["authorization"] = f"Bearer {Config.fallback_key}"
        return headers

    # -- response pieces --------------------------------------------------- #

    def _write_headers(self, status: int, resp, length: int | None = None,
                       chunked: bool = False) -> None:
        self.send_response(status)
        for name in PASS_RESPONSE_HEADERS:
            value = resp.getheader(name)
            if value:
                self.send_header(name, value)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        elif length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Connection", "close")
        self.end_headers()
        self._headers_sent = True
        self.close_connection = True

    def _relay_buffered(self, resp, status: int) -> tuple[int, bytes]:
        payload = resp.read()
        self._write_headers(status, resp, length=len(payload))
        if payload:
            self.wfile.write(payload)
            self.wfile.flush()
        return len(payload), payload

    def _relay_stream(self, resp, status: int) -> int:
        if Config.close_delimited:
            self._write_headers(status, resp, length=None, chunked=False)
        else:
            self._write_headers(status, resp, chunked=True)

        total = 0
        while True:
            chunk = resp.read1(65536)
            if not chunk:
                break
            total += len(chunk)
            if Config.close_delimited:
                self.wfile.write(chunk)
            else:
                self.wfile.write(b"%x\r\n" % len(chunk))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
            self.wfile.flush()
        if not Config.close_delimited:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        return total

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self._headers_sent = True
        self.close_connection = True
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _safe_error(self, status: int, kind: str, message: str) -> None:
        if getattr(self, "_headers_sent", False):
            return
        try:
            self._json(status, {"error": {"type": kind, "message": message}})
        except Exception:
            pass

    def _raw(self, status: int, data: bytes, content_type: str = "text/plain; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self._headers_sent = True
        self.close_connection = True
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _text(self, status: int, text: str) -> None:
        if not text.endswith("\n"):
            text += "\n"
        self._raw(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    # -- control API ------------------------------------------------------- #

    def _api_ok(self) -> bool:
        origin = self.headers.get("origin")
        if origin:
            self._text(403, "E origin")
            return False
        token = self.headers.get("x-ocgo-token") or ""
        if not Config.api_token or not secrets.compare_digest(token, Config.api_token):
            self._text(401, "E token")
            return False
        return True

    def _api_form(self) -> dict:
        raw = self._read_body()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {}
        if text.startswith("{"):
            try:
                data = json.loads(text)
                return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
            except ValueError:
                return {}
        parsed = parse_qs(text, keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    def _handle_api(self, method: str) -> None:
        path = urlsplit(self.path).path
        query = parse_qs(urlsplit(self.path).query)

        if path == "/api/status":
            if not self._api_ok():
                return
            with Config.lock:
                mode = Config.upstream_mode
                proxy = Config.proxy_url or ""
                token_masked = oc.mask_key(Config.fallback_key)
            count = oc.managed_count()
            models_state = "recovered" if oc.models_load_state()["recovered"] else "ok"
            self._text(200, " ".join([
                "S ok=1",
                f"ver={API_VERSION}",
                f"proxy_ver={VERSION}",
                f"pid={os.getpid()}",
                f"port={Config.port}",
                f"mode={mode}",
                f"proxy_url={proxy}",
                f"upstream={'mixed' if mode == 'auto' else ('proxy' if mode == 'manual' else 'direct')}",
                f"key={token_masked}",
                f"models={count}",
                f"models_state={models_state}",
                f"requests={Config.requests_total}",
                f"uptime={int(time.time() - Config.started_at)}",
            ]))
            return

        if path == "/api/quota":
            if not self._api_ok():
                return
            force = query.get("refresh", ["0"])[0] in ("1", "true", "yes")
            cached = Config.quota_cache
            if cached and not force and time.time() - cached[0] < 30:
                usage = cached[1]
            else:
                try:
                    usage = self._usage_with_fallback()
                except Exception as exc:  # noqa: BLE001
                    self._text(502, f"E quota {type(exc).__name__} {exc}")
                    return
                Config.quota_cache = (time.time(), usage)
            lines = ["Q ok=1"]
            for row in oc.quota_rows(usage):
                lines.append(f"R {row['label']}|{row['percent']}|{row['reset']}|{row['status']}")
            self._text(200, "\n".join(lines))
            return

        if path == "/api/models":
            if not self._api_ok():
                return
            try:
                models = [m for m in oc.fetch_catalog(Config.fallback_key, proxy=None)
                          if oc.is_chat(m)]
            except Exception as exc:  # noqa: BLE001
                self._text(502, f"E catalog {type(exc).__name__} {exc}")
                return
            managed = self._managed_ids()
            lines = [f"M ok=1 count={len(models)}"]
            for model in models:
                row = oc.model_row(model)
                imported = 1 if row["id"] in managed else 0
                lines.append("M {id}|{name}|{ctx}|{out}|{think}|{efforts}|{free}|{imported}"
                             "|{training}|{limited}|{zdr}".format(
                    id=row["id"], name=row["name"], ctx=row["ctx"], out=row["out"],
                    think=row["think"], efforts=row["efforts"] or "-",
                    free=1 if row["free"] else 0, imported=imported,
                    training=1 if row["training"] else 0,
                    limited=1 if row["limited"] else 0,
                    zdr=1 if row["zdr"] else 0))
            self._text(200, "\n".join(lines))
            return

        if path == "/api/log":
            if not self._api_ok():
                return
            try:
                lines = int(query.get("lines", ["200"])[0] or 200)
            except (TypeError, ValueError):
                self._text(400, "E lines")
                return
            self._text(200, self._tail_log(max(10, min(lines, 2000))))
            return

        if path.startswith("/api/job/"):
            if not self._api_ok():
                return
            job_id = path.rsplit("/", 1)[-1]
            job = Config.jobs.get(job_id)
            if not job:
                self._text(404, "E no such job")
                return
            lines = [f"J state={job.get('state')} done={job.get('done', 0)} "
                     f"total={job.get('total', 0)} current={job.get('current', '') or '-'} "
                     f"proxy={job.get('proxy', '') or 'none'} "
                     f"kind={job.get('kind', 'probe')} model={job.get('model', '') or '-'}"]
            if job.get("state") == "error":
                lines.append(f"E {job.get('error', '')}")
            for item in job.get("results", []):
                if "item" in item:  # selftest row
                    lines.append(f"T {item['item']}|{1 if item.get('ok') else 0}|{item.get('detail', '')}")
                    continue
                result = item["result"]
                lines.append(f"F {item['id']}|{item['route']}|{result['status']}|"
                             f"{result['kind']}|{item['name']}")
            self._text(200, "\n".join(lines))
            return

        if method != "POST":
            self._text(405, "E method")
            return

        if path == "/api/shutdown":
            if not self._api_ok():
                return
            self._text(200, "S ok=1 bye")
            Config.log.write("api_shutdown requested")
            server = self.server

            def _stop():
                time.sleep(0.4)
                try:
                    server.shutdown()
                except Exception:
                    pass

            threading.Thread(target=_stop, daemon=True, name="api-shutdown").start()
            return

        if path == "/api/refresh":
            if not self._api_ok():
                return
            form = self._api_form()
            reset = query.get("reset", ["1"])[0] in ("1", "true", "yes")
            free = form.get("free") or query.get("free", ["none"])[0] or "none"
            try:
                stats = oc.sync_models(
                    Config.fallback_key, base_url=oc.local_base_url(Config.port),
                    reset=reset, include_free=free,
                    proxy=None, force=False,
                )
            except oc.ModelsRecoveryRequired as exc:
                self._text(409, f"E recovery {exc}")
                return
            except Exception as exc:  # noqa: BLE001
                self._text(502, f"E refresh {type(exc).__name__} {exc}")
                return
            Config.models_count = oc.managed_count()
            Config.log.write(f"api_refresh written={stats['written']} removed={stats['removed']} "
                             f"prefix={stats['prefix']!r} free_prefix={stats['free_prefix']!r} free={free}")
            conflicts = stats.get("conflicts") or []
            self._text(200, f"R ok=1 added={stats['added']} updated={stats['updated']} "
                            f"removed={stats['removed']} rows={stats['rows']} "
                            f"written={stats['written']} kept={stats['kept']} "
                            f"conflicts={len(conflicts)} ids={','.join(conflicts)}")
            return

        if path == "/api/workbuddy/purge":
            if not self._api_ok():
                return
            try:
                stats = oc.purge_models()
            except oc.ModelsRecoveryRequired as exc:
                self._text(409, f"E recovery {exc}")
                return
            except Exception as exc:  # noqa: BLE001
                self._text(502, f"E purge {type(exc).__name__} {exc}")
                return
            Config.models_count = oc.managed_count()
            Config.log.write(f"api_purge purged={stats['purged']} kept={stats['kept']}")
            self._text(200, f"P ok=1 purged={stats['purged']} kept={stats['kept']} rows={stats['rows']}")
            return

        if path == "/api/models/restore":
            if not self._api_ok():
                return
            try:
                stats = oc.restore_models_from_backup()
            except Exception as exc:  # noqa: BLE001
                self._text(502, f"E restore {type(exc).__name__} {exc}")
                return
            Config.models_count = oc.managed_count()
            backup_name = os.path.basename(stats.get("backup") or "")
            Config.log.write(f"api_models_restore restored={stats['restored']} backup={backup_name}")
            self._text(200, f"M ok=1 restored={stats['restored']} backup={backup_name}")
            return

        if path == "/api/key":
            if not self._api_ok():
                return
            try:
                oc.require_models_writable()
            except oc.ModelsRecoveryRequired as exc:
                self._text(409, f"E recovery {exc}")
                return
            except Exception as exc:  # noqa: BLE001
                self._text(409, f"E models {type(exc).__name__} {exc}")
                return
            form = self._api_form()
            key = (form.get("key") or "").strip()
            if not key.startswith("oc_sk_"):
                self._text(400, "E key format")
                return
            try:
                usage = oc.fetch_usage(key, proxy=None, timeout=15)
            except Exception as exc:  # noqa: BLE001
                try:
                    usage = oc.fetch_usage(key, proxy=Config.proxy_url, timeout=15)
                except Exception:  # noqa: BLE001
                    self._text(401, f"E key rejected {type(exc).__name__}")
                    return
            old_env = ""
            try:
                with open(oc.ENV_PATH, "r", encoding="utf-8-sig") as fh:
                    old_env = fh.read()
            except OSError:
                pass
            try:
                oc.write_env({"OPENCODE_API_KEY": key})
                changed = oc.update_models_key(key)
            except oc.ModelsRecoveryRequired as exc:
                if old_env:
                    try:
                        with open(oc.ENV_PATH, "w", encoding="utf-8", newline="\n") as fh:
                            fh.write(old_env)
                    except OSError:
                        pass
                self._text(409, f"E recovery {exc}")
                return
            except Exception as exc:  # noqa: BLE001
                if old_env:
                    try:
                        with open(oc.ENV_PATH, "w", encoding="utf-8", newline="\n") as fh:
                            fh.write(old_env)
                    except OSError:
                        pass
                self._text(500, f"E key persist {type(exc).__name__} {exc}")
                return
            with Config.lock:
                Config.fallback_key = key
            Config.log.write(f"api_key changed models={changed}")
            monthly = (usage.get("monthly") or {}).get("percent")
            self._text(200, f"K ok=1 key={oc.mask_key(key)} models={changed} monthly={monthly}")
            return

        if path == "/api/upstream-proxy":
            if not self._api_ok():
                return
            form = self._api_form()
            mode = (form.get("mode") or "off").strip().lower()
            url = (form.get("url") or "").strip()
            if mode in ("none", "direct"):
                mode = "off"
            if mode not in ("off", "auto", "manual"):
                self._text(400, "E 模式无效，应为 off/auto/manual")
                return

            if mode == "off":
                with Config.lock:
                    Config.upstream_mode = "off"
                    if url:
                        Config.proxy_url = url
                try:
                    oc.write_env({"OPENCODE_UPSTREAM_PROXY": "off",
                                  "OPENCODE_UPSTREAM_PROXY_URL": Config.proxy_url or url})
                except OSError:
                    pass
                Config.log.write("api_upstream mode=off")
                self._text(200, f"U ok=1 mode=off url={Config.proxy_url or ''}")
                return

            if not url:
                self._text(400, "E 代理地址不能为空")
                return
            if not oc.parse_proxy(url):
                self._text(400, "E 代理地址无效，应为 http://IP:端口")
                return
            if not oc.test_proxy(url, timeout=3.0):
                self._text(400, f"E 代理不可用：{url} 连接失败")
                return
            with Config.lock:
                Config.upstream_mode = mode
                Config.proxy_url = url
            try:
                oc.write_env({"OPENCODE_UPSTREAM_PROXY": mode,
                              "OPENCODE_UPSTREAM_PROXY_URL": url})
            except OSError:
                pass
            Config.log.write(f"api_upstream mode={mode} proxy={url} verified=1")
            self._text(200, f"U ok=1 mode={mode} url={url}")
            return

        if path == "/api/selftest":
            if not self._api_ok():
                return
            form = self._api_form()
            model = (form.get("model") or "").strip()
            if not model:
                self._text(400, "E model")
                return
            with Config.lock:
                for existing in Config.jobs.values():
                    if existing.get("state") == "running":
                        self._text(409, "E 已有任务在运行")
                        return
                Config.job_seq += 1
                job_id = str(Config.job_seq)
                Config.jobs[job_id] = {"state": "running", "done": 0, "total": 5,
                                       "current": model, "results": [],
                                       "kind": "selftest", "model": model}
                _prune_jobs_locked()
            key = Config.fallback_key

            def _run_selftest():
                job = Config.jobs[job_id]
                try:
                    results = oc.selftest_model(key, model, port=Config.port)
                    job["results"] = results
                    job["done"] = len(results)
                    job["state"] = "done"
                    for item in results:
                        Config.log.write(f"api_selftest_item job={job_id} item={item['item']} "
                                         f"ok={1 if item['ok'] else 0} detail={item['detail']}")
                except Exception as exc:  # noqa: BLE001
                    job["state"] = "error"
                    job["error"] = f"{type(exc).__name__}: {exc}"
                    Config.log.write(f"api_selftest_failed job={job_id} model={model} "
                                     f"{type(exc).__name__}: {exc}")
                Config.log.write(f"api_selftest job={job_id} model={model} state={job['state']}")

            threading.Thread(target=_run_selftest, daemon=True, name=f"selftest-{job_id}").start()
            self._text(202, f"J ok=1 id={job_id}")
            return

        if path == "/api/probe":
            if not self._api_ok():
                return
            with Config.lock:
                for existing in Config.jobs.values():
                    if existing.get("state") == "running":
                        self._text(409, "E 已有任务在运行")
                        return
                Config.job_seq += 1
                job_id = str(Config.job_seq)
                Config.jobs[job_id] = {"state": "running", "done": 0, "total": 0,
                                       "current": "", "results": [], "kind": "probe"}
                _prune_jobs_locked()
            proxy = Config.proxy_url
            mode = Config.upstream_mode
            with Config.limited_lock:
                limited = dict(Config.limited)
            if mode == "auto" and not limited:
                refresh_limited(force=True)
                with Config.limited_lock:
                    limited = dict(Config.limited)
            key = Config.fallback_key

            def _run():
                job = Config.jobs[job_id]

                def progress(done, total, current):
                    job["done"] = done
                    job["total"] = total
                    job["current"] = current

                try:
                    job["proxy"] = proxy or ""
                    results = oc.probe_free_models(key, mode=mode, proxy=proxy,
                                                   limited=limited, progress=progress)
                    job["results"] = results
                    job["state"] = "done"
                except Exception as exc:  # noqa: BLE001
                    job["state"] = "error"
                    job["error"] = f"{type(exc).__name__}: {exc}"
                Config.log.write(f"api_probe job={job_id} state={job['state']} "
                                 f"mode={mode} proxy={proxy or 'none'}")

            threading.Thread(target=_run, daemon=True, name=f"probe-{job_id}").start()
            self._text(202, f"J ok=1 id={job_id}")
            return

        self._text(404, "E no such api")
        return

    def _usage_with_fallback(self) -> dict:
        try:
            return oc.fetch_usage(Config.fallback_key, proxy=None)
        except Exception:
            if Config.proxy_url:
                return oc.fetch_usage(Config.fallback_key, proxy=Config.proxy_url)
            raise

    @staticmethod
    def _managed_ids() -> set[str]:
        try:
            return {r.get("id") for r in oc.load_models() if isinstance(r, dict) and oc.managed_entry(r)}
        except Exception:
            return set()

    @staticmethod
    def _tail_log(lines: int) -> str:
        path = os.path.join(Config.log.log_dir, time.strftime("proxy-%Y%m%d.log"))
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.readlines()
        except OSError:
            return ""
        return "".join(data[-lines:])


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    request_queue_size = 64


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #

def port_in_use(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(
        description="Loopback relay for OpenCode Go (adds x-opencode-session)."
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    parser.add_argument("--session", choices=("auto", "fixed"), default="auto",
                        help="auto: derive a stable id per conversation; fixed: one id")
    parser.add_argument("--fixed-session", default=FALLBACK_SESSION)
    parser.add_argument("--key", default="", help="fallback API key when the client sends none")
    parser.add_argument("--env-file", default=os.path.join(here, ".env"))
    parser.add_argument("--log-dir", default=os.path.join(here, "cache", "logs"))
    parser.add_argument("--pid-file", default=os.path.join(here, "cache", "proxy.pid"),
                        help="written on start, used by the panel")
    parser.add_argument("--upstream-proxy", default=None,
                        help="off | auto | http://127.0.0.1:7897 (default: .env value)")
    parser.add_argument("--token-file", default=TOKEN_PATH,
                        help="control API token, regenerated on every start")
    parser.add_argument("--purge-models", action="store_true",
                        help="internal: delete managed rows from models.json and exit")
    parser.add_argument("--result-file", default="",
                        help="internal: where --purge-models writes its result line")
    parser.add_argument("--close-delimited", action="store_true",
                        help="end streams by closing the socket instead of chunked framing")
    parser.add_argument("--no-color", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = parse_args(argv)

    if args.purge_models:
        try:
            stats = oc.purge_models()
            line = (f"P ok=1 purged={stats['purged']} kept={stats['kept']} "
                    f"rows={stats['rows']}")
            if stats.get("backup"):
                line += f" backup={stats['backup']}"
            code = 0
        except Exception as exc:  # noqa: BLE001
            line = f"E purge {type(exc).__name__}: {exc}"
            code = 1
        target = (args.result_file or "").strip()
        if target:
            tmp = target + ".tmp"
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(line + "\n")
                os.replace(tmp, target)
            except OSError:
                pass
        return code

    env = load_env_file(args.env_file)

    parsed = urlsplit(args.upstream)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        safe_print(f"invalid --upstream: {args.upstream}", err=True)
        return 2

    Config.port = args.port
    Config.scheme = parsed.scheme
    Config.host = parsed.hostname
    Config.base_path = parsed.path.rstrip("/")
    Config.session_mode = args.session
    Config.fixed_session = args.fixed_session or env.get(
        "OPENCODE_FALLBACK_SESSION", FALLBACK_SESSION
    )
    Config.user_agent = env.get("OPENCODE_USER_AGENT", DEFAULT_UA)
    Config.fallback_key = (args.key or env.get("OPENCODE_API_KEY", "")).strip()
    Config.close_delimited = args.close_delimited
    Config.log = RequestLog(args.log_dir)
    Config.models_count = oc.managed_count()

    if port_in_use(args.port):
        safe_print(f"[opencode-go-proxy] 127.0.0.1:{args.port} is already in use.")
        safe_print("  Another instance is probably running. Close it first, or pick --port.")
        Config.log.write(f"proxy_start_denied port={args.port} already_in_use")
        return 3

    try:
        server = RelayServer(("127.0.0.1", args.port), RelayHandler)
    except OSError as exc:
        safe_print(f"[opencode-go-proxy] cannot bind 127.0.0.1:{args.port}: {exc}")
        return 3

    write_pid_file(args.pid_file)

    # control API token: fresh on every start, readable by the local panel only
    Config.api_token = secrets.token_urlsafe(32)
    try:
        with open(args.token_file, "w", encoding="ascii") as fh:
            fh.write(Config.api_token)
    except OSError as exc:
        safe_print(f"[!] cannot write token file {args.token_file}: {exc}")

    raw_mode = args.upstream_proxy or env.get("OPENCODE_UPSTREAM_PROXY") or "off"
    raw_url = env.get("OPENCODE_UPSTREAM_PROXY_URL") or ""
    mode, proxy_url = oc.normalize_upstream(raw_mode, raw_url)
    Config.upstream_mode = mode
    Config.proxy_url = proxy_url or None
    if mode != "off":
        threading.Thread(target=refresh_limited, daemon=True, name="limited-refresh").start()

    banner = (
        f"opencode-go-proxy {VERSION} listening on http://127.0.0.1:{args.port}\n"
        f"  upstream      : {args.upstream}\n"
        f"  outbound      : {Config.upstream_mode} ({Config.proxy_url or 'direct'})\n"
        f"  session mode  : {Config.session_mode}"
        f"{' (' + Config.fixed_session + ')' if Config.session_mode == 'fixed' else ''}\n"
        f"  fallback key  : {'yes' if Config.fallback_key else 'no'}\n"
        f"  models        : {Config.models_count} managed rows\n"
        f"  logs          : {args.log_dir}\n"
        f"  pid file      : {args.pid_file}\n"
        f"  api token     : {args.token_file}\n"
        f"  WorkBuddy URL : http://127.0.0.1:{args.port}/v1\n"
        f"  Ctrl+C to stop."
    )
    safe_print(banner)
    Config.log.write("proxy_start " + banner.replace("\n", " | "))

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        safe_print("\n[opencode-go-proxy] stopping...")
    finally:
        server.server_close()
        clear_pid_file(args.pid_file)
        Config.log.write("proxy_stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
