#!/usr/bin/env python3
"""Clawdmeter session-awareness sidecar — Claude Code hook listener.

Maintains a live table of open Claude Code sessions (state, context %, todo and
subagent counts) driven by Claude Code's hook events, and projects it onto the
compact wire format the firmware reads from the SS GATT characteristic
(4c41555a-4465-7669-6365-000000000005). See issue #135.

Runs two ways:

- **Standalone (Linux)** — `python3 clawdmeter_sessions.py` (or via the
  `clawdmeter-sessions.service` systemd user unit). Serves HTTP on
  127.0.0.1:<hook_port> and atomically writes `~/.clawdmeter/sessions.json`
  on every state change; the bash daemon ships that file's payload over BLE
  on its existing 5 s tick.
- **Standalone (Windows)** — the same command under `python`/`pythonw`; the
  Windows daemon reads the same `~/.clawdmeter/sessions.json` and ships it on
  its own 5 s tick. Config is read from `%LOCALAPPDATA%\\Clawdmeter\\config`
  when it exists. See daemon/README-windows.md.
- **Library** — the Python daemon (macOS/Windows) can import `SessionTable`,
  `fit_payload`, etc. and run the listener in-process.

Design rules carried from the issue:

- The listener is a read-only observer: it answers 204 No Content and never
  blocks or approves anything.
- Liveness comes from the session roster (`<config-dir>/sessions/<pid>.json`),
  NOT from activity timeouts — a chat blocked on a permission prompt is silent
  and must survive indefinitely (§4.2). 30 s grace before acting on roster
  absence; 6 h staleness sweep as backstop.
- The current tool is NOT cleared on PostToolUse (cleared on Stop and
  UserPromptSubmit), and `ntools` counts OPEN tool_use_ids, not a running
  total.
- Rows leave the host already sorted: (bucket, -last_event_at).
- Wire codes are append-only; ENDED rows never leave the host.

Python 3 stdlib only — no pip installs required.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Wire constants (§3, §5 of issue #135). State codes are append-only: they
# cross the BLE boundary, so renumbering would desync host/firmware pairs.
# ---------------------------------------------------------------------------

STATE_STARTING = 0
STATE_IDLE = 1
STATE_THINKING = 2
STATE_RESPONDING = 3
STATE_RUNNING_TOOL = 4
STATE_COMPACTING = 5
STATE_WAITING_PERMISSION = 6
STATE_WAITING_QUESTION = 7
STATE_WAITING_INPUT = 8
STATE_ERROR = 9
STATE_ENDED = 10  # never sent to the device

WAITING_STATES = frozenset(
    (STATE_WAITING_PERMISSION, STATE_WAITING_QUESTION, STATE_WAITING_INPUT, STATE_ERROR)
)
WORKING_STATES = frozenset(
    (STATE_THINKING, STATE_RESPONDING, STATE_RUNNING_TOOL, STATE_COMPACTING)
)

MODEL_CODES = (("opus", 1), ("sonnet", 2), ("haiku", 3), ("fable", 4))

TOOL_CODES = {
    "Bash": 1,
    "Read": 2,
    "Edit": 3,
    "Write": 4,
    "Grep": 5,
    "Glob": 6,
    "Task": 7,
    "WebFetch": 8,
    "WebSearch": 9,
}

# Remote Control, derived from the roster's `bridgeSessionId` (see
# remote_flag()). Same "unknown" convention as `ctx` / `tok`, so a roster we
# could not read stays distinguishable from a confirmed "off".
REMOTE_UNKNOWN = -1
REMOTE_OFF = 0
REMOTE_ON = 1

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

DEFAULT_BUDGET_BYTES = 180   # conservative fit target; see sessions_budget_bytes
LABEL_FLOOR = 8              # labels never elide below this many characters
# ASCII on purpose. This elides LABELS, which render in the Styrene faces, and
# those cover 32..126 only -- a real U+2026 is a placeholder box there. (The
# Hangul fallback does carry U+2026, but that font is reachable only from the
# message BODY; see fold_to_ascii below.) Same UTF-8 byte count (3), so the
# payload byte-budget math is unaffected either way.
ELLIPSIS = "..."

ROSTER_GRACE_S = 30          # roster absence tolerated this long (first hook may
                             # beat the roster file)
STALE_SWEEP_S = 6 * 3600     # backstop for an unreadable roster
SWEEP_INTERVAL_S = 5

DEFAULT_WINDOW = 200_000     # context window heuristic (§4.3)
ONE_M = 1_000_000

TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024
MAX_BODY_BYTES = 8 * 1024 * 1024

def default_config_file():
    """The daemon config file this sidecar shares with the platform daemon.

    Linux/macOS keep it at ~/.config/claude-usage-monitor/config. Windows has no
    XDG dir and its daemon already keeps config (and daemon.log) under
    %LOCALAPPDATA%\\Clawdmeter, so look there first — a Windows user should set
    hook_port in exactly one place — and fall back to the POSIX-style path so a
    config carried over from a Linux box still works.
    """
    posix = os.path.join(
        os.path.expanduser("~"), ".config", "claude-usage-monitor", "config"
    )
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local"
        )
        win = os.path.join(base, "Clawdmeter", "config")
        if os.path.exists(win) or not os.path.exists(posix):
            return win
    return posix


CONFIG_FILE = default_config_file()
DEFAULT_SESSIONS_FILE = os.path.join(
    os.path.expanduser("~"), ".clawdmeter", "sessions.json"
)

# Hook events the state machine consumes. Verified against the Claude Code
# hooks reference (code.claude.com/docs/en/hooks) — every name below is a real
# event. PostToolUseFailure is not in the issue's table but must pop its
# tool_use_id like PostToolUse, or a failed tool leaves ntools stuck.
HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "PermissionDenied",
    "Notification",
    "MessageDisplay",
    "Stop",
    "StopFailure",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "SessionEnd",
)
_HANDLED = frozenset(HOOK_EVENTS)


_FILE_LOGGER = None


def enable_file_log(filename="sessions.log", logger_name="clawdmeter.sessions"):
    """Mirror log output into %LOCALAPPDATA%\\Clawdmeter\\<filename> (Windows).

    Autostart launches the sidecar under pythonw.exe, which has no console at
    all: stdout is discarded and is in fact None. A rotating file is then the
    only trail there is when something goes wrong in the field — the same
    reasoning (and the same directory) as the Windows daemon's daemon.log.
    Called from main() and never at import, so importing this module as a
    library or unit-testing its helpers writes no files.

    The name is a parameter because the fleet poller is a SECOND autostarted,
    console-less process with the same problem and no business writing into
    the sidecar's log -- they are alternative producers of the same handoff
    file, so interleaving their lines would make both unreadable. It calls
    enable_file_log("fleet.log", "clawdmeter.fleet"); whichever of the two is
    running claims _FILE_LOGGER, and they are never both running.
    """
    global _FILE_LOGGER
    if sys.platform != "win32" or _FILE_LOGGER is not None:
        return
    import logging
    import logging.handlers
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local"
    )
    path = os.path.join(base, "Clawdmeter", filename)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
        )
    except OSError:
        return  # best-effort: logging setup must never stop the sidecar
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger = logging.getLogger(logger_name)
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    _FILE_LOGGER = logger


def file_log(msg):
    """Write one line to the rotating file log, if enable_file_log() set one up.

    Exposed so a second module (clawdmeter_fleet) can mirror its own prefixed
    output into the same machinery without borrowing this module's log()
    format on top of its own.
    """
    if _FILE_LOGGER is not None:
        _FILE_LOGGER.info(msg)


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    # Under pythonw.exe sys.stdout is None and print() raises AttributeError; a
    # missing console must never take the sidecar down (same guard as the
    # Windows daemon's log()).
    try:
        print(line, flush=True)
    except (OSError, ValueError, AttributeError, RuntimeError):
        pass
    if _FILE_LOGGER is not None:
        _FILE_LOGGER.info(msg)


# ---------------------------------------------------------------------------
# Config file (same file + format as the daemons: `key = value`, # comments)
# ---------------------------------------------------------------------------

def read_config_value(key, path=None):
    """Last-wins `key = value` lookup; trailing comments stripped. None if unset."""
    path = path or CONFIG_FILE
    pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*=\s*(.*)$")
    val = None
    try:
        # utf-8-sig strips a BOM, which Notepad writes by default and which
        # would otherwise glue itself to the first key and stop it matching.
        # ValueError catches an undecodable file: a background process the
        # user cannot see must not die over a stray byte in a comment.
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                m = pattern.match(line.rstrip("\r\n"))
                if m:
                    v = re.sub(r"\s*(#.*)?$", "", m.group(1)).strip()
                    val = v
    except (OSError, ValueError):
        return None
    return val or None


def read_config_dirs(path=None):
    """Claude config dirs to consult for rosters/transcripts (default ~/.claude)."""
    raw = read_config_value("config_dirs", path)
    home = os.path.expanduser("~")
    if not raw:
        return [os.path.join(home, ".claude")]
    dirs = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "~":
            part = home
        elif part.startswith("~/"):
            part = os.path.join(home, part[2:])
        dirs.append(part)
    return dirs or [os.path.join(home, ".claude")]


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------

def state_bucket(state):
    """§2.2: 0 waiting, 1 working, 2 idle. Sort key is (bucket, -last_event_at)."""
    if state in WAITING_STATES:
        return 0
    if state in WORKING_STATES:
        return 1
    return 2


def model_code(model_str):
    if not model_str:
        return 0
    lowered = model_str.lower()
    for name, code in MODEL_CODES:
        if name in lowered:
            return code
    return 0


def tool_code(tool_name):
    return TOOL_CODES.get(tool_name or "", 0)


def elide_label(label, max_chars):
    """Middle-elide to max_chars characters, preserving the trailing
    discriminator — `clawdmeter-36` and `clawdmeter-2c` must stay distinct."""
    if len(label) <= max_chars:
        return label
    if max_chars <= len(ELLIPSIS):
        return label[:max_chars]
    # The ellipsis itself has to come out of the budget. ELLIPSIS was a 1-char
    # "…" when this was written; it is now the 3-char ASCII "...", so
    # budgeting a single character for it overshot max_chars by 2.
    budget = max_chars - len(ELLIPSIS)
    tail = budget // 2
    head = budget - tail
    return label[:head] + ELLIPSIS + label[len(label) - tail:]


# ---------------------------------------------------------------------------
# Folding text into what the panel can actually draw
# ---------------------------------------------------------------------------
# The device's fonts are the constraint, and there are exactly two coverages:
# the brand faces (Styrene, Tiempos) carry ASCII 32..126, and the message
# body's fallback face (firmware/src/font_nanum_kr_28.c) carries the 2,350
# KS X 1001 Hangul syllables. A codepoint in neither draws as a placeholder
# box.
#
# This lives HERE rather than in clawdmeter_inbox because EVERY label on the
# wire passes through fit_payload() below, and every label needs folding: a
# Korean project directory name used to arrive as a row of empty boxes in the
# largest font on the screen, while the inbox's own sender field -- the only
# label anyone had thought about -- was already being folded. clawdmeter_inbox
# re-exports these names, and adds the message-body policy on top (it is the
# one field allowed to keep its Hangul).

# Punctuation that has an obvious ASCII equivalent. NFKD does not fold these
# (an em dash is not a decomposable hyphen), so they need naming.
_PUNCT = {
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "―": "-", "−": "-", "•": "-", "·": "-",
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "′": "'", "‹": "'", "›": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "«": '"', "»": '"', "″": '"',
    "…": "...", "\u00a0": " ", "\u200b": "", "\ufeff": "",
    "、": ",", "。": ".", "，": ",", "．": ".",
    "：": ":", "；": ";", "！": "!", "？": "?",
    "×": "x", "→": "->", "←": "<-", "✓": "ok",
}

# Hangul -> Revised Romanization, syllable by syllable. The owner writes
# Korean, and a Korean message rendered as "?" is useless where "polring
# jugi" is readable. Tables are the standard RR initial / medial / final sets.
_HANGUL_BASE = 0xAC00
_HANGUL_LAST = 0xD7A3
_INITIALS = ("g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "",
             "j", "jj", "ch", "k", "t", "p", "h")
_MEDIALS = ("a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae",
            "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i")
_FINALS = ("", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "p", "l",
           "l", "p", "l", "m", "p", "p", "t", "t", "ng", "t", "t", "k", "t",
           "p", "t")


def romanize_hangul(ch):
    """One precomposed Hangul syllable -> Revised Romanization, or None.

    Syllable-wise and therefore APPROXIMATE: RR's inter-syllable assimilation
    rules are not applied, so 학년 comes out "haknyeon" where the standard
    spells it "hangnyeon". Readable, not authoritative -- and the alternative
    on this panel is a blank.
    """
    code = ord(ch)
    if not (_HANGUL_BASE <= code <= _HANGUL_LAST):
        return None
    idx = code - _HANGUL_BASE
    return _INITIALS[idx // 588] + _MEDIALS[(idx % 588) // 28] + _FINALS[idx % 28]


def _ksx1001_hangul():
    """The 2350 precomposed Hangul syllables of KS X 1001 -- EXACTLY the set
    firmware/src/font_nanum_kr_28.c contains, derived the same way it was so
    the two cannot drift.

    The lead-byte test is what does the work, not the encode: CPython's
    `euc_kr` codec is really CP949 and encodes all 11,172 precomposed
    syllables without error, so "does it encode?" filters nothing. The KS X
    1001 wansung syllables are the ones in rows 0xB0..0xC8.

    Passing a syllable the font lacks would put a placeholder box on the panel
    -- the exact failure the Hangul font was added to remove -- so this set is
    the contract between host and firmware, and tools/ttf_to_lvgl.py's
    --ksx1001 is the other half of it.
    """
    out = set()
    for cp in range(_HANGUL_BASE, _HANGUL_LAST + 1):
        try:
            enc = chr(cp).encode("euc_kr")
        except UnicodeEncodeError:
            continue
        if len(enc) == 2 and 0xB0 <= enc[0] <= 0xC8:
            out.add(cp)
    return frozenset(out)


KSX1001_HANGUL = _ksx1001_hangul()

# Shown when folding leaves nothing legible at all (a body that is pure emoji,
# or pure CJK). Honest: says something arrived and that the panel cannot show
# it.
UNREADABLE_TEXT = "[non-ASCII msg]"

_DROP = "\x00"  # internal placeholder for one untranslatable character


def fold_to_ascii(text, translit=True, keep_hangul=False):
    """Make `text` renderable on the panel, honestly.

    Passes, most faithful first:

    1. ASCII passes through untouched.
    2. Named punctuation maps to its ASCII twin (em dash -> "-", curly quotes
       -> straight, U+2026 -> "...").
    3. With `keep_hangul`, a syllable the device's Hangul font actually has
       passes through AS ITSELF. Off by default, and it must stay off for
       every field except the message body: the body is the only one whose
       font has the Hangul fallback (see MSG_BODY_FONT in ui.cpp), so Hangul
       in a sender or a session label would be placeholder boxes.
    4. Hangul is transliterated (see romanize_hangul) when `translit`. This is
       what catches the 8,822 syllables outside KS X 1001 even when
       `keep_hangul` is on -- the font does not have them.
    5. Anything else is NFKD-normalised and stripped of combining marks, which
       is a genuine transliteration for accented Latin ("café" -> "cafe")
       and nothing at all for CJK, Cyrillic or emoji.

    What survives none of that is DROPPED, and each dropped RUN is replaced by
    a single "?" -- so the reader can see that something was there and that
    the panel could not show it. That is the whole point: a blank would lie by
    omission and per-character "?????" would drown the words that did survive.
    """
    out = []
    for ch in text:
        if " " <= ch <= "~":
            out.append(ch)
            continue
        if ch.isspace():
            # Newlines, tabs, NBSP, ideographic space: whitespace, not
            # untranslatable. Dropping them would put a "?" between every
            # paragraph of a perfectly ASCII message.
            out.append(" ")
            continue
        rep = _PUNCT.get(ch)
        if rep is not None:
            out.append(rep)
            continue
        if keep_hangul and ord(ch) in KSX1001_HANGUL:
            out.append(ch)
            continue
        if translit:
            rom = romanize_hangul(ch)
            if rom is not None:
                out.append(rom)
                continue
        keep = "".join(c for c in unicodedata.normalize("NFKD", ch)
                       if " " <= c <= "~")
        out.append(keep if keep else _DROP)
    # Collapse each run of dropped characters to one marker.
    return re.sub(_DROP + "+", "?", "".join(out))


def to_panel_text(body, translit=True, keep_hangul=False):
    """A body -> one folded, single-line string a card can hold."""
    body = body or ""
    folded = fold_to_ascii(body, translit, keep_hangul)
    single = re.sub(r"\s+", " ", folded).strip()
    # "Did anything readable survive?" -- Hangul counts as readable exactly
    # when it was allowed through, or a pure-Korean message would be declared
    # unreadable by the very pass that made it readable.
    survivors = r"[A-Za-z0-9가-힣]" if keep_hangul else r"[A-Za-z0-9]"
    if not re.search(survivors, single):
        # Nothing readable left. Say so -- but only if the folding is what ate
        # it: a body that was always just ":)" is not unreadable, it is short.
        if any(not (" " <= c <= "~") and not c.isspace() for c in body):
            return UNREADABLE_TEXT
    return single


def panel_label(label):
    """A row's label field, folded to what the panel can draw.

    ALWAYS folds Hangul away: labels render in font_styrene_20/24/28/48 and
    none of those has a Hangul fallback, so a Korean project name would be a
    row of empty boxes in the biggest font on the screen. Romanised is not
    ideal; boxes are useless.

    Returns "?" rather than "" for a label that folds to nothing -- a card
    with no name at all reads as a rendering bug, and the sid is not shown.
    """
    single = re.sub(r"\s+", " ", fold_to_ascii(label or "")).strip()
    return single or "?"


def encode_payload(rows):
    return json.dumps({"ss": rows}, separators=(",", ":"), ensure_ascii=False)


def fit_payload(rows, budget):
    """Fit already-sorted rows into `budget` bytes (UTF-8): first shrink labels
    (middle-elide, 8-char floor), then drop rows from the tail — never from the
    front, so a waiting chat is never the one dropped (§5).

    The measurement is of the fully encoded row, so every appended field (`tok`,
    `remote`, whatever comes next) is paid for out of the same budget: a longer
    row elides labels sooner and, past the floor, drops the least urgent row.
    Nothing is ever emitted over budget.

    Labels are folded here (panel_label) rather than where they are produced,
    because there are two producers — `Session.label()` for local chats and
    `clawdmeter_fleet._label_of()` for remote ones — and this is the single
    funnel both go through on the way to the wire. It also has to happen
    BEFORE the elide loop: folding changes the character count (한 -> "han"),
    so eliding first would blow the cap it was measured against."""
    def nbytes(s):
        return len(s.encode("utf-8"))

    for n in range(len(rows), -1, -1):
        subset = [list(r) for r in rows[:n]]
        originals = [panel_label(r[1]) for r in subset]
        max_label = max((len(lbl) for lbl in originals), default=LABEL_FLOOR)
        for cap in range(max(max_label, LABEL_FLOOR), LABEL_FLOOR - 1, -1):
            for row, orig in zip(subset, originals):
                row[1] = elide_label(orig, cap)
            payload = encode_payload(subset)
            if nbytes(payload) <= budget:
                return payload
        # Even at the label floor these rows don't fit -> drop the tail row.
    return encode_payload([])


def compute_window(observed_tokens, model_str, pinned_k=None):
    """Context window heuristic (§4.3): 200k default, 1M on a `[1m]` marker,
    snap up to the next 1M multiple if observed usage exceeds the assumption.
    A pinned `context_window_k` is fact, not a guess — no snap-up."""
    if pinned_k:
        return int(pinned_k) * 1000
    window = ONE_M if (model_str and "[1m]" in model_str) else DEFAULT_WINDOW
    if observed_tokens and observed_tokens > window:
        window = ((observed_tokens + ONE_M - 1) // ONE_M) * ONE_M
    return window


def context_percent(observed_tokens, model_str, pinned_k=None):
    """-1 when unknown; otherwise 0..100 against the heuristic window."""
    if observed_tokens is None:
        return -1
    window = compute_window(observed_tokens, model_str, pinned_k)
    if window <= 0:
        return -1
    return max(0, min(100, round(observed_tokens * 100 / window)))


def tokens_k(observed_tokens):
    """Absolute context tokens in 1k units, rounded half-up (deterministic —
    Python's round() would banker-round 160500 down). -1 when unknown."""
    if observed_tokens is None:
        return -1
    return int((observed_tokens + 500) // 1000)


def munge_cwd(cwd):
    """Claude Code's project-dir munging: every non-alphanumeric becomes '-'.
    ('/home/x/JBT Marel/Clawdmeter' -> '-home-x-JBT-Marel-Clawdmeter')"""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def short_sid(session_id):
    """2 hex chars, stable for the session's life. Session ids are UUIDs, so
    the first two chars are already hex; hash as a fallback. A collision only
    degrades the reorder animation (§5) — cosmetic."""
    prefix = session_id[:2].lower()
    if re.fullmatch(r"[0-9a-f]{2}", prefix):
        return prefix
    return hashlib.md5(session_id.encode("utf-8")).hexdigest()[:2]


# ---------------------------------------------------------------------------
# Transcript reading (context %, model) — §4.3
# ---------------------------------------------------------------------------

def read_context_from_transcript(path):
    """Newest non-sidechain assistant record's usage:
    input + cache_read_input + cache_creation_input tokens.
    Returns (tokens, model_str), or (None, None) when unreadable/absent.
    Reads only the file tail — transcripts grow to many MB."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > TRANSCRIPT_TAIL_BYTES:
                fh.seek(size - TRANSCRIPT_TAIL_BYTES)
                fh.readline()  # discard the partial line
            data = fh.read()
    except OSError:
        return (None, None)

    for line in reversed(data.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        if rec.get("isSidechain"):
            continue  # subagent turns would inflate the count
        message = rec.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        tokens = 0
        for field in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            v = usage.get(field)
            if isinstance(v, (int, float)):
                tokens += int(v)
        return (tokens, message.get("model"))
    return (None, None)


# ---------------------------------------------------------------------------
# Session roster (liveness + names) — §4.2
# ---------------------------------------------------------------------------

def _proc_starttime(pid):
    """Field 22 (starttime) of /proc/<pid>/stat, or None if the pid is gone.
    comm (field 2) may contain spaces/parens, so split after the last ')'."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            stat = fh.read().decode("ascii", "replace")
        rest = stat[stat.rindex(")") + 2:].split()
        return rest[19]
    except (OSError, ValueError, IndexError):
        return None


# Win32 bits for _win_pid_alive(). PROCESS_QUERY_LIMITED_INFORMATION is the
# least privilege that can read a process's times, and unlike
# PROCESS_QUERY_INFORMATION it is granted across integrity levels.
_WIN_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WIN_ERROR_ACCESS_DENIED = 5
_WIN_K32 = None


def _win_kernel32():
    """Lazily bind the kernel32 calls the Windows liveness check needs."""
    global _WIN_K32
    if _WIN_K32 is None:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # restype MUST be set: the default is c_int, which truncates a 64-bit
        # HANDLE and would both misreport failure and leak the handle.
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        ft_p = ctypes.POINTER(wintypes.FILETIME)
        k32.GetProcessTimes.argtypes = (wintypes.HANDLE, ft_p, ft_p, ft_p, ft_p)
        k32.GetProcessTimes.restype = wintypes.BOOL
        _WIN_K32 = (ctypes, wintypes, k32)
    return _WIN_K32


def _win_process_times(handle):
    """(creation, exit) FILETIMEs as ints behind an open process handle, or None.

    Both come from one GetProcessTimes call, which needs nothing beyond
    PROCESS_QUERY_LIMITED_INFORMATION. Exit time is 0 for a running process and
    non-zero once it has ended, which is the exact liveness answer — a process
    handle can outlive the process (anything still holding one keeps the pid
    resolvable), so "OpenProcess worked" on its own means nothing.
    WaitForSingleObject would answer the same question but needs SYNCHRONIZE
    access, which this handle deliberately does not ask for.
    """
    ctypes, wintypes, k32 = _win_kernel32()
    created, exited = wintypes.FILETIME(), wintypes.FILETIME()
    kernel, user = wintypes.FILETIME(), wintypes.FILETIME()
    if not k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                               ctypes.byref(kernel), ctypes.byref(user)):
        return None
    return ((created.dwHighDateTime << 32) | created.dwLowDateTime,
            (exited.dwHighDateTime << 32) | exited.dwLowDateTime)


def _win_proc_starttime(pid):
    """Windows analogue of _proc_starttime(): the creation FILETIME (100 ns ticks
    since 1601) of a RUNNING process, or None. Claude Code writes exactly this
    value into the roster as `procStart` on Windows — verified against a live
    roster on Windows 11 — so the pid-reuse check compares the two directly."""
    try:
        _ctypes, _wintypes, k32 = _win_kernel32()
    except (OSError, AttributeError, ImportError):  # pragma: no cover - not Windows
        return None
    handle = k32.OpenProcess(_WIN_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        times = _win_process_times(handle)
    finally:
        k32.CloseHandle(handle)
    if times is None or times[1]:
        return None  # unreadable, or already exited
    return times[0]


def _win_pid_alive(pid, proc_start=None):
    """Windows counterpart of the /proc branch of pid_alive().

    Same identity test as Linux — pid AND process start time — because a roster
    file outlives its process and Windows recycles pids aggressively.

    os.kill(pid, 0) is deliberately not used as the primary check: it cannot
    tell the original process from whatever inherited its pid, and it reports an
    exited-but-still-referenced process as alive. Anything that cannot be
    determined resolves to "alive" — dropping a live session is the worse error,
    and the 6 h staleness sweep is the backstop.
    """
    try:
        ctypes, _wintypes, k32 = _win_kernel32()
    except (OSError, AttributeError, ImportError):  # pragma: no cover - not Windows
        return True
    handle = k32.OpenProcess(_WIN_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # ERROR_ACCESS_DENIED: the process exists but this token may not query it
        # (the PermissionError branch of the POSIX path). Anything else
        # (ERROR_INVALID_PARAMETER) means there is no process with that pid.
        return ctypes.get_last_error() == _WIN_ERROR_ACCESS_DENIED
    try:
        times = _win_process_times(handle)
    finally:
        k32.CloseHandle(handle)
    if times is None:
        return True  # running, just not inspectable
    created, exited = times
    if exited:
        return False
    if proc_start is None:
        return True
    try:
        # The roster writes procStart as a decimal string on Windows; compare
        # numerically so an int would work too. A shape we do not recognise is
        # not evidence of pid reuse — keep the session and let the staleness
        # sweep deal with it.
        return int(str(proc_start).strip()) == created
    except (TypeError, ValueError):
        return True


def pid_alive(pid, proc_start=None):
    """Is this roster entry's process still running? Roster files can outlive a
    crashed process, so presence alone isn't liveness. The roster records
    procStart (jiffies, /proc/<pid>/stat field 22 on Linux; the process creation
    FILETIME on Windows) precisely so pid reuse can be told apart from the
    original process."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.path.isdir("/proc"):
        start = _proc_starttime(pid)
        if start is None:
            return False
        if proc_start is not None and str(proc_start) != start:
            return False  # pid was reused by another process
        return True
    if sys.platform == "win32":
        return _win_pid_alive(pid, proc_start)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def load_roster(config_dirs):
    """Union of `<dir>/sessions/*.json` across config dirs, keyed by sessionId.
    Returns (roster, readable). readable=False means no roster dir could be
    listed at all — liveness must not be enforced then (§9: sessions linger,
    nothing disappears wrongly)."""
    roster = {}
    readable = False
    for cdir in config_dirs:
        sdir = os.path.join(cdir, "sessions")
        try:
            names = os.listdir(sdir)
        except OSError:
            continue
        readable = True
        for fn in names:
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(sdir, fn), encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and rec.get("sessionId"):
                roster[rec["sessionId"]] = rec
    return roster, readable


def remote_flag(rec):
    """Remote Control for one roster record: 1 = on, 0 = off, -1 = unknown.

    Claude Code writes `bridgeSessionId` into the roster entry once a bridge
    handle is installed — Remote Control proper, the SDK-hosted bridge, or a
    supervised child of a bridge session. Teardown and switching Remote Control
    off write it back to `null`, so null/absent is a sound "off".

    What it deliberately does NOT claim: that a phone or browser is attached
    right now (that state lives only on Anthropic's servers — there is no local
    signal), nor that the session is unusual — where the auto-start rollout is
    active, every interactive session carries a bridge id. An unrecognised
    value shape reports unknown rather than guessing. The id itself never
    leaves the host: it is a server-side identifier the device has no use for.
    """
    if not isinstance(rec, dict):
        return REMOTE_UNKNOWN
    if "bridgeSessionId" not in rec:
        return REMOTE_OFF
    bridge = rec["bridgeSessionId"]
    if bridge is None:
        return REMOTE_OFF
    if isinstance(bridge, str):
        return REMOTE_ON if bridge.strip() else REMOTE_OFF
    return REMOTE_UNKNOWN


# ---------------------------------------------------------------------------
# The session table + state machine (§4.1)
# ---------------------------------------------------------------------------

class Session:
    __slots__ = (
        "session_id", "sid", "state", "state_since", "last_event_at",
        "roster_name", "cwd", "transcript_path", "current_tool", "open_tools",
        "nagents", "tdone", "ttotal", "ctx", "tok", "model", "missing_since",
        "remote",
    )

    def __init__(self, session_id, now):
        self.session_id = session_id
        self.sid = short_sid(session_id)
        self.state = STATE_STARTING
        self.state_since = now
        self.last_event_at = now
        self.roster_name = None
        self.cwd = None
        self.transcript_path = None
        self.current_tool = None   # last tool NAME; survives PostToolUse
        self.open_tools = []       # OPEN tool_use_ids — concurrent, not cumulative
        self.nagents = 0
        self.tdone = 0
        self.ttotal = 0
        self.ctx = -1
        self.tok = -1  # context tokens in 1k units; -1 whenever ctx is -1
        self.model = 0
        self.remote = REMOTE_UNKNOWN  # Remote Control; filled by the roster read
        self.missing_since = None  # first time the roster didn't vouch for us

    def label(self):
        if self.roster_name:
            return self.roster_name
        if self.cwd:
            base = os.path.basename(self.cwd.rstrip("/"))
            if base:
                return base
        return self.session_id[:8]


class SessionTable:
    """Hook-driven state machine over the live sessions. Thread-safe."""

    def __init__(self, pinned_window_k=None, config_dirs=None, now_fn=time.time):
        self.pinned_window_k = pinned_window_k
        self.config_dirs = config_dirs if config_dirs is not None else read_config_dirs()
        self.now_fn = now_fn
        self.sessions = {}  # session_id -> Session
        self._lock = threading.RLock()

    # -- event intake -------------------------------------------------------

    def handle_event(self, payload):
        """Apply one hook payload. Returns True if the table changed.
        Unknown events and malformed payloads are ignored gracefully."""
        if not isinstance(payload, dict):
            return False
        event = payload.get("hook_event_name")
        session_id = payload.get("session_id")
        if event not in _HANDLED or not isinstance(session_id, str) or not session_id:
            return False
        now = self.now_fn()
        with self._lock:
            if event == "SessionEnd":
                # Dropped immediately; ENDED rows never leave the host.
                return self.sessions.pop(session_id, None) is not None

            sess = self.sessions.get(session_id)
            if sess is None:
                sess = Session(session_id, now)
                self.sessions[session_id] = sess

            tp = payload.get("transcript_path")
            if isinstance(tp, str) and tp:
                sess.transcript_path = tp
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd:
                sess.cwd = cwd

            sess.last_event_at = now
            sess.missing_since = None  # a hook is proof of life
            self._apply(sess, event, payload, now)
            return True

    def _set_state(self, sess, state, now):
        if sess.state != state:
            sess.state = state
            sess.state_since = now

    def _apply(self, sess, event, payload, now):
        if event == "SessionStart":
            self._set_state(sess, STATE_IDLE, now)
            self._refresh_context(sess)

        elif event == "UserPromptSubmit":
            # A new turn: the previous turn's tool is genuinely over.
            sess.current_tool = None
            sess.open_tools.clear()
            self._set_state(sess, STATE_THINKING, now)

        elif event == "PreToolUse":
            tool = payload.get("tool_name")
            tuid = payload.get("tool_use_id")
            if tuid is None or tuid not in sess.open_tools:
                sess.open_tools.append(tuid)
            if isinstance(tool, str) and tool:
                sess.current_tool = tool
            if tool == "AskUserQuestion":
                self._set_state(sess, STATE_WAITING_QUESTION, now)
            else:
                self._set_state(sess, STATE_RUNNING_TOOL, now)

        elif event in ("PostToolUse", "PostToolUseFailure"):
            tuid = payload.get("tool_use_id")
            if tuid is not None and tuid in sess.open_tools:
                sess.open_tools.remove(tuid)
            elif tuid is None and sess.open_tools:
                sess.open_tools.pop()
            elif None in sess.open_tools:
                sess.open_tools.remove(None)
            # NOTE: sess.current_tool is deliberately NOT cleared here — between
            # two tools in one turn the state line would flicker (§4.1). It is
            # cleared on Stop and UserPromptSubmit.
            if event == "PostToolUse" and payload.get("tool_name") == "TodoWrite":
                todos = (payload.get("tool_input") or {}).get("todos")
                if isinstance(todos, list):
                    sess.ttotal = len(todos)
                    sess.tdone = sum(
                        1 for t in todos
                        if isinstance(t, dict) and t.get("status") == "completed"
                    )
            # Only tool-driven states advance here; a session waiting on a
            # permission prompt (or compacting) must not be clobbered by an
            # unrelated tool completing.
            if sess.state in (STATE_RUNNING_TOOL, STATE_WAITING_QUESTION):
                self._set_state(
                    sess,
                    STATE_RUNNING_TOOL if sess.open_tools else STATE_THINKING,
                    now,
                )

        elif event == "PermissionRequest":
            # Keep the tool name so the device can say what is being asked
            # for, not just that something is. PreToolUse normally fires
            # first and has already set current_tool, but the permission
            # payload carries it too on some paths -- prefer the fresher one.
            tool = payload.get("tool_name")
            if isinstance(tool, str) and tool:
                sess.current_tool = tool
            self._set_state(sess, STATE_WAITING_PERMISSION, now)

        elif event == "PermissionDenied":
            self._set_state(sess, STATE_THINKING, now)

        elif event == "Notification":
            ntype = payload.get("notification_type")
            if ntype == "permission_prompt":
                tool = payload.get("tool_name")
                if isinstance(tool, str) and tool:
                    sess.current_tool = tool
                self._set_state(sess, STATE_WAITING_PERMISSION, now)
            elif ntype in ("agent_needs_input", "elicitation_dialog"):
                self._set_state(sess, STATE_WAITING_INPUT, now)
            elif ntype in ("idle_prompt", "agent_completed"):
                self._set_state(sess, STATE_IDLE, now)
            # other notification types (auth_success, ...) carry no state

        elif event == "MessageDisplay":
            self._set_state(sess, STATE_RESPONDING, now)

        elif event == "Stop":
            sess.current_tool = None
            sess.open_tools.clear()
            self._set_state(sess, STATE_IDLE, now)
            self._refresh_context(sess)

        elif event == "StopFailure":
            self._set_state(sess, STATE_ERROR, now)

        elif event == "PreCompact":
            self._set_state(sess, STATE_COMPACTING, now)

        elif event == "PostCompact":
            self._set_state(sess, STATE_IDLE, now)
            self._refresh_context(sess)

        elif event == "SubagentStart":
            sess.nagents += 1

        elif event == "SubagentStop":
            sess.nagents = max(0, sess.nagents - 1)

    # -- context (§4.3): hook-driven re-reads, never polled ------------------

    def _refresh_context(self, sess):
        path = sess.transcript_path or self._guess_transcript(sess)
        if not path:
            return
        tokens, model_str = read_context_from_transcript(path)
        if model_str:
            sess.model = model_code(model_str)
        sess.ctx = context_percent(tokens, model_str, self.pinned_window_k)
        # tok mirrors the SAME read: the same token sum in 1k units, not divided
        # by the window. Forced to -1 whenever ctx is -1 so the pair can never
        # disagree on the wire.
        sess.tok = tokens_k(tokens) if sess.ctx != -1 else -1

    def _guess_transcript(self, sess):
        """Fallback when no hook carried transcript_path:
        <config-dir>/projects/<munged-cwd>/<session-id>.jsonl"""
        if not sess.cwd:
            return None
        munged = munge_cwd(sess.cwd)
        for cdir in self.config_dirs:
            cand = os.path.join(cdir, "projects", munged, sess.session_id + ".jsonl")
            if os.path.isfile(cand):
                return cand
        return None

    # -- liveness sweep (§4.2) ------------------------------------------------

    def sweep(self):
        """Roster-based liveness + 6 h staleness backstop + label refresh.
        Returns True if anything wire-visible changed."""
        now = self.now_fn()
        changed = False
        with self._lock:
            if not self.sessions:
                return False
            roster, readable = load_roster(self.config_dirs)
            for session_id, sess in list(self.sessions.items()):
                if now - sess.last_event_at > STALE_SWEEP_S:
                    del self.sessions[session_id]
                    changed = True
                    continue
                if not readable:
                    # Roster unreadable: liveness can't be confirmed either way.
                    # Sessions linger (6 h backstop above); nothing disappears
                    # wrongly.
                    continue
                rec = roster.get(session_id)
                if rec is not None:
                    # Remote Control rides along with the liveness read — same
                    # file, no extra I/O, and the roster is the only local place
                    # this shows up at all. Unknown (-1) until a roster record
                    # is actually seen, exactly like ctx/tok.
                    remote = remote_flag(rec)
                    if remote != sess.remote:
                        sess.remote = remote
                        changed = True
                if rec is not None and pid_alive(rec.get("pid"), rec.get("procStart")):
                    sess.missing_since = None
                    name = rec.get("name")
                    if isinstance(name, str) and name and name != sess.roster_name:
                        sess.roster_name = name
                        changed = True
                else:
                    if sess.missing_since is None:
                        sess.missing_since = now  # grace starts; not wire-visible
                    elif now - sess.missing_since > ROSTER_GRACE_S:
                        del self.sessions[session_id]
                        changed = True
        return changed

    # -- projection (§5) ------------------------------------------------------

    def rows(self):
        """Full-label rows, already sorted: (bucket, -last_event_at).
        Row: [sid, label, state, ctx, elapsed_s, model, tool, ntools,
              nagents, tdone, ttotal, tok, remote] — append-only, like the
        codes: new fields go on the end and old firmware just ignores them."""
        now = self.now_fn()
        with self._lock:
            ordered = sorted(
                self.sessions.values(),
                key=lambda s: (state_bucket(s.state), -s.last_event_at),
            )
            return [
                [
                    s.sid,
                    s.label(),
                    s.state,
                    s.ctx,
                    max(0, int(now - s.state_since)),
                    s.model,
                    tool_code(s.current_tool),
                    len(s.open_tools),
                    s.nagents,
                    s.tdone,
                    s.ttotal,
                    s.tok,
                    s.remote,
                ]
                for s in ordered
            ]

    def project(self, budget=DEFAULT_BUDGET_BYTES):
        return fit_payload(self.rows(), budget)


# ---------------------------------------------------------------------------
# sessions.json handoff (Linux sidecar -> bash daemon)
# ---------------------------------------------------------------------------

def _replace_with_retry(tmp, path, attempts=5, delay=0.02):
    """os.replace, tolerating a reader that happens to hold the target open.

    POSIX rename always wins. Windows MoveFileEx fails with a sharing violation
    (PermissionError) while another process has the destination open without
    FILE_SHARE_DELETE — which is exactly what a daemon reading sessions.json
    does, for the microseconds it lasts. Retry briefly; if it still loses, the
    caller logs it and the next publish rewrites the same content anyway.
    """
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def write_sessions_file(path, payload, index=None):
    """Atomic write (temp + rename). `payload` is the exact wire string; the
    daemon ships it verbatim, so it is stored as a string, not re-encoded.

    `index` is the sid lookup that goes with it: {sid: {...}}. It exists
    because the panel talks back. A tap on a card sends two characters over
    BLE, and the process that RECEIVES that -- the BLE daemon -- is not the
    process that minted the sid, so without a written-down mapping it cannot
    turn "g4" into an agent to message. Optional, and ignored by every reader
    that predates it: the payload contract is untouched.
    """
    doc = {"ts": round(time.time(), 3), "payload": payload}
    if index:
        doc["index"] = index
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"), ensure_ascii=False)
        fh.write("\n")
    _replace_with_retry(tmp, path)


# ---------------------------------------------------------------------------
# HTTP listener — loopback only, read-only observer
# ---------------------------------------------------------------------------

class HookServer(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR means opposite things on the two platforms. On POSIX it only
    # skips the TIME_WAIT wait. On Windows it lets a SECOND process bind a port
    # another process is already listening on, and the two then split the hook
    # POSTs between them — two half-populated session tables, no error anywhere.
    # Off there, so a duplicate sidecar fails loudly with "cannot bind".
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, addr, table, sessions_file, budget):
        super().__init__(addr, HookHandler)
        self.table = table
        self.sessions_file = sessions_file
        self.budget = budget
        self._publish_lock = threading.Lock()
        self._last_payload = None

    def publish(self):
        payload = self.table.project(self.budget)
        with self._publish_lock:
            if payload == self._last_payload:
                return
            self._last_payload = payload
            try:
                write_sessions_file(self.sessions_file, payload)
            except OSError as exc:
                log(f"sessions.json write failed: {exc}")


class HookHandler(BaseHTTPRequestHandler):
    server_version = "ClawdmeterSessions/1"
    protocol_version = "HTTP/1.1"

    def _is_loopback(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        body = b""
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)
            if len(body) < MAX_BODY_BYTES:
                body += chunk  # oversize tails are drained but not kept
        return body

    def do_POST(self):
        if not self._is_loopback():
            self.send_error(403)
            return
        body = self._read_body()
        payload = None
        if body:
            try:
                payload = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                payload = None
        if payload is not None and self.server.table.handle_event(payload):
            self.server.publish()
        # 204 always: a read-only observer never blocks or approves anything.
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        # Loopback debugging aid: current wire payload.
        if not self._is_loopback():
            self.send_error(403)
            return
        body = self.server.table.project(self.server.budget).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # hooks arrive constantly; keep the journal quiet


def _sweeper(server, stop_event):
    while not stop_event.wait(SWEEP_INTERVAL_S):
        try:
            if server.table.sweep():
                server.publish()
        except Exception as exc:  # never let the sweeper die silently
            log(f"sweep error: {exc}")


# ---------------------------------------------------------------------------
# Hook installation (used by install.sh; also runnable by hand — SESSIONS.md)
# ---------------------------------------------------------------------------

def install_hooks(settings_path, url):
    """Idempotently merge the Clawdmeter HTTP hook block into a Claude Code
    settings.json. Existing hooks are preserved; ours is appended per event."""
    settings_path = os.path.expanduser(settings_path)
    try:
        with open(settings_path, encoding="utf-8") as fh:
            settings = json.load(fh)
    except FileNotFoundError:
        settings = {}
    except ValueError:
        print(f"error: {settings_path} is not valid JSON; refusing to modify it",
              file=sys.stderr)
        return 1
    if not isinstance(settings, dict):
        print(f"error: {settings_path} is not a JSON object; refusing to modify it",
              file=sys.stderr)
        return 1

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        print(f"error: 'hooks' in {settings_path} is not an object; refusing to modify it",
              file=sys.stderr)
        return 1

    changed = False
    for event in HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            continue
        present = any(
            isinstance(h, dict) and h.get("type") == "http" and h.get("url") == url
            for entry in entries if isinstance(entry, dict)
            for h in (entry.get("hooks") or [])
        )
        if not present:
            entries.append(
                {"hooks": [{"type": "http", "url": url, "async": True, "timeout": 5}]}
            )
            changed = True

    if not changed:
        print(f"Clawdmeter session hooks already present in {settings_path}")
        return 0

    if os.path.exists(settings_path):
        shutil.copy2(settings_path, settings_path + ".clawdmeter-backup")
    directory = os.path.dirname(settings_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = settings_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, settings_path)
    print(f"Installed Clawdmeter session hooks into {settings_path}")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Clawdmeter session-awareness sidecar (Claude Code hook listener)"
    )
    parser.add_argument("--port", type=int, default=None,
                        help="listen port (default: hook_port from the daemon config; "
                             "0 = OS-assigned)")
    parser.add_argument("--config", default=None,
                        help=f"daemon config file (default: {CONFIG_FILE})")
    parser.add_argument("--sessions-file", default=None,
                        help=f"handoff file (default: {DEFAULT_SESSIONS_FILE})")
    parser.add_argument("--budget", type=int, default=None,
                        help="payload byte budget (default: sessions_budget_bytes "
                             f"from config, else {DEFAULT_BUDGET_BYTES})")
    parser.add_argument("--install-hooks", nargs=2, metavar=("SETTINGS_JSON", "URL"),
                        help="merge the hook block into a Claude Code settings.json "
                             "and exit")
    args = parser.parse_args(argv)

    if args.install_hooks:
        return install_hooks(*args.install_hooks)

    enable_file_log()  # no-op off Windows; see enable_file_log()

    config_path = args.config or CONFIG_FILE

    port = args.port
    if port is None:
        raw = read_config_value("hook_port", config_path)
        if raw is None:
            log("hook_port is not set in the config — live session awareness is off. "
                f"Set it in {config_path} to enable. Exiting.")
            return 0
        try:
            port = int(raw)
        except ValueError:
            log(f"hook_port '{raw}' is not a number. Exiting.")
            return 1

    pinned_k = None
    raw = read_config_value("context_window_k", config_path)
    if raw:
        try:
            pinned_k = int(raw)
        except ValueError:
            log(f"ignoring non-numeric context_window_k '{raw}' (heuristic stays on)")

    budget = args.budget
    if budget is None:
        raw = read_config_value("sessions_budget_bytes", config_path)
        try:
            budget = int(raw) if raw else DEFAULT_BUDGET_BYTES
        except ValueError:
            budget = DEFAULT_BUDGET_BYTES

    sessions_file = args.sessions_file or DEFAULT_SESSIONS_FILE
    table = SessionTable(pinned_window_k=pinned_k, config_dirs=read_config_dirs(config_path))

    try:
        server = HookServer(("127.0.0.1", port), table, sessions_file, budget)
    except OSError as exc:
        log(f"cannot bind 127.0.0.1:{port}: {exc}")
        return 1

    bound_port = server.server_address[1]
    log(f"listening on http://127.0.0.1:{bound_port}/ "
        f"(budget {budget} bytes -> {sessions_file})")
    server.publish()  # start from a clean, current file (clears stale sessions)

    stop_event = threading.Event()
    sweeper = threading.Thread(target=_sweeper, args=(server, stop_event), daemon=True)
    sweeper.start()

    def _shutdown(signum, frame):
        log("shutting down")
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    # SIGTERM and SIGINT both exist on Windows and signal.signal() accepts both;
    # SIGBREAK is Windows-only (Ctrl-Break, and what a console close sends), so
    # it is picked up by name when present. Note that `taskkill /F` /
    # Stop-Process uses TerminateProcess, which runs no handler at all — safe
    # here, since all state is rebuilt from hooks and the roster on restart.
    # signal.signal() raises ValueError off the main thread (library use), where
    # the host owns shutdown.
    for _signame in ("SIGTERM", "SIGINT", "SIGBREAK"):
        _sig = getattr(signal, _signame, None)
        if _sig is None:
            continue
        try:
            signal.signal(_sig, _shutdown)
        except (ValueError, OSError):
            pass

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
