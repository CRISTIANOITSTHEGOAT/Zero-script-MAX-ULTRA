# SPDX-License-Identifier: GPL-3.0-or-later
"""A fake Roblox Studio good enough to exercise the Dev Kit (local_tools.py)
without Roblox: it holds a place in memory and speaks the SAME tiny line
protocol the Dev Kit's Luau snippets return, so the Python side is tested
against exactly the bytes it has to parse in production.

It understands the snippets by their `zs_out("KIND")` marker and re-implements
just enough of each one:

    PLACE      -> place name + Edit/Play mode
    INVENTORY  -> tree lines + one line per script (path, class, bytes, first line)
    SOURCES    -> length-prefixed/base64 sources for the requested paths
    WRITE      -> create/overwrite scripts, reading the Source back to confirm
    VERIFY     -> "compile" every script (a source containing --ZS-BAD fails)
    VIEW       -> select instances, report the ones that do not exist

Knobs for the unhappy paths (see test_local_tools.py):
    offline            every call answers "Unable to find an active Studio instance"
    fail_source_write  WRITE reports a Source permission failure, so the bridge
                       has to fall back to multi_edit
    no_loadstring      VERIFY reports NOLOAD
    max_reply_bytes    SOURCES replies bigger than this come back as garbage,
                       which is how the chunk-halving retry gets tested
"""
import base64
import re

# The classes Studio's inventory reports as scripts (it filters on
# IsA("LuaSourceContainer")).
LUA_SOURCE_CLASSES = frozenset({"Script", "LocalScript", "ModuleScript"})

PROTOCOL_VERSION = "ZS-DEVKIT-FAKE/1"

KIND_RE = re.compile(r'zs_out\("([A-Z]+)"\)')
PAYLOAD_RE = re.compile(r"zs_records\((\[=*\[)(.*?)(\]=*\])\)", re.S)
SUB_RE = re.compile(r"local (MAX_DEPTH|MAX_NODES|MAX|WANT_SCRIPTS|WANT_TREE|FOCUS|wantSelect|wantClear) = (.+)")


def parse_records(payload):
    """Python twin of the Luau zs_records(): byte-exact, length-prefixed."""
    data = (payload or "").encode("utf-8")
    out, i, n = [], 0, len(data)

    def line():
        nonlocal i
        e = data.find(b"\n", i)
        if e == -1:
            e = n
        s = data[i:e].decode("utf-8", "replace")
        i = e + 1
        return s

    while i < n:
        head = line()
        if head == "END":
            break
        m = re.match(r"^PATH\t(.*)$", head)
        if not m:
            continue
        rec = {"path": m.group(1), "class": "Script", "code": ""}
        for _ in range(2):
            h = line()
            km = re.match(r"^([A-Z]+)\t(.*)$", h)
            if km and km.group(1) == "CLASS" and km.group(2):
                rec["class"] = km.group(2)
            if km and km.group(1) == "CODE":
                clen = int(km.group(2) or 0)
                if clen:
                    rec["code"] = data[i:i + clen].decode("utf-8", "replace")
                    i += clen
                    if data[i:i + 1] == b"\n":
                        i += 1
                break
        out.append(rec)
    return out


