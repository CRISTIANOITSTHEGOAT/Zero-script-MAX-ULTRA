# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end tests for bridge.py - they run the REAL bridge as a subprocess on a
free port, with a temporary config.json and log folder, so your own config and
logs are never touched. No Roblox Studio is needed: the MCP servers are the fakes
in this folder.

Run from the bridge folder:
    pip install websockets
    python -m unittest discover -s tests -v

Covers:
  * the origin lock (a web page can no longer talk to the bridge)
  * stdio server tool calls
  * list_tools staying responsive while a server hangs
  * remote (Streamable HTTP) servers: auth headers, ${ENV} expansion, SSE replies,
    pagination, session expiry + automatic reconnect, URL validation
  * adding a remote server from the extension's add_server message
  * the bridge-side Dev Kit (local_tools.py): advertised next to Studio's own
    tools, routed by the bridge, and driving a fake Studio through execute_luau
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)

try:
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect
    HAVE_WS = True
except ImportError:  # websockets >= 13 (sync client) is required
    HAVE_WS = False

from fake_mcp_http import FakeRemoteMCP  # noqa: E402

STDIO_FAKE = os.path.join(HERE, "fake_mcp_stdio.py")
STUDIO_FAKE = os.path.join(HERE, "fake_studio_mcp.py")
EXT_ORIGIN = "chrome-extension://zerosciriptextensionidabcdefghijk"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BridgeProc:
    """Runs bridge.py with an isolated config, log folder and port."""

    def __init__(self, tmp, servers, extra_env=None):
        self.tmp = tmp
        self.port = free_port()
        self.config_path = os.path.join(tmp, "config.json")
        self.logs = os.path.join(tmp, "logs")
        self.servers = servers
        self.extra_env = extra_env or {}
        self.proc = None
        self._out = None

    def write_config(self, servers=None):
        if servers is not None:
            self.servers = servers
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": self.servers}, f)

    def read_config(self):
        with open(self.config_path, encoding="utf-8") as f:
            return json.load(f)

    def start(self):
        self.write_config()
        env = dict(os.environ)
        env.update({
            "ZS_BRIDGE_PORT": str(self.port),
            "ZS_BRIDGE_CONFIG": self.config_path,
            "ZS_BRIDGE_LOGS_DIR": self.logs,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        })
        env.update(self.extra_env)
        self._out = open(os.path.join(self.tmp, "bridge-stdout.txt"), "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(BRIDGE_DIR, "bridge.py")],
            env=env, stdout=self._out, stderr=subprocess.STDOUT, cwd=BRIDGE_DIR)
        # Wait until it answers a plain (no Origin) client.
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with self.open() as ws:
                    ws.recv(timeout=10)  # the "connected" snapshot
                return self
            except Exception:
                time.sleep(0.3)
        raise RuntimeError("bridge did not start; see " + os.path.join(self.tmp, "bridge-stdout.txt"))

    def open(self, origin=None):
        return connect(f"ws://127.0.0.1:{self.port}", origin=origin, open_timeout=10)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._out:
            self._out.close()


def rpc(ws, payload, reply_type, timeout=30):
    """Send one message and return the reply of `reply_type` carrying its id.
    Other snapshot/pong messages that arrive first are skipped."""
    rid = payload.setdefault("id", int(time.time() * 1e6) % 10**9)
    ws.send(json.dumps(payload))
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.time())))
        if msg.get("type") == reply_type and msg.get("id") == rid:
            return msg
    raise TimeoutError(f"no {reply_type} reply within {timeout}s")


