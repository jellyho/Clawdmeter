#!/usr/bin/env python3
"""Clawdmeter inbox watcher -- messages other Claude Code sessions send here.

Claude Code has first-party cross-session messaging: one session calls
`SendMessage` and names a peer from `ListAgents`. This module puts the
arriving message on the desk device.

    ~/.claude/projects/<munged-cwd>/<session-id>.jsonl   (grows on append)
                    | tail-read what is NEW since last pass
                    v
            clawdmeter_inbox.InboxWatcher
                    | (timestamp, from-name, body) -> wire row, state 11
                    v
            clawdmeter_fleet -> ~/.clawdmeter/sessions.json -> BLE -> panel

WHY WATCH TRANSCRIPTS AND NOT SOMETHING NICER
---------------------------------------------
A received message is appended to the RECEIVING session's transcript when
that session processes it, as a `user` record with `userType: "external"`
whose content begins "Another Claude session sent a message:" followed by a
`<cross-session-message from-name="..." ...>` block. Verified on this machine
against a real probe message, not inferred from docs.

That has one hard consequence worth stating: **a session only writes the
message when it is alive to process it.** There is no host-side spool to read.
Watching every open session's transcript is therefore the zero-cost approach
-- it adds no session and no model turns beyond what already happens. The
alternative (a dedicated always-on "inbox" session) would burn a model turn
per message on a device whose entire purpose is watching quota.

WHAT IS *NOT* WATCHED
---------------------
Only top-level session transcripts: `<root>/<project>/<id>.jsonl`, depth
exactly two, no recursion. Files under `<project>/<id>/subagents/**` and
`.../workflows/**` are excluded on purpose. They are in-process agents an
orchestrator spawned; traffic to them is a session talking to its own
children, not another human's session reaching this machine, and there are an
order of magnitude more of them (193 files here, ~30 of them real sessions).
Surfacing them would turn the panel into a log of a workflow's internals.

ASCII ONLY -- SEE fold_to_ascii()
---------------------------------
The panel's Styrene/Tiempos fonts cover U+0020..U+007E. Anything else renders
as blanks. This module transliterates rather than shipping mojibake; the
rules and their honesty caveats are documented on fold_to_ascii().

PRIVACY -- READ THIS
--------------------
Every other row this project sends the device carries names, states and
counts only; SESSIONS.md says so explicitly. This one carries **message
body text** over BLE to the device. That is the feature. It is a real change
of posture, so it is a separate switch (`inbox = off` in the daemon config)
and it is documented in FLEET.md rather than buried here.

Python 3 stdlib only.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata

# The sidecar owns the wire format, the eliding, the byte fitting and the
# state codes. Reuse them rather than growing a second, drifting copy.
try:
    from . import clawdmeter_sessions as cs
except ImportError:  # run as a script, not a package
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import clawdmeter_sessions as cs

# ---------------------------------------------------------------------------
# Wire constants
# ---------------------------------------------------------------------------

# Next free state code. firmware/src/data.h defines 0..10 (SESSION_ENDED = 10)
# and says the codes are APPEND-ONLY because they cross the BLE boundary, so
# this one goes on the end. The firmware phase must add the same number.
STATE_MESSAGE = 11

# The message text is a NEW TRAILING field. Indices 0..12 keep the exact
# meaning and the exact bytes they have today, so firmware that stops reading
# at index 12 sees a normal (if oddly-stated) row and ignores the tail.
MSG_FIELD_INDEX = 13

# Only message rows carry index 13. Session rows stay 13 fields long: a field
# nobody reads is pure byte-budget, and the budget is the scarce thing here.

# ---------------------------------------------------------------------------
# Tunables (all overridable from the daemon config file)
# ---------------------------------------------------------------------------

# A message older than this is history, not news. This is also the first-run
# baseline: on a cold start the watcher tail-reads each transcript and then
# drops everything outside the window, so it can never dump hours of backlog
# onto the panel. Two minutes is long enough that a watcher restart does not
# lose a message that landed seconds ago.
DEFAULT_FRESHNESS_S = 120

# How long a message stays on the panel. A message row costs roughly half the
# default 180-byte budget, so while it is up it evicts session cards -- the
# window has to be short or the sessions view is permanently hostage to one
# message. Three minutes covers a glance cycle on a desk device; a message you
# have not noticed in three minutes is one you will read on the computer.
DEFAULT_EXPIRE_S = 180

# Hard cap on concurrent message rows. One message is the normal case; two is
# a burst worth showing. Beyond that the panel stops being glanceable and the
# byte budget stops being able to hold a session card at all.
DEFAULT_MAX_ROWS = 2

# Message text length, in characters, after folding and eliding. 40 keeps a
# message row near 75 bytes; MSG_TEXT_MIN is the floor the adaptive cap
# (text_max_for_budget) will not go below, because below it the text stops
# being a message and becomes a shrug.
MSG_TEXT_MAX = 40
MSG_TEXT_MIN = 16

# The sender goes in the label field, and the label field is a 32-char buffer
# on the device (SESSION_LABEL_MAX in firmware/src/data.h, which main.cpp
# snprintf()s into). Cap here rather than letting the firmware cut it: the
# host is the side that knows a name is being shortened and can middle-elide
# so the tail that tells two machines apart survives.
LABEL_MAX = 32

# Two message rows cost ~146 bytes whatever the text cap, so below this budget
# a pair of them evicts EVERY session card (measured against cs.fit_payload
# with five remote sessions: at 180 bytes two messages leave 0 cards, one
# message leaves 2). Under it, only the newest message is shown -- an older
# one has already had its own card and its own notification, and a Sessions
# tab with no sessions on it is a worse answer than a message you already saw.
MULTI_ROW_MIN_BUDGET = 200

# Shown when folding leaves nothing legible at all (a body that is pure emoji,
# or pure CJK with transliteration off). Honest: says a message arrived and
# that the panel cannot show it.
UNREADABLE_TEXT = "[non-ASCII msg]"

# On first sight of a transcript, read at most this much of its tail. Bounds
# the cold-start cost: transcripts reach many MB, and the freshness window
# will throw away almost all of it anyway.
DISCOVERY_TAIL_BYTES = 256 * 1024

# Per-pass read cap, so a transcript that grew enormously between passes
# cannot stall the poll loop.
MAX_READ_BYTES = 4 * 1024 * 1024

# ---------------------------------------------------------------------------
# Recognising a cross-session message
# ---------------------------------------------------------------------------

# Cheap bytes prefilter: 99.99% of transcript lines never contain this, and
# skipping json.loads on them is what makes watching every transcript free.
_SCAN_TEXT = "cross-session-message"

# Parse defensively. Neither the preamble sentence nor the attribute order is
# a contract -- both are prose emitted by a Claude Code version that can
# change under us. The structured tag is the primary signal; the preamble is
# a fallback for direct callers of parse_cross_session().
#
# Scope, honestly: the watcher's own prefilter (_SCAN_TEXT below) requires the
# TAG, so a line carrying only the preamble never reaches parse_cross_session
# from poll(). That is deliberate, not an oversight -- the preamble is one
# English sentence that appears verbatim in this repo's own docs and in the
# transcript of any session that discusses the feature, so accepting it as the
# sole signal would put documentation prose on the panel. If a future Claude
# Code really does drop the tag, the fix is to widen the prefilter to a marker
# that version actually emits, and to accept the false positives knowingly.
_OPEN_RE = re.compile(r"<cross-session-message\b([^>]*)>")
_ATTR_RE = re.compile(r'([A-Za-z][A-Za-z0-9_-]*)\s*=\s*"([^"]*)"')
_CLOSE_TAG = "</cross-session-message>"
_PREAMBLE_RE = re.compile(r"Another Claude session sent a message:?\s*", re.I)

# Boilerplate Claude Code appends AFTER the close tag ("This came from another
# Claude session -- not typed by your user..."). Taking the body strictly
# between the tags already excludes it; this only matters when the close tag
# is missing.
_TRAILER_RE = re.compile(r"\n\s*This came from another Claude session\b.*\Z", re.S)

DEFAULT_SENDER = "peer"


def log(msg):
    print(f"[inbox] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# ASCII folding -- the panel's fonts are 32..126 and nothing else
# ---------------------------------------------------------------------------

# Punctuation that has an obvious ASCII equivalent. NFKD does not fold these
# (an em dash is not a decomposable hyphen), so they need naming.
_PUNCT = {
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "―": "-", "−": "-", "•": "-", "·": "-",
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "′": "'", "‹": "'", "›": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "«": '"', "»": '"', "″": '"',
    "\u2026": "...", "\u00a0": " ", "\u200b": "", "\ufeff": "",
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


_DROP = "\x00"  # internal placeholder for one untranslatable character


def fold_to_ascii(text, translit=True):
    """Make `text` renderable in the panel's 32..126 font, honestly.

    Four passes, most faithful first:

    1. ASCII passes through untouched.
    2. Named punctuation maps to its ASCII twin (em dash -> "-", curly quotes
       -> straight, U+2026 -> "...").
    3. Hangul is transliterated (see romanize_hangul) when `translit`.
    4. Anything else is NFKD-normalised and stripped of combining marks, which
       is a genuine transliteration for accented Latin ("café" -> "cafe")
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


