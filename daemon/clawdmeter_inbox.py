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
A received message is appended to the RECEIVING session's transcript TWICE,
and both records are read here. Verified on this machine against a real probe
message, not inferred from docs:

  * **on arrival**, the moment it lands in the session's input queue, as a
    `queue-operation` / `enqueue` record whose top-level `content` string
    starts with the `<cross-session-message from-name="..." ...>` tag; and
  * **207 ms later**, once the session actually processed it, as a `user`
    record with `userType: "external"` whose content begins "Another Claude
    session sent a message:" followed by the same tag.

Reading the ARRIVAL record is what makes the panel independent of the
receiving session's state: a message shows up while that session is busy,
blocked, or parked at a prompt with an undrained queue -- and ~200 ms sooner
even when it is not. The processed record is read as well rather than
instead, because it is the shape verified to carry the preamble framing and
a Claude Code version that emits only one of the two must not go silent.
Two records for one message means DEDUP is mandatory: see message_id().

Watching every open session's transcript is the zero-cost approach -- it
adds no session and no model turns beyond what already happens. There is
still no host-side spool to read, and a dedicated always-on "inbox" session
would burn a model turn per message on a device whose entire purpose is
watching quota. (With the arrival record read, such a session would no
longer NEED to take a turn for its mail to reach the panel -- it could just
sit there and be a mail drop.)

WHAT IS *NOT* WATCHED
---------------------
Only top-level session transcripts: `<root>/<project>/<id>.jsonl`, depth
exactly two, no recursion. Files under `<project>/<id>/subagents/**` and
`.../workflows/**` are excluded on purpose. They are in-process agents an
orchestrator spawned; traffic to them is a session talking to its own
children, not another human's session reaching this machine, and there are an
order of magnitude more of them (193 files here, ~30 of them real sessions).
Surfacing them would turn the panel into a log of a workflow's internals.

WHAT THE PANEL CAN DRAW -- SEE cs.fold_to_ascii()
-------------------------------------------------
The brand fonts (Styrene, Tiempos) cover U+0020..U+007E and nothing else, so
the host folds everything else away rather than shipping mojibake. ONE field
is wider than that: the message BODY renders in a font that carries a Hangul
fallback (font_nanum_kr_28, the 2,350 KS X 1001 syllables), so Korean bodies
go out as real Hangul instead of `annyeonghaseyo`. The SENDER does not -- its
font has no fallback -- so panel_sender() keeps romanising. Both halves of
that asymmetry are load-bearing; see panel_sender() and docs/fonts.md.

`inbox_hangul = off` puts the body back to romanised, which is what firmware
older than the Korean font needs (it would draw Hangul as empty boxes).

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

# ---- Report states (see REPORT.md) ----------------------------------------
# A REPORT is a cross-session message whose body opens with the contract line
# in REPORT.md. It is not a new ROW KIND -- it is a message-shaped card whose
# STATE means something, which is the whole trick: the firmware already
# colours, sorts and auto-jumps on the state field, and filing every reply
# under STATE_MESSAGE threw all of that away.
#
# Four states, because three would hide the distinction the next feature needs.
# A later button will send "go ahead" back to an agent, and that only works on
# one of them:
#
#   WORKING    busy, needs nothing              -> nothing to send
#   NEEDS-YOU  stopped, waiting for direction    -> THE valid target: a message
#                                                   lands in its input queue
#                                                   and it carries on
#   BLOCKED    stopped at a permission prompt    -> a message CANNOT unblock it;
#                                                   it queues behind the dialog
#                                                   until a human clicks
#   DONE       finished, nothing running         -> nothing to continue
#
# WORKING/NEEDS-YOU/BLOCKED/DONE are what an agent can say about itself. MORE
# is host-minted: the overflow marker that makes a dropped report visible
# rather than silent (see overflow_row).
STATE_REPORT_WORKING   = 12
STATE_REPORT_NEEDS_YOU = 13
STATE_REPORT_BLOCKED   = 14
STATE_REPORT_DONE      = 15
STATE_REPORT_MORE      = 16

REPORT_STATES = (STATE_REPORT_WORKING, STATE_REPORT_NEEDS_YOU,
                 STATE_REPORT_BLOCKED, STATE_REPORT_DONE)
# Only these two mean "a human is needed": they are the firmware's waiting
# bucket (accent + pulse + sorts to the top + trips the auto-jump).
REPORT_WAITING_STATES = (STATE_REPORT_NEEDS_YOU, STATE_REPORT_BLOCKED)

# The message text is a NEW TRAILING field. Indices 0..12 keep the exact
# meaning and the exact bytes they have today, so firmware that stops reading
# at index 12 sees a normal (if oddly-stated) row and ignores the tail.
MSG_FIELD_INDEX = 13

# Only message rows carry index 13. Session rows stay 13 fields long: a field
# nobody reads is pure byte-budget, and the budget is the scarce thing here.
# Report rows carry it too -- the summary rides in the same field the message
# body does, because it renders in the same place on the same card anatomy.

