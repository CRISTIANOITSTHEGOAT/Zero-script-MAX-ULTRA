# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the ZeroScript Dev Kit (local_tools.py) - the bridge-side commands
that give the AI a real developer workflow.

No Roblox Studio is needed: tests/fake_studio.py holds a place in memory and
answers in the SAME line protocol the Dev Kit's Luau snippets return, so the
parsing, chunking, mirroring, history and fallback paths are all exercised for
real.

Run from the bridge folder:
    python -m unittest discover -s tests -v
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, BRIDGE_DIR)

import local_tools as L  # noqa: E402
from fake_studio import FakeStudio, parse_records  # noqa: E402

# Captured at import: DevKitCase zeroes L.INPUT_SETTLE_S so the suite does not
# really sleep, and the shipped default is still asserted from this copy.
DEFAULT_INPUT_SETTLE = L.INPUT_SETTLE_S


def run(tool, args, studio):
    """local_tools.run() unwrapped - tests want the text, and ToolError as itself."""
    return L.run(tool, args, studio)["text"]


class LuauCheckTests(unittest.TestCase):
    """The static pre-flight: it must catch real breakage and stay quiet on good
    code, because a linter that cries wolf gets ignored (and blocks writes)."""

    def check(self, code, **kw):
        return L.luau_check(code, "t", **kw)

    def sev(self, findings):
        return [f["severity"] for f in findings]

    def test_clean_code_is_clean(self):
        code = (
            'local Players = game:GetService("Players")\n'
            'local part = Instance.new("Part")\n'
            "part.Size = Vector3.new(4, 1, 4)\n"
            "part.Parent = workspace\n"
            "Players.PlayerAdded:Connect(function(player)\n"
            '    local character = player.Character or player.CharacterAdded:Wait()\n'
            "    task.wait(0.5)\n"
            "    if character and character.PrimaryPart then\n"
            "        part.CFrame = character.PrimaryPart.CFrame\n"
            "    end\n"
            "end)\n"
        )
        self.assertEqual(self.check(code), [])

    def test_unclosed_block_is_an_error_with_the_opening_line(self):
        f = self.check("local x = 1\nif x == 1 then\n    x = 2\n")
        self.assertEqual(self.sev(f), ["error"])
        self.assertEqual(f[0]["line"], 2)
        self.assertIn("if/then", f[0]["message"])

    def test_extra_end_is_an_error(self):
        f = self.check("local x = 1\nend\n")
        self.assertEqual(self.sev(f), ["error"])
        self.assertIn("extra 'end'", f[0]["message"])

    def test_nested_functions_and_loops_balance(self):
        code = (
            "for i = 1, 10 do\n"
            "    while true do\n"
            "        task.wait(1)\n"
            "        local f = function() return i end\n"
            "        if f() then break end\n"
            "    end\n"
            "end\n"
        )
        self.assertEqual([x for x in self.check(code) if x["severity"] == "error"], [])

    def test_repeat_needs_until_not_end(self):
        f = self.check("repeat\n    task.wait(1)\nuntil false\n")
        self.assertEqual([x for x in f if x["severity"] == "error"], [])
        f = self.check("repeat\n    task.wait(1)\nend\n")
        self.assertTrue(any("until" in x["message"] for x in f))

    def test_chat_corrupted_punctuation_is_an_error(self):
        f = self.check('local s = \u201chello\u201d\nlocal n = 1 \u2013 2\n')
        errors = [x for x in f if x["severity"] == "error"]
        self.assertEqual(len(errors), 3)
        self.assertTrue(all("U+" in x["message"] for x in errors))

    def test_curly_quotes_inside_a_real_string_are_fine(self):
        # Inside an ASCII-delimited string a curly quote is just text (a UI label);
        # in code position it is the corruption caught by the test above.
        self.assertEqual(self.check('local label = "\u201cWell done\u201d"\nreturn label\n'), [])

    def test_unclosed_string_is_reported(self):
        f = self.check('local s = "unterminated\nreturn s\n')
        self.assertTrue(any(x["severity"] == "error" and "unterminated" in x["message"] for x in f))

    def test_unbalanced_paren_is_reported(self):
        f = self.check("local n = (1 + 2\nreturn n\n")
        self.assertTrue(any("unclosed '('" in x["message"] for x in f))

    def test_waitforchild_without_a_timeout_warns(self):
        f = self.check('local c = workspace:WaitForChild("Shop")\nreturn c\n')
        self.assertTrue(any("WaitForChild" in x["message"] for x in f))
        f = self.check('local c = workspace:WaitForChild("Shop", 5)\nreturn c\n')
        self.assertFalse(any("WaitForChild" in x["message"] for x in f))

    def test_infinite_loop_without_a_yield_warns(self):
        f = self.check("while true do\n    local n = 1\nend\n")
        self.assertTrue(any("while true do" in x["message"] for x in f))
        f = self.check("while true do\n    task.wait(1)\nend\n")
        self.assertFalse(any("while true do" in x["message"] for x in f))

    def test_unparented_instance_warns(self):
        self.assertTrue(any(".Parent" in x["message"]
                            for x in self.check('local p = Instance.new("Part")\nreturn p\n')))
        self.assertFalse(any(".Parent" in x["message"]
                             for x in self.check('local p = Instance.new("Part")\np.Parent = workspace\n')))

    def test_deprecated_globals_warn(self):
        f = self.check("wait(1)\nspawn(function() end)\ndelay(1, function() end)\n")
        self.assertEqual(len([x for x in f if x["severity"] == "warn"]), 3)

    def test_misspelled_service_suggests_the_real_one(self):
        f = self.check('local S = game:GetService("Playrs")\nreturn S\n')
        self.assertTrue(any('did you mean "Players"' in x["message"] for x in f))

    def test_print_note_only_in_execute_context(self):
        code = 'print("hi")\n'
        self.assertFalse(any("print()" in x["message"] for x in self.check(code)))
        self.assertTrue(any("print()" in x["message"] for x in self.check(code, context="execute")))

    def test_comments_and_long_strings_do_not_confuse_the_block_count(self):
        code = (
            "--[==[ a comment with end and ] and \" quotes ]==]\n"
            'local s = [==[ if x then end ]==]\n'
            "-- if y then\n"
            "return s\n"
        )
        self.assertEqual([x for x in self.check(code) if x["severity"] == "error"], [])

    def test_the_dev_kits_own_luau_passes_the_linter(self):
        """Self-test: every snippet the bridge generates must be clean, or the
        pre-flight would refuse the Dev Kit's own writes."""
        cases = [
            (L._LUA_PLACE, {}),
            (L._LUA_INVENTORY, {"max_depth": 4, "max_nodes": 400, "want_scripts": True,
                                "want_tree": True, "root": "game"}),
            (L._LUA_READ, {}),
            (L._LUA_WRITE, {"select": True}),
            (L._LUA_VERIFY, {"max": 400}),
            (L._LUA_VIEW, {"focus": "game.Workspace.Part", "clear": False}),
        ]
        for body, subs in cases:
            code = L._snippet("X", body, payload=L._pack_records(
                [{"path": "game.ServerScriptService.Main", "class": "Script",
                  "code": 'local s = "x"\nreturn s\n'}]), **subs)
            errors = [x for x in L.luau_check(code, "snippet") if x["severity"] == "error"]
            self.assertEqual(errors, [], f"Dev Kit snippet has syntax errors: {errors}")
            self.assertNotIn("@", code.replace('"@', "").replace('@"', ""))


