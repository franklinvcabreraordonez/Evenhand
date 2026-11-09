"""A stand-in for a remote MCP server on Streamable HTTP, in either era, that records exactly what reaches it."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeHttpMcp:
    def __init__(self, require_session=False, legacy_get=False):
        self.requests = []
        self.require_session = require_session
        self.legacy_get = legacy_get
        self.progress_delay = 0.3
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def body(self):
                if "chunked" in self.headers.get("Transfer-Encoding", ""):
                    out = b""
                    while True:
                        size = int(self.rfile.readline().strip(), 16)
                        if size == 0:
                            self.rfile.readline()
                            return out
                        out += self.rfile.read(size)
                        self.rfile.readline()
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def reply(self, status, payload, headers=None):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def sse(self, events):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                for n, item in enumerate(events):
                    if isinstance(item, float):
                        time.sleep(item)
                        continue
                    self.wfile.write(f"id: {n}\nevent: message\ndata: {json.dumps(item)}\n\n".encode())
                    self.wfile.flush()
                self.close_connection = True

            def do_POST(self):
                raw = self.body()
                outer.requests.append({"method": "POST", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": raw})
                message = json.loads(raw)
                headers = {k.lower(): v for k, v in self.headers.items()}
                method, params = message.get("method"), message.get("params") or {}
                if "mcp-method" in headers:   # the newer era checks the headers against the body
                    if headers["mcp-method"] != method or (method == "tools/call" and headers.get("mcp-name") != params.get("name")):
                        return self.reply(400, {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32020, "message": "Header mismatch"}})
                if method == "initialize":
                    return self.reply(200, {"jsonrpc": "2.0", "id": message["id"], "result": {"protocolVersion": "2025-11-25", "capabilities": {}, "serverInfo": {"name": "fake"}}},
                                      {"Mcp-Session-Id": "sess-1"})
                if outer.require_session and headers.get("mcp-session-id") != "sess-1":
                    return self.reply(400, {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32000, "message": "Missing session"}})
                if "id" not in message:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                mid = message["id"]
                if method == "tools/list":
                    return self.reply(200, {"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": "buy_item"}]}})
                if method == "tools/call":
                    name, args = params.get("name"), params.get("arguments") or {}
                    if name == "buy_item":
                        final = {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"bought {args.get('item')}"}]}}
                        progress = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}
                        return self.sse([progress, outer.progress_delay, final])
                    if name == "search_products":
                        return self.reply(200, {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "found"}]}})
                    if name == "fail_buy":
                        return self.reply(200, {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "declined"}], "isError": True}})
                    if name == "boom":
                        self.send_response(500)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                return self.reply(404, {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}})

            def do_GET(self):
                outer.requests.append({"method": "GET", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": b""})
                if outer.legacy_get:
                    return self.sse([{"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "hello"}}])
                self.send_response(405)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_DELETE(self):
                outer.requests.append({"method": "DELETE", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": b""})
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/mcp"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()