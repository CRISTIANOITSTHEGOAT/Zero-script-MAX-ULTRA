#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the shop system through the whole Dev Kit pipeline and print every step.

    python3 examples/shop-system/pipeline_demo.py

Honesty first - there is no Roblox Studio in this repository's CI, so this demo
runs the REAL Dev Kit tools (local_tools.py, unchanged) against
tests/fake_studio.py, an in-memory place that speaks the same tiny line
protocol the real Studio plugin does. What that means per step:

  GENUINE (no Studio involved at all, identical to a real session)
    luau_check        the same linter that gates script_write in production
    workspace         the same mirror/grep on disk, same manifest format
    script_history    the same snapshots + index on disk

  SIMULATED (real Python, fake Studio on the other end of the wire)
    project_overview  parses a real INVENTORY report - the bytes are fake
    script_write      real chunking/read-back/verify logic, fake Source writes
    script_verify     real NOLOAD/SKIP/FAIL parsing, fake "compile"
    studio_view       real VIEW report parsing, fake Selection
    playtest          real Output scanning, fake start/stop + seeded Output

The seeded Output lines are marked SEEDED below: they stand in for what a real
play session would have printed, so the error-detection path is exercised.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, PKG)
sys.path.insert(0, os.path.join(PKG, "tests"))

# Keep the demo's mirror + undo history out of the user's real Dev Kit data.
os.environ.setdefault("ZS_BRIDGE_DATA_DIR", tempfile.mkdtemp(prefix="zs-shop-demo-"))

import fake_studio  # noqa: E402  (needs the sys.path above)

studio = None   # both are set in main() once the fake place exists
L = None


def read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


CONFIG = read("ShopConfig.lua")
SERVER = read("ShopServer.lua")
CLIENT = read("ShopClient.lua")

# Where each script belongs, per the architecture contract:
#   ReplicatedStorage.<System>Config      ModuleScript - pure data + export types
#   ServerScriptService.<System>Server    Script       - remotes, currency, DataStore
#   StarterGui.<System>Client             LocalScript  - rendering, tweens, input
TARGETS = [
    ("game.ReplicatedStorage.ShopConfig", "ModuleScript", CONFIG),
    ("game.ServerScriptService.ShopServer", "Script", SERVER),
    ("game.StarterGui.ShopClient", "LocalScript", CLIENT),
]


def build_studio():
    """A place that already has content, so the INSPECT step has something to
    report and the writes are genuinely creating new scripts."""
    fake = fake_studio.FakeStudio(
        place="Shop Demo Place",
        scripts={
            "game.ServerScriptService.Bootstrap": (
                "Script",
                "--!strict\n-- pre-existing: loads the place, prints a heartbeat\n"
                "print(\"[Bootstrap] place loaded\")\n",
            ),
        },
        instances={
            "game.Workspace": "Workspace",
            "game.Workspace.Baseplate": "Part",
            "game.Workspace.SpawnLocation": "SpawnLocation",
            "game.ReplicatedStorage": "ReplicatedStorage",
            "game.ServerScriptService": "ServerScriptService",
            "game.ServerStorage": "ServerStorage",
            "game.StarterGui": "StarterGui",
            "game.StarterPlayer": "StarterPlayer",
            "game.StarterPlayer.StarterCharacterScripts": "StarterCharacterScripts",
            "game.Lighting": "Lighting",
        },
    )
    # SEEDED Output: what a play session of this shop would have printed.
    fake.console = {
        "Server": [
            "[Bootstrap] place loaded",
            "[ShopServer] DataStore unavailable in this session - using an in-memory profile (this is expected in Studio without API access enabled)",
            "[ShopServer] player 1 profile ready: Cash=250",
            "[ShopServer] PURCHASE ok: player 1 bought SpeedBoost x1 for 120 (Cash 250 -> 130)",
            "SEEDED ServerScriptService.ShopServer:84: attempt to index nil with 'WaitForChild' (x1)",
        ],
        "Client": [
            "SEEDED [ShopClient] Shop opened with the B key - tab 'Boosts'",
            "SEEDED StarterGui.ShopClient:412: Infinite yield possible on 'ReplicatedStorage.ShopRemotes.PurchaseResult'",
        ],
    }
    return fake


def step(n, title, tool, args, studio, note=""):
    print()
    print("=" * 78)
    print("STEP %d - %s" % (n, title))
    print("  tool: %s%s" % (tool, ("  args: " + _brief(args)) if args else ""))
    if note:
        print("  note: " + note)
    print("-" * 78)
    try:
        out = L.run(tool, args, studio)
    except Exception as e:  # ToolError and anything the tool forgot to catch
        print("ERROR %s: %s" % (type(e).__name__, e))
        return None
    print(out["text"])
    return out


