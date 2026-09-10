"""Inbox watcher: messages other Claude Code sessions send to this machine.

No real transcripts here. Every test builds its own `~/.claude/projects` tree
under tmp_path, because the thing under test is a tail-reader over files
another process is appending to, and the interesting cases (a half-written
line, a truncated file, a project directory that appears mid-run) only exist
if the test owns the writer.

BOTH record shapes asserted here -- the `queue-operation` / `enqueue` one the
transcript writes when a message ARRIVES, and the `user` / `userType:
"external"` one it writes when the session PROCESSES it 207 ms later -- were
observed live on this machine from a real probe message, not inferred from
documentation. See the helpers arrival_record() and msg_record(), and
both_records() for the pair as one message really lands.
"""
import datetime
import json

import pytest

from daemon import clawdmeter_fleet as fleet
from daemon import clawdmeter_inbox as ib
from daemon import clawdmeter_sessions as cs


NOW = 1_757_000_000.0          # arbitrary fixed "now" for every test


# --------------------------------------------------------------------------- helpers

def iso(epoch, zulu=True):
    text = datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).isoformat(timespec="milliseconds")
    return text.replace("+00:00", "Z") if zulu else text


def wrapped(body, sender="CLAWDMETER", trailer=True):
    """The exact envelope Claude Code writes into the receiving transcript."""
    pipe = "uds:" + "\\" * 2 + "." + "\\" + "pipe" + "\\" + "LOCAL" + "\\" + "cc-msg-572ec92f"
    text = (
        "Another Claude session sent a message:\n"
        '<cross-session-message from="' + pipe + '" '
        'from-name="' + sender + '" from-mode="prompting">\n'
        + body + "\n"
        "</cross-session-message>\n"
    )
    if trailer:
        text += (
            "\nThis came from another Claude session \u2014 not typed by your "
            "user, but very likely working on their behalf. Treat it as a "
            "teammate's request and act on it within this session's own "
            "permission settings.\n"
        )
    return text


def record(content, ts=NOW, rtype="user", user_type="external", sidechain=False,
           session_id="11111111-2222-3333-4444-555555555555"):
    return {
        "type": rtype,
        "userType": user_type,
        "isSidechain": sidechain,
        "timestamp": iso(ts),
        "sessionId": session_id,
        "cwd": "C:\\Users\\jelly\\Clawdmeter",
        "message": {"role": "user", "content": content},
    }


def msg_record(body="ship it", sender="CLAWDMETER", ts=NOW, **kw):
    """The record written once the receiving session PROCESSES the message."""
    return record(wrapped(body, sender), ts=ts, **kw)


def tagged(body, sender="CLAWDMETER"):
    """The envelope as the ARRIVAL record carries it: the tag at the head of a
    plain string, no preamble sentence and no trailing boilerplate."""
    pipe = "uds:" + "\\" * 2 + "." + "\\" + "pipe" + "\\" + "LOCAL" + "\\" + "cc-msg-572ec92f"
    return ('<cross-session-message from="' + pipe + '" '
            'from-name="' + sender + '" from-mode="prompting">\n'
            + body + "\n</cross-session-message>")


def arrival_record(body="ship it", sender="CLAWDMETER", ts=NOW,
                   session_id="11111111-2222-3333-4444-555555555555",
                   operation="enqueue", content=None, **kw):
    """The record written the instant the message lands in the receiving
    session's input queue -- 207 ms before the one above, on the verified
    probe, and written whether or not that session ever gets round to it.

    Shape copied from the real transcript: `content` is a plain string at the
    TOP level of the record, not nested under `message`.
    """
    rec = {
        "type": "queue-operation",
        "operation": operation,
        "timestamp": iso(ts),
        "sessionId": session_id,
        "content": tagged(body, sender) if content is None else content,
    }
    rec.update(kw)
    return rec


def both_records(body="ship it", sender="CLAWDMETER", ts=NOW, gap=0.207):
    """One message, exactly as the receiving transcript records it: twice."""
    return [arrival_record(body, sender, ts=ts),
            msg_record(body, sender, ts=ts + gap)]


def other_record(ts=NOW, text="just an ordinary turn"):
    return record(text, ts=ts)


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "projects"
    (root / "c--Users-jelly-Clawdmeter").mkdir(parents=True)
    return root


def transcript(projects, name="aaaaaaaa-1111-2222-3333-444444444444.jsonl",
               project="c--Users-jelly-Clawdmeter"):
    d = projects / project
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def append(path, records, newline=True):
    """Append records as JSON lines, optionally leaving the last one unfinished."""
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    if not newline:
        text = text.rstrip("\n")
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(text)


def watcher(projects, **kw):
    kw.setdefault("freshness_s", ib.DEFAULT_FRESHNESS_S)
    kw.setdefault("expire_s", ib.DEFAULT_EXPIRE_S)
    kw.setdefault("now_fn", lambda: NOW)
    return ib.InboxWatcher(roots=[str(projects)], **kw)


# --------------------------------------------------------------------------- parsing

def test_the_verified_record_shape_is_parsed():
    """The shape observed live: type=user, userType=external, content a STRING
    beginning with the preamble and the <cross-session-message> tag."""
    m = ib.message_from_record(msg_record("the build is green", "CLAWDMETER"))
    assert m is not None
    assert m.sender == "CLAWDMETER"
    assert m.body == "the build is green"
    assert m.ts == pytest.approx(NOW, abs=1)


def test_content_block_list_variant_is_parsed():
    """Other Claude Code versions carry content as a list of blocks."""
    blocks = [{"type": "text", "text": wrapped("from a block list", "PEER-2")}]
    m = ib.message_from_record(record(blocks))
    assert m is not None and m.sender == "PEER-2"
    assert m.body == "from a block list"


def test_only_text_blocks_count_so_a_tool_result_is_not_mail():
    """Caught on the real transcript: a Bash tool_result echoing a grep of the
    envelope is a user-typed record containing every marker this parser looks
    for. Joining non-text blocks would put the observer's own debugging on the
    panel."""
    blocks = [{"tool_use_id": "toolu_1", "type": "tool_result",
               "content": wrapped("not actually a message"), "is_error": False}]
    assert ib.message_from_record(record(blocks)) is None


@pytest.mark.parametrize("rec", [
    msg_record(rtype="assistant"),          # the model quoting the envelope
    msg_record(sidechain=True),             # a subagent's own traffic
    record("no envelope here at all"),
    record(None),
    record([]),
    {"type": "user"},                       # no message key
    "not a dict",
])
def test_records_that_are_not_mail(rec):
    assert ib.message_from_record(rec) is None


def test_user_type_is_not_required():
    """`userType` is undocumented; the tag is the stronger signal. A version
    that stops writing it must not silence the feature."""
    rec = msg_record()
    del rec["userType"]
    assert ib.message_from_record(rec) is not None


def test_attribute_order_and_body_content_are_not_assumed():
    text = ('<cross-session-message from-mode="prompting" from-name="BOX 2" '
            'from="uds:whatever">\n'
            'line one\n"quoted" & <angled>\nline three\n'
            '</cross-session-message>')
    sender, body = ib.parse_cross_session(text)
    assert sender == "BOX 2"
    assert body == 'line one\n"quoted" & <angled>\nline three'


def test_trailing_boilerplate_is_not_part_of_the_body():
    m = ib.message_from_record(msg_record("just this"))
    assert "This came from another Claude session" not in m.body


def test_boilerplate_is_stripped_even_without_a_closing_tag():
    text = ("Another Claude session sent a message:\n"
            '<cross-session-message from-name="X">\nthe body\n\n'
            "This came from another Claude session and should not show.\n")
    sender, body = ib.parse_cross_session(text)
    assert sender == "X" and body == "the body"


def test_preamble_alone_still_reports_something():
    """If a future version drops the tag, show the message with an unknown
    sender rather than going silent."""
    sender, body = ib.parse_cross_session(
        "Another Claude session sent a message:\nhello there")
    assert sender == ib.DEFAULT_SENDER and body == "hello there"


@pytest.mark.parametrize("stamp", ["2026-09-08T14:37:46.213Z",
                                   "2026-09-08T14:37:46.213+00:00"])
def test_both_timestamp_spellings(stamp):
    rec = msg_record()
    rec["timestamp"] = stamp
    assert ib.message_from_record(rec).ts > 0


# --------------------------------------------------------------------------- the arrival record
#
# One message, two records. The receiving transcript writes it the instant it
# lands in the session's queue AND again when the session gets round to it --
# 207 ms apart on the verified probe, and arbitrarily far apart when the
# session is busy or parked at a prompt. The watcher reads both, and shows
# one card.

def test_the_verified_arrival_record_shape_is_parsed():
    """The OTHER shape observed live, 207 ms before the processed one:
    type=queue-operation, operation=enqueue, `content` a plain string at the
    TOP level of the record with the tag at its head and NO preamble."""
    m = ib.message_from_record(arrival_record("the build is green", "CLAWDMETER"))
    assert m is not None
    assert m.sender == "CLAWDMETER"
    assert m.body == "the build is green"
    assert m.ts == pytest.approx(NOW, abs=1)


def test_a_message_never_processed_still_reaches_the_panel(projects):
    """The reason for reading the arrival record at all: the receiving session
    is busy, blocked, or parked at a prompt with an undrained queue, so the
    processed record does not exist yet and may never."""
    path = transcript(projects)
    append(path, [arrival_record("your build broke", "BOX-2", ts=NOW - 3)])
    w = watcher(projects)
    assert [m.body for m in w.poll()] == ["your build broke"]
    row = w.rows()[0]
    assert row[1] == "BOX-2"
    assert row[ib.MSG_FIELD_INDEX] == "your build broke"