def to_panel_text(body, translit=True):
    """A message body -> one folded, single-line string a card can hold."""
    body = body or ""
    folded = fold_to_ascii(body, translit)
    single = re.sub(r"\s+", " ", folded).strip()
    if not re.search(r"[A-Za-z0-9]", single):
        # Nothing readable left. Say so -- but only if the folding is what ate
        # it: a body that was always just ":)" is not unreadable, it is short.
        if any(not (" " <= c <= "~") and not c.isspace() for c in body):
            return UNREADABLE_TEXT
    return single


def elide_message(text, max_chars):
    """Head-elide. Unlike cs.elide_label (which middle-elides to keep a name's
    trailing discriminator), a message's information is front-loaded: the
    first words are the ones worth the pixels."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= len(cs.ELLIPSIS):
        return text[:max_chars]
    return text[:max_chars - len(cs.ELLIPSIS)].rstrip() + cs.ELLIPSIS


def panel_sender(sender, translit=True):
    """A sender name the panel can actually render.

    The body is folded and the label was not, which made the documented
    fallback ("even an unreadable body still tells you who it came from") a
    lie for a peer whose machine name is Korean: the body romanised and the
    sender came out as tofu. Same fold, then a middle-elide to the device's
    label buffer -- a name's tail is its discriminator, unlike a message's.

    A name that folds to nothing legible becomes DEFAULT_SENDER: "peer" says
    less than the real name but it is a name, where "[non-ASCII msg]" in the
    sender slot would just be noise.
    """
    folded = to_panel_text(sender or "", translit)
    if not folded or folded == UNREADABLE_TEXT:
        return DEFAULT_SENDER
    return cs.elide_label(folded, LABEL_MAX)


def max_rows_for_budget(budget, configured=DEFAULT_MAX_ROWS):
    """`configured`, or 1 when the budget cannot hold two rows and a session.

    See MULTI_ROW_MIN_BUDGET. An explicitly raised inbox_max_rows is honoured
    once the budget is big enough to pay for it; it is never raised here.
    """
    try:
        budget = int(budget)
    except (TypeError, ValueError):
        return configured
    return configured if budget >= MULTI_ROW_MIN_BUDGET else min(configured, 1)


def text_max_for_budget(budget):
    """Message length that leaves room for a session card beside it.

    Scales with the payload budget instead of hard-coding 40, so raising
    sessions_budget_bytes (the firmware buffer is 1 KB and it asks for a
    517-byte MTU) buys longer messages up to MSG_TEXT_MAX, and shrinking it
    shortens them rather than blanking the panel.
    """
    try:
        budget = int(budget)
    except (TypeError, ValueError):
        return MSG_TEXT_MAX
    return max(MSG_TEXT_MIN, min(MSG_TEXT_MAX, budget // 4))


# ---------------------------------------------------------------------------
# Parsing one transcript record
# ---------------------------------------------------------------------------

def content_text(content):
    """The human-readable text of a record's `message.content`.

    Verified shape on this machine is a bare string. Other Claude Code
    versions use the block-list form, so both are handled -- and ONLY `text`
    blocks are joined. That exclusion is load-bearing: a `tool_result` block
    echoing a transcript grep would otherwise look exactly like a received
    message and put the observer's own debugging on the panel.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        txt = content.get("text")
        return txt if (content.get("type") == "text" and isinstance(txt, str)) else ""
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                txt = block.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
        return "\n".join(parts)
    return ""


