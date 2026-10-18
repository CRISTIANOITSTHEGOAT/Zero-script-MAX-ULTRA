// Node smoke test for the FAST paths (run: node test-fast.js). Not shipped.
//
// The agent loop used to sit in flat sleeps between every DOM read, so a reply
// that had already finished was noticed up to 250ms late, and every tool
// result inherited that dead time. This file guards the replacements:
//
//   - the core waits on the NEXT DOM CHANGE (waitTick) instead of a timer
//   - the Arena (LM Arena) provider ends a turn the INSTANT the site's own
//     "Stop generation" button goes away, instead of waiting out a 1.5s idle
//     window on every single turn
//   - lmarena.ai (the same app under its old domain) is a supported route
//
// The provider is a browser IIFE, so it is evaluated against a minimal stub
// DOM - the same no-dependency approach as test-chatgpt.js.
const fs = require("fs");

const ok = (name, cond) => { console.log((cond ? "PASS" : "FAIL") + "  " + name); if (!cond) process.exitCode = 1; };

// ── Stub DOM ────────────────────────────────────────────────────────────────
function mkEl(classes, children, attrs) {
  const o = attrs || {};
  const node = {
    nodeType: 1,
    className: classes.join(" "),
    classList: { contains: (c) => classes.includes(c) },
    getAttribute: (n) => (n in o ? o[n] : null),
    matches: (sel) => sel.split(",").some((s) => {
      const t = s.trim();
      return t.startsWith(".") && classes.includes(t.slice(1));
    }),
    offsetParent: {},
    childNodes: (children || []).map((c) => (typeof c === "string"
      ? { nodeType: 3, nodeValue: c, childNodes: [] } : c)),
  };
  node.querySelector = (sel) => {
    const hit = (n) => {
      if (n.nodeType !== 1) return null;
      if (n.matches(sel)) return n;
      for (const c of n.childNodes) { const r = hit(c); if (r) return r; }
      return null;
    };
    for (const c of node.childNodes) { const r = hit(c); if (r) return r; }
    return null;
  };
  node.textContent = node.childNodes.map((c) => (c.nodeValue || c.textContent || "")).join("");
  return node;
}

let TURN_TEXT = "";
const turn = mkEl(["mx-auto"], []);
const prose = mkEl(["prose"], []);
turn.querySelector = (sel) => (sel.includes("prose") ? prose : null);
turn.childNodes = [prose];
prose.querySelector = () => null;
const ol = { children: [turn], querySelector: () => null };

const buttons = [];
function setButtons(list) { buttons.length = 0; list.forEach((b) => buttons.push(b)); }
function mkButton(label) {
  return { nodeType: 1, getAttribute: (n) => (n === "aria-label" ? label : null),
           offsetParent: {}, textContent: "", classList: { contains: () => false },
           matches: () => false, click() {} };
}

// The composer: Arena's is the only <textarea> inside a <form>.
const editor = { nodeType: 1, value: "", tagName: "TEXTAREA",
                 getAttribute: () => null, setAttribute: () => {},
                 closest: () => null, focus: () => {} };
let HAS_EDITOR = true;

global.document = {
  querySelector: (sel) => (String(sel).includes("flex-col-reverse") ? ol : null),
  querySelectorAll: (sel) => {
    if (sel === "button") return buttons;
    if (sel === "form textarea") return HAS_EDITOR ? [editor] : [];
    return [];
  },
  addEventListener: () => {},
  dispatchEvent: () => {},
  documentElement: { classList: { contains: () => false } },
  body: null,
};
global.window = { location: { pathname: "/text/direct" }, addEventListener: () => {} };
global.location = global.window.location;
global.CustomEvent = class { constructor(t) { this.type = t; } };
global.MutationObserver = class { observe() {} disconnect() {} };
global.getComputedStyle = () => ({});
global.atob = (s) => Buffer.from(s, "base64").toString("binary");

// A controllable clock: the provider asks Date.now() for every freshness
// decision, so the test can move time exactly.
let CLOCK = 1000;
const realNow = Date.now;
Date.now = () => CLOCK;

const P = new Function(
  fs.readFileSync(__dirname + "/providers/arena.js", "utf8") + "; return ZSProvider;"
)();

// ── The reply text the provider reads ───────────────────────────────────────
function setReply(text) {
  TURN_TEXT = text;
  prose.childNodes = [{ nodeType: 3, nodeValue: text, childNodes: [] }];
  prose.textContent = text;
}

// ── Generation end is detected the instant the button goes away ─────────────
setReply("Hello there, this is the model's answer.");
setButtons([mkButton("Stop generation")]);
ok("a live Stop button means the model is generating", P.isGenerating() === true);

// The site swaps the Stop button for the Send button the moment the model
// stops. From that moment on the turn is over - the old code waited out
// GEN_IDLE_MS (1.5s) before believing it, on EVERY turn.
setButtons([mkButton("Send message")]);
CLOCK += 120;   // 120ms after the last token - well inside the old 1.5s window
ok("the turn ends the moment the Stop button goes away", P.isGenerating() === false);

// ...but a button that merely blinks out for a frame while the text is still
// growing must NOT end the turn.
setReply("Hello there, this is the model's answer, and it keeps going");
setButtons([mkButton("Stop generation")]);
CLOCK += 50;
ok("a growing reply keeps the turn alive", P.isGenerating() === true);
setButtons([mkButton("Send message")]);
CLOCK += 20;
ok("text that grew after the button was last seen keeps the turn alive",
   P.isGenerating() === true);

