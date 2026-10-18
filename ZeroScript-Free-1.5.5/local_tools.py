# SPDX-License-Identifier: GPL-3.0-or-later
# local_tools.py
# ──────────────────────────────────────────────────────────────────────────
#  ZeroScript Dev Kit - bridge-side tools that turn "chat with Roblox Studio"
#  into an actual developer workflow.
#
#  WHY THIS EXISTS
#  The Roblox Studio MCP server exposes primitives (execute_luau, multi_edit,
#  script_read, inspect_instance...). They work, but a task like "add a shop
#  system" then costs 15-30 model turns: read the tree, read this script, read
#  that script, create a script, create another, check it compiled... Every turn
#  is a full model generation (10-60s), so the primitives - not Studio - are the
#  bottleneck.
#
#  These tools are implemented HERE, in the bridge, and each one collapses a
#  whole routine into ONE call:
#    project_overview  - place name + instance tree + every script (path, size,
#                        first line) + recent Output + memory status, at once.
#    script_write      - create/overwrite MANY whole scripts in one call, with a
#                        read-back verification and an automatic multi_edit
#                        fallback if Studio refuses a direct Source write.
#    script_verify     - compile every script in the place (loadstring) and
#                        report syntax errors with line numbers.
#    luau_check        - static pre-flight lint that needs NO Studio at all
#                        (block/paren balance, chat-corrupted unicode, blocking
#                        WaitForChild, unparented instances, deprecated waits).
#    script_history    - snapshots/diff/revert for every script written through
#                        the bridge: a real undo, stored on disk.
#    workspace         - the place's scripts mirrored to disk as .lua files, with
#                        instant local grep, push and drift status.
#    studio_view       - select/focus what the AI just touched, so the user SEES
#                        it in the editor instead of trusting a text reply.
#    playtest          - start play, let it run, capture Server+Client Output,
#                        flag errors, stop: "run it and tell me what broke".
#
#  The second, quieter win: BULK STAYS LOCAL. A pull of 40 scripts moves ~200 KB
#  through the bridge and lands on disk - the model only ever sees a summary.
#  Chat context is the scarcest resource in this whole pipeline, so nothing here
#  returns raw sources unless the model explicitly asks for one file.
#
#  Every tool returns {"text": ..., "images": [...]} exactly like an MCP
#  tools/call result, and raises ToolError for anything the model should see as
#  a red chip + "ERROR ..." feedback.
# ──────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# Everything the Dev Kit writes lives under one folder (git-ignored). Tests point
# ZS_BRIDGE_DATA_DIR at a temp dir so they never touch a real user's mirror.
DATA_DIR = os.environ.get("ZS_BRIDGE_DATA_DIR") or os.path.join(HERE, ".zeroscript")
WORKSPACE_ROOT = os.path.join(DATA_DIR, "workspace")
HISTORY_ROOT = os.path.join(DATA_DIR, "history")
LAST_OUTPUT_DIR = os.path.join(DATA_DIR, "last")

# The pseudo server id these tools are advertised under. The extension shows them
# inside the normal Roblox command list (they ARE Roblox workflow tools), but
# keeping a distinct id means list_mcp_servers can describe them honestly and the
# bridge's health report never credits them to Studio's own catalogue.
LOCAL_SERVER_ID = "zeroscript"

# Per inner Studio call. execute_luau itself runs on a ~20s budget in Studio, so
# anything longer just waits for a timeout that Studio already decided.
STUDIO_TIMEOUT = 25.0
# Player input lands on the frames AFTER the call that sent it returns, so
# playtest waits this long before reading Output - without it, "0 suspicious
# lines" can mean "the click had not happened yet".
INPUT_SETTLE_S = 1.5
# A tool's own reply is typed into a chat box by the extension, so it is capped.
# The FULL text of anything bigger is written to .zeroscript/last/<tool>.txt and
# the path is mentioned, so nothing is ever silently lost.
MAX_RESULT_CHARS = 12000
# Chunk size for payloads pushed INTO Studio through execute_luau. Studio's
# parser rejects pathologically large chunks (documented live at ~2 KB for a
# single flat expression; string literals are far more tolerant), so writes are
# split well below anything risky and re-split again on a parse failure.
MAX_WRITE_BYTES = 24000
# Same idea for sources coming OUT (a chunk that fails to parse is halved and
# retried, down to a single script).
MAX_READ_BYTES = 120000

# Snapshots kept per script before the oldest is pruned.
HISTORY_KEEP = 40

_lock = threading.RLock()


class ToolError(Exception):
    """A failure the model should see as an ERROR (and adapt to)."""


# ══════════════════════════════════════════════════════════════════════════
#  SMALL HELPERS
# ══════════════════════════════════════════════════════════════════════════
def _slug(text, default="place", maxlen=60):
    """Filesystem-safe name (Windows included: no drive letters, no reserved
    names, no trailing dots/spaces). Anything unusable becomes a dash, and runs
    of dashes collapse, so mirror folders read like "My-Place-v2" rather than
    "My-Place_-v2_"."""
    s = re.sub(r"[^A-Za-z0-9._ -]+", "-", str(text or ""))
    s = re.sub(r"[\s]+", "-", s)
    s = re.sub(r"-{2,}", "-", s)[:maxlen].strip("-.")
    reserved = {"con", "prn", "aux", "nul", "com1", "com2", "lpt1"}
    if not s or s.lower() in reserved:
        return default
    return s


def _inside(root, rel):
    """Absolute path for `rel` under `root`, or ToolError if it escapes.

    The model supplies these paths (from a chat page), so every one is checked:
    no absolute paths, no drive letters, no '..' climb-out after resolution.
    """
    rel = str(rel or "").replace("\\", "/").strip()
    if not rel:
        raise ToolError("empty path")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise ToolError(f"'{rel}' is absolute - use a path relative to the workspace")
    cand = os.path.realpath(os.path.join(root, *rel.split("/")))
    root_r = os.path.realpath(root)
    if cand != root_r and not cand.startswith(root_r + os.sep):
        raise ToolError(f"'{rel}' escapes the workspace folder")
    return cand


