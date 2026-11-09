"""The HTTP gateway: a reverse proxy in front of a remote MCP server that speaks Streamable HTTP.

Clients point at the gateway instead of the server. Every request is passed on with its headers and body
untouched, which matters because the newer protocol revisions check headers against the body. The gateway
looks only at tool calls it was asked to witness, and releases a call's result to the client only after the
witness has recorded it. Each HTTP exchange is tracked on its own, so two clients that happen to use the same
JSON-RPC id never get mixed up."""
from __future__ import annotations

import http.client
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

from . import rpc
from .core import Engine, Pending

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "trailers", "transfer-encoding", "upgrade"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
MAX_BODY = 20_000_000


class Gateway:
    def __init__(self, engine: Engine, upstream_url: str, host: str = "127.0.0.1", port: int = 8788, headers: Optional[Dict[str, str]] = None,
                 allow_origins: Iterable[str] = (), timeout: float = 300.0, allow_hosts: Iterable[str] = ()):
        parts = urlsplit(upstream_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("the upstream url must look like https://example.com/mcp")
        self.engine = engine
        self.upstream = parts
        self.path = parts.path or "/"
        self.headers = dict(headers or {})
        self.allow_origins = {o.rstrip("/").lower() for o in allow_origins}
        self.bound_to_loopback = host in LOCAL_HOSTS
        self.allow_hosts = LOCAL_HOSTS | {h.lower() for h in allow_hosts} | {host.lower()}
        self.timeout = timeout
        handler = type("GatewayHandler", (_Handler,), {"gateway": self})
        self.server = ThreadingHTTPServer((host, port), handler)
        self.server.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> Tuple[str, int]:
        host, port = self.server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        self._thread = threading.Thread(target=self.server.serve_forever, name="evenhand-gateway", daemon=True)
        self._thread.start()

    def serve_forever(self) -> None:
        self.server.serve_forever()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def host_allowed(self, host_header: Optional[str]) -> bool:
        """A gateway bound to this machine only answers to this machine's names. Otherwise a web page could point its own
        domain at 127.0.0.1 and use the gateway through the browser (DNS rebinding)."""
        if not self.bound_to_loopback or not host_header:
            return True
        host = host_header.strip().lower()
        name = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return name in self.allow_hosts or f"[{name}]" in self.allow_hosts

    def origin_allowed(self, origin: Optional[str]) -> bool:
        """Browsers send an Origin header, and a page on another site must not be able to drive a local gateway."""
        if origin is None:
            return True
        if origin.rstrip("/").lower() in self.allow_origins:
            return True
        return (urlsplit(origin).hostname or "").lower() in LOCAL_HOSTS


class _Handler(BaseHTTPRequestHandler):
    gateway: Gateway
    server_version = "EvenhandMcpGateway/0.1"

    def log_message(self, *args: Any) -> None:
        pass

    # ----- plumbing -----
    def _send_json(self, status: int, payload: Any) -> None:
        body = rpc.dumps(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> Optional[bytes]:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            chunks: List[bytes] = []
            total = 0
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                total += size
                if total > MAX_BODY:
                    return None
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        return self.rfile.read(length) if 0 <= length <= MAX_BODY else None

    def _check(self) -> bool:
        gw = self.gateway
        if not gw.host_allowed(self.headers.get("Host")):
            self._send_json(403, rpc.rpc_error(None, -32000, "Host not allowed"))
            return False
        if not gw.origin_allowed(self.headers.get("Origin")):
            self._send_json(403, rpc.rpc_error(None, -32000, "Origin not allowed"))
            return False
        if urlsplit(self.path).path.rstrip("/") != gw.path.rstrip("/"):
            self._send_json(404, rpc.rpc_error(None, -32601, "Not found"))
            return False
        return True

    # ----- the three verbs -----
    def do_POST(self) -> None:
        try:
            body = self._read_body()  # read it first: refusing a request with unread data left on the connection resets it
        except (ValueError, OSError):
            body = None
        if not self._check():
            return
        if body is None:
            self._send_json(400, rpc.rpc_error(None, -32700, "The request body could not be read"))
            return
        pending: Dict[Any, Pending] = {}
        if b"tools/call" in body:
            message = rpc.parse(body)
            if message is not None:
                try:
                    reply = self.gateway.engine.screen(message, pending)
                except Exception as exc:
                    self.gateway.engine.log(f"could not screen a message: {exc}")
                    reply = None
                if reply is not None:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(reply)))
                    self.end_headers()
                    self.wfile.write(reply)
                    return
        self._forward("POST", body, pending)

    def do_GET(self) -> None:
        if self._check():
            self._forward("GET", None, {})

    def do_DELETE(self) -> None:
        if self._check():
            self._forward("DELETE", None, {})

    def do_OPTIONS(self) -> None:
        if self._check():
            self._forward("OPTIONS", None, {})

    # ----- passing it on -----
    def _forward(self, method: str, body: Optional[bytes], pending: Dict[Any, Pending]) -> None:
        gw = self.gateway
        up = gw.upstream
        factory = http.client.HTTPSConnection if up.scheme == "https" else http.client.HTTPConnection
        conn = factory(up.hostname, up.port, timeout=gw.timeout)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP | {"host", "content-length", "accept-encoding"}}
        headers["Accept-Encoding"] = "identity"
        headers.update(gw.headers)
        if body is not None:
            headers["Content-Length"] = str(len(body))
        query = urlsplit(self.path).query
        try:
            conn.request(method, gw.path + (f"?{query}" if query else ""), body=body, headers=headers)
            response = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            gw.engine.abandon(pending, "the upstream server could not be reached")
            self._send_json(502, rpc.rpc_error(None, -32000, f"The upstream MCP server could not be reached: {exc}"))
            return
        try:
            self.send_response(response.status, response.reason)
            for name, value in response.msg.items():
                if name.lower() not in HOP_BY_HOP | {"content-length"}:
                    self.send_header(name, value)
            if response.getheader("Content-Type", "").lower().startswith("text/event-stream"):
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                if conn.sock is not None:
                    conn.sock.settimeout(None)  # a stream may be quiet for a long time
                self._stream(response, pending)
            else:
                payload = response.read()
                if pending and payload:
                    message = rpc.parse(payload)
                    if message is not None:
                        gw.engine.observe(message, pending)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client went away
        except (OSError, http.client.HTTPException) as exc:
            gw.engine.log(f"the upstream connection broke: {exc}")
        finally:
            conn.close()
            if pending:
                gw.engine.abandon(pending, "the connection ended before the call was answered")

    def _stream(self, response: http.client.HTTPResponse, pending: Dict[Any, Pending]) -> None:
        """Passes a Server-Sent Events stream on one event at a time, recording a witnessed call's receipt
        before the event that carries its result is released."""
        event: List[bytes] = []
        while True:
            line = response.readline()
            if not line:
                break
            event.append(line)
            if line in (b"\n", b"\r\n", b"\r"):
                self._release(event, pending)
                event = []
        if event:
            self._release(event, pending)

    def _release(self, lines: List[bytes], pending: Dict[Any, Pending]) -> None:
        if pending:
            data = b"\n".join(l.rstrip(b"\r\n")[5:][1:] if l[5:6] == b" " else l.rstrip(b"\r\n")[5:] for l in lines if l.startswith(b"data:"))
            message = rpc.parse(data) if data else None
            if message is not None:
                self.gateway.engine.observe(message, pending)
        self.wfile.write(b"".join(lines))
        self.wfile.flush()