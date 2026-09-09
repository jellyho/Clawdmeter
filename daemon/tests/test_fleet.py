"""Fleet poller: the sessions running on OTHER machines.

No network here. Every fetch is fed a fake opener, because the endpoint under
test is an undocumented internal one and the point of these tests is that the
mapping and the failure handling are right, not that Anthropic is up.
"""
import json
import os
import urllib.error

import pytest

from daemon import clawdmeter_fleet as fleet
from daemon import clawdmeter_inbox as inbox
from daemon import clawdmeter_sessions as cs


# --------------------------------------------------------------------------- helpers

def api_row(rid="session_01AAA", title="laptop-api", kind="bridge",
            worker="running", conn="connected", status="active",
            tool=None, last="2026-09-08T12:00:00Z"):
    row = {
        "id": rid,
        "title": title,
        "environment_kind": kind,
        "worker_status": worker,
        "connection_status": conn,
        "status": status,
        "last_event_at": last,
    }
    if tool:
        row["external_metadata"] = {"pending_action": {
            "display_tool_name": tool,
            "action_description": f"run {tool}",
        }}
    return row


def opener_for(pages):
    """A urlopen stand-in that returns each page in turn."""
    calls = {"n": 0, "urls": []}

    class Resp:
        def __init__(self, body):
            self._body = json.dumps(body).encode()

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _open(req, timeout=None):
        calls["urls"].append(req.full_url)
        i = min(calls["n"], len(pages) - 1)
        calls["n"] += 1
        return Resp(pages[i])

    _open.calls = calls
    return _open


# --------------------------------------------------------------------------- id handling

def test_strip_id_prefix_handles_both_spellings():
    assert fleet.strip_id_prefix("session_01ABC") == "01ABC"
    assert fleet.strip_id_prefix("cse_01ABC") == "01ABC"
    assert fleet.strip_id_prefix("01ABC") == "01ABC"
    assert fleet.strip_id_prefix(None) == ""


def test_sids_are_distinct_for_ulid_shaped_ids():
    """Caught live: every server-side id in this listing is ULID-shaped and
    starts "01", and cs.short_sid() keeps a leading hex pair verbatim -- so
    four different machines all came back as card "01" and the firmware could
    not tell the cards apart. fleet_sid must hash instead."""
    ids = ["session_01ABCDEF", "session_01GHIJKL", "session_01MNOPQR", "cse_01STUVWX"]
    sids = [fleet.fleet_sid(i) for i in ids]
    assert len(set(sids)) == len(ids), sids
    assert all(len(s) == 2 for s in sids)
    # and stable across polls
    assert fleet.fleet_sid(ids[0]) == sids[0]


def test_wire_rows_from_one_listing_have_distinct_sids():
    rows = [api_row(rid=f"session_01ROW{i}", title=f"box-{i}") for i in range(4)]
    out = fleet.select_rows(rows, attention_only=False)
    assert len({r[0] for r in out}) == 4


def test_local_sessions_are_excluded(tmp_path):
    """The machine the owner is sitting at must not fill the panel."""
    (tmp_path / "111.json").write_text(
        json.dumps({"pid": 111, "bridgeSessionId": "session_01LOCAL"}), encoding="utf-8")
    ids = fleet.local_bridge_ids(str(tmp_path))
    assert ids == {"01LOCAL"}

    rows = [api_row(rid="session_01LOCAL"), api_row(rid="session_01REMOTE", title="other-box")]
    out = fleet.select_rows(rows, exclude_ids=ids, attention_only=False)
    assert len(out) == 1
    assert out[0][1] == "other-box"


def test_local_bridge_ids_survives_junk(tmp_path):
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "empty.json").write_text("[]", encoding="utf-8")
    (tmp_path / "ok.json").write_text(
        json.dumps({"bridgeSessionId": "cse_01OK"}), encoding="utf-8")
    assert fleet.local_bridge_ids(str(tmp_path)) == {"01OK"}


def test_local_bridge_ids_missing_dir_is_empty():
    assert fleet.local_bridge_ids("/nonexistent/sessions/dir") == set()


# --------------------------------------------------------------------------- filtering

def test_only_bridge_rows_survive():
    rows = [
        api_row(rid="session_1", kind="bridge", title="keep"),
        api_row(rid="session_2", kind="anthropic_cloud", title="drop-cloud"),
        api_row(rid="session_3", kind="byoc", title="drop-byoc"),
    ]
    out = fleet.select_rows(rows, attention_only=False)
    assert [r[1] for r in out] == ["keep"]