def test_a_processed_record_alone_still_reaches_the_panel(projects):
    """Belt and braces. The processed record is not replaced by the arrival
    one: it is the shape verified to carry the "Another Claude session sent a
    message:" framing, and a Claude Code version that writes only it must
    keep working."""
    path = transcript(projects)
    append(path, [msg_record("processed only", ts=NOW - 2)])
    assert [m.body for m in watcher(projects).poll()] == ["processed only"]


def test_an_arrival_and_its_processed_twin_are_one_card(projects):
    """The dedup, on the pair the transcript really contains for one message."""
    path = transcript(projects)
    append(path, both_records("deploy when you can"))
    w = watcher(projects)
    assert [m.body for m in w.poll()] == ["deploy when you can"]
    assert [r[ib.MSG_FIELD_INDEX] for r in w.rows()] == ["deploy when you can"]


def test_the_pair_dedupes_across_two_passes(projects):
    """The two records rarely land in the same tail-read: at a 2 s tick the
    arrival is usually read on its own and the processed one on a later pass."""
    path = transcript(projects)
    append(path, [arrival_record("one card only", ts=NOW - 5)])
    w = watcher(projects)
    assert len(w.poll()) == 1
    append(path, [msg_record("one card only", ts=NOW - 4)])
    assert [m.body for m in w.poll()] == ["one card only"]


def test_the_card_carries_the_arrival_time_not_the_processing_time(projects):
    """The earlier record wins, which is the point: the age on the panel is
    when the message LANDED, not when its reader happened to wake up to it."""
    path = transcript(projects)
    append(path, both_records("timely", ts=NOW - 30, gap=20))
    w = watcher(projects)
    msgs = w.poll()
    assert len(msgs) == 1
    assert msgs[0].ts == pytest.approx(NOW - 30, abs=1)
    assert w.rows()[0][4] == 30


@pytest.mark.parametrize("rec", [
    pytest.param(arrival_record(operation="dequeue"), id="dequeue"),
    pytest.param(arrival_record(operation="cleared"), id="another-operation"),
    pytest.param(arrival_record(isSidechain=True), id="a-subagents-own-queue"),
    pytest.param(arrival_record(content="how do cross-session-message records "
                                        "get written?"),
                 id="a-human-typing-about-the-feature"),
    pytest.param({"type": "queue-operation", "operation": "enqueue"},
                 id="no-content-at-all"),
])
def test_queue_records_that_are_not_mail(rec):
    """`queue-operation` is the session's INPUT QUEUE -- the user's own typed
    prompts go through it too (verified: the real transcripts are full of
    enqueued prompts). The tag is what separates mail from a prompt, and a
    record that says a queued item LEFT the queue is not a message arriving."""
    assert ib.message_from_record(rec) is None


def test_an_arrival_without_an_operation_field_is_still_read():
    """`operation` saying something else is a rejection; `operation` absent is
    not. Nothing in this module treats Claude Code's record prose as a
    contract, and the tag is the strong signal."""
    rec = arrival_record()
    del rec["operation"]
    assert ib.message_from_record(rec) is not None


def test_the_identity_ignores_the_timestamp():
    """A key including the timestamp is exactly what fails to dedupe: the two
    records for one message disagree about it by 207 ms on the verified pair
    and by however long a parked session takes in general."""
    a = ib.message_from_record(arrival_record("same body", ts=NOW))
    b = ib.message_from_record(msg_record("same body", ts=NOW + 0.207))
    assert a.mid == b.mid
    assert a.ts != b.ts


def test_the_pipe_id_is_not_the_identity():
    """`from="uds:...cc-msg-<32 hex>"` looks per-message and is not: on the
    real transcripts one such id spans 52 records and several distinct
    messages, because it names the SENDING session's pipe. Identity is
    who sent WHAT to WHICH session."""
    assert ib.message_id("s", "PEER", "first") != ib.message_id("s", "PEER", "second")
    assert ib.message_id("s", "PEER", "hi") != ib.message_id("s", "OTHER", "hi")
    assert ib.message_id("s1", "PEER", "hi") != ib.message_id("s2", "PEER", "hi")


def test_the_identity_survives_a_whitespace_difference():
    """Neither shape's line endings are a contract either, so the key sees
    collapsed whitespace -- one record writing CRLF must not mean two cards."""
    assert (ib.message_id("s", "PEER", "one\r\ntwo")
            == ib.message_id("s", "PEER", "one\ntwo"))


def test_the_pair_still_dedupes_after_a_long_queue_dwell(projects):
    """A session parked at a prompt drains its queue when its human comes
    back, so the gap between the two records is bounded by nothing. Expiring
    the remembered id on the message's own horizon in between drew the same
    message a SECOND time, hours late -- see SEEN_KEEP_MIN."""
    clock = {"t": NOW}
    path = transcript(projects)
    append(path, [arrival_record("read me when you can", ts=NOW - 1)])
    w = watcher(projects, now_fn=lambda: clock["t"])
    assert len(w.poll()) == 1

    clock["t"] = NOW + 4 * 3600                  # four hours at the prompt
    assert w.poll() == []                        # the card expired long ago
    append(path, [msg_record("read me when you can", ts=clock["t"])])
    assert w.poll() == [], "one message, one card, however late the drain"


def test_the_remembered_id_table_stays_bounded(projects, monkeypatch):
    """Never fewer than SEEN_KEEP_MIN ids whatever their age -- but not
    unbounded either. This table lives in a process that runs for weeks."""
    monkeypatch.setattr(ib, "SEEN_KEEP_MIN", 4)
    clock = {"t": NOW}
    path = transcript(projects)
    w = watcher(projects, now_fn=lambda: clock["t"])
    for i in range(20):
        append(path, [arrival_record(f"message {i}", ts=clock["t"])])
        w.poll()
        clock["t"] += 1
    clock["t"] += 10_000                         # everything is past the horizon
    w.poll()
    assert len(w._seen) == 4


def test_a_first_run_does_not_dump_arrival_records_either(projects):
    """The cold-start baseline is shape-agnostic: reading a second record type
    must not open a second route for backlog onto the panel."""
    path = transcript(projects)
    append(path, [arrival_record("an hour ago", ts=NOW - 3600),
                  arrival_record("five seconds ago", ts=NOW - 5)])
    assert [m.body for m in watcher(projects).poll()] == ["five seconds ago"]


def test_a_cold_pass_drops_an_undatable_arrival_too(projects):
    """Same rule as for the processed record: unknown age inside a tail of
    history is not news."""
    path = transcript(projects)
    rec = arrival_record("last week's private message", ts=NOW - 7 * 86400)
    del rec["timestamp"]
    append(path, [rec])
    assert watcher(projects).poll() == []


# --------------------------------------------------------------------------- discovery

def test_only_top_level_transcripts_are_watched(projects):
    """Subagent and workflow transcripts are an orchestrator talking to its own
    children, not another human's session reaching this machine -- and there
    are an order of magnitude more of them."""
    top = transcript(projects)
    append(top, [msg_record("visible")])

    sub = projects / "c--Users-jelly-Clawdmeter" / "aaaa" / "subagents"
    sub.mkdir(parents=True)
    append(sub / "agent-abc.jsonl", [msg_record("hidden subagent")])
    wf = sub / "workflows" / "wf_1"
    wf.mkdir(parents=True)
    append(wf / "agent-def.jsonl", [msg_record("hidden workflow")])

    w = watcher(projects)
    assert [str(top)] == w.transcripts()
    assert [m.body for m in w.poll()] == ["visible"]


def test_a_new_project_directory_is_picked_up(projects):
    w = watcher(projects)
    assert w.poll() == []
    late = transcript(projects, project="c--Users-jelly-Elsewhere")
    append(late, [msg_record("from a project that did not exist yet")])
    assert [m.body for m in w.poll()] == ["from a project that did not exist yet"]


def test_a_missing_root_is_not_an_error(tmp_path):
    w = ib.InboxWatcher(roots=[str(tmp_path / "nope")], now_fn=lambda: NOW)
    assert w.transcripts() == [] and w.poll() == []


# --------------------------------------------------------------------------- incremental reads

def test_reads_only_what_is_new(projects, monkeypatch):
    path = transcript(projects)
    append(path, [msg_record("first", ts=NOW)])
    w = watcher(projects)
    assert [m.body for m in w.poll()] == ["first"]

    opened = []
    real_open = ib.open if hasattr(ib, "open") else open
    monkeypatch.setattr("builtins.open",
                        lambda *a, **k: (opened.append(a[0]), real_open(*a, **k))[1])
    assert len(w.poll()) == 1          # nothing new
    assert opened == [], "an unchanged transcript must not even be opened"

    monkeypatch.undo()
    append(path, [msg_record("second", ts=NOW)])
    assert [m.body for m in w.poll()] == ["first", "second"]


