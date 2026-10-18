// Node smoke test for the Dev Kit plumbing on the extension side
// (run: node test-devkit.js). Not shipped.
//
// The Dev Kit itself lives in the bridge (local_tools.py) and is tested there by
// tests/test_local_tools.py. What is tested here is the half that lives in the
// page: the command reference the model is given, the prompt that teaches the
// workflow, and the two lists that must never drift out of sync with the bridge.
const fs = require("fs");
const load = (f, ret) => new Function(fs.readFileSync(__dirname + "/" + f, "utf8") + "; return " + ret + ";")();
const ZS = load("core/config.js", "ZS");
const BRIDGE = fs.readFileSync(__dirname + "/../local_tools.py", "utf8");

const ok = (name, cond) => { console.log((cond ? "PASS" : "FAIL") + "  " + name); if (!cond) process.exitCode = 1; };

// ── the bridge's own tool list, read out of local_tools.py ────────────────
// Parsed rather than duplicated: a tool added on the Python side without a
// matching note/category here is exactly the drift this test exists to catch.
const DEVKIT = [...BRIDGE.matchAll(/^\s{8}"name": "([a-z_]+)",$/gm)].map((m) => m[1]);
ok("the Dev Kit's tools are read from local_tools.py", DEVKIT.length >= 8);
for (const t of ["project_overview", "script_write", "script_verify", "luau_check",
                 "script_history", "workspace", "studio_view", "playtest"]) {
  ok(`bridge advertises ${t}`, DEVKIT.includes(t));
}

// ── server id must match on both sides ────────────────────────────────────
ok("the server id matches the bridge's", /LOCAL_SERVER_ID = "zeroscript"/.test(BRIDGE) &&
  ZS.LOCAL_SERVER_ID === "zeroscript");

// ── every Dev Kit command has a tested usage note ─────────────────────────
for (const t of DEVKIT) ok(`TOOL_NOTES covers ${t}`, typeof ZS.TOOL_NOTES[t] === "string" && ZS.TOOL_NOTES[t].length > 40);

// The reference is typed into the chat box at the start of EVERY session, so its
// size is a real cost: description and note must not say the same thing twice.
// Budgets, not exact sizes - enough headroom to add a command, tight enough that
// re-bloating a note fails the test.
{
  const total = DEVKIT.reduce((n, t) => n + ZS.TOOL_NOTES[t].length, 0);
  ok("the Dev Kit's notes stay terse (typed into every session)", total < 3200);
  for (const t of DEVKIT) ok(`the note for ${t} is not a second description`, ZS.TOOL_NOTES[t].length <= 700);
}

// ── every Dev Kit command gets a known chip theme ─────────────────────────
const CATS = new Set(["read", "edit", "screen", "generate", "roblox", "tool"]);
for (const t of DEVKIT) ok(`toolCategory(${t}) is a real theme`, CATS.has(ZS.toolCategory(t)));
ok("a prefixed name is themed the same", ZS.toolCategory("zeroscript/script_write") === ZS.toolCategory("script_write"));

// ── formatToolList: the reference the model actually reads ────────────────
const tools = [
  { name: "script_write", description: "Create or overwrite WHOLE scripts.\nSecond line is dropped.",
    inputSchema: { type: "object", required: ["files"], properties: {
      files: { type: "array", description: "Files to write.", items: { type: "object",
        required: ["path", "code"], properties: { path: { type: "string" }, className: { type: "string" },
          code: { type: "string" } } } },
      check: { type: "boolean", description: "lint first" },
      force: { type: "boolean", description: "x".repeat(60) } } } },
  { name: "execute_luau", description: "Run Luau", inputSchema: { type: "object",
    properties: { code: { type: "string" }, datamodel_type: { type: "string", enum: ["Edit", "Server", "Client"] } } } },
];
const ref = ZS.formatToolList(tools);
ok("the short description is used, not the whole thing",
  ref.includes("script_write: Create or overwrite WHOLE scripts.") && !ref.includes("Second line is dropped"));
ok("required params are unmarked and optional ones carry ?",
  /files: array/.test(ref) && /check\?:boolean/.test(ref));
ok("array-of-object params keep their per-item shape",
  ref.includes("each item: {path:string, className?:string, code:string}"));
ok("a long description gets its own line", ref.includes("force?: boolean - " + "x".repeat(60)));
ok("the tested usage note is appended", ref.includes("PREFER IT over multi_edit"));
ok("enums are shown inline instead of \"string\"",
  ref.includes("datamodel_type?:(Edit|Server|Client)"));
ok("notes are appended for Studio's own commands too", ref.includes("Use `return` to produce output"));

