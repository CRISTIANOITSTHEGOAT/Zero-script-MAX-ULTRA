# SPDX-License-Identifier: GPL-3.0-or-later
"""A stdio MCP server that pretends to be Roblox Studio's, backed by the
in-memory place in fake_studio.py.

Used by test_bridge.py to exercise the Dev Kit end to end through the REAL
bridge (WebSocket -> MCPManager -> local_tools -> execute_luau) without Roblox:
the bridge cannot tell the difference, which is the point.

Environment:
    FAKE_STUDIO_PLACE   place name (default "FakePlace")
    FAKE_STUDIO_STATE   JSON file the place is loaded from and saved to after
                        every mutation, so a test in the parent process can
                        assert on what a tool call actually changed.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_studio import FakeStudio  # noqa: E402

STATE_PATH = os.environ.get("FAKE_STUDIO_STATE") or ""
PLACE = os.environ.get("FAKE_STUDIO_PLACE", "FakePlace")

TOOLS = [
    {"name": "execute_luau",
     "description": "Execute Luau code in the active DataModel",
     "inputSchema": {"type": "object",
                     "properties": {"code": {"type": "string"},
                                    "datamodel_type": {"type": "string",
                                                       "enum": ["Edit", "Server", "Client"]}},
                     "required": ["code", "datamodel_type"]}},
    {"name": "multi_edit",
     "description": "Apply edits to a script",
     "inputSchema": {"type": "object",
                     "properties": {"file_path": {"type": "string"},
                                    "className": {"type": "string"},
                                    "datamodel_type": {"type": "string"},
                                    "edits": {"type": "array", "items": {"type": "object"}}},
                     "required": ["file_path", "edits", "datamodel_type"]}},
    {"name": "script_read",
     "description": "Read a script's contents",
     "inputSchema": {"type": "object", "properties": {"target_file": {"type": "string"}},
                     "required": ["target_file"]}},
    {"name": "get_console_output",
     "description": "Read the Output window",
     "inputSchema": {"type": "object", "properties": {"datamodel_type": {"type": "string"}}}},
    {"name": "start_stop_play",
     "description": "Start or stop play testing",
     "inputSchema": {"type": "object", "properties": {"is_start": {"type": "boolean"}},
                     "required": ["is_start"]}},
    {"name": "list_roblox_studios", "description": "List connected Studios",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_studio_state", "description": "Studio state",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_instance", "description": "Inspect an instance",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                     "required": ["path"]}},
]


def load_place():
    scripts = {
        "game.ServerScriptService.Main": ("Script", 'local Players = game:GetService("Players")\nprint("v1")\n'),
        "game.ReplicatedStorage.Modules.Config": ("ModuleScript", "return {version = 1}\n"),
    }
    if STATE_PATH and os.path.isfile(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
            scripts = {k: tuple(v) for k, v in (state.get("scripts") or {}).items()} or scripts
        except (OSError, ValueError):
            pass
    return FakeStudio(place=PLACE, scripts=scripts)


STUDIO = load_place()


def save_state():
    """Persist what the place looks like now, so the test process can assert on
    the effect of a call that ran inside this child."""
    if not STATE_PATH:
        return
    payload = {
        "place": STUDIO.place,
        "playing": STUDIO.playing,
        "selection": STUDIO.selection,
        "scripts": {k: [v["class"], v["source"]] for k, v in STUDIO.scripts.items()},
        "calls": [[n, a.get("code") and "<luau>" or a] for n, a in STUDIO.calls[-20:]],
    }
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, STATE_PATH)


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-studio", "version": "1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = msg["params"].get("name")
        args = msg["params"].get("arguments") or {}
        res = STUDIO.call(name, args)
        text = res["text"] if res["ok"] else f"Error: {res['error']}"
        save_state()
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": text}]}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown method"}})