class IfExpressionTests(unittest.TestCase):
    """Luau `if` used as a VALUE (`local b = if x then 1 else 2`) is closed by
    its `else` and has no `end`. Read as a statement it looks like a block that
    is never closed - and since script_write refuses the whole batch on one
    lint error, that false positive would block perfectly good modern code."""

    CLEAN = {
        "assignment": 'local a = 5\nlocal b = if a > 3 then "big" else "small"\nreturn b\n',
        "call argument": 'print(if true then 1 else 2)\n',
        "table field": 'local t = { v = if true then 1 else 2, w = 3 }\nreturn t.v\n',
        "returned": 'local function f(x)\n\treturn if x then "y" else "n"\nend\nreturn f(true)\n',
        "elseif chain": 'local n = 3\nlocal s = if n == 1 then "one" elseif n == 2 then "two" else "many"\nreturn s\n',
        "nested in then": 'local b = if true then if false then 1 else 2 else 3\nreturn b\n',
        "nested after else": 'local b = if false then 1 else if true then 2 else 3\nreturn b\n',
        "a function value": 'local f = if true then function() return 1 end else function() return 2 end\nreturn f()\n',
        "inside a loop": 'for i = 1, 3 do\n\tlocal s = if i == 1 then "a" else "b"\n\tprint(s)\nend\n',
        "as a while condition": 'local n = 0\nwhile if n < 3 then true else false do\n\tn += 1\nend\nreturn n\n',
        "as a repeat condition": 'local n = 0\nrepeat\n\tn += 1\nuntil if n >= 2 then true else false\n',
        "as an elseif condition": ('local n = 2\nlocal s = ""\nif n == 1 then\n\ts = "a"\n'
                                   'elseif if n == 2 then true else false then\n\ts = "b"\nend\nreturn s\n'),
        "a statement if inside one": ('local b = if true then (function() if false then return 1 end '
                                      'return 2 end)() else 3\nreturn b\n'),
        # The else-branch below IS a value, but _mask() blanks its string to
        # spaces - so a naive lookahead lands on the `if` of a later statement
        # and swallows it as `else if`, desyncing the whole block stack.
        "a string else-branch, then a statement if":
            'local text = if true then "a" else "b"\nif text == "a" then\n\tprint(1)\nend\n',
        "a string else-branch, then an if/elseif chain":
            ('local s = if true then "x" else "y"\nlocal n = 1\n'
             'if n == 1 then\n\tprint(s)\nelseif n == 2 then\n\tprint(2)\nend\n'),
        "a comment between else and the next if":
            'local b = if true then 1 else 2\n-- why not\nif b == 1 then\n\tprint("one")\nend\n',
    }
    BROKEN = {
        "no else": ('local b = if true then 1\nreturn b\n', "if-EXPRESSION"),
        "statement if still needs its end": ('if true then\nprint(1)\n', "unclosed 'if/then'"),
    }

    def check(self, code, label="t"):
        return L.luau_check(code, label)

    def test_if_expressions_are_not_unclosed_blocks(self):
        for name, code in self.CLEAN.items():
            errs = [f for f in self.check(code, name) if f["severity"] == "error"]
            self.assertEqual(errs, [], "%s: %s" % (name, [e["message"][:70] for e in errs]))

    def test_a_literal_else_branch_does_not_swallow_the_next_if(self):
        """End-to-end shape from examples/shop-system/ShopClient.lua: an
        if-expression whose else-branch is a string, inside a handler, followed
        by two statement ifs. One swallowed `end` here surfaces much further
        down the file as a bogus "extra 'end'"."""
        code = (
            'e.OnClientEvent:Connect(function(ok, message)\n'
            '\tlocal text = if typeof(message) == "string" then message else "done"\n'
            '\tif ok then\n'
            '\t\tprint(text)\n'
            '\telse\n'
            '\t\twarn(text)\n'
            '\tend\n'
            '\tif true then\n'
            '\t\tprint(1)\n'
            '\tend\n'
            'end)\n'
        )
        errs = [f for f in self.check(code) if f["severity"] == "error"]
        self.assertEqual(errs, [], [e["message"][:90] for e in errs])

    def test_a_broken_if_expression_says_what_it_is(self):
        for name, (code, want) in self.BROKEN.items():
            errs = [f for f in self.check(code) if f["severity"] == "error"]
            self.assertTrue(errs, "%s: expected an error" % name)
            self.assertIn(want, errs[0]["message"], name)

    def test_a_statement_if_is_not_mistaken_for_one(self):
        # The trap: _mask() blanks a string INCLUDING its quotes, so the last
        # visible character before this `if` is the `=` of the assignment above
        # it. Reading that as "an expression is expected" turns a plain statement
        # into an if-expression, and then its real `end` looks like one too many.
        for code in ('local s = ""\nif true then\n\tprint(1)\nend\n',
                     'local s = [[long]]\nif true then\n\tprint(1)\nend\n',
                     '-- a comment\nif true then\n\tprint(1)\nend\n',
                     'local n = 2\nif n == 1 then\n\tprint(1)\nend\n',
                     'if true then\n\tif false then\n\t\tprint(1)\n\tend\nend\n'):
            errs = [f for f in self.check(code) if f["severity"] == "error"]
            self.assertEqual(errs, [], [e["message"][:70] for e in errs])