def _brief(args):
    parts = []
    for k, v in (args or {}).items():
        if k in ("code", "files"):
            parts.append("%s=<%s>" % (k, _size(v)))
        else:
            parts.append("%s=%r" % (k, v))
    return "{" + ", ".join(parts) + "}"


def _size(v):
    if isinstance(v, str):
        return "%d chars" % len(v)
    if isinstance(v, list):
        return "%d file(s), %d chars" % (len(v), sum(len(str(f.get("code", ""))) for f in v if isinstance(f, dict)))
    return "?"


def main():
    global L, studio
    fake = build_studio()
    studio, L = fake_studio.as_studio(fake)

    print("ZeroScript Dev Kit - shop system pipeline demo")
    print("data dir (mirror + undo history): %s" % os.environ["ZS_BRIDGE_DATA_DIR"])
    print("Studio side: SIMULATED by tests/fake_studio.py; luau_check/workspace/history: GENUINE")

    # -- 1. INSPECT -------------------------------------------------------
    step(1, "INSPECT the place", "project_overview",
         {"root": "game", "depth": 3, "scripts": True}, studio,
         "one call replaces the old tree-walk + per-script read warm-up")

    # -- 2. PRE-FLIGHT LINT ----------------------------------------------
    step(2, "PRE-FLIGHT LINT (before any write)", "luau_check",
         {"files": [{"path": p.rsplit(".", 1)[1] + ".lua", "code": c} for p, _, c in TARGETS],
          "context": "script"}, studio,
         "genuine: this is the exact gate script_write re-runs itself")

    # -- 3. ATOMIC WRITE --------------------------------------------------
    step(3, "ATOMIC WRITE - all 3 scripts in ONE call", "script_write",
         {"files": [{"path": p, "className": c, "code": s} for p, c, s in TARGETS],
          "check": True, "history": True, "select": True}, studio,
         "simulated Studio; real lint-all-or-write-nothing + read-back confirm")

    # -- 4. VERIFY --------------------------------------------------------
    step(4, "VERIFY they compile in Studio", "script_verify",
         {"paths": [p for p, _, _ in TARGETS]}, studio,
         "simulated compile; real NOLOAD/SKIP/FAIL parsing")

    # -- 5. FIND CODE (mirror + grep) ------------------------------------
    step(5, "MIRROR the place locally", "workspace", {"action": "pull"}, studio)
    step(6, "GREP the mirror", "workspace",
         {"action": "grep", "pattern": "RemoteEvent", "context": 1}, studio,
         "genuine grep over real files on disk")
    step(7, "MIRROR STATUS", "workspace", {"action": "status", "compare_live": True}, studio)

    # -- 8. FOCUS ---------------------------------------------------------
    step(8, "FOCUS Studio on the client script", "studio_view",
         {"select": ["game.StarterGui.ShopClient"],
          "focus": "game.StarterGui.ShopClient"}, studio,
         "so the human sees what the agent just wrote")

    # -- 9. TEST ----------------------------------------------------------
    step(9, "PLAYTEST + capture Output (press B to open the shop)", "playtest",
         {"seconds": 3, "stop": True, "capture": "both",
          "keys": [{"action": "keyPress", "key_code": "B"},
                   {"action": "wait", "wait_time_ms": 400}]}, studio,
         "SEEDED Output - the 3 error/warning lines are planted to exercise "
         "the scan; the input actions really are forwarded")

    # -- 10. UNDO surface -------------------------------------------------
    step(10, "UNDO surface - what is on record", "script_history",
         {"action": "list"}, studio,
         "genuine: real snapshot files + index.json on disk")

    print()
    print("=" * 78)
    print("Studio MCP calls made: %d   (execute_luau: %d)" % (len(fake.calls), fake.luau_calls))
    print("Selection after studio_view: %s" % (fake.selection or "(none)"))
    print("Playing at the end: %s" % fake.playing)
    print("Input forwarded to the Client DataModel: %s" % (fake.input_sent or "(none)"))
    mirror = os.path.join(os.environ["ZS_BRIDGE_DATA_DIR"], "workspace")
    for root, dirs, files in os.walk(mirror):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(root, f)
            print("  mirror: %s (%d bytes)" % (os.path.relpath(p, os.environ["ZS_BRIDGE_DATA_DIR"]),
                                                os.path.getsize(p)))
    hist = os.path.join(os.environ["ZS_BRIDGE_DATA_DIR"], "history")
    for root, dirs, files in os.walk(hist):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(root, f)
            print("  history: %s (%d bytes)" % (os.path.relpath(p, os.environ["ZS_BRIDGE_DATA_DIR"]),
                                                os.path.getsize(p)))
    print("=" * 78)


if __name__ == "__main__":
    main()