def _sha(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


def _size(text):
    """A script's size in BYTES - the unit Luau's `#s` counts, so a size read
    here matches the same size read in Studio. Python's len() counts code
    points, which is smaller for any emoji, accent or en-dash in the source;
    mixing the two made freshly written scripts look "changed" on disk."""
    return len((text or "").encode("utf-8"))


def _entry_size(entry):
    """A snapshot's size, tolerating index.json entries written before bytes."""
    return entry.get("bytes", entry.get("chars", 0))


def _read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _clip(text, tool):
    """Cap a reply, keeping the head (the model needs the start) and saving the
    whole thing to disk so a long report is still readable by the user."""
    text = text or ""
    if len(text) <= MAX_RESULT_CHARS:
        return text
    try:
        os.makedirs(LAST_OUTPUT_DIR, exist_ok=True)
        path = os.path.join(LAST_OUTPUT_DIR, f"{_slug(tool, 'tool')}.txt")
        _write_text(path, text)
        where = f" (full {len(text)} chars saved to {path})"
    except OSError:
        where = ""
    return text[:MAX_RESULT_CHARS] + f"\n…[output cut at {MAX_RESULT_CHARS} chars{where}]"


def _as_list(value):
    """Models send a single string where an array was asked for, constantly."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v is not None]
    return [value]


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _as_int(value, default, lo=None, hi=None):
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


# ══════════════════════════════════════════════════════════════════════════
#  LUAU STATIC PRE-FLIGHT (luau_check) - no Studio needed
# ══════════════════════════════════════════════════════════════════════════
# A round trip to Studio to discover a missing `end` costs a whole model turn.
# Almost every syntax mistake this pipeline sees is mechanical, and all of it is
# detectable here in microseconds - so writes are linted BEFORE they go anywhere
# and the model gets "line 42: 'end' expected" instead of a Studio stack trace.

# Characters a chat editor silently substitutes (curly quotes, long dashes,
# non-breaking spaces). Inside a string they are harmless; in CODE position they
# make Studio reject the whole script with a useless "Failed to parse command
# code". This is the single most reported failure mode in CHANGELOG.md, so it is
# checked first and reported as an error, not a warning.
BAD_UNICODE = {
    "\u2018": "curly single quote '", "\u2019": "curly single quote '",
    "\u201c": 'curly double quote "', "\u201d": 'curly double quote "',
    "\u2013": "long dash --", "\u2014": "long dash --", "\u2026": "ellipsis ...",
    "\u00a0": "non-breaking space", "\u2028": "line separator",
    "\uff02": "fullwidth quote", "\uff07": "fullwidth quote",
}

# Services Roblox actually has, for the "did you mean" spelling check below.
SERVICES = {
    "Workspace", "Players", "ReplicatedStorage", "ReplicatedFirst", "ServerStorage",
    "ServerScriptService", "StarterPlayer", "StarterGui", "StarterPack", "StarterChat",
    "Lighting", "SoundService", "Chat", "TextChatService", "Teams", "HttpService",
    "TweenService", "RunService", "UserInputService", "Debris", "Selection", "Studio",
    "StudioService", "CoreGui", "LogService", "MarketplaceService", "TeleportService",
    "DataStoreService", "MessagingService", "PathfindingService", "PhysicsService",
    "PolicyService", "ProximityPromptService", "SocialService", "CollectionService",
    "ContentProvider", "GroupService", "GuiService", "HapticService", "InsertService",
    "JointsService", "KeyframeSequenceProvider", "LocalizationService", "NetworkClient",
    "PointsService", "RbxAnalyticsService", "RemoteEvent", "ReplicatedScriptService",
    "RobloxReplicatedStorage", "ScriptContext", "ScriptService", "TestService",
    "TimerService", "ToolboxService", "TouchInputService", "VRService", "VirtualUser",
    "Visit", "VoiceChatService", "AssetService", "BadgeService", "BrowserService",
    "EventIngestService", "FacialAnimationStreamingService", "FriendService",
    "GamepadService", "Geometry", "KeyboardService", "MemoryStoreService",
    "MouseService", "PermissionsService", "PlayerEmulatorService", "SessionService",
    "Stats", "TaskScheduler", "ThirdPartyUserService", "TracerService",
    "UGCValidationService", "UnvalidatedAssetService", "UserService", "VersionControlService",
    "VirtualInputManager", "ClusterPacketCache", "AnalyticsService", "SolidModelContentProvider",
    "MeshContentProvider", "LanguageService", "LuauScriptAnalyzer", "ScriptEditorService",
    "RenderSettings", "NetworkSettings", "PackageService", "StopWatchReporter",
    "DraftsService", "DebuggerManager", "RuntimeScriptService", "SharedTableRegistry",
    "ShutdownService", "SpawnerService", "TemporaryScriptService", "CookiesService",
    "CrossDMScriptChangeListener", "DeviceIdiomService", "FacialAnimationRecordingService",
    "FlagStandService", "FlyweightService", "GoogleAnalyticsConfiguration", "Hopper",
    "LocalStorageService", "LogService", "LuaWebService", "MemStorageService",
    "NetworkReplicator", "NotificationService", "PackageUIService", "ProcessInstancePhysicsService",
    "RemoteDebuggerServer", "RobloxPluginGuiService", "ServiceVisibilityService",
    "StarterPlayerScripts", "StarterCharacterScripts", "StarterPlayerScriptsService",
    "TextChatService", "AssetCounterService", "BulkImportService", "CacheableContentProvider",
    "DataModelPatchService", "DebuggerConnectionManager", "DebuggerUIService",
    "EventIngestService", "HttpRbxApiService", "InternalContainer", "CSGDictionaryService",
    "NonReplicatedCSGDictionaryService", "ServiceReplicationCache", "StudioData",
    "StudioDeviceEmulatorService", "StudioHighDpi", "TemporaryCageMeshProviderService",
}

_BLOCK_OPEN = re.compile(
    r"\b(function|if|elseif|else|then|for|while|do|repeat|until|end)\b|([(){}\[\]])")


# Tokens after which Luau requires an EXPRESSION rather than a statement - the
# only places an `if` can be an if-EXPRESSION (`local b = if x then 1 else 2`),
# which is closed by its `else` and has no `end` at all.
_EXPR_BEFORE_CHARS = "=+-*/%^<>,([{"
# `while`/`elseif`/`until` take a CONDITION and `in` an expression list, so an
# expression always follows them - an `if` there is an if-expression whatever the
# enclosing block is.
_EXPR_BEFORE_WORDS = ("return", "and", "or", "not", "until", "in", "while", "elseif")
# `then`/`else` continue the block they belong to: what follows them is an
# expression only when THAT block is an if-expression (and nothing - no function,
# no do - has opened in between, because then statements are expected again).
_EXPR_BRANCH_WORDS = ("else", "then")


def _literal_flags(masked, spans):
    """A bytearray marking every position that is inside a string or comment.

    _mask() blanks a literal INCLUDING its quotes, so `local s = ""` leaves a
    bare `=` as the last visible character - and an `if` on the next line would
    look like the right-hand side of an assignment. The flags restore the fact
    that a value already sits there."""
    flags = bytearray(len(masked))
    for _kind, start, end, _closed in spans:
        for i in range(max(0, start), min(len(masked), end)):
            flags[i] = 1
    return flags


def _next_significant(masked, pos, literals=None):
    """Index of the next character that is neither blank nor part of a literal,
    or None when a string/comment comes first.

    Stopping AT a literal matters: an if-expression's else-branch is a value, so
    `... else "text"` followed two lines later by a statement `if` must not read
    as `else if` - the masked-out string is exactly what is between them."""
    i = pos
    while i < len(masked):
        if literals is not None and literals[i]:
            return None
        if masked[i] not in " \t\r\n":
            return i
        i += 1
    return None


def _expr_expected(masked, pos, stack, literals=None):
    """True when the token before `pos` means an expression must follow.

    Without this, every if-expression reads as a statement whose `end` never
    arrives, and a script using one is refused for a block that is not open."""
    i = pos - 1
    while i >= 0:
        # Check the literal flag BEFORE skipping blanks: _mask() turns a string
        # into spaces, so testing afterwards would look straight through
        # `local s = ""` and see the `=` behind it.
        if literals is not None and literals[i]:
            return False                   # a value ends here, not an operator
        if masked[i] not in " \t\r\n":
            break
        i -= 1
    if i < 0:
        return False                       # start of the file: a statement
    if masked[i] in _EXPR_BEFORE_CHARS:     # also covers the tail of ~= <= .. and
        return True
    for word in _EXPR_BRANCH_WORDS + _EXPR_BEFORE_WORDS:
        start = i - len(word) + 1
        if start < 0 or masked[start:i + 1] != word:
            continue
        if not (start == 0 or not (masked[start - 1].isalnum() or masked[start - 1] == "_")):
            continue                        # a longer identifier ending in the word
        if word in _EXPR_BRANCH_WORDS:
            return bool(stack) and stack[-1][0] == "ifexpr"
        return True
    return False


def _long_bracket_level(text, i):
    """If text[i:] starts a long bracket ([==[), return its '=' count, else None."""
    if i >= len(text) or text[i] != "[":
        return None
    j = i + 1
    while j < len(text) and text[j] == "=":
        j += 1
    if j < len(text) and text[j] == "[":
        return j - i - 1
    return None


def _blank(text):
    return "".join("\n" if c == "\n" else " " for c in text)


def _mask(code):
    """Replace string/comment CONTENTS with spaces, keeping positions and line
    breaks, so structure can be analysed without matching inside literals.
    Returns (masked, spans) where spans are (kind, start, end, closed)."""
    out = []
    spans = []
    i, n = 0, len(code)
    while i < n:
        ch = code[i]
        if ch == "-" and code.startswith("--", i):
            j = i + 2
            lvl = _long_bracket_level(code, j)
            if lvl is not None:
                close = "]" + "=" * lvl + "]"
                end = code.find(close, j + lvl + 2)
                j = n if end == -1 else end + len(close)
                spans.append(("comment", i, j, end != -1))
            else:
                end = code.find("\n", j)
                j = n if end == -1 else end
                spans.append(("comment", i, j, True))
            out.append(_blank(code[i:j]))
            i = j
        elif ch in "\"'":
            j, closed = i + 1, False
            while j < n:
                if code[j] == "\\":
                    j += 2
                    continue
                if code[j] == ch:
                    j += 1
                    closed = True
                    break
                if code[j] == "\n":
                    break
                j += 1
            spans.append(("string", i, j, closed))
            out.append(_blank(code[i:j]))
            i = j
        else:
            lvl = _long_bracket_level(code, i)
            if lvl is not None:
                close = "]" + "=" * lvl + "]"
                end = code.find(close, i + lvl + 2)
                j = n if end == -1 else end + len(close)
                spans.append(("longstring", i, j, end != -1))
                out.append(_blank(code[i:j]))
                i = j
            else:
                out.append(ch)
                i += 1
    return "".join(out), spans


def _line_index(code):
    starts = [0]
    for m in re.finditer("\n", code):
        starts.append(m.end())
    return starts


def _line_of(starts, pos):
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= pos:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def _levenshtein(a, b, cap=3):
    """Edit distance, abandoning early past `cap` (only used for suggestions)."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            best = min(best, cur[j])
        if best > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _suggest_service(name):
    low = name.lower()
    best, dist = None, 3
    for svc in SERVICES:
        d = _levenshtein(low, svc.lower())
        if d < dist:
            best, dist = svc, d
    return best


def _matching_paren(masked, open_pos):
    """Index just past the ')' matching the '(' at open_pos, or -1."""
    depth = 0
    for j in range(open_pos, len(masked)):
        if masked[j] == "(":
            depth += 1
        elif masked[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
    return -1


def luau_check(code, name="<code>", context="script"):
    """Static pre-flight over one Luau source. Returns a list of findings:
    {"severity": "error"|"warn"|"info", "line": int, "message": str}.

    Deliberately conservative: only report things that are wrong in EVERY
    context (unbalanced blocks, corrupted punctuation) or that this pipeline has
    actually seen break runs (blocking WaitForChild, unparented instances,
    deprecated waits). A linter that cries wolf gets ignored by the model.
    """
    code = code or ""
    findings = []
    add = lambda sev, line, msg: findings.append(  # noqa: E731
        {"severity": sev, "line": line, "message": msg})
    starts = _line_index(code)
    line_of = lambda pos: _line_of(starts, pos)  # noqa: E731
    masked, spans = _mask(code)

    # 1. Chat-corrupted punctuation in CODE position (see BAD_UNICODE).
    for i, ch in enumerate(masked):
        if ch in BAD_UNICODE:
            add("error", line_of(i),
                f"{BAD_UNICODE[ch]} (U+{ord(ch):04X}) in code position - a chat editor replaced an ASCII "
                f"character here. Studio rejects the whole script; retype it as plain ASCII.")
    # An unterminated long comment/string usually means the same corruption.
    for kind, start, _end, closed in spans:
        if not closed:
            add("error", line_of(start),
                f"unterminated {kind} starting here - the closing delimiter is missing "
                f"(a long dash or curly quote from the chat is the usual cause).")

    # 2. Block structure + bracket balance.
    stack = []          # (kind, pos); kind "ifexpr" is an if-EXPRESSION (no 'end')
    pending_loop = []   # 'for'/'while' waiting for their 'do'
    parens = []
    literals = _literal_flags(masked, spans)
    else_leads_to_ifexpr = False   # set by an if-expression's 'else' when an
                                   # if-expression follows it directly
    for m in _BLOCK_OPEN.finditer(masked):
        word, bracket = m.group(1), m.group(2)
        pos = m.start()
        if bracket:
            if bracket in "([{":
                parens.append((bracket, pos))
            else:
                closer = {")": "(", "]": "[", "}": "{"}[bracket]
                if not parens or parens[-1][0] != closer:
                    add("error", line_of(pos),
                        f"'{bracket}' does not close anything here "
                        + (f"- the '{parens[-1][0]}' opened on line {line_of(parens[-1][1])} is still open."
                           if parens else "- there is no matching opener."))
                else:
                    parens.pop()
            continue
        if word == "function":
            stack.append(("function", pos))
        elif word == "if":
            # An if-expression is closed by its `else` and never sees an `end`,
            # so it must not be pushed as a block that one is expected for.
            if else_leads_to_ifexpr or _expr_expected(masked, pos, stack, literals):
                stack.append(("ifexpr", pos))
            else:
                stack.append(("if", pos))
            else_leads_to_ifexpr = False
        elif word in ("for", "while"):
            pending_loop.append((word, pos))
        elif word == "do":
            if pending_loop:
                kind, lpos = pending_loop.pop()
                stack.append((kind, lpos))
            else:
                stack.append(("do", pos))
        elif word == "else":
            # An if-expression ENDS at its `else` (the else-branch is a plain
            # expression, so nothing is left to close). `else if ...` continuing
            # the chain is an if-expression too - flag it, because by the time
            # that `if` is reached the block it would inherit from is gone.
            if stack and stack[-1][0] == "ifexpr":
                stack.pop()
                nxt = _next_significant(masked, m.end(), literals)
                after = masked[nxt + 2:nxt + 3] if nxt is not None else ""
                else_leads_to_ifexpr = (nxt is not None and masked.startswith("if", nxt)
                                        and not (after.isalnum() or after == "_"))
        elif word == "repeat":
            stack.append(("repeat", pos))
        elif word == "until":
            if stack and stack[-1][0] == "repeat":
                stack.pop()
            else:
                add("error", line_of(pos), "'until' without a matching 'repeat'.")
        elif word == "end":
            # An if-expression still open here never reached its `else`.
            while stack and stack[-1][0] == "ifexpr":
                _kind, ipos = stack.pop()
                add("error", line_of(ipos),
                    "if-EXPRESSION opened here never reaches its 'else' - an if used as a "
                    "value (local b = if x then 1 else 2) needs both branches and takes no 'end'.")
            if not stack:
                add("error", line_of(pos),
                    "extra 'end' - nothing is open here (an 'end' too many, or a block closed twice).")
            else:
                kind, opos = stack.pop()
                if kind == "repeat":
                    add("error", line_of(pos),
                        f"'end' closes a 'repeat' block opened on line {line_of(opos)} - use 'until <condition>' instead.")
    for kind, pos in stack:
        if kind == "ifexpr":
            add("error", line_of(pos),
                "if-EXPRESSION opened here never reaches its 'else' - an if used as a value "
                "(local b = if x then 1 else 2) needs both branches and takes no 'end'.")
            continue
        label = {"function": "function", "if": "if/then", "do": "do block",
                 "for": "for loop", "while": "while loop", "repeat": "repeat"}.get(kind, kind)
        closer = "until <condition>" if kind == "repeat" else "'end'"
        add("error", line_of(pos),
            f"unclosed '{label}' opened here - it is never closed by {closer}.")
    for kind, pos in pending_loop:
        add("error", line_of(pos), f"'{kind}' loop opened here never reaches its 'do'.")
    for kind, pos in parens:
        add("error", line_of(pos), f"unclosed '{kind}' opened here.")

    # 3. Blocking calls (the #1 cause of execute_luau timeouts and frozen games).
    for m in re.finditer(r"\bWaitForChild\s*\(", masked):
        end = _matching_paren(masked, m.end() - 1)
        inner = masked[m.end():(end - 1 if end > 0 else len(masked))]
        if "," not in inner:
            add("warn", line_of(m.start()),
                "WaitForChild with no timeout blocks forever if the child never appears "
                "(inside execute_luau it burns the whole 20s budget). Add one: WaitForChild(\"X\", 5).")
    for m in re.finditer(r"(?<![\w.:])wait\s*\(", masked):
        add("warn", line_of(m.start()),
            "global wait() is deprecated and yields the calling thread - use task.wait(n). "
            "Inside execute_luau it also eats the 20s budget.")
    for m in re.finditer(r"(?<![\w.:])(spawn|delay)\s*\(", masked):
        add("warn", line_of(m.start()), f"{m.group(1)}() is deprecated - use task.{m.group(1)}(...).")
    for m in re.finditer(r":connect\s*\(", code):
        add("warn", line_of(m.start()), "lowercase :connect( is deprecated - use :Connect(.")

    # 4. Infinite loops with no yield: freeze the game (or the 20s Luau budget).
    for m in re.finditer(r"\bwhile\s+(true|1)\s+do\b", masked):
        end = _matching_end(masked, m.end())
        body = masked[m.end():(end if end > 0 else len(masked))]
        if not re.search(r"\b(task\.wait|task\.spawn|wait\s*\(|:Wait\s*\(|:WaitAsync|coroutine\.yield)", body):
            add("warn", line_of(m.start()),
                "'while true do' with no wait/yield inside spins forever - it freezes the game "
                "(or times out execute_luau). Add task.wait(...) or an exit condition.")

    # 5. Instances that are never parented are garbage collected: the classic
    #    "the AI said it created the part but nothing is there".
    if re.search(r"\bInstance\.new\s*\(", masked) and not re.search(r"\.Parent\s*=", masked):
        add("warn", line_of(_first(masked, r"\bInstance\.new\s*\(")),
            "Instance.new is used but nothing sets .Parent - an unparented instance is garbage "
            "collected and never shows up in the game. Parent it (or return it).")

    # 6. print() output is not captured by execute_luau (only `return` is).
    if context == "execute" and re.search(r"(?<![\w.:])print\s*\(", masked):
        add("info", line_of(_first(masked, r"(?<![\w.:])print\s*\(")),
            "print() is not captured by execute_luau - use `return` for anything you want back.")

    # 7. Misspelled service names (GetService fails at RUNTIME, not compile time,
    #    so Studio's own check never catches these).
    for m in re.finditer(r"\bGetService\s*\(\s*[\"']([^\"']+)[\"']\s*\)", code):
        svc = m.group(1)
        if svc not in SERVICES:
            hint = _suggest_service(svc)
            add("warn", line_of(m.start(1)),
                f'GetService("{svc}") is not a known Roblox service'
                + (f' - did you mean "{hint}"?' if hint else " - check the spelling.")
                + " GetService fails at runtime, so nothing else will catch this.")

    findings.sort(key=lambda f: (f["line"], {"error": 0, "warn": 1, "info": 2}[f["severity"]]))
    return findings


def _first(text, pattern):
    m = re.search(pattern, text)
    return m.start() if m else 0


def _matching_end(masked, from_pos):
    """Position just past the 'end' that closes the block whose head ended at
    from_pos, or -1. Openers are counted as function / if / do / repeat and
    closers as end / until: 'for' and 'while' are deliberately NOT counted
    because the 'do' that follows them already is, and counting both would make
    every loop one level too deep."""
    depth = 1
    for m in re.finditer(r"\b(function|if|do|repeat|until|end)\b", masked[from_pos:]):
        depth += 1 if m.group(1) in ("function", "if", "do", "repeat") else -1
        if depth <= 0:
            return from_pos + m.end()
    return -1


def format_findings(findings, label=None, head=None):
    """Human/model-readable lint report."""
    errors = [f for f in findings if f["severity"] == "error"]
    warns = [f for f in findings if f["severity"] == "warn"]
    infos = [f for f in findings if f["severity"] == "info"]
    where = f" in {label}" if label else ""
    if not findings:
        return f"OK{where}: no problems found ({_plural(len((head or '').splitlines()), 'line')} checked)."
    lines = [f"{'ERROR' if errors else 'WARN'}{where}: "
             f"{len(errors)} error(s), {len(warns)} warning(s), {len(infos)} note(s)"]
    for f in findings:
        lines.append(f"  {f['severity'].upper():5} line {f['line']}: {f['message']}")
    if errors:
        lines.append("Fix the errors before writing this to Studio (or pass \"check\": false / \"force\": true to write it anyway).")
    return "\n".join(lines)


def check_many(files, context="script"):
    """files: [{"path":..., "code":...}] -> (findings_by_path, all_findings)"""
    per = {}
    for f in files:
        path = str(f.get("path") or f.get("file") or "<code>")
        per[path] = luau_check(f.get("code") or "", path, context)
    return per, [x for v in per.values() for x in v]


# ══════════════════════════════════════════════════════════════════════════
#  STUDIO SIDE: the Luau the Dev Kit runs through execute_luau
# ══════════════════════════════════════════════════════════════════════════
# Everything the Dev Kit asks Studio to do is ONE execute_luau call that returns
# ONE plain string in a tiny line protocol:
#
#     ZS-<KIND> 1
#     <TAB separated fields>
#     ...
#     END
#
# A returned STRING (never a table) is used on purpose: it is the one thing every
# Studio MCP build serialises predictably, so no JSON-vs-Lua-dump guessing. Bulk
# data travelling IN is length-prefixed bytes; bulk data travelling OUT is
# base64 (buffer.tobase64) with a raw length-prefixed fallback, because Luau's
# `#` counts bytes while Python's len() counts characters and a source file full
# of unicode would otherwise desynchronise the parse.

_LUA_PRELUDE = r'''-- ZeroScript Dev Kit helpers (bridge-generated; runs inside execute_luau)
local function zs_split(path)
  local parts = {}
  for seg in string.gmatch(path or "", "[^%.]+") do
    if seg ~= "" then parts[#parts + 1] = seg end
  end
  return parts
end

local function zs_root(seg)
  local low = string.lower(seg or "")
  if low == "game" or low == "datamodel" then return game end
  local ok, svc = pcall(function() return game:GetService(seg) end)
  if ok and svc then return svc end
  return game:FindFirstChild(seg)
end

local function zs_find(path)
  local parts = zs_split(path)
  if #parts == 0 then return nil, "empty path" end
  local cur = zs_root(parts[1])
  if not cur then
    return nil, "cannot resolve '" .. tostring(parts[1]) .. "' (start a path with game or a service name)"
  end
  for i = 2, #parts do
    local nxt = cur:FindFirstChild(parts[i])
    if not nxt then return nil, "no child '" .. parts[i] .. "' under " .. cur:GetFullName() end
    cur = nxt
  end
  return cur
end

-- Walk a dot path, creating what is missing: Folders for every intermediate
-- segment, `leafClass` for the last one. Returns inst, err, createdLeaf.
local function zs_ensure(path, leafClass)
  local parts = zs_split(path)
  if #parts < 2 then
    return nil, "a path needs a container and a name, e.g. game.ServerScriptService.Main"
  end
  local cur = zs_root(parts[1])
  if not cur then return nil, "cannot resolve '" .. tostring(parts[1]) .. "'" end
  local created = false
  for i = 2, #parts do
    local isLeaf = (i == #parts)
    local nxt = cur:FindFirstChild(parts[i])
    if not nxt then
      local class = "Folder"
      if isLeaf then class = leafClass or "Script" end
      local okNew, inst = pcall(function() return Instance.new(class) end)
      if not okNew or not inst then
        return nil, "cannot create a " .. class .. " named '" .. parts[i] .. "' (" .. tostring(inst) .. ")"
      end
      inst.Name = parts[i]
      inst.Parent = cur
      nxt = inst
      if isLeaf then created = true end
    elseif isLeaf and inst.ClassName ~= (leafClass or "Script") and inst:IsA("LuaSourceContainer") then
      -- an existing script of another kind (LocalScript vs Script): keep it, the
      -- caller reports the class it found rather than silently swapping types
      created = false
    end
    cur = nxt
  end
  return cur, nil, created
end

local function zs_out(kind)
  local lines = {}
  local say = function(s) lines[#lines + 1] = tostring(s) end
  local finish = function()
    return table.concat({ "ZS-" .. kind .. " 1", table.concat(lines, "\n"), "END" }, "\n")
  end
  return say, finish
end

-- Length-prefixed record payload:
--   PATH\t<dot path>\n[CLASS\t<class>\n]CODE\t<byte count>\n<bytes>\n ... END\n
local function zs_records(payload)
  local out = {}
  local i, n = 1, #payload
  local function line()
    local e = string.find(payload, "\n", i, true)
    if not e then e = n + 1 end
    local s = string.sub(payload, i, e - 1)
    i = e + 1
    return s
  end
  while i <= n do
    local head = line()
    if head == "END" then break end
    local path = string.match(head, "^PATH\t(.*)$")
    if path then
      local rec = { path = path, class = "Script", code = "" }
      for _ = 1, 2 do
        local h = line()
        local k, v = string.match(h, "^([A-Z]+)\t(.*)$")
        if k == "CLASS" and v ~= "" then rec.class = v end
        if k == "CODE" then
          local clen = tonumber(v)
          if clen and clen > 0 then
            rec.code = string.sub(payload, i, i + clen - 1)
            i = i + clen
            if string.sub(payload, i, i) == "\n" then i = i + 1 end
          end
          break
        end
      end
      out[#out + 1] = rec
    end
  end
  return out
end

local function zs_first_line(src)
  for l in string.gmatch(src or "", "[^\n]+") do
    local t = string.gsub(l, "^%s+", "")
    t = string.gsub(t, "%s+$", "")
    if t ~= "" and not string.find(t, "^%-%-") then
      t = string.gsub(t, "[\t\r]", " ")
      if #t > 90 then t = string.sub(t, 1, 87) .. "..." end
      return t
    end
  end
  return ""
end

local function zs_encode(src)
  local ok, b64 = pcall(function() return buffer.tobase64(buffer.fromstring(src)) end)
  if ok and type(b64) == "string" then return "B64", b64 end
  return "RAW", src
end

-- Containers that are Studio/Roblox internals: listing them buries the user's
-- own tree under hundreds of noise lines.
local ZS_SKIP = {
  CoreGui = true, CorePackages = true, RobloxReplicatedStorage = true,
  InternalContainer = true, CSGDictionaryService = true,
  NonReplicatedCSGDictionaryService = true, Terrain = true,
  ServiceVisibilityService = true, StudioData = true,
}
'''


def _lua_long(text):
    """A Luau long-bracket string literal holding `text` verbatim (no escapes).
    The level is raised until the content cannot contain the closing sequence.
    A newline is placed right after the opener because Luau drops exactly one
    there, which keeps the payload byte-aligned."""
    level = 2
    while True:
        close = "]" + "=" * level + "]"
        if close not in text and not text.startswith("="):
            break
        level += 1
    open_br = "[" + "=" * level + "["
    return open_br + "\n" + text.lstrip("\n") + close


def _pack_records(records):
    """records: [{"path":..., "class":..., "code":...}] -> the payload string
    zs_records() reads. Byte lengths are computed on the UTF-8 encoding because
    Luau's string.sub is byte based."""
    out = bytearray()
    for r in records:
        path = re.sub(r"[\t\r\n]+", " ", str(r.get("path") or ""))
        cls = re.sub(r"[\t\r\n]+", " ", str(r.get("class") or r.get("className") or "Script"))
        code = (r.get("code") or "").replace("\r\n", "\n")
        data = code.encode("utf-8")
        out += f"PATH\t{path}\n".encode("utf-8")
        out += f"CLASS\t{cls}\n".encode("utf-8")
        out += f"CODE\t{len(data)}\n".encode("utf-8")
        out += data
        out += b"\n"
    out += b"END\n"
    return out.decode("utf-8")


def _snippet(kind, body, payload=None, **subs):
    """Prelude + body, with @NAME@ placeholders replaced and the optional record
    payload inlined as a long-bracket literal named PAYLOAD."""
    code = _LUA_PRELUDE + "\n" + body
    code = code.replace("@PAYLOAD@", _lua_long(payload or ""))
    for k, v in subs.items():
        # Luau spells booleans in lower case; str(True) would leave a nil global
        # behind a flag name and silently disable the feature.
        if isinstance(v, bool):
            v = "true" if v else "false"
        code = code.replace("@" + k.upper() + "@", str(v))
    return code


# ── the snippets ───────────────────────────────────────────────────────────
_LUA_PLACE = r'''
local say, finish = zs_out("PLACE")
say("PLACE\t" .. tostring(game.Name))
local state = "Edit"
if game:GetService("RunService"):IsRunning() then state = "Running" end
say("RUN\t" .. state)
return finish()
'''

_LUA_INVENTORY = r'''
local say, finish = zs_out("INVENTORY")
local MAX_DEPTH = @MAX_DEPTH@
local MAX_NODES = @MAX_NODES@
local WANT_SCRIPTS = @WANT_SCRIPTS@
local WANT_TREE = @WANT_TREE@
say("PLACE\t" .. tostring(game.Name))
local okRun, running = pcall(function() return game:GetService("RunService"):IsRunning() end)
say("RUN\t" .. ((okRun and running) and "Playing" or "Edit"))
local root, rerr = zs_find("@ROOT@")
if not root then
  say("FATAL\t" .. tostring(rerr))
  return finish()
end
local nodes, scripts, truncated = 0, 0, false
local slines = {}
local function walk(inst, depth, indent)
  local okKids, kids = pcall(function() return inst:GetChildren() end)
  if not okKids then return end
  table.sort(kids, function(a, b) return a.Name < b.Name end)
  for _, c in ipairs(kids) do
    if not ZS_SKIP[c.Name] and not ZS_SKIP[c.ClassName] then
      nodes = nodes + 1
      if nodes > MAX_NODES then
        truncated = true
        return
      end
      local label = indent .. c.Name .. " (" .. c.ClassName .. ")"
      if c:IsA("LuaSourceContainer") then
        local okS, src = pcall(function() return c.Source or "" end)
        local size = okS and #src or -1
        scripts = scripts + 1
        label = label .. " [" .. size .. " bytes]"
        if WANT_SCRIPTS then
          local full = "game." .. c:GetFullName()
          slines[#slines + 1] = "S\t" .. full .. "\t" .. c.ClassName .. "\t" .. size .. "\t" .. zs_first_line(okS and src or "")
        end
      end
      if WANT_TREE then say("T\t" .. label) end
      if depth < MAX_DEPTH then walk(c, depth + 1, indent .. "  ") end
      if truncated then return end
    end
  end
end
walk(root, 0, "")
for _, l in ipairs(slines) do say(l) end
say("N\t" .. nodes .. "\t" .. scripts .. "\t" .. tostring(truncated))
return finish()
'''

_LUA_READ = r'''
local say, finish = zs_out("SOURCES")
local recs = zs_records(@PAYLOAD@)
say("PLACE\t" .. tostring(game.Name))
for _, rec in ipairs(recs) do
  local inst, err = zs_find(rec.path)
  if not inst then
    say("MISS\t" .. rec.path .. "\t" .. tostring(err))
  elseif not inst:IsA("LuaSourceContainer") then
    say("MISS\t" .. rec.path .. "\tnot a script (" .. inst.ClassName .. ")")
  else
    local okS, src = pcall(function() return inst.Source or "" end)
    if not okS then
      say("MISS\t" .. rec.path .. "\tcannot read Source (" .. tostring(src) .. ")")
    else
      local enc, data = zs_encode(src)
      say("FILE\tgame." .. inst:GetFullName() .. "\t" .. inst.ClassName .. "\t" .. enc .. "\t" .. #src .. "\t" .. #data)
      say(data)
    end
  end
end
return finish()
'''

_LUA_WRITE = r'''
local say, finish = zs_out("WRITE")
local recs = zs_records(@PAYLOAD@)
local wantSelect = @SELECT@
say("PLACE\t" .. tostring(game.Name))
local sel = {}
for _, rec in ipairs(recs) do
  local inst, err, created = zs_ensure(rec.path, rec.class)
  if not inst then
    say("FAIL\t" .. rec.path .. "\t" .. tostring(err))
  elseif not inst:IsA("LuaSourceContainer") then
    say("FAIL\t" .. rec.path .. "\talready exists as a " .. inst.ClassName .. ", not a script - pick another name")
  else
    local okR, old = pcall(function() return inst.Source or "" end)
    if not okR then
      say("FAIL\t" .. rec.path .. "\tcannot read the current Source (" .. tostring(old) .. ")")
    elseif old == rec.code then
      say("OK\tunchanged\t" .. rec.path .. "\t" .. inst.ClassName .. "\t" .. #old .. "\t" .. #old)
      sel[#sel + 1] = inst
    else
      local okW, werr = pcall(function() inst.Source = rec.code end)
      if not okW then
        say("FAIL\t" .. rec.path .. "\tcannot write Source (" .. tostring(werr) .. ")")
      else
        local okB, back = pcall(function() return inst.Source or "" end)
        if okB and back == rec.code then
          say("OK\t" .. (created and "created" or "updated") .. "\t" .. rec.path .. "\t" .. inst.ClassName .. "\t" .. #old .. "\t" .. #back)
          sel[#sel + 1] = inst
        else
          local got = okB and tostring(#back) or "unreadable"
          say("FAIL\t" .. rec.path .. "\tthe Source write did not stick (read back " .. got .. " of " .. #rec.code .. " bytes)")
        end
      end
    end
  end
end
if wantSelect and #sel > 0 then
  local okSel, selErr = pcall(function() game:GetService("Selection"):Set(sel) end)
  if okSel then say("SEL\t" .. #sel) else say("SELFAIL\t" .. tostring(selErr)) end
end
return finish()
'''

_LUA_VERIFY = r'''
local say, finish = zs_out("VERIFY")
local recs = zs_records(@PAYLOAD@)
local MAX = @MAX@
say("PLACE\t" .. tostring(game.Name))
if type(loadstring) ~= "function" then
  say("NOLOAD\tloadstring is unavailable in this context, so scripts cannot be compiled here")
  return finish()
end
local targets = {}
if #recs > 0 then
  for _, rec in ipairs(recs) do
    local inst, err = zs_find(rec.path)
    if not inst then
      say("SKIP\t" .. rec.path .. "\t" .. tostring(err))
    elseif inst:IsA("LuaSourceContainer") then
      targets[#targets + 1] = inst
    else
      say("SKIP\t" .. rec.path .. "\tnot a script (" .. inst.ClassName .. ")")
    end
  end
else
  local okD, desc = pcall(function() return game:GetDescendants() end)
  if okD then
    for _, d in ipairs(desc) do
      if d:IsA("LuaSourceContainer") and not ZS_SKIP[d.ClassName] and not ZS_SKIP[d.Name] then
        targets[#targets + 1] = d
      end
    end
  end
end
local checked, bad = 0, 0
for _, inst in ipairs(targets) do
  if checked >= MAX then
    say("CAP\t" .. #targets .. "\t" .. MAX)
    break
  end
  local okS, src = pcall(function() return inst.Source or "" end)
  if okS and src ~= "" then
    checked = checked + 1
    local fn, cerr = loadstring(src)
    if not fn then
      bad = bad + 1
      say("FAIL\tgame." .. inst:GetFullName() .. "\t" .. inst.ClassName .. "\t" .. tostring(cerr))
    end
  end
end
say("N\t" .. #targets .. "\t" .. checked .. "\t" .. bad)
return finish()
'''

_LUA_VIEW = r'''
local say, finish = zs_out("VIEW")
local recs = zs_records(@PAYLOAD@)
local FOCUS = "@FOCUS@"
local wantClear = @CLEAR@
local Selection = nil
pcall(function() Selection = game:GetService("Selection") end)
say("PLACE\t" .. tostring(game.Name))
if wantClear and Selection then
  local okC, errC = pcall(function() Selection:Set({}) end)
  say(okC and "OK\tselection cleared" or ("FAIL\tclear: " .. tostring(errC)))
end
local sel, names, missing = {}, {}, {}
for _, rec in ipairs(recs) do
  local inst, err = zs_find(rec.path)
  if inst then
    sel[#sel + 1] = inst
    names[#names + 1] = inst:GetFullName()
  else
    missing[#missing + 1] = rec.path .. " (" .. tostring(err) .. ")"
  end
end
if Selection and #sel > 0 then
  local okS, errS = pcall(function() Selection:Set(sel) end)
  if okS then
    say("OK\tselected " .. #sel .. ": " .. table.concat(names, ", "))
  else
    say("FAIL\tselection: " .. tostring(errS))
  end
end
for _, m in ipairs(missing) do say("MISS\t" .. m) end
if FOCUS ~= "" then
  local inst, err = zs_find(FOCUS)
  if not inst then
    say("MISS\t" .. FOCUS .. " (" .. tostring(err) .. ")")
  else
    local cam = workspace:FindFirstChildOfClass("Camera")
    local okB, cf, size = pcall(function() return inst:GetBoundingBox() end)
    if not cam then
      say("MISS\tno editor camera to move")
    elseif not okB then
      say("FAIL\tcannot measure " .. FOCUS .. " (" .. tostring(cf) .. ")")
    else
      local dist = math.max((size and size.Magnitude or 10) * 1.5, 12)
      local okM, errM = pcall(function()
        cam.CameraType = Enum.CameraType.Scriptable
        cam.CFrame = CFrame.new(cf.Position + Vector3.new(dist * 0.7, dist * 0.55, dist * 0.85), cf.Position)
        cam.CameraType = Enum.CameraType.Custom
      end)
      say(okM and ("OK\tcamera framed " .. inst:GetFullName()) or ("FAIL\tcamera: " .. tostring(errM)))
    end
  end
end
return finish()
'''


# ══════════════════════════════════════════════════════════════════════════
#  STUDIO ADAPTER + REPORT PARSING
# ══════════════════════════════════════════════════════════════════════════
NO_STUDIO_HINT = (
    "Roblox Studio is not reachable right now (it is closed, has no place open, or its MCP server "
    "is disabled), so this command could not run. This is an environment problem on the user's "
    "machine, NOT your mistake. Tell the user in one short sentence to open their place in Roblox "
    "Studio and enable the MCP server (Assistant AI > ... > Manage MCP Servers), then wait for them. "
    "Commands that do not need Studio still work: luau_check, workspace (grep/status on an existing "
    "mirror) and script_history (list/diff)."
)
_NO_STUDIO_RE = re.compile(
    r"Unable to find an active Studio instance|previously active Studio has disconnected|"
    r"doesn't have a place open|no active studio|not connected to any studio", re.I)


class Studio:
    """The Dev Kit's only view of the bridge: call a Studio tool, ask whether a
    tool exists, and run Luau. Kept tiny so local_tools never imports bridge.py
    (which would be a circular import) and stays unit-testable with a stub."""

    def __init__(self, manager=None, call=None, has=None, budget=None):
        self._mgr = manager
        self._call = call
        self._has = has
        # One Dev Kit call may spend several Studio round trips, so it gets an
        # overall budget (the extension's own timeout for this command). Past it,
        # calls fail fast with a clear message instead of the extension giving up
        # on a bridge that is still happily working.
        self.deadline = time.monotonic() + budget if budget else None

    def remaining(self, default=None):
        """Seconds left in this call's budget (or `default` when unlimited)."""
        if self.deadline is None:
            return default if default is not None else STUDIO_TIMEOUT
        return max(1.0, self.deadline - time.monotonic())

    def expired(self):
        return self.deadline is not None and time.monotonic() >= self.deadline

    # -- raw ---------------------------------------------------------------
    def has(self, name):
        if self._has:
            return bool(self._has(name))
        if self._mgr is None:
            return False
        with self._mgr.index_lock:
            return name in self._mgr.index

    def call(self, name, arguments=None, timeout=STUDIO_TIMEOUT):
        """Returns {"ok": bool, "text": str, "error": str|None} - never raises."""
        if is_local(name):
            # Guard against a Dev Kit tool calling another one through here,
            # which would recurse straight back into MCPManager.call.
            return {"ok": False, "text": "",
                    "error": f"'{name}' is a ZeroScript command, not a Studio command"}
        if self.expired():
            return {"ok": False, "text": "",
                    "error": "this command ran out of its time budget before Studio answered "
                             "- ask for less in one call (fewer files, a smaller subtree)"}
        if timeout is None or timeout > self.remaining(timeout):
            timeout = self.remaining(timeout)
        if self._call is None and self._mgr is not None:
            def _call(n, a, t):
                try:
                    r = self._mgr.call(n, a, t)
                    return {"ok": True, "text": r.get("text") or "", "error": None}
                except Exception as e:  # noqa: BLE001 - every failure is data here
                    return {"ok": False, "text": "", "error": str(e)}
            self._call = _call
        if self._call is None:
            return {"ok": False, "text": "", "error": "no Studio connection is available"}
        try:
            r = self._call(name, arguments or {}, timeout)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "text": "", "error": str(e)}
        if not isinstance(r, dict):
            return {"ok": False, "text": "", "error": f"bad result from '{name}'"}
        return r

    # -- luau --------------------------------------------------------------
    def luau(self, code, timeout=STUDIO_TIMEOUT, datamodel="Edit"):
        """Run Luau in Studio and return its text. Raises ToolError (with a
        Studio-offline message when that is what happened) so callers stay short."""
        if self.expired():
            raise ToolError("this command ran out of its time budget - ask for less in one call "
                            "(fewer files, a smaller subtree, or a lower max_files).")
        if timeout is None or timeout > self.remaining(timeout):
            timeout = self.remaining(timeout)
        if not self.has("execute_luau"):
            raise ToolError(
                "the Roblox Studio MCP server does not advertise execute_luau, so the Dev Kit "
                "cannot run anything in Studio. Check that Studio is open with a place loaded "
                "and its MCP server enabled.")
        r = self.call("execute_luau", {"code": code, "datamodel_type": datamodel}, timeout)
        text = (r.get("text") or "").strip()
        if not r.get("ok"):
            err = r.get("error") or text or "no error message"
            if _NO_STUDIO_RE.search(err):
                raise ToolError(NO_STUDIO_HINT)
            raise ToolError(f"Studio refused the call: {err}")
        if _NO_STUDIO_RE.search(text) and not text.startswith("ZS-"):
            raise ToolError(NO_STUDIO_HINT)
        return text

    def tool(self, name, variants, timeout=STUDIO_TIMEOUT):
        """Call an MCP tool trying several argument shapes (Studio's schema has
        moved between builds; a required-param complaint is answered by the next
        variant). Returns (ok, text, error)."""
        if not self.has(name):
            return False, "", f"'{name}' is not available in this Studio MCP build"
        last = ""
        for args in variants:
            r = self.call(name, args, timeout)
            text = (r.get("text") or "").strip()
            if r.get("ok") and not re.match(r"^[\w .'\"-]{0,60}\bis (required|not available|invalid)\b", text):
                if _NO_STUDIO_RE.search(text):
                    raise ToolError(NO_STUDIO_HINT)
                return True, text, None
            last = r.get("error") or text or "unknown failure"
        return False, "", last


# ── report parsing ─────────────────────────────────────────────────────────
def parse_report(text, kind):
    """Lines of a ZS-<KIND> report, or ToolError explaining what Studio said
    instead. Tolerant of any wrapper text the MCP puts around the return value."""
    raw = text or ""
    head = f"ZS-{kind} 1"
    idx = raw.find(head)
    if idx == -1:
        raise ToolError(
            f"Studio did not return a {kind} report (got {len(raw)} chars). "
            f"First 400 chars: {raw[:400] or '(empty)'}")
    body = raw[idx + len(head):]
    end = re.search(r"^END\s*$", body, re.M)
    if end:
        body = body[:end.start()]
    lines = [ln for ln in body.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _report_fields(lines, tag):
    """Fields of every line starting with `tag` (TAB separated)."""
    out = []
    for ln in lines:
        parts = ln.split("\t")
        if parts and parts[0] == tag:
            out.append(parts[1:])
    return out


def _place_from(lines):
    f = _report_fields(lines, "PLACE")
    return f[0][0] if f and f[0] else None


def parse_sources(text):
    """A ZS-SOURCES report -> (files, missing). Byte-exact: the report is parsed
    on its UTF-8 encoding because Luau counts bytes, not characters."""
    data = (text or "").encode("utf-8", "replace")
    idx = data.find(b"ZS-SOURCES 1")
    if idx == -1:
        raise ToolError(f"Studio did not return a SOURCES report. First 300 chars: {(text or '')[:300]}")
    pos = idx + len(b"ZS-SOURCES 1")
    end = data.find(b"\nEND", pos)
    if end == -1:
        # No terminator: Studio's reply was cut off mid-stream. Parsing what is
        # there would hand back half a script and look like success - the single
        # worst failure mode for a tool whose whole job is moving source code -
        # so it is raised instead, and read_sources halves the chunk and retries.
        raise ToolError(f"Studio's SOURCES reply was cut short at {len(data)} bytes (no END marker), "
                        f"so no source was trusted.")
    stop = end
    files, missing, place = [], [], None

    def read_line(p):
        e = data.find(b"\n", p)
        if e == -1 or e > stop:
            e = stop
        return data[p:e].decode("utf-8", "replace"), e + 1

    while pos < stop:
        line, nxt = read_line(pos)
        pos = nxt
        if not line.strip() or line == "END":
            continue
        parts = line.split("\t")
        if parts[0] == "PLACE" and len(parts) > 1:
            place = parts[1]
        elif parts[0] == "MISS" and len(parts) > 1:
            missing.append({"path": parts[1], "reason": "\t".join(parts[2:]) or "not found"})
        elif parts[0] == "FILE" and len(parts) >= 6:
            path, cls, enc, _src_len, data_len = parts[1], parts[2], parts[3], parts[4], parts[5]
            try:
                n = int(data_len)
            except ValueError:
                n = 0
            blob = data[pos:pos + n]
            pos += n
            if data[pos:pos + 1] == b"\n":
                pos += 1
            try:
                if enc == "B64":
                    raw = base64.b64decode(blob, validate=True)
                else:
                    raw = blob
                code = raw.decode("utf-8")
            except Exception as e:  # noqa: BLE001 - undecodable is a hard failure
                raise ToolError(f"the source of {path} came back undecodable ({e}) - the reply was "
                                f"probably cut short") from None
            # The report declares each script's byte length: checking it turns a
            # silently truncated read into a retry instead of a half-written file.
            want = _as_int(parts[4], -1)
            if want >= 0 and len(code.encode("utf-8")) != want:
                raise ToolError(f"the source of {path} came back incomplete "
                                f"({len(code.encode('utf-8'))} of {want} bytes)")
            files.append({"path": path, "class": cls, "code": code})
    return files, missing, place


# ── place-name cache (every report carries PLACE, so this is nearly free) ──
_place_cache = {"name": None, "at": 0.0}


def _note_place(name):
    if name:
        _place_cache["name"] = name
        _place_cache["at"] = time.time()


def place_slug(studio, max_age=180.0):
    """Filesystem slug of the open place, cached: history/workspace folders are
    per place, and asking Studio for its name costs a round trip."""
    with _lock:
        if _place_cache["name"] and time.time() - _place_cache["at"] < max_age:
            return _slug(_place_cache["name"])
    lines = parse_report(studio.luau(_snippet("PLACE", _LUA_PLACE), timeout=15), "PLACE")
    name = _place_from(lines) or "place"
    run = _report_fields(lines, "RUN")
    with _lock:
        _note_place(name)
        _place_cache["running"] = bool(run and run[0] and run[0][0] == "Running")
    return _slug(name)


def normalize_path(path):
    """Models write 'ServerScriptService.Main', 'game.ServerScriptService.Main'
    and 'Workspace.Part' interchangeably. Everything here wants the explicit
    'game.' form the MCP's own tools use."""
    p = re.sub(r"\s+", "", str(path or ""))
    p = p.strip(".").strip()
    if not p:
        raise ToolError("empty instance path")
    if re.match(r"^(game|DataModel)\.", p, re.I):
        return "game." + p.split(".", 1)[1]
    if p.lower() in ("game", "datamodel"):
        return "game"
    return "game." + p


# ══════════════════════════════════════════════════════════════════════════
#  READING SOURCES OUT OF STUDIO (chunked, self-healing)
# ══════════════════════════════════════════════════════════════════════════
# What "that call was too big for Studio" looks like from here: either Studio's
# own parser complaint, or one of our reports coming back cut short / not at all.
# Any of them means "split the chunk and retry", not "give up".
_SIZE_PROBLEM_RE = re.compile(
    r"did not return a \w+ report|cut short|came back (undecodable|incomplete)|"
    r"Failed to parse command code|timed out|too large", re.I)


def read_sources(studio, paths, timeout=STUDIO_TIMEOUT):
    """Read whole scripts from Studio in as few calls as possible. A chunk that
    comes back unparseable (too big for Studio's transport, or its parser) is
    halved and retried, down to a single script, so one huge script can never
    take the whole pull down with it. Returns (files, missing)."""
    paths = [normalize_path(p) for p in _as_list(paths)]
    files, missing, queue = [], [], [paths]
    while queue:
        chunk = queue.pop(0)
        if not chunk:
            continue
        payload = _pack_records([{"path": p} for p in chunk])
        try:
            text = studio.luau(_snippet("SOURCES", _LUA_READ, payload=payload), timeout=timeout)
            got, miss, place = parse_sources(text)
            _note_place(place)
            files.extend(got)
            missing.extend(miss)
        except ToolError as e:
            if len(chunk) > 1 and _SIZE_PROBLEM_RE.search(str(e)):
                mid = len(chunk) // 2
                queue.insert(0, chunk[mid:])
                queue.insert(0, chunk[:mid])
                continue
            if len(chunk) == 1:
                missing.append({"path": chunk[0], "reason": str(e).split("\n")[0][:200]})
                continue
            raise
    return files, missing


def inventory(studio, root="game", depth=4, max_nodes=400, want_scripts=True,
              want_tree=True, timeout=STUDIO_TIMEOUT):
    """One call -> {"place", "mode", "tree": [lines], "scripts": [...], "nodes",
    "truncated"}. This is the whole warm-up a session used to spend 5 turns on.

    want_tree=False walks just as deep but only reports scripts: that is how a
    full-place pull finds every script without shipping a 20k-line tree back
    through one execute_luau reply."""
    text = studio.luau(_snippet(
        "INVENTORY", _LUA_INVENTORY, max_depth=depth, max_nodes=max_nodes,
        want_scripts=want_scripts, want_tree=want_tree, root=root), timeout=timeout)
    lines = parse_report(text, "INVENTORY")
    place = _place_from(lines)
    _note_place(place)
    fatal = _report_fields(lines, "FATAL")
    if fatal:
        raise ToolError(f"could not read the place: {fatal[0][0]}")
    run = _report_fields(lines, "RUN")
    scripts = []
    for f in _report_fields(lines, "S"):
        if len(f) >= 4:
            scripts.append({"path": f[0], "class": f[1],
                            "bytes": _as_int(f[2], -1), "first": f[3] if len(f) > 3 else ""})
    stats = _report_fields(lines, "N")
    return {
        "place": place or "place",
        "mode": (run[0][0] if run and run[0] else "Edit"),
        "tree": [f[0] for f in _report_fields(lines, "T")],
        "scripts": scripts,
        "nodes": _as_int(stats[0][0], 0) if stats else 0,
        "script_count": _as_int(stats[0][1], len(scripts)) if stats else len(scripts),
        "truncated": bool(stats and stats[0][2:] and stats[0][2] == "True"),
    }


# ══════════════════════════════════════════════════════════════════════════
#  THE DISK MIRROR (workspace) AND VERSION HISTORY
# ══════════════════════════════════════════════════════════════════════════
def _rel_for(dot_path, ext=".lua"):
    """game.ServerScriptService.Shop.Handler -> ServerScriptService/Shop/Handler.lua
    The mirror is a real folder tree, so the place can be browsed/diffed/grepped
    with ordinary tools - including by the user, outside a chat."""
    p = re.sub(r"^(game|DataModel)\.", "", normalize_path(dot_path), flags=re.I)
    segs = [_slug(s, "unnamed", 80) for s in p.split(".") if s.strip()]
    if not segs:
        raise ToolError(f"'{dot_path}' is not a usable script path")
    return "/".join(segs[:-1] + [segs[-1] + ext])


def _ws_dir(slug):
    return os.path.join(WORKSPACE_ROOT, _slug(slug))


def _ws_manifest(slug):
    return os.path.join(_ws_dir(slug), "zsmanifest.json")


def _load_manifest(slug):
    try:
        with open(_ws_manifest(slug), encoding="utf-8") as f:
            m = json.load(f)
        if isinstance(m, dict) and isinstance(m.get("files"), dict):
            return m
    except (OSError, ValueError):
        pass
    return {"place": slug, "pulled_at": 0, "files": {}}


def _save_manifest(slug, manifest):
    _write_text(_ws_manifest(slug), json.dumps(manifest, indent=1, sort_keys=True))


def _hist_dir(slug, dot_path):
    rel = _rel_for(dot_path)
    return os.path.join(HISTORY_ROOT, _slug(slug), rel[:-4] if rel.endswith(".lua") else rel)


def _load_index(hdir):
    try:
        with open(os.path.join(hdir, "index.json"), encoding="utf-8") as f:
            idx = json.load(f)
        if isinstance(idx, dict) and isinstance(idx.get("entries"), list):
            return idx
    except (OSError, ValueError):
        pass
    return {"path": None, "entries": []}


def _save_index(hdir, idx):
    _write_text(os.path.join(hdir, "index.json"), json.dumps(idx, indent=1))


def history_add(slug, dot_path, code, label="manual"):
    """Snapshot one script. Identical-to-newest snapshots are skipped (a write
    that changed nothing must not create a version). Returns the entry or None."""
    with _lock:
        hdir = _hist_dir(slug, dot_path)
        idx = _load_index(hdir)
        idx["path"] = normalize_path(dot_path)
        newest = idx["entries"][-1] if idx["entries"] else None
        sha = _sha(code)
        if newest and newest.get("sha") == sha:
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        n = sum(1 for e in idx["entries"] if str(e.get("id", "")).startswith(stamp)) + 1
        entry = {"id": f"{stamp}-{n:02d}", "ts": time.time(), "label": str(label)[:60],
                 "bytes": _size(code), "sha": sha, "file": f"{stamp}-{n:02d}.lua"}
        _write_text(os.path.join(hdir, entry["file"]), code or "")
        idx["entries"].append(entry)
        # Prune oldest beyond HISTORY_KEEP so a chatty session cannot grow
        # without bound on the user's disk.
        while len(idx["entries"]) > HISTORY_KEEP:
            old = idx["entries"].pop(0)
            try:
                os.remove(os.path.join(hdir, old.get("file", "")))
            except OSError:
                pass
        _save_index(hdir, idx)
        return entry


def history_entries(slug, dot_path=None):
    """Every snapshot, newest first, optionally filtered by path substring."""
    root = os.path.join(HISTORY_ROOT, _slug(slug))
    out = []
    if not os.path.isdir(root):
        return out
    filt = str(dot_path or "").lower().replace("\\", ".")
    filt = re.sub(r"^(game|datamodel)\.", "", filt) if filt else ""
    for dirpath, _dirs, files in os.walk(root):
        if "index.json" not in files:
            continue
        idx = _load_index(dirpath)
        path = idx.get("path") or ""
        if filt and filt not in path.lower():
            continue
        for e in idx.get("entries", []):
            e = dict(e)
            e["script"] = path
            e["dir"] = dirpath
            out.append(e)
    out.sort(key=lambda e: e.get("ts", 0), reverse=True)
    return out


def history_read(entry):
    return _read_text(os.path.join(entry["dir"], entry["file"]))


def _plural(n, word, plural=None):
    """1 script / 3 scripts - the summaries read like prose, so they are
    pluralised properly rather than written as the terser "script(s)" the
    per-result lines use."""
    n = _as_int(n, 0)
    return "%d %s" % (n, word if n == 1 else (plural or word + "s"))


def _fmt_age(ts):
    d = max(0, int(time.time() - (ts or 0)))
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60} min ago"
    if d < 86400:
        return f"{d // 3600} h ago"
    return f"{d // 86400} d ago"


# ══════════════════════════════════════════════════════════════════════════
#  WRITING SCRIPTS (one call, many files, verified, with a fallback)
# ══════════════════════════════════════════════════════════════════════════
SCRIPT_CLASSES = ("Script", "LocalScript", "ModuleScript")


def normalize_files(raw, default_class="Script"):
    """Accept the shapes models actually send: {"files":[...]}, a bare
    {"path","code"}, multi_edit's spelling (file_path/className), an "edits"
    array with one full-source entry, or a {path: code} object."""
    out = []
    items = raw if isinstance(raw, list) else [raw]
    if isinstance(raw, dict) and not any(k in raw for k in
                                         ("path", "file_path", "target_file", "code", "source", "files")):
        items = [{"path": k, "code": v} for k, v in raw.items() if isinstance(v, str)]
    for it in items:
        if not isinstance(it, dict):
            raise ToolError(f"each file must be an object with path and code (got {type(it).__name__})")
        path = it.get("path") or it.get("file_path") or it.get("target_file") or it.get("name")
        code = it.get("code")
        if code is None:
            code = it.get("source")
            if code is None and isinstance(it.get("edits"), list):
                # multi_edit-shaped: one create-edit carrying the whole source
                code = "".join(str(e.get("new_string", "")) for e in it["edits"] if isinstance(e, dict))
        if not path:
            raise ToolError("every file needs a \"path\" (a full dot-path like game.ServerScriptService.Main)")
        if code is None:
            raise ToolError(f"{path}: no \"code\" was given")
        cls = it.get("className") or it.get("class") or it.get("type") or default_class
        cls = str(cls).strip()
        if cls not in SCRIPT_CLASSES:
            fixed = next((c for c in SCRIPT_CLASSES if c.lower() == cls.lower()), None)
            if not fixed:
                raise ToolError(f'{path}: className "{cls}" is not a script class - use one of '
                                + ", ".join(SCRIPT_CLASSES))
            cls = fixed
        out.append({"path": normalize_path(path), "class": cls, "code": str(code)})
    if not out:
        raise ToolError('nothing to write - pass "files": [{"path": "...", "code": "..."}]')
    seen = {}
    for f in out:
        if f["path"] in seen:
            raise ToolError(f"{f['path']} appears twice in one call - merge them into one file")
        seen[f["path"]] = True
    return out


def _chunks_by_bytes(files, max_bytes):
    """Group files so no execute_luau payload exceeds max_bytes (a single file
    bigger than that goes alone)."""
    groups, cur, cur_bytes = [], [], 0
    for f in files:
        n = len(f["code"].encode("utf-8")) + len(f["path"]) + 40
        if cur and cur_bytes + n > max_bytes:
            groups.append(cur)
            cur, cur_bytes = [], 0
        cur.append(f)
        cur_bytes += n
    if cur:
        groups.append(cur)
    return groups


def _write_via_luau(studio, files, select=False, timeout=STUDIO_TIMEOUT):
    """Returns (results, hard_error). results: one dict per file with
    status/path/old/new. A payload Studio refuses to parse is halved and retried
    once, then reported so the multi_edit fallback can take over."""
    # Chunk up front: Studio's parser has a size limit (documented live in
    # CHANGELOG.md), and a 40-file system must not ride on one giant call. A
    # chunk that is still refused is halved again below.
    groups = _chunks_by_bytes(files, MAX_WRITE_BYTES)
    results = []
    # Selection is one Studio-wide state, so only the LAST chunk sets it -
    # otherwise each chunk would deselect the previous one's scripts.
    queue = [(g, bool(select) and i == len(groups) - 1) for i, g in enumerate(groups)]
    hard = None
    while queue:
        group, sel = queue.pop(0)
        payload = _pack_records(group)
        code = _snippet("WRITE", _LUA_WRITE, payload=payload, select=sel)
        try:
            text = studio.luau(code, timeout=timeout)
            lines = parse_report(text, "WRITE")
        except ToolError as e:
            if len(group) > 1 and _SIZE_PROBLEM_RE.search(str(e)):
                mid = len(group) // 2
                queue.insert(0, (group[mid:], False))
                queue.insert(0, (group[:mid], sel))
                continue
            hard = str(e)
            for f in group:
                results.append({"status": "failed", "path": f["path"], "class": f["class"],
                                "reason": hard.split("\n")[0][:200]})
            continue
        _note_place(_place_from(lines))
        by_path = {}
        # OK lines are: <status> <path> <class> <old bytes> <new bytes>
        for f in _report_fields(lines, "OK"):
            if len(f) >= 5:
                by_path[f[1]] = {"status": f[0], "path": f[1], "class": f[2],
                                 "old": _as_int(f[3], 0), "new": _as_int(f[4], 0)}
        for f in _report_fields(lines, "FAIL"):
            if len(f) >= 2:
                by_path[f[0]] = {"status": "failed", "path": f[0],
                                 "class": next((x["class"] for x in group if x["path"] == f[0]), "?"),
                                 "reason": "\t".join(f[1:])[:300]}
        for f in group:
            got = by_path.get(f["path"])
            if got:
                results.append(got)
            else:
                results.append({"status": "failed", "path": f["path"], "class": f["class"],
                                "reason": "Studio's reply did not mention this file"})
        sel_n = _report_fields(lines, "SEL")
        if sel_n:
            for r in results[-len(group):]:
                r["selected"] = True
    return results, hard


def _write_via_multi_edit(studio, files, old_sources, timeout=STUDIO_TIMEOUT):
    """Fallback for anything the direct Source write could not do: the MCP's own
    multi_edit, one call per file, old_string = the file's exact current source
    ("" creates it). Slower, but it is the documented path and it always knows
    how to create the containers it needs."""
    results = []
    for f in files:
        old = old_sources.get(f["path"])
        args = {
            "datamodel_type": "Edit",
            "file_path": f["path"],
            "className": f["class"],
            "edits": [{"old_string": old if old is not None else "",
                       "new_string": f["code"]}],
        }
        ok, text, err = studio.tool("multi_edit", [args], timeout=timeout)
        if ok and not re.search(r"not found in current content|identical old_string|is required|failed", text or "", re.I):
            results.append({"status": "updated" if old else "created", "path": f["path"],
                            "class": f["class"], "old": len(old or ""), "new": len(f["code"]),
                            "via": "multi_edit"})
        else:
            results.append({"status": "failed", "path": f["path"], "class": f["class"],
                            "reason": (err or text or "multi_edit refused the write")[:300],
                            "via": "multi_edit"})
    return results


def write_scripts(studio, files, history=True, check=True, force=False,
                  select=True, timeout=STUDIO_TIMEOUT):
    """The whole "create scripts" routine in one call:
      1. static pre-flight (unless force) - a missing `end` never reaches Studio
      2. snapshot what is about to be overwritten, so it can be reverted
      3. write everything in as few Studio calls as fit
      4. anything Studio refused is retried through multi_edit
      5. every write is confirmed by reading the Source back (done in step 3)
    Returns a dict: {"results", "blocked", "history_error", "hard", "slug"}.
    "blocked" is the lint report when the pre-flight refused the write, in which
    case nothing was sent to Studio at all."""
    if check and not force:
        per, allf = check_many([{"path": f["path"], "code": f["code"]} for f in files])
        errors = {p: [x for x in v if x["severity"] == "error"] for p, v in per.items()}
        errors = {p: v for p, v in errors.items() if v}
        if errors:
            lines = ["ERROR: script_write wrote NOTHING - luau_check found syntax errors:"]
            for p, items in errors.items():
                lines.append(f"── {p}")
                for it in items[:8]:
                    lines.append(f"   line {it['line']}: {it['message']}")
            lines.append('Fix the code and call script_write again (or pass "force": true to write it anyway).')
            return {"results": [], "blocked": "\n".join(lines), "history_error": None,
                    "hard": None, "slug": None}

    slug = place_slug(studio)
    old_sources = {}
    if history:
        try:
            got, _miss = read_sources(studio, [f["path"] for f in files], timeout=timeout)
            old_sources = {g["path"]: g["code"] for g in got}
            classes = {g["path"]: g["class"] for g in got}
            for f in files:
                if f["path"] in classes and classes[f["path"]] in SCRIPT_CLASSES:
                    f["found_class"] = classes[f["path"]]
        except ToolError as e:
            # History is a safety net, not a precondition: if reading the old
            # sources fails, still write (the user's request wins) and say so.
            old_sources = {}
            history_error = str(e).split("\n")[0][:160]
        else:
            history_error = None
    else:
        history_error = None

    results, hard = _write_via_luau(studio, files, select=select and len(files) <= 20, timeout=timeout)

    # Retry the failures through multi_edit when the reason looks like a write
    # permission/transport problem rather than a bad path.
    retry = [r for r in results if r["status"] == "failed" and
             re.search(r"Source|did not stick|parse command code|did not mention", r.get("reason", ""), re.I)]
    if retry:
        paths = {r["path"] for r in retry}
        todo = [f for f in files if f["path"] in paths]
        for f in todo:
            if f["path"] not in old_sources:
                try:
                    got, _m = read_sources(studio, [f["path"]], timeout=timeout)
                    old_sources[f["path"]] = got[0]["code"] if got else ""
                except ToolError:
                    old_sources[f["path"]] = ""
        fixed = _write_via_multi_edit(studio, todo, old_sources, timeout=timeout)
        by_path = {r["path"]: r for r in fixed}
        results = [by_path.get(r["path"], r) for r in results]

    if history:
        for r in results:
            if r["status"] in ("created", "updated") and r["path"] in old_sources:
                entry = history_add(slug, r["path"], old_sources[r["path"]], "auto:before-write")
                if entry:
                    r["snapshot"] = entry["id"]
        # Brand-new files get an EMPTY snapshot: that is the state they were in
        # before this write, so `revert` undoes a create by emptying the script
        # (the instance itself is left alone - deleting instances is the user's
        # call, and the system prompt forbids broad destruction).
        for f in files:
            r = next((x for x in results if x["path"] == f["path"]), None)
            if r and r["status"] == "created" and f["path"] not in old_sources:
                entry = history_add(slug, f["path"], "", "auto:before-create")
                if entry:
                    r["snapshot"] = entry["id"]
    return {"results": results, "blocked": None, "history_error": history_error,
            "hard": hard, "slug": slug}


def format_write(results, blocked=None, history_error=None, hard=None, slug=None):
    if blocked:
        return blocked
    done = [r for r in results if r["status"] in ("created", "updated")]
    same = [r for r in results if r["status"] == "unchanged"]
    bad = [r for r in results if r["status"] == "failed"]
    created = sum(1 for r in done if r["status"] == "created")
    head = (f"script_write: {len(done)} written ({created} created, {len(done) - created} updated), "
            f"{len(same)} already up to date, {len(bad)} failed.")
    head += " Every write was confirmed by reading the Source back from Studio."
    lines = [head]
    for r in results:
        if r["status"] == "failed":
            lines.append(f"  FAIL    {r['path']}: {r.get('reason', '?')}")
        elif r["status"] == "unchanged":
            lines.append(f"  =       {r['path']} ({r.get('class', '?')}, {r.get('new', 0)} bytes - already identical)")
        else:
            via = f", via {r['via']}" if r.get("via") else ""
            size = (f"{r.get('old', 0)} -> {r.get('new', 0)} bytes" if r["status"] == "updated"
                    else f"{r.get('new', 0)} bytes")
            snap = f", snapshot {r['snapshot']}" if r.get("snapshot") else ""
            lines.append(f"  {r['status'].upper():7} {r['path']} ({r.get('class', '?')}, {size}{via}{snap})")
    if history_error:
        lines.append(f"NOTE: the pre-write snapshot failed ({history_error}), so these files have no undo point.")
    if hard:
        lines.append(f"NOTE: one Studio call failed outright: {hard.split(chr(10))[0][:160]}")
    if bad:
        lines.append("Failed files were NOT written - read the reason, fix it, and call script_write again for just those files.")
    if done or same:
        lines.append("Next: script_verify compiles every script in the place and reports syntax errors with line numbers.")
        if slug:
            lines.append(f"Undo points are in .zeroscript/history/{slug} (script_history list/diff/revert); "
                         f"workspace pull mirrors the place to disk.")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
#  TOOLS
# ══════════════════════════════════════════════════════════════════════════
MEMORY_PATH = "game.ServerStorage.ZeroScript.Memory"


def _console(studio, want="both", tail_lines=14, max_chars=2600):
    """Recent Output from Studio, best effort: the tool's parameter shape has
    changed between MCP builds, so several are tried.

    A bare {} is only ever tried for the SERVER, because an argument-less call
    answers with whatever Studio defaults to - accepting that as the Client's
    Output would label one stream as the other (and a playtest report that says
    "Client" while showing Server lines sends the model to fix the wrong side)."""
    out = []
    which = {"server": ("Server",), "client": ("Client",), "both": ("Server", "Client")}.get(
        str(want).lower(), ("Server", "Client"))
    for dm in which:
        variants = [{"datamodel_type": dm}, {"datamodel": dm}, {"type": dm.lower()}]
        if dm == "Server":
            variants.append({})
        ok, text, err = studio.tool("get_console_output", variants)
        body = (text or "").strip()
        if not ok or not body:
            if err and "not available" in str(err):
                continue
            out.append(f"── Output ({dm}) ──\n(unavailable: {str(err or 'empty')[:120]})")
            continue
        lines = body.splitlines()
        keep = lines[-tail_lines:]
        text_out = "\n".join(keep)
        if len(text_out) > max_chars:
            text_out = text_out[-max_chars:]
        cut = f"\n…({len(lines) - len(keep)} earlier lines)" if len(lines) > len(keep) else ""
        noun = "line" if len(keep) == 1 else "lines"
        out.append(f"── Output ({dm}, last {len(keep)} {noun}) ──\n{text_out}{cut}")
    return "\n\n".join(out)


def _tool_project_overview(args, studio):
    root = normalize_path(args["root"]) if args.get("root") else "game"
    depth = _as_int(args.get("depth"), 4, 1, 12)
    max_nodes = _as_int(args.get("max_nodes"), 400, 20, 5000)
    want_scripts = _as_bool(args.get("scripts"), True)
    want_console = _as_bool(args.get("console"), True)
    t0 = time.time()
    inv = inventory(studio, root=root, depth=depth, max_nodes=max_nodes, want_scripts=want_scripts)
    slug = _slug(inv["place"])
    lines = [f"project_overview: \"{inv['place']}\" ({inv['mode']} mode) - "
             f"{_plural(inv['nodes'], 'instance')} listed, "
             f"{_plural(inv['script_count'], 'script')}, in {time.time() - t0:.1f}s"]
    if inv["truncated"]:
        lines.append(f"(the tree was cut at {max_nodes} nodes - pass a bigger \"max_nodes\", a \"root\" subtree, "
                     f"or \"depth\" to see more)")
    lines.append("")
    lines.append("── Instance tree ──")
    lines.extend(inv["tree"] or ["(nothing listed - try a deeper \"depth\")"])
    if want_scripts:
        lines.append("")
        lines.append(f"── Scripts ({len(inv['scripts'])}) ──")
        if not inv["scripts"]:
            lines.append("(no scripts in this place yet - script_write creates them, containers included)")
        for s in inv["scripts"]:
            lines.append(f"{s['path']} | {s['class']} | {s['bytes']} bytes | {s['first']}")
    mem = next((s for s in inv["scripts"] if s["path"].lower() == MEMORY_PATH.lower()), None)
    lines.append("")
    lines.append("── Project memory ──")
    if mem:
        lines.append(f"{MEMORY_PATH} exists ({mem['bytes']} bytes) - script_read it before changing how this game works, "
                     f"and keep it updated with what you learn.")
    else:
        lines.append(f"{MEMORY_PATH} does not exist yet - create it (script_write, className ModuleScript) once you know "
                     f"something worth remembering about this project.")
    mirror = _load_manifest(slug)
    lines.append("")
    lines.append("── Local mirror / undo ──")
    if mirror.get("files"):
        lines.append(f".zeroscript/workspace/{slug}: {len(mirror['files'])} script file(s), pulled "
                     f"{_fmt_age(mirror.get('pulled_at'))} - `workspace grep` searches them instantly, no Studio call.")
    else:
        lines.append(f"no mirror yet - `workspace pull` writes every script to .zeroscript/workspace/{slug} as .lua files.")
    hist = history_entries(slug)
    lines.append(f"undo points: {len(hist)} snapshot(s)" + (f", newest {_fmt_age(hist[0]['ts'])}" if hist else "") +
                 " (script_history list / diff / revert).")
    if want_console:
        try:
            con = _console(studio, args.get("capture") or "server")
            if con:
                lines.append("")
                lines.append(con)
        except ToolError as e:
            lines.append("")
            lines.append(f"── Output ──\n(unavailable: {str(e).split(chr(10))[0][:140]})")
    lines.append("")
    lines.append("Fast path: script_write (whole scripts, many at once) -> script_verify (compiles them) -> "
                 "playtest (runs the game and captures Output). Use multi_edit for surgical edits to an existing "
                 "script, execute_luau for building instances, and batch for independent steps in one turn.")
    return "\n".join(lines)


def _tool_script_write(args, studio):
    raw = args.get("files")
    if raw is None:
        raw = args
    files = normalize_files(raw)
    if len(files) > 40:
        raise ToolError(f"script_write takes at most 40 files per call (you sent {len(files)}) - "
                        f"split it into two calls, or write the rest after script_verify passes.")
    total = sum(len(f["code"].encode("utf-8")) for f in files)
    if total > 900_000:
        raise ToolError(f"script_write got {total} bytes of code in one call - that is too much for one turn. "
                        f"Split it into several calls (a system at a time).")
    res = write_scripts(
        studio, files,
        history=_as_bool(args.get("history"), True),
        check=_as_bool(args.get("check"), True),
        force=_as_bool(args.get("force"), False),
        select=_as_bool(args.get("select"), True) and _as_bool(args.get("select_in_studio"), True),
    )
    return format_write(res["results"], res["blocked"], res["history_error"], res["hard"], res["slug"])


def _tool_script_verify(args, studio):
    paths = [normalize_path(p) for p in _as_list(args.get("paths") or args.get("path"))]
    cap = _as_int(args.get("max"), 400, 1, 2000)
    payload = _pack_records([{"path": p} for p in paths]) if paths else ""
    text = studio.luau(_snippet("VERIFY", _LUA_VERIFY, payload=payload, max=cap), timeout=STUDIO_TIMEOUT)
    lines = parse_report(text, "VERIFY")
    _note_place(_place_from(lines))
    noload = _report_fields(lines, "NOLOAD")
    fails = _report_fields(lines, "FAIL")
    skips = _report_fields(lines, "SKIP")
    caps = _report_fields(lines, "CAP")
    stats = _report_fields(lines, "N")
    total, checked, bad = (_as_int(stats[0][0], 0), _as_int(stats[0][1], 0),
                           _as_int(stats[0][2], 0)) if stats else (0, 0, len(fails))
    if noload:
        return ("script_verify: Studio cannot compile scripts in this context "
                f"({noload[0][0]}).\nUse luau_check instead - it runs here in the bridge, needs no Studio, and catches "
                "unbalanced blocks, corrupted punctuation, blocking WaitForChild and deprecated calls.\n"
                "(script_write already runs luau_check on everything before it writes, unless \"force\" is set.)")
    head = (f"script_verify: {checked} script(s) compiled in Studio - "
            f"{checked - bad} OK, {bad} FAILED" + (f" (of {total} found)" if total != checked else "") + ".")
    out = [head]
    for f in fails:
        path = f[0] if f else "?"
        cls = f[1] if len(f) > 1 else "?"
        err = "\t".join(f[2:]) if len(f) > 2 else "?"
        # loadstring prefixes '[string "<first chars of the source>"]:' - noise
        # next to the line number the model actually needs.
        err = re.sub(r'^\[string "[^"]*"\]:\s*', "line ", err)
        out.append(f"  FAIL {path} ({cls}): {err}")
    for s in skips:
        why = "\t".join(s[1:]) if len(s) > 1 else "not a script"
        out.append(f"  SKIP {s[0] if s else '?'}: {why}")
    if caps:
        out.append(f"  (capped: {caps[0][0]} scripts found, the first {caps[0][1]} were checked - pass \"paths\" to check specific ones)")
    if bad:
        out.append("Fix each failure with script_write (whole file) or multi_edit (surgical), then run script_verify again.")
    else:
        out.append("Every script compiles. Next: playtest to run the game and capture Output, or continue building.")
    return "\n".join(out)


def _tool_luau_check(args, studio):
    # Not normalize_files(): these paths are LABELS for the report, not instance
    # paths, so they must not be rewritten into game.<label> dot-paths.
    files = []
    for it in _as_list(args.get("files")):
        if isinstance(it, str):
            files.append({"path": "<code>", "code": it})
        elif isinstance(it, dict):
            files.append({"path": str(it.get("path") or it.get("name") or "<code>"),
                          "code": str(it.get("code") or it.get("source") or "")})
    if not files and args.get("code") is not None:
        files = [{"path": str(args.get("path") or "<code>"), "code": str(args["code"])}]
    if not files:
        raise ToolError('luau_check needs "code" (one snippet) or "files": [{"path": "...", "code": "..."}]')
    context = "execute" if str(args.get("context") or "").lower() in ("execute", "luau", "execute_luau") else "script"
    per, allf = check_many([{"path": f["path"], "code": f["code"]} for f in files], context)
    errors = sum(1 for f in allf if f["severity"] == "error")
    warns = sum(1 for f in allf if f["severity"] == "warn")
    total_lines = sum(len((f["code"] or "").splitlines()) for f in files)
    if not allf:
        return (f"luau_check: clean - {len(files)} snippet(s), {_plural(total_lines, 'line')}, "
                f"no errors or warnings. "
                f"Runs here in the bridge: no Studio call was needed.")
    head = (f"{'ERROR' if errors else 'WARN'}: luau_check found {errors} error(s) and {warns} warning(s) "
            f"in {len(files)} snippet(s) ({_plural(total_lines, 'line')}) - no Studio call was needed.")
    out = [head]
    for f in files:
        items = per.get(f["path"], [])
        if not items:
            out.append(f"── {f['path']}: clean")
            continue
        out.append(f"── {f['path']} ({_plural(len(f['code'].splitlines()), 'line')})")
        for it in items:
            out.append(f"   {it['severity'].upper():5} line {it['line']}: {it['message']}")
    if errors:
        out.append("Errors mean Studio will refuse or misrun this code - fix them before script_write/execute_luau.")
    return "\n".join(out)


def _tool_script_history(args, studio):
    action = str(args.get("action") or "list").strip().lower()
    slug = None
    if args.get("place"):
        slug = _slug(args["place"])
    if slug is None and action in ("snapshot", "revert"):
        slug = place_slug(studio)
    if slug is None:
        # No Studio needed to read history: use the only place we have snapshots
        # for (usually exactly one), else the cached place name.
        known = []
        if os.path.isdir(HISTORY_ROOT):
            known = sorted(d for d in os.listdir(HISTORY_ROOT)
                           if os.path.isdir(os.path.join(HISTORY_ROOT, d)))
        if _place_cache.get("name"):
            slug = _slug(_place_cache["name"])
        elif len(known) == 1:
            slug = known[0]
        elif known:
            raise ToolError("snapshots exist for several places (" + ", ".join(known) +
                            ') - pass "place": "<name>" to choose one.')
        else:
            raise ToolError("no snapshots yet. They are created automatically by every script_write; "
                            'use "action": "snapshot" to capture the current place first.')

    path = args.get("path") or (args.get("paths") or [None])[0] if isinstance(args.get("paths"), list) else args.get("path")
    limit = _as_int(args.get("limit"), 12, 1, 100)

    if action == "snapshot":
        paths = [normalize_path(p) for p in _as_list(args.get("paths") or args.get("path"))]
        if not paths:
            inv = inventory(studio, want_tree=False, want_scripts=True, depth=24, max_nodes=20000)
            paths = [s["path"] for s in inv["scripts"]]
            slug = _slug(inv["place"])
        if not paths:
            return "script_history snapshot: this place has no scripts to snapshot."
        files, missing = read_sources(studio, paths)
        saved = 0
        for f in files:
            if history_add(slug, f["path"], f["code"], "snapshot"):
                saved += 1
        out = [f"script_history snapshot: {saved} new undo point(s) saved for {len(files)} script(s) "
               f"of \"{slug}\" ({len(paths) - len(files)} unreadable)."]
        for m in missing[:6]:
            out.append(f"  MISS {m['path']}: {m['reason'][:120]}")
        out.append('Use "action": "diff" to compare a script with a snapshot, or "revert" to put one back.')
        return "\n".join(out)

    if action == "list":
        entries = history_entries(slug, path)
        if not entries:
            return (f"script_history list: no snapshots for \"{slug}\""
                    + (f" matching \"{path}\"" if path else "")
                    + ". script_write saves one automatically before every overwrite; "
                      '"action": "snapshot" captures the place now (needs Studio).')
        out = [f"script_history list: {len(entries)} snapshot(s) for \"{slug}\" (newest first"
               + (f", filtered on \"{path}\"" if path else "") + "):"]
        for e in entries[:limit]:
            out.append(f"  {e['id']}  {_fmt_age(e['ts'])}  {_entry_size(e):>7} bytes  {e['label']:<20} {e['script']}")
        if len(entries) > limit:
            out.append(f"  …{len(entries) - limit} older (pass a bigger \"limit\" or a \"path\" filter)")
        out.append('diff: {"action":"diff","path":"<script>","entry":"<id>"}   revert: {"action":"revert","path":"<script>","entry":"<id>"}')
        return "\n".join(out)

    if action in ("diff", "revert"):
        want = normalize_path(path) if path else None
        entries = history_entries(slug, want)
        if not entries:
            raise ToolError(f"no snapshots for \"{want or slug}\" - run script_history list to see what exists, "
                            f'or "action": "snapshot" to capture the place now.')
        entry = entries[0]
        if args.get("entry"):
            picked = [e for e in entries if e["id"] == str(args["entry"])]
            if not picked:
                raise ToolError(f'no snapshot with id "{args["entry"]}" for {want or slug}. '
                                f"Available: " + ", ".join(e["id"] for e in entries[:10]))
            entry = picked[0]
        old = history_read(entry)
        if action == "diff":
            live = None
            if _as_bool(args.get("live"), True):
                try:
                    got, missing = read_sources(studio, [entry["script"]])
                    live = got[0]["code"] if got else None
                    if missing and live is None:
                        live = None
                except ToolError:
                    live = None
            if live is None:
                newest = entries[0]
                a, b, la, lb = old, history_read(newest), f"snapshot {entry['id']}", f"snapshot {newest['id']}"
                if newest["id"] == entry["id"]:
                    return (f"script_history diff: {entry['script']} has one snapshot ({entry['id']}, "
                            f"{_entry_size(entry)} bytes) and Studio could not be read, so there is nothing to compare it with.")
            else:
                a, b, la, lb = old, live, f"snapshot {entry['id']} ({_fmt_age(entry['ts'])})", "live in Studio"
            diff = list(difflib.unified_diff(a.splitlines(), b.splitlines(),
                                             fromfile=f"{entry['script']} @{la}",
                                             tofile=f"{entry['script']} @{lb}", lineterm="", n=2))
            if not diff:
                return f"script_history diff: {entry['script']} is identical to {lb}."
            capped = diff[:400]
            tail = f"\n…{len(diff) - len(capped)} more diff lines" if len(diff) > len(capped) else ""
            return (f"script_history diff: {entry['script']} - {lb} vs {la} "
                    f"({sum(1 for d in capped if d.startswith('+') and not d.startswith('+++'))} added, "
                    f"{sum(1 for d in capped if d.startswith('-') and not d.startswith('---'))} removed):\n"
                    + "\n".join(capped) + tail)
        # revert
        current = ""
        try:
            got, _m = read_sources(studio, [entry["script"]])
            current = got[0]["code"] if got else ""
        except ToolError as e:
            raise ToolError(f"cannot revert without reading the current script from Studio: {e}") from None
        if current == old:
            return (f"script_history revert: {entry['script']} already matches snapshot {entry['id']} - nothing to do.")
        history_add(slug, entry["script"], current, "auto:before-revert")
        files = [{"path": entry["script"], "class": "Script", "code": old}]
        cls = re.sub(r".*\((.*)\).*$", r"\1", entry.get("label") or "")
        res = write_scripts(studio, files, history=False, check=False, select=True)
        written = [r for r in res["results"] if r["status"] in ("created", "updated")]
        if not written:
            raise ToolError("the revert did not write: " +
                            (res["results"][0].get("reason", "?") if res["results"] else res["hard"] or "unknown"))
        actual = written[0].get("class") or cls
        if actual and actual in SCRIPT_CLASSES:
            pass  # class already right
        return (f"script_history revert: {entry['script']} restored to snapshot {entry['id']} "
                f"({_fmt_age(entry['ts'])}, {_entry_size(entry)} bytes). The content it replaced was snapshotted too, "
                f"so this revert can itself be reverted.\nNext: script_verify to confirm the place still compiles.")

    raise ToolError(f'unknown action "{action}" - use snapshot, list, diff or revert.')


def _tool_workspace(args, studio):
    action = str(args.get("action") or "status").strip().lower()
    slug = _slug(args["place"]) if args.get("place") else None
    if slug is None and action in ("pull", "push", "status") :
        try:
            slug = place_slug(studio)
        except ToolError:
            if action != "status":
                raise
            known = sorted(d for d in os.listdir(WORKSPACE_ROOT)) if os.path.isdir(WORKSPACE_ROOT) else []
            if len(known) == 1:
                slug = known[0]
            elif known:
                raise ToolError("mirrors exist for several places (" + ", ".join(known) +
                                ') and Studio is not reachable - pass "place": "<name>".') from None
            raise

    if action == "pull":
        want = [normalize_path(p) for p in _as_list(args.get("paths") or args.get("path"))]
        cap = _as_int(args.get("max_files"), 300, 1, 2000)
        if want:
            files, missing = read_sources(studio, want)
            place = _place_cache.get("name") or slug
            slug = _slug(place)
        else:
            inv = inventory(studio, want_tree=False, want_scripts=True, depth=24, max_nodes=20000)
            slug = _slug(inv["place"])
            place = inv["place"]
            scripts = inv["scripts"]
            if len(scripts) > cap:
                scripts = scripts[:cap]
            files, missing = read_sources(studio, [s["path"] for s in scripts])
        manifest = _load_manifest(slug)
        root = _ws_dir(slug)
        written, skipped = 0, 0
        for f in files:
            rel = _rel_for(f["path"])
            dest = _inside(root, rel)
            old = None
            if os.path.exists(dest):
                old = _read_text(dest)
            if old == f["code"]:
                skipped += 1
            elif old is not None:
                # The mirror is a working copy the user may have edited: never
                # throw that away silently - snapshot it before overwriting.
                history_add(slug, f["path"], old, "auto:mirror-overwritten")
            _write_text(dest, f["code"])
            manifest["files"][rel] = {"path": f["path"], "className": f["class"],
                                      "bytes": _size(f["code"]), "sha": _sha(f["code"]),
                                      "pulled_at": time.time()}
            written += 1
        manifest["place"] = place if not want else manifest.get("place", slug)
        manifest["pulled_at"] = time.time()
        _save_manifest(slug, manifest)
        out = [f"workspace pull: {written} script file(s) written to {root}, {skipped} already identical, "
               f"{len(manifest['files'])} in the mirror total."]
        for f in files[:40]:
            out.append(f"  {_rel_for(f['path'])}  <-  {f['path']} ({f['class']}, {_size(f['code'])} bytes)")
        if len(files) > 40:
            out.append(f"  …{len(files) - 40} more")
        for m in missing[:6]:
            out.append(f"  MISS {m['path']}: {m['reason'][:120]}")
        out.append('Now `workspace grep` searches all of it in milliseconds with no Studio call, and '
                   '`workspace push` writes local changes back.')
        return "\n".join(out)

    if action == "grep":
        pattern = args.get("pattern") or args.get("query")
        if not pattern:
            raise ToolError('workspace grep needs a "pattern" (text or regex).')
        if slug is None:
            slug = place_slug(studio)
        root = _ws_dir(slug)
        manifest = _load_manifest(slug)
        if not manifest.get("files"):
            if not _as_bool(args.get("auto_pull"), True):
                raise ToolError(f"no mirror for \"{slug}\" yet - run workspace pull first.")
            pulled = _tool_workspace({"action": "pull", "place": slug}, studio)
            manifest = _load_manifest(slug)
            note = "(the mirror was empty, so it was pulled first: " + pulled.splitlines()[0] + ")"
        else:
            note = f"(mirror: {_plural(len(manifest['files']), 'file')}, pulled {_fmt_age(manifest.get('pulled_at'))})"
        ignore_case = _as_bool(args.get("ignore_case"), True)
        try:
            rx = re.compile(pattern, re.I if ignore_case else 0) \
                if _as_bool(args.get("regex"), True) else None
        except re.error as e:
            rx = None
            note += f" (regex invalid: {e} - searched as plain text)"
        # A plain-text search only lowers the line when it lowered the needle:
        # lowering one side alone turns "ignore_case": false into a search that
        # can never match an uppercase needle.
        lower = rx is None and ignore_case
        needle = str(pattern).lower() if lower else str(pattern)
        max_matches = _as_int(args.get("max_matches"), 60, 1, 500)
        context = _as_int(args.get("context"), 0, 0, 4)
        filt = str(args.get("path_contains") or "").lower()
        hits, files_hit, total = [], 0, 0
        for rel in sorted(manifest.get("files", {})):
            if filt and filt not in rel.lower():
                continue
            try:
                src = _read_text(_inside(root, rel))
            except (OSError, ToolError):
                continue
            lines = src.splitlines()
            matches = [i for i, ln in enumerate(lines)
                       if (rx.search(ln) if rx else (needle in (ln.lower() if lower else ln)))]
            total += len(matches)
            if not matches:
                continue
            files_hit += 1
            room = max_matches - len(hits)
            if room <= 0:
                continue
            shown = matches[:room]
            if not context:
                for i in shown:
                    hits.append(f"  > {rel}:{i + 1}: {lines[i].strip()[:220]}")
                continue
            # Overlapping windows merge (like grep -C) instead of repeating the
            # same line once per neighbouring match.
            show = {}
            for i in shown:
                for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                    show[j] = show.get(j, False) or j == i
            prev = None
            for j in sorted(show):
                if prev is not None and j > prev + 1:
                    hits.append("  --")
                mark = ">" if show[j] else " "
                hits.append(f"  {mark} {rel}:{j + 1}: {lines[j].rstrip()[:220]}")
                prev = j
        head = (f"workspace grep \"{pattern}\": {total} match(es) in {files_hit} file(s) {note}")
        if not total:
            return head + ("\nNothing matched in the local mirror. If the mirror is older than your last edits, run "
                           "workspace pull; for live-only content (instances, attributes) use search_game_tree or "
                           "inspect_instance." if True else "")
        capped = hits[:max_matches]
        tail = f"\n  …{len(hits) - len(capped)} more match(es) cut (pass \"max_matches\")" if len(hits) > max_matches else ""
        return head + "\n" + "\n".join(capped) + tail

    if action == "push":
        if slug is None:
            slug = place_slug(studio)
        root = _ws_dir(slug)
        manifest = _load_manifest(slug)
        if not manifest.get("files"):
            raise ToolError(f'no mirror for "{slug}" - run workspace pull first (push needs the path map it creates).')
        filt = str(args.get("path_contains") or "").lower()
        only = [_rel_for(p) if str(p).count(".") else str(p) for p in _as_list(args.get("files"))]
        push_all = _as_bool(args.get("all"), False)
        todo = []
        for rel, meta in sorted(manifest["files"].items()):
            if only and rel not in only and meta["path"] not in only:
                continue
            if filt and filt not in rel.lower():
                continue
            try:
                src = _read_text(_inside(root, rel))
            except (OSError, ToolError):
                continue
            if not push_all and _sha(src) == meta.get("sha"):
                continue
            todo.append({"path": meta["path"], "class": meta.get("className") or "Script", "code": src, "rel": rel})
        if not todo:
            return (f"workspace push: nothing to write - every mirrored file matches what was pulled "
                    f"({len(manifest['files'])} file(s) in {root}). Pass \"all\": true to rewrite them anyway.")
        res = write_scripts(studio, [{k: v for k, v in t.items() if k != "rel"} for t in todo],
                            history=_as_bool(args.get("history"), True),
                            check=_as_bool(args.get("check"), True),
                            force=_as_bool(args.get("force"), False), select=False)
        if res["blocked"]:
            return res["blocked"]
        for r, t in zip(res["results"], todo):
            if r["status"] in ("created", "updated", "unchanged"):
                meta = manifest["files"].get(t["rel"])
                if meta:
                    meta["sha"] = _sha(t["code"])
                    meta["bytes"] = _size(t["code"])
                    meta["pushed_at"] = time.time()
        _save_manifest(slug, manifest)
        return ("workspace push -> " + format_write(res["results"], None, res["history_error"], res["hard"], slug))

    if action == "status":
        manifest = _load_manifest(slug) if slug else {"files": {}}
        root = _ws_dir(slug) if slug else None
        out = [f"workspace status: \"{slug}\" - {len(manifest.get('files', {}))} mirrored script file(s) in {root}, "
               f"pulled {_fmt_age(manifest.get('pulled_at')) if manifest.get('pulled_at') else 'never'}."]
        by_dir = {}
        for rel in manifest.get("files", {}):
            d = rel.rsplit("/", 1)[0] if "/" in rel else "(root)"
            by_dir[d] = by_dir.get(d, 0) + 1
        for d in sorted(by_dir):
            out.append(f"  {d}/  {by_dir[d]} file(s)")
        dirty = []
        for rel, meta in sorted(manifest.get("files", {}).items()):
            try:
                if _sha(_read_text(_inside(root, rel))) != meta.get("sha"):
                    dirty.append(rel)
            except (OSError, ToolError):
                dirty.append(rel + " (unreadable)")
        if dirty:
            out.append(f"{len(dirty)} local edit(s) not pushed to Studio yet: " + ", ".join(dirty[:8])
                       + (" …" if len(dirty) > 8 else "") + '  -> workspace push')
        hist = history_entries(slug) if slug else []
        out.append(f"undo points: {len(hist)} snapshot(s)" + (f", newest {_fmt_age(hist[0]['ts'])}" if hist else ""))
        if _as_bool(args.get("compare_live"), False):
            inv = inventory(studio, want_tree=False, want_scripts=True, depth=24, max_nodes=20000)
            live = {s["path"]: s for s in inv["scripts"]}
            mirrored = {m["path"]: (rel, m) for rel, m in manifest.get("files", {}).items()}
            # live["bytes"] is Luau's #source; the manifest must be compared in
            # the same unit or every non-ASCII script reads as stale forever.
            changed = [p for p, (rel, m) in mirrored.items()
                       if p in live and live[p]["bytes"] != m.get("bytes", m.get("chars"))]
            added = [p for p in live if p not in mirrored]
            gone = [p for p in mirrored if p not in live]
            out.append(f"live in Studio: {len(live)} script(s) - {len(changed)} changed since the pull, "
                       f"{len(added)} not mirrored, {len(gone)} mirrored but gone from the place.")
            for p in (changed + added + gone)[:20]:
                tag = "CHANGED" if p in changed else ("NEW" if p in added else "GONE")
                out.append(f"  {tag:7} {p}")
            if changed or added:
                out.append("Run workspace pull to refresh the mirror (local edits are snapshotted first, never lost).")
        else:
            out.append('Pass "compare_live": true to diff the mirror against Studio (needs Studio).')
        return "\n".join(out)

    raise ToolError(f'unknown action "{action}" - use pull, push, grep or status.')


def _tool_studio_view(args, studio):
    paths = [normalize_path(p) for p in _as_list(args.get("select") or args.get("paths") or args.get("path"))]
    focus = normalize_path(args["focus"]) if args.get("focus") else ""
    clear = _as_bool(args.get("clear"), False)
    if not paths and not focus and not clear:
        raise ToolError('studio_view needs "select": [paths], "focus": path, or "clear": true.')
    payload = _pack_records([{"path": p} for p in paths]) if paths else ""
    text = studio.luau(_snippet("VIEW", _LUA_VIEW, payload=payload, focus=focus, clear=clear),
                       timeout=STUDIO_TIMEOUT)
    lines = parse_report(text, "VIEW")
    _note_place(_place_from(lines))
    ok = _report_fields(lines, "OK")
    fail = _report_fields(lines, "FAIL")
    miss = _report_fields(lines, "MISS")
    sel = _report_fields(lines, "SEL")
    out = ["studio_view: " + ("; ".join("\t".join(o) for o in ok) if ok else "nothing changed")]
    for f in fail:
        out.append("  FAIL " + "\t".join(f))
    for m in miss:
        out.append("  MISS " + "\t".join(m))
    if sel:
        out.append(f"  ({sel[0][0]} instance(s) are now selected in Studio's Explorer/viewport, so the user can see them)")
    if fail or miss:
        out.append("Check the paths with project_overview or search_game_tree, then try again.")
    return "\n".join(out)


PLAY_ERROR_RE = re.compile(
    r"\b(error|attempt to (index|call|compare|perform|yield)|nil value|stack traceback|infinite yield|"
    r"exception|is not a valid member|not enough memory|timed out|failed)\b", re.I)
PLAY_OK_RE = re.compile(r"^\s*(0 errors|no errors|error count: 0)\b", re.I)


def _tool_playtest(args, studio):
    seconds = _as_int(args.get("seconds"), 5, 1, 30)
    stop_after = _as_bool(args.get("stop"), True)
    capture = str(args.get("capture") or "both").lower()
    if capture not in ("server", "client", "both"):
        capture = "both"
    ok, text, err = studio.tool("start_stop_play", [{"is_start": True}])
    if not ok:
        raise ToolError(f"could not start play-testing: {err or text or 'start_stop_play refused'}. "
                        f"Studio may already be playing - try again with \"stop\": true, or check get_studio_state.")
    started = text
    time.sleep(seconds)   # runs in the bridge's worker thread; the socket keeps answering pings

    notes = []
    sent_input = False
    for kind, key in (("keyboard", "keys"), ("mouse", "mouse")):
        actions = args.get(key)
        if not actions:
            continue
        tool = f"user_{kind}_input"
        ok2, txt2, err2 = studio.tool(tool, [{"datamodel_type": "Client", "actions": actions}])
        sent_input = sent_input or bool(ok2)
        notes.append(f"{kind} input: {'sent' if ok2 else 'FAILED - ' + str(err2 or txt2)[:140]}")
    if sent_input:
        time.sleep(INPUT_SETTLE_S)
        notes.append(f"(waited {INPUT_SETTLE_S:g}s for that input to land before reading Output)")

    console = ""
    try:
        console = _console(studio, capture)
    except ToolError as e:
        console = f"── Output ──\n(could not read Output: {str(e).split(chr(10))[0][:140]})"

    stopped = None
    if stop_after:
        ok3, txt3, err3 = studio.tool("start_stop_play", [{"is_start": False}])
        stopped = "stopped" if ok3 else f"STILL PLAYING (stop failed: {str(err3 or txt3)[:120]})"

    body = [ln for ln in console.splitlines() if ln.strip()]
    problems = [ln.strip()[:200] for ln in body if PLAY_ERROR_RE.search(ln) and not PLAY_OK_RE.search(ln)]
    head = (f"playtest: play-tested for {seconds}s in Studio"
            + (f", {stopped}" if stopped else ", left RUNNING")
            + f" - {len(problems)} suspicious line(s) in Output.")
    out = [head]
    if started and started.strip():
        out.append(f"  start: {started.strip().splitlines()[0][:140]}")
    for n in notes:
        out.append("  " + n)
    if problems:
        out.append("── Possible errors in Output ──")
        seen = set()
        for p in problems[:20]:
            key = re.sub(r"\d+", "n", p)[:120]
            if key in seen:
                continue
            seen.add(key)
            out.append("  ! " + p)
        out.append("Fix these with script_write (whole file) or multi_edit (surgical), then playtest again.")
    else:
        out.append("── Output showed no errors ──")
    out.append(console)
    return "\n".join(out)


# ══════════════════════════════════════════════════════════════════════════
#  WHAT THE MODEL SEES: schemas, dispatch
# ══════════════════════════════════════════════════════════════════════════
# These are advertised next to Studio's own tools (server id "zeroscript"), in
# the same MCP shape, so list_commands renders them identically and the model
# calls them the same way. The first line of each description is what the
# compact command list shows, so it carries the "use this instead of that".
_S = {"type": "string"}
_N = {"type": "number"}
_B = {"type": "boolean"}


def _arr(items, description=None):
    d = {"type": "array", "items": items}
    if description:
        d["description"] = description
    return d


LOCAL_TOOLS = [
    {
        "name": "project_overview",
        "description": (
            "ONE call that maps the whole place: instance tree, every script (path, class, size, "
            "first line), recent Output, project memory and mirror status. Start work here instead of "
            "chaining search_game_tree / inspect_instance / script_read."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": dict(_S, description="Subtree to map (default game: everything)."),
                "depth": dict(_N, description="Tree depth (1-12). Default 4."),
                "max_nodes": dict(_N, description="Instance cap (20-5000). Default 400."),
                "scripts": dict(_B, description="Include the script inventory. Default true."),
                "console": dict(_B, description="Include recent Output. Default true."),
                "capture": dict(_S, description="Output: server|client|both. Default server."),
            },
        },
    },
    {
        "name": "script_write",
        "description": (
            "Create or overwrite WHOLE scripts - up to 40 files in one call, containers auto-created, "
            "linted first, every write read back from Studio, previous content snapshotted for undo. "
            "PREFER THIS over multi_edit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": _arr({
                    "type": "object",
                    "properties": {
                        "path": dict(_S, description="Full dot-path, e.g. game.ServerScriptService.Shop.Handler"),
                        "className": dict(_S, description="Script | LocalScript | ModuleScript. Default Script."),
                        "code": dict(_S, description="The COMPLETE source of the file (no escaping needed - it is not JSON-in-JSON)."),
                    },
                    "required": ["path", "code"],
                }, description="Files to write, in order (up to 40)."),
                "check": dict(_B, description="Lint first; refuse on errors. Default true."),
                "force": dict(_B, description="Write even if lint fails. Default false."),
                "history": dict(_B, description="Snapshot old content for undo. Default true."),
                "select": dict(_B, description="Select them in Studio. Default true."),
            },
            "required": ["files"],
        },
    },
    {
        "name": "script_verify",
        "description": (
            "Compile every script in the place (or only the ones named) inside Studio's own Luau VM "
            "and report errors with line numbers - the 'does it still build' step after a batch of "
            "writes, before telling the user something works."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": _arr(_S, description="Only these scripts (dot-paths). Default all."),
                "max": dict(_N, description="Script cap (1-2000). Default 400."),
            },
        },
    },
    {
        "name": "luau_check",
        "description": (
            "Static lint of Luau that runs HERE in the bridge: instant, no Studio call, and it works "
            "with Studio closed. Use it on a snippet before execute_luau (script_write already runs "
            "it)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": dict(_S, description="One snippet of Luau to check."),
                "files": _arr({
                    "type": "object",
                    "properties": {"path": _S, "code": _S},
                    "required": ["code"],
                }, description="Or several named snippets at once."),
                "context": dict(_S, description="script (default) | execute."),
            },
        },
    },
    {
        "name": "script_history",
        "description": (
            "Undo and diff for scripts: every script_write snapshots what it replaced, on disk in the "
            "bridge, so it survives restarts. List, diff or revert those snapshots."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": dict(_S, description="snapshot | list | diff | revert. Default list."),
                "path": dict(_S, description="Script dot-path (also a filter for list)."),
                "paths": _arr(_S, description="For snapshot: which scripts to capture. Default: all of them."),
                "entry": dict(_S, description="Snapshot id from list (default: newest)."),
                "live": dict(_B, description="diff: compare vs the live script. Default true."),
                "limit": dict(_N, description="How many snapshots to list. Default 12."),
                "place": dict(_S, description="Place name, if several places exist."),
            },
        },
    },
    {
        "name": "workspace",
        "description": (
            "The place's scripts as REAL .lua files on disk: pull mirrors them down, grep searches "
            "the whole place locally with NO Studio call, push writes local edits back, status shows "
            "the drift."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": dict(_S, description="pull | grep | push | status. Default status."),
                "pattern": dict(_S, description="For grep: text or regex to search for."),
                "path_contains": dict(_S, description="Only paths containing this (e.g. 'Shop')."),
                "files": _arr(_S, description="For push: only these mirrored files."),
                "paths": _arr(_S, description="For pull: only these scripts (default: all)."),
                "context": dict(_N, description="grep: context lines (0-4). Default 0."),
                "max_matches": dict(_N, description="grep: cap on matches. Default 60."),
                "max_files": dict(_N, description="For pull: cap on files mirrored. Default 300."),
                "all": dict(_B, description="push: rewrite every file, changed or not."),
                "compare_live": dict(_B, description="status: also compare with live Studio."),
                "auto_pull": dict(_B, description="grep: auto-pull if empty. Default true."),
                "check": dict(_B, description="For push: lint before writing (default true)."),
                "place": dict(_S, description="Place name, if several are mirrored."),
            },
        },
    },
    {
        "name": "studio_view",
        "description": (
            "Point the user's Studio at what you just built: select instances, frame one with the "
            "editor camera, or clear the selection - so they SEE the result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "select": _arr(_S, description="Dot-paths to select."),
                "focus": dict(_S, description="Dot-path to frame with the camera."),
                "clear": dict(_B, description="Clear the selection instead."),
            },
        },
    },
    {
        "name": "playtest",
        "description": (
            "Run the game and report what broke in ONE call: start play, let it run for "
            "\"seconds\", send any player input, capture Server and Client Output, flag "
            "suspicious lines, stop."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "seconds": dict(_N, description="Seconds to run (1-30). Default 5."),
                "stop": dict(_B, description="Stop play-testing after. Default true."),
                "capture": dict(_S, description="server|client|both. Default both."),
                # The action objects are forwarded to Studio's user_keyboard_input /
                # user_mouse_input verbatim, so their vocabularies are spelled out
                # here: an item schema of just {action: string} is what makes a
                # model send {"action": "keyPress"} with no key_code and get
                # "key_code is required" back. Per-item descriptions are not
                # rendered by the extension's reference (only the enum and the
                # array's own description are), so the required companions are
                # named in the array description.
                "keys": _arr({
                    "type": "object",
                    "properties": {
                        "action": dict(_S, enum=["keyPress", "keyDown", "keyUp", "textInput", "wait"]),
                        "key_code": dict(_S, description="Roblox KeyCode NAME: Return, Space, E, LeftShift."),
                        "text_inputs": dict(_S, description="The text for a textInput step."),
                        "wait_time_ms": dict(_N, description="For a wait step (0-10000)."),
                        "instance_path": dict(_S, description="Route input to a focused GUI element."),
                    },
                    "required": ["action"],
                }, description="Keyboard steps: key_code with key*, text_inputs with textInput, wait_time_ms with wait."),
                "mouse": _arr({
                    "type": "object",
                    "properties": {
                        "action": dict(_S, enum=["moveTo", "mouseButtonClick", "mouseButtonDown",
                                                 "mouseButtonUp", "scrollUp", "scrollDown", "wait"]),
                        "x": dict(_N, description="Screen pixels."),
                        "y": dict(_N, description="Screen pixels."),
                        "mouse_button": dict(_S, enum=["left", "right"]),
                        "instance_path": dict(_S, description="Aim at this instance instead of x/y."),
                        "wait_time_ms": dict(_N, description="For a wait step (0-10000)."),
                    },
                    "required": ["action"],
                }, description="Mouse steps: the FIRST needs x/y or instance_path; clicks need mouse_button."),
            },
        },
    },
]