@pytest.mark.parametrize("status", ["archived", "failed"])
def test_dead_rows_are_dropped(status):
    assert fleet.select_rows([api_row(status=status)]) == []


def test_disconnected_rows_drop_by_default_and_can_be_asked_for():
    rows = [api_row(rid="session_1", title="asleep", conn="disconnected")]
    assert fleet.select_rows(rows, attention_only=False) == []
    assert len(fleet.select_rows(rows, show_offline=True,
                                 attention_only=False)) == 1


# --------------------------------------------------------------------------- state mapping

def test_requires_action_with_a_tool_is_a_permission_wait():
    row = api_row(worker="requires_action", tool="Bash")
    assert fleet.row_state(row) == cs.STATE_WAITING_PERMISSION
    wire = fleet.to_wire_row(row)
    assert wire[2] == cs.STATE_WAITING_PERMISSION
    assert wire[6] == cs.TOOL_CODES["Bash"]   # drives the device's "allow Bash?"


def test_requires_action_without_a_tool_is_an_input_wait():
    assert fleet.row_state(api_row(worker="requires_action")) == cs.STATE_WAITING_INPUT


def test_running_maps_by_whether_a_tool_is_known():
    assert fleet.row_state(api_row(worker="running", tool="Edit")) == cs.STATE_RUNNING_TOOL
    assert fleet.row_state(api_row(worker="running")) == cs.STATE_THINKING


def test_idle_and_unknown_states():
    assert fleet.row_state(api_row(worker="idle")) == cs.STATE_IDLE
    assert fleet.row_state(api_row(worker="something-new")) == cs.STATE_STARTING


def test_waiting_rows_sort_above_working_and_idle():
    rows = [
        api_row(rid="session_1", title="idle-one", worker="idle"),
        api_row(rid="session_2", title="busy-one", worker="running"),
        api_row(rid="session_3", title="needs-you", worker="requires_action", tool="Bash"),
    ]
    assert [r[1] for r in fleet.select_rows(rows, attention_only=False)] == [
        "needs-you", "busy-one", "idle-one"]


# --------------------------------------------------------------------------- row shape

def test_unknown_fields_are_unknown_not_zero():
    """The device hides a bar it has no number for; zeroes would draw a lie."""
    wire = fleet.to_wire_row(api_row())
    assert len(wire) == 13
    assert wire[3] == -1     # ctx
    assert wire[11] == -1    # tok
    assert wire[12] == cs.REMOTE_ON


def test_label_falls_back_to_repo_then_to_remote():
    no_title = {"id": "session_1", "environment_kind": "bridge",
                "config": {"sources": [{"url": "https://github.com/acme/widgets"}]}}
    assert fleet.to_wire_row(no_title)[1] == "widgets"
    assert fleet.to_wire_row({"id": "session_1"})[1] == "remote"


def test_elapsed_comes_from_the_last_event():
    row = api_row(last="2026-09-08T12:00:00Z")
    now = fleet._epoch("2026-09-08T12:01:40Z")
    assert fleet.to_wire_row(row, now=now)[4] == 100


# --------------------------------------------------------------------------- fetching

def test_pagination_follows_cursors():
    pages = [
        {"data": [api_row(rid="session_1")], "next_cursor": "c1"},
        {"data": [api_row(rid="session_2")], "next_cursor": None},
    ]
    op = opener_for(pages)
    rows = fleet.fetch_sessions("tok", op)
    assert len(rows) == 2
    assert "cursor=c1" in op.calls["urls"][1]


def test_pagination_stops_at_the_page_cap():
    endless = [{"data": [api_row()], "next_cursor": "more"}]
    op = opener_for(endless)
    fleet.fetch_sessions("tok", op)
    assert op.calls["n"] <= fleet.MAX_PAGES


# --------------------------------------------------------------------------- failure is a normal state

def _raiser(exc):
    def _open(req, timeout=None):
        raise exc
    return _open


@pytest.mark.parametrize("exc", [
    urllib.error.HTTPError("u", 401, "unauthorized", None, None),
    urllib.error.HTTPError("u", 403, "forbidden", None, None),
    urllib.error.HTTPError("u", 404, "gone", None, None),
    urllib.error.URLError("no route to host"),
    OSError("connection reset"),
])
def test_transport_failures_return_none_not_an_empty_panel(exc):
    """None means 'could not tell'. An empty list would blank a live panel."""
    assert fleet.fetch_sessions("tok", _raiser(exc)) is None