def parse_cross_session(text):
    """(sender, body) from a message text, or None if this is not one."""
    if not text:
        return None
    m = _OPEN_RE.search(text)
    if m:
        attrs = dict(_ATTR_RE.findall(m.group(1)))
        sender = (attrs.get("from-name") or attrs.get("from_name")
                  or attrs.get("from") or "").strip()
        rest = text[m.end():]
        close = rest.find(_CLOSE_TAG)
        body = rest[:close] if close >= 0 else _TRAILER_RE.sub("", rest)
    elif _PREAMBLE_RE.search(text):
        # Tag gone (a future version reworded it). Still worth showing.
        sender = ""
        body = _TRAILER_RE.sub("", _PREAMBLE_RE.split(text, 1)[-1])
    else:
        return None
    sender = sender or DEFAULT_SENDER
    return sender, body.strip()


def _epoch(value):
    """ISO-8601 (or numeric) timestamp -> epoch seconds; 0 when unreadable."""
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if value > 1e11 else float(value)
    if isinstance(value, str) and value:
        try:
            from datetime import datetime
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


class Message(object):
    """One received cross-session message."""

    __slots__ = ("mid", "ts", "sender", "body", "source")

    def __init__(self, mid, ts, sender, body, source=""):
        self.mid = mid
        self.ts = ts
        self.sender = sender
        self.body = body
        self.source = source

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<Message {self.mid} from={self.sender!r} ts={self.ts:.0f}>"


