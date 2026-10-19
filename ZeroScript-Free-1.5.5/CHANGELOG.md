# Changelog

All notable changes to ZeroScript Free are documented here.

## [Unreleased]

### Added
- **LM Arena (`lmarena.ai`) is supported next to Arena (`arena.ai`).** They are the same app - LMArena rebranded to Arena and moved domains in January 2026 - but old links, bookmarks and tabs still point at `lmarena.ai`, and there the fresh-chat route is `/c/new?mode=direct` instead of `/text/direct`. The Arena provider now recognises both route shapes (a fresh chat on either domain is a fresh chat, a real conversation keeps its id), and the extension is registered for `lmarena.ai` and `www.lmarena.ai` in the manifest and in the background worker's site list.
- **`test-fast.js`: 26 cases for the fast paths.** The Arena provider's "the turn is over" decision (the site's own Stop button is the signal, and the turn must end the moment it goes away instead of waiting out a 1.5s idle window), the lmarena.ai route handling, the core's DOM-change wait, and the parallel batch - run against a stub DOM with a controllable clock, so "the reply finished" is asserted in milliseconds instead of by reading the code.

### Changed
- **Nothing waits any more: the agent loop reacts to the page, not to a timer.** The response watcher sat in flat sleeps of 120-250ms between every read, so a reply that had already finished was noticed up to 250ms late, and every tool result inherited that dead time. It now waits for the next DOM CHANGE (a `MutationObserver` on the page, with the old timer kept only as a ceiling), the send path waits for a PAINT instead of a flat 200ms, the tab-switch back to the AI waits for a paint instead of 400ms, and the settle window after a finished reply is 250ms instead of 500ms (it is provider-tunable through `SETTLE_MS`). On a normal turn that is roughly a second and a half of dead time removed, and it was pure waiting on top of the model's own thinking.
- **Arena ends a turn the moment the site says so.** Arena shows its "Stop generation" button for the whole generation and swaps it back for the send button the instant the model stops, so the button going away IS the site's "the answer is finished" signal. The provider now believes it at once (after a 60ms beat that covers a re-render blinking the button out for a frame) instead of waiting out a 1.5s idle window on EVERY turn - and it still waits when the text is growing, so a mid-stream blink cannot end a turn early.
- **A write is ONE Studio round trip instead of two.** `script_write` used to read the old sources of every file it was about to overwrite in a separate call, purely to have something to snapshot for undo, and then write in a second call. The write snippet now reads the previous source of each file BEFORE it writes it and reports it back with the write, so the undo snapshot and the write travel together - and the place name comes out of the same report, so finding out where the history lives is free too. The capture is bounded (24 KB per chunk, the same bound the code payload already uses), so a big write cannot blow up the reply; a file past the bound is reported as "not snapshotted" instead of silently losing its undo point. The snapshot itself is unchanged and still holds the source from BEFORE the write.
- **A batch runs its read-only steps together.** A batch is mostly reads (`project_overview`, `script_read`, `luau_check`, `workspace grep`), and each one is a full round trip to the Bridge and back - sixteen of them in a row was sixteen round trips of pure waiting. Read-only steps now go out together, while anything that WRITES (`script_write`, `multi_edit`, `execute_luau`, `workspace push`, `playtest`) still runs one at a time, in order, so a write never races another write. The Bridge no longer serialises tool calls per server either (the JSON-RPC layer matches every reply to its own request), with a cap of four calls in flight per server and the restart path still exclusive.
- **A play-test stops at the first error in the Output.** A build that was already broken used to cost the whole window (5s by default, up to 30s) before the model heard about it. The Output is now read while the place runs, and the first hard error ends the test - so a broken build is reported in about a second. `"fail_fast": false` turns it off.
- **Reading the Output costs one Studio call instead of up to four.** `get_console_output` answers to several argument spellings and the Dev Kit tried them in turn; the spelling that worked is now remembered, so the second and later reads (every `project_overview` and every `playtest`) cost one round trip.
- **The play-test's error probe costs one call a second, not two, and backs off.** The probe reads only the Server Output (one Studio call), the gap between probes grows (1s, 1.5s, 2.25s, ... up to 4s), and the snapshot it already has is reused for the report when nothing happened since it was taken. A 30s play-test costs 7 probes, not 30 - and a broken build is still reported in about a second.

### Measured
The old and the new code were run side by side (the old files from the previous commit, the new ones from this branch) against the same fake Studio, with every Studio call modelled as a 120ms round trip:

| | before | after |
|---|---|---|
| Arena: "the reply is finished" is believed | 1500ms after the last token | **60ms** |
| Any provider: the finished reply is noticed | 760ms after the last token | **250ms** |
| After sending a message / switching back to the tab | 200ms / 400ms of fixed sleep | **32ms** (a paint) |
| `script_write` (2 files) | 2 Studio round trips | **1** |
| `project_overview` on a Studio build with one Output spelling | 3 round trips (every time) | **2** (the shape is remembered) |
| `playtest` of a broken build (10s window) | 10.5s before the model hears about it | **1.5s** |
| `playtest` of a clean build (5s window) | 5.5s | 5.5s (the probes happen inside the window) |
- **Prompt presets.** The panel's *Custom prompt* box has a preset picker above it. A preset is a ready-made brief that is loaded **into the box** - visible, editable, and sent through the same path as anything you type, never a hidden prompt layer - and the choice is remembered (`zsPromptPreset`); editing the text clears it, so the dropdown never claims a preset that is no longer there. The first one, **Systems architect (strict Luau)**, is a principal-engineer brief: `--!strict` typed Luau with `export type`s and `unknown` at every remote boundary, a server-authoritative three-file architecture (`ReplicatedStorage.[System]Config` ModuleScript, `ServerScriptService.[System]Server` Script, `StarterGui.[System]Client` LocalScript), the modern Luau rules (`task.*` only, properties before `Parent`, generalized iteration, no magic values), a Scale-only dark UI contract down to the exact palette and constraints, and the Dev Kit in pipeline order: `project_overview` → `luau_check` → one atomic `script_write` → `script_verify` → `studio_view` → `playtest` → `script_history`. It puts `script_verify` between writing and play-testing, which the brief it is based on left out. `node test-devkit.js` holds every preset to the same standard as the system prompt: each command name in the text must exist (checked against `local_tools.py`, `TOOL_NOTES` and the Studio MCP surface in `tests/fake_studio.py`), the order must be right, and the text must stay under 4,000 characters because it is typed into every session.
- **A worked example: `examples/shop-system/`.** A complete shop - `ShopConfig.lua` (pure data plus `export type`s), `ShopServer.lua` (creates its own remotes, validates every argument with `typeof`, rolling-window rate limit and cooldown, quantities clamped against `NaN`/`inf`, `maxOwned` enforced server-side, price recomputed from the config, `UpdateAsync` inside a three-attempt retry, `BindToClose` save) and `ShopClient.lua` (Scale-only dark GUI, tabs, `UIGridLayout` cards tagged through `CollectionService`, `UIScale` hover tweens, `B` to toggle) - 1,258 lines, `luau_check` clean. Sizes are Scale throughout, with three frames (root, toggle, close button) sized in Scale and clamped in pixels by a `UISizeConstraint` so no tap target shrinks below usability; the one pixel-sized `UDim2` is the nameplate's `BillboardGui`, which is world space, not screen space. Its README documents the security model, the UI contract, the exact Dev Kit calls that build it in Studio, and the one place it deviates from the brief and why (hover tweens a `UIScale`, not `Size`: inside a `UIGridLayout`, the layout wins). `pipeline_demo.py` runs all three through the real tools against `tests/fake_studio.py` and prints every step, labelling which steps are genuine (`luau_check`, `workspace`, `script_history`) and which have a simulated Studio on the other end (`project_overview`, `script_write`, `script_verify`, `studio_view`, `playtest`). Writing it found the four bugs below.