@pytest.mark.parametrize("body", [
    {"sessions": []},          # renamed key
    {"data": "not-a-list"},    # retyped
    ["not", "an", "object"],   # reshaped entirely
])
def test_shape_changes_return_none(body):
    assert fleet.fetch_sessions("tok", opener_for([body])) is None


def test_poll_once_without_a_token_is_quiet(monkeypatch):
    monkeypatch.setattr(fleet, "read_token", lambda path=None: None)
    assert fleet.poll_once(180) is None


def test_poll_once_keeps_quiet_when_the_listing_fails(monkeypatch):
    monkeypatch.setattr(fleet, "read_token", lambda path=None: "tok")
    monkeypatch.setattr(fleet, "fetch_sessions", lambda *a, **k: None)
    assert fleet.poll_once(180) is None


# --------------------------------------------------------------------------- credentials

def test_read_token_wants_the_oauth_credential(tmp_path):
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "secret"}}), encoding="utf-8")
    assert fleet.read_token(str(p)) == "secret"


@pytest.mark.parametrize("blob", [
    {},                                  # empty
    {"claudeAiOauth": {}},               # no token
    {"claudeAiOauth": "not-an-object"},  # wrong type
    {"apiKey": "sk-ant-..."},            # an API key will not work on this endpoint
])
def test_read_token_rejects_everything_else(tmp_path, blob):
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps(blob), encoding="utf-8")
    assert fleet.read_token(str(p)) is None


def test_read_token_missing_file(tmp_path):
    assert fleet.read_token(str(tmp_path / "nope.json")) is None


# --------------------------------------------------------------------------- payload

def test_payload_is_the_wire_format_the_firmware_parses():
    rows = [api_row(rid="session_1", title="other-box",
                    worker="requires_action", tool="Bash")]
    payload = fleet.build_payload(rows, 180)
    doc = json.loads(payload)
    assert list(doc.keys()) == ["ss"]
    assert doc["ss"][0][2] == cs.STATE_WAITING_PERMISSION


def test_payload_respects_the_byte_budget():
    # Rows that SURVIVE the attention filter, or this measures an empty payload.
    rows = [api_row(rid=f"session_{i}", title=f"machine-number-{i}",
                    worker="requires_action", tool="Bash") for i in range(8)]
    payload = fleet.build_payload(rows, 120)
    assert len(payload.encode("utf-8")) <= 120
    assert json.loads(payload)["ss"], "and it must not fit by dropping everything"


# ===========================================================================
# Attention, not a roster
# ===========================================================================
# The tab used to list every reachable Remote Control session. On the day this
# was written that was nine rows, seven of them idle, two of those idle for
# FIVE and SEVEN DAYS -- a roster, not information, crowding out the one row
# that would have mattered. select_rows() now keeps only what needs a person.


def test_only_rows_that_need_a_human_survive_by_default():
    rows = [
        api_row(rid="session_1", title="idle-for-a-week", worker="idle"),
        api_row(rid="session_2", title="busy", worker="running", tool="Bash"),
        api_row(rid="session_3", title="thinking", worker="running"),
        api_row(rid="session_4", title="needs-you", worker="requires_action", tool="Bash"),
    ]
    assert [r[1] for r in fleet.select_rows(rows)] == ["needs-you"]


def test_both_waiting_flavours_survive_the_filter():
    """`requires_action` with and without a named tool: permission and input."""
    rows = [
        api_row(rid="session_1", title="perm", worker="requires_action", tool="Bash"),
        api_row(rid="session_2", title="input", worker="requires_action"),
    ]
    out = fleet.select_rows(rows)
    assert sorted(r[2] for r in out) == [cs.STATE_WAITING_PERMISSION,
                                         cs.STATE_WAITING_INPUT]


@pytest.mark.parametrize("state", sorted(cs.WAITING_STATES))
def test_needs_a_human_is_exactly_the_waiting_bucket(state):
    assert fleet.needs_a_human(state) is True
    assert cs.state_bucket(state) == 0


@pytest.mark.parametrize("state", [cs.STATE_IDLE, cs.STATE_STARTING,
                                   cs.STATE_THINKING, cs.STATE_RUNNING_TOOL,
                                   cs.STATE_RESPONDING, cs.STATE_COMPACTING])