def message_from_record(rec, source=""):
    """A transcript record -> Message, or None.

    The record filter is deliberately narrow, because the transcript of a
    session that *investigates* cross-session messaging is full of text that
    mentions them:

      * `type` must be "user"           -- an assistant turn quoting the tag is not mail;
      * `isSidechain` must be falsy     -- sidechain traffic is a subagent's, not yours;
      * content must be text            -- see content_text() on tool_result blocks.

    `userType == "external"` is what the verified record carries, but it is
    NOT required: it is undocumented and the tag is the stronger signal.
    """
    if not isinstance(rec, dict):
        return None
    if rec.get("type") != "user" or rec.get("isSidechain"):
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    parsed = parse_cross_session(content_text(msg.get("content")))
    if parsed is None:
        return None
    sender, body = parsed
    ts = _epoch(rec.get("timestamp"))
    key = "|".join((str(rec.get("sessionId") or source), str(rec.get("timestamp") or ""),
                    sender, body[:160]))
    mid = hashlib.md5(key.encode("utf-8")).hexdigest()[:8]
    return Message(mid, ts, sender, body, source)


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------

def default_roots():
    """`<config-dir>/projects` for every configured Claude config dir."""
    return [os.path.join(d, "projects") for d in cs.read_config_dirs()]


class InboxWatcher(object):
    """Incremental tail-reader over every top-level session transcript.

    Holds one offset per file and reads only the bytes appended since the last
    pass -- these files reach megabytes and re-reading them on a 2 s tick is
    not acceptable. Rotation, truncation and half-written lines are all normal
    states here, not errors: Claude Code is appending to these files while we
    read them.

    `max_rows` here is taken as given. The BUDGET-aware cap lives in
    watcher_from_config() (see max_rows_for_budget): two message rows at the
    default 180-byte budget leave room for no session card at all, so build
    the watcher through that helper rather than constructing one directly with
    a configured row count.
    """

    def __init__(self, roots=None, freshness_s=DEFAULT_FRESHNESS_S,
                 expire_s=DEFAULT_EXPIRE_S, max_rows=DEFAULT_MAX_ROWS,
                 text_max=MSG_TEXT_MAX, translit=True, now_fn=time.time):
        self.roots = list(roots) if roots is not None else default_roots()
        self.freshness_s = freshness_s
        self.expire_s = expire_s
        self.max_rows = max_rows
        self.text_max = text_max
        self.translit = translit
        self._now = now_fn
        self._files = {}       # path -> [offset, inode, cold]
        self._messages = []    # live, newest last
        self._seen = {}        # mid -> ts, for de-duplication across re-reads

    # -- discovery ---------------------------------------------------------

    def transcripts(self):
        """Top-level session transcripts only: <root>/<project>/<id>.jsonl.

        No recursion, which is exactly how `subagents/` and `workflows/` are
        excluded (see the module docstring). A project directory that appears
        between passes is picked up here for free -- the scan is by listing,
        not from a cached set.
        """
        found = []
        for root in self.roots:
            try:
                projects = os.scandir(root)
            except OSError:
                continue          # no such config dir: normal, not an error
            with projects:
                for project in projects:
                    if not project.is_dir():
                        continue
                    try:
                        entries = os.scandir(project.path)
                    except OSError:
                        continue
                    with entries:
                        for entry in entries:
                            if entry.name.endswith(".jsonl") and entry.is_file():
                                found.append(entry.path)
        return found

    # -- incremental reading ----------------------------------------------

    def _baseline_offset(self, path, size):
        """Where to start on a file we have never seen.

        The start of the first COMPLETE line within DISCOVERY_TAIL_BYTES of
        the end -- seeking to a raw byte offset lands mid-line and the first
        record read would be a fragment. Bounds the cold-start cost on
        multi-MB transcripts; the freshness window then discards nearly all
        of what this does read.
        """
        start = max(0, size - DISCOVERY_TAIL_BYTES)
        if start == 0:
            return 0
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                fh.readline()          # discard the partial line
                return fh.tell()
        except OSError:
            return size                # unreadable: baseline at EOF, lose nothing

    def _read_new(self, path):
        """Complete lines appended since the last pass, as str.

        Never raises: a transcript that vanishes, locks, or turns unreadable
        mid-pass yields nothing this time and is retried on the next tick --
        Claude Code is appending to these files while we read them, so that is
        a normal state, not an error.
        """
        try:
            st = os.stat(path)
        except OSError:
            return []
        ino = getattr(st, "st_ino", 0)
        state = self._files.get(path)

        if state is None:
            # COLD: the bytes this pass reads are a tail of history we have
            # never seen, not something that just arrived. poll() needs to
            # know, because a record whose timestamp it cannot read must not
            # be dated "now" on a pass like this one.
            state = [self._baseline_offset(path, st.st_size), ino, True]
            self._files[path] = state
        elif st.st_size < state[0] or (ino and state[1] and ino != state[1]):
            # Truncated in place, or replaced by a different file under the
            # same name. Either way the offset is meaningless -> start over,
            # and the mid dedup keeps an already-shown message from repeating.
            # Re-reading a whole file is reading history again: cold as well.
            state[0] = 0
            state[1] = ino
            state[2] = True
        if st.st_size <= state[0]:
            return []                  # nothing new: no open() at all

        try:
            with open(path, "rb") as fh:
                fh.seek(state[0])
                data = fh.read(MAX_READ_BYTES)
        except OSError:
            return []

        end = data.rfind(b"\n")
        if end < 0:
            if len(data) >= MAX_READ_BYTES:
                # Not a half-written line: a COMPLETE line longer than the
                # per-pass cap (transcripts do carry these -- a huge tool
                # result, an embedded image). Consuming nothing here would
                # re-read the same 4 MB every tick forever and never yield
                # another message from this transcript. Skip what we read;
                # the next pass resumes mid-line and json.loads rejects the
                # fragment, which is the right outcome for a line we could
                # not have shown anyway.
                state[0] += len(data)
                state[1] = ino
                return []
            # Only a half-written line so far. Consume nothing, so it is
            # re-read whole once the writer finishes it.
            return []
        state[0] += end + 1
        state[1] = ino
        return data[:end].decode("utf-8", "replace").splitlines()

    # -- the pass ----------------------------------------------------------

    def poll(self):
        """One scan. Returns the live message list, newest last."""
        now = self._now()
        current = self.transcripts()
        # Forget files that are gone, so a long-running daemon's offset table
        # does not grow for the life of the process.
        self._files = {p: v for p, v in self._files.items() if p in set(current)}
        for path in current:
            lines = self._read_new(path)
            # Was this pass a first sight (or a re-read after rotation)? The
            # flag is consumed here, not inside _read_new, so a pass that
            # yielded no lines still clears it.
            st = self._files.get(path)
            cold = bool(st and st[2])
            if st:
                st[2] = False
            for line in lines:
                if _SCAN_TEXT not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue      # half-written or corrupt line: skip it
                msg = message_from_record(rec, path)
                if msg is None:
                    continue
                if not msg.ts:
                    if cold:
                        # History with an unreadable date, read out of the
                        # 256 KB discovery tail. Dating it "now" would put
                        # last week's private message text on the panel the
                        # moment the poller starts -- and the timestamp
                        # FORMAT is exactly the kind of prose this module
                        # refuses to treat as a contract everywhere else.
                        # Unknown age on a cold pass means "not news".
                        continue
                    # Appended since the last pass, so "arrived now" is true.
                    # Leaving it at 0 would make it immortal: the freshness
                    # test and the expiry test both compare against it.
                    msg.ts = now
                # The freshness window IS the cold-start baseline: on the
                # first pass the tail-read sees old messages and they die
                # right here, so the panel never shows backlog.
                if now - msg.ts > self.freshness_s:
                    continue
                if msg.mid in self._seen:
                    continue      # re-read after a rotation, or a duplicate
                self._seen[msg.mid] = now
                self._messages.append(msg)
        self._expire(now)
        return list(self._messages)

    def _expire(self, now):
        live = [m for m in self._messages if now - (m.ts or now) <= self.expire_s]
        # Sorted by arrival, not by the order files happened to be scanned in:
        # two transcripts read in one pass can yield messages out of order, and
        # both the "newest first" row order and the max_rows cut depend on it.
        live.sort(key=lambda m: m.ts)
        self._messages = live[-self.max_rows:] if len(live) > self.max_rows else live
        horizon = self.expire_s + self.freshness_s + 60
        self._seen = {k: v for k, v in self._seen.items() if now - v <= horizon}

    # -- wire rows ---------------------------------------------------------

    def rows(self, now=None, text_max=None):
        """Live messages as wire rows, newest first.

        The per-message length shrinks when several are live at once. Without
        that, two full-length message rows overflow the default 180-byte
        budget and cs.fit_payload() drops the older one from the tail --
        a message would vanish silently rather than arrive shortened.
        """
        now = self._now() if now is None else now
        cap = self.text_max if text_max is None else text_max
        live = list(reversed(self._messages))
        if len(live) > 1:
            cap = max(MSG_TEXT_MIN, cap // len(live))
        return [message_row(m, now, cap, self.translit) for m in live]


def message_row(msg, now, text_max=MSG_TEXT_MAX, translit=True):
    """One Message -> the positional wire row.

    Indices 0..12 carry exactly what they always have; index 13 is new.
    Unknown fields go out as the documented "unknown" values so the device
    hides those elements instead of drawing a confident zero.
    """
    elapsed = int(max(0.0, now - msg.ts)) if msg.ts else 0
    text = elide_message(to_panel_text(msg.body, translit), text_max)
    return [
        msg.mid[:2],                     # 0  sid: stable, keys the card
        panel_sender(msg.sender, translit),  # 1  label: who sent it, in the
                                         #    panel's font (see panel_sender)
        STATE_MESSAGE,                   # 2  state
        -1,                              # 3  ctx: not applicable
        elapsed,                         # 4  age of the message
        0,                               # 5  model: unknown
        0,                               # 6  tool: none
        0, 0, 0, 0,                      # 7-10 ntools/nagents/tdone/ttotal
        -1,                              # 11 tok: not applicable
        cs.REMOTE_UNKNOWN,               # 12 remote: meaningless for a message
        text,                            # 13 NEW: the message itself
    ]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _config_int(key, default, config_path=None):
    raw = cs.read_config_value(key, config_path)
    if raw is None:
        return default
    try:
        val = int(str(raw).strip())
    except ValueError:
        return default
    return val if val > 0 else default


def _config_flag(key, default, config_path=None):
    raw = cs.read_config_value(key, config_path)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "on", "true", "yes")


