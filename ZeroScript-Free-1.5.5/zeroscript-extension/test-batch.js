// Node smoke test for the batch command (run: node test-batch.js). Not shipped.
// Covers the pure helpers in core/config.js and the parser's view of a batch
// envelope in core/parser.js. The runner (runBatch in core/main.js) needs the
// DOM and is exercised live, not here.
const fs = require("fs");
const load = (f, ret) => new Function(fs.readFileSync(__dirname + "/" + f, "utf8") + "; return " + ret + ";")();
const ZS = load("core/config.js", "ZS");
const ZSParse = load("core/parser.js", "ZSParse");

const ok = (name, cond) => { console.log((cond ? "PASS" : "FAIL") + "  " + name); if (!cond) process.exitCode = 1; };

// ── normalizeBatchSteps ───────────────────────────────────────────────────
const good = ZS.normalizeBatchSteps([
  { command: "script_read", params: { target_file: "A" } },
  { command: "inspect_instance", params: { path: "Workspace" } },
], 8);
ok("valid batch gives two steps", good.steps && good.steps.length === 2);
ok("step keeps tool and params", good.steps && good.steps[0].tool === "script_read" && good.steps[0].arguments.target_file === "A");
ok("tool/arguments spelling also accepted",
  (ZS.normalizeBatchSteps([{ tool: "x", arguments: { a: 1 } }], 8).steps || [])[0].arguments.a === 1);
ok("missing params becomes {}",
  ZS.normalizeBatchSteps([{ command: "list_commands" }], 8).steps[0].arguments !== undefined);
ok("empty array refused", !!ZS.normalizeBatchSteps([], 8).error);
ok("non-array refused", !!ZS.normalizeBatchSteps({ command: "x" }, 8).error);
ok("too many steps refused", !!ZS.normalizeBatchSteps(Array(9).fill({ command: "x" }), 8).error);
ok("exactly max steps allowed", !ZS.normalizeBatchSteps(Array(8).fill({ command: "x" }), 8).error);
ok("missing name refused", /step 2 has no/.test(ZS.normalizeBatchSteps([{ command: "x" }, { params: {} }], 8).error));
ok("nested batch refused", /cannot contain another batch/.test(ZS.normalizeBatchSteps([{ command: "batch", params: {} }], 8).error));
ok("array params replaced by {}", ZS.normalizeBatchSteps([{ command: "x", params: [1, 2] }], 8).steps[0].arguments.constructor === Object);

// ── formatBatch ───────────────────────────────────────────────────────────
const allOk = ZS.formatBatch([
  { tool: "a", text: "one", isErr: false },
  { tool: "b", text: "two", isErr: false },
], 2, 6000);
ok("all-ok batch starts with Output of 'batch'", allOk.startsWith("Output of 'batch' (2/2 steps ran):"));
ok("all-ok batch lists each step", allOk.includes("[1/2] a ok\none") && allOk.includes("[2/2] b ok\ntwo"));
ok("all-ok batch is not an ERROR", !allOk.startsWith("ERROR"));

const mid = ZS.formatBatch([
  { tool: "a", text: "fine", isErr: false },
  { tool: "b", text: "ERROR: bad", isErr: true },
], 4, 6000);
ok("failure makes the whole message an ERROR", mid.startsWith("ERROR: batch stopped at step 2 of 4"));
ok("failed step is marked FAILED", mid.includes("[2/4] b FAILED"));
ok("skipped steps are reported", mid.includes("[not run] 2 step(s) after the stop were skipped."));

const big = ZS.formatBatch([{ tool: "a", text: "x".repeat(10000), isErr: false }], 1, 500);
ok("long step output is cut", big.length < 800 && big.includes("cut at 500 chars"));

const empty = ZS.formatBatch([{ tool: "a", text: "   ", isErr: false }], 1, 500);
ok("empty step output is labelled", empty.includes("(empty result)"));

// ── parser: a batch envelope is ONE command, nested steps stay inside it ──
const reply = 'Reading both.\n```json\n{"command": "batch", "params": {"calls": [' +
  '{"command": "script_read", "params": {"target_file": "A"}},' +
  '{"command": "multi_edit", "params": {"file_path": "B", "edits": []}}]}}\n```';
const calls = ZSParse.parseToolCalls(reply);
ok("batch reply parses as exactly one call", calls.length === 1);
ok("the one call is the batch", calls.length === 1 && calls[0].tool === "batch");
ok("the batch keeps its nested calls", calls.length === 1 && Array.isArray(calls[0].arguments.calls) && calls[0].arguments.calls.length === 2);
ok("the nested steps round-trip through normalizeBatchSteps",
  calls.length === 1 && ZS.normalizeBatchSteps(calls[0].arguments.calls, 8).steps[1].tool === "multi_edit");