// ── The stop button is the site's own signal, so a long silent generation is
// still a generation (the wedge guard only fires after 10s of no growth).
setReply("x");
setButtons([mkButton("Stop generation")]);
CLOCK += 9000;
ok("a slow but live generation is still generating", P.isGenerating() === true);

// ── lmarena.ai is the same app under its old domain ────────────────────────
// A FRESH chat has no turns in the list - that is what the start gate needs.
ol.children = [];
global.location.pathname = "/c/new";
global.window.location.pathname = "/c/new";
ok("a fresh lmarena.ai chat has no conversation id", P.conversationKey() === "");
ok("a fresh lmarena.ai chat is recognised as fresh", P.isFreshChat() === true);

global.location.pathname = "/c/1234-abcd";
global.window.location.pathname = "/c/1234-abcd";
ok("a real lmarena.ai conversation keeps its id", P.conversationKey() === "/c/1234-abcd");
ok("an existing lmarena.ai conversation is not a fresh chat", P.isFreshChat() === false);

global.location.pathname = "/text/direct";
global.window.location.pathname = "/text/direct";
ok("a fresh arena.ai chat has no conversation id", P.conversationKey() === "");
ok("a fresh arena.ai chat is recognised as fresh", P.isFreshChat() === true);

// ...and a chat that already has turns is never "fresh", whatever the route.
ol.children = [turn];
ok("a chat with turns is not a fresh chat", P.isFreshChat() === false);

Date.now = realNow;

// ── The core: no flat sleeps left in the hot paths ──────────────────────────
const mainSrc = fs.readFileSync(__dirname + "/core/main.js", "utf8");
ok("the core still has its DOM-change wait", /function waitTick\(/.test(mainSrc));
ok("the core still has its paint wait", /function nextPaint\(/.test(mainSrc));
ok("the response watcher no longer sleeps on a flat timer", !/await sleep\(/.test(mainSrc));
ok("the watcher waits on DOM changes instead", (mainSrc.match(/await waitTick\(/g) || []).length >= 8);
ok("the send path waits for a paint, not a fixed delay", /await nextPaint\(\);/.test(mainSrc));
ok("the settle window is short and provider-tunable",
   /const SETTLE_MS = T.SETTLE_MS \|\| 250;/.test(mainSrc));

// ── The Arena provider: no flat sleeps in its waits either ─────────────────
const arenaSrc = fs.readFileSync(__dirname + "/providers/arena.js", "utf8");
ok("the Arena provider waits on DOM changes", /function waitTick\(/.test(arenaSrc));
ok("the Arena provider no longer polls on a flat timer", !/await sleep\(120\)/.test(arenaSrc));
ok("the Arena provider ends a turn on the button going away",
   /_stopSeenAt && now - _stopSeenAt < 3000/.test(arenaSrc));

// ── The batch: read-only steps run in parallel, writes do not ──────────────
{
  const src = fs.readFileSync(__dirname + "/core/main.js", "utf8");
  const start = src.indexOf("  const BATCH_MAX_STEPS");
  const end = src.indexOf("  function argSummary(call) {");
  ok("runBatch source found", start > 0 && end > start);
  const body = src.slice(start, end);
  const ZS = new Function(fs.readFileSync(__dirname + "/core/config.js", "utf8") + "; return ZS;")();

  const makeRunner = (scripted) => {
    const A = { stop: false, imageTools: new Set() };
    const calls = [];
    let inflight = 0, peak = 0;
    const runTool = async (call) => {
      calls.push(call.tool);
      inflight++; peak = Math.max(peak, inflight);
      await new Promise((r) => setTimeout(r, 5));
      inflight--;
      const out = scripted[call.tool];
      return out && out.text !== undefined ? out.text : `Output of '${call.tool}':\nok`;
    };
    const bareToolName = (n) => n.split("/").pop();
    const VISION_TOOLS = new Set(["screen_capture"]);
    const isBlockedTool = (n) => n === "subagent";
    const feedbackIsError = (f) => f.startsWith("ERROR");
    const stripOutputPrefix = (f) => f.replace(/^Output of '[^']*':\n?/, "");
    const diag = () => {};
    const run = new Function("ZS", "A", "runTool", "bareToolName", "VISION_TOOLS", "isBlockedTool",
      "feedbackIsError", "stripOutputPrefix", "diag",
      body + "\nreturn runBatch;")(ZS, A, runTool, bareToolName, VISION_TOOLS, isBlockedTool,
      feedbackIsError, stripOutputPrefix, diag);
    return { run, calls, peak: () => peak };
  };

  (async () => {
    const r = makeRunner({});
    await r.run({ calls: [{ command: "script_read", params: {} },
                          { command: "project_overview", params: {} }] });
    ok("read-only steps run in parallel", r.peak() >= 2);
    ok("the results keep the plan order",
       JSON.stringify(r.calls) === '["script_read","project_overview"]');

    const w = makeRunner({});
    await w.run({ calls: [{ command: "script_write", params: {} },
                          { command: "script_read", params: {} }] });
    ok("a write step still runs on its own", w.peak() === 1);
    ok("the write still comes first", JSON.stringify(w.calls) === '["script_write","script_read"]');
  })();
}