def enabled(config_path=None):
    """`inbox = off` turns it off; on by default wherever the fleet runs.

    Defaulting to on is a judgement call: the fleet poller it rides on is
    itself an explicit opt-in (`fleet = on`), so nothing here reaches a
    machine whose owner has not already asked for remote session data. The
    privacy consequence -- message TEXT crosses the BLE link, which no other
    row does -- is documented in FLEET.md, and this switch is how you decline.
    """
    return _config_flag("inbox", True, config_path)


def watcher_from_config(budget=cs.DEFAULT_BUDGET_BYTES, config_path=None, roots=None):
    wanted = _config_int("inbox_max_rows", DEFAULT_MAX_ROWS, config_path)
    rows = max_rows_for_budget(budget, wanted)
    if rows < wanted:
        # Say it out loud rather than quietly showing fewer: this is the one
        # place the byte budget changes what the owner asked for.
        log(f"inbox_max_rows {wanted} -> {rows}: at a {budget}-byte budget two "
            f"message rows leave no room for a session card "
            f"(raise sessions_budget_bytes past {MULTI_ROW_MIN_BUDGET})")
    return InboxWatcher(
        roots=roots,
        freshness_s=_config_int("inbox_freshness_s", DEFAULT_FRESHNESS_S, config_path),
        expire_s=_config_int("inbox_expire_s", DEFAULT_EXPIRE_S, config_path),
        max_rows=rows,
        text_max=text_max_for_budget(budget),
        translit=_config_flag("inbox_translit", True, config_path),
    )


