# SPDX-License-Identifier: GPL-3.0-or-later
# A tiny stdio MCP server used by the bridge tests (no Roblox Studio needed).
#   FAKE_MODE=ok    (default) answers initialize, tools/list and tools/call
#   FAKE_MODE=hang  answers initialize, then never answers tools/list - a server
#                   that is alive but stuck, like Studio's proxy with no Studio.
import json
import os
import sys

MODE = os.environ.get("FAKE_MODE", "ok")
TOOLS = [{
    "name": "echo",
    "description": "Echo the text argument back",
    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
}]


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-stdio", "version": "1"}}})
    elif method == "tools/list":
        if MODE == "hang":
            continue
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        args = msg["params"].get("arguments", {})
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": "echo:" + str(args.get("text", ""))}]}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown method"}})