### Fixed
- **The linter no longer reads an `if` used as a value as an unclosed block.** `local b = if x then 1 else 2` is closed by its `else` and has no `end`; the block scanner treated it as a statement `if` that never closed, so `script_write` refused perfectly good modern Luau - and since it refuses the whole batch on one finding, that blocked entire systems. If-expressions are now recognised in every position (assignment, call argument, table field, `return`, `elseif`/`while`/`until` conditions, nested in either branch, as a function value, inside a loop), and one that never reaches its `else` gets an error message that spells out the shape.
- **A literal `else`-branch no longer swallows the next statement `if`.** The linter masks string literals to spaces before scanning blocks, so the "is the next thing another `if`?" lookahead after `else` looked *through* `else "done"` and found the `if` of a statement two lines later. The block stack desynced and the error surfaced 200 lines away as a bogus `extra 'end' - nothing is open here`. The lookahead now stops *at* a literal, because an if-expression's else-branch is a value: a string, comment or number there means the branch is not another `if`.
- **Script sizes are reported in one unit.** Luau's `#source` counts bytes while Python's `len()` counts code points, so a script containing an emoji or a `·` read as 6,557 from `script_write` and 6,156 from `workspace pull` - and `workspace status` with `compare_live`, comparing the two, reported freshly written scripts as `CHANGED` on a mirror pulled seconds earlier, which sends the model re-pulling (or re-writing) a mirror that is already exact. Every size is bytes now and says "bytes": the inventory tree, `project_overview`'s script list and memory line, the write report, history entries (older `index.json` files are still read), the pull report, `zsmanifest.json` and the live comparison.
- **`workspace grep` printed shared context lines once per neighbouring match.** Overlapping windows now merge with a `--` separator, the way `grep -C` does, and every match still carries its `>`: five adjacent hits with `context: 1` went from 15 lines of output to 5.
- **A case-sensitive plain-text grep could never match an uppercase needle.** The line was lowered but the needle only sometimes, so `{"regex": false, "ignore_case": false, "pattern": "GetService"}` found nothing at all. Both sides are lowered together now, or neither.
- `tests/fake_studio.py` reports byte lengths the way the real Luau snippets do (`#src`, `#old`) and labels them bytes, so the test double cannot be more forgiving than Studio about a script's size.

### Tests
- **`tests/test_examples.py` (15 cases) makes the worked example part of the suite.** It lints all three shop scripts on every run - so the next linter change is checked against 1,258 lines of real modern Luau, not only against hand-written snippets - and asserts the standards the example's README claims: `--!strict` first, no deprecated scheduler calls, nothing parented inside `Instance.new`, generalized iteration, every GUI `Size` in Scale (with the single world-space `BillboardGui` exception), the exact palette, hover tweening a `UIScale` and never a `Size`, the config being pure data, and the server owning every decision while the client never touches `DataStoreService`. Every assertion was mutation-checked: seven deliberate rule violations in a throwaway copy, seven caught, none missed.
- 155 Python cases (`tests/test_local_tools.py` 115, `tests/test_bridge.py` 25, `tests/test_examples.py` 15) and 242 extension cases (`test-devkit.js` 108, `test-batch.js` 43, `test-parser.js` 37, `test-popup.js` 20, `test-chatgpt.js` 8, `test-fast.js` 26). New coverage: if-expressions in twelve positions plus the traps that broke them, multibyte size agreement across write/pull/status/history, grep case sensitivity and merged context windows, and every preset assertion above.

## [1.6.0] - 2026-10-13

