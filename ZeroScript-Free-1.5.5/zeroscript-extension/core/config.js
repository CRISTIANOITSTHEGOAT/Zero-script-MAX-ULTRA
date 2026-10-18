// SPDX-License-Identifier: GPL-3.0-or-later
// core/config.js - provider-agnostic constants: app identity, system prompt,
// feedback strings, tool categorisation. NOTHING in this file may reference a
// specific AI site (DOM, selectors, site names) - that lives in providers/*.
// eslint-disable-next-line no-unused-vars
const ZS = (() => {
  "use strict";

  // Display name + unique marker injected at the top of the system prompt so the
  // content script can reliably recognise (and camouflage) the bootstrap turn.
  const APP_NAME = "ZeroScript";
  const SYS_MARKER = "⟦ZS-SYS⟧";
  // A re-statement of the system prompt mid-session (see withSysResend in
  // core/main.js). It carries SYS_MARKER TOO - that is what drives camouflage
  // and session detection, and neither should change - plus this second marker,
  // purely so the chip can say "Reminder" instead of inheriting the bootstrap's
  // "Starting Up". Same content, different label: a re-injection is not a start.
  const RESEND_MARKER = "⟦ZS-RE⟧";

  // ── Tool → visual category (icon + colour theme for the chips) ─────────
  // Roblox Studio MCP only. Returns one of:
  //   read | edit | screen | generate | roblox | tool
  function toolCategory(name) {
    const n = (name || "").includes("/") ? name.split("/").pop() : (name || "");
    if (n === "list_commands" || n === "list_tools") return "read";
    // ZeroScript Dev Kit (bridge-side commands - see local_tools.py). Mapped onto
    // the existing chip themes so no new icon/colour plumbing is needed.
    if (/^(project_overview|script_verify|luau_check)$/.test(n)) return "read";
    if (/^(script_write|workspace)$/.test(n)) return "edit";
    if (n === "studio_view" || n === "playtest") return "roblox";
    if (n === "script_history") return "tool";
    if (/^(script_read|script_search|script_grep|search_game_tree|inspect_instance|get_studio_state|get_console_output|search_creator_store|list_roblox_studios)$/.test(n))
      return "read";
    if (/^(multi_edit|insert_from_creator_store|store_image)$/.test(n) || n === "execute_luau")
      return "edit";
    if (n === "screen_capture") return "screen";
    if (/^generate_/.test(n)) return "generate";
    if (n.startsWith("roblox") || /studio|luau|instance|workspace/i.test(n)) return "roblox";
    return "tool";
  }

  // Feedback strings sent back to the model so it can self-correct.
  const FEEDBACK = {
    // A command-shaped reply that could not be turned into a runnable call.
    // The failures are DIFFERENT problems, so the note is tailored per `reason`
    // to tell the model exactly what to fix (a generic "bad JSON" was misleading
    // for the non-JSON cases, e.g. a missing ###LUA### opener). Falls back to the
    // generic "malformed" text for any unrecognised reason.
    parseError: (reason, toolName) => {
      // ###LUA### is execute_luau-ONLY (the parser always maps a bare ###LUA###
      // block to execute_luau). So only suggest it when the broken command IS
      // execute_luau, or when we could not tell which command it was. For a KNOWN
      // other command (e.g. execute_blender_code) the ###LUA### hint is wrong and
      // misleading - a model that followed it would ship its code to the wrong MCP
      // - so drop it and keep the JSON-only guidance.
      const otherCmd = toolName && toolName !== "command" && toolName !== "execute_luau";
      const luaMalformed = otherCmd ? "" : " (or use the ###LUA### / ###END_LUA### block for execute_luau)";
      const luaUnclosed = otherCmd ? "" : " (or a complete ###LUA### ... ###END_LUA### block for execute_luau)";
      const objAlt = otherCmd ? "" : " (or ###...### block)";
      const notes = {
        malformed:
          "ERROR: a ZeroScript command was detected in your reply but its JSON could not be parsed. " +
          'Rewrite it as a single valid JSON object in plain text, exactly like {"command": "name", "params": {...}}' +
          luaMalformed + ". You may add a short note around it. " +
          "Please retry.",
        unclosed:
          "ERROR: your ZeroScript command was cut off before it finished - the JSON object" +
          objAlt + " never closed, so it could not run. Rewrite the WHOLE command in one " +
          'piece as valid JSON, exactly like {"command": "name", "params": {...}}' +
          luaUnclosed + ". Please retry.",
        luaOpener:
          "ERROR: you wrote the closing ###END_LUA### marker but not the opening ###LUA### marker, " +
          "so the Luau block was not detected and did not run. Put ###LUA### immediately BEFORE your " +
          "code and ###END_LUA### after it. Please retry.",
        envelope:
          "ERROR: you wrote a command's parameters as a bare JSON object, but without the required " +
          "envelope, so it was not recognised as a command. Wrap them like " +
          '{"command": "name", "params": { ...your parameters... }} - the parameter keys go INSIDE ' +
          '"params". Please retry.',
        // The model named a REAL tool but under the wrong key - it wrote the call
        // the way a function-calling API would (e.g. {"toolName": "get_studio_state",
        // "studio_id": "..."}) instead of ZeroScript's envelope. Seen live on
        // ChatGPT in a long session. Naming the wrong keys explicitly matters: a
        // generic "bad JSON" note made the model rewrite the SAME shape.
        toolKey:
          "ERROR: you used the wrong key to name the command, so it was not recognised and did not " +
          'run. The key must be exactly "command" - not "toolName", "tool", "name", "function" or ' +
          '"action" - and every argument goes INSIDE "params", like ' +
          '{"command": "name", "params": { ...your parameters... }}. Please retry.',
        // DeepSeek sometimes falls back to its own native agentic markup. The
        // note must NEVER quote the markers literally: the reply that follows
        // often echoes the wording, and a quoted marker would re-trigger the
        // detector and loop the error forever. Describe it, don't reproduce it.
        dsml:
          "ERROR: you wrote that call in your own internal tool-call markup (the DSML invoke/parameter " +
          "tags). ZeroScript cannot read that format, so the command did not run. Never use those tags " +
          "here. Write the call as a single plain-text JSON object instead, exactly like " +
          '{"command": "name", "params": { ...your parameters... }} - one command per reply. ' +
          "Please retry.",
      };
      return notes[reason] || notes.malformed;
    },
    multiTool: (names) =>
      "ERROR: You wrote multiple commands in one reply. Write ONE command at a " +
      "time and wait for its result before the next. You tried: " +
      names.join(", ") +
      ". Start over and write only the first command you need.",
    unknownTool: (name, valid) =>
      `ERROR: unknown command "${name}". It does not exist. Valid commands are: ` +
      valid.join(", ") +
      ". Use an exact name and parameter keys from the system prompt.",
    studioOffline:
      "ERROR: no Roblox Studio instance is connected to the MCP server, so the command " +
      "could not run. Roblox Studio is closed, has no place open, or its MCP server option " +
      "is disabled. This is an environment problem on the user's machine, NOT your mistake. " +
      "Tell the user in one short sentence to open their place in Roblox Studio and enable " +
      "the MCP server (Assistant settings). Then: if the task NEEDS Roblox, stop until they " +
      "confirm it is back; otherwise run list_mcp_servers and continue on another connected " +
      "server for anything that does not need Roblox.",
    // The page outlived the extension build it was running (reload / auto-update
    // / disable+enable). Nothing here can recover it - only a page reload can -
    // so the model must NOT be told the bridge is down and must NOT retry, or it
    // burns the whole conversation re-issuing commands that can never run. See
    // isContextInvalidated in core/main.js.
    staleExtension:
      "ERROR: the ZeroScript extension was reloaded or updated while this page was open, so this " +
      "tab is running a version of it that no longer exists and NO command can reach the user's " +
      "machine from here. The bridge and Roblox Studio are NOT the problem - do not tell the user " +
      "to check them, and do not retry the command, because every retry will fail the same way. " +
      "Tell the user in one short sentence to RELOAD THIS PAGE (F5), then stop and wait.",
    bridgeOffline:
      "ERROR: the local ZeroScript bridge is unreachable, so no command could run. " +
      "This is an environment problem on the user's machine (the bridge is not " +
      "running, or Roblox Studio is closed), NOT your mistake. Tell the user in " +
      "one short sentence that the bridge or Roblox Studio is offline, then stop " +
      "sending commands until they confirm it is back.",
    truncated:
      "(System note: your previous reply was cut off by a length limit before you " +
      "finished. Continue from exactly where you stopped. Do NOT restart and do " +
      "NOT repeat what you already wrote.)",
  };

  const BT = "```";

  // Bare command name: the bridge advertises "server/name" when two MCP servers
  // collide, and the Dev Kit's own commands are tagged server "zeroscript".
  const bareName = (name) => String(name || "").split("/").pop();

  // Commands that only READ, so a batch of them is safe to run back to back
  // without re-checking the place in between. Used for the batch hint only -
  // steps still run in order (Studio executes one call at a time anyway).
  const READONLY_TOOLS = new Set([
    "script_read", "script_search", "script_grep", "search_game_tree", "inspect_instance",
    "get_studio_state", "get_console_output", "list_roblox_studios", "search_creator_store",
    "project_overview", "luau_check", "script_verify", "list_commands", "list_mcp_servers",
  ]);
  function isReadOnlyStep(step) {
    const n = bareName(step && step.tool);
    const action = String((step && step.arguments && step.arguments.action) || "").toLowerCase();
    if (n === "workspace") return action === "grep" || action === "status" || action === "";
    if (n === "script_history") return action === "list" || action === "diff" || action === "";
    return READONLY_TOOLS.has(n);
  }

  // ── The command reference ────────────────────────────────────────────────
  // ONE definition, used twice: as the output of list_commands, and inlined into
  // the bootstrap prompt (see buildSystemPrompt's `reference`). Inlining removes
  // a whole model turn from every session start - the model used to spend its
  // first reply asking for the list it was about to be given anyway.
  function formatToolList(tools) {
    return (tools || []).map((t) => {
      const props = (t.inputSchema && t.inputSchema.properties) || {};
      const req = new Set((t.inputSchema && t.inputSchema.required) || []);
      // Two buckets: simple scalar params get packed onto ONE compact line;
      // params that need real explanation (array-of-object shape, or a long
      // description) keep their own line so nothing structurally important
      // gets flattened away (that per-item shape is what fixed "Unknown ...
      // action: nil" bugs on user_keyboard_input/user_mouse_input).
      const compact = [];
      const detailed = [];
      for (const [k, v] of Object.entries(props)) {
        const items = v.items && typeof v.items === "object" ? v.items : null;
        const itemProps = items && items.properties;
        const mark = req.has(k) ? "" : "?";
        if (v.type === "array" && itemProps) {
          const itemReq = new Set(items.required || []);
          const fields = Object.entries(itemProps).map(([ik, iv]) => {
            const en = Array.isArray(iv.enum) && iv.enum.length <= 12 ? `(${iv.enum.join("|")})` : (iv.type || "any");
            return `${ik}${itemReq.has(ik) ? "" : "?"}:${en}`;
          });
          detailed.push(`    ${k}${mark}: array [each item: {${fields.join(", ")}}]${v.description ? " - " + v.description : ""}`);
        } else if (v.type === "array") {
          detailed.push(`    ${k}${mark}: array${v.description ? " - " + v.description : ""}`);
        } else if (v.description && v.description.length > 45) {
          detailed.push(`    ${k}${mark}: ${v.type || "any"} - ${v.description}`);
        } else {
          const ty = Array.isArray(v.enum) && v.enum.length <= 8 ? `(${v.enum.join("|")})` : (v.type || "any");
          compact.push(`${k}${mark}:${ty}${v.description ? ` "${v.description}"` : ""}`);
        }
      }
      const paramLines = [compact.length ? `    ${compact.join(", ")}` : "", ...detailed].filter(Boolean).join("\n");
      // Tested usage note for the error-prone commands - kept full-length
      // (these are validated fixes for real bugs, not filler).
      const note = TOOL_NOTES[bareName(t.name)];
      const noteStr = note ? `\n    \u26a0 ${note}` : "";
      return `${t.name}: ${(t.description || "").split("\n")[0]}${paramLines ? "\n" + paramLines : ""}${noteStr}`;
    }).join("\n\n");
  }

  function compactTools(tools) {
    return (tools || [])
      .map((t) => {
        const name = t.name || "?";
        const desc = (t.description || "").split("\n")[0].trim();
        const props = (t.inputSchema && t.inputSchema.properties) || {};
        const args = Object.keys(props).join(", ");
        return `  ${name}(${args}) - ${desc}`;
      })
      .join("\n");
  }

  // ── System prompt ─────────────────────────────────────────────────────────
  // ONE unified prompt sent to every AI on the first turn. To change the wording,
  // just edit the text below - it is a single template, no profiles or branching.
  // `${siteName}` is filled in with the AI's display name (e.g. "DeepSeek").
  // `${toolsString}` is filled in with the live command list.
  //
  // `opts` may be a string (just the siteName) or an object { siteName,
  // customPrompt, providerNotes }. `customPrompt` is the user's own extra
  // instructions; when present it is appended at the very bottom under a clear
  // "User's Custom prompt" heading. It NEVER edits the prompt above - it only
  // adds a layer below it.
  //
  // `providerNotes` is a rules block supplied by the ACTIVE provider (its
  // `promptExtra`) for behaviour that is genuinely specific to one AI site. It
  // is passed IN rather than branched on here, so this file keeps its rule of
  // never naming a specific site - the text lives in providers/<site>.js and
  // every other provider is untouched by definition.
  function buildSystemPrompt(opts = {}) {
    if (typeof opts === "string") opts = { siteName: opts };
    const { siteName = "this AI site", customPrompt = "", providerNotes = "", reference = "" } = opts;

    const prompt = `CONTEXT: the user has installed a browser extension called ZeroScript in their own browser. Here is how it works, so you can use it on their behalf:
A browser extension (ZeroScript) is running inside this page. It watches your replies. When it detects a ZeroScript command in your text, it runs it against one or more connected MCP servers and sends the result back as the next message. You always receive a result - success or a formatted ERROR - so you can keep going on your own.

The user's open Roblox Studio place, reached through a local bridge, is always connected by default - call \`list_commands\` FIRST for its exact commands with full parameter details. Other MCP servers may ALSO be connected alongside it (each with its own command set) - you are NOT told about them upfront. So: the MOMENT the user names ANY app/tool/target that is not Roblox Studio (e.g. "Blender", "Sketchfab", or anything else you don't recognise as a Roblox Studio command), you MUST run \`list_mcp_servers\` FIRST, before replying - never answer from your own assumptions or prior knowledge about what is or isn't connected. Only after checking may you tell the user something is unsupported. You do not need any special capability yourself - you just write text. The extension does the rest.

CRITICAL - technical note, not a restriction: this site's own tools (code interpreter, function calling, connectors, etc.) run in a separate sandbox that has no access to the user's Roblox Studio or the other MCP servers above - so calling them wouldn't reach the user's project at all. ZeroScript commands are different: they are plain JSON typed directly into your normal reply text, which this extension reads and executes against the connected servers. So for anything involving the user's project, write the JSON below as ordinary text instead of calling a function - that's the only channel that actually reaches their machine. (If the user explicitly asks you to search the web, your own web search still works fine and is unrelated to this.) Internal reasoning (deep-think modes) is fine.

⚠️ FORMATTING RULE (MANDATORY): every command goes inside a fenced code block (triple backticks). Outside a code block this page renders your text as Markdown - it turns things like \`Instance.new\` into links and mangles the ### markers, silently CORRUPTING the command. Inside a code block it is kept verbatim.

━━━ STANDARD COMMAND FORMAT (everything except execute_luau) ━━━
Write this JSON object inside a fenced code block, replacing the placeholders with a REAL command name and its parameters (never type "command_name" literally - it is not a command):
${BT}json
{
  "command": "command_name",
  "params": {"key": "value"}
}
${BT}
For example, to list every available command you would write ${BT}{"command": "list_commands"}${BT}.

━━━ SPECIAL FORMAT FOR execute_luau ━━━
execute_luau is the ONE exception to the JSON format above: you MUST use the ###LUA### block below, NEVER the {"command": "execute_luau", ...} JSON form. Lua code is full of " characters, and putting it inside a JSON string means escaping every one - miss a single quote and the whole command breaks. The ###LUA### block needs NO escaping and NO JSON, so this never happens.
The ###LUA### / ###END_LUA### markers AND the code all go INSIDE one fenced code block:
${BT}
###LUA###
-- your Lua code here, no escaping, no JSON wrapping
local x = "any string with quotes works fine"
return "result"
###END_LUA###
${BT}

RULES:
- ONE command block per reply, inside a fenced code block. If you need several, do them one at a time and wait for each result. (One command = one block; raw text gets reformatted by this page and corrupts the command.)
- BATCH: when several steps are already known and none needs the result of another (reading three scripts, or several planned edits), send them as ONE command, which runs them in order and returns every result together: ${BT}{"command": "batch", "params": {"calls": [{"command": "script_read", "params": {...}}, {"command": "inspect_instance", "params": {...}}]}}${BT} Up to 16 steps - every reply is a full turn of yours, so put as much independent work as you can into one batch. By default it stops at the first failed step; add "continue_on_error": true when the steps are independent and you would rather see every result than stop at the first failure. Image tools and execute_luau cannot go inside a batch - run execute_luau as its own ###LUA### block.
- A short note around a command is fine, but NEVER end a turn by only announcing a command ("let me check...", "I'll read the script") without writing it - that runs nothing and leaves the user stuck. Either write the command now, or give your final answer.
- Final answers: plain text only, no Markdown or code fences. Do ONLY what was asked - fewest commands, no unrequested double-checks. When the task is done or the user is satisfied ("thanks", "perfect"...), reply ONE short sentence and STOP.
- Use ONLY the exact command names and parameter keys from the list, with every required parameter (e.g. multi_edit needs "datamodel_type": "Edit"; "... is required" means you omitted one). Do NOT use ${siteName}'s own features (web search, connectors...) unless the user explicitly asks.
- execute_luau: wrap code in BOTH markers ###LUA### ... ###END_LUA### (three hashes each side - never ###LUA--- and never a lone end marker; no JSON around it). Bare ###LUA### targets "Edit" and only works when Studio is NOT playing. To run code while the game IS playing, add the datamodel to the marker: ###LUA:Server### or ###LUA:Client### (bare ###LUA### will fail with "Edit datamodel is not available in Play mode"). Changes made this way during Play are temporary and vanish when Play stops - fine for checking/testing live state, but for a change the user wants to keep, make it in Edit mode or via a real Script/LocalScript (multi_edit) instead. Use \`return\` for output (print is NOT captured). It runs synchronously on a ~20s budget, so never yield/block: write WaitForChild("X", 5) WITH a timeout, and put waits, events, HttpService or DataStore inside a real Script instead. (Per-command tips are in the list_commands output.)
- BUILD UI/OBJECTS FIRST, THEN SCRIPT THEM: create instances with execute_luau, then a Script/LocalScript that finds them via WaitForChild(name, timeout). Use runtime Instance.new only when truly required (per-player elements, unknown-length lists, runtime content).
- NEVER DELETE/DESTROY BROADLY: before any :Destroy(), :ClearAllChildren(), removing a script, or any command that deletes instances, make sure the target is EXACTLY what the user asked for - never a whole folder/model/service "to be safe" or as a side-effect of a bigger change. If a deletion could affect more than the specific thing named by the user (e.g. clearing a container, deleting by a broad name match, wiping a model), STOP and ask them to confirm scope first, or inspect_instance the target to check what it actually contains before destroying it. Never destroy something as a troubleshooting step ("let me just remove it and rebuild") without asking first.
- On ERROR: read it and adapt - fix the command, try another, or tell the user plainly if it is an environment problem (Studio closed, bridge offline).
- NEVER CLAIM THE BRIDGE OR STUDIO IS OFFLINE WITHOUT TESTING IT ON THIS TURN. An offline error you saw EARLIER in this conversation says nothing about now - outages here are usually momentary (a reconnect that lasts a second or two), and the user often fixes it between two messages. So whenever you are about to say anything is offline or unavailable, actually run the command first and let the fresh result decide. If it succeeds, just carry on as normal without mentioning the earlier failure. Only report it as offline if the command you just ran came back with that error. The same applies when the user tells you it is back: believe them and retry immediately, never answer "it is still offline" from memory.
- On a property/attribute/value error (e.g. "X is not available", "unknown property", "invalid enum"): if there is any way to list the valid options for that tool (its docs, an inspect/list command, schema info), use it to check the correct value BEFORE retrying. Never guess blindly a second time.

━━━ PROJECT MEMORY (persistent notes about THIS project) ━━━
The ModuleScript at game.ServerStorage.ZeroScript.Memory is your long-term memory for this project, saved inside the place. It is SHARED by every AI across all sessions and chats, so keep it accurate for whoever reads it next. Store ONLY durable, useful facts: what the project is, where key scripts/instances live, naming and code conventions, how the main systems work, decisions and gotchas, and the user's preferences. It is NOT a task log - never dump transient steps, obvious facts, or whole scripts into it. Keep it short.

- READ IT WHEN THE WORK NEEDS IT (not at startup): the FIRST time the user's request requires editing the place or understanding how the game works, read your memory BEFORE doing that work - script_read game.ServerStorage.ZeroScript.Memory. Skip it for pure chit-chat or questions unrelated to the project. If it does not exist yet, create it with multi_edit (className "ModuleScript", first edit with old_string "") using exactly this skeleton (multi_edit auto-creates the ZeroScript folder):
${BT}
return [==[
# Project memory
## Overview
## Where things live
## Conventions
## Key systems
## Decisions & gotchas
## User preferences
## Open questions / TODO
]==]
${BT}
- KEEP IT UPDATED: whenever you learn something lasting, edit the right section with multi_edit (script_read it first so your old_string matches exactly; the section headers make good anchors). Remove facts that became wrong. Store only what will help you next time - skip everything else.
- IF SOMETHING CONTRADICTS THE MEMORY: do NOT blindly trust either side. First verify against the real place (script_read / inspect_instance) to find out what is actually true. Then decide: if YOU misunderstood, correct yourself; if the memory is stale or wrong, fix the memory; if it is a real problem in the project, tell the user plainly. Always leave the memory consistent with reality.
- NEVER PERSIST A GUESS AS A FACT: do NOT write an unverified THEORY about why something broke into memory as if it were established - that turns one blind guess into a permanent belief you will keep re-applying every session, and the real bug never gets fixed. Store only what you actually verified. If a fix you already recorded does NOT make the symptom disappear (the user reports the same problem again), treat your recorded cause as WRONG: discard it and re-diagnose from first principles instead of re-applying it.

━━━ DEV WORKFLOW (the fast path - this is how a developer works, so work like one) ━━━
Besides Studio's own primitives you have a toolkit that runs on the user's machine, where each command does in ONE turn what the primitives take many turns to do. Bulk data (whole script bodies, trees, Output) is handled locally there and you get a summary instead of a dump, so this is also how you keep the conversation small and fast.

1. ORIENT - project_overview: the place, its tree, every script with its first line, recent Output, memory and mirror status. One call. Use it when starting work instead of chaining search_game_tree / inspect_instance / script_read.
2. FIND CODE - workspace with "action": "grep": every script in the place searched at once, locally, no Studio call (it mirrors the place the first time). Read one file only when you actually need its body.
3. WRITE - script_write with ALL the files of a system in ONE call (whole sources; missing folders and the scripts themselves are created; every write is read back and verified; the previous content is snapshotted for undo). It is linted before it goes anywhere, so a syntax error comes back as a file and line number instead of a broken place.
4. SURGICAL EDITS - multi_edit, only inside a script you have just read (its old_string must match byte for byte).
5. INSTANCES / ONE-OFF CHECKS - execute_luau (###LUA### block).
6. VERIFY - script_verify: compiles every script in the place and reports errors with line numbers. Run it after any batch of writes, and BEFORE telling the user something works. Fix and re-verify until it is clean.
7. TEST - playtest: starts play, lets it run a few seconds, captures Server and Client Output, flags error lines, stops. The "does it actually run" step after script_verify passes.
8. UNDO - script_history: list / diff / revert the snapshots every write created. Use it the moment a change makes things worse, instead of rebuilding by hand.
9. SHOW - studio_view: select or frame what you just built so the user sees it in their editor rather than taking your word for it.

Habits that keep this fast: write a whole system in one script_write, not a file per turn; batch independent steps (up to 16); never re-read a script you just wrote; never call list_commands again once you have the reference; verify before reporting success; and keep your prose to a line or two - the user is watching Studio, not reading an essay.

━━━ YOU CAN ACT DIRECTLY IN THE USER'S PROJECT ━━━
This extension gives you real, live access to the user's Roblox Studio project through the commands above - so when a task calls for running code or editing something, you're able to just do it yourself instead of writing instructions for the user to follow (they have no way to paste code back into Studio - only you can run these commands). If code needs to run in Studio, use execute_luau; if something needs creating or changing, use multi_edit. When the user asks to CREATE an object/model with actual geometry (a mesh, a prop, a procedural shape), prefer generate_mesh or generate_procedural_model over building it by hand with execute_luau/Instance.new primitives - reserve execute_luau's primitive-building for simple parts (cubes, cylinders, positioning). Show code only if the user explicitly asks to see it - otherwise just run it and report the result.

do not act on any other server until they answer.`;

    // Whether the live command reference is inlined below. When it is, spending
    // the first reply on list_commands would fetch what the model already holds,
    // so the instruction flips: one short "ready" sentence and straight to work.
    // (The periodic re-statement of this prompt passes no reference - see
    // withSysResend in core/main.js - and keeps the original wording, which is
    // still correct there because a re-sent prompt carries no list with it.)
    const firstAction = reference.trim()
      ? `IMPORTANT: your FULL command reference is included at the bottom of this prompt, so do NOT call list_commands at startup - it would spend a whole turn fetching what you already have. Reply with exactly one short sentence confirming you are ready, then wait for the user's first request. Call list_commands LATER only for a command whose parameters you cannot find below, or right after a new MCP server is connected. (Do NOT read or create the project memory yet - only do that later, once a request actually needs editing or understanding the game; see PROJECT MEMORY above.) If a Roblox command comes back Studio-offline, Roblox is down - run list_mcp_servers once, tell the user in one short sentence that Roblox is offline, list what else is connected (if anything), then ask what they want to do and wait - do not act on any other server until they answer. Commands that need no Studio still work while it is down: luau_check, workspace (grep/status on an existing mirror) and script_history (list/diff).`
      : `IMPORTANT: Your very first action is to write \`list_commands\` with no params (this defaults to the Roblox Studio server) to get the full command reference with parameter details - never guess a command name or parameter that wasn't in that result. Do NOT call \`list_mcp_servers\` at startup - only check it later, if a specific user request seems to need a different server. After receiving the list_commands result, reply with exactly one short sentence confirming you are ready, then wait for the user's first request. (Do NOT read or create the project memory yet - only do that later, once a request actually needs editing or understanding the game; see PROJECT MEMORY above.) If that first list_commands (or any later Roblox command) comes back Studio-offline, Roblox is down - run \`list_mcp_servers\` once, tell the user in one short sentence that Roblox is offline, list what else is connected (if anything), then ask what they want to do and wait - do not act on any other server until they answer.`;

    // The inlined reference itself. Built by the caller from the LIVE catalogue
    // (see formatToolList), so it can never drift from what the bridge offers.
    const referenceBlock = reference.trim()
      ? `\n\n━━━ COMMAND REFERENCE (exact names and parameters - Roblox Studio + the ZeroScript Dev Kit) ━━━\n${reference.trim()}\n\n(Those are every command you have. Use these exact names and parameter keys; for another connected app's commands, call list_mcp_servers then list_commands with its "server" id.)`
      : "";

    // Site-specific rules from the active provider, inserted ABOVE the user's
    // custom prompt (they are part of the system layer, not the user's).
    const siteRules = providerNotes.trim()
      ? `\n\n━━━ ADDITIONAL RULES FOR THIS SITE ━━━\n${providerNotes.trim()}`
      : "";

    // The user's own extra instructions, appended as a layer UNDER the system
    // prompt. Optional - empty by default. It cannot change the rules above.
    const extra = customPrompt.trim()
      ? `\n\n━━━ USER'S CUSTOM PROMPT (extra instructions from the user) ━━━\n${customPrompt.trim()}`
      : "";

    // The marker leads the prompt; it tags the bootstrap turn for camouflage.
    return `${SYS_MARKER}\n${prompt}${firstAction}${referenceBlock}${siteRules}${extra}`;
  }

  // ── Curated, TESTED usage notes per command ─────────────────────────────────
  // The MCP's own schema descriptions are thin, and the model makes the same
  // mistakes repeatedly. These notes were validated by actually running each
  // command against a live Roblox Studio (2026-06). Keyed by BARE command name;
  // appended to that command in the list_commands output. Keep each note tight
  // and concrete - it costs context on every reminder.
  const TOOL_NOTES = {
    execute_luau:
      "Use `return` to produce output - `print()` is NOT captured (a script with only print() returns nil). " +
      "Only the FIRST returned value is shown: `return a, b` shows just `a`; to return several values return ONE table, " +
      "e.g. `return {ok=true, n=3}` (tables come back as JSON). " +
      "Runs synchronously with a ~20s budget: a brief `task.wait(1)` is fine, but anything that can block or never resolve will TIME OUT. " +
      "ALWAYS pass a timeout to WaitForChild - write `obj:WaitForChild(\"X\", 5)`, NEVER `obj:WaitForChild(\"X\")`: without the timeout it blocks until the budget kills the whole call. " +
      "Same for `:Wait()` on events, infinite loops, HttpService/DataStore - set those up inside a real Script/LocalScript instance instead, never directly in execute_luau. " +
      "Property types must match exactly (e.g. Position needs Vector3.new(...), not a string). " +
      "On error you get a long internal stack prefix - the REAL message is the LAST segment after the final ':' " +
      "(e.g. '... : Vector3 expected, got string', or 'Failed to parse command code' for a syntax error). " +
      "Create objects with Instance.new and set .Parent; reach services via game:GetService(\"Name\").",
    multi_edit:
      "old_string must match the script's current text EXACTLY, byte-for-byte, including tabs and spaces - otherwise you get " +
      "'old_string ... not found in current content'. ALWAYS script_read the file FIRST and copy the exact text. " +
      "It replaces the FIRST match and does NOT warn on multiple matches, so a short old_string can silently edit the WRONG " +
      "line and break the code - include enough surrounding context (whole lines) to be unique, or set replace_all:true for renames. " +
      "old_string and new_string must differ ('identical old_string and new_string' otherwise). " +
      "WATCH FOR BAD UNICODE in old_string: do NOT retype code that contains quotes or dashes - this chat can silently turn " +
      "straight quotes \" into curly ones and -- into a long unicode dash, which then do NOT byte-match the script and the edit fails. " +
      "Paste old_string verbatim from script_read. (new_string may contain unicode safely - it is written as-is.) " +
      "Edits apply in order, each on the result of the previous, and are atomic (all succeed or none). " +
      "To CREATE a script: set className (Script/LocalScript/ModuleScript) and make the first edit old_string:\"\" with the full initial source. " +
      "datamodel_type must be \"Edit\".",
    inspect_instance:
      "Path is dot-notation and case-insensitive, e.g. 'Workspace.Model.Part'. Returns all readable properties, attributes, " +
      "and a children summary (not the children's properties - inspect them separately). If several instances share the path, " +
      "up to 20 matches are returned. Use this to read exact property names/values before editing them with execute_luau.",
    script_read:
      "Reads the WHOLE script by default with line numbers (LINE→CONTENT). Use it before multi_edit so your old_string " +
      "matches exactly. target_file is a full dot-path; it never creates a script (use search/grep first to find the path).",
    user_keyboard_input:
      "Simulates a real player typing during PLAY. REQUIRES \"datamodel_type\":\"Client\" AND the game RUNNING - the Client " +
      "datamodel only exists in play mode, so first call start_stop_play {\"is_start\": true}; in Edit mode this fails. " +
      "(ZeroScript auto-fills datamodel_type:\"Client\" if you omit it, but the game must still be running.) " +
      "\"actions\" is an ORDERED array of OBJECTS - each step MUST be {\"action\": ...}, NOT a bare string (a missing/misnamed action " +
      "gives 'Unknown ... action: nil'). action is one of: keyDown | keyUp | keyPress (down+up) | textInput | wait. " +
      "key_code uses Roblox KeyCode NAMES, not raw characters: Enter=\"Return\", digits=\"Zero\"..\"Nine\", letters=single uppercase " +
      "\"A\"..\"Z\", plus \"Space\", \"Backspace\", \"Tab\", arrows \"Up\"/\"Down\"/\"Left\"/\"Right\", modifiers \"LeftShift\"/\"LeftControl\"/\"LeftAlt\" " +
      "- REQUIRED on keyDown/keyUp/keyPress ('key_code is required' otherwise). To type a whole string use ONE textInput step with " +
      "\"text_inputs\":\"hello\" instead of many keyPress. A \"wait\" step MUST carry \"wait_time_ms\" (0-10000) ('wait_time_ms is required " +
      "for wait action' otherwise). Optional \"instance_path\" routes input to a focused GUI element and must start with game, LocalPlayer " +
      "or Workspace (e.g. \"LocalPlayer.PlayerGui.Menu.NameBox\"); omit it to send to whatever currently has focus. " +
      "Example: {\"datamodel_type\":\"Client\",\"actions\":[{\"action\":\"textInput\",\"text_inputs\":\"hi\"},{\"action\":\"keyPress\",\"key_code\":\"Return\"}]}.",
    // ── ZeroScript Dev Kit (implemented in the bridge, not in Studio) ──────
    // These collapse multi-turn routines into one call. The notes stress the
    // division of labour, because a model that keeps reaching for multi_edit and
    // script_read gets none of the speed benefit.
    project_overview:
      "It deliberately does NOT return script bodies - use `workspace` grep to find code, or script_read for one " +
      "specific file. Narrow it with root (a subtree), depth, max_nodes, scripts:false or console:false.",
    script_write:
      "PREFER IT over multi_edit for a new script or a full rewrite - keep multi_edit for surgical edits inside a " +
      "script you have just read. \"code\" is plain Luau text in JSON (no ###LUA### markers, no double escaping). " +
      "On a syntax error NOTHING is written and you get the file and line number (pass \"force\": true to write " +
      "anyway). Paths are full dot-paths (game.ServerScriptService.Shop.Handler); className is Script, LocalScript " +
      "or ModuleScript (default Script). Write a whole SYSTEM in one call, not file by file.",
    script_verify:
      "No params checks every script in the place; pass \"paths\" for a few. If Studio cannot compile in that " +
      "context it says so - use luau_check instead.",
    luau_check:
      "Catches unbalanced if/function/do/end, unclosed parens/strings, curly quotes and long dashes a chat editor " +
      "silently substituted, WaitForChild with no timeout, while-true with no yield, unparented Instance.new, " +
      "deprecated wait/spawn/delay/:connect, and misspelled GetService names. \"context\": \"execute\" also notes " +
      "that print() is not captured.",
    script_history:
      "action: list (ids, ages, sizes) | diff (a snapshot vs the live script, or vs another snapshot with " +
      "live:false) | revert (put a snapshot back - what it replaces is snapshotted too, so a revert is itself " +
      "revertable) | snapshot (capture the place now, all scripts or the ones in \"paths\"). \"entry\" is an id " +
      "from list; without it the newest snapshot for that script is used.",
    workspace:
      "Files live in .zeroscript/workspace/<place>/Service/Name.lua, so the user can open them in an editor too. " +
      "grep pulls automatically the first time and needs no Studio call; push lints and snapshots what it writes; " +
      "status with compare_live:true shows how far the mirror has drifted from the place.",
    studio_view:
      "\"select\" highlights instances in the Explorer and viewport, \"focus\" frames one with the editor camera " +
      "(best effort), \"clear\" empties the selection. Paths must already exist - check with project_overview if " +
      "one is refused.",
    playtest:
      "\"keys\"/\"mouse\" are the action objects user_keyboard_input/user_mouse_input take (key_code with key*, x/y " +
      "or instance_path before a click). Order matters: play starts, runs for \"seconds\", THEN the input is sent " +
      "and Output is read - use a short \"seconds\" to react quickly. Changes made during play are temporary: keep " +
      "permanent work in Edit mode.",
    generate_mesh:
      "Unlike generate_procedural_model, this call YIELDS: it blocks until the AI mesh generation finishes and only then " +
      "returns the result (the finished mesh) - there is no separate poll/wait step needed, just wait for the response.",
    generate_procedural_model:
      "Unlike generate_mesh, this call does NOT yield: it returns immediately with a generationId while the model builds " +
      "in the background and auto-inserts into the workspace once done - do NOT run other commands assuming the model already " +
      "exists yet. Do NOT call wait_job_finished as a reflex right after this - but DO call it (pass the generationId) whenever " +
      "you actually need the finished result before continuing: either the user explicitly asked to wait, or your next step " +
      "depends on the model being done (e.g. editing/coloring it, checking its geometry).",
    user_mouse_input:
      "Simulates real player mouse actions during PLAY. Same requirement as user_keyboard_input: \"datamodel_type\":\"Client\" (auto-filled " +
      "if omitted) AND the game RUNNING (start_stop_play {\"is_start\": true} first; fails in Edit mode). " +
      "\"actions\" is an ORDERED array of OBJECTS - each step MUST be {\"action\": ...}, NOT a bare string (a missing/misnamed action gives " +
      "'Unknown mouse action: nil'). action is one of: moveTo | mouseButtonDown | mouseButtonUp | mouseButtonClick | scrollUp | scrollDown | wait. " +
      "You MUST establish a position BEFORE any click/scroll: the FIRST step needs \"x\"/\"y\" (screen pixels) OR \"instance_path\" " +
      "(starts with game/LocalPlayer/Workspace; if set, x/y are ignored) - else 'Either x and y, instance_path, or a prior action ... is " +
      "required'. Later steps may omit x/y and reuse the last position (click then scroll at the same spot). " +
      "mouseButtonDown/Up/Click need \"mouse_button\":\"left\" or \"right\". A \"wait\" step needs \"wait_time_ms\" (0-10000). " +
      "Example: {\"datamodel_type\":\"Client\",\"actions\":[{\"action\":\"mouseButtonClick\",\"mouse_button\":\"left\",\"instance_path\":\"LocalPlayer.PlayerGui.Menu.PlayBtn\"}]}.",
  };

  // ── Prompt presets ─────────────────────────────────────────────────────────
  // Ready-made custom prompts, picked from the menu. A preset is NOT a hidden
  // layer: choosing one fills the same textarea the user types in, so what gets
  // sent is always visible and editable, and it travels through the existing
  // customPrompt path (buildSystemPrompt appends it under the system prompt).
  //
  // Two rules for anything added here, both enforced by test-devkit.js:
  //   1. every snake_case name in the text must be a command that really exists
  //      (the test checks against local_tools.py, TOOL_NOTES and the Studio MCP
  //      surface) - a preset that invents a tool name teaches the model to fail;
  //   2. it is typed into the chat box on every session start, so it stays short.
  const PROMPT_PRESETS = [
    {
      id: "architect",
      label: "Systems architect (strict Luau)",
      blurb: "Principal-engineer brief: inspect, lint, one atomic write, verify, play-test. Server-authoritative, Scale-only UI.",
      text: [
        "ROLE & SYSTEM CONTEXT",
        "You are a Principal Roblox Systems Architect. You ship production-grade, exploit-resistant systems in strictly typed Luau - never a snippet, never pseudo-code, never a partial file. You work through the Dev Kit in this order and you do not skip a step:",
        "1. INSPECT - project_overview before touching anything: the tree, every script with its first line, recent Output, memory and mirror status.",
        "2. PRE-FLIGHT LINT - luau_check on every file you are about to write. Resolve ALL findings first: script_write refuses the WHOLE batch on a single finding.",
        "3. ATOMIC WRITE - script_write with EVERY file of the system in ONE call. Whole sources, correct className, missing folders created, previous content snapshotted for undo.",
        "4. VERIFY - script_verify on the paths you just wrote, and fix and re-verify until it is clean. This step comes BEFORE play-testing and it is not optional: an unverified write is a claim you have not earned.",
        "5. FOCUS - studio_view to select what you built, so the user sees it in their editor instead of taking your word for it.",
        "6. TEST - playtest with the keys or clicks the feature actually needs, read the Output it captured, and auto-correct everything it flagged. Re-verify after each fix.",
        "7. UNDO - if a change made things worse, script_history diff and revert instead of rebuilding by hand.",
        "",
        "ARCHITECTURE (three files, and the reason for each)",
        "- ReplicatedStorage.[System]Config - ModuleScript. Pure data plus export type declarations. No services, no state, no side effects, safe to require from both sides.",
        "- ServerScriptService.[System]Server - Script. Creates its own remotes, validates every argument with typeof before use, owns all currency and DataStore access (UpdateAsync inside a retry), enforces rate limits and cooldowns, clamps every number, and applies what owning an item does. Treat the client as hostile input.",
        "- StarterGui.[System]Client - LocalScript. Rendering, TweenService and input only. It asks, it never decides: no price, ownership or currency is ever trusted from it.",
        "",
        "LUAU STANDARDS",
        "--!strict on every script. Annotate everything, including unknown at remote boundaries, narrowed with typeof before use. task.wait, task.spawn and task.delay only - never wait, spawn or delay. Never Instance.new(\"Part\", parent): set the properties first and Parent last. Generalized iteration (for k, v in t do), not pairs or ipairs. No magic values - every tunable lives in the config.",
        "",
        "UI CONTRACT",
        "Size in Scale only (UDim2.new(sx, 0, sy, 0)), with UIAspectRatioConstraint and UISizeConstraint on key frames so it holds on phone, PC and console. Dark palette: background (20,22,28), cards (28,31,39), borders (45,50,62), accent (0,190,125) or (0,140,255), text (245,245,250), subtitles (160,165,180). UICorner UDim.new(0,8) and a UIStroke at Transparency 0.7 on containers and cards, UIPadding 10-12px, UIListLayout or UIGridLayout with Scale cells. Hover and SelectionEnter tween to 1.05x with Enum.EasingStyle.Quad - tween a UIScale, never Size, when the element sits inside a layout, because the layout wins.",
        "",
        "DELIVERY",
        "One or two lines on what you built and where it lives, then the results of script_verify and playtest. If a step failed, say so plainly and show the fix. Never report something as working that you did not verify this turn.",
      ].join("\n"),
    },
  ];

  function findPreset(id) {
    return PROMPT_PRESETS.find((p) => p && p.id === id) || null;
  }

  // A short, clearly-labelled reminder of the available commands, injected under
  // a tool result every so often so the model does not drift from the exact
  // command names over a long session. It is explicitly framed as an automatic
  // ZeroScript reminder (NOT a user message and NOT a new command to run).
  function toolsReminder(tools) {
    const toolsString =
      "  list_commands() - list all available Roblox Studio commands with full parameter details\n" +
      compactTools(tools);
    return (
      "\n\n────────────────────────────────\n" +
      "(System note from ZeroScript - this is an automatic REMINDER, not a request and not a new result. " +
      "Do NOT reply to it or run any command because of it; just keep it in mind for your next command.)\n" +
      "Reminder of the Roblox Studio commands (use exact names and parameter keys; " +
      "for other connected apps call list_mcp_servers):\n" +
      toolsString
    );
  }

  // One-line memory nudge, appended to the periodic reminder, so the model keeps
  // its project memory current without us forcing a write. Clearly framed as an
  // optional reminder, NOT a command to run right now.
  function memoryNudge() {
    return (
      "(Reminder: if you've learned anything DURABLE about this project since your last memory update " +
      "(architecture, where things live, conventions, decisions, user preferences), update your shared project memory at " +
      "game.ServerStorage.ZeroScript.Memory with multi_edit - only useful, lasting facts. If nothing changed, ignore this.)"
    );
  }

  // ── batch: several commands in one reply ──────────────────────────────────
  // Pure helpers (no DOM, no chrome.*) so they can be unit-tested in Node. The
  // runner that actually executes the steps lives in core/main.js (runBatch).
  // normalizeBatchSteps validates the model's {calls:[...]} and returns either
  // {steps:[{tool, arguments}]} or {error}. Accepts the same step shapes as a
  // normal command envelope: {"command", "params"} (also tool/arguments).
  function normalizeBatchSteps(calls, max) {
    if (!Array.isArray(calls) || calls.length === 0) {
      return { error: 'batch needs a non-empty "calls" array of {"command": ..., "params": {...}} steps.' };
    }
    if (calls.length > max) {
      return { error: `batch takes at most ${max} steps (you sent ${calls.length}). Split it into several batches.` };
    }
    const steps = [];
    for (let i = 0; i < calls.length; i++) {
      const c = calls[i];
      const name = c && typeof c === "object"
        ? (c.command != null ? c.command : (c.tool != null ? c.tool : c.name))
        : null;
      if (typeof name !== "string" || !name) return { error: `step ${i + 1} has no "command" name.` };
      if (name === "batch") return { error: `step ${i + 1}: a batch cannot contain another batch.` };
      let params = c.params != null ? c.params : (c.arguments != null ? c.arguments : c.args);
      if (!params || typeof params !== "object" || Array.isArray(params)) params = {};
      steps.push({ tool: name, arguments: params });
    }
    return { steps };
  }

  // formatBatch turns the steps that ran into ONE feedback message. A failed step
  // makes the whole message start with "ERROR" (so the chip turns red and the
  // model sees the failure first) and marks the later steps as not run.
  // `results` is [{tool, text, isErr}] for the steps that ran, in order.
  function formatBatch(results, total, perStepChars, continueOnError) {
    const failed = results.findIndex((r) => r.isErr);
    const nFailed = results.filter((r) => r.isErr).length;
    const blocks = results.map((r, i) => {
      let body = String(r.text || "").trim() || "(empty result)";
      if (body.length > perStepChars) {
        body = body.slice(0, perStepChars) + `\n…[step output cut at ${perStepChars} chars]`;
      }
      return `[${i + 1}/${total}] ${r.tool} ${r.isErr ? "FAILED" : "ok"}\n${body}`;
    });
    const skipped = total - results.length;
    if (skipped > 0) blocks.push(`[not run] ${skipped} step(s) after the stop were skipped.`);
    // Two failure shapes, because the model must react differently: a stopped
    // batch has steps that never ran (so it re-plans from the failure), while a
    // continue_on_error batch ran everything and each failure stands alone.
    const head = failed === -1
      ? `Output of 'batch' (${results.length}/${total} steps ran):`
      : continueOnError
        ? `ERROR: batch ran ${results.length} of ${total} steps and ${nFailed} FAILED - every step ran anyway, so read each result and fix only the failed ones.`
        : `ERROR: batch stopped at step ${failed + 1} of ${total}. Steps before it already ran, so check their results before you retry.`;
    return `${head}\n\n${blocks.join("\n\n")}`;
  }

  return {
    APP_NAME,
    SYS_MARKER,
    RESEND_MARKER,
    FEEDBACK,
    normalizeBatchSteps,
    formatBatch,
    toolCategory,
    buildSystemPrompt,
    compactTools,
    formatToolList,
    isReadOnlyStep,
    READONLY_TOOLS,
    toolsReminder,
    memoryNudge,
    TOOL_NOTES,
    PROMPT_PRESETS,
    findPreset,
    LOCAL_SERVER_ID: "zeroscript",
  };
})();
