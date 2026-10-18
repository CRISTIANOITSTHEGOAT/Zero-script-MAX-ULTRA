// Node smoke test for the extension popup (run: node test-popup.js). Not shipped.
//
// popup.js is a whole document script, so it is run here against a stub DOM and a
// stub `chrome`. What matters is the connectivity dot: the Bridge now reports a
// "zeroscript" Dev Kit server that is ALWAYS alive, and a dot that counted it
// would read green with Roblox Studio closed - the one signal users trust.
const fs = require("fs");
const SRC = fs.readFileSync(__dirname + "/popup.js", "utf8");

const ok = (name, cond) => { console.log((cond ? "PASS" : "FAIL") + "  " + name); if (!cond) process.exitCode = 1; };

function loadPopup() {
  const els = {};
  const el = (id) => (els[id] = els[id] || { id, className: "", textContent: "", addEventListener() {} });
  const document = { getElementById: el };
  const chrome = {
    runtime: { getManifest: () => ({ version: "9.9.9" }), sendMessage: (_m, cb) => cb && cb(null),
               onMessage: { addListener() {} } },
    tabs: { create() {}, query: (_q, cb) => cb([]), sendMessage() {}, update() {} },
  };
  const render = new Function("document", "chrome", "setInterval",
    SRC + "\n; return render;")(document, chrome, () => 0);
  return { render, els, ver: els.ver.textContent };
}

const P = loadPopup();
ok("the popup shows the manifest version", P.ver === "v9.9.9");

const dot = () => P.els.dot.className;
const state = () => P.els.state.textContent;
const tools = () => P.els.tools.textContent;
const servers = () => P.els.servers.textContent;

const KIT = { id: "zeroscript", alive: true, tools: 8, local: true };

// ── bridge offline ────────────────────────────────────────────────────────
P.render({ connected: false });
ok("offline: no dot", dot() === "dot ");
ok("offline: says the bridge is offline", state() === "Bridge offline");
ok("offline: tells the user to run the bridge", tools() === "Run bridge.py");
ok("offline: no server list", servers() === "");

// ── bridge up, Dev Kit alive, Roblox Studio closed ────────────────────────
// The regression this file exists for: 8 always-available tools must not look
// like a working Studio.
P.render({ connected: true, mcpAlive: false, studio: false, tools: 0, localTools: 8,
           servers: [{ id: "roblox", alive: false, tools: 0 }, KIT] });
ok("studio closed: dot is warn, never green", dot() === "dot warn");
ok("studio closed: says to open Roblox Studio", state() === "Bridge OK · open Roblox Studio");
ok("studio closed: still counts the Dev Kit's tools", tools() === "8 tools available");
ok("studio closed: the Dev Kit is listed as built in",
  servers().includes("● zeroscript dev kit (built in) (8 tools)") &&
  servers().includes("○ roblox (down)"));

// ── bridge + Studio + a place ─────────────────────────────────────────────
P.render({ connected: true, mcpAlive: true, studio: true, tools: 14, localTools: 8,
           servers: [{ id: "roblox", alive: true, tools: 14 }, KIT] });
ok("ready: dot is green", dot() === "dot on");
ok("ready: says Studio is ready", state() === "Connected · Roblox Studio ready");
ok("ready: counts Studio's tools plus the Dev Kit's", tools() === "22 tools available");

// ── Studio's MCP answers but no place is loaded ───────────────────────────
P.render({ connected: true, mcpAlive: true, studio: false, tools: 14, localTools: 8,
           servers: [{ id: "roblox", alive: true, tools: 14 }, KIT] });
ok("no place: dot is warn", dot() === "dot warn");
ok("no place: points at the MCP setting",
  state() === "Studio not connected · enable the MCP server in Studio");

// ── an addon server alive while Roblox is down must not light the dot ─────
P.render({ connected: true, mcpAlive: false, studio: true, tools: 5, localTools: 8,
           servers: [{ id: "roblox", alive: false, tools: 0 }, { id: "blender", alive: true, tools: 5 }, KIT] });
ok("addon only: dot stays warn", dot() === "dot warn");
ok("addon only: the addon is still listed", servers().includes("● blender (5 tools)"));

// ── an old bridge that reports no per-server health ───────────────────────
P.render({ connected: true, mcpAlive: true, studio: true, tools: 14, servers: [] });
ok("old bridge: falls back to mcpAlive", dot() === "dot on");
P.render({ connected: true, mcpAlive: false, studio: false, tools: 0, localTools: 8, servers: [KIT] });
ok("old bridge: a Dev Kit alone is not connectivity", dot() === "dot warn");

// ── the in-page bar applies the same rule (core/main.js) ──────────────────
const MAIN = fs.readFileSync(__dirname + "/core/main.js", "utf8");
ok("main.js gates on the primary Roblox server",
  /const roblox = servers.find\(\(x\) => x.id === "roblox"\);/.test(MAIN));
ok("main.js excludes the Dev Kit from the no-health fallback",
  /servers.some\(\(x\) => x.alive && !x.local && x.id !== ZS.LOCAL_SERVER_ID\)/.test(MAIN));
