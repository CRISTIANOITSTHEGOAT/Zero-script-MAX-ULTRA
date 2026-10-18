# SPDX-License-Identifier: GPL-3.0-or-later
# A remote MCP server speaking Streamable HTTP (MCP spec 2025-03-26+), used by the
# bridge tests. It runs in-process on a background thread.
#
# Options (constructor / FakeRemoteMCP.start kwargs):
#   sse=True         reply to every request as a text/event-stream (SSE) body
#   expire_after=N   after N tools/call requests, the NEXT call is answered 404 once
#                    and the session is forgotten, as a restarted server does
#   require_key=K    reject requests whose X-Api-Key header is not K (HTTP 401)
#
# Tools: web_echo(text) echoes the text plus the headers it received, and
# web_page2 lives on the SECOND page of tools/list (tests pagination).
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL = "2025-06-18"


class FakeRemoteMCP:
    def __init__(self, sse=False, expire_after=None, require_key=None):
        self.sse = sse
        self.expire_after = expire_after
        self.require_key = require_key
        self.session = None
        self.calls = 0
        self.seen_methods = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep test output quiet
                pass

            def _write(self, status, body=b"", ctype=None, extra=None):
                self.send_response(status)
                if ctype:
                    self.send_header("Content-Type", ctype)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _reply(self, obj, extra=None):
                if outer.sse:
                    body = (b": keep-alive\n\n" + b"event: message\ndata: "
                            + json.dumps(obj).encode() + b"\n\n")
                    self._write(200, body, "text/event-stream", extra)
                else:
                    self._write(200, json.dumps(obj).encode(), "application/json", extra)

            def do_DELETE(self):
                sid = self.headers.get("Mcp-Session-Id")
                if sid and sid == outer.session:
                    outer.session = None
                    self._write(200)
                else:
                    self._write(404, b"unknown session")

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                msg = json.loads(self.rfile.read(length) or b"{}")
                method = msg.get("method")
                outer.seen_methods.append(method)
                if outer.require_key and self.headers.get("X-Api-Key") != outer.require_key:
                    self._write(401, b"bad key")
                    return
                if method == "initialize":
                    outer.session = uuid.uuid4().hex
                    self._reply({"jsonrpc": "2.0", "id": msg["id"], "result": {
                        "protocolVersion": PROTOCOL,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-remote", "version": "1"}}},
                        extra={"Mcp-Session-Id": outer.session})
                    return
                if msg.get("id") is None:  # notification such as notifications/initialized
                    self._write(202)
                    return
                sid = self.headers.get("Mcp-Session-Id")
                if not sid:
                    self._write(400, b"missing session id")
                    return
                if sid != outer.session:
                    self._write(404, b"unknown session")
                    return
                if method == "tools/list":
                    cursor = (msg.get("params") or {}).get("cursor")
                    if cursor == "page2":
                        result = {"tools": [{"name": "web_page2", "description": "second page",
                                             "inputSchema": {"type": "object", "properties": {}}}]}
                    else:
                        result = {"tools": [{"name": "web_echo", "description": "echo + headers",
                                             "inputSchema": {"type": "object", "properties": {
                                                 "text": {"type": "string"}}}}],
                                  "nextCursor": "page2"}
                    self._reply({"jsonrpc": "2.0", "id": msg["id"], "result": result})
                    return
                if method == "tools/call":
                    outer.calls += 1
                    if outer.expire_after is not None and outer.calls == outer.expire_after + 1:
                        outer.session = None  # forget the session once, as a restarted server would
                        self._write(404, b"session not found")
                        return
                    args = (msg.get("params") or {}).get("arguments") or {}
                    text = (f"echo:{args.get('text', '')}"
                            f"|key={self.headers.get('X-Api-Key') or ''}"
                            f"|proto={self.headers.get('MCP-Protocol-Version') or ''}")
                    self._reply({"jsonrpc": "2.0", "id": msg["id"], "result": {
                        "content": [{"type": "text", "text": text}]}})
                    return
                self._reply({"jsonrpc": "2.0", "id": msg["id"],
                             "error": {"code": -32601, "message": f"unknown method {method}"}})

        return Handler