### Added
- **ZeroScript Dev Kit: eight commands the Bridge implements itself.** Studio's MCP tools are primitives, so a real task cost dozens of turns - search the tree, open each script, edit it in pieces, read it back to check. The Bridge now serves eight commands of its own beside them: `project_overview`, `script_write`, `script_verify`, `luau_check`, `script_history`, `workspace`, `studio_view` and `playtest`. They are advertised in the tool list as a server called `zeroscript`, they are always available (they do not depend on a Studio being open), and they run through the same WebSocket, Stop button, timeouts, chips and feedback format as any other command, so nothing on the extension side had to learn a second protocol. Each one does in a single round trip what the primitives take many to do, and it keeps bulk data - whole script bodies, instance trees, Output - on the user's machine, so the model gets a summary instead of a dump.
- **`script_write`: whole scripts, several at once, verified.** Up to 40 files in one call, each a complete source with its class name; missing folders and the scripts themselves are created. The code goes to Studio over one `execute_luau` call as a byte-exact payload (base64, or a long bracket whose `[==[` level is computed from the source so no terminator can appear inside it), split into chunks of at most 24 KB, and every file is then read back and compared against what was sent before the call reports success. Files that fail there are retried through `multi_edit`; anything still failing is reported per file with the reason. A twelve-script system is one turn instead of twelve, and a script no longer has to be quoted back at the model before it can be changed.
- **`luau_check`: syntax errors are caught before they reach your place.** The Bridge now carries its own Luau linter - block and delimiter balance (`if/then/end`, `function/end`, `do`, `repeat/until`, strings, comments, long brackets), curly quotes in code position, Python-isms (`elif`, `True`, `f"..."`), `==` where `=` was meant, and more - reporting the file, the line, the offending text and a plain-English fix. `script_write` runs it over every file first: if any file fails, NOTHING is written and the report comes back with line numbers. It is also a command of its own, so a snippet can be checked without opening Studio. The six Luau payloads the Dev Kit itself generates are checked by the same linter in the tests, so the toolkit cannot fail its own gate.
- **`script_verify`: does it actually compile?** Compiles every script in the place (or only the ones named) inside Studio's real Luau VM with `loadstring`, which catches what a linter cannot, and reports `line N:` errors per script. Scripts it could not load are listed as such instead of being counted as passing.
- **`project_overview`: the whole place in one call.** The instance tree to the depth asked for, every script with its class, size and first line, recent Output from the Server and Client DataModels, the project memory, and whether the local mirror is stale. It replaces the `search_game_tree` + `inspect_instance` + `script_read`-per-script chain that used to open a session.
- **`workspace`: a local mirror of your place.** `pull` writes every script to `<bridge folder>/.zeroscript/workspace/<place>/Service/Name.lua` plus a `zsmanifest.json` (class, size, hash, when pulled, when pushed). `grep` then searches the entire project locally with **no Studio call at all** - pulling first if the mirror is empty - and reports matches as `file:line: text`. `status` says which local edits are not pushed, how many undo points exist, and what has changed in Studio since the pull (changed / not mirrored / gone). `push` writes local edits back, linted first. The mirror also means the project's scripts can be opened in a real editor.
- **`script_history`: undo for AI edits.** Every write snapshots the old content to `.zeroscript/history/<place>/<script>/<timestamp>.lua` (40 kept per script, indexed in `index.json`). `list`, `diff` (a unified diff of a snapshot against the live source) and `revert` (restores it in Studio, then snapshots again, so a revert is itself revertable) run on those snapshots. A newly created file gets an empty `auto:before-create` snapshot, so undoing a creation is one call too. This is the answer to "that change made it worse".
- **`playtest`: run it and read the Output.** Starts play, lets it run for the seconds asked (1-30), optionally sends keystrokes and mouse actions first, captures Server and Client Output, flags suspicious lines, and stops. The "does it actually run" step is one call instead of five, and the AI no longer has to guess whether the place works.
- **`studio_view`: show the user what you built.** Selects, frames or clears the selection in their editor, so a change is visible in Studio rather than only described in chat.
- **Tests for the Dev Kit.** `tests/test_local_tools.py` (103 cases) runs against `tests/fake_studio.py`, an in-memory place that speaks both Studio's MCP tool shapes and the Dev Kit's Luau wire protocol: linting, path safety, the write pipeline (chunking, halving when Studio reports an over-size reply, the `multi_edit` fallback, read-back verification), snapshots/diff/revert, the mirror, Studio being offline, and output shaping. `tests/test_bridge.py` gained 11 cases that drive the REAL Bridge over its WebSocket against `tests/fake_studio_mcp.py`, a fake Studio MCP server that persists its place to disk - so the extension -> Bridge -> Dev Kit -> Studio path is covered end to end without Roblox. On the extension side `node test-devkit.js` (77 cases) covers the command reference the model reads, the prompt that teaches the workflow, and the names that must not drift from `local_tools.py`, and `node test-popup.js` (20 cases) runs the popup against a stub DOM to keep the connectivity dot honest: the always-alive Dev Kit must never make it read green while Roblox Studio is closed. The fake Studio models what Studio really does (it reports scripts only for LuaSourceContainers, and skips anything else with its class), so a missing filter in the generated Luau cannot hide behind a forgiving test double.
- **Batch commands.** The AI can send one `batch` command that runs up to 16 steps in order and returns every result together. Each step goes through the same checks, timeouts and Stop handling as a normal command. By default the run stops at the first failed step and the later steps are reported as not run; with `"continue_on_error": true` every step runs anyway and each failure stands alone, which is what independent reads and edits want. Every reply is a full model turn, so this cuts the number of turns for independent work. Image tools and `execute_luau` cannot go inside a batch.
- **Headers box for remote MCP servers.** The MCP menu has a Headers field for a remote server (JSON object of strings, `${VAR}` expanded from the environment at connect time).
- **Remote MCP servers.** The MCP menu accepts an `https://` address as well as a start command. The Bridge connects to the server over Streamable HTTP (MCP spec 2025-03-26 and later) and routes tool calls to it like any local server. `config.json` takes `url` and `headers`; `${VAR}` in a header value is expanded from the environment. A server that forgets its session is reconnected once.
- `ZS_BRIDGE_CONFIG` and `ZS_BRIDGE_LOGS_DIR` environment variables redirect the Bridge's config file and logs folder. `ZS_BRIDGE_ALLOW_ORIGINS` admits extra browser origins (see Security).
- Automated tests in `tests/` (`python -m unittest discover -s tests`). They run the real Bridge against fake MCP servers, and against a fake Roblox Studio for the Dev Kit.

### Changed
- **The system prompt now teaches a developer's workflow.** A nine-step DEV WORKFLOW section (orient -> find code -> write -> surgical edits -> one-off Luau -> verify -> play-test -> undo -> show) says which command to reach for and in what order, plus the habits that keep a session short: write a whole system in one `script_write` instead of a file per turn, never re-read a script you just wrote, verify before reporting success.
- **The command reference is typed into the session at startup when it fits,** so the model's first turn is work instead of `list_commands`. `list_commands` still exists and covers the Dev Kit by default; it and the startup reference are rendered by the same function, so they cannot disagree. `list_mcp_servers` shows the Dev Kit as its own always-available server.
- **Per-command timeouts.** A batch of writes or a play-test is allowed longer than a read: `script_write` gets 150s, `playtest` 120s, `project_overview` and `workspace` 90s, instead of one flat timeout for every command.
- **The status panel separates the Dev Kit from Studio.** The Bridge reports Studio's tool count and the Dev Kit's separately, and the connectivity dot ignores the Dev Kit - a Bridge with no Studio open still shows yellow instead of pretending everything is ready.
- Asking for the tool list no longer blocks on dead or unresponsive servers. The reply waits at most 1.5 seconds for a fresh catalogue; a slow server's last known tools are returned, and the refresh finishes in the background. Restarts run in the background. With a silent server, `list_tools` went from 20.0s to 1.5s in our test.
- The Windows Studio process check is cached for 10 seconds for the connect and tool-list replies, so each reply no longer starts a `tasklist` process. The background watchers still check live.
- Studio status is cached for 30 seconds, so connecting to the Bridge no longer waits for a fresh Studio probe. With a hung Studio, the worst-case connect and tool-list wait dropped from about 8s to about 0s in our test.
- `start.bat` installs `websockets` without a version pin, so a future major release could break the Bridge.

### Security
- **The Bridge only answers ZeroScript.** It used to accept WebSocket connections from any web page, so a page could list tools or add a server that runs an arbitrary program. Handshakes whose Origin is not a browser extension are now refused with close code 1008. Clients that send no Origin header (scripts running as you) are still accepted. The allowlist assumes Chrome and Edge send `chrome-extension://<id>` for the extension's WebSocket; this has not been checked in a real browser yet.

## [1.5.5] - 2026-09-10

### Fixed
- **DeepSeek: the agent starts again on DeepSeek's new unified model.**
  DeepSeek merged Instant, Expert and Vision into a single model and removed
  the model picker from the chat box. ZeroScript waited for one of those tabs
  to be selected before starting, so "Start Roblox agent" stopped with
  "DeepSeek mode not ready". A chat box with no model picker is now recognised
  as the unified model: ZeroScript switches Search off as before, leaves
  DeepThink on, and starts.
- **DeepSeek: screenshots work on every chat.** Images used to need the Vision
  tab, which no longer exists, so `screen_capture` was refused. The unified
  model reads images, and ZeroScript now sends them - one capture or several
  in a row. Older conversations still marked Instant or Expert stay text-only.

## [1.5.4] - 2026-09-06