def wait_for_tool(bp, name, timeout=30):
    """Poll list_tools until a tool with this advertised name exists."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:  # the bridge may be mid-restart (add_server restarts it): just retry
            with bp.open(EXT_ORIGIN) as ws:
                ws.recv(timeout=10)
                tools = rpc(ws, {"type": "list_tools"}, "tools")["tools"]
            if any(t["name"] == name for t in tools):
                return tools
        except (OSError, ConnectionClosed, TimeoutError):
            pass
        time.sleep(0.5)
    raise AssertionError(f"tool {name!r} never appeared")


@unittest.skipUnless(HAVE_WS, "pip install websockets (>= 13) to run the bridge tests")
class OriginLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="zs-origin-")
        cls.bp = BridgeProc(cls.tmp, {"fake": {"command": sys.executable, "args": [STDIO_FAKE]}}).start()

    @classmethod
    def tearDownClass(cls):
        cls.bp.stop()

    def test_web_page_origin_is_refused(self):
        with self.bp.open(origin="https://evil.example") as ws:
            with self.assertRaises(ConnectionClosed) as cm:
                ws.recv(timeout=10)  # must be closed BEFORE any snapshot is sent
        self.assertEqual(cm.exception.rcvd.code, 1008)

    def test_web_page_cannot_add_a_server(self):
        before = self.bp.read_config()
        with self.bp.open(origin="https://evil.example") as ws:
            try:
                ws.send(json.dumps({"type": "add_server", "id": 1, "server_id": "pwn",
                                    "command": sys.executable, "args": ["-c", "print(1)"]}))
                ws.recv(timeout=5)
            except Exception:
                pass  # the bridge closed the socket - that is the expected outcome
        self.assertEqual(self.bp.read_config(), before)
        self.assertNotIn("pwn", self.bp.read_config()["mcpServers"])

    def test_extension_origin_is_accepted(self):
        with self.bp.open(origin=EXT_ORIGIN) as ws:
            snap = json.loads(ws.recv(timeout=10))
            self.assertEqual(snap["type"], "connected")
        # The stdio server finishes starting in the background; poll for its tool.
        self.assertIn("echo", [t["name"] for t in wait_for_tool(self.bp, "echo")])

    def test_non_browser_client_without_origin_is_accepted(self):
        with self.bp.open(origin=None) as ws:
            self.assertEqual(json.loads(ws.recv(timeout=10))["type"], "connected")


@unittest.skipUnless(HAVE_WS, "pip install websockets (>= 13) to run the bridge tests")
class StdioServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="zs-stdio-")
        servers = {
            "fake": {"command": sys.executable, "args": [STDIO_FAKE]},
            "stuck": {"command": sys.executable, "args": [STDIO_FAKE],
                      "env": {"FAKE_MODE": "hang"}},
        }
        cls.bp = BridgeProc(cls.tmp, servers).start()

    @classmethod
    def tearDownClass(cls):
        cls.bp.stop()

    def test_tool_call_round_trip(self):
        with self.bp.open(EXT_ORIGIN) as ws:
            ws.recv(timeout=10)
            r = rpc(ws, {"type": "call_tool", "name": "echo",
                         "arguments": {"text": "hi"}, "timeout": 20000}, "tool_result")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["text"], "echo:hi")

    def test_list_tools_answers_while_a_server_hangs(self):
        """The regression: a silent server used to block the connection for ~20s,
        so pings went unanswered and the extension dropped the socket."""
        with self.bp.open(EXT_ORIGIN) as ws:
            ws.recv(timeout=10)
            t0 = time.time()
            ws.send(json.dumps({"type": "list_tools", "id": 4001}))
            ws.send(json.dumps({"type": "ping", "id": 4002}))
            pong_at = tools_at = None
            tools = None
            deadline = time.time() + 20
            while time.time() < deadline and (pong_at is None or tools is None):
                msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.time())))
                if msg.get("type") == "pong" and pong_at is None:
                    pong_at = time.time() - t0
                if msg.get("type") == "tools" and tools is None:
                    tools_at = time.time() - t0
                    tools = msg["tools"]
            self.assertIsNotNone(pong_at, "bridge did not answer a ping during list_tools")
            self.assertLess(pong_at, 2.0)
            self.assertIsNotNone(tools, "list_tools never answered")
            self.assertLess(tools_at, 8.0)
            self.assertIn("echo", [t["name"] for t in tools])

    def test_status_reports_both_servers(self):
        with self.bp.open(EXT_ORIGIN) as ws:
            snap = json.loads(ws.recv(timeout=10))
        ids = {s["id"]: s for s in snap["servers"]}
        self.assertTrue(ids["fake"]["alive"])
        self.assertTrue(ids["stuck"]["alive"])


@unittest.skipUnless(HAVE_WS, "pip install websockets (>= 13) to run the bridge tests")
class RemoteServerTests(unittest.TestCase):
    """Remote (Streamable HTTP) servers: the bridge reaches them with a URL."""

    def test_remote_tools_auth_headers_and_env_expansion(self):
        remote = FakeRemoteMCP(require_key="s3cret").start()
        tmp = tempfile.mkdtemp(prefix="zs-remote-")
        bp = BridgeProc(tmp, {"web": {"url": remote.url,
                                      "headers": {"X-Api-Key": "${ZS_TEST_KEY}"}}},
                        extra_env={"ZS_TEST_KEY": "s3cret"}).start()
        try:
            tools = wait_for_tool(bp, "web_echo")
            names = [t["name"] for t in tools]
            self.assertIn("web_page2", names, "pagination (nextCursor) was not followed")
            with bp.open(EXT_ORIGIN) as ws:
                ws.recv(timeout=10)
                r = rpc(ws, {"type": "call_tool", "name": "web_echo",
                             "arguments": {"text": "ping"}, "timeout": 20000}, "tool_result")
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["text"], "echo:ping|key=s3cret|proto=2025-06-18")
        finally:
            bp.stop()
            remote.stop()

    def test_sse_replies_are_read(self):
        remote = FakeRemoteMCP(sse=True).start()
        tmp = tempfile.mkdtemp(prefix="zs-sse-")
        bp = BridgeProc(tmp, {"web": {"url": remote.url}}).start()
        try:
            wait_for_tool(bp, "web_echo")
            with bp.open(EXT_ORIGIN) as ws:
                ws.recv(timeout=10)
                r = rpc(ws, {"type": "call_tool", "name": "web_echo",
                             "arguments": {"text": "sse"}, "timeout": 20000}, "tool_result")
            self.assertTrue(r["ok"], r)
            self.assertTrue(r["text"].startswith("echo:sse|"))
        finally:
            bp.stop()
            remote.stop()

    def test_session_expiry_reconnects_and_the_call_succeeds(self):
        remote = FakeRemoteMCP(expire_after=1).start()
        tmp = tempfile.mkdtemp(prefix="zs-expire-")
        bp = BridgeProc(tmp, {"web": {"url": remote.url}}).start()
        try:
            wait_for_tool(bp, "web_echo")
            with bp.open(EXT_ORIGIN) as ws:
                ws.recv(timeout=10)
                first = rpc(ws, {"type": "call_tool", "name": "web_echo",
                                 "arguments": {"text": "one"}, "timeout": 20000}, "tool_result")
                second = rpc(ws, {"type": "call_tool", "name": "web_echo",
                                  "arguments": {"text": "two"}, "timeout": 20000}, "tool_result")
            self.assertTrue(first["ok"], first)
            self.assertTrue(second["ok"], second)  # the expired session was re-created
            self.assertTrue(second["text"].startswith("echo:two|"))
            self.assertEqual(remote.seen_methods.count("initialize"), 2)  # original + one re-handshake
        finally:
            bp.stop()
            remote.stop()

    def test_wrong_key_is_reported_as_an_error(self):
        remote = FakeRemoteMCP(require_key="right").start()
        tmp = tempfile.mkdtemp(prefix="zs-badkey-")
        bp = BridgeProc(tmp, {"web": {"url": remote.url, "headers": {"X-Api-Key": "wrong"}}}).start()
        try:
            time.sleep(2)
            with bp.open(EXT_ORIGIN) as ws:
                snap = json.loads(ws.recv(timeout=10))
            web = [s for s in snap["servers"] if s["id"] == "web"][0]
            self.assertFalse(web["alive"])  # the handshake was refused, not silently accepted
            self.assertEqual(web["tools"], 0)
        finally:
            bp.stop()
            remote.stop()

    def test_add_remote_server_from_the_extension(self):
        remote = FakeRemoteMCP().start()
        tmp = tempfile.mkdtemp(prefix="zs-add-")
        bp = BridgeProc(tmp, {}).start()
        try:
            with bp.open(EXT_ORIGIN) as ws:
                ws.recv(timeout=10)
                r = rpc(ws, {"type": "add_server", "server_id": "web", "url": remote.url,
                             "headers": {"Authorization": "Bearer abc"}}, "server_changed")
            self.assertTrue(r["ok"], r)
            self.assertEqual(bp.read_config()["mcpServers"]["web"],
                             {"url": remote.url, "headers": {"Authorization": "Bearer abc"}})
            # The bridge restarts itself to load the new server; wait for its tools.
            wait_for_tool(bp, "web_echo")
        finally:
            bp.stop()
            remote.stop()

    def test_invalid_urls_are_refused(self):
        tmp = tempfile.mkdtemp(prefix="zs-badurl-")
        bp = BridgeProc(tmp, {}).start()
        try:
            for bad in ("file:///etc/passwd", "ftp://example.com/mcp", "https://", "notaurl"):
                with bp.open(EXT_ORIGIN) as ws:
                    ws.recv(timeout=10)
                    r = rpc(ws, {"type": "add_server", "server_id": "x", "url": bad}, "server_changed")
                self.assertFalse(r["ok"], f"accepted {bad!r}")
                self.assertIn("http", (r.get("error") or "").lower())
            self.assertEqual(bp.read_config()["mcpServers"], {})
        finally:
            bp.stop()

    def test_stdio_add_still_works(self):
        tmp = tempfile.mkdtemp(prefix="zs-stdioadd-")
        bp = BridgeProc(tmp, {}).start()
        try:
            with bp.open(EXT_ORIGIN) as ws:
                ws.recv(timeout=10)
                r = rpc(ws, {"type": "add_server", "server_id": "local", "command": sys.executable,
                             "args": [STDIO_FAKE]}, "server_changed")
            self.assertTrue(r["ok"], r)
            wait_for_tool(bp, "echo")
        finally:
            bp.stop()


DEVKIT_TOOLS = ("project_overview", "script_write", "script_verify", "luau_check",
                "script_history", "workspace", "studio_view", "playtest")


@unittest.skipUnless(HAVE_WS, "pip install websockets (>= 13) to run the bridge tests")
class DevKitTests(unittest.TestCase):
    """The Dev Kit end to end through the REAL bridge: the extension's WebSocket
    message -> MCPManager.call -> local_tools -> execute_luau -> a fake Studio
    (tests/fake_studio_mcp.py) that persists its place to a JSON file this test
    can read back. Nothing here needs Roblox."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="zs-devkit-bridge-")
        cls.state = os.path.join(cls.tmp, "place.json")
        cls.data = os.path.join(cls.tmp, "data")
        cls.bp = BridgeProc(
            cls.tmp,
            {"roblox": {"command": sys.executable, "args": [STUDIO_FAKE]}},
            extra_env={"ZS_BRIDGE_DATA_DIR": cls.data,
                       "FAKE_STUDIO_STATE": cls.state,
                       "FAKE_STUDIO_PLACE": "FakePlace"}).start()
        wait_for_tool(cls.bp, "execute_luau")

    @classmethod
    def tearDownClass(cls):
        cls.bp.stop()

    def place(self):
        with open(self.state, encoding="utf-8") as f:
            return json.load(f)

    def call(self, name, args=None, timeout=60000):
        with self.bp.open(EXT_ORIGIN) as ws:
            ws.recv(timeout=10)
            return rpc(ws, {"type": "call_tool", "name": name, "arguments": args or {},
                            "timeout": timeout}, "tool_result", timeout=90)

    def test_dev_kit_commands_are_advertised_next_to_studios_own(self):
        tools = wait_for_tool(self.bp, "script_write")
        by_name = {t["name"]: t for t in tools}
        for n in DEVKIT_TOOLS:
            self.assertIn(n, by_name, f"{n} was not advertised")
            self.assertEqual(by_name[n]["server"], "zeroscript")
            self.assertTrue(by_name[n]["local"])
            self.assertEqual(by_name[n]["inputSchema"]["type"], "object")
            self.assertTrue(by_name[n]["description"])
        self.assertIn("execute_luau", by_name)          # Studio's own tools untouched
        self.assertEqual(by_name["execute_luau"]["server"], "roblox")

    def test_status_reports_the_dev_kit_as_its_own_always_alive_server(self):
        with self.bp.open(EXT_ORIGIN) as ws:
            snap = json.loads(ws.recv(timeout=10))
        ids = {s["id"]: s for s in snap["servers"]}
        self.assertIn("zeroscript", ids)
        self.assertTrue(ids["zeroscript"]["alive"])
        self.assertEqual(ids["zeroscript"]["tools"], len(DEVKIT_TOOLS))
        self.assertTrue(ids["roblox"]["alive"])

    def test_luau_check_answers_without_touching_studio(self):
        r = self.call("luau_check", {"code": "if true then\nreturn 1"})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["text"].startswith("ERROR: luau_check found 1 error"), r["text"])
        self.assertIn("line 1", r["text"])

    def test_project_overview_maps_the_place_in_one_call(self):
        r = self.call("project_overview", {"console": False})
        self.assertTrue(r["ok"], r)
        self.assertIn('"FakePlace"', r["text"])
        self.assertIn("game.ServerScriptService.Main | Script |", r["text"])
        self.assertIn("── Instance tree ──", r["text"])

    def test_script_write_lands_in_studio_in_one_call(self):
        r = self.call("script_write", {"files": [
            {"path": "game.ServerScriptService.Shop.Handler", "className": "Script",
             "code": "local Shop = {}\nreturn Shop\n"},
            {"path": "game.ServerScriptService.Main", "code": 'print("v2")\n'}]})
        self.assertTrue(r["ok"], r)
        self.assertIn("2 written (1 created, 1 updated)", r["text"])
        scripts = self.place()["scripts"]
        self.assertEqual(scripts["game.ServerScriptService.Shop.Handler"][1],
                         "local Shop = {}\nreturn Shop\n")
        self.assertEqual(scripts["game.ServerScriptService.Main"][1], 'print("v2")\n')
        # ...and it left an undo point behind on the bridge's disk
        hist = self.call("script_history", {"action": "list"})
        self.assertIn("auto:before-write", hist["text"])

    def test_a_syntax_error_never_reaches_studio(self):
        # Not an exception: a normal tool result whose text starts with ERROR, so
        # the extension paints the chip red and the model reads the lint report
        # (see bodyLooksFailed in core/main.js) - with the file and line in it.
        r = self.call("script_write", {"files": [
            {"path": "game.ServerScriptService.Broken", "code": "if true then\n"}]})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["text"].startswith("ERROR: script_write wrote NOTHING"), r["text"])
        self.assertIn("luau_check found syntax errors", r["text"])
        self.assertIn("line 1", r["text"])
        self.assertNotIn("game.ServerScriptService.Broken", self.place()["scripts"])
        # nothing was sent to Studio either: no execute_luau/multi_edit for it
        self.assertNotIn("Broken", json.dumps(self.place()["calls"]))

    def test_script_verify_compiles_the_place(self):
        r = self.call("script_verify", {})
        self.assertTrue(r["ok"], r)
        self.assertIn("script(s) compiled in Studio", r["text"])
        self.assertIn("0 FAILED", r["text"])

    def test_workspace_mirrors_the_place_to_disk_and_greps_it_locally(self):
        pull = self.call("workspace", {"action": "pull"})
        self.assertTrue(pull["ok"], pull)
        self.assertIn("script file(s) written", pull["text"])
        mirror = os.path.join(self.data, "workspace", "FakePlace")
        self.assertTrue(os.path.isfile(os.path.join(mirror, "ServerScriptService", "Main.lua")))
        # Config.lua is never modified by another test, so this is order-proof.
        grep = self.call("workspace", {"action": "grep", "pattern": "version"})
        self.assertTrue(grep["ok"], grep)
        self.assertIn("ReplicatedStorage/Modules/Config.lua:1", grep["text"])
        self.assertIn("1 match(es) in 1 file(s)", grep["text"])

    def test_studio_view_selects_and_reports_a_missing_path(self):
        r = self.call("studio_view", {"select": ["game.ServerScriptService.Main", "game.Nope.X"]})
        self.assertTrue(r["ok"], r)
        self.assertIn("selected 1", r["text"])
        self.assertIn("MISS game.Nope.X", r["text"])
        self.assertEqual(self.place()["selection"], ["game.ServerScriptService.Main"])

    def test_playtest_runs_the_place_and_stops_it(self):
        r = self.call("playtest", {"seconds": 1})
        self.assertTrue(r["ok"], r)
        self.assertIn("play-tested for 1s in Studio, stopped", r["text"])
        self.assertFalse(self.place()["playing"])

    def test_an_unknown_dev_kit_command_is_refused(self):
        r = self.call("deploy_the_game", {})
        self.assertFalse(r["ok"], r)
        self.assertIn("unknown tool", r.get("error") or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