LOCAL_TOOL_NAMES = frozenset(t["name"] for t in LOCAL_TOOLS)

_DISPATCH = {
    "project_overview": _tool_project_overview,
    "script_write": _tool_script_write,
    "script_verify": _tool_script_verify,
    "luau_check": _tool_luau_check,
    "script_history": _tool_script_history,
    "workspace": _tool_workspace,
    "studio_view": _tool_studio_view,
    "playtest": _tool_playtest,
}

# Tools that are useful with Studio closed - the extension can keep offering them
# in degraded mode instead of refusing everything.
STUDIO_FREE_TOOLS = frozenset({"luau_check", "script_history", "workspace"})

assert set(_DISPATCH) == set(LOCAL_TOOL_NAMES), "every local tool needs a schema and an implementation"


def is_local(name):
    """True if `name` (bare or 'zeroscript/'-prefixed) is a Dev Kit tool."""
    n = str(name or "")
    if "/" in n:
        n = n.split("/")[-1]
    return n in LOCAL_TOOL_NAMES


def bare(name):
    n = str(name or "")
    return n.split("/")[-1] if "/" in n else n


def run(name, args, studio, timeout=None):
    """Execute one Dev Kit tool. Returns {"text": ..., "images": []} exactly like
    an MCP tools/call result; raises ToolError for anything the model should see
    as a red ERROR chip."""
    fn = _DISPATCH.get(bare(name))
    if fn is None:
        raise ToolError(f"unknown ZeroScript command '{name}'")
    if not isinstance(args, dict):
        args = {}
    text = fn(args, studio)
    return {"text": _clip(text, bare(name)), "images": []}
