# ZeroScript - Free AI Agent for Roblox Studio

![GitHub stars](https://img.shields.io/github/stars/sebattfg/ZeroScript-Free?style=social)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-lightgrey)
![License](https://img.shields.io/badge/license-GPL--3.0-blue)

**ZeroScript** is a free browser extension that turns ChatGPT, DeepSeek, Gemini, Kimi, GLM, Qwen, Arena or Meta AI into a Roblox Studio AI agent.
Control Roblox Studio with AI directly from your browser - read/edit scripts, run Luau, generate assets, all from a normal AI chat. No API key, no terminal, no coding needed.

> 🌐 **Website: [zerodev.tools/zeroscript](https://zerodev.tools/zeroscript)** the free Lemonade.gg / Luamotion alternative for building Roblox games with AI.

Eight AI providers are supported: **DeepSeek** (chat.deepseek.com, recommended), **ChatGPT** (chatgpt.com), **Google Gemini** (gemini.google.com), **Kimi** (kimi.ai, Moonshot AI), **GLM** (chat.z.ai, Z.ai), **Qwen** (chat.qwen.ai), **Arena** (arena.ai and lmarena.ai, a multi-model playground) and **Meta AI** (meta.ai). On ChatGPT, screenshots and image input are turned off on purpose: the free tier limits files and images on a separate quota from messages, so vision would only work part of the day. Gemini and Kimi can be unstable: Gemini tends to stop using the Roblox tools in long sessions, and Kimi sometimes uses its own native tools instead of the Roblox commands. On Arena, use **Direct** mode (ZeroScript only supports Direct; it blocks Start in Battle / Side-by-Side / Agent modes). DeepSeek is the recommended provider.

> 💬 **Stuck? Join the [Discord community](https://discord.gg/9aNyZsMWcb)** get help, share feedback, and follow updates.

> *Also known as: ZeroScript Roblox, ZeroScript free download, Roblox ChatGPT agent, Roblox DeepSeek agent, Roblox Gemini agent, Roblox Kimi agent, Roblox GLM agent, Roblox Qwen agent, Roblox Arena agent, Roblox Meta AI agent, Roblox Studio AI automation, Luau AI, MCP Roblox, lemonade alternative free, lemonade.gg alternative, free Roblox AI agent, free lemonade roblox alternative*

## ⚠️ ZeroScript is Free Beware of Paid Copycats

ZeroScript is 100% free and open-source. It always has been, and it always will be. There is no official paid version, no subscription, and no sign-in required to use the extension.

If you come across a site or extension using the ZeroScript name that asks for payment or account creation, it is **not** this project. The only official links are the ones listed at the top of this README.

## How it works

```
AI chat (ChatGPT / DeepSeek / Gemini / Kimi / GLM / Qwen / Arena / Meta AI, in your browser) -> ZeroScript Extension -> Bridge (your PC) -> Roblox Studio
```

The extension runs inside the chat page (ChatGPT, DeepSeek, Gemini, Kimi, GLM, Qwen, Arena or Meta AI). When you type a request, it sends commands to the Bridge running on your PC, which drives Roblox Studio through the built-in MCP server.

## Setup

> 📺 **Lost? Watch the [setup tutorial on YouTube](https://youtu.be/kPKiZLZ9_Ps) it covers every step below.**

### 1. Download the zip and install the extension

Download the latest zip from the **Releases** page and extract it. The zip contains both the **Bridge** and the **extension folder**.

To load the extension:

- Go to `edge://extensions` (Edge) or `chrome://extensions` (Chrome)
- Enable **Developer mode** (top right toggle)
- Click **Load unpacked**
- Select the `zeroscript-extension` folder from the extracted zip

### 2. Start Roblox Studio and enable MCP

Open Studio and load a Place, then enable MCP (first time only):

- Click **Assistant AI** in the top bar
- Click **...** (top right of the Assistant panel)
- Click **Manage MCP Servers**
- Click **Enable Studio as MCP Server**

> Not sure where to find these options? The [video tutorial](https://youtu.be/kPKiZLZ9_Ps) shows exactly where to click.

### 3. Run the Bridge

- **Windows:** double-click `start.bat` inside the extracted folder.
- **macOS:** double-click `MacOS_Start.command` inside the extracted folder. The first time, macOS will show a security warning ("could not verify... free of malware") - this is normal for any script downloaded outside the App Store, click **Done**, then go to **System Settings > Privacy & Security**, scroll to the bottom, and click **Open Anyway**. You only need to do this once.

A small window opens, that means the Bridge is running.

### 4. Start a session

Go to https://chat.deepseek.com (recommended), https://chatgpt.com, https://gemini.google.com, https://www.kimi.ai, https://chat.z.ai, https://chat.qwen.ai, https://arena.ai (or https://lmarena.ai), or https://www.meta.ai and open a new chat. The ZeroScript bar appears above the input box. Click **Start session**. Type what you want to build.

> Only works on chat.deepseek.com, chatgpt.com, gemini.google.com, kimi.ai, chat.z.ai, chat.qwen.ai, arena.ai (and lmarena.ai, the same app under its old name) and meta.ai - it will not work on any other site.
> On Arena, keep the mode dropdown on **Direct** - ZeroScript blocks Start in Battle / Side-by-Side / Agent modes (it only drives a single Direct reply).
> Gemini and Kimi can be unstable (model behavior, not the extension): Gemini may stop using the Roblox tools after a while, and Kimi may use its own native tools instead. If the AI starts answering in plain text instead of acting, remind it to use the commands or start a new session.
### 5. Watch the setup tutorial

[Watch the setup tutorial on YouTube](https://youtu.be/kPKiZLZ9_Ps)

## What the AI can do

- Read and edit scripts
- Run Luau code directly in Studio
- Inspect the game tree and instances
- Generate meshes, materials, and models
- Browse and insert from the Creator Store
- Control play-testing
- **Remember your project across sessions** persistent project memory saved inside your place
- **Several steps in one turn** - the AI can batch up to 16 independent commands into one, with every result returned together, and ask it to keep going after a failure
- **Write whole scripts in one command** - up to 40 files at once, folders and scripts created for you, every write read back from Studio to prove it landed, and the previous content snapshotted so it can be reverted
- **Linted before it touches your place** - a syntax error comes back as a file and a line number instead of a broken script
- **Verify and play-test** - compile every script in Studio's own Luau VM, then run the place for a few seconds and read the Output, before the AI tells you it works
- **A local mirror of your place** - every script on disk under `.zeroscript/`, so the AI can grep the whole project without asking Studio, and you can open the files in your own editor

## Faster: no waiting

Most of the wait in a session was never the model - it was the tooling around it. That is gone:

| Where the time went | Now | Measured |
|---------------------|-----|----------|
| The loop polled the page every 120-250ms, so a finished reply was noticed late | It waits for the page to **change** (a DOM observer), so it reacts the moment the answer lands | 760ms -> 250ms after the last token |
| The send path paused 200ms, and a tab switch back to the AI paused 400ms | A **paint** - two frames - instead of a fixed pause | 200ms / 400ms -> 32ms |
| Arena kept a 1.5s idle window open after every reply | Arena's own **"Stop generation" button** is the end-of-reply signal, so the turn ends at once | 1500ms -> 60ms after the last token |
| `script_write` read the old sources in one call and wrote in another | **One** Studio round trip: the undo snapshot rides along with the write | 2 round trips -> 1 |
| A batch of 16 reads was 16 round trips, one after the other | Read-only steps are **sent together** (and the Bridge lets up to 4 Studio calls overlap); writes still run one at a time, in order | the steps leave together, not one after the other |
| A broken build cost the whole play-test window (5-30s) before anyone heard about it | The Output is read **while the place runs**, so the first error ends the test in about a second | 10.5s -> 1.5s on a 10s window |
| Every Output read tried up to four argument spellings | The spelling that worked is **remembered** - one call | 3 round trips -> 2 |

(The "measured" column is the old code and the new code run side by side against
the same fake Studio, with every Studio call modelled as a 120ms round trip.)

## The Dev Kit

Studio's own MCP tools are primitives: search the tree, read one script, edit it in pieces, read it back to check. That is many round trips per idea, and every reply is a full turn of the AI's conversation.

The Bridge therefore implements eight commands of its own and serves them beside Studio's, as a server called `zeroscript`. They need no extra setup, they are always available (even before Studio is open), and they keep bulk data - whole script bodies, trees, Output - on your machine, so the AI reads a summary instead of a dump.

| Command | What one call does |
|---------|--------------------|
| `project_overview` | The place name, its instance tree, every script with its class, size and first line, recent Server/Client Output, project memory, and whether the local mirror is stale. |
| `workspace` | `pull` mirrors every script to disk, `grep` searches the whole project locally with no Studio call, `status` shows unpushed edits and what changed in Studio, `push` writes local edits back. |
| `script_write` | Creates or overwrites up to 40 WHOLE scripts at once - folders and scripts included - linted first, read back from Studio to prove every write landed, old content snapshotted for undo. |
| `luau_check` | Syntax-checks Luau (a whole file or several) in the Bridge and answers with file and line numbers, without touching Studio. |
| `script_verify` | Compiles every script in Studio's real Luau VM and reports errors with line numbers - the "will this actually run" check. |
| `script_history` | `list` / `diff` / `revert` the snapshots every write created. |
| `playtest` | Starts play, runs it a few seconds, optionally sends keys and mouse input, captures Server and Client Output, flags error lines, and stops. |
| `studio_view` | Selects or frames what was just built, so you see it in your editor. |

**Where its files live.** Everything is under `.zeroscript/` next to `bridge.py` (override with `ZS_BRIDGE_DATA_DIR`):

- `.zeroscript/workspace/<place>/` - the mirror of your scripts (`Service/Name.lua`) plus `zsmanifest.json`. Open them in any editor; `workspace push` sends your local edits back.
- `.zeroscript/history/<place>/` - one snapshot per write, 40 kept per script, so an AI edit can always be reverted.
- `.zeroscript/last/` - the full text of the last result of each command, when a summary was too short.

It is all derived data: deleting the folder loses the mirror and the undo history, never your place.

**Prompt presets.** The panel's *Custom prompt* box has a preset picker above it. A preset is a ready-made brief that is loaded **into the box**, so you can read it, edit it, and see exactly what will be sent - it is never a hidden layer. The built-in one, **Systems architect (strict Luau)**, asks for `--!strict` typed Luau, a server-authoritative three-file architecture (config ModuleScript / server Script / client LocalScript), Scale-only dark UI, and the Dev Kit in the order that keeps a session short: `project_overview` → `luau_check` → one atomic `script_write` → `script_verify` → `studio_view` → `playtest` → `script_history`. Your choice is remembered between sessions, and the moment you edit the text the picker stops claiming it is still the preset.

**A worked example.** [`examples/shop-system/`](examples/shop-system/) is a complete shop - config, server, client, 1,258 lines - written to exactly that brief and put through the whole pipeline, with a README covering the security model, the UI contract and the calls to run it in Studio. `python3 examples/shop-system/pipeline_demo.py` runs it end to end without Roblox (the linter, the mirror and the undo history are the real thing; the Studio side is the test double, and the README says which is which). Writing it found four real bugs in the tooling, all fixed and all covered by tests - see that README's last sections. The example is linted by `tests/test_examples.py` on every run, so the linter is checked against 1,258 lines of real modern Luau and the example cannot quietly drift from the brief.

## New in 1.6.0

- **The ZeroScript Dev Kit: eight commands that do a developer's work in one turn.** Studio's MCP tools are primitives, so building a system cost dozens of turns - search the tree, open each script, edit it in pieces, read it back to check. The Bridge now serves eight commands of its own beside them: `project_overview` (the place, its tree, every script's first line, recent Output, memory and mirror status in one call), `workspace` (a local mirror of every script, so `grep` searches the whole project without touching Studio), `script_write` (whole scripts, up to 40 files, created and verified and snapshotted), `luau_check` (syntax errors caught before anything is written), `script_verify` (every script compiled in Studio's real Luau VM), `script_history` (list / diff / revert the snapshots every write left), `playtest` (run the place, capture Server and Client Output, stop) and `studio_view` (select or frame what was just built so you can see it). They show up in the tool list as a server called **zeroscript**, they are always available even before Studio is open, and they go through the same Stop button, timeouts and chips as any other command.
- **Batches are 16 steps, and can survive a failure.** `continue_on_error: true` runs every step of a batch even after one fails, reporting each result - useful when the steps are independent reads or edits rather than one plan.
- **No wasted first turn.** When the command list fits, it is typed into the session at startup, so the model starts working instead of spending its first turn asking what commands exist.

## New in 1.5.5

- **DeepSeek: the agent starts again on the new unified model.** DeepSeek merged Instant, Expert and Vision into one model and removed the model picker, which left "Start Roblox agent" stuck on "DeepSeek mode not ready". ZeroScript now recognises the new chat box, switches Search off and starts, with DeepThink left on.
- **DeepSeek: screenshots work on every chat.** Images no longer need the Vision tab (it is gone) - the unified model sees your Studio captures, one at a time or several in a row.

## New in 1.5.4

- **ChatGPT: the ZeroScript bar is back above the composer.** ChatGPT redesigned its input box and renamed the layout slot the bar sits in. ZeroScript kept asking for the old name, so the browser dropped the bar into a stray strip at the bottom right of the composer and squeezed the text field to nothing. The bar now takes the right row again, and it reads the layout live instead of trusting a fixed name, so the next redesign should not knock it out.
- **ChatGPT: long commands read cleanly on the new interface.** The same redesign replaced the code-block editor that used to hide line breaks and cut long lines off - the cause of the truncated commands fixed in 1.5.1. A 400-line block now reads back whole. If you are still on the old interface, the previous workaround is untouched.

## New in 1.5.3

- **Kimi moved to kimi.ai.** The old address, kimi.com, now asks for a Chinese phone number to sign in, which locked most people out. Open https://www.kimi.ai instead - the page is unchanged, the bar appears above the input box exactly as before. Reopen any Kimi tab you had on the old address.
- **DeepSeek: the Instant model can now run the agent.** Picking Instant used to leave "Start Roblox agent" spinning forever with no explanation, because only Expert and Vision were accepted. Choose Instant before starting and the session runs on it - much faster than Expert, without the reasoning pass. Images stay off on Instant just like on Expert; the Vision tab remains the only one that can see screenshots.
- **DeepSeek: a reply written in DeepSeek's own tool-call format no longer kills the turn.** DeepSeek occasionally answers with its internal markup instead of a ZeroScript command. Nothing recognised it, so the tool never ran, the raw tags stayed on screen and the agent stopped dead with you waiting. It is now caught, hidden behind a tool chip like any other command, and DeepSeek is told to rewrite the call properly.
- **ChatGPT: the bar no longer clips into the composer's rounded corners.**

See [CHANGELOG.md](CHANGELOG.md) for older releases.

## Panel status

| Dot | Meaning |
|-----|---------|
| Green | Bridge + Studio ready (a place is open) |
| Yellow | Bridge OK, but Studio isn't usable yet - open Roblox Studio, load a place, or enable its MCP server (hover the dot for the exact reason) |
| Grey | Bridge offline - run start.bat (Windows) or MacOS_Start.command (macOS) |

## Other MCP servers (local or remote)

The Bridge can also run other MCP servers next to Roblox Studio. In the extension's MCP menu, enter either:

- a **start command** for a local server, for example `npx -y @some/mcp-server`, or
- an **https:// address** for a remote server that speaks MCP over Streamable HTTP. If it needs headers (for example an API key), type them as JSON in the **Headers** box, such as `{"Authorization": "Bearer ${MY_KEY}"}`. A value such as `${MY_KEY}` is read from the environment when the Bridge connects, so the secret does not have to be stored in `config.json`. Typing a literal key does store it in `config.json`.

Servers are saved in `config.json`. Only pages from the ZeroScript extension can talk to the Bridge: a normal web page you visit cannot list tools or add servers.

For tests or unusual setups, `ZS_BRIDGE_CONFIG` and `ZS_BRIDGE_LOGS_DIR` change where the Bridge reads its config and writes its logs. `ZS_BRIDGE_ALLOW_ORIGINS` (comma-separated) admits extra browser origins to the Bridge. Leave all three unset for normal use.

## Requirements

- Windows or macOS
- Roblox Studio (MCP support built-in)
- Microsoft Edge or Chrome
- Python 3.9+ (installed automatically on Windows, or install it yourself on macOS - see [python.org/downloads](https://www.python.org/downloads/))

## Support

ZeroScript is free. If it saves you time: [Ko-fi](https://ko-fi.com/sebattfg) - Robux tip passes available in the extension panel

---

Credit: the idea for connecting other MCP servers (Blender, Sketchfab, etc.) alongside Roblox Studio came from [javnpa](https://github.com/javnpa).

Credit: macOS/Linux support contributed by [archivealf](https://github.com/archivealf).
