"""Fleet poller: the sessions running on OTHER machines.

No network here. Every fetch is fed a fake opener, because the endpoint under
test is an undocumented internal one and the point of these tests is that the
mapping and the failure handling are right, not that Anthropic is up.
"""
import json
import urllib.error

import pytest

from daemon import clawdmeter_fleet as fleet
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


def test_local_sessions_are_excluded(tmp_path):
    """The machine the owner is sitting at must not fill the panel."""
    (tmp_path / "111.json").write_text(
        json.dumps({"pid": 111, "bridgeSessionId": "session_01LOCAL"}), encoding="utf-8")
    ids = fleet.local_bridge_ids(str(tmp_path))
    assert ids == {"01LOCAL"}

    rows = [api_row(rid="session_01LOCAL"), api_row(rid="session_01REMOTE", title="other-box")]
    out = fleet.select_rows(rows, exclude_ids=ids)
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
    out = fleet.select_rows(rows)
    assert [r[1] for r in out] == ["keep"]


@pytest.mark.parametrize("status", ["archived", "failed"])
def test_dead_rows_are_dropped(status):
    assert fleet.select_rows([api_row(status=status)]) == []


def test_disconnected_rows_drop_by_default_and_can_be_asked_for():
    rows = [api_row(rid="session_1", title="asleep", conn="disconnected")]
    assert fleet.select_rows(rows) == []
    assert len(fleet.select_rows(rows, show_offline=True)) == 1


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
    assert [r[1] for r in fleet.select_rows(rows)] == ["needs-you", "busy-one", "idle-one"]


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
    rows = [api_row(rid=f"session_{i}", title=f"machine-number-{i}") for i in range(8)]
    payload = fleet.build_payload(rows, 120)
    assert len(payload.encode("utf-8")) <= 120