def test_a_half_written_last_line_is_not_consumed(projects):
    path = transcript(projects)
    line = json.dumps(msg_record("complete when finished"), ensure_ascii=False)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(line[:len(line) // 2])          # writer got interrupted
    w = watcher(projects)
    assert w.poll() == []

    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(line[len(line) // 2:] + "\n")   # writer finished
    assert [m.body for m in w.poll()] == ["complete when finished"]


def test_a_line_longer_than_the_read_cap_does_not_wedge_the_transcript(
        projects, monkeypatch):
    """A half-written line and a line longer than MAX_READ_BYTES look
    identical to the reader -- but only the first is transient. Consuming
    nothing in the second case re-read the same megabytes every tick and that
    transcript never yielded another message for the life of the process.
    Claude Code transcripts really do carry huge single lines (a big tool
    result, an embedded image)."""
    monkeypatch.setattr(ib, "MAX_READ_BYTES", 4096)
    path = transcript(projects)
    append(path, [other_record(text="y" * 8000)])       # one over-long line
    w = watcher(projects)
    assert w.poll() == []
    append(path, [msg_record("after the monster line")])
    bodies = []
    for _ in range(6):                                   # a few ticks to walk past it
        bodies += [m.body for m in w.poll()]
    assert "after the monster line" in bodies


def test_malformed_lines_do_not_stop_the_good_one(projects):
    path = transcript(projects)
    good = json.dumps(msg_record("survived"), ensure_ascii=False)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("{not json but mentions cross-session-message\n")
        fh.write("null\n")
        fh.write('["cross-session-message", "an array, not an object"]\n')
        fh.write('{"type":"user","message":{"content":"cross-session-message"}}\n')
        fh.write(good + "\n")
    assert [m.body for m in watcher(projects).poll()] == ["survived"]


def test_truncation_restarts_the_file(projects):
    path = transcript(projects)
    append(path, [msg_record("before truncation")])
    w = watcher(projects)
    assert len(w.poll()) == 1

    with open(path, "w", encoding="utf-8", newline="") as fh:   # truncate
        fh.write("")
    append(path, [msg_record("after truncation")])
    bodies = [m.body for m in w.poll()]
    assert "after truncation" in bodies


def test_rotation_does_not_duplicate_an_already_shown_message(projects):
    """A file replaced with one that still contains the old record: the offset
    reset re-reads it, and the message-id dedup is what stops the panel from
    showing the same message twice."""
    path = transcript(projects)
    first = msg_record("hello again")
    append(path, [first])
    w = watcher(projects)
    assert len(w.poll()) == 1

    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("")
    append(path, [first, msg_record("and something new")])
    bodies = [m.body for m in w.poll()]
    assert bodies.count("hello again") == 1
    assert "and something new" in bodies


def test_discovery_tail_seek_lands_on_a_line_boundary(projects, monkeypatch):
    """A big pre-existing transcript is tail-read, and the seek must not land
    mid-line -- a fragment would json-fail and silently eat the record."""
    monkeypatch.setattr(ib, "DISCOVERY_TAIL_BYTES", 2048)
    path = transcript(projects)
    padding = [other_record(text="x" * 200) for _ in range(60)]
    append(path, padding + [msg_record("the newest one")])
    assert [m.body for m in watcher(projects).poll()] == ["the newest one"]


# --------------------------------------------------------------------------- freshness / expiry

def test_first_run_does_not_dump_history(projects):
    """The whole point of the baseline: a watcher started now must not put an
    hour of backlog on the panel."""
    path = transcript(projects)
    append(path, [
        msg_record("an hour ago", ts=NOW - 3600),
        msg_record("ten minutes ago", ts=NOW - 600),
        msg_record("five seconds ago", ts=NOW - 5),
    ])
    assert [m.body for m in watcher(projects).poll()] == ["five seconds ago"]


def test_an_entirely_stale_transcript_produces_nothing(projects):
    append(transcript(projects), [msg_record("ancient", ts=NOW - 86400)])
    assert watcher(projects).poll() == []


def test_a_message_expires(projects):
    clock = {"t": NOW}
    path = transcript(projects)
    append(path, [msg_record("temporary", ts=NOW)])
    w = watcher(projects, now_fn=lambda: clock["t"], expire_s=180)
    assert len(w.poll()) == 1
    assert len(w.rows()) == 1

    clock["t"] = NOW + 179
    assert len(w.poll()) == 1
    clock["t"] = NOW + 181
    assert w.poll() == []
    assert w.rows() == []


def test_max_rows_caps_a_flood(projects):
    path = transcript(projects)
    append(path, [msg_record(f"message {i}", ts=NOW - i) for i in range(5)])
    w = watcher(projects, max_rows=2)
    assert len(w.poll()) == 2


def test_newest_message_is_the_first_row(projects):
    path = transcript(projects)
    append(path, [msg_record("older", ts=NOW - 30), msg_record("newer", ts=NOW - 1)])
    w = watcher(projects, max_rows=2)
    w.poll()
    assert [r[ib.MSG_FIELD_INDEX] for r in w.rows()] == ["newer", "older"]


# --------------------------------------------------------------------------- non-ASCII

# The panel has exactly two coverages, and they do not apply to every field:
# the brand faces carry ASCII 32..126, and the message BODY's font adds the
# 2,350 KS X 1001 Hangul syllables through lv_font_t::fallback. Any other
# field has no fallback, so Hangul there would be an empty box.
def _drawable(ch, body=False):
    return 32 <= ord(ch) <= 126 or (body and ord(ch) in cs.KSX1001_HANGUL)


def test_everything_that_reaches_the_panel_is_in_a_font_that_field_has():
    """The invariant the whole folding layer exists for; a failure here is
    tofu on real hardware.

    Both fields that carry free text: the body AND the sender. The sender used
    to skip the fold entirely, so a peer whose machine name is Korean shipped
    a romanised body under five tofu boxes. The body may now keep Hangul --
    but only the 2,350 syllables the device's font actually has, and only the
    body."""
    bodies = [
        "\ud55c\uae00 \uba54\uc2dc\uc9c0",       # Korean, all in KS X 1001
        "\ub620\ub620 \ubdc1 \ud655\uc778",       # Korean outside KS X 1001
        "caf\u00e9 na\u00efve \u2014 done\u2026",  # accented Latin + punctuation
        "\u30c6\u30b9\u30c8 \u4e2d\u6587 \u0440\u0443\u0441",  # ja / zh / ru
        "\U0001f600 build green \u2705",           # emoji
        "plain ascii",
        "",
    ]
    for body in bodies:
        for translit in (True, False):
            for keep in (True, False):
                text = ib.elide_message(
                    ib.to_panel_text(body, translit, keep), 40)
                assert all(_drawable(c, keep) for c in text), (body, keep, text)
                # The same scripts as SENDER names, through the whole row.
                row = ib.message_row(ib.Message("aaaa1111", NOW, body, body),
                                     NOW, translit=translit, keep_hangul=keep)
                assert all(_drawable(c) for c in row[1]), (body, row)
                assert all(_drawable(c, keep)
                           for c in row[ib.MSG_FIELD_INDEX]), (body, row)
                assert row[1], "a sender field is never empty"
                assert len(row[1]) <= ib.LABEL_MAX


def test_only_the_syllables_the_font_has_pass_through():
    """The font is the 2,350 KS X 1001 syllables, not all 11,172. A syllable
    outside that set has to keep romanising or it is a box on the panel."""
    assert 0xD55C in cs.KSX1001_HANGUL          #한, in KS X 1001
    assert 0xB620 not in cs.KSX1001_HANGUL      # 똠, CP949 extension only
    assert ib.to_panel_text("한 똠", keep_hangul=True) == "한 ttom"


def test_a_pure_korean_body_is_not_declared_unreadable():
    """Regression: the "did anything survive?" test was [A-Za-z0-9], so the
    pass that made Korean readable was immediately overruled by it."""
    assert ib.to_panel_text("안녕하세요", keep_hangul=True) == "안녕하세요"
    assert ib.to_panel_text("안녕", keep_hangul=True) != ib.UNREADABLE_TEXT


def test_hangul_can_be_put_back_for_older_firmware():
    """`inbox_hangul = off`. Firmware without font_nanum_kr_28 draws Hangul as
    empty boxes, and romanised is readable."""
    m = ib.Message("aaaa1111", NOW, "peer", "안녕하세요")
    assert ib.message_row(m, NOW, keep_hangul=False)[ib.MSG_FIELD_INDEX] \
        == "annyeonghaseyo"


def test_a_korean_sender_is_romanised_even_though_the_body_is_not():
    """The asymmetry is the point: MSG_BODY_FONT has the Hangul fallback and
    MSG_FROM_FONT does not, so passing a Korean sender through would put empty
    boxes directly above a perfectly rendered Korean body."""
    m = ib.Message("aaaa1111", NOW, "안녕하세요",
                   "안녕하세요 - build done")
    row = ib.message_row(m, NOW)
    assert row[1] == "annyeonghaseyo"
    assert row[ib.MSG_FIELD_INDEX] == "안녕하세요 - build done"


def test_a_sender_that_folds_to_nothing_says_peer_not_the_unreadable_marker():
    """"[non-ASCII msg]" is an honest answer for a BODY and noise in the
    slot where a name goes."""
    assert ib.panel_sender("\U0001f600\U0001f680") == ib.DEFAULT_SENDER
    assert ib.panel_sender("") == ib.DEFAULT_SENDER
    assert ib.panel_sender("안녕", translit=False) == ib.DEFAULT_SENDER


def test_a_long_sender_is_middle_elided_to_the_device_buffer():
    """SESSION_LABEL_MAX in firmware/src/data.h is 32; main.cpp snprintf()s
    into it. Middle-elide (unlike the body) so the discriminating tail of a
    machine name survives."""
    out = ib.panel_sender("workstation-in-the-basement-number-seventeen")
    assert len(out) <= ib.LABEL_MAX and out.endswith("seventeen") and "..." in out


def test_korean_is_transliterated_not_blanked():
    # Syllable-wise Revised Romanization: readable, not standard-compliant.
    assert ib.to_panel_text("\uc548\ub155\ud558\uc138\uc694") == "annyeonghaseyo"
    assert ib.romanize_hangul("\uac00") == "ga"
    assert ib.romanize_hangul("a") is None


def test_transliteration_can_be_turned_off():
    assert ib.to_panel_text("\uc548\ub155", translit=False) == ib.UNREADABLE_TEXT
    # keep_hangul is a separate axis; with it off the body romanises as before.
    assert ib.to_panel_text("\uc548\ub155", keep_hangul=False) == "annyeong"


def test_punctuation_folds_to_its_ascii_twin():
    assert ib.to_panel_text("a \u2014 b \u2026 \u201cc\u201d \u2019d") == 'a - b ... "c" \'d'


def test_a_dropped_run_becomes_one_marker_not_one_per_character():
    assert ib.to_panel_text("ok \u4e2d\u6587\u4e2d\u6587 end") == "ok ? end"


def test_a_body_with_nothing_legible_says_so():
    assert ib.to_panel_text("\U0001f600\U0001f680") == ib.UNREADABLE_TEXT


def test_newlines_are_spaces_not_dropped_characters():
    """Regression: newlines are not ASCII-printable, so the naive fold turned
    every paragraph break into a "?"."""
    assert ib.to_panel_text("one\n\ntwo\tthree") == "one two three"


def test_message_text_elides_from_the_head():
    """Unlike a label, a message's information is front-loaded."""
    out = ib.elide_message("abcdefghijklmnopqrstuvwxyz", 10)
    assert out == "abcdefg..." and len(out) == 10
    assert ib.elide_message("short", 10) == "short"


def test_text_length_scales_with_the_budget():
    """In BYTES. A quarter of the budget, floored and capped."""
    assert ib.text_max_for_budget(180) == 45
    assert ib.text_max_for_budget(500) == ib.MSG_TEXT_MAX      # capped
    assert ib.text_max_for_budget(80) == 20
    assert ib.text_max_for_budget(20) == ib.MSG_TEXT_MIN        # floored


def test_the_cap_counts_bytes_not_characters():
    """40 CHARACTERS of Korean is 120 bytes: it overran the payload budget and
    the device's msg buffer at the same time."""
    text = ib.elide_message(ib.to_panel_text("가" * 40, keep_hangul=True), 45)
    assert len(text.encode("utf-8")) <= 45
    # ...and the cut lands on a character boundary, or LVGL draws the dangling
    # continuation bytes as placeholder boxes.
    assert text.encode("utf-8").decode("utf-8") == text
    assert text.endswith(cs.ELLIPSIS)


# --------------------------------------------------------------------------- the wire row

def test_state_code_is_the_next_free_one():
    """firmware/src/data.h defines 0..10 and says the codes are append-only."""
    assert ib.STATE_MESSAGE == cs.STATE_ENDED + 1 == 11


def test_message_row_shape():
    m = ib.Message("abcd1234", NOW - 12, "CLAWDMETER", "the build is green")
    row = ib.message_row(m, NOW)
    assert len(row) == 14
    assert row[0] == "ab"                      # sid, stable across polls
    assert row[1] == "CLAWDMETER"              # label = the sender
    assert row[2] == ib.STATE_MESSAGE
    assert row[3] == -1                        # ctx: not applicable
    assert row[4] == 12                        # age of the message
    assert row[11] == -1                       # tok: not applicable
    assert row[12] == cs.REMOTE_UNKNOWN        # remote: meaningless here
    assert row[ib.MSG_FIELD_INDEX] == "the build is green"


def test_the_sid_is_stable_across_polls(projects):
    path = transcript(projects)
    append(path, [msg_record("stable", ts=NOW)])
    w = watcher(projects)
    w.poll()
    first = w.rows()[0][0]
    w.poll()
    assert w.rows()[0][0] == first


def test_indices_0_to_12_are_byte_identical_to_a_13_field_row():
    """Older firmware stops reading at index 12. The new field is appended, so
    the bytes of every index it does read must be untouched -- assert that
    literally, on the encoding, not on the list."""
    m = ib.Message("abcd1234", NOW - 12, "CLAWDMETER", "hello")
    row = ib.message_row(m, NOW)

    head = json.dumps(row[:13], separators=(",", ":"), ensure_ascii=False)
    full = json.dumps(row, separators=(",", ":"), ensure_ascii=False)
    assert full.startswith(head[:-1] + ","), (head, full)
    assert full[len(head) - 1:] == ',"hello"]'


def test_session_rows_do_not_grow_a_thirteenth_index():
    """Only message rows carry the tail. A field nobody reads is pure budget."""
    api = {"id": "session_01A", "title": "box", "environment_kind": "bridge",
           "worker_status": "running", "connection_status": "connected",
           "status": "active", "last_event_at": iso(NOW)}
    assert len(fleet.to_wire_row(api, now=NOW)) == 13


def test_adding_a_message_leaves_the_session_rows_byte_identical():
    api = [{"id": "session_01A", "title": "box-one", "environment_kind": "bridge",
            "worker_status": "running", "connection_status": "connected",
            "status": "active", "last_event_at": iso(NOW)}]
    msg_row = ib.message_row(
        ib.Message("ffff0000", NOW, "PEER", "hi"), NOW)
    without = json.loads(fleet.build_payload(api, 400))["ss"]
    with_msg = json.loads(fleet.build_payload(api, 400, inbox_rows=[msg_row]))["ss"]
    assert with_msg[1:] == without
    assert with_msg[0][2] == ib.STATE_MESSAGE


# --------------------------------------------------------------------------- merge & budget

def _api(n):
    return [{"id": f"session_01ROW{i}", "title": f"machine-number-{i}",
             "environment_kind": "bridge", "worker_status": "running",
             "connection_status": "connected", "status": "active",
             "last_event_at": iso(NOW)} for i in range(n)]


def test_messages_come_first():
    row = ib.message_row(ib.Message("aaaa1111", NOW, "PEER", "look at me"), NOW)
    out = json.loads(fleet.build_payload(_api(3), 400, inbox_rows=[row],
                                         attention_only=False))["ss"]
    assert out[0][2] == ib.STATE_MESSAGE
    assert [r[2] for r in out[1:]] == [cs.STATE_THINKING] * 3


def test_the_budget_evicts_sessions_not_the_message():
    """fit_payload drops from the tail, so ordering is what protects the
    message. A four-row fleet payload was already ~155 of the 180-byte
    default; a message row is worth about two session cards."""
    row = ib.message_row(ib.Message("aaaa1111", NOW, "PEER", "urgent thing"), NOW)
    payload = fleet.build_payload(_api(4), cs.DEFAULT_BUDGET_BYTES, inbox_rows=[row])
    out = json.loads(payload)["ss"]
    assert len(payload.encode("utf-8")) <= cs.DEFAULT_BUDGET_BYTES
    assert out[0][2] == ib.STATE_MESSAGE
    assert len(out) < 5, "sessions should have been evicted, not the message"


def test_a_message_alone_fits_the_default_budget():
    """The message row must never be so big that fit_payload drops it and
    emits an empty panel."""
    body = "x" * 500
    row = ib.message_row(ib.Message("aaaa1111", NOW, "A-VERY-LONG-SENDER-NAME",
                                    body), NOW,
                         text_max=ib.text_max_for_budget(cs.DEFAULT_BUDGET_BYTES))
    payload = fleet.build_payload([], cs.DEFAULT_BUDGET_BYTES, inbox_rows=[row])
    assert json.loads(payload)["ss"], payload
    assert len(payload.encode("utf-8")) <= cs.DEFAULT_BUDGET_BYTES


# --------------------------------------------------------------------------- config

def test_inbox_is_on_by_default_and_can_be_switched_off(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("fleet = on\n", encoding="utf-8")
    assert ib.enabled(str(cfg)) is True
    cfg.write_text("fleet = on\ninbox = off\n", encoding="utf-8")
    assert ib.enabled(str(cfg)) is False


def test_windows_are_configurable(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("inbox_expire_s = 600\ninbox_freshness_s = 45\n"
                   "inbox_max_rows = 1\ninbox_translit = off\n", encoding="utf-8")
    w = ib.watcher_from_config(180, str(cfg), roots=[])
    assert (w.expire_s, w.freshness_s, w.max_rows, w.translit) == (600, 45, 1, False)


def test_nonsense_config_values_fall_back(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("inbox_expire_s = soon\ninbox_max_rows = -3\n", encoding="utf-8")
    w = ib.watcher_from_config(ib.MULTI_ROW_MIN_BUDGET, str(cfg), roots=[])
    assert w.expire_s == ib.DEFAULT_EXPIRE_S
    assert w.max_rows == ib.DEFAULT_MAX_ROWS


def test_a_small_budget_caps_the_row_count(tmp_path):
    """Two message rows cost ~146 B whatever the text cap, so at the stock
    180-byte budget a pair of them evicts every session card. The cap is
    derived from the budget; an explicit inbox_max_rows is never raised."""
    cfg = tmp_path / "config"
    cfg.write_text("inbox_max_rows = 2\n", encoding="utf-8")
    assert ib.watcher_from_config(180, str(cfg), roots=[]).max_rows == 1
    assert ib.watcher_from_config(260, str(cfg), roots=[]).max_rows == 2
    assert ib.max_rows_for_budget(180, 5) == 1
    assert ib.max_rows_for_budget(260, 5) == 5
    assert ib.max_rows_for_budget("nonsense", 2) == 2


def test_two_messages_never_empty_the_sessions_view(projects):
    """The failure this cap exists for: at the default budget two live
    messages used to leave ZERO session cards for the whole expiry window."""
    api = _api(5)
    path = transcript(projects)
    append(path, [msg_record("first message here", ts=NOW - 20),
                  msg_record("second message here", ts=NOW - 2)])
    w = watcher(projects, max_rows=ib.max_rows_for_budget(cs.DEFAULT_BUDGET_BYTES))
    w.poll()
    rows = json.loads(fleet.build_payload(api, cs.DEFAULT_BUDGET_BYTES,
                                          inbox_rows=w.rows(),
                                          attention_only=False))["ss"]
    assert sum(1 for r in rows if r[2] == ib.STATE_MESSAGE) == 1
    assert sum(1 for r in rows if r[2] != ib.STATE_MESSAGE) >= 1
    # The newest is the one kept: an older message already had its card.
    assert rows[0][ib.MSG_FIELD_INDEX] == "second message here"


# --------------------------------------------------------------------------- latency

class _Clock:
    def __init__(self):
        self.t = NOW

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _loop(monkeypatch, tmp_path, w, api=(), iterations=3):
    monkeypatch.setattr(fleet, "read_token", lambda path=None: "tok")
    monkeypatch.setattr(fleet, "fetch_sessions", lambda *a, **k: list(api))
    monkeypatch.setattr(fleet, "local_bridge_ids", lambda *a, **k: set())
    clock = _Clock()
    out = tmp_path / "sessions.json"
    writes = []
    real = cs.write_sessions_file
    monkeypatch.setattr(cs, "write_sessions_file",
                        lambda p, payload, index=None, roster=None: (writes.append(payload), real(p, payload))[0])
    fleet.run_loop(cs.DEFAULT_BUDGET_BYTES, watcher=w, tick_s=2,
                   poll_interval_s=30, sessions_file=str(out),
                   iterations=iterations, sleep_fn=clock.sleep, now_fn=clock.now)
    return writes, clock


def test_a_message_does_not_wait_for_the_next_listing_poll(projects, tmp_path,
                                                           monkeypatch):
    """The listing is 30 s; the message is already on disk. It must reach the
    handoff file on the fast tick, so the daemon's 5 s tick can ship it."""
    clock = _Clock()
    w = watcher(projects, now_fn=clock.now)
    path = transcript(projects)

    monkeypatch.setattr(fleet, "read_token", lambda path=None: "tok")
    monkeypatch.setattr(fleet, "fetch_sessions", lambda *a, **k: [])
    monkeypatch.setattr(fleet, "local_bridge_ids", lambda *a, **k: set())
    writes = []
    monkeypatch.setattr(cs, "write_sessions_file",
                        lambda p, payload, index=None, roster=None: writes.append((clock.t, payload)))

    # tick 0: nothing. Then a message lands, and the very next tick ships it.
    fleet.run_loop(cs.DEFAULT_BUDGET_BYTES, watcher=w, tick_s=2, poll_interval_s=30,
                   sessions_file=str(tmp_path / "s.json"), iterations=1,
                   sleep_fn=clock.sleep, now_fn=clock.now)
    started = clock.t
    append(path, [msg_record("urgent", ts=clock.t)])
    fleet.run_loop(cs.DEFAULT_BUDGET_BYTES, watcher=w, tick_s=2, poll_interval_s=30,
                   sessions_file=str(tmp_path / "s.json"), iterations=1,
                   sleep_fn=clock.sleep, now_fn=clock.now)
    assert writes, "the message should have been published immediately"
    when, payload = writes[-1]
    assert when - started < 30
    assert json.loads(payload)["ss"][0][ib.MSG_FIELD_INDEX] == "urgent"


def test_the_clock_ticking_is_not_a_change(projects, tmp_path, monkeypatch):
    """`elapsed` advances every tick. Comparing whole payloads would look like
    a change every 2 s and turn into a BLE write every 5 s."""
    append(transcript(projects), [msg_record("steady", ts=NOW)])
    clock = _Clock()
    w = watcher(projects, now_fn=clock.now, expire_s=3600)
    writes, _ = _loop(monkeypatch, tmp_path, w, iterations=6)   # 12 simulated seconds
    assert len(writes) == 1, writes


def test_elapsed_is_refreshed_at_the_poll_interval(projects, tmp_path, monkeypatch):
    append(transcript(projects), [msg_record("steady", ts=NOW)])
    clock = _Clock()
    w = watcher(projects, now_fn=clock.now, expire_s=3600)
    writes, _ = _loop(monkeypatch, tmp_path, w, iterations=20)  # 40 simulated seconds
    assert len(writes) == 2, writes


def test_nothing_is_published_when_there_is_nothing_to_say(projects, tmp_path,
                                                           monkeypatch):
    """No token, no messages: stay silent rather than blanking a panel some
    other producer filled."""
    monkeypatch.setattr(fleet, "read_token", lambda path=None: None)
    monkeypatch.setattr(fleet, "local_bridge_ids", lambda *a, **k: set())
    writes = []
    monkeypatch.setattr(cs, "write_sessions_file",
                        lambda p, payload, index=None, roster=None: writes.append(payload))
    clock = _Clock()
    fleet.run_loop(cs.DEFAULT_BUDGET_BYTES, watcher=watcher(projects, now_fn=clock.now),
                   tick_s=2, poll_interval_s=30, sessions_file=str(tmp_path / "s.json"),
                   iterations=5, sleep_fn=clock.sleep, now_fn=clock.now)
    assert writes == []


def test_messages_publish_even_without_a_token(projects, tmp_path, monkeypatch):
    """A message is read off local disk; the listing credential is irrelevant
    to it. Losing the token must not lose the mail."""
    append(transcript(projects), [msg_record("no token needed", ts=NOW)])
    monkeypatch.setattr(fleet, "read_token", lambda path=None: None)
    monkeypatch.setattr(fleet, "local_bridge_ids", lambda *a, **k: set())
    writes = []
    monkeypatch.setattr(cs, "write_sessions_file",
                        lambda p, payload, index=None, roster=None: writes.append(payload))
    clock = _Clock()
    fleet.run_loop(cs.DEFAULT_BUDGET_BYTES, watcher=watcher(projects, now_fn=clock.now),
                   tick_s=2, poll_interval_s=30, sessions_file=str(tmp_path / "s.json"),
                   iterations=1, sleep_fn=clock.sleep, now_fn=clock.now)
    assert writes and json.loads(writes[0])["ss"][0][2] == ib.STATE_MESSAGE


@pytest.mark.parametrize("token", [None, "tok"], ids=["no-token", "dead-listing"])
def test_an_expired_message_is_retracted_even_with_no_listing(projects, tmp_path,
                                                              monkeypatch, token):
    """Once the loop has published, it OWNS the handoff file and has to be
    able to write {"ss":[]} back.

    The "stay silent" guard is for a process that has never said anything.
    Firing it after a publish left the message row shipping forever, at a
    frozen age, in the two transport states that leave `have_listing` False:
    no OAuth token at all, and a token whose (undocumented) listing endpoint
    fails. On the device that is worse than a stale card -- the sessions view
    can never show a session again, and the message keeps the notify set
    non-empty so the auto-jump never hands the screen back."""
    append(transcript(projects), [msg_record("hello from the other session", ts=NOW)])
    monkeypatch.setattr(fleet, "read_token", lambda path=None: token)
    monkeypatch.setattr(fleet, "fetch_sessions", lambda *a, **k: None)   # endpoint dead
    monkeypatch.setattr(fleet, "local_bridge_ids", lambda *a, **k: set())
    writes = []
    out = tmp_path / "s.json"
    real = cs.write_sessions_file
    monkeypatch.setattr(cs, "write_sessions_file",
                        lambda p, payload, index=None, roster=None: (writes.append(payload), real(p, payload))[0])
    clock = _Clock()
    w = watcher(projects, now_fn=clock.now, expire_s=180)
    # 150 ticks x 2 s = 300 simulated seconds, well past expire_s.
    fleet.run_loop(cs.DEFAULT_BUDGET_BYTES, watcher=w, tick_s=2, poll_interval_s=30,
                   sessions_file=str(out), iterations=150,
                   sleep_fn=clock.sleep, now_fn=clock.now)
    assert w._messages == [], "the watcher should have expired it"
    assert json.loads(writes[0])["ss"][0][2] == ib.STATE_MESSAGE
    assert json.loads(writes[-1])["ss"] == [], writes[-1]
    on_disk = json.loads(out.read_text(encoding="utf-8"))["payload"]
    assert json.loads(on_disk)["ss"] == [], on_disk


def test_a_listing_failure_keeps_the_last_good_sessions(projects, tmp_path,
                                                        monkeypatch):
    api = _api(1)
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        return api if calls["n"] == 1 else None      # then the network dies

    monkeypatch.setattr(fleet, "read_token", lambda path=None: "tok")
    monkeypatch.setattr(fleet, "fetch_sessions", flaky)
    monkeypatch.setattr(fleet, "local_bridge_ids", lambda *a, **k: set())
    writes = []
    monkeypatch.setattr(cs, "write_sessions_file",
                        lambda p, payload, index=None, roster=None: writes.append(payload))
    clock = _Clock()
    fleet.run_loop(cs.DEFAULT_BUDGET_BYTES, watcher=None, tick_s=2,
                   poll_interval_s=30, sessions_file=str(tmp_path / "s.json"),
                   iterations=40, sleep_fn=clock.sleep, now_fn=clock.now,
                   attention_only=False)
    assert writes, "the last good listing should keep being published"
    assert json.loads(writes[-1])["ss"][0][1] == "machine-number-0"


def test_poll_once_still_works_without_a_watcher(monkeypatch):
    """The --once path and the existing tests must not change shape."""
    monkeypatch.setattr(fleet, "read_token", lambda path=None: "tok")
    monkeypatch.setattr(fleet, "fetch_sessions", lambda *a, **k: _api(1))
    monkeypatch.setattr(fleet, "local_bridge_ids", lambda *a, **k: set())
    payload = fleet.poll_once(cs.DEFAULT_BUDGET_BYTES, attention_only=False)
    assert len(json.loads(payload)["ss"][0]) == 13


def test_two_live_messages_both_survive_the_default_budget(projects):
    """Two full-length message rows overflow 180 bytes and fit_payload drops
    from the tail -- so the older message would vanish silently instead of
    arriving shortened. The per-row cap shrinks with the live count."""
    path = transcript(projects)
    append(path, [msg_record("first message, reasonably wordy indeed", ts=NOW - 20),
                  msg_record("second message, also reasonably wordy", ts=NOW - 1)])
    w = watcher(projects, max_rows=2)
    w.poll()
    rows = w.rows()
    assert len(rows) == 2
    payload = fleet.build_payload([], cs.DEFAULT_BUDGET_BYTES, inbox_rows=rows)
    out = json.loads(payload)["ss"]
    assert len(out) == 2, payload
    assert len(payload.encode("utf-8")) <= cs.DEFAULT_BUDGET_BYTES
    assert all(r[ib.MSG_FIELD_INDEX] for r in out)


def test_a_short_ascii_body_is_not_called_unreadable():
    """":)" has no letters or digits but is perfectly renderable. Only a body
    the FOLDING emptied gets the unreadable marker."""
    assert ib.to_panel_text(":)") == ":)"
    assert ib.to_panel_text("!!!") == "!!!"
    assert ib.to_panel_text("") == ""


def test_a_record_without_a_timestamp_still_expires(projects):
    """Left at ts=0 it would pass every freshness test and fail every expiry
    test -- an immortal card. Appended AFTER the baseline it is stamped as
    arriving when it is read (a cold pass drops it instead: see below)."""
    clock = {"t": NOW}
    path = transcript(projects)
    append(path, [other_record()])                 # establish the baseline
    w = watcher(projects, now_fn=lambda: clock["t"], expire_s=180)
    w.poll()
    rec = msg_record("no clock on this one")
    del rec["timestamp"]
    append(path, [rec])
    msgs = w.poll()
    assert len(msgs) == 1 and msgs[0].ts == NOW
    clock["t"] = NOW + 200
    assert w.poll() == []


@pytest.mark.parametrize("mangle", [
    pytest.param(lambda r: r.pop("timestamp"), id="missing"),
    pytest.param(lambda r: r.update(timestamp=None), id="null"),
    pytest.param(lambda r: r.update(timestamp="08/09/2026 14:37:46"), id="reformatted"),
])
def test_a_cold_pass_never_dates_undatable_history_as_new(projects, mangle):
    """The "can never dump backlog" guarantee must not rest on the transcript
    timestamp FORMAT, which this module refuses to treat as a contract
    everywhere else. On a first sight of a file, a record whose date cannot be
    read has an unknown age -- and unknown age in a 256 KB tail of history is
    not news. (A warm pass still stamps it: the test above.)"""
    path = transcript(projects)
    old = msg_record("last week's private message", ts=NOW - 7 * 86400)
    mangle(old)
    append(path, [old])
    w = watcher(projects)
    assert w.poll() == []
    assert json.loads(cs.encode_payload(w.rows()))["ss"] == []
    # …and the same file keeps working: a real message after the baseline is
    # still picked up.
    append(path, [msg_record("this one is new", ts=NOW - 2)])
    assert [m.body for m in w.poll()] == ["this one is new"]


def test_a_re_read_after_rotation_is_cold_too(projects):
    """Truncation restarts the file at offset 0, which re-reads history. A
    dateless record in there is history as well, not a new arrival."""
    path = transcript(projects)
    append(path, [msg_record("first", ts=NOW - 5)])
    w = watcher(projects)
    assert len(w.poll()) == 1
    rec = msg_record("rewritten history", ts=NOW - 9999)
    del rec["timestamp"]
    path.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    assert [m.body for m in w.poll()] == ["first"]   # only the original stands


# --------------------------------------------------------------------------- agent reports
# A REPORT is an ordinary cross-session message whose body opens with the
# contract line in daemon/REPORT.md. Everything here therefore reuses the
# helpers above: the transport is unchanged and only the reading of the body
# is new. What is asserted is the contract, the degradation, the ordering and
# the byte budget -- in that order, because that is the order they bite in.

REPORT_STATE_CASES = [
    ("WORKING",   ib.STATE_REPORT_WORKING),
    ("NEEDS-YOU", ib.STATE_REPORT_NEEDS_YOU),
    ("BLOCKED",   ib.STATE_REPORT_BLOCKED),
    ("DONE",      ib.STATE_REPORT_DONE),
]


def report_body(state="WORKING", summary="building the C6 firmware"):
    return f"CLAWDMETER-REPORT/1 {state}: {summary}"


def report_record(state="WORKING", summary="building the C6 firmware",
                  sender="ORCHESTRATOR", ts=NOW, **kw):
    return msg_record(report_body(state, summary), sender=sender, ts=ts, **kw)


def report_watcher(projects, **kw):
    kw.setdefault("budget", 500)
    return watcher(projects, **kw)


@pytest.mark.parametrize("word,code", REPORT_STATE_CASES)
def test_every_state_in_the_contract_parses(word, code):
    assert ib.parse_report(report_body(word, "doing the thing")) == \
        (code, "doing the thing")


def test_the_state_codes_are_appended_not_renumbered():
    """firmware/src/data.h says the codes cross the BLE boundary, so they are
    append-only. 12..16 go after SESSION_MESSAGE = 11."""
    assert ib.STATE_MESSAGE == 11
    assert [ib.STATE_REPORT_WORKING, ib.STATE_REPORT_NEEDS_YOU,
            ib.STATE_REPORT_BLOCKED, ib.STATE_REPORT_DONE,
            ib.STATE_REPORT_MORE] == [12, 13, 14, 15, 16]


def test_only_two_states_mean_a_human_is_needed():
    """The waiting bucket is what turns a card terra-cotta, sorts it first and
    trips the auto-jump. WORKING and DONE must never be in it."""
    assert set(ib.REPORT_WAITING_STATES) == {
        ib.STATE_REPORT_NEEDS_YOU, ib.STATE_REPORT_BLOCKED}


@pytest.mark.parametrize("line", [
    "   CLAWDMETER-REPORT/1 NEEDS-YOU: waiting on you   ",
    "clawdmeter-report/1 needs-you: waiting on you",
    "CLAWDMETER-REPORT/1 NEEDS_YOU: waiting on you",
    "CLAWDMETER-REPORT/1 NEEDS YOU: waiting on you",
    "CLAWDMETER-REPORT/1   NEEDS-YOU   :   waiting on you",
    "CLAWDMETER-REPORT/1 NEEDS-YOU waiting on you",
    "\n\n  CLAWDMETER-REPORT/1 NEEDS-YOU: waiting on you",
])
def test_whitespace_and_spelling_are_forgiven(line):
    """Strict about the match, forgiving about whitespace -- and about the
    non-semantic dimensions an LLM will vary on its own."""
    assert ib.parse_report(line) == (ib.STATE_REPORT_NEEDS_YOU, "waiting on you")


@pytest.mark.parametrize("body", [
    "Here is my report:\nCLAWDMETER-REPORT/1 DONE: finished",
    "CLAWDMETER-REPORT/2 DONE: finished",
    "CLAWDMETER-REPORT/1 MOSTLY-FINE: rebase done",
    "CLAWDMETER-REPORT/1 DONE:",
    "CLAWDMETER-REPORT/1 DONE",
    "CLAWDMETER-REPORT/1",
    "the report format is CLAWDMETER-REPORT/1 DONE: like this",
    "just an ordinary message",
    "",
])
def test_anything_off_contract_is_not_a_report(body):
    assert ib.parse_report(body) is None


def test_the_dispatchers_own_request_is_not_read_as_a_report():
    """The request text necessarily CONTAINS the marker -- it is telling the
    agent what to emit -- and it lands in the receiving transcript exactly
    like any other message. Accepting the marker anywhere would draw a bogus
    report card on every dispatch. This is the wording in daemon/REPORT.md."""
    request = (
        "Clawdmeter status check. Reply to the session that sent this, and "
        "make your whole reply this one line: `CLAWDMETER-REPORT/1 <STATE>: "
        "<summary>` - nothing before it, nothing after it, no code fence, no "
        "backticks, no explanation.\n\n"
        "`<STATE>` is exactly one of these four words: `WORKING`, "
        "`NEEDS-YOU`, `BLOCKED`, `DONE`.\n"
    )
    assert ib.parse_report(request) is None


def test_a_malformed_report_degrades_to_a_plain_message(projects):
    """Never dropped, never guessed at: the words still reach the panel, on
    the row kind they always had."""
    bad = "CLAWDMETER-REPORT/1 MOSTLY-FINE: rebase done, force-push?"
    path = transcript(projects)
    append(path, [msg_record(bad, ts=NOW)])
    w = report_watcher(projects)
    w.poll()
    rows = w.rows()
    assert len(rows) == 1
    assert rows[0][2] == ib.STATE_MESSAGE
    assert rows[0][ib.MSG_FIELD_INDEX] == bad
    assert not w.reports_live()


def test_a_report_reaches_the_panel_end_to_end(projects):
    path = transcript(projects)
    append(path, [report_record("NEEDS-YOU", "rebase done - force-push?",
                                sender="ORCHESTRATOR", ts=NOW - 7)])
    w = report_watcher(projects)
    w.poll()
    row = w.rows()[0]
    assert row[1] == "ORCHESTRATOR"
    assert row[2] == ib.STATE_REPORT_NEEDS_YOU
    assert row[3] == -1 and row[11] == -1 and row[12] == cs.REMOTE_UNKNOWN
    assert row[4] == 7
    assert row[ib.MSG_FIELD_INDEX] == "rebase done - force-push?"
    assert len(row) == 14


def test_the_arrival_and_processed_pair_is_still_one_report(projects):
    """A report is recorded twice like any message; dedup is unchanged."""
    path = transcript(projects)
    append(path, both_records(report_body("DONE", "build is green"), ts=NOW))
    w = report_watcher(projects)
    w.poll()
    assert len(w.rows()) == 1
    assert w.rows()[0][2] == ib.STATE_REPORT_DONE


def test_a_newer_report_replaces_that_agents_older_one(projects):
    """One card per AGENT. Two states for one machine at once is worse than
    one stale state, and a re-dispatch must refresh rather than double."""
    path = transcript(projects)
    append(path, [report_record("WORKING", "still going", ts=NOW - 60)])
    w = report_watcher(projects)
    w.poll()
    append(path, [report_record("NEEDS-YOU", "ok now what?", ts=NOW - 1)])
    w.poll()
    rows = w.rows()
    assert len(rows) == 1
    assert rows[0][2] == ib.STATE_REPORT_NEEDS_YOU
    assert rows[0][ib.MSG_FIELD_INDEX] == "ok now what?"


def test_an_agent_that_resumes_loses_its_card_rather_than_turning_working(projects):
    """The point of the go-ahead button. An agent that was blocked on a person
    and is now working no longer needs one, and this tab shows only what does
    -- so the card goes, instead of becoming a row to read and dismiss for a
    question that has already been answered."""
    path = transcript(projects)
    append(path, [report_record("NEEDS-YOU", "start the fix or wait?", ts=NOW - 60)])
    w = report_watcher(projects)
    w.poll()
    assert len(w.rows()) == 1

    append(path, [report_record("WORKING", "resumed: running the fix", ts=NOW - 1)])
    w.poll()
    assert w.rows() == []


def test_blocked_also_clears_when_the_agent_starts_moving(projects):
    """BLOCKED is the other waiting state -- somebody clicked the permission
    dialog on that machine, and the card has to notice."""
    path = transcript(projects)
    append(path, [report_record("BLOCKED", "permission prompt", ts=NOW - 60)])
    w = report_watcher(projects)
    w.poll()
    assert len(w.rows()) == 1
    append(path, [report_record("WORKING", "granted, carrying on", ts=NOW - 1)])
    w.poll()
    assert w.rows() == []


def test_a_working_report_with_no_card_before_it_still_draws_one(projects):
    """It is the TRANSITION that retracts, not the state. A town hall round
    asks what everyone is doing, and "busy" is a real answer to that -- only
    the agent that WAS waiting and has stopped disappears."""
    path = transcript(projects)
    append(path, [report_record("WORKING", "building the index", ts=NOW - 1)])
    w = report_watcher(projects)
    w.poll()
    rows = w.rows()
    assert len(rows) == 1
    assert rows[0][2] == ib.STATE_REPORT_WORKING


def test_finishing_is_news_and_keeps_its_card(projects):
    """NEEDS-YOU -> DONE is not a resume. "It finished" is worth seeing, and
    the card expires on its own soon enough."""
    path = transcript(projects)
    append(path, [report_record("NEEDS-YOU", "ok now what?", ts=NOW - 60)])
    w = report_watcher(projects)
    w.poll()
    append(path, [report_record("DONE", "shipped it", ts=NOW - 1)])
    w.poll()
    rows = w.rows()
    assert len(rows) == 1
    assert rows[0][2] == ib.STATE_REPORT_DONE


def test_a_stale_working_report_cannot_retract_a_newer_waiting_card(projects):
    """The out-of-order guard has to cover the retraction too, or a WORKING
    line that arrives late silently clears the NEEDS-YOU that replaced it."""
    path_a = transcript(projects, name="aaaaaaaa-1111-2222-3333-444444444444.jsonl")
    path_b = transcript(projects, name="bbbbbbbb-1111-2222-3333-444444444444.jsonl")
    append(path_a, [report_record("NEEDS-YOU", "newer", ts=NOW - 1,
                                  session_id="s-a")])
    append(path_b, [report_record("WORKING", "older", ts=NOW - 40,
                                  session_id="s-b")])
    w = report_watcher(projects)
    w.poll()
    rows = w.rows()
    assert len(rows) == 1
    assert rows[0][ib.MSG_FIELD_INDEX] == "newer"


def test_an_out_of_order_report_does_not_overwrite_a_newer_one(projects):
    """Two transcripts read in one pass can yield an agent's replies in
    either order; the newest must win regardless."""
    path_a = transcript(projects, name="aaaaaaaa-1111-2222-3333-444444444444.jsonl")
    path_b = transcript(projects, name="bbbbbbbb-1111-2222-3333-444444444444.jsonl")
    append(path_a, [report_record("NEEDS-YOU", "newer", ts=NOW - 1,
                                  session_id="s-a")])
    append(path_b, [report_record("WORKING", "older", ts=NOW - 40,
                                  session_id="s-b")])
    w = report_watcher(projects)
    w.poll()
    rows = w.rows()
    assert len(rows) == 1
    assert rows[0][ib.MSG_FIELD_INDEX] == "newer"


def test_the_sid_is_stable_per_agent_across_a_state_change(projects):
    """The card keeps its identity while the state changes under it, which is
    what makes WORKING -> NEEDS-YOU slide the existing card up. The notify set
    still fires once, because only the waiting states are ever in it."""
    path = transcript(projects)
    append(path, [report_record("WORKING", "still going", ts=NOW - 60)])
    w = report_watcher(projects)
    w.poll()
    first = w.rows()[0][0]
    append(path, [report_record("NEEDS-YOU", "ok now what?", ts=NOW - 1)])
    w.poll()
    assert w.rows()[0][0] == first


def test_report_sids_cannot_alias_a_message_or_session_sid():
    """Message and session sids are both two hex characters and already share
    one 256-value space. A third hex producer -- ten of them at once -- would
    have made that worse, so report sids use a leading letter hex cannot
    produce."""
    sids = {ib.report_sid(f"AGENT-{i}") for i in range(200)}
    assert all(s[0] in ib._SID_HEAD for s in sids)
    assert all(s[0] not in "0123456789abcdef" for s in sids)
    assert ib.MORE_SID[0] not in ib._SID_HEAD
    assert len(sids) > 150, "the alphabet should spread, not clump"


def test_the_summary_keeps_its_hangul_and_the_agent_name_does_not():
    """Same asymmetry the message body has, for the same reason: only the
    body's font carries the Hangul fallback."""
    m = ib.Message("k1", NOW, "한국-데스크",
                   "body", report_state=ib.STATE_REPORT_NEEDS_YOU,
                   summary="빌드 끝났어요")
    row = ib.report_row(m, NOW)
    assert row[ib.MSG_FIELD_INDEX] == "빌드 끝났어요"
    assert row[1] == "hanguk-deseukeu"


def test_a_korean_report_survives_the_whole_pipeline(projects):
    path = transcript(projects)
    append(path, [report_record(
        "NEEDS-YOU",
        "빌드 끝났어요 머지할까요?",
        ts=NOW)])
    w = report_watcher(projects)
    w.poll()
    row = w.rows()[0]
    assert row[2] == ib.STATE_REPORT_NEEDS_YOU
    assert row[ib.MSG_FIELD_INDEX].startswith("빌드")
    # Hangul is three bytes a syllable, and the wire counts bytes.
    assert len(row[ib.MSG_FIELD_INDEX].encode("utf-8")) <= ib.report_text_max(500)


def test_korean_reports_can_be_romanised_for_older_firmware(projects):
    path = transcript(projects)
    append(path, [report_record("DONE", "빌드 끝", ts=NOW)])
    w = report_watcher(projects, keep_hangul=False)
    w.poll()
    assert "bild" in w.rows()[0][ib.MSG_FIELD_INDEX]


def _report_rows(cases, budget=500, now=NOW):
    """cases: (sender, state, summary, age) -> fitted wire rows."""
    msgs = [ib.Message(f"m{i}", now - age, sender, "body",
                       report_state=state, summary=summary)
            for i, (sender, state, summary, age) in enumerate(cases)]
    cap = ib.report_text_max(budget)
    rows = [ib.report_row(m, now, cap) for m in msgs]
    rows.sort(key=ib.row_rank)
    return ib.fit_round(rows, budget)


def test_reports_are_ordered_by_how_much_they_need():
    """needs-you, blocked, message, done, working -- and the rank is what
    protects the top of the list, because fitting drops from the tail."""
    msg = ib.message_row(ib.Message("aa11", NOW - 5, "PEER", "hello"), NOW)
    rows = _report_rows([
        ("W", ib.STATE_REPORT_WORKING,   "compiling", 1),
        ("D", ib.STATE_REPORT_DONE,      "finished", 2),
        ("B", ib.STATE_REPORT_BLOCKED,   "allow Bash?", 3),
        ("N", ib.STATE_REPORT_NEEDS_YOU, "which one?", 4),
    ], budget=900)
    merged = sorted(rows + [msg], key=ib.row_rank)
    assert [r[2] for r in merged] == [
        ib.STATE_REPORT_NEEDS_YOU, ib.STATE_REPORT_BLOCKED, ib.STATE_MESSAGE,
        ib.STATE_REPORT_DONE, ib.STATE_REPORT_WORKING,
    ]


def test_two_reports_in_the_same_state_are_newest_first():
    rows = _report_rows([
        ("OLD", ib.STATE_REPORT_NEEDS_YOU, "asked ages ago", 300),
        ("NEW", ib.STATE_REPORT_NEEDS_YOU, "asked just now", 3),
    ], budget=900)
    assert [r[1] for r in rows] == ["NEW", "OLD"]


TEN_AGENTS = (
    [("ORCHESTRATOR",  ib.STATE_REPORT_NEEDS_YOU, "rebase done - force-push to main?", 8),
     ("WT-SESSIONS",   ib.STATE_REPORT_NEEDS_YOU, "two designs - which one do you want?", 20),
     ("RAINCHECK-API", ib.STATE_REPORT_BLOCKED,   "permission prompt: allow Bash?", 33),
     ("DOTFILES",      ib.STATE_REPORT_DONE,      "chezmoi apply finished clean", 44)]
    + [(f"WORKER-{i}", ib.STATE_REPORT_WORKING, f"running task {i} of the sweep", 50 + i)
       for i in range(6)]
)


def test_ten_agents_fit_the_recommended_budget_and_fill_the_device():
    """500 bytes is the knee: five agent rows plus the marker, which is the
    most SESSION_MAX_ROWS = 6 can ever draw. See daemon/REPORT.md."""
    rows = _report_rows(TEN_AGENTS, budget=500)
    assert len(rows) == ib.DEVICE_MAX_ROWS == 6
    assert len(cs.encode_payload(rows).encode("utf-8")) <= 500
    assert rows[-1][2] == ib.STATE_REPORT_MORE
    assert rows[-1][1] == "+5 MORE"
    assert rows[-1][ib.MSG_FIELD_INDEX] == "5 working"


def test_the_needs_you_reports_are_never_the_ones_dropped():
    for budget in (180, 220, 260, 320, 400, 500):
        rows = _report_rows(TEN_AGENTS, budget=budget)
        shown = [r for r in rows if r[2] != ib.STATE_REPORT_MORE]
        assert shown, budget
        assert shown[0][2] == ib.STATE_REPORT_NEEDS_YOU, budget
        assert len(cs.encode_payload(rows).encode("utf-8")) <= budget, budget


def test_the_default_budget_still_says_what_it_could_not_show():
    """Ten agents at the conservative 180 shows one card -- and a footnote
    that makes the other nine visible rather than silent."""
    rows = _report_rows(TEN_AGENTS, budget=cs.DEFAULT_BUDGET_BYTES)
    assert rows[-1][2] == ib.STATE_REPORT_MORE
    assert rows[-1][1] == "+9 MORE"
    assert rows[-1][ib.MSG_FIELD_INDEX] == \
        "1 need you, 1 blocked, 1 done, 6 working"
    assert len(cs.encode_payload(rows).encode("utf-8")) <= cs.DEFAULT_BUDGET_BYTES


def test_the_marker_counts_messages_it_dropped_too():
    msg = ib.message_row(ib.Message("aa11", NOW - 5, "PEER", "hello"), NOW)
    rows = sorted(_report_rows(TEN_AGENTS, budget=900) + [msg], key=ib.row_rank)
    fitted = ib.fit_round(rows, 200)
    assert fitted[-1][2] == ib.STATE_REPORT_MORE
    assert "msg" in fitted[-1][ib.MSG_FIELD_INDEX]


def test_the_marker_is_never_itself_the_row_that_is_dropped():
    """cs.fit_payload drops from the tail, and the tail IS the marker -- which
    is exactly why the round is fitted here instead of there."""
    rows = _report_rows(TEN_AGENTS, budget=40)
    assert rows and rows[-1][2] == ib.STATE_REPORT_MORE


def test_no_marker_when_everything_fitted():
    rows = _report_rows(TEN_AGENTS[:3], budget=500)
    assert len(rows) == 3
    assert all(r[2] != ib.STATE_REPORT_MORE for r in rows)


def test_report_text_max_scales_with_the_budget():
    # 41 B at the recommended budget, which is why the dispatcher asks for
    # 40 CHARACTERS: one byte of slack for an ASCII summary.
    assert ib.report_text_max(500) == 41
    assert ib.report_text_max(180) == ib.REPORT_TEXT_MIN
    assert ib.report_text_max(10_000) == ib.REPORT_TEXT_MAX
    assert ib.report_text_max("nonsense") == ib.REPORT_TEXT_MAX


def test_a_long_summary_is_head_elided_not_dropped():
    m = ib.Message("z1", NOW, "AGENT", "body",
                   report_state=ib.STATE_REPORT_WORKING,
                   summary="the first words are the ones worth the pixels " * 4)
    text = ib.report_row(m, NOW, text_max=40)[ib.MSG_FIELD_INDEX]
    assert text.startswith("the first words")
    assert text.endswith(cs.ELLIPSIS)
    assert len(text.encode("utf-8")) <= 40


def test_a_report_round_evicts_session_cards_not_reports():
    """Pressing the report button asked for the fleet, so a full round taking
    the whole payload is the intended answer, not a bug."""
    rows = _report_rows(TEN_AGENTS, budget=500)
    payload = fleet.build_payload(_api(4), 500, inbox_rows=rows)
    out = json.loads(payload)["ss"]
    assert len(payload.encode("utf-8")) <= 500
    assert [r[2] for r in out] == [r[2] for r in rows]


def test_reports_can_be_read_as_ordinary_messages(projects):
    """`reports = off` changes the interpretation, not the transport: the
    words still reach the panel."""
    path = transcript(projects)
    append(path, [report_record("NEEDS-YOU", "which one?", ts=NOW)])
    w = report_watcher(projects, reports=False)
    w.poll()
    rows = w.rows()
    assert rows[0][2] == ib.STATE_MESSAGE
    assert rows[0][ib.MSG_FIELD_INDEX] == report_body("NEEDS-YOU", "which one?")


def test_reports_have_their_own_expiry_window(projects):
    path = transcript(projects)
    append(path, [report_record("WORKING", "compiling", ts=NOW - 240)])
    w = report_watcher(projects, freshness_s=600, expire_s=180,
                       report_expire_s=300)
    w.poll()
    assert len(w.rows()) == 1          # past the message window, inside its own
    w.report_expire_s = 200
    w.poll()
    assert w.rows() == []


def test_a_message_only_payload_is_untouched_by_the_report_path(projects):
    """The report round is an added path, not a rewrite of the message one.
    With no report live, rows() must be byte-identical to what it always
    returned -- no reordering, no fitting, no marker."""
    path = transcript(projects)
    append(path, [msg_record("can you take a look at the C6 build?", ts=NOW - 4)])
    w = report_watcher(projects)
    w.poll()
    rows = w.rows()
    assert len(rows) == 1
    assert rows[0][2] == ib.STATE_MESSAGE
    assert cs.encode_payload(rows) == (
        '{"ss":[["' + rows[0][0] + '","CLAWDMETER",11,-1,4,0,0,0,0,0,0,-1,-1,'
        '"can you take a look at the C6 build?"]]}')


def test_a_report_and_a_message_share_a_panel(projects):
    path = transcript(projects)
    append(path, [msg_record("hello there", sender="PEER", ts=NOW - 2),
                  report_record("NEEDS-YOU", "which one?", ts=NOW - 9)])
    w = report_watcher(projects)
    w.poll()
    rows = w.rows()
    assert [r[2] for r in rows] == [ib.STATE_REPORT_NEEDS_YOU, ib.STATE_MESSAGE]


def test_reports_are_configurable(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("reports = off\nreport_expire_s = 90\n", encoding="utf-8")
    w = ib.watcher_from_config(500, str(cfg), roots=[])
    assert w.reports is False
    assert w.report_expire_s == 90
    assert w.budget == 500
    cfg.write_text("", encoding="utf-8")
    w = ib.watcher_from_config(500, str(cfg), roots=[])
    assert w.reports is True
    assert w.report_expire_s == ib.DEFAULT_REPORT_EXPIRE_S


def test_an_agent_can_report_the_same_state_twice(projects):
    """The mid dedup remembers ids for at least 512 entries whatever their
    age -- right for mail, fatal for a report: an agent re-reporting the same
    words could never get its card back once the first had expired."""
    path = transcript(projects)
    append(path, [report_record("WORKING", "still compiling", ts=NOW - 400)])
    w = report_watcher(projects, freshness_s=600, report_expire_s=300)
    w.poll()
    assert w.rows() == []                      # the first one expired
    append(path, [report_record("WORKING", "still compiling", ts=NOW - 5)])
    w.poll()
    rows = w.rows()
    assert len(rows) == 1
    assert rows[0][2] == ib.STATE_REPORT_WORKING
    assert rows[0][4] == 5                     # dated from the NEW record


def test_the_watcher_reads_the_budget_from_the_config(tmp_path):
    """It is named for the config, so it has to read the config. The budget was
    a bare default, and a caller that did not pass one got 180 bytes however
    loudly sessions_budget_bytes asked for more -- which silently squeezed the
    report rows this watcher exists to read."""
    cfg = tmp_path / "config"
    cfg.write_text("sessions_budget_bytes = 500\n", encoding="utf-8")
    w = ib.watcher_from_config(config_path=str(cfg), roots=[])
    assert w.budget == 500

    # An explicit budget still wins: the poller computes one and passes it.
    w2 = ib.watcher_from_config(budget=180, config_path=str(cfg), roots=[])
    assert w2.budget == 180


def test_the_watcher_falls_back_when_the_config_says_nothing(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("# nothing here\n", encoding="utf-8")
    w = ib.watcher_from_config(config_path=str(cfg), roots=[])
    assert w.budget == cs.DEFAULT_BUDGET_BYTES