class ProtocolTests(unittest.TestCase):
    """The wire format between the bridge and Studio."""

    def test_record_payload_round_trips(self):
        recs = [
            {"path": "game.ServerScriptService.A", "class": "Script", "code": 'local a = "x"\n'},
            {"path": "game.ReplicatedStorage.B", "class": "ModuleScript", "code": ""},
            {"path": "game.StarterPlayer.C", "class": "LocalScript",
             "code": "-- tabs\tand ünïcode 日本語\nreturn 1\n"},
        ]
        self.assertEqual(parse_records(L._pack_records(recs)), recs)

    def test_code_containing_the_delimiters_cannot_break_the_parse(self):
        nasty = 'local s = "PATH\\tfake\\nCODE\\t999\\nEND\\n"\nreturn s\n'
        recs = [{"path": "game.A.B", "class": "Script", "code": nasty}]
        self.assertEqual(parse_records(L._pack_records(recs)), recs)

    def test_long_bracket_level_rises_until_the_payload_is_safe(self):
        lit = L._lua_long("a ]==] b")
        self.assertTrue(lit.startswith("[===[") and lit.endswith("]===]"))
        self.assertEqual(L._lua_long("plain").count("[==["), 1)

    def test_report_parsing_ignores_wrapper_text(self):
        text = 'Result of execution:\nZS-PLACE 1\nPLACE\tMyGame\nRUN\tEdit\nEND\n(0.1s)'
        lines = L.parse_report(text, "PLACE")
        self.assertIn("PLACE\tMyGame", lines)
        self.assertEqual(L._place_from(lines), "MyGame")

    def test_a_missing_report_is_an_error_that_quotes_studio(self):
        with self.assertRaises(L.ToolError) as cm:
            L.parse_report("Unable to find an active Studio instance.", "WRITE")
        self.assertIn("Unable to find an active Studio instance", str(cm.exception))

    def test_sources_parsing_is_byte_exact_and_rejects_a_cut_reply(self):
        src = "-- ünïcode 日本語\nreturn 1\n"
        raw = src.encode("utf-8")
        import base64
        b64 = base64.b64encode(raw).decode("ascii")
        good = "ZS-SOURCES 1\nPLACE\tP\nFILE\tgame.A.B\tScript\tB64\t%d\t%d\n%s\nEND" % (
            len(raw), len(b64), b64)
        files, missing, place = L.parse_sources(good)
        self.assertEqual((files[0]["code"], files[0]["class"], place), (src, "Script", "P"))
        self.assertEqual(missing, [])
        with self.assertRaises(L.ToolError):
            L.parse_sources(good[: len(good) // 2])          # no END marker
        with self.assertRaises(L.ToolError):
            L.parse_sources(good.replace(f"\t{len(raw)}\t", f"\t{len(raw) + 9}\t"))  # short data

    def test_normalize_path_accepts_every_spelling(self):
        for given in ("ServerScriptService.Main", "game.ServerScriptService.Main",
                      "Game.ServerScriptService.Main", " ServerScriptService . Main "):
            self.assertEqual(L.normalize_path(given), "game.ServerScriptService.Main")
        self.assertEqual(L.normalize_path("game"), "game")
        with self.assertRaises(L.ToolError):
            L.normalize_path("   ")

    def test_rel_for_maps_a_dot_path_to_a_file_tree(self):
        self.assertEqual(L._rel_for("game.ServerScriptService.Shop.Handler"),
                         "ServerScriptService/Shop/Handler.lua")
        # A dot-path can never climb out of the mirror folder.
        self.assertNotIn("..", L._rel_for("game.Workspace/../../Evil"))


class PathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zs-safety-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_escaping_paths_are_refused(self):
        for bad in ("/etc/passwd", "../../escape.lua", "C:\\Windows\\system32\\x.lua",
                    "a/../../b", ""):
            with self.assertRaises(L.ToolError, msg=f"accepted {bad!r}"):
                L._inside(self.tmp, bad)

    def test_inside_paths_resolve(self):
        self.assertTrue(L._inside(self.tmp, "a/b.lua").startswith(os.path.realpath(self.tmp)))

    def test_slugs_are_windows_safe(self):
        self.assertEqual(L._slug("My Place: v2/finale?"), "My-Place-v2-finale")
        self.assertEqual(L._slug("con"), "place")
        self.assertEqual(L._slug(""), "place")


class DevKitCase(unittest.TestCase):
    """Base: an isolated data dir + a fake Studio, per test."""

    PLACE = "TestPlace"
    SCRIPTS = {
        "game.ServerScriptService.Main": ("Script", 'local Players = game:GetService("Players")\nprint("v1")\n'),
        "game.ReplicatedStorage.Modules.Config": ("ModuleScript", "return {version = 1}\n"),
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zs-devkit-")
        self._saved = (L.DATA_DIR, L.WORKSPACE_ROOT, L.HISTORY_ROOT, L.LAST_OUTPUT_DIR,
                       L.INPUT_SETTLE_S)
        L.DATA_DIR = self.tmp
        L.WORKSPACE_ROOT = os.path.join(self.tmp, "workspace")
        L.HISTORY_ROOT = os.path.join(self.tmp, "history")
        L.LAST_OUTPUT_DIR = os.path.join(self.tmp, "last")
        L.INPUT_SETTLE_S = 0   # the real wait is asserted from DEFAULT_INPUT_SETTLE
        L._place_cache.update({"name": None, "at": 0.0})
        self.fake = FakeStudio(place=self.PLACE, scripts=dict(self.SCRIPTS))
        self.studio = L.Studio(call=self.fake.call, has=self.fake.has)

    def tearDown(self):
        (L.DATA_DIR, L.WORKSPACE_ROOT, L.HISTORY_ROOT, L.LAST_OUTPUT_DIR,
         L.INPUT_SETTLE_S) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mirror_path(self, rel):
        return os.path.join(L.WORKSPACE_ROOT, self.PLACE, *rel.split("/"))

    def read_mirror(self, rel):
        with open(self.mirror_path(rel), encoding="utf-8") as f:
            return f.read()

    def write_mirror(self, rel, text):
        with open(self.mirror_path(rel), "w", encoding="utf-8") as f:
            f.write(text)

    def luau_kinds(self):
        """Which Dev Kit snippets reached Studio, in order."""
        out = []
        for name, args in self.fake.calls:
            if name == "execute_luau":
                m = L.KIND_RE.search(args.get("code") or "") if hasattr(L, "KIND_RE") else None
                import re
                m = re.search(r'zs_out\("([A-Z]+)"\)', args.get("code") or "")
                out.append(m.group(1) if m else "?")
        return out


class OverviewTests(DevKitCase):
    def test_one_call_reports_the_place_tree_and_every_script(self):
        text = run("project_overview", {}, self.studio)
        self.assertIn(f'project_overview: "{self.PLACE}" (Edit mode)', text)
        self.assertIn("── Instance tree ──", text)
        self.assertIn("Main (Script) [", text)
        self.assertIn("game.ServerScriptService.Main | Script |", text)
        self.assertIn('local Players = game:GetService("Players")', text)  # the first line
        self.assertIn("── Project memory ──", text)
        self.assertIn("Fast path:", text)

    def test_it_costs_exactly_one_studio_call_for_the_tree(self):
        run("project_overview", {"console": False}, self.studio)
        self.assertEqual(self.luau_kinds(), ["INVENTORY"])

    def test_a_subtree_and_depth_are_honoured(self):
        text = run("project_overview", {"root": "game.ServerScriptService", "depth": 1,
                                        "console": False}, self.studio)
        self.assertIn("Main (Script)", text)
        self.assertNotIn("Config (ModuleScript)", text)

    def test_play_mode_is_reported(self):
        self.fake.playing = True
        self.assertIn("(Playing mode)", run("project_overview", {"console": False}, self.studio))

    def test_console_output_can_be_skipped(self):
        self.assertNotIn("── Output", run("project_overview", {"console": False}, self.studio))
        self.assertIn("── Output", run("project_overview", {}, self.studio))

    def test_memory_is_recognised_when_it_exists(self):
        self.fake.add_script(L.MEMORY_PATH, "ModuleScript", "return [==[\n# Project memory\n]==]\n")
        text = run("project_overview", {"console": False}, self.studio)
        self.assertIn("exists (", text)
        self.assertIn("script_read it before changing", text)


    def test_the_script_count_is_the_place_s_count_not_the_listed_rows(self):
        # scripts:false suppresses the listing but the header still reports how
        # many scripts the place has - that is what Studio's own counter does,
        # and it is what tells the model the place is bigger than what it sees.
        full = run("project_overview", {"console": False}, self.studio)
        self.assertIn("2 scripts", full)
        self.assertIn("── Scripts (2) ──", full)
        quiet = run("project_overview", {"console": False, "scripts": False}, self.studio)
        self.assertIn("2 scripts", quiet)
        self.assertNotIn("── Scripts", quiet)
        self.assertNotIn("game.ServerScriptService.Main |", quiet)

    def test_a_non_script_instance_is_not_listed_as_a_script(self):
        # Studio's inventory filters on IsA("LuaSourceContainer"); the fake does
        # the same, so a container that happens to be in the fixture cannot
        # masquerade as a script (and a missing filter in the real snippet would
        # not be hidden by the test double).
        self.fake.scripts["game.ReplicatedStorage.Remotes"] = {"class": "Folder", "source": ""}
        self.fake.instances["game.ReplicatedStorage.Remotes"] = "Folder"
        text = run("project_overview", {"console": False}, self.studio)
        self.assertIn("Remotes (Folder)", text)          # in the tree, as an instance
        self.assertNotIn("game.ReplicatedStorage.Remotes |", text)   # not as a script
        self.assertIn("── Scripts (2) ──", text)


class WriteTests(DevKitCase):
    CODE = 'local Shop = {}\n\nfunction Shop.buy(player, item)\n    return true\nend\n\nreturn Shop\n'

    def write(self, files, **kw):
        args = {"files": files}
        args.update(kw)
        return run("script_write", args, self.studio)

    def test_creates_a_whole_system_in_one_call(self):
        text = self.write([
            {"path": "game.ServerScriptService.Shop.Handler", "className": "Script", "code": self.CODE},
            {"path": "game.ReplicatedStorage.Modules.ShopConfig", "className": "ModuleScript",
             "code": "return {prices = {sword = 10}}\n"},
            {"path": "game.StarterPlayer.StarterPlayerScripts.ShopClient", "className": "LocalScript",
             "code": 'local ReplicatedStorage = game:GetService("ReplicatedStorage")\n'},
        ])
        self.assertIn("3 written (3 created", text)
        self.assertEqual(self.fake.source_of("game.ServerScriptService.Shop.Handler"), self.CODE)
        self.assertEqual(self.fake.scripts["game.ReplicatedStorage.Modules.ShopConfig"]["class"],
                         "ModuleScript")
        self.assertEqual(self.fake.scripts["game.StarterPlayer.StarterPlayerScripts.ShopClient"]["class"],
                         "LocalScript")
        # One Studio call for the whole system (plus one to read the old sources
        # for the undo snapshots) - not three multi_edit turns.
        self.assertEqual(self.luau_kinds().count("WRITE"), 1)

    def test_overwrite_is_confirmed_and_snapshotted(self):
        text = self.write([{"path": "game.ServerScriptService.Main", "code": 'print("v2")\n'}])
        self.assertIn("1 written (0 created, 1 updated)", text)
        self.assertIn("snapshot ", text)
        self.assertEqual(self.fake.source_of("game.ServerScriptService.Main"), 'print("v2")\n')
        entries = L.history_entries(self.PLACE, "game.ServerScriptService.Main")
        self.assertEqual(len(entries), 1)
        self.assertEqual(L.history_read(entries[0]), 'local Players = game:GetService("Players")\nprint("v1")\n')

    def test_writing_the_same_content_changes_nothing(self):
        self.write([{"path": "game.ServerScriptService.Main",
                     "code": self.SCRIPTS["game.ServerScriptService.Main"][1]}])
        text = self.write([{"path": "game.ServerScriptService.Main",
                            "code": self.SCRIPTS["game.ServerScriptService.Main"][1]}])
        self.assertIn("1 already up to date", text)
        self.assertEqual(L.history_entries(self.PLACE, "game.ServerScriptService.Main"), [])

    def test_a_syntax_error_blocks_the_whole_write(self):
        text = self.write([
            {"path": "game.ServerScriptService.Good", "code": "return 1\n"},
            {"path": "game.ServerScriptService.Broken", "code": "if true then\nreturn 1\n"},
        ])
        self.assertTrue(text.startswith("ERROR: script_write wrote NOTHING"))
        self.assertIn("game.ServerScriptService.Broken", text)
        self.assertIn("line 1", text)
        self.assertNotIn("game.ServerScriptService.Good", self.fake.scripts)
        self.assertEqual(self.luau_kinds(), [])   # never reached Studio

    def test_force_writes_past_the_linter(self):
        text = self.write([{"path": "game.ServerScriptService.Broken",
                            "code": "if true then\nreturn 1\n"}], force=True)
        self.assertIn("1 written (1 created", text)
        self.assertIn("game.ServerScriptService.Broken", self.fake.scripts)

    def test_check_can_be_turned_off(self):
        self.assertIn("1 written", self.write(
            [{"path": "game.ServerScriptService.Broken", "code": "if true then\n"}], check=False))

    def test_a_bad_class_or_path_is_refused_before_anything_runs(self):
        with self.assertRaises(L.ToolError):
            self.write([{"path": "game.A.B", "className": "Folder", "code": "x"}])
        with self.assertRaises(L.ToolError):
            self.write([{"path": "game.A.B", "code": "x"}, {"path": "game.A.B", "code": "y"}])
        with self.assertRaises(L.ToolError):
            self.write([{"code": "x"}])
        self.assertEqual(self.fake.calls, [])

    def test_it_accepts_the_shapes_models_actually_send(self):
        # a bare path/code pair, multi_edit's spelling, and a {path: code} object
        self.assertIn("1 written", self.write({"path": "ServerScriptService.A", "code": "return 1\n"}))
        self.assertIn("1 written", self.write(
            [{"file_path": "game.ServerScriptService.B", "className": "modulescript",
              "edits": [{"old_string": "", "new_string": "return 2\n"}]}]))
        self.assertIn("1 written", run("script_write", {"files": {"ServerScriptService.C": "return 3\n"}},
                                       self.studio))
        self.assertEqual(self.fake.source_of("game.ServerScriptService.A"), "return 1\n")
        self.assertEqual(self.fake.scripts["game.ServerScriptService.B"]["class"], "ModuleScript")
        self.assertEqual(self.fake.source_of("game.ServerScriptService.C"), "return 3\n")

    def test_the_written_scripts_are_selected_in_studio(self):
        self.write([{"path": "game.ServerScriptService.Shop.Handler", "code": self.CODE}])
        self.assertEqual(self.fake.selection, ["game.ServerScriptService.Shop.Handler"])
        self.write([{"path": "game.ServerScriptService.Quiet", "code": "return 1\n"}], select=False)
        self.assertEqual(self.fake.selection, ["game.ServerScriptService.Shop.Handler"])

    def test_a_refused_source_write_falls_back_to_multi_edit(self):
        self.fake.fail_source_write = True
        text = self.write([{"path": "game.ServerScriptService.Main", "code": 'print("new")\n'},
                           {"path": "game.ServerScriptService.Fresh", "className": "LocalScript",
                            "code": "return 1\n"}])
        self.assertIn("2 written", text)
        self.assertIn("via multi_edit", text)
        self.assertEqual(self.fake.source_of("game.ServerScriptService.Main"), 'print("new")\n')
        self.assertEqual(self.fake.scripts["game.ServerScriptService.Fresh"]["class"], "LocalScript")
        self.assertTrue(any(n == "multi_edit" for n, _ in self.fake.calls))

    def test_a_big_batch_is_split_into_several_studio_calls(self):
        files = [{"path": f"game.ServerScriptService.S{i}", "code": "x" * 4000 + f"\n--{i}\n"}
                 for i in range(12)]
        text = self.write(files)
        self.assertIn("12 written (12 created", text)
        self.assertGreaterEqual(self.luau_kinds().count("WRITE"), 2)   # never one giant call
        for f in files:
            self.assertEqual(self.fake.source_of(f["path"]), f["code"])

    def test_unicode_survives_the_round_trip(self):
        code = '-- café 日本語 “quoted”\nlocal s = "héllo\\n"\nreturn s\n'
        self.write([{"path": "game.ServerScriptService.Uni", "code": code}])
        self.assertEqual(self.fake.source_of("game.ServerScriptService.Uni"), code)

    def test_too_many_files_at_once_is_refused(self):
        with self.assertRaises(L.ToolError):
            self.write([{"path": f"game.ServerScriptService.S{i}", "code": "return 1\n"}
                        for i in range(41)])


class VerifyTests(DevKitCase):
    def test_clean_place_passes(self):
        text = run("script_verify", {}, self.studio)
        self.assertIn("2 script(s) compiled in Studio - 2 OK, 0 FAILED", text)
        self.assertIn("Every script compiles", text)

    def test_a_broken_script_is_reported_with_its_line(self):
        self.fake.add_script("game.ServerScriptService.Broken", "Script",
                             "local a = 1\n--ZS-BAD\nreturn a\n")
        text = run("script_verify", {}, self.studio)
        self.assertIn("1 FAILED", text)
        self.assertIn("FAIL game.ServerScriptService.Broken (Script): line 2:", text)
        self.assertIn("Fix each failure", text)

    def test_only_the_requested_paths_are_checked(self):
        self.fake.add_script("game.ServerScriptService.Broken", "Script", "--ZS-BAD\n")
        text = run("script_verify", {"paths": ["ServerScriptService.Main"]}, self.studio)
        self.assertIn("0 FAILED", text)

    def test_a_non_script_path_is_skipped_with_its_class(self):
        # Studio compiles LuaSourceContainers only, so asking about a Part must
        # come back as a SKIP that says what it actually is - not as a script
        # that "passed", and not as a silent drop that leaves the count wrong.
        text = run("script_verify", {"paths": ["game.Workspace.Baseplate",
                                               "game.ServerScriptService.Main"]}, self.studio)
        self.assertIn("SKIP game.Workspace.Baseplate: not a script (Part)", text)
        self.assertIn("1 script(s) compiled in Studio - 1 OK, 0 FAILED.", text)
        self.assertNotIn("of 2 found", text)   # the Part is not counted as a script

    def test_a_whole_place_scan_ignores_non_scripts(self):
        self.fake.instances["game.ReplicatedStorage.Remotes"] = "Folder"
        text = run("script_verify", {}, self.studio)
        self.assertIn("2 script(s) compiled in Studio - 2 OK, 0 FAILED.", text)
        self.assertNotIn("SKIP", text)

    def test_a_place_that_cannot_compile_falls_back_to_luau_check(self):
        self.fake.no_loadstring = True
        text = run("script_verify", {}, self.studio)
        self.assertIn("cannot compile scripts in this context", text)
        self.assertIn("Use luau_check instead", text)


class LintToolTests(DevKitCase):
    def test_one_snippet(self):
        text = run("luau_check", {"code": "if true then\nreturn 1"}, self.studio)
        self.assertTrue(text.startswith("ERROR: luau_check found 1 error"))
        self.assertIn("line 1", text)
        self.assertEqual(self.fake.calls, [])   # no Studio, no round trip

    def test_several_named_snippets(self):
        text = run("luau_check", {"files": [{"path": "Handler", "code": "return 1\n"},
                                            {"path": "Broken", "code": "end\n"}]}, self.studio)
        self.assertIn("── Handler: clean", text)
        self.assertIn("── Broken", text)
        self.assertNotIn("game.Broken", text)   # labels are not rewritten into paths

    def test_clean_code_says_so(self):
        self.assertIn("clean", run("luau_check", {"code": "local x = 1\nreturn x\n"}, self.studio))

    def test_it_needs_something_to_check(self):
        with self.assertRaises(L.ToolError):
            run("luau_check", {}, self.studio)


class WorkspaceTests(DevKitCase):
    def test_pull_mirrors_every_script_as_real_files(self):
        text = run("workspace", {"action": "pull"}, self.studio)
        self.assertIn("2 script file(s) written", text)
        self.assertTrue(os.path.isfile(self.mirror_path("ServerScriptService/Main.lua")))
        self.assertEqual(self.read_mirror("ReplicatedStorage/Modules/Config.lua"),
                         "return {version = 1}\n")
        manifest = json.loads(self.read_mirror("zsmanifest.json"))
        self.assertEqual(manifest["files"]["ServerScriptService/Main.lua"]["path"],
                         "game.ServerScriptService.Main")

    def test_grep_finds_code_without_touching_studio(self):
        run("workspace", {"action": "pull"}, self.studio)
        before = len(self.fake.calls)
        text = run("workspace", {"action": "grep", "pattern": "GetService"}, self.studio)
        self.assertEqual(len(self.fake.calls), before)     # purely local
        self.assertIn("ServerScriptService/Main.lua:1", text)
        self.assertIn("1 match(es) in 1 file(s)", text)

    def test_grep_auto_pulls_an_empty_mirror(self):
        text = run("workspace", {"action": "grep", "pattern": "version"}, self.studio)
        self.assertIn("the mirror was empty, so it was pulled first", text)
        self.assertIn("Config.lua", text)

    def test_grep_takes_regex_context_and_filters(self):
        run("workspace", {"action": "pull"}, self.studio)
        self.assertIn("2 match", run("workspace", {"action": "grep", "pattern": "version|Players"},
                                     self.studio))
        ctx = run("workspace", {"action": "grep", "pattern": "version", "context": 1}, self.studio)
        self.assertIn("return {version = 1}", ctx)
        self.assertEqual(run("workspace", {"action": "grep", "pattern": "version",
                                           "path_contains": "ServerScript"}, self.studio).count(">"), 0)
        self.assertIn("0 match", run("workspace", {"action": "grep", "pattern": "nothing_here"},
                                     self.studio))

    def test_push_writes_local_edits_back_and_refreshes_the_manifest(self):
        run("workspace", {"action": "pull"}, self.studio)
        self.write_mirror("ServerScriptService/Main.lua", 'print("edited on disk")\n')
        text = run("workspace", {"action": "push"}, self.studio)
        self.assertIn("workspace push -> script_write: 1 written", text)
        self.assertEqual(self.fake.source_of("game.ServerScriptService.Main"), 'print("edited on disk")\n')
        # and it is not pushed twice
        self.assertIn("nothing to write", run("workspace", {"action": "push"}, self.studio))

    def test_push_lints_local_edits_before_writing_them(self):
        run("workspace", {"action": "pull"}, self.studio)
        self.write_mirror("ServerScriptService/Main.lua", "if true then\n")
        text = run("workspace", {"action": "push"}, self.studio)
        self.assertTrue(text.startswith("ERROR: script_write wrote NOTHING"))
        self.assertIn('print("v1")', self.fake.source_of("game.ServerScriptService.Main"))

    def test_a_local_edit_is_never_thrown_away_by_a_pull(self):
        run("workspace", {"action": "pull"}, self.studio)
        self.write_mirror("ServerScriptService/Main.lua", "-- my local notes\n")
        self.fake.add_script("game.ServerScriptService.Main", "Script", 'print("v2 in studio")\n')
        run("workspace", {"action": "pull"}, self.studio)
        self.assertEqual(self.read_mirror("ServerScriptService/Main.lua"), 'print("v2 in studio")\n')
        entries = L.history_entries(self.PLACE, "game.ServerScriptService.Main")
        self.assertTrue(any(e["label"] == "auto:mirror-overwritten" for e in entries))
        self.assertTrue(any("-- my local notes" in L.history_read(e) for e in entries))

    def test_status_reports_the_mirror_local_edits_and_live_drift(self):
        text = run("workspace", {"action": "status"}, self.studio)
        self.assertIn("0 mirrored script file(s)", text)
        run("workspace", {"action": "pull"}, self.studio)
        self.write_mirror("ServerScriptService/Main.lua", "-- local only\n")
        self.fake.add_script("game.ServerScriptService.Extra", "Script", "return 1\n")
        text = run("workspace", {"action": "status", "compare_live": True}, self.studio)
        self.assertIn("2 mirrored script file(s)", text)
        self.assertIn("1 local edit(s) not pushed", text)
        self.assertIn("NEW     game.ServerScriptService.Extra", text)

    def test_a_non_ascii_script_reports_one_size_everywhere(self):
        """Luau's `#source` counts BYTES, Python's len() counts code points, so
        an emoji-and-middot script used to read as one number from script_write
        and a smaller one from workspace pull - and compare_live then called it
        CHANGED forever, which sends an agent re-pulling a mirror that is
        already exact."""
        code = '--!strict\nlocal title = "\U0001F6D2 Shop \u00b7 \u2026"\nprint(title)\n'
        want = len(code.encode("utf-8"))
        self.assertGreater(want, len(code))          # the fixture really is multibyte
        written = run("script_write", {"files": [{"path": "game.StarterGui.ShopClient",
                                                  "className": "LocalScript", "code": code}]},
                      self.studio)
        self.assertIn("%d bytes" % want, written)
        self.assertNotIn(" chars", written)
        pulled = run("workspace", {"action": "pull"}, self.studio)
        self.assertIn("%d bytes" % want, pulled)
        self.assertEqual(self.read_mirror("StarterGui/ShopClient.lua"), code)
        status = run("workspace", {"action": "status", "compare_live": True}, self.studio)
        self.assertIn("0 changed since the pull", status)
        self.assertNotIn("CHANGED", status)
        self.assertIn("bytes", run("script_history", {"action": "list"}, self.studio))

    def test_a_plain_text_search_honours_ignore_case_false(self):
        run("workspace", {"action": "pull"}, self.studio)
        exact = run("workspace", {"action": "grep", "pattern": "GetService",
                                 "regex": False, "ignore_case": False}, self.studio)
        self.assertIn("1 match", exact)
        self.assertIn("0 match", run("workspace", {"action": "grep", "pattern": "getservice",
                                                  "regex": False, "ignore_case": False},
                                     self.studio))
        self.assertIn("1 match", run("workspace", {"action": "grep", "pattern": "getservice",
                                                  "regex": False}, self.studio))

    def test_context_windows_merge_instead_of_repeating_lines(self):
        """Five adjacent matches with context 1 used to print the shared lines
        twice each - 15 lines of output where 5 would do."""
        code = "".join("local RemoteEvent%d = %d\n" % (i, i) for i in range(1, 6))
        code += "\n".join("local filler%d = %d" % (i, i) for i in range(1, 6)) + "\n"
        code += "local RemoteEventFar = 99\n"
        self.fake.add_script("game.ServerScriptService.Remotes", "Script", code)
        run("workspace", {"action": "pull"}, self.studio)
        text = run("workspace", {"action": "grep", "pattern": "RemoteEvent", "context": 1},
                   self.studio)
        self.assertIn("6 match", text)
        for i in range(1, 6):
            self.assertEqual(text.count("Remotes.lua:%d:" % i), 1, text)
        self.assertEqual(text.count("  > "), 6)          # every match still marked
        self.assertIn("\n  --\n", text)                  # the disjoint window is separated

    def test_pull_can_be_limited_to_a_few_scripts(self):
        text = run("workspace", {"action": "pull", "paths": ["ServerScriptService.Main"]}, self.studio)
        self.assertIn("1 script file(s) written", text)
        self.assertFalse(os.path.exists(self.mirror_path("ReplicatedStorage/Modules/Config.lua")))

    def test_an_unknown_action_is_refused(self):
        with self.assertRaises(L.ToolError):
            run("workspace", {"action": "delete-everything"}, self.studio)


class HistoryTests(DevKitCase):
    def write_v2(self):
        return run("script_write", {"files": [{"path": "game.ServerScriptService.Main",
                                               "code": 'print("v2")\n'}]}, self.studio)

    def test_every_overwrite_leaves_an_undo_point(self):
        self.write_v2()
        text = run("script_history", {"action": "list"}, self.studio)
        self.assertIn("1 snapshot(s)", text)
        self.assertIn("auto:before-write", text)
        self.assertIn("game.ServerScriptService.Main", text)

    def test_diff_against_the_live_script(self):
        self.write_v2()
        text = run("script_history", {"action": "diff", "path": "game.ServerScriptService.Main"},
                   self.studio)
        self.assertIn('-print("v1")', text)
        self.assertIn('+print("v2")', text)
        self.assertIn("live in Studio", text)

    def test_revert_restores_and_is_itself_revertable(self):
        self.write_v2()
        text = run("script_history", {"action": "revert", "path": "game.ServerScriptService.Main"},
                   self.studio)
        self.assertIn("restored to snapshot", text)
        self.assertEqual(self.fake.source_of("game.ServerScriptService.Main"),
                         self.SCRIPTS["game.ServerScriptService.Main"][1])
        entries = L.history_entries(self.PLACE, "game.ServerScriptService.Main")
        self.assertTrue(any(e["label"] == "auto:before-revert" for e in entries))
        # and reverting again puts v2 back
        run("script_history", {"action": "revert", "path": "game.ServerScriptService.Main",
                               "entry": entries[0]["id"]}, self.studio)
        self.assertEqual(self.fake.source_of("game.ServerScriptService.Main"), 'print("v2")\n')

    def test_revert_of_an_unchanged_script_does_nothing(self):
        self.assertIn("already matches snapshot",
                      run("script_history", {"action": "snapshot",
                                             "paths": ["game.ServerScriptService.Main"]}, self.studio)
                      and run("script_history", {"action": "revert",
                                                 "path": "game.ServerScriptService.Main"}, self.studio))

    def test_snapshot_captures_the_whole_place(self):
        text = run("script_history", {"action": "snapshot"}, self.studio)
        self.assertIn("2 script(s)", text)
        self.assertEqual(len(L.history_entries(self.PLACE)), 2)

    def test_an_unknown_entry_id_lists_the_real_ones(self):
        self.write_v2()
        with self.assertRaises(L.ToolError) as cm:
            run("script_history", {"action": "revert", "path": "game.ServerScriptService.Main",
                                   "entry": "nope"}, self.studio)
        self.assertIn("Available:", str(cm.exception))

    def test_snapshots_are_pruned(self):
        for i in range(L.HISTORY_KEEP + 6):
            L.history_add(self.PLACE, "game.ServerScriptService.Main", f"return {i}\n", "test")
        entries = L.history_entries(self.PLACE, "game.ServerScriptService.Main")
        self.assertEqual(len(entries), L.HISTORY_KEEP)
        self.assertEqual(L.history_read(entries[0]), f"return {L.HISTORY_KEEP + 5}\n")

    def test_history_reads_without_studio(self):
        self.write_v2()
        offline = L.Studio(call=lambda *a, **k: {"ok": False, "text": "", "error": "down"},
                           has=lambda n: False)
        self.assertIn("1 snapshot(s)", run("script_history", {"action": "list"}, offline))


class ViewAndPlaytestTests(DevKitCase):
    def test_select_reports_what_it_selected_and_what_it_missed(self):
        text = run("studio_view", {"select": ["ServerScriptService.Main", "game.Nope.Missing"]},
                   self.studio)
        self.assertIn("selected 1", text)
        self.assertIn("MISS game.Nope.Missing", text)
        self.assertEqual(self.fake.selection, ["game.ServerScriptService.Main"])

    def test_focus_and_clear(self):
        self.assertIn("camera framed", run("studio_view", {"focus": "Workspace.Baseplate"}, self.studio))
        self.assertIn("selection cleared", run("studio_view", {"clear": True}, self.studio))

    def test_it_needs_something_to_do(self):
        with self.assertRaises(L.ToolError):
            run("studio_view", {}, self.studio)

    def test_playtest_runs_captures_and_stops(self):
        self.fake.console = {"Server": ["booting", "ERROR: attempt to index nil with 'FindFirstChild'"],
                             "Client": ["client ready"]}
        text = run("playtest", {"seconds": 1}, self.studio)
        self.assertIn("play-tested for 1s in Studio, stopped", text)
        self.assertIn("1 suspicious line(s)", text)
        self.assertIn("! ERROR: attempt to index nil", text)
        self.assertIn("── Output (Server", text)
        self.assertIn("── Output (Client", text)
        self.assertIn("client ready", text)
        self.assertFalse(self.fake.playing)

    def test_a_clean_run_says_so(self):
        self.fake.console = {"Server": ["all good"], "Client": []}
        text = run("playtest", {"seconds": 1}, self.studio)
        self.assertIn("0 suspicious line(s)", text)
        self.assertIn("Output showed no errors", text)

    def test_play_can_be_left_running(self):
        run("playtest", {"seconds": 1, "stop": False}, self.studio)
        self.assertTrue(self.fake.playing)

    def test_input_actions_are_forwarded_to_the_player_input_tools(self):
        text = run("playtest", {"seconds": 1,
                                "keys": [{"action": "keyPress", "key_code": "E"}],
                                "mouse": [{"action": "mouseButtonClick", "mouse_button": "left",
                                           "x": 100, "y": 200}]}, self.studio)
        self.assertIn("keyboard input: sent", text)
        self.assertIn("mouse input: sent", text)
        self.assertEqual([k for k, _ in self.fake.input_sent], ["keyboard", "mouse"])
        self.assertEqual(self.fake.input_sent[0][1]["datamodel_type"], "Client")

    def test_input_gets_a_beat_to_land_before_output_is_read(self):
        # A keypress that prints does so on a later frame, so reading Output the
        # instant the input call returns would report a clean run for a change
        # that had not happened yet. The wait is part of the result, and only
        # happens when input was actually accepted.
        self.assertGreater(DEFAULT_INPUT_SETTLE, 0)   # the shipped default really waits
        text = run("playtest", {"seconds": 1, "keys": [{"action": "keyPress", "key_code": "E"}]},
                   self.studio)
        self.assertIn("waited 0s for that input to land before reading Output", text)
        self.assertTrue(text.index("keyboard input: sent")
                        < text.index("waited 0s for that input to land"))
        # ...and the order in Studio is the one the reference promises: play
        # starts, the input goes in while it is running, Output is read after.
        names = [n for n, _a in self.fake.calls]
        self.assertLess(names.index("start_stop_play"), names.index("user_keyboard_input"))
        self.assertLess(names.index("user_keyboard_input"), names.index("get_console_output"))

    def test_a_refused_input_call_does_not_wait_or_stop_the_test(self):
        self.fake.TOOLS = tuple(t for t in FakeStudio.TOOLS if t != "user_keyboard_input")
        text = run("playtest", {"seconds": 1, "keys": [{"action": "keyPress"}]}, self.studio)
        self.assertIn("keyboard input: FAILED", text)
        self.assertIn("Output (Server", text)
        self.assertNotIn("for that input to land", text)   # nothing was sent
        self.assertFalse(self.fake.playing)


class OfflineTests(DevKitCase):
    """Studio closed: the commands that do not need it must still work, and the
    ones that do must say exactly what the user has to do."""

    def setUp(self):
        super().setUp()
        self.fake.offline = True

    def test_a_studio_command_explains_the_environment_problem(self):
        for tool, args in (("project_overview", {}), ("script_verify", {}),
                           ("studio_view", {"select": ["game.A"]}),
                           ("script_write", {"files": [{"path": "game.A.B", "code": "return 1\n"}]})):
            with self.assertRaises(L.ToolError, msg=tool) as cm:
                run(tool, args, self.studio)
            self.assertIn("Roblox Studio is not reachable", str(cm.exception))
            self.assertIn("NOT your mistake", str(cm.exception))

    def test_lint_and_history_still_work(self):
        self.assertIn("clean", run("luau_check", {"code": "return 1\n"}, self.studio))
        with self.assertRaises(L.ToolError):
            run("script_history", {"action": "list"}, self.studio)   # nothing snapshotted yet

    def test_the_place_slug_comes_from_the_cache_when_studio_is_down(self):
        L._place_cache.update({"name": "CachedPlace", "at": __import__("time").time()})
        self.assertEqual(L.place_slug(self.studio), "CachedPlace")


class ChunkingTests(DevKitCase):
    """A reply Studio truncates must never be handed back as a half script."""

    def make(self, count=6, size=1200, cap=0):
        self.fake = FakeStudio(place="Big", scripts={
            f"game.ServerScriptService.S{i}": ("Script", "x" * size + f"--{i}\nlocal ü = {i}\n")
            for i in range(count)})
        self.fake.max_reply_bytes = cap
        self.studio = L.Studio(call=self.fake.call, has=self.fake.has)
        return self.fake

    def test_a_truncated_read_is_retried_in_smaller_chunks(self):
        fake = self.make(cap=9000)
        files, missing = L.read_sources(self.studio, list(fake.scripts))
        self.assertEqual((len(files), len(missing)), (6, 0))
        self.assertGreater(fake.luau_calls, 1)                 # it did not fit in one go
        for f in files:
            self.assertEqual(f["code"], fake.scripts[f["path"]]["source"])

    def test_a_chunk_that_can_never_fit_is_reported_not_guessed(self):
        self.make(cap=200)
        files, missing = L.read_sources(self.studio, ["game.ServerScriptService.S0"])
        self.assertEqual(files, [])
        self.assertEqual(len(missing), 1)
        self.assertIn("cut short", missing[0]["reason"])

    def test_a_write_studio_cannot_parse_is_split_and_completes(self):
        fake = self.make(count=4, size=2000)
        fake.max_code_bytes = 9000          # all four at once will not parse
        text = run("script_write", {"files": [
            {"path": f"game.ServerStorage.W{i}", "code": "y" * 2000 + f"\n--{i}\n"} for i in range(4)],
            "history": False}, self.studio)
        self.assertIn("4 written (4 created", text)
        self.assertGreater(fake.luau_calls, 1)      # it had to be split
        for i in range(4):
            self.assertEqual(len(self.fake.source_of(f"game.ServerStorage.W{i}")), 2005)

    def test_one_file_studio_will_not_parse_falls_back_to_multi_edit(self):
        # A single file cannot be halved, so the direct Source write is abandoned
        # and the MCP's own multi_edit carries it - the write still lands.
        fake = self.make(count=1, size=100)
        fake.max_code_bytes = 8000            # the PLACE snippet fits, a 5 KB write does not
        text = run("script_write", {"files": [{"path": "game.ServerStorage.Huge",
                                               "code": "z" * 5000}], "history": False}, self.studio)
        self.assertIn("1 written (1 created", text)
        self.assertIn("via multi_edit", text)
        self.assertEqual(len(self.fake.source_of("game.ServerStorage.Huge")), 5000)


class WordingTests(DevKitCase):
    """The summaries are read by the model AND by the user in the chip, so the
    counts have to agree with what is listed, and read like English."""

    def test_plural_helper(self):
        self.assertEqual(L._plural(1, "script"), "1 script")
        self.assertEqual(L._plural(2, "script"), "2 scripts")
        self.assertEqual(L._plural(0, "instance"), "0 instances")
        self.assertEqual(L._plural(1, "entry", "entries"), "1 entry")
        self.assertEqual(L._plural(3, "entry", "entries"), "3 entries")

    def test_the_overview_header_agrees_with_the_listing(self):
        text = run("project_overview", {"console": False}, self.studio)
        head = text.splitlines()[0]
        self.assertIn("2 scripts", head)
        self.assertIn("instance", head)
        self.assertNotIn("1 instances", head)
        self.assertNotIn("1 scripts", head)
        self.assertIn("── Scripts (2) ──", text)

    def test_a_single_script_place_reads_as_one_script(self):
        for gone in [k for k in self.fake.scripts if k.endswith("Config")]:
            del self.fake.scripts[gone]
        text = run("project_overview", {"console": False}, self.studio)
        self.assertIn("1 script,", text.splitlines()[0])
        self.assertIn("── Scripts (1) ──", text)

    def test_lint_and_mirror_lines_are_pluralised_too(self):
        one = run("luau_check", {"code": "local a = 1\n"}, self.studio)
        self.assertIn("1 snippet(s), 1 line, no errors", one)
        two = run("luau_check", {"code": "local a = 1\nlocal b = 2\n"}, self.studio)
        self.assertIn("2 lines, no errors", two)
        bad = run("luau_check", {"code": "if true then\nreturn 1\n"}, self.studio)
        self.assertIn("(2 lines) - no Studio call was needed", bad)
        # the pre-flight report script_write shows when a file is clean
        self.assertIn("1 line checked", L.format_findings([], "t", "local a = 1\n"))
        self.assertIn("2 lines checked", L.format_findings([], "t", "local a = 1\nlocal b = 2\n"))
        self.assertIn("script file(s) written", run("workspace", {"action": "pull"}, self.studio))
        self.assertIn("mirror: 2 files", run("workspace", {"action": "grep", "pattern": "version"},
                                             self.studio))


class ResultShapingTests(DevKitCase):
    def test_a_huge_reply_is_capped_and_saved_to_disk(self):
        big = run("project_overview", {"max_nodes": 5000, "console": False}, self.studio)
        self.assertLess(len(big), L.MAX_RESULT_CHARS + 200)
        L.MAX_RESULT_CHARS_SAVED = None
        text = L._clip("x" * (L.MAX_RESULT_CHARS + 5000), "project_overview")
        self.assertIn("output cut at", text)
        self.assertTrue(os.path.isfile(os.path.join(L.LAST_OUTPUT_DIR, "project_overview.txt")))

    def test_every_tool_is_advertised_with_a_schema(self):
        for t in L.LOCAL_TOOLS:
            self.assertIn("name", t)
            self.assertIn("description", t)
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertIn(t["name"], L._DISPATCH)
        self.assertEqual(set(L.LOCAL_TOOL_NAMES), set(L._DISPATCH))

    def test_descriptions_survive_the_extensions_single_line_render(self):
        # core/config.js renders a tool as "<name>: <description first line>", so a
        # description with a newline in it would be silently cut in half - and the
        # reference is typed into every session, so it is kept to one short
        # paragraph (the operational detail lives in the extension's TOOL_NOTES).
        for t in L.LOCAL_TOOLS:
            d = t["description"]
            self.assertNotIn("\n", d, t["name"])
            self.assertLessEqual(len(d), 300, "%s: %d chars" % (t["name"], len(d)))
            for prop, spec in t["inputSchema"]["properties"].items():
                pd = spec.get("description", "")
                self.assertNotIn("\n", pd, "%s.%s" % (t["name"], prop))
                self.assertLessEqual(len(pd), 90, "%s.%s" % (t["name"], prop))

    def test_no_glued_words_at_a_wrapped_description_join(self):
        # Descriptions are written as several string fragments on their own lines
        # (implicit concatenation). A fragment that does not end in a space
        # renders as "...for one" + "specific file" = "for onespecific file" in
        # the command reference the model reads - invisible in the source. The
        # check is scoped to the LOCAL_TOOLS literal: the Luau snippets in this
        # file legitimately join lines with no trailing space.
        with open(os.path.join(os.path.dirname(L.__file__), "local_tools.py"),
                  encoding="utf-8") as f:
            src = f.read()
        start = src.index("LOCAL_TOOLS = [")
        end = src.index("\n]\n", start)
        glued = re.findall(r'^\s*"([^"\n]*[^"\n ])"\n\s*"', src[start:end], re.M)
        self.assertEqual(glued, [], "fragment(s) missing their joining space: %r" % (glued[:3],))

    def test_is_local_accepts_bare_and_prefixed_names(self):
        self.assertTrue(L.is_local("script_write"))
        self.assertTrue(L.is_local("zeroscript/script_write"))
        self.assertFalse(L.is_local("execute_luau"))

    def test_an_unknown_tool_is_refused(self):
        with self.assertRaises(L.ToolError):
            L.run("deploy_to_production", {}, self.studio)


if __name__ == "__main__":
    unittest.main(verbosity=2)