class FakeStudio:
    """An in-memory place + the Studio MCP tools that touch it."""

    TOOLS = ("execute_luau", "multi_edit", "script_read", "get_console_output",
             "start_stop_play", "list_roblox_studios", "get_studio_state",
             "inspect_instance", "search_game_tree",
             "user_keyboard_input", "user_mouse_input")

    def __init__(self, place="TestPlace", scripts=None, instances=None):
        self.place = place
        self.scripts = {}          # "game.ServerScriptService.Main" -> {"class","source"}
        self.folders = set()       # created containers, so the tree can show them
        self.instances = dict(instances or {
            "game.Workspace": "Workspace",
            "game.Workspace.Baseplate": "Part",
            "game.ReplicatedStorage": "ReplicatedStorage",
            "game.ServerScriptService": "ServerScriptService",
            "game.ServerStorage": "ServerStorage",
            "game.StarterPlayer": "StarterPlayer",
            "game.Lighting": "Lighting",
        })
        self.playing = False
        self.selection = []
        self.console = {"Server": ["Server started"], "Client": []}
        self.calls = []            # every tool call, for assertions
        self.luau_calls = 0
        # knobs
        self.offline = False
        self.fail_source_write = False
        self.no_loadstring = False
        self.max_reply_bytes = 0   # 0 = unlimited
        self.max_code_bytes = 0    # 0 = unlimited; over it, Luau "fails to parse"
        self.input_sent = []
        for path, (cls, src) in (scripts or {}).items():
            self.scripts[self.norm(path)] = {"class": cls, "source": src}

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def norm(path):
        p = re.sub(r"\s+", "", str(path or "")).strip(".")
        if not p.lower().startswith(("game.", "datamodel.")):
            p = "game." + p
        return "game." + p.split(".", 1)[1]

    def add_script(self, path, cls, source):
        self.scripts[self.norm(path)] = {"class": cls, "source": source}

    def source_of(self, path):
        return self.scripts.get(self.norm(path), {}).get("source")

    def class_of(self, path):
        """The class Studio would report for this path, or None if it does not
        exist. One definition, used by the inventory, verify and inspect paths,
        so they cannot disagree about what a path is."""
        p = self.norm(path)
        cls = self.scripts.get(p, {}).get("class") or self.instances.get(p)
        if cls:
            return cls
        return "Folder" if p in self.folders else None

    def has(self, name):
        return name in self.TOOLS

    # -- the tree ----------------------------------------------------------
    def _tree(self):
        """Nested {name: {className, children}} built from every known path."""
        root = {}
        paths = list(self.instances) + list(self.scripts) + sorted(self.folders)
        for p in paths:
            segs = [s for s in self.norm(p).split(".")[1:] if s]
            node = root
            for i, seg in enumerate(segs):
                full = "game." + ".".join(segs[:i + 1])
                cls = (self.scripts.get(full, {}).get("class")
                       or self.instances.get(full)
                       or ("Folder" if i < len(segs) - 1 else "Part"))
                if i == len(segs) - 1 and full in self.scripts:
                    cls = self.scripts[full]["class"]
                node = node.setdefault(seg, {"class": cls, "children": {}})
                node["children"] = node.get("children") or {}
                node = node["children"]
        return root

    # -- tool calls --------------------------------------------------------
    def call(self, name, arguments=None, timeout=None):
        """MCP-shaped: {"ok","text","error"} - never raises."""
        arguments = arguments or {}
        self.calls.append((name, arguments))
        if self.offline:
            return {"ok": True, "text": "Unable to find an active Studio instance.", "error": None}
        if not self.has(name):
            return {"ok": False, "text": "", "error": f"unknown tool '{name}'"}
        try:
            text = getattr(self, "_" + name)(arguments)
        except Exception as e:  # a fake that crashes would look like a Studio bug
            return {"ok": False, "text": "", "error": f"fake studio: {type(e).__name__}: {e}"}
        return {"ok": True, "text": text, "error": None}

    # -- individual tools --------------------------------------------------
    def _list_roblox_studios(self, a):
        return '{"studios": [{"name": "%s", "active": true}]}' % self.place

    def _get_studio_state(self, a):
        return "Available DataModels: Edit%s" % (", Server, Client" if self.playing else "")

    def _get_console_output(self, a):
        which = a.get("datamodel_type") or a.get("datamodel") or "Server"
        if which not in self.console and a:
            return ""
        return "\n".join(self.console.get(which.capitalize(), []))

    def _start_stop_play(self, a):
        self.playing = bool(a.get("is_start"))
        return "Play testing started." if self.playing else "Play testing stopped."

    def _inspect_instance(self, a):
        path = self.norm(a.get("path") or a.get("instance_path") or "game")
        cls = self.class_of(path)
        if not cls:
            return f"Instance not found: {path}"
        return f"{path} ({cls})\nProperties: Name = {path.split('.')[-1]}"

    def _search_game_tree(self, a):
        q = str(a.get("query") or "").lower()
        return "\n".join(p for p in list(self.instances) + list(self.scripts) if q in p.lower())

    def _script_read(self, a):
        path = self.norm(a.get("target_file") or a.get("file_path") or "")
        src = self.source_of(path)
        if src is None:
            return f"Script not found: {path}"
        return "\n".join(f"{i + 1}\u2192{ln}" for i, ln in enumerate(src.splitlines()))

    def _multi_edit(self, a):
        path = self.norm(a.get("file_path") or a.get("target_file") or "")
        cls = a.get("className") or "Script"
        edits = a.get("edits") or []
        cur = self.source_of(path)
        if cur is None:
            if not edits or edits[0].get("old_string", "") != "":
                return f"{path} does not exist and no create edit was given"
            cur = ""
            self.scripts[path] = {"class": cls, "source": ""}
        for e in edits:
            old, new = e.get("old_string", ""), e.get("new_string", "")
            if old and old not in cur:
                return f"old_string not found in current content of {path}"
            if old == new:
                return "identical old_string and new_string"
            cur = cur.replace(old, new, 1) if old else new + cur
        self.scripts[path]["source"] = cur
        self.scripts[path]["class"] = self.scripts[path].get("class") or cls
        return f"Applied {len(edits)} edit(s) to {path}"

    def _user_keyboard_input(self, a):
        self.input_sent.append(("keyboard", a))
        if not a.get("actions"):
            return "actions is required"
        return f"Sent {len(a['actions'])} keyboard action(s)."

    def _user_mouse_input(self, a):
        self.input_sent.append(("mouse", a))
        if not a.get("actions"):
            return "actions is required"
        return f"Sent {len(a['actions'])} mouse action(s)."

    def _execute_luau(self, a):
        self.luau_calls += 1
        code = a.get("code") or ""
        if self.max_code_bytes and len(code.encode("utf-8")) > self.max_code_bytes:
            # Studio's generic parse rejection for a chunk it will not swallow -
            # the Dev Kit has to halve the chunk and retry.
            return "Failed to parse command code"
        m = KIND_RE.search(code)
        if not m:
            # Not a Dev Kit snippet: a plain `return <expr>` from the model.
            return ""
        kind = m.group(1)
        payload = ""
        pm = PAYLOAD_RE.search(code)
        if pm:
            payload = pm.group(2).lstrip("\n")
        subs = {k: v.strip() for k, v in SUB_RE.findall(code)}
        handler = getattr(self, "_zs_" + kind.lower(), None)
        if handler is None:
            return f"fake studio: unsupported snippet {kind}"
        return handler(payload, subs, code)

    # -- the Dev Kit protocol ---------------------------------------------
    def _report(self, kind, lines):
        return "\n".join([f"ZS-{kind} 1"] + [str(x) for x in lines] + ["END"])

    def _zs_place(self, payload, subs, code):
        return self._report("PLACE", [f"PLACE\t{self.place}",
                                      f"RUN\t{'Playing' if self.playing else 'Edit'}"])

    def _first_line(self, src):
        for ln in (src or "").splitlines():
            t = ln.strip()
            if t and not t.startswith("--"):
                return t.replace("\t", " ")[:90]
        return ""

    def _zs_inventory(self, payload, subs, code):
        want_tree = subs.get("WANT_TREE", "true") == "true"
        want_scripts = subs.get("WANT_SCRIPTS", "true") == "true"
        max_depth = int(subs.get("MAX_DEPTH", 4) or 4)
        max_nodes = int(subs.get("MAX_NODES", 400) or 400)
        lines = [f"PLACE\t{self.place}", f"RUN\t{'Playing' if self.playing else 'Edit'}"]
        nodes = [0]
        scripts = []
        found = [0]      # scripts SEEN, whether or not they are listed below
        truncated = [False]

        def walk(node, depth, indent, prefix):
            for name in sorted(node):
                info = node[name]
                full = f"{prefix}.{name}"
                nodes[0] += 1
                if nodes[0] > max_nodes:
                    truncated[0] = True
                    return
                label = f"{indent}{name} ({info['class']})"
                # Studio's inventory only reports LuaSourceContainers, so a
                # fixture that parks a Folder in `scripts` must NOT come back as
                # a script here - otherwise the fake would let a missing filter
                # in the real inventory snippet pass unnoticed.
                if full in self.scripts and self.scripts[full]["class"] in LUA_SOURCE_CLASSES:
                    found[0] += 1
                    src = self.scripts[full]["source"]
                    label += f" [{len(src.encode('utf-8'))} bytes]"
                    if want_scripts:
                        scripts.append("S\t%s\t%s\t%d\t%s" % (
                            full, self.scripts[full]["class"],
                            len(src.encode("utf-8")), self._first_line(src)))
                if want_tree:
                    lines.append("T\t" + label)
                if depth < max_depth:
                    walk(info["children"], depth + 1, indent + "  ", full)
                if truncated[0]:
                    return

        walk(self._tree(), 0, "", "game")
        lines.extend(scripts)
        # Studio counts the LuaSourceContainers it walked, NOT the rows it
        # printed: with WANT_SCRIPTS=false the header still says how many
        # scripts the place has. len(self.scripts) would also count fixtures
        # that are not scripts at all.
        lines.append("N\t%d\t%d\t%s" % (nodes[0], found[0], truncated[0]))
        return self._report("INVENTORY", lines)

    def _zs_sources(self, payload, subs, code):
        lines = [f"PLACE\t{self.place}"]
        for rec in parse_records(payload):
            path = self.norm(rec["path"])
            if path not in self.scripts:
                lines.append(f"MISS\t{rec['path']}\tno child '{path.split('.')[-1]}'")
                continue
            info = self.scripts[path]
            src = info["source"].encode("utf-8")
            b64 = base64.b64encode(src).decode("ascii")
            lines.append("FILE\t%s\t%s\tB64\t%d\t%d" % (path, info["class"], len(src), len(b64)))
            lines.append(b64)
        out = self._report("SOURCES", lines)
        if self.max_reply_bytes and len(out.encode("utf-8")) > self.max_reply_bytes:
            # Emulate a transport that mangles an over-big reply: the parser must
            # notice and halve the chunk.
            return out[: self.max_reply_bytes // 2] + "\n<<<TRUNCATED>>>"
        return out

    def _zs_write(self, payload, subs, code):
        lines = [f"PLACE\t{self.place}"]
        selected = []
        for rec in parse_records(payload):
            path = self.norm(rec["path"])
            for i in range(2, len(path.split("."))):
                self.folders.add("game." + ".".join(path.split(".")[1:i]))
            existing = self.scripts.get(path)
            if existing and existing["class"] not in ("Script", "LocalScript", "ModuleScript"):
                lines.append(f"FAIL\t{rec['path']}\talready exists as a {existing['class']}")
                continue
            if self.fail_source_write:
                lines.append(f"FAIL\t{rec['path']}\tcannot write Source (Source is not available in this context)")
                continue
            created = existing is None
            old = existing["source"] if existing else ""
            if old == rec["code"]:
                lines.append("OK\tunchanged\t%s\t%s\t%d\t%d" % (
                    path, rec["class"], len(old.encode("utf-8")), len(old.encode("utf-8"))))
            else:
                self.scripts[path] = {"class": rec["class"], "source": rec["code"]}
                lines.append("OK\t%s\t%s\t%s\t%d\t%d" % (
                    "created" if created else "updated", path, rec["class"],
                    len(old.encode("utf-8")), len(rec["code"].encode("utf-8"))))
            selected.append(path)
        if subs.get("wantSelect") == "true" and selected:
            self.selection = selected
            lines.append(f"SEL\t{len(selected)}")
        return self._report("WRITE", lines)

    def _zs_verify(self, payload, subs, code):
        lines = [f"PLACE\t{self.place}"]
        if self.no_loadstring:
            lines.append("NOLOAD\tloadstring is unavailable in this context, so scripts cannot be compiled here")
            return self._report("VERIFY", lines)
        recs = parse_records(payload)
        # Studio compiles LuaSourceContainers only: an explicit path that is not
        # one comes back as a SKIP with its class, and a whole-place scan never
        # counts non-scripts. Without this the fake would happily "compile" a
        # Folder and hide a missing filter in the real snippet.
        targets = []
        if recs:
            for r in recs:
                path = self.norm(r["path"])
                cls = self.class_of(path)
                if cls is None:
                    lines.append("SKIP\t%s\tnot found" % r["path"])
                elif cls not in LUA_SOURCE_CLASSES:
                    lines.append("SKIP\t%s\tnot a script (%s)" % (r["path"], cls))
                else:
                    targets.append(path)
        else:
            targets = sorted(p for p, v in self.scripts.items()
                             if v["class"] in LUA_SOURCE_CLASSES)
        cap = int(subs.get("MAX", 400) or 400)
        checked = bad = 0
        for path in targets:
            if checked >= cap:
                lines.append(f"CAP\t{len(targets)}\t{cap}")
                break
            src = self.scripts[path]["source"]
            if not src:
                continue
            checked += 1
            if "--ZS-BAD" in src:
                bad += 1
                lineno = src.splitlines().index("--ZS-BAD") + 1
                lines.append('FAIL\t%s\t%s\t[string "..."]:%d: unexpected symbol' % (
                    path, self.scripts[path]["class"], lineno))
        lines.append("N\t%d\t%d\t%d" % (len(targets), checked, bad))
        return self._report("VERIFY", lines)

    def _zs_view(self, payload, subs, code):
        lines = [f"PLACE\t{self.place}"]
        focus = subs.get("FOCUS", '""').strip('"')
        if subs.get("wantClear") == "true":
            self.selection = []
            lines.append("OK\tselection cleared")
        recs = parse_records(payload)
        found, missing = [], []
        for rec in recs:
            path = self.norm(rec["path"])
            if path in self.scripts or path in self.instances:
                found.append(path)
            else:
                missing.append(f"{rec['path']} (no child '{path.split('.')[-1]}')")
        if found:
            self.selection = found
            lines.append("OK\tselected %d: %s" % (len(found), ", ".join(p[5:] for p in found)))
        for m in missing:
            lines.append("MISS\t" + m)
        if focus:
            p = self.norm(focus)
            if p in self.scripts or p in self.instances:
                lines.append("OK\tcamera framed " + p[5:])
            else:
                lines.append("MISS\t%s (not found)" % focus)
        return self._report("VIEW", lines)


def as_studio(fake, budget=None):
    """A local_tools.Studio wired to a FakeStudio (import-free, so the test can
    build it before local_tools is imported)."""
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import local_tools
    return local_tools.Studio(call=fake.call, has=fake.has, budget=budget), local_tools