### Fixed
- **ChatGPT: the ZeroScript bar is back where it belongs, above the composer.**
  ChatGPT redesigned its input box and renamed the layout slots it is built
  from: the full-width row across the top used to be called `header` and is now
  called `eyebrow`. ZeroScript still asked for `header`, a name that no longer
  exists, so the browser invented a place for the bar instead - it landed in a
  stray strip at the bottom right of the composer, and the text field itself was
  squeezed to zero width in the process. The bar now claims the correct row, and
  it also reads the layout live rather than trusting a fixed name, so the next
  time ChatGPT renames its slots the bar will follow instead of breaking.

### Changed
- **ChatGPT: replies are read straight from the page again.** The same redesign
  replaced the code-block editor that used to render each line separately and
  cut long lines off around 2000 characters - the cause of the truncated
  commands fixed in 1.5.1. Code blocks are now plain text with real line breaks,
  and a 400-line block reads back whole. The workaround stays in place for
  anyone still on the old interface.

## [1.5.3] - 2026-08-22

### Changed
- **Kimi moved to kimi.ai.** Kimi's old address, `kimi.com`, now asks for a
  Chinese phone number to sign in, which locked most people out. Kimi runs on
  `kimi.ai` from now on - the extension only activates there. The page itself is
  unchanged, so nothing about using Kimi with ZeroScript is different: open
  https://www.kimi.ai, the bar appears above the input box as before. If you had
  Kimi tabs open on the old address, reopen them on the new one.
- **DeepSeek: the Instant model is now allowed to run the agent.** Starting a
  session forced the Expert tab and, worse, the readiness gate only ever accepted
  Expert or Vision - so picking Instant left "Start Roblox agent" spinning
  forever with no explanation. Instant is now respected like Vision: pick it
  before starting and the session runs on it. It is much faster than Expert, at
  the cost of the reasoning pass. Images stay disabled on Instant exactly as they
  are on Expert - the Vision tab is still the only one that can see screenshots,
  so `screen_capture` is not offered to the model on the other two.

### Fixed
- **DeepSeek: a reply written in DeepSeek's own tool-call markup no longer kills
  the turn.** DeepSeek sometimes answers with its internal DSML invoke tags
  instead of a ZeroScript command. That format carries none of the markers
  ZeroScript looks for, so nothing recognised it as a command attempt: the tool
  never ran, the raw tags were left on screen, and the agent silently stopped
  with the user waiting on a dead turn. It is now detected, the markup is hidden
  behind a tool chip like any other command, and DeepSeek is told the format is
  unreadable so it rewrites the call properly. The chip shows the usual spinner
  while the model is still writing, then settles red as "wrong format".
- **ChatGPT: the ZeroScript bar no longer collides with the composer's rounded
  corners.** The composer card is rounded by 28px and the bar sits flush against
  its top edge, so the Discord button's corner fell outside the rounded shape and
  was sliced off by the card. Both ends of the bar are inset to clear the curve.

## [1.5.2] - 2026-08-14

### Added
- **ChatGPT: the operating instructions are now re-stated periodically.** ChatGPT
  summarises its own context mid-session, and the part it drops first is the
  *mechanism* - that an extension reads its replies and really runs them. It
  would then answer "I can't invoke those commands in this session" while the
  extension sat there, ready. The full instructions are now re-sent
  automatically, riding along on a tool result so they cost no extra message and
  stay hidden from you (they appear as a "Reminder" chip). A tool result is
  never shortened to make room: if the pair would not fit, the reminder simply
  waits for the next one. ChatGPT only - no other provider needs it.
- **ChatGPT: an image you send is now treated as reference material.** Unless you
  explicitly ask for a picture, ChatGPT used to answer a screenshot or a mockup
  by *generating a new image* instead of doing the work it was meant to
  illustrate - and its image tool cannot reach your place anyway.

### Fixed
- **ChatGPT: you can chat normally again without starting an agent.** On a blank
  ChatGPT tab the extension refused to let a message send until you clicked
  "Start Roblox agent". Every other provider only suggests it; ChatGPT was the
  odd one out.
- **A finished command is no longer stranded as "not run".** After a long reply
  (seen on Qwen writing for 400s and more) the loop could give up while the model
  was still going; eight seconds later the completed command was written off for
  good. That window is now three minutes, so the command actually runs.
- **A clear message when ZeroScript is updated while a tab is open.** Chrome
  updates extensions underneath open tabs, which leaves the page running a
  version that no longer exists. ZeroScript reported this as "the bridge stopped
  on your PC - run start.bat", sending you to fix something that was never
  broken. It now says plainly that the page needs reloading, and offers a Reload
  button - your bridge and Studio are untouched.
- **The AI no longer claims your bridge is offline without checking.** After one
  momentary outage it would keep repeating "Roblox is offline" from memory, even
  once everything was back. It must now actually run a command before saying so.

## [1.5.1] - 2026-08-13

### Added
- **ChatGPT support (chatgpt.com).** ZeroScript now runs on ChatGPT as an
  eighth provider. Image input is deliberately disabled there: ChatGPT's free
  tier caps files/images on a separate quota from messages, so vision would
  work only part of the day. Reasoning mode ("Analyser") and the model picker
  are left entirely to you.

### Fixed
- **Meta AI: fixed large commands failing with "bad JSON".** Meta renders a
  ```json block as an interactive viewer whose default *Tree* view does not
  merely decorate the JSON - it **abridges** it: a large array or object is
  replaced by a summary placeholder. A 19103-character `multi_edit` was present
  in the page as 223 characters ending in `"edits":[1 item]`, so ZeroScript sent
  the parser a truncated object and the command came back as a parse error every
  time. This is also why the tool chip's token counter climbed while the reply
  streamed and then **collapsed to about 44 tokens** the moment the block
  finished rendering - the counter was faithfully reporting what could be read.
  Command blocks are now switched to the viewer's *Raw* tab, which holds the
  verbatim source; the 19103-character payload is read whole.
- **Clearer diagnosis when Roblox refuses to parse Luau.** "Failed to parse
  command code" is Studio's generic parse rejection, but ZeroScript always
  answered it with "your code block was empty or the marker was wrong". When a
  full code string *had* been sent, that advice pointed the model at a problem
  that did not exist, so it re-sent the same payload and failed again. The hint
  now only mentions the `###LUA###` markers when the code really was empty, and
  otherwise reports how many characters were sent and names the real causes -
  invalid syntax, or code too large/complex for the parser. (Measured live: a
  `return 1+1+1+…` chain ran at 1006 characters and was rejected at 2006.)
- **ChatGPT: fixed most tool calls failing outright.** ChatGPT renders code
  blocks with CodeMirror, which puts one element per line and **no newline
  characters at all** in the page. Reading a reply the usual way therefore
  returned the whole script glued onto a single line, so a perfectly valid
  command came back as "Failed to parse command code / your code block was
  empty", and the calls that did run reported every Luau error on line 1.
  Replies are now read with the line structure preserved.
- **ChatGPT: fixed long commands being executed truncated.** Beyond roughly
  2000-4000 characters, CodeMirror only renders *part* of a long line and the
  rendered text then stays frozen while the model keeps writing - the tool
  chip's token counter would climb, drop back to about 500 tokens, freeze
  there, and the command would run cut off. Measured live: a 21273-character
  command of which the page exposed 4049. ZeroScript now reads the editor's
  real document instead of the rendered page, through a new MAIN-world tap
  (`providers/chatgpt-cm.js`), the same approach already used for Qwen's
  Monaco editor. A 5.3k-token `multi_edit` now applies whole.