def test_nothing_else_needs_a_human(state):
    assert fleet.needs_a_human(state) is False


def test_full_roster_is_still_available_on_request():
    rows = [api_row(rid=f"session_{i}", title=f"box-{i}", worker="idle")
            for i in range(3)]
    assert fleet.select_rows(rows) == []
    assert len(fleet.select_rows(rows, attention_only=False)) == 3


def test_the_real_nine_row_payload_collapses_to_nothing():
    """The exact shape the owner's device was showing: nine bridge sessions,
    every one of them idle, two idle for the best part of a week."""
    ages = [23, 111, 213, 1502, 5830, 46813, 52398, 502932, 606983]
    rows = [api_row(rid=f"session_01ROW{i}", title=f"ACRFT-{i}", worker="idle",
                    last=1_000_000 - age)
            for i, age in enumerate(ages)]
    assert len(json.loads(fleet.build_payload(rows, 500,
                                              attention_only=False))["ss"]) == 9
    assert fleet.build_payload(rows, 500) == '{"ss":[]}'


def test_the_filter_runs_after_the_structural_ones():
    """A waiting row that is archived, offline or this machine's still goes."""
    ours = api_row(rid="session_01MINE", worker="requires_action", tool="Bash")
    dead = api_row(rid="session_2", worker="requires_action", status="archived")
    gone = api_row(rid="session_3", worker="requires_action", conn="disconnected")
    cloud = api_row(rid="session_4", worker="requires_action", kind="anthropic_cloud")
    out = fleet.select_rows([ours, dead, gone, cloud], exclude_ids={"01MINE"})
    assert out == []


