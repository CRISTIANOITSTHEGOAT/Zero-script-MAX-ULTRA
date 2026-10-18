# SPDX-License-Identifier: GPL-3.0-or-later
"""The worked example is part of the test suite on purpose.

examples/shop-system is 1,258 lines of modern Luau written to the strict
architect brief, and building it found four real bugs in the tooling: the linter
reading an `if` used as a value as an unclosed block, its lookahead seeing
through a masked string literal, sizes reported in bytes by Luau and characters
by Python, and `workspace grep` repeating context lines. None of those were
caught by the hand-written snippets in test_local_tools.py.

So this file does two jobs:

  1. it lints the example on every run, which means the next linter change is
     checked against a real, large, modern Luau codebase rather than only
     against the shapes someone thought to write a case for; and
  2. it asserts the standards the example's README claims - strict mode, no
     deprecated scheduler, no parenting inside Instance.new, generalized
     iteration, Scale-only sizes, the exact palette, and a server that owns
     every decision - so the example cannot quietly drift away from the brief it
     is meant to demonstrate.

Run from the bridge folder:
    python -m unittest discover -s tests -v
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, BRIDGE_DIR)

import local_tools as L  # noqa: E402

EXAMPLE_DIR = os.path.join(BRIDGE_DIR, "examples", "shop-system")
FILES = ("ShopConfig.lua", "ShopServer.lua", "ShopClient.lua")
PRESENT = all(os.path.isfile(os.path.join(EXAMPLE_DIR, n)) for n in FILES)


def read(name):
    with open(os.path.join(EXAMPLE_DIR, name), encoding="utf-8") as f:
        return f.read()


# `UDim2.new(a, b, c, d)` where b or d is not 0 - a pixel component. Reported
# with the assignment it belongs to, because a POSITION pinned to a corner with a
# pixel margin is correct and a SIZE in pixels is not.
UDIM2 = re.compile(r"UDim2\.new\(([^)]*)\)")

# The brief's palette, as the README states it.
PALETTE = ["(20, 22, 28)", "(28, 31, 39)", "(45, 50, 62)", "(0, 190, 125)",
           "(0, 140, 255)", "(245, 245, 250)", "(160, 165, 180)"]


def pixel_udim2(src):
    """[(line, assignment, call)] for every UDim2 with a pixel component."""
    out = []
    for m in UDIM2.finditer(src):
        args = [a.strip() for a in m.group(1).split(",")]
        if len(args) != 4 or args[1] == "0" or args[3] == "0":
            continue
        start = src.rfind("\n", 0, m.start()) + 1
        out.append((src.count("\n", 0, m.start()) + 1, src[start:m.start()].strip(), m.group(0)))
    return out


@unittest.skipUnless(PRESENT, "examples/shop-system is not in this checkout")
class ShopExampleLintTests(unittest.TestCase):
    """The linter's real-world regression net: a large, modern, strict codebase."""

    @classmethod
    def setUpClass(cls):
        cls.src = {n: read(n) for n in FILES}

    def test_every_file_lints_clean(self):
        for name in FILES:
            findings = L.luau_check(self.src[name], name)
            self.assertEqual([f for f in findings if f["severity"] == "error"], [],
                             "%s: %s" % (name, [f["message"][:90] for f in findings]))
            self.assertEqual([f for f in findings if f["severity"] == "warning"], [],
                             "%s: %s" % (name, [f["message"][:90] for f in findings]))

    def test_the_batch_reports_clean_as_one_call(self):
        """script_write lints the whole batch before writing anything, so the
        example has to pass as a batch too, not just file by file."""
        text = L.run("luau_check", {"files": [{"path": n, "code": self.src[n]} for n in FILES],
                                    "context": "script"}, None)["text"]
        self.assertTrue(text.startswith("luau_check: clean"), text[:300])
        self.assertIn("3 snippet(s)", text)

    def test_it_is_big_enough_to_be_worth_linting(self):
        lines = sum(s.count("\n") for s in self.src.values())
        self.assertGreater(lines, 1000, "the example shrank to %d lines" % lines)