- **ChatGPT: fixed the raw command staying visible.** When the model writes a
  command without wrapping it in a code fence, ChatGPT splits it into dozens of
  sibling paragraphs (68 of them for a 208-line script) and only the first one
  carried the marker, so the rest of the script stayed on screen. The whole
  marker-to-marker range is now hidden, including while it streams.
- **Fixed the agent dying silently when the model called a tool the
  function-calling way.** A reply like
  `{"toolName": "get_studio_state", "studio_id": "…"}` names a real tool but
  uses the wrong key, so nothing recognised it as a command: the turn was
  finalised as a plain-text answer and the loop simply ended, leaving the agent
  looking frozen (seen on ChatGPT in a long session). ZeroScript now spots a
  known tool named under `toolName` / `tool` / `name` / `function` / `action`
  and asks the model to rewrite it with the proper envelope, exactly as it
  already did for a missing `###LUA###` opener or bare parameters. Prose that
  merely mentions a tool name is not affected - the check requires a tool that
  is really in the catalogue.
- **Fixed the "Agent is working…" cover hanging past the composer on the first
  send.** Injecting the system prompt grows the composer, the page gains a
  scrollbar and the content column narrows, so the composer slides sideways -
  and a site that animates that move updates its layout after the cover has
  already been placed, leaving it a frame behind (28px past the card's right
  edge on ChatGPT). The cover is now clamped to the composer card, so a stale
  measurement can never be seen. Only the first send was affected, because the
  composer stops moving once it is docked at the bottom.

## [1.5.0] - 2026-07-30

### Fixed
- **Backgrounding the AI tab no longer strands a pending command as a grey
  "not run".** `waitForResponse` now parks entirely while the tab is hidden and
  shifts every internal deadline (inactivity timeout, warm-up, text-stability,
  etc.) forward by the parked duration, instead of letting them keep ticking
  off-screen. `waitVisible` switched from polling to listening for
  `visibilitychange` - Chrome clamps chained background timers to one tick per
  minute after 5 minutes hidden, which used to delay the resume by up to a
  minute. The bar now shows a **Paused** state while parked, and a genuinely
  empty reply from the site now shows a banner instead of ending the loop
  silently.
- **Gemini: fixed the page freezing (nothing clickable) on a large tool
  result.** Gemini's composer inserts text line by line, synchronously, on the
  main thread - a 2599-line `http_get` result froze the page for about a
  minute. Outgoing text is now capped (120k chars / 1200 lines, head and tail
  kept) and the insert yields to the browser every 120 lines.
- **Gemini: fixed the system prompt occasionally never leaving the composer on
  Start.** The wedged-stop-button detector latches for 2 seconds from the
  first time it sees a stop button, so the single recovery attempt at boot -
  the very first sighting - was refused by its own guard. It now retries
  across that window and retypes as a last resort.
- **Kimi: fixed the model picker opening and closing in a loop.** Kimi's K3
  update removed the model (K2.6) the default-model routine used to select,
  so it kept hunting for a row that no longer exists. It now only acts when
  the current model is **K3 Swarm** (matched by name, any UI language) and
  gives up after a few tries instead of looping. The native-agent warning
  guard was equally broken by the same update and now reads the model label
  at its new location.
- **Degraded mode (Roblox Studio closed, running on an addon server only)
  starts much faster.** The tool catalogue request blocks until timeout when
  Roblox is down, and the boot sequence called it three times in a row. Added
  a 30s cache on the catalogue and cut the request timeout from 25s to 10s.

## [1.4.9] - 2026-07-24

### Added
- **Popup: new Settings button.** Opens the same Switch AI / support panel
  as the in-page bar, without needing an already-started conversation. The
  footer text no longer singles out chat.deepseek.com - it now points to
  "a supported AI" since seven providers are supported.
- **Bridge: auto-recovers its own port on relaunch.** Relaunching `start.bat`
  while a previous Bridge was still holding port 17613 (window closed with
  the X, a crash, a double launch) used to crash with a cryptic, sometimes
  localized `OSError [WinError 10048]`. The Bridge now detects and kills a
  leftover Bridge process it can positively identify (by command line, never
  by process name alone) before binding, and falls through to a clear,
  actionable message - with the exact `netstat`/`taskkill` commands and the
  `ZS_BRIDGE_PORT` override - if the port is held by something else.

### Fixed
- **The agent could parse/execute commands while its AI tab was backgrounded
  or the window minimized.** Background tabs throttle rendering and timers,
  which made DOM reads unreliable and could send duplicate feedback or run a
  tool blind (observed live: GLM kept running `execute_luau` while minimized).
  The agent loop, the tool-dispatch step, and the auto-resume watchdog now
  all gate on `document.visibilityState` and park - with no time limit -
  until the AI tab is the foreground tab again, then resume exactly where
  they left off. Working with Roblox Studio focused while the AI tab stays
  the active tab in its own window is unaffected; this only pauses execution
  while that tab is truly backgrounded or its window minimized.

## [1.4.8] - 2026-07-22

### Added
- **macOS and Linux support for the Bridge.** A new self-contained
  `MacOS_Start.command` launcher (double-click in Finder - no Terminal
  knowledge needed) finds Python 3.9+, installs `websockets` if missing,
  frees a previous Bridge still holding the port, and runs `bridge.py`,
  mirroring what `start.bat` already does on Windows. `launch_studio_mcp.py`
  now also locates Roblox Studio's MCP binary inside the macOS app bundle
  (`RobloxStudio.app/Contents/MacOS/StudioMCP`), with a `ZS_STUDIO_MCP_PATH`
  override for non-standard installs.
- **DeepSeek: outgoing messages are now truncated to fit its input limit.**
  DeepSeek's composer silently refuses to send past 163840 characters
  (validated live), which could wedge the agent in the input box after a
  large tool result (a big `http_get` / `get_page_text` / Luau dump). Long
  results are now truncated to a safe margin below that limit, keeping both
  the start and the end of the content, the same approach already used for
  Qwen and Arena.

## [1.4.7] - 2026-07-21

### Fixed
- **Qwen: a tool could show a green "done" check while it never ran and returned
  no result** (seen rarely with repeated `multi_edit` / `execute_luau` calls, with
  no Stop or regenerate involved). Qwen virtualizes its message list, so the
  off-DOM "already executed" record was keyed on the positional turn index, and
  two turns that shared the same 60-character command prefix could collide on the
  same index. That false positive made the auto-resume watchdog skip the fresh
  command (so it never ran, no result was injected) while the chip was still
  painted a green check. The dedupe now keys on Qwen's stable per-turn id
  (`chat-response-message-<uuid>`, exposed as `itemKey`) instead of the index, so
  the collision cannot happen.
- **Qwen: the ZeroScript bar covered the "Expand more models" submenu.** That
  fly-out is a separate body-portalled `.ant-dropdown` at a low z-index, not the
  main model dropdown, so the bar drew on top of it. Raised just that dropdown
  above the bar (scoped so other Ant menus and tooltips are untouched).