def test_attention_only_reads_the_config(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("fleet = on\n", encoding="utf-8")
    assert fleet.attention_only_from_config(str(cfg)) is True
    cfg.write_text("fleet_attention_only = off\n", encoding="utf-8")
    assert fleet.attention_only_from_config(str(cfg)) is False
    cfg.write_text("fleet_attention_only = on\n", encoding="utf-8")
    assert fleet.attention_only_from_config(str(cfg)) is True


def test_inbox_rows_are_never_filtered(monkeypatch):
    """Messages and reports come from local disk and are attention-shaped by
    construction; the roster filter must not touch them."""
    msg = ["79", "PEER", 11, -1, 5, 0, 0, 0, 0, 0, 0, -1, -1, "hello"]
    working = ["mp", "AGENT", 12, -1, 5, 0, 0, 0, 0, 0, 0, -1, -1, "building"]
    idle = api_row(rid="session_1", title="quiet", worker="idle")
    out = json.loads(fleet.build_payload([idle], 500, inbox_rows=[msg, working]))["ss"]
    assert [r[2] for r in out] == [11, 12]


# ===========================================================================
# Staleness -- the nine-hour lie
# ===========================================================================
# The poller writes only on change, so silence is ambiguous between "the fleet
# is quiet" and "the listing has been 401ing since last night". These tests pin
# both halves of the contract: quiet inside the grace window, unmistakable
# after it.

HOUR = 3600.0


def test_a_single_failure_says_nothing():
    h = fleet.ListingHealth(now=0.0)
    h.fail(fleet.REASON_AUTH, now=0.0)
    assert h.stale_row(now=30.0) is None


def test_a_transient_401_never_reaches_the_device():
    """The case that must NOT shout. The token lives ~5 h and Claude Code
    refreshes the credential file itself; the poller re-reads it every 30 s, so
    a 401 normally heals in a poll or two. Nothing may reach the panel."""
    h = fleet.ListingHealth(now=0.0)
    now = 0.0
    for _ in range(6):                       # three minutes of 401s
        h.fail(fleet.REASON_AUTH, now=now)
        assert h.stale_row(now=now) is None
        now += 30.0
    h.ok(now=now)                            # the refresh landed
    assert h.stale_row(now=now) is None
    assert h.failures == 0
    # ...and the clock restarted from that success, so the next blip gets a
    # fresh window. The clock is anchored to the last SUCCESS rather than to
    # the first failure of a streak, deliberately: the number on the card is
    # "how old is this data", which is a fact about the last good listing.
    h.fail(fleet.REASON_AUTH, now=now + 30.0)
    assert h.stale_row(now=now + 600.0) is None
    assert h.stale_row(now=now + fleet.STALE_AFTER_S) is not None


def test_a_401_that_persists_does_shout():
    h = fleet.ListingHealth(now=0.0)
    now = 0.0
    while now < fleet.STALE_AFTER_S:
        h.fail(fleet.REASON_AUTH, now=now)
        now += 30.0
    row = h.stale_row(now=fleet.STALE_AFTER_S)
    assert row is not None
    assert row[13] == fleet.REASON_AUTH


def test_the_threshold_is_thirty_polls():
    """Sanity-check the number against the poll interval it is chosen for."""
    assert fleet.STALE_AFTER_S == 30 * fleet.POLL_INTERVAL_S


def test_a_poller_that_never_succeeded_still_shouts():
    """A cold start with a bad credential is exactly as blind as an outage."""
    h = fleet.ListingHealth(now=0.0)
    h.fail(fleet.REASON_NO_TOKEN, now=0.0)
    assert h.stale_row(now=fleet.STALE_AFTER_S - 1) is None
    assert h.stale_row(now=fleet.STALE_AFTER_S) is not None


def test_the_age_is_the_age_of_the_last_good_listing():
    h = fleet.ListingHealth(now=0.0)
    h.ok(now=100.0)
    h.fail(fleet.REASON_UNREACHABLE, now=200.0)
    row = h.stale_row(now=100.0 + 9 * HOUR)
    assert row[4] == int(9 * HOUR)


def test_a_recovery_retracts_the_marker():
    h = fleet.ListingHealth(now=0.0)
    h.fail(fleet.REASON_AUTH, now=0.0)
    assert h.stale_row(now=HOUR) is not None
    h.ok(now=HOUR)
    assert h.stale_row(now=HOUR) is None


@pytest.mark.parametrize("code,reason", [
    (401, fleet.REASON_AUTH),
    (403, fleet.REASON_FORBIDDEN),
    (404, fleet.REASON_SHAPE),
    (410, fleet.REASON_SHAPE),
])
def test_http_status_becomes_words_the_owner_can_act_on(code, reason):
    assert fleet.http_reason(code) == reason


def test_an_unknown_status_still_says_something_true():
    assert "503" in fleet.http_reason(503)


def test_every_fetch_failure_is_recorded(monkeypatch):
    """A failure the health object never hears about is a failure the device
    never hears about -- which was the whole bug."""
    h = fleet.ListingHealth(now=0.0)
    opener = _raiser(urllib.error.HTTPError("u", 401, "no", None, None))
    assert fleet.fetch_sessions("tok", opener, h) is None
    assert h.reason == fleet.REASON_AUTH
    assert h.failures == 1


def test_a_successful_fetch_clears_the_streak():
    h = fleet.ListingHealth(now=0.0)
    h.fail(fleet.REASON_AUTH, now=0.0)
    fleet.fetch_sessions("tok", opener_for([{"data": []}]), h)
    assert h.reason is None and h.failures == 0


# --------------------------------------------------------------------- marker

def test_the_marker_is_the_wire_row_the_firmware_parses():
    h = fleet.ListingHealth(now=0.0)
    h.fail(fleet.REASON_AUTH, now=0.0)
    row = h.stale_row(now=HOUR)
    assert len(row) == 14                       # same 14 fields a message has
    assert row[0] == fleet.STALE_SID
    assert row[1] == fleet.STALE_LABEL
    assert row[2] == fleet.STATE_HOST_STALE == 17
    assert row[3] == -1 and row[11] == -1       # ctx / tok: not a session
    assert row[12] == cs.REMOTE_UNKNOWN
    assert row[13]                              # the words


def test_the_marker_sid_collides_with_nothing():
    """Session and message sids are two hex characters; report sids are
    [g-y][0-9a-z]; the overflow marker owns 'zz'."""
    sid = fleet.STALE_SID
    assert sid != inbox.MORE_SID
    assert sid[0] not in "0123456789abcdef"
    assert sid[0] not in inbox._SID_HEAD


def test_the_marker_replaces_the_listing_rows_it_can_no_longer_vouch_for():
    """Keeping a nine-hour-old 'allow Bash?' would be worse than nothing: it
    is terra-cotta, it pulses, it sorts first and it can pull the panel to the
    tab, all to send the owner to a machine where nobody is waiting."""
    live = [api_row(rid="session_1", title="needs-you",
                    worker="requires_action", tool="Bash")]
    h = fleet.ListingHealth(now=0.0)
    h.fail(fleet.REASON_AUTH, now=0.0)
    stale = h.stale_row(now=9 * HOUR)
    out = json.loads(fleet.build_payload(live, 500, stale=stale))["ss"]
    assert [r[2] for r in out] == [fleet.STATE_HOST_STALE]


def test_messages_keep_flowing_while_the_listing_is_blind():
    """The marker means the LISTING is dark, not that everything is: mail is
    read from local disk and is still true."""
    msg = ["79", "PEER", 11, -1, 5, 0, 0, 0, 0, 0, 0, -1, -1, "hello"]
    h = fleet.ListingHealth(now=0.0)
    h.fail(fleet.REASON_AUTH, now=0.0)
    out = json.loads(fleet.build_payload([], 500, inbox_rows=[msg],
                                         stale=h.stale_row(now=HOUR)))["ss"]
    assert [r[2] for r in out] == [11, fleet.STATE_HOST_STALE]


def test_the_marker_survives_a_full_report_round():
    """Both caps eat the tail -- the byte budget and the device's six-row
    parser -- so the marker's room is reserved before the round is fitted, not
    left to compete with it."""
    class FakeWatcher:
        def poll(self):
            pass

        def rows(self, now=None, text_max=None, budget=None,
                 max_rows=inbox.DEVICE_MAX_ROWS):
            rows = [[inbox.report_sid(f"AGENT-{i}"), f"WORKER-ALPHA-{i}",
                     inbox.STATE_REPORT_NEEDS_YOU, -1, i, 0, 0, 0, 0, 0, 0,
                     -1, -1, "x" * inbox.report_text_max(budget)]
                    for i in range(10)]
            return inbox.fit_round(rows, budget, max_rows)

    h = fleet.ListingHealth(now=0.0)
    h.fail(fleet.REASON_AUTH, now=0.0)
    stale = h.stale_row(now=HOUR)
    rows = fleet.inbox_rows_for(FakeWatcher(), 0.0, 500, stale)
    payload = fleet.build_payload([], 500, inbox_rows=rows, stale=stale)
    out = json.loads(payload)["ss"]
    assert len(out) <= 6, "the firmware parses six rows and drops the rest"
    assert out[-1][2] == fleet.STATE_HOST_STALE
    assert len(payload.encode("utf-8")) <= 500


def test_row_cost_counts_the_comma():
    row = ["z0", "X", 17, -1, 0, 0, 0, 0, 0, 0, 0, -1, -1, "why"]
    two = len(cs.encode_payload([row, row]).encode("utf-8"))
    one = len(cs.encode_payload([row]).encode("utf-8"))
    assert fleet.row_cost(row) == two - one


# ------------------------------------------------------------------ the loop

class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _run(monkeypatch, tmp_path, fetch, iterations, token="tok", **kw):
    writes = []
    monkeypatch.setattr(fleet, "read_token", lambda path=None: token)
    monkeypatch.setattr(fleet, "fetch_sessions", fetch)
    monkeypatch.setattr(fleet, "local_bridge_ids", lambda *a, **k: set())
    monkeypatch.setattr(cs, "write_sessions_file",
                        lambda p, payload: writes.append(payload))
    clock = _Clock()
    fleet.run_loop(500, watcher=None, tick_s=2, poll_interval_s=30,
                   sessions_file=str(tmp_path / "s.json"),
                   iterations=iterations, sleep_fn=clock.sleep,
                   now_fn=clock.now, heartbeat=str(tmp_path / "beat"), **kw)
    return writes


def _waiting(n=1):
    return [api_row(rid=f"session_{i}", title=f"needs-you-{i}",
                    worker="requires_action", tool="Bash") for i in range(n)]


def test_the_loop_holds_the_last_good_rows_through_a_short_outage(tmp_path,
                                                                  monkeypatch):
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _waiting()
        k.get("health").fail(fleet.REASON_AUTH)
        return None

    # 10 ticks x 2 s = 20 s of failures: well inside the grace window.
    writes = _run(monkeypatch, tmp_path, flaky, iterations=10)
    assert writes, "the last good listing should keep being published"
    last = json.loads(writes[-1])["ss"]
    assert last[0][1] == "needs-you-0"
    assert all(r[2] != fleet.STATE_HOST_STALE for r in last)


def test_the_loop_marks_the_panel_stale_once_the_outage_is_real(tmp_path,
                                                                monkeypatch):
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _waiting()
        k.get("health").fail(fleet.REASON_AUTH)
        return None

    # 600 ticks x 2 s = 1200 s, past the 900 s threshold.
    writes = _run(monkeypatch, tmp_path, flaky, iterations=600)
    last = json.loads(writes[-1])["ss"]
    assert [r[2] for r in last] == [fleet.STATE_HOST_STALE]
    assert last[0][13] == fleet.REASON_AUTH
    assert last[0][4] >= fleet.STALE_AFTER_S


def test_the_loop_takes_the_marker_back_when_the_listing_returns(tmp_path,
                                                                 monkeypatch):
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1 or calls["n"] > 40:
            h = k.get("health")
            if h is not None:
                h.ok()
            return _waiting()
        k.get("health").fail(fleet.REASON_AUTH)
        return None

    writes = _run(monkeypatch, tmp_path, flaky, iterations=700)
    stale_seen = any(fleet.STATE_HOST_STALE in [r[2] for r in json.loads(w)["ss"]]
                     for w in writes)
    assert stale_seen, "the marker should have appeared while it was down"
    last = json.loads(writes[-1])["ss"]
    assert [r[2] for r in last] == [cs.STATE_WAITING_PERMISSION]


def test_a_missing_credential_is_a_listing_failure_like_any_other(tmp_path,
                                                                  monkeypatch):
    """It used to be one log line and nothing else -- a poller with no token
    published whatever it had and then went quiet forever."""
    writes = _run(monkeypatch, tmp_path, lambda *a, **k: None, iterations=600,
                  token=None)
    assert writes
    last = json.loads(writes[-1])["ss"]
    assert [r[2] for r in last] == [fleet.STATE_HOST_STALE]
    assert last[0][13] == fleet.REASON_NO_TOKEN


# ===========================================================================
# Heartbeat -- so something other than this process can notice it died
# ===========================================================================

def test_the_heartbeat_round_trips(tmp_path):
    path = str(tmp_path / "beat")
    h = fleet.ListingHealth(now=0.0)
    h.ok(now=1234.0)
    fleet.write_heartbeat(path, now=1300.0, health=h)
    doc = fleet.read_heartbeat(path)
    assert doc["ts"] == 1300.0
    assert doc["last_ok"] == 1234.0
    assert doc["pid"] == os.getpid()


def test_liveness_is_a_window_not_a_flag(tmp_path):
    path = str(tmp_path / "beat")
    fleet.write_heartbeat(path, now=1000.0)
    assert fleet.is_alive(path, now=1000.0 + fleet.HEARTBEAT_STALE_S - 1)
    assert not fleet.is_alive(path, now=1000.0 + fleet.HEARTBEAT_STALE_S + 1)


def test_liveness_window_is_four_missed_polls():
    assert fleet.HEARTBEAT_STALE_S == 4 * fleet.POLL_INTERVAL_S


def test_a_missing_heartbeat_is_not_alive(tmp_path):
    assert fleet.heartbeat_age(str(tmp_path / "nope")) is None
    assert fleet.is_alive(str(tmp_path / "nope")) is False


def test_a_torn_heartbeat_falls_back_to_the_mtime(tmp_path):
    """A read caught mid-replace must not read as death -- a spurious restart
    is a second producer racing the first over the handoff file."""
    path = tmp_path / "beat"
    path.write_text("{half-writ", encoding="utf-8")
    doc = fleet.read_heartbeat(str(path))
    assert doc is not None and isinstance(doc["ts"], float)
    assert fleet.is_alive(str(path)) is True


def test_the_heartbeat_survives_an_unwritable_directory(tmp_path):
    """Best-effort: a supervisor's convenience must never be able to take down
    the thing it is supervising."""
    blocked = tmp_path / "file" / "beat"
    (tmp_path / "file").write_text("not a directory", encoding="utf-8")
    fleet.write_heartbeat(str(blocked), now=1.0)   # must not raise


def test_the_loop_stamps_the_heartbeat(tmp_path, monkeypatch):
    path = tmp_path / "beat"
    _run(monkeypatch, tmp_path, lambda *a, **k: _waiting(), iterations=4)
    assert path.exists()
    assert fleet.read_heartbeat(str(path))["pid"] == os.getpid()