@unittest.skipUnless(PRESENT, "examples/shop-system is not in this checkout")
class ShopExampleStandardsTests(unittest.TestCase):
    """What the example's README claims, checked against the source."""

    @classmethod
    def setUpClass(cls):
        cls.src = {n: read(n) for n in FILES}
        cls.config = cls.src["ShopConfig.lua"]
        cls.server = cls.src["ShopServer.lua"]
        cls.client = cls.src["ShopClient.lua"]

    def test_every_file_declares_strict_mode_first(self):
        for name in FILES:
            self.assertEqual(self.src[name].splitlines()[0], "--!strict", name)

    def test_no_deprecated_scheduler_calls(self):
        for name, src in self.src.items():
            hits = re.findall(r"(?<![.\w])(?:wait|spawn|delay)\s*\(", src)
            self.assertEqual(hits, [], "%s: %s" % (name, hits[:5]))
        self.assertIn("task.wait", self.server)
        self.assertIn("task.spawn", self.client)
        self.assertIn("task.delay", self.client)

    def test_nothing_is_parented_inside_instance_new(self):
        for name, src in self.src.items():
            self.assertEqual(re.findall(r'Instance\.new\(\s*"[^"]+"\s*,', src), [], name)
        self.assertGreater(self.client.count("Instance.new("), 20,
                           "the client should be building its GUI at runtime")

    def test_iteration_is_generalized(self):
        for name, src in self.src.items():
            self.assertEqual(re.findall(r"\b(?:pairs|ipairs)\s*\(", src), [], name)
            self.assertRegex(src, r"for \w+(?:, \w+)? in ", "%s: no generalized loop" % name)

    def test_types_are_declared_and_boundaries_are_unknown(self):
        self.assertIn("export type", self.config)
        for shape in ("ItemData", "TabData", "Palette", "ShopData"):
            self.assertIn(shape, self.config)
        # The config is annotated with its own type, so a typo is caught at
        # author time instead of at runtime on someone's phone.
        self.assertRegex(self.config, r"local CONFIG: ShopData = \{")
        self.assertIn("typeof(", self.server)
        self.assertIn("typeof(", self.client)

    def test_every_gui_size_is_scale_only(self):
        """The brief's UI rule. Positions are exempt: pinning a toggle to the
        bottom-right corner is `Scale 1` plus a pixel MARGIN, which is how a
        corner pin is written - and the sizes themselves stay in Scale, clamped
        in pixels by a UISizeConstraint so they remain tappable."""
        for line, assignment, call in pixel_udim2(self.client):
            self.assertTrue(assignment.endswith(".Position ="),
                            "ShopClient.lua:%d sizes in pixels: %s = %s" % (line, assignment, call))
        self.assertEqual(pixel_udim2(self.config), [])
        # Sizes really are Scale, and the tap targets really are clamped.
        self.assertRegex(self.client, r"toggle\.Size = UDim2\.new\(0\.062, 0, 0\.062, 0\)")
        self.assertRegex(self.client, r"root\.Size = UDim2\.new\([\d.]+, 0, [\d.]+, 0\)")
        # Three frames are clamped in pixels so they stay usable at any size:
        # the root (max 1040x700, min 320x240) and the two buttons.
        self.assertEqual(self.client.count('Instance.new("UISizeConstraint")'), 3)
        for clamped in ("rootSize", "toggleMax", "closeSize"):
            self.assertIn(clamped, self.client, clamped)
        self.assertIn("UIAspectRatioConstraint", self.client)
        self.assertIn("UIGridLayout", self.client)
        self.assertIn("UIListLayout", self.client)

    def test_the_one_pixel_size_lives_in_world_space(self):
        """A BillboardGui is not a screen GUI: Scale there would mean a fraction
        of the screen and stretch the nameplate with the camera."""
        pixels = pixel_udim2(self.server)
        self.assertEqual(len(pixels), 1, pixels)
        self.assertTrue(pixels[0][1].startswith("billboard.Size ="), pixels)
        self.assertIn("BillboardGui", self.server)

    def test_the_palette_is_the_one_the_brief_specifies(self):
        for colour in PALETTE:
            self.assertIn(colour, self.config, colour)
        self.assertIn("UDim.new(0, 8)", self.client)
        self.assertRegex(self.client, r"stroke\([^)]*0\.7")

    def test_hover_tweens_a_scale_not_a_size(self):
        """Tweening Size inside a UIGridLayout fights the layout, and the layout
        wins - the deviation the README documents."""
        self.assertIn("UIScale", self.client)
        self.assertIn("Enum.EasingStyle.Quad", self.client)
        self.assertIn("1.05", self.client)
        self.assertNotRegex(self.client, r"TweenService:Create\([^)]*\{ Size =")

    def test_the_server_owns_every_decision(self):
        self.assertIn("UpdateAsync", self.server)
        self.assertIn("BindToClose", self.server)
        self.assertIn("math.clamp", self.server)
        self.assertIn("rateLimit", self.server)
        self.assertIn("DataStoreService", self.server)
        # ...and the client never touches persistence or money.
        self.assertNotIn("DataStore", self.client)
        self.assertNotIn("UpdateAsync", self.client)
        self.assertNotIn("leaderstats", self.client)
        # The client asks with an id and a quantity - never a price.
        self.assertRegex(self.client, r"purchaseRemote:FireServer\(itemId, quantity\)")
        self.assertNotRegex(self.client, r"FireServer\([^)]*price")

    def test_the_config_is_pure_data(self):
        """No services, no state, no side effects: it has to be safe to require
        from both sides."""
        for banned in ("GetService", "Instance.new", "game.Players", "WaitForChild"):
            self.assertNotIn(banned, self.config, banned)
        self.assertEqual(self.config.rstrip().splitlines()[-1], "return ShopConfig",
                         "it must return the module table")

    def test_the_demo_script_is_shipped_and_compiles(self):
        import py_compile
        demo = os.path.join(EXAMPLE_DIR, "pipeline_demo.py")
        self.assertTrue(os.path.isfile(demo), "pipeline_demo.py is gone")
        py_compile.compile(demo, doraise=True)
        with open(demo, encoding="utf-8") as f:
            src = f.read()
        # The demo must keep saying which half is real - that is the whole point
        # of running an example in a repo that has no Roblox Studio.
        self.assertIn("SIMULATED", src)
        self.assertIn("GENUINE", src)
        readme = os.path.join(EXAMPLE_DIR, "README.md")
        self.assertTrue(os.path.isfile(readme), "the example has no README")
        with open(readme, encoding="utf-8") as f:
            doc = f.read()
        self.assertIn("pipeline_demo.py", doc)
        self.assertIn("script_verify", doc)
        self.assertLess(doc.index("script_verify"), doc.index("playtest"),
                        "the README must verify before it play-tests, like the preset does")


if __name__ == "__main__":
    unittest.main()