### Added
- **Per-model image support on Qwen.** Qwen offers both multimodal and text-only
  models, switchable mid-conversation, and image input only works on the
  multimodal ones. `screen_capture` and image input are now enabled only on a
  vision-capable model (Qwen3.7-Plus, Qwen3.6-Plus, Qwen3.6-27B, Qwen3.8-Max-Preview)
  and correctly withheld on a text-only one (Qwen3.7-Max, Qwen3.6-Max-Preview),
  read from the selected model and updated when you switch models.
- **Image support on DeepSeek's Vision model.** DeepSeek forces its Expert model
  for the agent, but if you choose the Vision tab that choice is now respected and
  `screen_capture` plus image input are enabled for it. Selecting Vision is
  detected reliably, including after switching conversations. Image attachment was
  also fixed: it used to stage the same image multiple times and never send,
  because the upload went through a paste that only made a local preview and never
  uploaded the file. It now uses DeepSeek's real file upload and sends once the
  upload completes.

## [1.4.6] - 2026-07-19

### Fixed
- **Kimi's login and "priority queue" popups were covered by the ZeroScript
  bar**: both render as full-screen fixed masks (`.login-modal-mask` and
  `.modal-mask`) rather than a standard `[role="dialog"]`, so the generic
  overlay probe used by other providers never caught them. The anchored bar
  (a full-width fixed element hugging the composer) and the "unstable"
  warning pill sat on top of the mask and could intercept clicks meant for
  its buttons (e.g. "Continue with Google"). Added a Kimi-specific
  `overlayBlocking()` that detects both mask classes by real visibility; the
  core already hides the whole bar while it reports true, and restores it the
  instant the mask clears.

### Added
- **Kimi now defaults fresh chats to K2.6**: Kimi lands new chats on K3,
  which is flagged unstable here and easy to miss switching away from. A
  brand new or emptied chat now picks K2.6 automatically, once; a deliberate
  manual switch to K3 on that same chat is left alone.

## [1.4.5] - 2026-07-18

### Fixed
- **DeepSeek re-executed old tool commands when scrolling up in a long
  conversation**: DeepSeek virtualizes its message list, so scrolling up makes
  an OLD command turn the last *rendered* assistant turn - its injected result
  sits below the fold (unrendered), the in-memory "already executed" record is
  empty after a page reload, and the node change makes generation detection
  flicker true, refreshing the auto-resume watchdog's freshness clock. The
  watchdog then re-fired the historical tool. Three-layer fix (validated live):
  - The off-DOM executed/halted dedupe maps now key on a virtualization-stable
    per-turn id (`itemKey`, DeepSeek's `data-virtual-list-item-key`) instead of
    the positional assistant index, which collides across scroll windows.
  - The watchdog skips any command turn whose stable id is below the session's
    high-water mark (`A.maxTurnId`) - a scrolled-back old turn can never resume,
    even right after a reload (`resume.skipOld` in the diag ring).
  - The watchdog also skips a command turn whose injected result is rendered
    right below it (settled history), a provider-generic guard.
- **Gemini stranded a tool result in the composer ("Message could not be
  sent")**: after a generation ends, Gemini's action button can stay WEDGED on
  the stop icon instead of reverting to the send arrow. The loop's generation
  *detection* already tolerates this (WEDGE_MS), so the tool ran, but the *send*
  waited for an `arrow_upward` button that never appeared - four retries failed
  and the injected result sat unsent in the composer. `typeAndSend` now resets a
  frozen stop button (clicking it, guarded by the same not-actually-generating
  check) so the send button reappears, then sends (validated live). The native
  stop-click hook now ignores non-trusted (programmatic) clicks, so this
  un-wedge click is never mistaken for the user halting the agent - otherwise
  the next legitimate command was wrongly marked "stopped".

## [1.4.4] - 2026-07-16

### Fixed
- **Qwen fired tool commands mid-stream ("Bad JSON" while Qwen was still
  writing)**: Qwen's frontend update (fe 0.2.73) now emits `status:"finished"`
  in its SSE stream ~12s before the stream actually closes. The network tap
  treated that as the turn's end, so a still-incomplete command (e.g. an
  unclosed `###LUA###` block) was extracted and sent, and the loop's premature
  "unclosed" feedback was injected while the model kept writing. Fixed by no
  longer treating `status:"finished"` as done, and by having generation
  detection check the DOM stop button first (validated live: it now tracks the
  real stream end closely, unlike its old ~6s lag).

### Changed
- **Removed the "⚠ unstable" badge on Qwen's Auto/Think modes**: those modes
  used to make Qwen claim a tool "does not exist" without even trying it. The
  extension never force-switches Qwen's mode, so Auto (Qwen's own default) is
  left untouched.

## [1.4.3] - 2026-07-15

Adds a seventh AI provider (Meta AI) and fixes a Qwen tool-turn regression, plus
further Studio-port recovery hardening and a friendlier system prompt.

### Added
- **Meta AI (www.meta.ai) as a provider**: full ZeroScript support on Meta AI -
  new `providers/meta.js`, manifest content script + host permissions, and the
  provider switcher entry. Handles Meta's React DOM: reasoning ("Réflexion")
  chain-of-thought is excluded from the read text, the interactive JSON viewer
  and collapsible code blocks are masked so a streamed command never flashes, and
  the composer card is fully covered while typing. Meta AI accepts very large
  prompts, so no Qwen-style send cap is needed.

### Fixed
- **Qwen tool result took ~30s to inject on every tool turn**: Qwen dropped the
  assistant turn's own `id`, so `lastAssistantId()` returned null and the core
  fell back to the virtualized flat count, waiting the full ~30s NO_TURN_GRACE
  each turn. It now reads the stable `chat-response-message-<uuid>` descendant
  (with the old id kept as a fallback).
- **Qwen refused oversized messages**: a large tool result past Qwen's 131072
  character composer cap silently wedged the loop in the input box. Outgoing text
  is now truncated to a safe margin, keeping the head and tail and marking the gap
  so the model does not re-run the command.