// ── the batch cap (core/main.js) ──────────────────────────────────────────
// Every reply is a full model turn, so the cap is what decides how many turns a
// task costs. 16 is asserted here because lowering it silently would make every
// session slower without anything else noticing.
{
  const src = fs.readFileSync(__dirname + "/core/main.js", "utf8");
  const m = /const BATCH_MAX_STEPS = (\d+);/.exec(src);
  ok("BATCH_MAX_STEPS is 16", !!m && m[1] === "16");
  ok("the prompt tells the model the same cap",
    /Up to 16 steps/.test(fs.readFileSync(__dirname + "/core/config.js", "utf8")));
}

// ── runBatch (core/main.js): run it for real, with runTool stubbed ─────────
// runBatch lives inside main.js's closure, so pull its source out and run it with
// the few helpers it uses replaced by stubs. This checks the control flow: order,
// stop-on-failure, blocked steps and the Stop flag.
(async () => {
  const src = fs.readFileSync(__dirname + "/core/main.js", "utf8");
  const start = src.indexOf("  const BATCH_MAX_STEPS");
  const end = src.indexOf("  function argSummary(call) {");
  ok("runBatch source found in core/main.js", start > 0 && end > start);
  const body = src.slice(start, end);

  // runBatch reads these names from its closure; they are the stubs below.
  const makeRunner = (scripted, opts = {}) => {
    const A = { stop: false, imageTools: new Set(opts.images || []) };
    const calls = [];
    const runTool = async (call) => {
      calls.push(call.tool);
      const out = scripted[call.tool];
      if (out && out.stopAfter) A.stop = true;
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
    return { run, calls, A };
  };

  {
    const r = makeRunner({});
    const out = await r.run({ calls: [{ command: "a", params: {} }, { command: "b", params: {} }] });
    ok("runBatch runs every step in order", JSON.stringify(r.calls) === '["a","b"]');
    ok("runBatch success is not an ERROR", out.startsWith("Output of 'batch' (2/2"));
  }
  {
    const r = makeRunner({ b: { text: "ERROR: boom" } });
    const out = await r.run({ calls: [{ command: "a" }, { command: "b" }, { command: "c" }] });
    ok("runBatch stops at the first failed step", JSON.stringify(r.calls) === '["a","b"]');
    ok("runBatch reports the failure as an ERROR", out.startsWith("ERROR: batch stopped at step 2 of 3"));
    ok("runBatch marks the skipped step", out.includes("[not run] 1 step(s)"));
  }
  {
    const r = makeRunner({});
    const out = await r.run({ calls: [{ command: "subagent" }, { command: "a" }] });
    ok("runBatch refuses a blocked step and stops", JSON.stringify(r.calls) === "[]" && out.startsWith("ERROR: batch stopped at step 1"));
  }
  {
    const r = makeRunner({}, { images: ["learned_img"] });
    const out = await r.run({ calls: [{ command: "learned_img" }, { command: "a" }] });
    ok("runBatch refuses a learned image tool", JSON.stringify(r.calls) === "[]" && out.includes("cannot be used inside a batch"));
  }
  {
    const r = makeRunner({ a: { stopAfter: true } });
    await r.run({ calls: [{ command: "a" }, { command: "b" }] });
    ok("runBatch honours the Stop flag between steps", JSON.stringify(r.calls) === '["a"]');
  }
  {
    const r = makeRunner({});
    const out = await r.run({ calls: "nope" });
    ok("runBatch reports bad input without running", r.calls.length === 0 && out.startsWith("ERROR: batch needs"));
  }
  {
    // continue_on_error: independent steps all run, every failure is reported.
    const r = makeRunner({ b: { text: "ERROR: boom" } });
    const out = await r.run({ calls: [{ command: "a" }, { command: "b" }, { command: "c" }],
                              continue_on_error: true });
    ok("continue_on_error runs the steps after a failure", JSON.stringify(r.calls) === '["a","b","c"]');
    ok("continue_on_error still reports the failure as an ERROR",
      out.startsWith("ERROR: batch ran 3 of 3 steps and 1 FAILED"));
    ok("continue_on_error marks which step failed", out.includes("[2/3] b FAILED"));
    ok("continue_on_error keeps the results of the steps that worked",
      out.includes("[1/3] a ok") && out.includes("[3/3] c ok"));
    ok("continue_on_error does not report skipped steps", !out.includes("[not run]"));
  }
  {
    // ...and it accepts the string form too (models send "true" for a boolean).
    const r = makeRunner({ b: { text: "ERROR: boom" } });
    await r.run({ calls: [{ command: "a" }, { command: "b" }, { command: "c" }],
                  continue_on_error: "true" });
    ok("continue_on_error accepts \"true\"", JSON.stringify(r.calls) === '["a","b","c"]');
  }
  {
    // A 16-step batch is accepted whole.
    const r = makeRunner({});
    const calls = Array.from({ length: 16 }, (_x, i) => ({ command: "s" + i }));
    const out = await r.run({ calls });
    ok("a 16-step batch runs whole", r.calls.length === 16 && out.startsWith("Output of 'batch' (16/16"));
    const tooMany = await r.run({ calls: calls.concat([{ command: "s16" }]) });
    ok("a 17-step batch is refused before running", tooMany.startsWith("ERROR: batch takes at most 16"));
  }
})();