# The device parses at most this many rows (SESSION_MAX_ROWS in
# firmware/src/data.h) and drops the rest without telling anyone. Ten agents
# can be reachable at once, so for reports that cap is not theoretical: the
# host has to do the dropping itself, in a defensible order, and say so.
DEVICE_MAX_ROWS = 6

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

# Message text length in BYTES after folding and eliding — not characters,
# which is the distinction the Hangul support turns on: ASCII spends one byte
# per character and Korean spends three, so a character cap silently means
# three different things.
#
# 96 is the panel's two-line ceiling, measured rather than guessed: a list
# card gives the body two 422 px lines and Hangul advances 26.31 px at 28 px,
# so two full lines are 32 syllables = 96 B. It is a ceiling, not the usual
# answer — text_max_for_budget() below almost always binds first (45 B at the
# 180-byte default budget), so this only becomes reachable once
# `sessions_budget_bytes` is raised. MSG_TEXT_MIN is the floor that cap will
# not go below, because under it the text stops being a message and becomes a
# shrug.
MSG_TEXT_MAX = 96
MSG_TEXT_MIN = 16

# The sender goes in the label field, and the label field is a 32-char buffer
# on the device (SESSION_LABEL_MAX in firmware/src/data.h, which main.cpp
# snprintf()s into). Cap here rather than letting the firmware cut it: the
# host is the side that knows a name is being shortened and can middle-elide
# so the tail that tells two machines apart survives.
LABEL_MAX = 32

# Report summary length, in BYTES after folding and eliding. A report is a
# one-liner by construction (the dispatcher asks for <= 40 characters), so it
# gets a tighter ceiling than a message body: the scarce thing in a report
# round is ROWS, and every byte a summary spends is a byte the next agent's
# card cannot have.
#
# The floor is 24 B -- eight Hangul syllables, or "waiting for go-ahead" with
# room to spare. Under that a summary stops saying anything and the round
# would be better spent on fewer, readable cards.
REPORT_TEXT_MAX = 64
REPORT_TEXT_MIN = 24

# How long a report stays on the panel. Longer than a message's 180 s because
# a report is a SNAPSHOT the owner asked for, not something that arrived
# unbidden -- but bounded, because the host cannot see the agent change its
# mind: the card goes away by expiry, never by resolution. The age on the card
# is what keeps it honest in the meantime.
DEFAULT_REPORT_EXPIRE_S = 300

# Two message rows cost ~146 bytes whatever the text cap, so below this budget
# a pair of them evicts EVERY session card (measured against cs.fit_payload
# with five remote sessions: at 180 bytes two messages leave 0 cards, one
# message leaves 2). Under it, only the newest message is shown -- an older
# one has already had its own card and its own notification, and a Sessions
# tab with no sessions on it is a worse answer than a message you already saw.
MULTI_ROW_MIN_BUDGET = 200

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

# The receiving transcript records ONE arriving message TWICE, and the first
# of the two is the one worth reading. Verified on this machine (grep
# PROBE-MARKER-7f3a2b in the probe transcript), 207 ms apart:
#
#   1. ON ARRIVAL, the moment it lands in the session's input queue:
#      {"type":"queue-operation","operation":"enqueue","sessionId":...,
#       "timestamp":...,"content":"<cross-session-message from=... >\n...body"}
#      Note the shape: `content` is a plain string at the TOP level, and the
#      tag sits at its head with no "Another Claude session sent a message:"
#      preamble.
#
#   2. ONCE THE SESSION PROCESSES IT, as the `user` / `userType: "external"`
#      record with the preamble and the body nested under `message.content`.
#
# Both are parsed. Reading (1) is what lets a message reach the panel while
# the receiving session is busy, blocked, or parked at a prompt with an
# undrained queue -- and (2) is the shape verified to carry the preamble
# framing, so it stays as the belt to (1)'s braces. Dedup across the pair is
# message_id(); see it for the identity and its one honest cost.
#
# `queue-operation` is NOT a message-only record type: the user's own typed
# prompts are enqueued through it too. What separates mail from a prompt is
# the same thing that separates it in a `user` record -- the tag -- so the
# false-positive surface is the one this module already accepted (a person
# who PASTES an envelope gets it back on their panel), not a new one.
_ARRIVAL_TYPE = "queue-operation"
_ARRIVAL_OP = "enqueue"

# Ceiling on remembered message ids, over and above the time-based horizon in
# _expire(). The horizon alone is not enough now that the two records can be
# far apart: a session parked at a prompt drains its queue whenever its human
# gets back, so the gap between (1) and (2) is bounded by nothing. Forgetting
# the id in between would put the message on the panel a SECOND time at drain.
# 512 ids is 49 KB (measured) and covers any plausible backlog.
SEEN_KEEP_MIN = 512

DEFAULT_SENDER = "peer"