# ---------------------------------------------------------------------------
# CLI -- read-only inspection, never writes the handoff file
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true",
                        help="scan once, print what was found, exit")
    parser.add_argument("--watch", action="store_true",
                        help="scan every --tick seconds and print new messages")
    parser.add_argument("--tick", type=float, default=2.0)
    parser.add_argument("--freshness", type=int, default=None,
                        help=f"seconds of history to accept (default {DEFAULT_FRESHNESS_S})")
    parser.add_argument("--expire", type=int, default=None,
                        help=f"seconds a message stays live (default {DEFAULT_EXPIRE_S})")
    parser.add_argument("--budget", type=int, default=cs.DEFAULT_BUDGET_BYTES)
    parser.add_argument("--no-translit", action="store_true",
                        help="drop non-ASCII instead of transliterating it")
    parser.add_argument("--root", action="append", default=None,
                        help="projects dir to watch (repeatable; default from config)")
    args = parser.parse_args(argv)

    watcher = InboxWatcher(
        roots=args.root,
        freshness_s=args.freshness if args.freshness is not None else DEFAULT_FRESHNESS_S,
        expire_s=args.expire if args.expire is not None else DEFAULT_EXPIRE_S,
        max_rows=max_rows_for_budget(args.budget, DEFAULT_MAX_ROWS),
        text_max=text_max_for_budget(args.budget),
        translit=not args.no_translit,
    )

    def report():
        msgs = watcher.poll()
        for m in msgs:
            log(f"{time.strftime('%H:%M:%S', time.localtime(m.ts))} "
                f"<{m.sender}> {to_panel_text(m.body, watcher.translit)[:120]}")
        rows = watcher.rows()
        print(cs.encode_payload(rows))
        return rows

    if args.watch:
        log(f"watching {len(watcher.transcripts())} transcripts every {args.tick}s")
        while True:
            report()
            time.sleep(args.tick)
    report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