### Changed
- **Friendlier, less restrictive system prompt**: the "do not use native tools"
  wording is reframed as a technical note (the site's own sandbox cannot reach the
  user's Studio) rather than a hard prohibition, with an explicit "you can act
  directly in the user's project" section. Reduces provider refusals.
- **Studio-port recovery hardening**: PID-based reclaim of leftover `StudioMCP`
  zombies and clearer, de-duplicated action banners on top of the 1.4.2 port
  hijack recovery.

## [1.4.2] - 2026-07-13

Follow-up robustness fixes for the Studio-connection failures the 1.4.1 work
did not cover: a rare "0 tools that survives every restart" deadlock, and a
third-party app silently hijacking Studio's MCP port.

### Fixed
- **A third-party app (e.g. ropilot) hijacking Studio's MCP port**: whichever
  program binds Studio's MCP port (13469) FIRST wins it, and if that is not
  Studio, `StudioMCP.exe` connects to the wrong host - the handshake succeeds
  but no tools ever appear. A PC reboot never helps because the offending app
  restarts with Windows and can grab the port before Studio again. The existing
  one-shot port check at boot could miss it. The bridge now detects the hijack
  from an unmistakable, timing-independent signal - `StudioMCP.exe` reporting it
  cannot parse the host's messages on that port - then kills the offending
  process (by port owner, with a fallback that kills the known squatter by
  name), restarts the proxy, and tells the user which app to uninstall or remove
  from Windows startup so it stops coming back. It never stays silent: if it
  cannot identify or kill the squatter it prints how to find it by hand.
- **`_port_owner` was IPv4-only**: the internal port-owner probe ran
  `netstat -p TCP`, so a squatter listening on IPv6 loopback was invisible to
  it; it now scans TCP and TCPv6.
- **A missing custom-MCP command (e.g. `uvx` not installed) looked like an
  endless silent restart loop**: when a configured server's command could not
  be found on PATH, the process never started, so there was no exit code and no
  stderr, and the crash-loop banner printed "the server printed no error output
  before dying". The bridge now catches the launch failure and names the real
  cause ("command not found: 'uvx' ...") both on the first attempt and in the
  crash-loop banner, while auto-restart keeps retrying in case the dependency
  is installed later.

### Changed
- After killing a port squatter, the "toggle Studio's MCP server OFF/ON"
  instruction now prints IMMEDIATELY (right after the kill) instead of only
  after the ~48s server-launch grace loop - so the user acts within seconds
  instead of staring at a seemingly-idle terminal for a minute. Toggling early
  also lets the grace loop pick up the tools and go green right away.
- **0 tools that no restart could fix**: if a `StudioMCP.exe` from a crashed
  session kept listening on Studio's MCP port (13469), reopening Studio made
  its MCP plugin do its one-shot registration against that *zombie* process.
  Because a Studio window was now running, both existing cleanups skipped it
  (the orphan-killer only acts when no Studio runs; the port check treats any
  Roblox-path owner as legitimate), so our fresh proxy could never own the
  port - 0 tools forever, unfixable by restarting Studio or the bridge in any
  order. The bridge now identifies the port owner by process ID: a
  `StudioMCP.exe` holding the port that this bridge did not launch (outside our
  own process tree) is a leftover by definition, so it is killed and the proxy
  restarted - at boot and again in the live watcher if the catalogue stays
  empty with Studio open. It then tells the user the one action that finishes
  recovery: open Assistant Settings > MCP Servers so Studio re-registers. If
  the process tree can't be read, nothing is killed (a healthy connection is
  never put at risk).
- The extension now tells non-technical users to "Run start.bat" instead of
  "Run python bridge.py" / "Run the ZeroScript bridge" in the offline panel,
  popup, and startup banner, matching the one-click launcher the README ships.

## [1.4.1] - 2026-07-11

Robustness release focused on the Roblox Studio connection lifecycle. Every
fix below was reproduced and validated live against a real Studio + Blender
setup, including the Roblox-side bugs reported on the devforum (StudioMCP
stale-pipe disconnects, MCP toggle turning off after a Studio update).

### Fixed
- **Phantom "Studio connected" state**: leftover `StudioMCP.exe` processes
  from a previous session or a Studio crash kept answering the bridge as if a
  Studio were attached, so the terminal and the extension showed green with
  Studio fully closed. The bridge now kills orphaned `StudioMCP.exe` at boot
  (only when no real Studio window exists, so a live connection can never be
  hit), and the boot banner re-confirms a positive probe before announcing a
  connection.
- **Status dot stuck green with Studio closed**: when StudioMCP advertised an
  empty tool catalogue (Studio closed at launch), the connectivity probe
  returned "unknown" instead of "disconnected", and the extension's
  don't-degrade-on-unknown rule kept the dot green forever. An alive Roblox
  proxy with an empty catalogue is now an authoritative "not connected".
- **Studio opened after the bridge was never detected** (yellow until a full
  bridge restart): two combined causes. (1) Nothing ever re-asked for the
  tool catalogue once the launch-time retry window expired - the watcher now
  re-polls `tools/list` while the catalogue is empty, so a late-attaching
  Studio is picked up within seconds. (2) Studio's MCP plugin registers with
  the MCP channel exactly ONCE (late in Studio's boot, or when the Assistant
  Settings > MCP Servers panel is opened/toggled) and never retries; the
  bridge's own recovery restarts could kill the MCP listener at that exact
  moment, permanently orphaning the plugin. The bridge no longer restarts the
  Roblox proxy while a Studio window is running, and both the terminal and
  the extension now say the one thing that actually fixes an orphaned
  plugin: open Assistant Settings > MCP Servers in Studio (validated three
  times live; a proxy-side restart provably cannot repair it).
- **Watcher crash silently disabling all Studio monitoring**: an unbound
  variable in the place-churn detector could kill the background watcher
  right after a reconnect, silently stopping every status update until the
  next bridge restart. Fixed, and both watchers are now supervised: a crash
  is logged in red and the watcher restarts itself in 5 seconds.