def log(msg):
    print(f"[inbox] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Folding -- the panel's fonts are the constraint
# ---------------------------------------------------------------------------
# The fold itself now lives in clawdmeter_sessions, next to elide_label and
# fit_payload, because EVERY label on the wire needs it and not just this
# module's sender field (a Korean session label was arriving as empty boxes).
# Re-exported here so the names this module documents and its tests exercise
# keep working, and so the message-body policy below reads in one place.
fold_to_ascii = cs.fold_to_ascii
to_panel_text = cs.to_panel_text
romanize_hangul = cs.romanize_hangul
panel_label = cs.panel_label
KSX1001_HANGUL = cs.KSX1001_HANGUL

# Shown when folding leaves nothing legible at all (a body that is pure emoji,
# or pure CJK with transliteration off). Honest: says a message arrived and
# that the panel cannot show it.
UNREADABLE_TEXT = cs.UNREADABLE_TEXT


def _cut_bytes(text, nbytes):
    """`text` truncated to at most `nbytes` UTF-8 bytes, never mid-character.

    `errors="ignore"` is doing the work: a cut that lands inside a multi-byte
    sequence leaves an undecodable tail, and dropping it is exactly the
    character-boundary rule. Everything downstream (the wire, the firmware's
    48->104 byte buffer, LVGL's decoder) counts bytes, so this is the honest
    unit even though `len()` is not."""
    if nbytes <= 0:
        return ""
    return text.encode("utf-8")[:nbytes].decode("utf-8", "ignore")


def elide_message(text, max_bytes):
    """Head-elide to a BYTE budget. Unlike cs.elide_label (which middle-elides
    to keep a name's trailing discriminator), a message's information is
    front-loaded: the first words are the ones worth the pixels.

    Bytes, not characters, since the body stopped being ASCII: one Hangul
    syllable is three bytes, so a 40-CHARACTER cap is 120 bytes on the wire
    and overruns both the payload budget and the device's buffer. The
    ellipsis is paid for out of the same budget."""
    if max_bytes <= 0:
        return ""
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    ell = len(cs.ELLIPSIS)          # ASCII "...", so bytes == characters
    if max_bytes <= ell:
        return _cut_bytes(text, max_bytes)
    return _cut_bytes(text, max_bytes - ell).rstrip() + cs.ELLIPSIS


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

    NOTE the missing `keep_hangul`: this one always romanises, deliberately.
    The sender renders in font_styrene_20/24 and the session card's name in
    font_styrene_28/48, and none of those has the Hangul fallback the message
    BODY got -- only MSG_BODY_FONT does. A Korean sender passed through would
    be a row of empty boxes above a perfectly rendered Korean body. Fixing
    that properly means Hangul faces at 20/24/48 px, ~262 KB of extra flash
    (measured, docs/fonts.md), which is not worth it for a machine name.
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
    """Message length, in BYTES, that leaves room for a session card beside it.

    Scales with the payload budget instead of hard-coding a length, so raising
    sessions_budget_bytes (the firmware buffer is 1 KB and it asks for a
    517-byte MTU) buys longer messages up to MSG_TEXT_MAX, and shrinking it
    shortens them rather than blanking the panel.

    A quarter of the budget is the share that leaves room for session cards;
    it used to be a quarter of the budget in CHARACTERS, which for Korean was
    three times too generous — one message then ate the whole payload and
    fit_payload() dropped every session row to pay for it. Practical
    consequence for Korean: 45 B (15 syllables) at the 180-byte default, 65 B
    (21 syllables) at the 260 that daemon/SESSIONS.md recommends once the link
    has a real MTU. The default stays 180 because it is sized for the 185-byte
    minimum an unlucky host stack may hand you, not because Korean fits in it.
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


# ---------------------------------------------------------------------------
# The REPORT contract -- daemon/REPORT.md is the normative copy
# ---------------------------------------------------------------------------
# One line, and the agent's whole reply:
#
#     CLAWDMETER-REPORT/1 NEEDS-YOU: rebase done, force-push?
#
# STRICT ABOUT THE MATCH, FORGIVING ABOUT WHITESPACE, and one rule carries
# most of the strictness: the marker must open the body's FIRST NON-EMPTY
# LINE. That is not fussiness, it is the only thing standing between this
# parser and its own request text -- the dispatcher's message tells the agent
# what to emit, so it necessarily CONTAINS the marker, and it lands in the
# receiving transcript exactly like any other cross-session message. Accepting
# the marker anywhere would make every dispatch draw a bogus report card on
# the panel. REPORT.md's request text therefore keeps the template indented
# inside prose, never at the head of the body.
#
# What IS forgiven: leading/trailing whitespace, the case of the marker and
# the state word, `NEEDS_YOU` / `NEEDS YOU` for `NEEDS-YOU`, a missing colon,
# and any amount of space around the separator. Everything else degrades to an
# ordinary message row -- never dropped, never crashed, never guessed at. An
# unknown state word in particular must NOT become "working": the panel would
# then be confidently wrong about a machine the owner cannot see.
REPORT_MARKER = "CLAWDMETER-REPORT/1"

REPORT_STATE_WORDS = {
    "WORKING":   STATE_REPORT_WORKING,
    "NEEDS-YOU": STATE_REPORT_NEEDS_YOU,
    "BLOCKED":   STATE_REPORT_BLOCKED,
    "DONE":      STATE_REPORT_DONE,
}

_REPORT_RE = re.compile(
    r"^\s*CLAWDMETER-REPORT/1\s+"
    r"(WORKING|NEEDS[-_ ]?YOU|BLOCKED|DONE)"
    r"(?:\s*:\s*|\s+)"
    r"(\S.*)$",
    re.IGNORECASE,
)


def parse_report(body):
    """`(state_code, summary)` if `body` is a report reply, else None.

    None is not a failure path: it is how a malformed report degrades to the
    ordinary message row it already was. The caller keeps the body either way,
    so nothing is ever dropped for being badly formatted.
    """
    if not body:
        return None
    for line in body.splitlines():
        if not line.strip():
            continue                      # blank lead-in is whitespace, not prose
        m = _REPORT_RE.match(line)
        if not m:
            return None                   # first real line is not the contract
        word = re.sub(r"[_ ]+", "-", m.group(1).upper())
        state = REPORT_STATE_WORDS.get(word)
        if state is None:                 # unreachable via the regex; belt to
            return None                   # its braces if the alternation grows
        # Collapse the summary to one line: the card draws it as running text
        # and a stray newline would otherwise eat one of its two lines.
        summary = " ".join(m.group(2).split())
        return (state, summary) if summary else None
    return None


def report_text_max(budget):
    """Summary length, in BYTES, that keeps a full report round on the wire.

    Scales with the payload budget the same way text_max_for_budget does for
    message bodies, but a twelfth of it rather than a quarter: a round is
    several rows and a message is one. At the 500 bytes REPORT.md recommends
    this is 41 B -- 41 ASCII characters, or 13 Hangul syllables -- which is
    why the dispatcher asks agents for 40 CHARACTERS: one byte of slack, so
    an on-spec ASCII summary is never elided.
    """
    try:
        budget = int(budget)
    except (TypeError, ValueError):
        return REPORT_TEXT_MAX
    return max(REPORT_TEXT_MIN, min(REPORT_TEXT_MAX, budget // 12))


# Report sids live in their own 2-character alphabet, disjoint from every
# other producer's. Message sids and session sids are both 2 hex characters,
# so they already share one 256-value key space and can alias (see the note on
# s_notify_sids in ui.cpp); adding a third hex producer would have made that
# worse at exactly the moment there are ten of them. A leading letter outside
# [0-9a-f] cannot collide with either, and 19x36 = 684 slots keeps the
# birthday odds among ten agents around 6%.
_SID_HEAD = "ghijklmnopqrstuvwxy"          # 'z' reserved for MORE_SID
_SID_TAIL = "0123456789abcdefghijklmnopqrstuvwxyz"
MORE_SID = "zz"


def report_sid(sender):
    """Stable per AGENT, not per report: the card keeps its identity (and its
    slot in the notify set) while the same agent's state changes under it, so
    a WORKING -> NEEDS-YOU flip slides the existing card up instead of fading
    a new one in. The notify set is keyed on (sid, kind) and only the waiting
    states join it, so that flip still produces exactly one auto-jump."""
    h = int(hashlib.md5(("report|" + sender).encode("utf-8")).hexdigest()[:8], 16)
    return _SID_HEAD[h % len(_SID_HEAD)] + _SID_TAIL[(h // len(_SID_HEAD)) % len(_SID_TAIL)]


def row_sid(msg, translit=True):
    """The sid the DEVICE will see for this message.

    The panel knows a card by two characters and nothing else, so when a tap
    comes back over BLE ("go ahead on g4", "drop g4") this is the only thing
    that can turn it into a message again. It has to be computed exactly the
    way the wire row computes it -- so it is computed HERE, and message_row /
    report_row call it, rather than each end having its own copy of the rule.
    """
    if msg.is_report:
        return report_sid(panel_sender(msg.sender, translit))
    return msg.mid[:2]


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
    """One received cross-session message.

    `report_state` / `summary` are filled when the body matched the REPORT
    contract; they are None on ordinary mail, which is what every consumer
    tests to tell the two apart. The raw `body` is kept either way, so a
    report can still be rendered as the message it also is.
    """

    __slots__ = ("mid", "ts", "sender", "body", "source",
                 "report_state", "summary")

    def __init__(self, mid, ts, sender, body, source="",
                 report_state=None, summary=None):
        self.mid = mid
        self.ts = ts
        self.sender = sender
        self.body = body
        self.source = source
        self.report_state = report_state
        self.summary = summary

    @property
    def is_report(self):
        return self.report_state is not None

    def __repr__(self):  # pragma: no cover - debugging aid
        kind = f"report={self.report_state}" if self.is_report else "msg"
        return f"<Message {self.mid} from={self.sender!r} {kind} ts={self.ts:.0f}>"


def record_text(rec):
    """The candidate message text of a transcript record, or None.

    Both shapes a received message takes are accepted here -- see the
    _ARRIVAL_TYPE block above for what they are and why both are read.

    The filter is deliberately narrow either way, because the transcript of a
    session that *investigates* cross-session messaging is full of text that
    mentions them:

      * `isSidechain` must be falsy     -- sidechain traffic is a subagent's, not yours;
      * an assistant turn quoting the tag is not mail;
      * content must be text            -- see content_text() on tool_result blocks;
      * a `queue-operation` must be an ENQUEUE. A record that says a queued
        item was removed is not a message arriving. `operation` missing is
        allowed (a future version may spell it differently); `operation`
        present and saying something else is not.

    `userType == "external"` is what the verified processed record carries,
    but it is NOT required: it is undocumented and the tag is the stronger
    signal.
    """
    if not isinstance(rec, dict) or rec.get("isSidechain"):
        return None
    rtype = rec.get("type")
    if rtype == "user":
        msg = rec.get("message")
        if not isinstance(msg, dict):
            return None
        return content_text(msg.get("content"))
    if rtype == _ARRIVAL_TYPE:
        op = rec.get("operation")
        if op is not None and op != _ARRIVAL_OP:
            return None
        # content_text() rather than the bare string the verified record
        # carries: the shape is not a contract, and routing through the one
        # reader keeps the tool_result exclusion in force here too.
        return content_text(rec.get("content"))
    return None


def message_id(session, sender, body):
    """The stable identity of one arriving message: WHO sent WHAT to WHICH session.

    This is what collapses the arrival record and the processed record into a
    single card. It deliberately does NOT include:

      * the TIMESTAMP -- the whole point is that the two records disagree
        about it (207 ms on the verified pair, and unbounded when the
        receiving session sits on its queue). A coarse time bucket would only
        move the problem to the pair that straddles a bucket edge.
      * the `from` PIPE ID -- tempting, since it looks per-message
        (cc-msg-572ec92ff3b152decf8ea5ffef7664ad), but it is not: on this
        machine's transcripts one such id appears across 52 records and
        several distinct messages. It is the SENDING session's pipe, so it
        would not dedupe, and keying on an attribute that a version might
        emit in one shape and not the other would resurrect the double card
        this exists to prevent.

    Whitespace is collapsed before hashing so a shape that writes `\\r\\n`
    where the other writes `\\n` still matches, and only the first 160
    characters are taken -- long enough to tell messages apart, short enough
    that the key does not carry the whole body around.

    THE COST, stated honestly: a byte-identical body from the same sender to
    the same session, twice inside the id-retention window, shows as one card.
    That is the right answer for a device you read at a glance, where the
    second card would be indistinguishable from the first anyway.
    """
    key = "|".join((str(session), sender, " ".join(body.split())[:160]))
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:8]


def message_from_record(rec, source="", reports=True):
    """A transcript record -> Message, or None.

    `reports=False` reads a report reply as the ordinary message it is made
    of, which is what the `reports = off` switch does: the body is unchanged,
    only its interpretation is.
    """
    text = record_text(rec)
    if text is None:
        return None
    parsed = parse_cross_session(text)
    if parsed is None:
        return None
    sender, body = parsed
    mid = message_id(rec.get("sessionId") or source, sender, body)
    rep = parse_report(body) if reports else None
    return Message(mid, _epoch(rec.get("timestamp")), sender, body, source,
                   rep[0] if rep else None, rep[1] if rep else None)


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
                 text_max=MSG_TEXT_MAX, translit=True, keep_hangul=True,
                 now_fn=time.time, reports=True,
                 report_expire_s=DEFAULT_REPORT_EXPIRE_S,
                 budget=cs.DEFAULT_BUDGET_BYTES):
        self.roots = list(roots) if roots is not None else default_roots()
        self.freshness_s = freshness_s
        self.expire_s = expire_s
        self.max_rows = max_rows
        self.text_max = text_max
        self.translit = translit
        self.keep_hangul = keep_hangul
        self.reports = reports
        self.report_expire_s = report_expire_s
        self.budget = budget
        self._now = now_fn
        self._files = {}       # path -> [offset, inode, cold]
        self._messages = []    # live, newest last
        # Reports are keyed by AGENT, not by message: a status report
        # supersedes that agent's previous one rather than stacking beside it,
        # which is what makes a re-dispatch refresh the panel instead of
        # doubling it. `max_rows` (a burst cap for unsolicited mail) does not
        # apply here -- a round is meant to be one card per reachable agent,
        # and what bounds it is the device's row cap and the byte budget.
        self._reports = {}     # panel sender -> Message
        self._seen = {}        # mid -> ts, for de-duplication across re-reads
        # Message ids the owner tapped away on the device; refreshed each tick
        # by the poller from the daemon's file (see set_dismissed).
        self._dismissed = set()

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

    # ---- Dismissal: cards the owner tapped away on the device ----
    # The device suppresses a dismissed card locally the moment it is tapped --
    # that is what makes it leave under the finger, with no round trip. This
    # side is the other half of the promise: the panel is redrawn from what the
    # HOST sends every few seconds, so without a record here the card comes
    # back, which reads as the dismissal having failed rather than as the host
    # being the source of truth.
    #
    # Keyed on the MESSAGE ID, which is a hash of (session, sender, body): the
    # same words stay gone, and new words from the same agent come back. That
    # is the same rule the firmware applies, reached from the same direction,
    # and it is the one that matters -- suppressing an agent by NAME would
    # silence it for good, which is the failure a notifier must not have.
    #
    # The set is pushed in rather than accumulated, because the process that
    # LEARNS about a dismissal is the BLE daemon and the process that renders
    # rows is this one. The daemon owns the file; this is where it lands.

    def set_dismissed(self, mids):
        """Replace the dismissed-id set, and drop anything already live."""
        self._dismissed = set(mids or ())
        if not self._dismissed:
            return
        self._messages = [m for m in self._messages
                          if m.mid not in self._dismissed]
        self._reports = {k: m for k, m in self._reports.items()
                         if m.mid not in self._dismissed}

    def find_by_sid(self, sid):
        """The live message the device means by this sid, or None.

        Reports are searched first. Sids are two characters from two
        independent hashes, so a message and a report CAN collide -- and when
        they do the report is the one a tap is far more likely to have meant,
        because it is the only kind of card that carries an action.
        """
        if not sid:
            return None
        for msg in self.reports_live():
            if row_sid(msg, self.translit) == sid:
                return msg
        for msg in reversed(self._messages):
            if row_sid(msg, self.translit) == sid:
                return msg
        return None

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
                msg = message_from_record(rec, path, self.reports)
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
                if msg.mid in self._dismissed:
                    continue      # the owner cleared this one on the device
                if msg.is_report:
                    # One card per agent. A newer report replaces the older
                    # one outright -- keeping both would show a machine in two
                    # states at once, which is worse than showing it in the
                    # stale one. Out-of-order arrival is guarded explicitly
                    # rather than by luck: two transcripts read in one pass can
                    # yield an agent's replies in either order.
                    #
                    # AND IT DELIBERATELY SKIPS THE MID DEDUP BELOW. `_seen`
                    # remembers the most recent SEEN_KEEP_MIN ids whatever
                    # their age, which for mail is what stops a long-queued
                    # message drawing a second card when its session finally
                    # drains -- and for a report would mean an agent that
                    # re-reports the SAME state, in the same words, could
                    # never get its card back once the first one expired. On a
                    # quiet machine that is "never" literally: 512 ids is more
                    # than a day of reports. The arrival/processed pair still
                    # collapses to ONE card without it, because both records
                    # name the same agent and this key is the agent.
                    key = panel_sender(msg.sender, self.translit)
                    prev = self._reports.get(key)
                    if prev is None or msg.ts >= prev.ts:
                        self._reports[key] = msg
                    continue
                if msg.mid in self._seen:
                    # The normal case, not an edge one: every message is
                    # recorded twice -- on arrival and again when the session
                    # processes it -- so the second sighting lands here and
                    # the panel gets ONE card, dated from the first. Also
                    # covers a re-read after a rotation.
                    continue
                self._seen[msg.mid] = now
                self._messages.append(msg)
        self._expire(now)
        return list(self._messages) + list(self._reports.values())

    def reports_live(self):
        """Live reports, newest first. Read-only view for callers and tests."""
        return sorted(self._reports.values(), key=lambda m: -m.ts)

    def _expire(self, now):
        self._reports = {k: m for k, m in self._reports.items()
                         if now - (m.ts or now) <= self.report_expire_s}
        live = [m for m in self._messages if now - (m.ts or now) <= self.expire_s]
        # Sorted by arrival, not by the order files happened to be scanned in:
        # two transcripts read in one pass can yield messages out of order, and
        # both the "newest first" row order and the max_rows cut depend on it.
        live.sort(key=lambda m: m.ts)
        self._messages = live[-self.max_rows:] if len(live) > self.max_rows else live
        # Forget message ids on a horizon, but never fewer than the most
        # recent SEEN_KEEP_MIN of them whatever their age. The horizon on its
        # own was right when one message meant one record; it is not now that
        # a message is recorded on ARRIVAL and again when the session gets
        # round to it, because a session parked at a prompt can leave hours
        # between the two and the second sighting would draw a second card.
        # Insertion order is the age order here (mids are only ever added).
        horizon = self.expire_s + self.freshness_s + 60
        items = list(self._seen.items())
        floor = len(items) - SEEN_KEEP_MIN
        self._seen = {k: v for i, (k, v) in enumerate(items)
                      if i >= floor or now - v <= horizon}

    # -- wire rows ---------------------------------------------------------

    def rows(self, now=None, text_max=None, budget=None,
             max_rows=DEVICE_MAX_ROWS):
        """Live messages and reports as wire rows, most urgent first.

        The per-message length shrinks when several are live at once. Without
        that, two full-length message rows overflow the default 180-byte
        budget and cs.fit_payload() drops the older one from the tail --
        a message would vanish silently rather than arrive shortened.

        With no reports live this returns exactly what it always did, byte for
        byte: the report round is an added path, not a rewrite of the message
        one, and mail must keep behaving the way it is documented to.

        `max_rows` is the caller's share of the device's row cap. It is a
        parameter and not the constant because the fleet poller sometimes has
        to append a row of its own -- the staleness marker -- and a row
        reserved after the round has been fitted is a row the firmware
        silently discards (SESSION_MAX_ROWS is 6, and it drops the tail).
        """
        now = self._now() if now is None else now
        cap = self.text_max if text_max is None else text_max
        live = list(reversed(self._messages))
        if len(live) > 1:
            cap = max(MSG_TEXT_MIN, cap // len(live))
        msg_rows = [message_row(m, now, cap, self.translit, self.keep_hangul)
                    for m in live]
        if not self._reports:
            # inbox_max_rows already caps this well under the device's own
            # limit; the slice only matters when a caller has reserved a row.
            return msg_rows[:max_rows]

        budget = self.budget if budget is None else budget
        rcap = report_text_max(budget)
        rep_rows = [report_row(m, now, rcap, self.translit, self.keep_hangul)
                    for m in self.reports_live()]
        return fit_round(sorted(rep_rows + msg_rows, key=row_rank), budget,
                         max_rows)


def message_row(msg, now, text_max=MSG_TEXT_MAX, translit=True,
                keep_hangul=True):
    """One Message -> the positional wire row.

    Indices 0..12 carry exactly what they always have; index 13 is new.
    Unknown fields go out as the documented "unknown" values so the device
    hides those elements instead of drawing a confident zero.

    The BODY is the one field that keeps its Hangul (`keep_hangul`), because
    it is the one field whose font has the Hangul fallback. The sender does
    not -- see panel_sender.
    """
    elapsed = int(max(0.0, now - msg.ts)) if msg.ts else 0
    text = elide_message(to_panel_text(msg.body, translit, keep_hangul),
                         text_max)
    return [
        row_sid(msg, translit),          # 0  sid: stable, keys the card
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


def report_row(msg, now, text_max=REPORT_TEXT_MAX, translit=True,
               keep_hangul=True):
    """One report Message -> the positional wire row.

    Identical in SHAPE to a message row -- same 14 fields, same "not
    applicable" values, the summary riding in the same index 13 a body does --
    and different in exactly one field: the state. That is the point. The
    firmware keeps the message card's LAYOUT (sender line, then the words) and
    takes the dot colour, the sort bucket and the auto-jump from the state, so
    a report needs no new row kind, no new field and no new parser on the
    device.

    The summary keeps its Hangul for the same reason a message body does: it
    renders in the one font that has the fallback. The sender does not (see
    panel_sender), and neither does the state word -- that one is drawn by the
    firmware from the code, so it is English on the panel whatever language
    the agent wrote in.
    """
    elapsed = int(max(0.0, now - msg.ts)) if msg.ts else 0
    sender = panel_sender(msg.sender, translit)
    text = elide_message(to_panel_text(msg.summary or "", translit, keep_hangul),
                         text_max)
    return [
        row_sid(msg, translit),          # 0  sid: stable per AGENT
        sender,                          # 1  label: which agent reported
        msg.report_state,                # 2  state: the whole trick
        -1,                              # 3  ctx: not applicable
        elapsed,                         # 4  age of the report
        0,                               # 5  model: unknown
        0,                               # 6  tool: none
        0, 0, 0, 0,                      # 7-10 ntools/nagents/tdone/ttotal
        -1,                              # 11 tok: not applicable
        cs.REMOTE_UNKNOWN,               # 12 remote: not this API's to say
        text,                            # 13 the one-line summary
    ]


# What a row is worth on a panel you read at a glance. Reports jump the
# session sort entirely (they are people asking, not machines running), and
# among themselves they rank by how much they need:
#
#   0  NEEDS-YOU  a human's answer unblocks it, and the device will one day
#                 be able to send that answer -- the most actionable row there
#                 is, so it is the last thing that may ever be dropped
#   1  BLOCKED    a human is needed too, but at the keyboard: no message can
#                 clear a permission dialog
#   2  message    somebody said something unbidden
#   3  DONE       finished. Rarer than WORKING after a dispatch and therefore
#                 more informative: a finished agent is news, a busy one is
#                 the expected answer
#   4  WORKING    needs nothing. The default answer, and the first to go
#
# Tie-break is newest first, matching the message rows' own order.
_ROW_RANK = {
    STATE_REPORT_NEEDS_YOU: 0,
    STATE_REPORT_BLOCKED:   1,
    STATE_MESSAGE:          2,
    STATE_REPORT_DONE:      3,
    STATE_REPORT_WORKING:   4,
    STATE_REPORT_MORE:      5,
}


def row_rank(row):
    return (_ROW_RANK.get(row[2], 9), row[4])


_MORE_WORDS = (
    (STATE_REPORT_NEEDS_YOU, "need you"),
    (STATE_REPORT_BLOCKED,   "blocked"),
    (STATE_REPORT_DONE,      "done"),
    (STATE_REPORT_WORKING,   "working"),
)


def overflow_row(dropped):
    """The row that makes a dropped report visible instead of silent.

    Ten agents can answer one dispatch and the device parses six rows, so
    dropping is the normal case, not the edge one -- and a panel that just
    shows the first five is a panel that lies about the fleet. This says how
    many did not fit and what they said, in the same words the cards above it
    use, so the count is checkable at a glance.

    It is the quietest thing on the screen by design (state MORE renders dim,
    idle bucket, never in the notify set): the urgent rows are the ones above
    it, and this is the footnote that says the list is not the whole story.
    """
    counts = {}
    for row in dropped:
        counts[row[2]] = counts.get(row[2], 0) + 1
    parts = [f"{counts[state]} {word}" for state, word in _MORE_WORDS
             if counts.get(state)]
    nmsg = counts.get(STATE_MESSAGE, 0)
    if nmsg:
        parts.append(f"{nmsg} msg" + ("s" if nmsg > 1 else ""))
    return [
        MORE_SID,
        f"+{len(dropped)} MORE",
        STATE_REPORT_MORE,
        -1, 0, 0, 0, 0, 0, 0, 0, -1, cs.REMOTE_UNKNOWN,
        ", ".join(parts) or "not shown",
    ]


def fit_round(rows, budget, max_rows=DEVICE_MAX_ROWS):
    """Rank-sorted rows -> what actually goes on the wire, plus the marker.

    TWO caps bind here and both of them bite at ten agents:

      * the DEVICE parses SESSION_MAX_ROWS = 6 rows and silently discards the
        rest, so the host must do that cut itself to know what was lost; and
      * the BYTE BUDGET, which at the recommended 480 holds about five report
        rows (see REPORT.md for the arithmetic).

    Rows are dropped from the TAIL, which is why row_rank exists: the tail is
    the least urgent row, and NEEDS-YOU is never in it. Whatever goes,
    overflow_row() says so.

    The whole result is measured against `budget` here rather than left to
    cs.fit_payload downstream, because that one drops from the tail too -- and
    the tail, once a marker is appended, IS the marker. The one row whose job
    is to report the dropping would be the first thing dropped.
    """
    kept = list(rows[:max_rows])
    dropped = list(rows[max_rows:])
    while True:
        trial = kept + ([overflow_row(dropped)] if dropped else [])
        if len(trial) <= max_rows and _payload_bytes(trial) <= budget:
            return trial
        if not kept:
            # Nothing fits at all. Ship the marker alone: "N agents reported,
            # none of them fit" is a panel that is still telling the truth.
            return trial[:1]
        dropped.insert(0, kept.pop())


def _payload_bytes(rows):
    return len(cs.encode_payload(rows).encode("utf-8"))


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
        keep_hangul=_config_flag("inbox_hangul", True, config_path),
        # `reports = off` does not turn a report into nothing: it turns it back
        # into the ordinary message it is made of, so the words still reach the
        # panel and only the state colouring goes away.
        reports=_config_flag("reports", True, config_path),
        report_expire_s=_config_int("report_expire_s", DEFAULT_REPORT_EXPIRE_S,
                                    config_path),
        budget=budget,
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
    parser.add_argument("--no-hangul", action="store_true",
                        help="romanise Korean instead of sending it as "
                             "Hangul (for firmware older than the Korean "
                             "font, which would draw it as empty boxes)")
    parser.add_argument("--no-reports", action="store_true",
                        help="read report replies as ordinary messages "
                             "(see daemon/REPORT.md for the contract)")
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
        keep_hangul=not args.no_hangul,
        reports=not args.no_reports,
        budget=args.budget,
    )

    def report():
        msgs = watcher.poll()
        for m in msgs:
            kind = f"[{m.report_state}] " if m.is_report else ""
            log(f"{time.strftime('%H:%M:%S', time.localtime(m.ts))} "
                f"<{m.sender}> {kind}"
                f"{to_panel_text(m.body, watcher.translit, watcher.keep_hangul)[:120]}")
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