// ── no glued words at a wrapped string join ───────────────────────────────
// These notes are written as several fragments joined with "+". A fragment that
// does not end in a space renders as "...for one" + "specific file" =
// "for onespecific file" in the reference the model reads - invisible in the
// source, obvious to the model. Caught live once; this keeps it caught.
{
  const cfg = fs.readFileSync(__dirname + "/core/config.js", "utf8");
  const glued = [...cfg.matchAll(/^\s*"([^"\n]*[^"\n ])" \+\n\s*"/gm)]
    .map((m) => m[1])
    .filter((t) => !t.endsWith("\\n"));   // a newline ends the fragment cleanly
  ok("no note fragment is missing its joining space", glued.length === 0);
  if (glued.length) console.log("      " + glued.map((t) => "…" + t.slice(-30)).join("\n      "));
}

// ── read-only classification (drives the batch hint) ──────────────────────
const ro = (tool, args) => ZS.isReadOnlyStep({ tool, arguments: args || {} });
ok("reads are read-only", ro("project_overview") && ro("script_read") && ro("inspect_instance") &&
  ro("luau_check") && ro("script_verify"));
ok("writes are not", !ro("script_write") && !ro("multi_edit") && !ro("execute_luau") && !ro("playtest"));
ok("workspace depends on the action", ro("workspace", { action: "grep" }) && ro("workspace", { action: "status" }) &&
  !ro("workspace", { action: "pull" }) && !ro("workspace", { action: "push" }));
ok("script_history depends on the action", ro("script_history", { action: "list" }) &&
  ro("script_history", { action: "diff" }) && !ro("script_history", { action: "revert" }));
ok("a prefixed name classifies the same", ro("zeroscript/project_overview"));

// ── the system prompt: with and without an inlined reference ──────────────
const plain = ZS.buildSystemPrompt({ siteName: "DeepSeek" });
const withRef = ZS.buildSystemPrompt({ siteName: "DeepSeek", reference: ref });
ok("the reference is inlined under its own heading",
  withRef.includes("━━━ COMMAND REFERENCE") && withRef.includes("script_write: Create or overwrite"));
ok("an inlined reference cancels the startup list_commands turn",
  !/Your very first action is to write/.test(withRef) &&
  /do NOT call list_commands at startup/.test(withRef));
ok("without a reference the old instruction stays",
  /Your very first action is to write/.test(plain) && !plain.includes("COMMAND REFERENCE"));
ok("the dev workflow is taught in both", plain.includes("━━━ DEV WORKFLOW") &&
  withRef.includes("━━━ DEV WORKFLOW"));
for (const step of ["project_overview", "workspace", "script_write", "multi_edit",
                    "execute_luau", "script_verify", "playtest", "script_history", "studio_view"]) {
  ok(`the workflow names ${step}`, plain.includes(step));
}
{
  // Order is the whole point of the workflow: orient before writing, verify
  // before testing, and both before the model tells the user it works.
  const wf = plain.slice(plain.indexOf("━━━ DEV WORKFLOW"));
  const at = (label) => wf.indexOf(label);
  ok("the workflow keeps the order that matters",
    at("1. ORIENT") > -1 && at("2. FIND CODE") > at("1. ORIENT") &&
    at("3. WRITE") > at("2. FIND CODE") && at("6. VERIFY") > at("3. WRITE") &&
    at("7. TEST") > at("6. VERIFY") && at("8. UNDO") > at("7. TEST") &&
    at("9. SHOW") > at("8. UNDO"));
}
ok("batch is documented at 16 with the opt-out", plain.includes("Up to 16 steps") &&
  plain.includes("continue_on_error"));
ok("the user's custom prompt still goes last",
  ZS.buildSystemPrompt({ siteName: "X", customPrompt: "be terse", reference: ref })
    .endsWith("━━━ USER'S CUSTOM PROMPT (extra instructions from the user) ━━━\nbe terse"));
ok("the marker still leads the prompt (camouflage depends on it)",
  withRef.startsWith(ZS.SYS_MARKER) && plain.startsWith(ZS.SYS_MARKER));

// ── main.js: the reference is scoped to the Roblox workflow ───────────────
const MAIN = fs.readFileSync(__dirname + "/core/main.js", "utf8");
ok("the bootstrap builds a reference", /function bootReference\(\)/.test(MAIN) &&
  /systemPrompt\(reference\)/.test(MAIN));
ok("addon servers are not inlined into every session start",
  /const tools = A.toolList.filter\(isWorkflowTool\);/.test(MAIN));
ok("the reference is capped before it is typed into a chat box",
  /REFERENCE_MAX_CHARS/.test(MAIN));
ok("list_commands and the bootstrap share one renderer",
  /ZS.formatToolList\(scoped\)/.test(MAIN));
ok("the Dev Kit is included in the default command list",
  /requested === "roblox" && srv === ZS.LOCAL_SERVER_ID/.test(MAIN));
ok("the Dev Kit is not offered as a removable addon server",
  /sv.id !== ZS.LOCAL_SERVER_ID/.test(MAIN) && /&& !sv.local/.test(MAIN));
ok("an addon server cannot be named \"zeroscript\"",
  /s === "roblox" \|\| s === ZS.LOCAL_SERVER_ID/.test(MAIN));
// Ordering, not just presence: the reference is built from the catalogue
// ensureTools() just fetched. Build it any earlier and every session silently
// falls back to "call list_commands first" - a wasted turn nobody would notice.
ok("the reference is built AFTER the catalogue is refreshed",
  MAIN.indexOf("await ensureTools(true)") > -1 &&
  MAIN.indexOf("await ensureTools(true)") < MAIN.indexOf("const reference = bootReference();"));
ok("the Dev Kit has its own per-command timeouts", /const TOOL_TIMEOUT_MS = \{/.test(MAIN) &&
  /script_write: 150000/.test(MAIN));

// ── background.js / popup.js: connectivity must ignore the always-alive kit ─
const BG = fs.readFileSync(__dirname + "/background.js", "utf8");
const POPUP = fs.readFileSync(__dirname + "/popup.js", "utf8");
ok("status counts Dev Kit tools separately", /localTools: toolsCache.length - realTools.length/.test(BG) &&
  /const realTools = toolsCache.filter\(\(t\) => !t.local\)/.test(BG));
ok("the popup dot ignores the Dev Kit", /const real = list.filter\(\(x\) => !x.local/.test(POPUP));

// ── prompt presets: ready-made custom prompts ─────────────────────────────
// A preset is typed into the chat box on EVERY session start, so it is held to
// the same standard as the system prompt itself: only command names that exist,
// the pipeline in the order that matters, and small enough to be worth sending.
const PRESETS = ZS.PROMPT_PRESETS;
ok("presets are exported and each one is complete",
  Array.isArray(PRESETS) && PRESETS.length > 0 && PRESETS.every((p) =>
    typeof p.id === "string" && p.id.length > 0 &&
    typeof p.label === "string" && p.label.length > 0 &&
    typeof p.blurb === "string" && p.blurb.length > 20 &&
    typeof p.text === "string" && p.text.length > 400));
ok("preset ids are unique", new Set(PRESETS.map((p) => p.id)).size === PRESETS.length);
ok("findPreset finds one and refuses a made-up id",
  ZS.findPreset("architect") && ZS.findPreset("architect").id === "architect" &&
  ZS.findPreset("nope") === null && ZS.findPreset("") === null &&
  ZS.findPreset(undefined) === null && ZS.findPreset(null) === null);

// Every command a preset names must exist. The known set is read from the same
// three sources the rest of this file uses, so renaming a tool on the Python
// side fails HERE instead of shipping a preset that teaches the model to call a
// command nobody implements.
{
  const STUDIO_MCP = [...fs.readFileSync(__dirname + "/../tests/fake_studio.py", "utf8")
    .match(/TOOLS = \(([^)]*)\)/s)[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
  ok("the Studio MCP surface is read from fake_studio.py",
    STUDIO_MCP.includes("execute_luau") && STUDIO_MCP.includes("user_keyboard_input"));
  const EXT_COMMANDS = ["batch", "list_commands", "list_mcp_servers"];
  for (const t of EXT_COMMANDS) ok(`main.js implements ${t}`, MAIN.includes(`"${t}"`));
  const KNOWN = new Set([...DEVKIT, ...Object.keys(ZS.TOOL_NOTES), ...STUDIO_MCP, ...EXT_COMMANDS]);
  for (const p of PRESETS) {
    const named = [...new Set([...p.text.matchAll(/\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/g)].map((m) => m[0]))];
    const invented = named.filter((n) => !KNOWN.has(n));
    ok(`"${p.id}" only names commands that exist (${named.length} checked)`, invented.length === 0);
    if (invented.length) console.log("      invented: " + invented.join(", "));
  }
}

// The architect preset is the user's own brief, so its substance is asserted
// line by line: a preset that quietly drops "--!strict" is worse than none.
{
  const A = ZS.findPreset("architect");
  const at = (s) => A.text.indexOf(s);
  ok("it verifies BEFORE it play-tests (the brief omitted script_verify)",
    at("script_verify") > -1 && at("playtest") > at("script_verify"));
  ok("it keeps the whole pipeline in order",
    at("project_overview") > -1 && at("luau_check") > at("project_overview") &&
    at("script_write") > at("luau_check") && at("script_verify") > at("script_write") &&
    at("studio_view") > at("script_verify") && at("playtest") > at("studio_view") &&
    at("script_history") > at("playtest"));
  ok("lint is explained as all-or-nothing, which is why it comes first",
    /refuses the WHOLE batch on a single finding/.test(A.text));
  ok("it demands strict typing with unknown at the boundaries",
    A.text.includes("--!strict") && A.text.includes("export type") && A.text.includes("typeof"));
  ok("it bans the deprecated scheduler calls",
    A.text.includes("task.wait") && /never wait, spawn or delay/.test(A.text));
  ok("it bans parenting inside Instance.new and pairs/ipairs",
    A.text.includes('Instance.new("Part", parent)') && /not pairs or ipairs/.test(A.text));
  ok("it puts the three files where the architecture wants them",
    A.text.includes("ReplicatedStorage.[System]Config") &&
    A.text.includes("ServerScriptService.[System]Server") &&
    A.text.includes("StarterGui.[System]Client") &&
    A.text.includes("ModuleScript") && A.text.includes("LocalScript"));
  ok("it makes the server authoritative and the client a renderer",
    /Treat the client as hostile input/.test(A.text) && A.text.includes("UpdateAsync") &&
    A.text.includes("rate limit") && /It asks, it never decides/.test(A.text));
  ok("it carries the Scale-only UI contract and the exact dark palette",
    A.text.includes("UDim2.new(sx, 0, sy, 0)") && A.text.includes("UIAspectRatioConstraint") &&
    A.text.includes("UISizeConstraint") && A.text.includes("UDim.new(0,8)") &&
    ["(20,22,28)", "(28,31,39)", "(45,50,62)", "(0,190,125)", "(0,140,255)",
     "(245,245,250)", "(160,165,180)"].every((c) => A.text.includes(c)));
  ok("it says to tween a UIScale, not Size, inside a layout",
    A.text.includes("UIScale") && A.text.includes("1.05") &&
    A.text.includes("Enum.EasingStyle.Quad") && /never Size/.test(A.text));
  ok("it forbids claiming success that was not verified this turn",
    /did not verify this turn/.test(A.text));
  ok("a preset stays small enough to send on every session", A.text.length <= 4000);
  ok("it arrives as the user's custom prompt, not as a hidden layer",
    ZS.buildSystemPrompt({ siteName: "X", customPrompt: A.text })
      .endsWith("━━━ USER'S CUSTOM PROMPT (extra instructions from the user) ━━━\n" + A.text));
}

// ── main.js: the menu offers them, and the choice survives a reload ───────
ok("the menu renders one option per preset", /ZS.PROMPT_PRESETS.forEach/.test(MAIN) &&
  /presetOptions \+= `<option value="\$\{esc\(p.id\)\}"/.test(MAIN) &&
  /<select id="zs-set-preset"/.test(MAIN));
ok("the picker sits in the Custom prompt section, above the textarea it fills",
  MAIN.indexOf('<div class="zs-sec-label"><span>Custom prompt</span></div>') <
    MAIN.indexOf('<select id="zs-set-preset"') &&
  MAIN.indexOf('<select id="zs-set-preset"') < MAIN.indexOf('<textarea id="zs-set-text"'));
ok("picking a preset fills the visible textarea instead of hiding it",
  /ta.value = picked.text;/.test(MAIN) && /customPrompt = picked.text;/.test(MAIN));
ok("the choice is persisted and read back", /chrome.storage.local.set\(\{ zsPromptPreset: presetId \}\)/.test(MAIN) &&
  /chrome.storage.local.get\("zsPromptPreset"/.test(MAIN));
ok("a stored id that no longer exists is ignored", /ZS.findPreset\(r.zsPromptPreset\)/.test(MAIN));
ok("the menu re-selects the stored preset when it is rebuilt",
  /p.id === presetId \? " selected" : ""/.test(MAIN));
ok("editing a preset stops the dropdown claiming it is still loaded",
  /loaded.text !== customPrompt/.test(MAIN) && /presetSel.value = "";/.test(MAIN));
ok("clearing the picker leaves the user's own text alone",
  /if \(!picked\) \{\s*status\.textContent = "Preset cleared - type your own, then Save";\s*\} else \{/.test(MAIN));

const CSS = fs.readFileSync(__dirname + "/overlay.css", "utf8");
ok("the picker is styled in both themes", /#zs-set-preset \{/.test(CSS) &&
  /html\.zs-light #zs-set-preset \{/.test(CSS));
ok("the light theme keeps the CSS-drawn arrow",
  /html\.zs-light #zs-set-preset \{\s*background-color:/.test(CSS));