- Boot/connection messages no longer blame the merged multi-server tool count
  on Roblox ("49 tools loaded but NO Roblox Studio connected" when 22 of
  those were Blender's): every Roblox-specific message now uses the
  Roblox-only count.

### Added
- **Fast startup with addon servers**: MCP servers now launch in parallel and
  the extension-facing socket opens immediately, so a slow or absent Roblox
  Studio no longer delays Blender (or any addon) by up to a minute. The
  Roblox diagnostic continues in the background and the bridge pushes status
  updates to already-connected extensions as servers come up - previously an
  extension that connected early could keep a stale "addon offline" snapshot
  forever (greyed Start button instead of the orange degraded start).
- **Self-healing for Roblox's own disconnect bugs**: sustained loss of the
  Studio connection (stale named-pipe state, periodic silent disconnects)
  now auto-restarts the Roblox proxy - but only when no Studio window is
  running, where it is safe and effective.
- **Studio-update detection**: when a disconnect coincides with a new Studio
  version folder appearing, the terminal says Studio likely turned its MCP
  toggle off after updating (a known Roblox bug) and points at the exact
  setting, instead of retrying a recovery that cannot work.
- Extension messages distinguish "Roblox Studio is not running" from "Studio
  is running but not connected" (new `studio_proc` status field), each with
  its own corrective step.
- Terminal spinner during slow startup phases (server launch, Studio
  attach), so the console never looks frozen; only one spinner animates at a
  time.
- start.bat hardening: refuses to run from an unextracted ZIP, handles
  missing winget, rescans install folders after a winget install (PATH not
  refreshed), prints the Python version and the bridge's exit code on
  screen, and logs the Windows build - so a single screenshot of the
  terminal carries enough context for support.

## [1.4.0] - 2026-07-08

### Added
- Multi-MCP addon servers (experimental): a new "MCP servers" section in the
  panel menu lets you add or remove additional MCP servers (Blender,
  Sketchfab, or any local MCP command) alongside the always-primary Roblox
  Studio connection. The bridge rewrites `config.json` and restarts itself to
  load a change; Roblox stays protected from edits/removal and its status dot
  is scoped to Roblox alone so an addon going down never misrepresents the
  primary connection. New `list_mcp_servers` command and a `server` param on
  `list_commands` let the model discover and use addon tool sets on demand.
  When Roblox is down but an addon server is alive, the panel now offers a
  degraded start instead of refusing to start at all.
- Vision support (screen_capture / other tool-returned images) enabled for
  Arena, Gemini, GLM, Kimi and Qwen, each with a real "upload finished" signal
  before sending instead of trusting the first local preview, fixing several
  silent-attachment-drop and duplicate-attachment-on-retry bugs. A tool from
  any connected server that returns an image now gets the camera chip and is
  remembered for future calls, even for a custom MCP server whose name gives
  no hint it returns images.
- Parser: a JSON command cut off by the model's own output limit, missing
  only its trailing closing brackets, is now auto-completed and executed
  instead of failing with a parse error and forcing a full retry turn.
  Strictly refuses to salvage anything where real content (not just closers)
  was cut off.
- Per-reason parse-error feedback (cut off, bad JSON, missing ###LUA###
  opener, wrong envelope) instead of one generic "bad JSON" message, so the
  model fixes the actual problem instead of guessing.

### Fixed
- DeepSeek: a command's chip could show green "done" while DeepSeek was still
  streaming the reply, on back-to-back calls to the same tool. Caused by
  DeepSeek's list virtualization defeating the turn-count identity guard;
  fixed with a stable per-turn id.
- GLM: new "scroll to bottom" buttons were mistaken for the Stop button and
  permanently latched generation state to "busy." Raw command JSON could leak
  into the visible reply when nested inside a paragraph. An image filename
  could corrupt result-chip detection.
- Kimi: added detection of Kimi's own native "Agent" mode, which conflicts
  with ZeroScript's command protocol; Start is disabled with a warning until
  it's turned off. Fixed the hidden file-upload input not existing until the
  "+" menu is opened, raw command text leaking when nested/oversized, and
  normal model prose containing "try again" being misread as a site error.
- Qwen: same "try again" false-busy fix as Kimi. A/B "carousel" comparison
  turns (where the composer disappears mid-carousel) now auto-resolve to
  Response 1 once both candidates finish, instead of stalling or misreading a
  candidate as a truncated command.
- Arena: send is now confirmed until the composer actually clears instead of
  trusting a single click, preventing stranded messages/attachments; the chip
  now anchors below the reply text instead of floating above it.
- A command turn abandoned mid-stream (reload, or superseded by a
  regenerate) no longer shows a false green checkmark; it now shows a
  neutral "not run" state instead.
- A tool's own in-body error (e.g. "Output of '...': Error executing code...")
  now settles the chip red instead of green, even when the tool didn't use
  ZeroScript's own ERROR wrapper.
- Regenerating a stopped command no longer briefly re-shows the old call's
  chip before the new one streams in.

### Changed
- The version number next to the ZeroScript name in the panel is now small,
  plain text instead of a bordered green badge.
- System prompt updated to cover multiple MCP servers: the model must call
  `list_mcp_servers` before assuming something outside Roblox is unsupported,
  and the tool list is no longer inlined in the prompt (fetched on demand via
  `list_commands`).

## [1.3.9] - 2026-07-04

### Fixed
- Bridge: kill the full process tree on restart instead of just the wrapper
  process, which used to leave orphaned StudioMCP.exe instances behind that
  fought the next launch and caused seemingly random "Studio looks connected
  but nothing responds" failures.
- Bridge: a dead MCP server is now auto-restarted by a background watchdog
  instead of waiting for the next tool call to notice.
- Bridge: a tool call that hits one of Studio's own brief connection blips now
  retries once instead of surfacing a spurious "Studio not connected" error.
- Extension: the status bar no longer shows a falsely healthy "N tools" label
  when the agent is active but Studio, the place, or the bridge itself isn't
  actually usable, it now names the real blocker (open a place / enable the
  MCP server / bridge offline).
- Cross-provider: DeepSeek, Gemini, Kimi, GLM and Qwen composer menus, model
  pickers and tooltips (including GLM's search hover card and Kimi's model
  popover) no longer render clipped or hidden behind ZeroScript's own
  bar/pill/cover.
- Cross-provider: a thinking model quoting command JSON in its own reasoning
  area no longer makes the tool chip flap between done/run/done (Gemini, Kimi,
  GLM and Qwen).
- The "Agent is working" composer cover now blocks clicks into the composer
  underneath it instead of letting them through, and can no longer balloon
  past the composer's visible band or drag itself off position when a site
  recreates its editor node mid-session (seen on Kimi).
- A command chip could briefly flash or restart its spinner when revisiting a
  past turn; it now settles to done correctly instead.
- DeepSeek: the raw system-prompt turn no longer flashes for a frame before
  being hidden.
- Gemini: "New chat" no longer gets stuck on "Agent active" from a reused
  previous conversation URL.
- Kimi: reasoning is read separately from the actual reply, so a command
  drafted while the model is still "thinking" is no longer detected or
  executed; input can no longer be typed mid-run after the editor node is
  recreated.
- Arena: unsupported-mode gate now also covers Web Search and Generate Image,
  and chip alignment is fixed when a command turn renders as an A/B
  model-comparison carousel.
- Bridge: a long-running tool call no longer starves the connection's ping
  handling and trips the half-open-socket watchdog.

### Changed
- Bridge and installer logs moved to `logs/bridge_debug.log` and
  `logs/start.log`; the console now only shows what a user actually needs to
  read, full detail still lands in the log files.
- `start.bat` now detects and explains a double launch instead of silently
  replacing the previous instance, and warns clearly if port 17613 stays held
  after trying to free it.
- Removed remaining em dashes from user-visible strings.
- Removed remaining em dashes from user-visible strings.

## [1.3.3] - 2026-06-24

### Fixed
- Bridge no longer depends on Roblox's `mcp.bat`, which hard-coded a single
  Studio version path and broke (0 tools / "Bridge or Studio offline") once
  Studio auto-updated and that version folder was removed. A new
  `launch_studio_mcp.py` finds the newest installed `StudioMCP.exe` and launches
  it directly.
- `bridge.py` now runs a `.py` MCP command with the same Python interpreter as
  the bridge, so it works on installs where only the `py` launcher exists.

## [1.0.0] - 2026-06-09

### Added
- Initial public release of ZeroScript Free
- Browser extension for Chrome and Edge (DeepSeek chat integration)
- Local Python bridge (`bridge.py` + `start.bat`) for Roblox Studio communication
- Built-in MCP server support (no plugin required - activate directly in Roblox Studio)
- Read and edit Luau scripts directly from DeepSeek chat
- Run Luau code in real time inside Roblox Studio
- Inspect game tree and instances
- Generate meshes, materials, and models
- Browse and insert assets from the Creator Store
- Control play-testing from chat
- Panel status indicator (green / yellow / grey)
- Auto kill port 17613 on start to avoid conflicts
- Ko-fi support link with Robux tip passes in the extension panel
- Setup tutorial video on YouTube
