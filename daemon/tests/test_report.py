"""The report dispatcher: who gets asked, what they are asked, and the brakes.

Nothing here spawns a process or reads the network. The listing is a literal
list of dicts and the spawn is a fake `subprocess.run`, because what is under
test is the SELECTION and the REFUSALS -- the parts that decide whether the
owner's quota gets spent and whether the answers have anywhere to land.

One autouse fixture below points the round record at a tmp path. The real one
lives beside the logs and is what the rate limit reads; a test that stamped it
would silence a real button press for five minutes.
"""
import json
import os
import subprocess

import pytest

from daemon import clawdmeter_report as report
from daemon import clawdmeter_inbox as inbox


# --------------------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    beat = tmp_path / "report_round.json"
    monkeypatch.setattr(report, "state_path", lambda base=None: str(base or beat))
    return beat


# --------------------------------------------------------------------------- helpers

def api_row(rid="cse_01AAA", title="ALPHA", kind="bridge", worker="idle",
            conn="connected", status="active", last="2026-09-09T06:00:00Z",
            inbound="available"):
    row = {
        "id": rid,
        "title": title,
        "environment_kind": kind,
        "worker_status": worker,
        "connection_status": conn,
        "status": status,
        "last_event_at": last,
    }
    if inbound is not None:
        row["external_metadata"] = {"cross_session_inbound": inbound}
    return row


MAILDROP = report.DEFAULT_MAILDROP_NAME       # "clawdmeter-inbox"
HOME_BRIDGE = "01HOME"


def roster(name=MAILDROP, pid=1234, kind="bg", started=1000,
           bridge="session_" + HOME_BRIDGE):
    rec = {"name": name, "pid": pid, "kind": kind, "startedAt": started,
           "sessionId": f"sid-{name}", "procStart": "1"}
    if bridge:
        rec["bridgeSessionId"] = bridge
    return rec


def home_row(title=MAILDROP, **kw):
    """The mail drop as the FLEET sees it: a bridge row of its own.

    A local session is only usable as a reply address if it is in this listing,
    because the listing's title is the only name a remote agent can resolve.
    """
    return api_row("cse_" + HOME_BRIDGE, title, **kw)


NOW = 1788950000.0


class FakeProc(object):
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def runner_ok(payload=None):
    """A subprocess.run stand-in that succeeds and records the argv it saw."""
    calls = []

    def _run(argv, **kw):
        calls.append({"argv": argv, "kw": kw})
        body = payload if payload is not None else {
            "type": "result", "is_error": False, "result": "sent 2/2",
        }
        return FakeProc(0, json.dumps(body))

    _run.calls = calls
    return _run


# --------------------------------------------------------------------------- the contract

def test_request_template_is_report_md_verbatim():
    """The doc and the dispatcher are one artefact in two files.

    If this fails, somebody edited the wording in one place. The parser on the
    other side is strict, so the request text IS the contract -- fix both.
    """
    from_md = report.request_text_from_markdown()
    assert from_md is not None, "REPORT.md's request blockquote could not be found"
    assert from_md == report.REQUEST_TEMPLATE


def test_request_names_the_reply_address():
    text = report.build_request_text("home-1")
    assert "home-1" in text
    assert report.REPLY_TO_PLACEHOLDER not in text
    # The line that used to send the reply back to the sender is gone.
    assert "Reply to the session that sent this" not in text


def test_request_never_opens_a_line_with_the_marker():
    """The request necessarily contains the marker -- it is telling the agent
    what to emit -- and it lands in the receiving transcript like any other
    message. A line that STARTED with it would draw a bogus report card on the
    panel for every agent in the round."""
    text = report.build_request_text("home-1")
    assert inbox.REPORT_MARKER in text
    for line in text.splitlines():
        assert not line.lstrip(" \t`").startswith(inbox.REPORT_MARKER)
    # ...and the request itself must not parse as a report.
    assert inbox.parse_report(text) is None


def test_build_request_text_rejects_a_template_that_would_self_report(monkeypatch):
    monkeypatch.setattr(report, "REQUEST_TEMPLATE",
                        "Clawdmeter status check.\n"
                        "CLAWDMETER-REPORT/1 WORKING: like this\n")
    with pytest.raises(ValueError):
        report.build_request_text("home-1")


def test_prompt_carries_the_contract_and_the_targets():
    targets, _ = report.select_targets(
        [api_row("cse_1", "ALPHA"), api_row("cse_2", "BETA")], now=NOW)
    prompt = report.build_prompt(targets, "home-1")
    assert report.build_request_text("home-1") in prompt   # verbatim, not paraphrased
    assert "- ALPHA" in prompt and "- BETA" in prompt
    assert "nobody else" in prompt
    for line in prompt.splitlines():
        assert not line.lstrip(" \t`").startswith(inbox.REPORT_MARKER)


# --------------------------------------------------------------------------- target selection

def test_selects_only_live_bridge_rows_that_are_not_ours():
    rows = [
        api_row("cse_keep", "KEEP"),
        api_row("cse_cloud", "CLOUD", kind="anthropic_cloud"),
        api_row("cse_arch", "ARCHIVED", status="archived"),
        api_row("cse_fail", "FAILED", status="failed"),
        api_row("cse_off", "OFFLINE", conn="disconnected"),
        api_row("cse_mine", "MINE"),
        api_row("cse_deaf", "DEAF", inbound="unavailable"),
        api_row("cse_blank", "   "),
        api_row("cse_slash", "/compact leftovers"),
    ]
    # local_bridge_ids() strips the `cse_`/`session_` prefix, so the exclusion
    # set this is fed is prefixless -- feeding it the raw id excludes nothing.
    kept_all, _ = report.select_targets(rows, exclude_ids={"cse_mine"},
                                        max_targets=10, now=NOW)
    assert [t.name for t in kept_all] == ["KEEP", "MINE"]

    targets, dropped = report.select_targets(rows, exclude_ids={"mine"},
                                             max_targets=10, now=NOW)
    assert [t.name for t in targets] == ["KEEP"]
    why = {d["name"]: d["why"] for d in dropped}
    assert why["MINE"] == "this machine"
    assert "not a bridge" in why["CLOUD"]
    assert why["ARCHIVED"] == "archived"
    assert why["FAILED"] == "failed"
    assert why["OFFLINE"] == "disconnected"
    assert "refuses inbound" in why["DEAF"]
    assert "no title" in why["(untitled)"]
    assert "not addressable" in why["/compact leftovers"]


def test_missing_inbound_field_is_reachable():
    """Absence of the field is absence of evidence: older clients predate it,
    and dropping them would silently shrink every round on a mixed fleet."""
    targets, _ = report.select_targets([api_row("cse_1", "OLD", inbound=None)],
                                       now=NOW)
    assert [t.name for t in targets] == ["OLD"]


def test_freshest_first_and_the_cap_drops_the_stalest():
    rows = [
        api_row("cse_old", "OLD", last="2026-09-01T00:00:00Z"),
        api_row("cse_new", "NEW", last="2026-09-09T06:00:00Z"),
        api_row("cse_mid", "MID", last="2026-09-05T00:00:00Z"),
    ]
    targets, dropped = report.select_targets(rows, max_targets=2, now=NOW)
    assert [t.name for t in targets] == ["NEW", "MID"]
    assert {"name": "OLD", "id": "cse_old", "why": "over the 2-agent cap"} in dropped


def test_default_cap_is_five_and_matches_what_the_device_draws():
    assert report.DEFAULT_MAX_TARGETS == 5
    rows = [api_row(f"cse_{i}", f"A{i}", last=f"2026-09-0{i}T00:00:00Z")
            for i in range(1, 9)]
    targets, dropped = report.select_targets(rows, now=NOW)
    assert len(targets) == 5
    assert sum(1 for d in dropped if "cap" in d["why"]) == 3


def test_duplicate_titles_collapse_to_one_recipient():
    """SendMessage addresses peers BY NAME, so two live sessions sharing a
    title are one ambiguous recipient -- not two turns' worth of round."""
    rows = [
        api_row("cse_a", "ACRFT-D", last="2026-09-09T06:00:00Z"),
        api_row("cse_b", "ACRFT-D", last="2026-09-02T00:00:00Z"),
    ]
    targets, dropped = report.select_targets(rows, now=NOW)
    assert [t.name for t in targets] == ["ACRFT-D"]
    assert [t.row_id for t in targets] == ["cse_a"]      # the fresher one
    assert any("duplicate name" in d["why"] for d in dropped)


def test_selection_survives_junk_rows():
    targets, _ = report.select_targets([None, 42, "nope", api_row()], now=NOW)
    assert [t.name for t in targets] == ["ALPHA"]


# --------------------------------------------------------------------------- the reply address

def test_live_local_sessions_drops_dead_pids_and_unnamed_rows(monkeypatch):
    recs = {
        "a": roster("alive", pid=1),
        "b": roster("dead", pid=2),
        "c": {"pid": 3, "sessionId": "c"},          # no name: unaddressable
    }
    monkeypatch.setattr(report.cs, "load_roster", lambda dirs: (recs, True))
    monkeypatch.setattr(report.cs, "read_config_dirs", lambda: ["/nope"])
    out = report.live_local_sessions(alive=lambda pid, ps=None: int(pid) == 1)
    assert [r["name"] for r in out] == ["alive"]


def test_the_address_is_the_name_the_FLEET_sees_not_the_local_one():
    """The defect that made the first real round work by luck.

    A session has two names: the local roster's (what peers on THIS machine
    use) and the account listing's title (what remote agents resolve). Every
    target of a round is remote, so the address handed out must be the title.
    """
    sessions = [roster(name="clawdmeter-d0")]
    cands, dropped = report.reply_candidates(sessions, [home_row("CLAWDMETER")])
    assert dropped == []
    assert [c.name for c in cands] == ["CLAWDMETER"]
    assert [c.roster_name for c in cands] == ["clawdmeter-d0"]


def test_a_stale_local_name_cannot_leak_into_the_address(tmp_path):
    """Derived names are regenerated on every restart. Whatever the roster
    calls the session today, the address comes from the listing read in the
    same run."""
    run = runner_ok()
    rnd = report.dispatch([home_row("CLAWDMETER"), api_row("cse_1", "ALPHA")],
                          sessions=[roster(name="clawdmeter-9f")],
                          exclude_ids={HOME_BRIDGE}, runner=run, now=NOW,
                          reply_to="clawdmeter-9f",
                          state_file=str(tmp_path / "s.json"))
    assert rnd.ok
    assert rnd.reply_to == "CLAWDMETER"
    assert "CLAWDMETER" in run.calls[0]["argv"][2]
    assert "clawdmeter-9f" not in run.calls[0]["argv"][2]


def test_an_ambiguous_title_is_refused_not_chosen_between():
    """SendMessage resolves by name. Two live rows called the same thing is a
    coin toss over which machine a round lands on -- and every session started
    in this project's directory derives a name beginning `clawdmeter-`."""
    sessions = [roster(name="clawdmeter-d0")]
    rows = [home_row("CLAWDMETER"), api_row("cse_other", "CLAWDMETER")]
    cands, dropped = report.reply_candidates(sessions, rows)
    assert cands == []
    assert "ambiguous" in dropped[0]["why"]
    assert "2 live sessions" in dropped[0]["why"]


def test_case_alone_does_not_make_two_titles_distinct():
    sessions = [roster(name="clawdmeter-d0")]
    rows = [home_row("CLAWDMETER"), api_row("cse_other", "clawdmeter")]
    cands, _ = report.reply_candidates(sessions, rows)
    assert cands == []


def test_a_session_the_fleet_cannot_see_is_not_an_address():
    """Three ways to be invisible, and none of them may become a round."""
    rows = [home_row()]
    for rec, expect in (
        (roster(bridge=None), "no Remote Control bridge"),
        (roster(bridge="session_01NOPE"), "not in the listing"),
        (roster(), "shows disconnected"),
    ):
        listing = rows if expect != "shows disconnected" else [
            home_row(conn="disconnected")]
        cands, dropped = report.reply_candidates([rec], listing)
        if expect == "not in the listing":
            cands, dropped = report.reply_candidates([rec], rows)
        assert cands == [], expect
        assert expect in dropped[0]["why"], (expect, dropped)


def test_archived_rows_are_not_addresses_either():
    cands, dropped = report.reply_candidates(
        [roster()], [home_row(status="archived")])
    assert cands == []
    assert "not in the listing" in dropped[0]["why"]


def test_find_maildrop_wants_exactly_one():
    two = [roster(pid=1), roster(pid=2)]
    assert report.find_maildrop(MAILDROP, two)[0] is None
    assert "ambiguous" in report.find_maildrop(MAILDROP, two)[1]
    assert report.find_maildrop(MAILDROP, [roster()])[0]["pid"] == 1234
    assert report.find_maildrop("CLAWDMETER-INBOX", [roster()])[0] is not None
    assert report.find_maildrop("nobody", [roster()])[0] is None


def test_no_maildrop_refuses_and_says_the_one_command(tmp_path):
    """No fallback to a working session. A round delivered into somebody's
    editor is worse than a round not sent."""
    run = runner_ok()
    working = roster(name="clawdmeter-d0", kind="interactive")
    rnd = report.dispatch([home_row(), api_row()], sessions=[working],
                          exclude_ids={HOME_BRIDGE}, runner=run, now=NOW,
                          state_file=str(tmp_path / "s.json"))
    assert rnd.ok is False
    assert rnd.maildrop_action == "missing"
    assert MAILDROP in rnd.reason
    assert "claude --bg" in rnd.reason        # the refusal says how to fix it
    assert run.calls == []


def test_maildrop_is_never_started_unless_asked(tmp_path):
    starts = []

    def _run(argv, **kw):
        starts.append(argv)
        return FakeProc(0, "")

    rec, action, why = report.ensure_maildrop(MAILDROP, sessions=[], create=False,
                                              runner=_run)
    assert rec is None and action == "missing" and starts == []


def test_ensure_maildrop_starts_one_and_waits_for_the_roster():
    """`claude --bg` returns before the roster file lands, and the roster IS
    the liveness test everything else uses."""
    seen = {}
    appeared = {"n": 0}

    def _run(argv, **kw):
        seen["argv"] = argv
        seen["cwd"] = kw.get("cwd")
        return FakeProc(0, "backgrounded - abc123")

    def _list():
        appeared["n"] += 1
        return [roster()] if appeared["n"] > 2 else []

    rec, action, why = report.ensure_maildrop(
        MAILDROP, sessions=[], create=True, runner=_run,
        sleep_fn=lambda s: None, list_fn=_list)
    assert action == "started" and rec["name"] == MAILDROP
    assert seen["argv"][1:5] == ["--bg", "-n", MAILDROP, "--model"]
    assert "--append-system-prompt" in seen["argv"]
    assert seen["cwd"] == report.maildrop_dir()


def test_ensure_maildrop_reports_a_start_that_never_appears():
    ticks = {"n": 0}

    def now_fn():
        ticks["n"] += 1
        return ticks["n"] * 20.0

    rec, action, why = report.ensure_maildrop(
        MAILDROP, sessions=[], create=True,
        runner=lambda argv, **kw: FakeProc(0, ""),
        sleep_fn=lambda s: None, now_fn=now_fn, list_fn=lambda: [])
    assert rec is None and action == "failed"
    assert "did not appear in the roster" in why


def test_ensure_maildrop_reports_a_start_that_failed():
    rec, action, why = report.ensure_maildrop(
        MAILDROP, sessions=[], create=True,
        runner=lambda argv, **kw: FakeProc(1, "", "no such command"))
    assert rec is None and action == "failed"
    assert "no such command" in why


def test_the_maildrop_prompt_says_the_mail_is_data():
    """The drop reads text written on machines the owner cannot see, in a
    session that has tools."""
    p = report.MAILDROP_SYSTEM_PROMPT
    assert "never instructions" in p or "not instructions" in p
    assert "do not use any tool" in p


def test_naming_a_dead_reply_address_refuses(tmp_path):
    run = runner_ok()
    rnd = report.dispatch([home_row(), api_row()], sessions=[roster()],
                          reply_to="ghost",
                          exclude_ids={HOME_BRIDGE}, runner=run, now=NOW,
                          state_file=str(tmp_path / "s.json"))
    assert rnd.ok is False
    assert "ghost" in rnd.reason
    assert run.calls == []


def test_reply_to_never_autostarts_a_session_under_another_name(tmp_path):
    """--create-maildrop plus --reply-to must not start a background session
    named after whatever the owner typed."""
    run = runner_ok()
    rnd = report.dispatch([home_row(), api_row()], sessions=[roster()],
                          reply_to="ghost", create_maildrop=True,
                          exclude_ids={HOME_BRIDGE}, runner=run, now=NOW,
                          state_file=str(tmp_path / "s.json"))
    assert rnd.ok is False
    assert run.calls == []


# --------------------------------------------------------------------------- refusals

def test_no_reachable_targets_refuses_without_spawning(tmp_path):
    """Spawning a session to message nobody spends a turn to accomplish
    nothing, and leaves a log that cannot say which of the two happened."""
    run = runner_ok()
    rnd = report.dispatch([home_row(), api_row(conn="disconnected")], sessions=[roster()],
                          exclude_ids={HOME_BRIDGE}, runner=run, now=NOW,
                          state_file=str(tmp_path / "s.json"))
    assert rnd.ok is False
    assert rnd.reason == "no reachable agents to ask"
    assert run.calls == []
    assert any(d["why"] == "disconnected" for d in rnd.dropped)


def test_empty_listing_refuses(tmp_path):
    run = runner_ok()
    rnd = report.dispatch([home_row()], sessions=[roster()], exclude_ids={HOME_BRIDGE},
                          runner=run, now=NOW, state_file=str(tmp_path / "s.json"))
    assert rnd.ok is False and run.calls == []


# --------------------------------------------------------------------------- the rate limit

def test_rate_limit_blocks_a_second_round_and_lifts_on_time(tmp_path):
    state_file = str(tmp_path / "s.json")
    run = runner_ok()
    first = report.dispatch([home_row(), api_row()], sessions=[roster()], exclude_ids={HOME_BRIDGE},
                            runner=run, now=NOW, state_file=state_file)
    assert first.ok and len(run.calls) == 1

    second = report.dispatch([home_row(), api_row()], sessions=[roster()], exclude_ids={HOME_BRIDGE},
                             runner=run, now=NOW + 10, state_file=state_file)
    assert second.ok is False
    assert "rate limited" in second.reason
    assert len(run.calls) == 1, "the second round must not have spawned"

    later = report.dispatch([home_row(), api_row()], sessions=[roster()], exclude_ids={HOME_BRIDGE},
                            runner=run, now=NOW + report.DEFAULT_MIN_INTERVAL_S + 1,
                            state_file=state_file)
    assert later.ok and len(run.calls) == 2


def test_rate_limit_is_anchored_on_the_attempt_not_the_success(tmp_path):
    """A spawn that fails instantly, retried in a loop by a stuck button, would
    otherwise start a process per press for as long as the fault lasts."""
    state_file = str(tmp_path / "s.json")

    def boom(argv, **kw):
        raise OSError("no such file")

    first = report.dispatch([home_row(), api_row()], sessions=[roster()], exclude_ids={HOME_BRIDGE},
                            runner=boom, now=NOW, state_file=state_file)
    assert first.ok is False and first.spawn.error == "not-found"

    calls = runner_ok()
    again = report.dispatch([home_row(), api_row()], sessions=[roster()], exclude_ids={HOME_BRIDGE},
                            runner=calls, now=NOW + 5, state_file=state_file)
    assert again.ok is False and "rate limited" in again.reason
    assert calls.calls == []


def test_ignore_rate_limit_is_the_deliberate_override(tmp_path):
    state_file = str(tmp_path / "s.json")
    run = runner_ok()
    report.dispatch([home_row(), api_row()], sessions=[roster()], exclude_ids={HOME_BRIDGE},
                    runner=run, now=NOW, state_file=state_file)
    forced = report.dispatch([home_row(), api_row()], sessions=[roster()], exclude_ids={HOME_BRIDGE},
                             runner=run, now=NOW + 1, state_file=state_file,
                             ignore_rate_limit=True)
    assert forced.ok and len(run.calls) == 2


def test_rate_limit_remaining_is_zero_when_disabled_or_unstamped():
    assert report.rate_limit_remaining({}, NOW) == 0.0
    assert report.rate_limit_remaining({"dispatched_at": NOW}, NOW, 0) == 0.0
    assert report.rate_limit_remaining({"dispatched_at": "junk"}, NOW) == 0.0


# --------------------------------------------------------------------------- dry run

def test_dry_run_sends_nothing_and_does_not_touch_the_rate_limit(tmp_path):
    state_file = str(tmp_path / "s.json")
    run = runner_ok()
    dry = report.dispatch([home_row(), api_row("cse_1", "ALPHA")], sessions=[roster()],
                          exclude_ids={HOME_BRIDGE}, runner=run, now=NOW,
                          state_file=state_file, dry_run=True)
    assert dry.ok and dry.dry_run
    assert run.calls == []
    assert not (tmp_path / "s.json").exists(), "a dry run must not stamp the limit"
    assert "ALPHA" in dry.prompt
    assert report.build_request_text(MAILDROP) in dry.prompt

    # ...and a real round straight after it is still allowed.
    real = report.dispatch([home_row(), api_row("cse_1", "ALPHA")], sessions=[roster()],
                           exclude_ids={HOME_BRIDGE}, runner=run, now=NOW + 1,
                           state_file=state_file)
    assert real.ok and len(run.calls) == 1


def test_dry_run_reports_targets_and_drops(capsys, tmp_path):
    dry = report.dispatch([home_row(), api_row("cse_1", "ALPHA"),
                           api_row("cse_2", "GONE", conn="disconnected")],
                          sessions=[roster()], exclude_ids={HOME_BRIDGE},
                          now=NOW, state_file=str(tmp_path / "s.json"),
                          dry_run=True)
    report._print_dry_run(dry)
    out = capsys.readouterr().out
    assert "ALPHA" in out
    assert "GONE: disconnected" in out
    assert MAILDROP in out
    assert "Nothing was sent." in out


# --------------------------------------------------------------------------- the spawn

def test_spawn_argv_is_the_narrow_one():
    argv = report.build_argv("PROMPT", model="haiku", binary="claude")
    assert argv[:3] == ["claude", "-p", "PROMPT"]
    joined = " ".join(argv)
    assert "--model haiku" in joined
    assert "--output-format json" in joined
    assert "--tools ListAgents,SendMessage" in joined
    assert "--allowedTools ListAgents,SendMessage" in joined
    assert "--permission-prompts none" in joined
    assert "--no-session-persistence" in argv
    assert "--setting-sources user" in joined
    # Nothing that would widen it back out again.
    assert not any(a.startswith("--dangerously") for a in argv)
    assert "--add-dir" not in argv


def test_spawn_runs_outside_this_repo():
    """`claude -p` auto-discovers CLAUDE.md and .claude/ from its working
    directory. Run from the repo it would load the project's own instructions
    into a session whose only job is to copy a fixed string N times."""
    seen = {}

    def _run(argv, **kw):
        seen.update(kw)
        return FakeProc(0, "{}")

    report.spawn("p", runner=_run, binary="claude")
    cwd = seen["cwd"]
    repo = os.path.dirname(os.path.dirname(os.path.abspath(report.__file__)))
    assert os.path.basename(cwd).startswith("clawdmeter-report")
    assert os.path.commonpath([os.path.abspath(cwd), repo]) != repo
    assert not os.path.exists(cwd), "the throwaway cwd must be cleaned up"
    assert seen["capture_output"] is True
    assert seen["encoding"] == "utf-8"


def test_spawn_captures_a_failure_rather_than_losing_it():
    def _run(argv, **kw):
        return FakeProc(1, "", "Error: unknown model 'haiku'")

    res = report.spawn("p", runner=_run, binary="claude")
    assert res.ok is False and res.error == "exit"
    assert "unknown model" in res.stderr


def test_spawn_treats_an_is_error_result_as_a_failure():
    def _run(argv, **kw):
        return FakeProc(0, json.dumps({"is_error": True,
                                       "result": "permission denied"}))

    res = report.spawn("p", runner=_run, binary="claude")
    assert res.ok is False
    assert res.result["result"] == "permission denied"


def test_spawn_timeout_is_reported_as_a_timeout(tmp_path):
    def _run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout", 1),
                                        output="partial", stderr="")

    rnd = report.dispatch([home_row(), api_row()], sessions=[roster()], exclude_ids={HOME_BRIDGE},
                          runner=_run, now=NOW, timeout_s=7,
                          state_file=str(tmp_path / "s.json"))
    assert rnd.ok is False
    assert rnd.spawn.error == "timeout"
    assert "7s" in rnd.reason
    # The honest half: a killed dispatcher does not un-send what it sent.
    assert "already sent" in rnd.reason


def test_a_failed_spawn_still_records_the_round(tmp_path):
    state_file = str(tmp_path / "s.json")

    def _run(argv, **kw):
        return FakeProc(2, "", "boom")

    report.dispatch([home_row(), api_row("cse_1", "ALPHA")], sessions=[roster()],
                    exclude_ids={HOME_BRIDGE}, runner=_run, now=NOW,
                    state_file=state_file)
    doc = json.loads(open(state_file, encoding="utf-8").read())
    assert doc["targets"][0]["name"] == "ALPHA"
    assert doc["spawn"]["returncode"] == 2
    assert doc["spawn"]["stderr"] == "boom"
    assert doc["reply_to"] == MAILDROP


def test_the_prompt_that_is_spawned_is_the_prompt_that_was_built(tmp_path):
    run = runner_ok()
    rnd = report.dispatch([home_row(), api_row("cse_1", "ALPHA")], sessions=[roster()],
                          exclude_ids={HOME_BRIDGE}, runner=run, now=NOW,
                          state_file=str(tmp_path / "s.json"))
    argv = run.calls[0]["argv"]
    assert argv[1] == "-p"
    assert argv[2] == rnd.prompt
    assert MAILDROP in argv[2]


# --------------------------------------------------------------------------- watching the replies

class FakeWatcher(object):
    def __init__(self, batches):
        self.batches = list(batches)

    def poll(self):
        return self.batches.pop(0) if self.batches else []


def msg(mid, sender, body):
    rep = inbox.parse_report(body)
    return inbox.Message(mid, NOW, sender, body,
                         report_state=rep[0] if rep else None,
                         summary=rep[1] if rep else None)


def test_follow_collects_replies_and_dedupes_the_two_records():
    body = "CLAWDMETER-REPORT/1 WORKING: building the thing"
    watcher = FakeWatcher([[msg("m1", "ALPHA", body)],
                           [msg("m1", "ALPHA", body), msg("m2", "BETA", "hello")]])
    ticks = {"n": 0}

    def now_fn():
        ticks["n"] += 1
        return NOW + ticks["n"]

    got = report.follow(watcher, [], seconds=2, sleep_fn=lambda s: None,
                        now_fn=now_fn)
    assert len(got) == 2
    by_sender = {g["sender"]: g for g in got}
    assert by_sender["ALPHA"]["is_report"] is True
    assert by_sender["ALPHA"]["state"] == inbox.STATE_REPORT_WORKING
    assert by_sender["ALPHA"]["summary"] == "building the thing"
    assert by_sender["BETA"]["is_report"] is False
    assert by_sender["BETA"]["body"] == "hello"


def test_summarise_tells_the_three_failures_apart():
    targets, _ = report.select_targets(
        [api_row("cse_1", "ALPHA"), api_row("cse_2", "BETA"),
         api_row("cse_3", "GAMMA")], now=NOW)
    replies = [
        {"sender": "ALPHA", "is_report": True, "state": 12, "summary": "x",
         "body": "CLAWDMETER-REPORT/1 WORKING: x", "ts": NOW},
        {"sender": "BETA", "is_report": False, "state": None, "summary": None,
         "body": "I am currently working on the refactor.", "ts": NOW},
    ]
    text = report.summarise(None, replies, targets)
    assert "asked 3" in text
    assert "answered 2, 1 matched the contract, 1 did not" in text
    assert "silent: GAMMA" in text


def test_summarise_flags_a_replier_nobody_asked_for():
    targets, _ = report.select_targets([api_row("cse_1", "ALPHA")], now=NOW)
    replies = [{"sender": "alpha-laptop", "is_report": True, "state": 12,
                "summary": "x", "body": "b", "ts": NOW}]
    text = report.summarise(None, replies, targets)
    # The listing title and the peer's `from-name` are not guaranteed to match;
    # when they do not, the round looks silent unless this says otherwise.
    assert "name mapping" in text


def test_state_name_maps_the_four_and_passes_anything_else_through():
    assert report.state_name(inbox.STATE_REPORT_NEEDS_YOU) == "NEEDS-YOU"
    assert report.state_name(inbox.STATE_REPORT_BLOCKED) == "BLOCKED"
    assert report.state_name(inbox.STATE_REPORT_DONE) == "DONE"
    assert report.state_name(99) == "99"


# --------------------------------------------------------------------------- config

def test_config_reads_the_three_keys(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("report_max_targets = 3\n"
                   "report_min_interval_s = 60\n"
                   "report_model = sonnet\n", encoding="utf-8")
    assert report.max_targets_from_config(str(cfg)) == 3
    assert report.min_interval_from_config(str(cfg)) == 60
    assert report.model_from_config(str(cfg)) == "sonnet"


def test_config_defaults_when_unset_or_junk(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("report_max_targets = banana\n", encoding="utf-8")
    assert report.max_targets_from_config(str(cfg)) == report.DEFAULT_MAX_TARGETS
    assert report.min_interval_from_config(str(cfg)) == report.DEFAULT_MIN_INTERVAL_S
    assert report.model_from_config(str(cfg)) == report.DEFAULT_MODEL


def test_state_round_trips_and_a_junk_file_reads_as_empty(tmp_path):
    path = str(tmp_path / "s.json")
    assert report.write_state({"dispatched_at": NOW}, path)
    assert report.read_state(path)["dispatched_at"] == NOW
    open(path, "w", encoding="utf-8").write("{ not json")
    assert report.read_state(path) == {}


def test_force_utf8_stdio_survives_a_missing_console(monkeypatch):
    """Under pythonw (which is how the button will eventually reach this)
    sys.stdout is None. Reconfiguring it must not be the thing that kills the
    round."""
    monkeypatch.setattr(report.sys, "stdout", None)
    monkeypatch.setattr(report.sys, "stderr", None)
    report.force_utf8_stdio()          # must not raise


def test_skip_running_leaves_mid_turn_agents_alone():
    """Off by default -- a round asks everybody, and a WORKING answer in the
    agent's own words beats the listing's bare "running". On, for a round that
    is not meant to disturb anything in flight."""
    rows = [api_row("cse_busy", "BUSY", worker="running",
                    last="2026-09-09T06:00:00Z"),
            api_row("cse_calm", "CALM", worker="idle",
                    last="2026-09-08T06:00:00Z")]
    both, _ = report.select_targets(rows, now=NOW)
    assert [t.name for t in both] == ["BUSY", "CALM"]

    calm, dropped = report.select_targets(rows, now=NOW, skip_running=True)
    assert [t.name for t in calm] == ["CALM"]
    assert any(d["name"] == "BUSY" and "mid-turn" in d["why"] for d in dropped)


def test_skip_running_reaches_dispatch(tmp_path):
    run = runner_ok()
    rnd = report.dispatch([home_row(), api_row("cse_busy", "BUSY", worker="running"),
                           api_row("cse_calm", "CALM",
                                   last="2026-09-08T00:00:00Z")],
                          sessions=[roster()], exclude_ids={HOME_BRIDGE}, runner=run,
                          now=NOW, state_file=str(tmp_path / "s.json"),
                          skip_running=True)
    assert [t.name for t in rnd.targets] == ["CALM"]
    assert "BUSY" not in run.calls[0]["argv"][2]


def test_spawn_opens_the_peer_gate_and_keeps_the_login():
    """Without these two, a one-shot's ListAgents shows it no fleet at all and
    the round sends nothing while reporting success. Measured on 2.1.263."""
    seen = {}

    def _run(argv, **kw):
        seen.update(kw)
        return FakeProc(0, "{}")

    report.spawn("p", runner=_run, binary="claude",
                 env={"PATH": "/usr/bin", "HOME": "/home/x"})
    env = seen["env"]
    assert env["CLAUDE_CODE_HARBOR_KITE_CLOUD"] == "1"
    assert env["CLAUDE_CODE_REMOTE"] == "true"
    assert env["HOME"] == "/home/x", "the inherited environment carries the login"


def test_no_peer_env_leaves_the_environment_alone():
    seen = {}

    def _run(argv, **kw):
        seen.update(kw)
        return FakeProc(0, "{}")

    report.spawn("p", runner=_run, binary="claude", peer_env=False,
                 env={"PATH": "/usr/bin"})
    assert set(seen["env"]) == {"PATH"}


def test_peer_env_reaches_dispatch(tmp_path):
    run = runner_ok()
    report.dispatch([home_row(), api_row()], sessions=[roster()], exclude_ids={HOME_BRIDGE},
                    runner=run, now=NOW, state_file=str(tmp_path / "s.json"),
                    peer_env=False)
    assert "CLAUDE_CODE_REMOTE" not in run.calls[0]["kw"]["env"]


def test_prompt_tells_the_one_shot_to_retry_the_listing():
    """The bridge peer list is fetched under a 5 s deadline and the tool says
    so when it misses; a single ListAgents call would read that as an empty
    fleet and send nothing."""
    targets, _ = report.select_targets([api_row()], now=NOW)
    prompt = report.build_prompt(targets, "home-1")
    assert "ONE more time" in prompt


# --------------------------------------------------------------------------- language

def test_the_request_asks_for_english():
    """It used to say "the language you normally use with this user - Korean is
    fine", and on a Korean-speaking fleet that is what came back: all three
    replies of the first real round were Hangul, one of them 54 bytes for 25
    characters against a 41-byte cap. The budget is counted in BYTES."""
    text = report.build_request_text(MAILDROP)
    assert "Write it in English" in text
    assert "Korean is fine" not in text
    assert "at most 40 characters" in text     # the limit itself does not move


def test_a_korean_reply_still_parses_and_still_renders():
    """Asking for English is about the REQUEST, not about what the panel can
    display. Agents quote Korean and humans write it by hand; Hangul support
    stays exactly where it was."""
    body = "CLAWDMETER-REPORT/1 NEEDS-YOU: 빌드 끝났어요 머지할까요?"
    parsed = inbox.parse_report(body)
    assert parsed is not None
    state, summary = parsed
    assert state == inbox.STATE_REPORT_NEEDS_YOU
    assert summary == "빌드 끝났어요 머지할까요?"
    # ...and it reaches the wire as real Hangul, not as romanisation or '?'.
    row = inbox.report_row(
        inbox.Message("m", NOW, "PEER", body, report_state=state, summary=summary),
        NOW, text_max=64)
    assert "빌드" in row[inbox.MSG_FIELD_INDEX]


def test_korean_costs_three_bytes_a_syllable_which_is_why_english():
    """The measurement behind the rule, kept as a test so it cannot rot."""
    korean = "머지·정리 완료, 학습 job은 계속 실행 중"
    assert len(korean) == 25
    assert len(korean.encode("utf-8")) == 54
    assert inbox.report_text_max(500) == 41
    # 25 on-spec characters, and the panel still has to cut it.
    assert inbox.elide_message(korean, 41) != korean
    # The same 40 characters in English fit with room to spare.
    english = "merged and tidied; training job runs on"
    assert len(english) <= 40
    assert inbox.elide_message(english, 41) == english


# ---------------------------------------------------------------------------
# Broadcasting the standing rules
# ---------------------------------------------------------------------------

def test_the_rules_come_from_the_document_not_a_string():
    """The text is going to be written into files on other people's machines.
    A copy in the source that had drifted from the document explaining why it
    is safe would be the one thing nobody could audit."""
    rules = report.rules_text_from_markdown()
    assert rules and rules.startswith("## Clawdmeter")


def _flat(text):
    """The rules are wrapped prose in a document; assert on the sentences, not
    on where the line breaks happen to fall."""
    return " ".join(text.lower().split())


def test_the_rules_say_that_nothing_authenticates_anybody():
    """The paragraph that makes this safe to send. Without it the broadcast
    would be teaching agents to expect messages from a tool, and expecting is
    halfway to trusting."""
    rules = _flat(report.rules_text_from_markdown())
    assert "authenticates nobody" in rules
    assert "is permission for" in rules
    assert "approval comes only from your own conversation" in rules


def test_the_rules_do_not_ask_anyone_to_trust_clawdmeter():
    """A rule that relaxed an agent's authorisation check would turn every
    peer into a way to approve work on every machine."""
    rules = _flat(report.rules_text_from_markdown())
    for phrase in ("treat as the owner", "trust this",
                   "treat this as approval", "you may approve"):
        assert phrase not in rules
    # It may say what a go-ahead MEANS; it may not say it carries authority.
    assert "acknowledgement, not an approval" in rules


def test_the_broadcast_tells_agents_to_do_nothing_if_they_already_know():
    """What makes re-running it after new sessions appear cheap: the ones that
    already have the section touch no files."""
    body = report.build_broadcast_body("clawdmeter-inbox")
    assert "## Clawdmeter" in body
    assert "DO NOTHING AT ALL" in body
    assert "NOOP" in body and "SAVED" in body
    assert "clawdmeter-inbox" in body


def test_the_broadcast_prompt_names_every_target_and_nobody_else():
    prompt = report.build_broadcast_prompt(["A-one", "B-two"], "drop")
    assert "A-one" in prompt and "B-two" in prompt
    assert "and to nobody else" in " ".join(prompt.split())
    assert "ListAgents and SendMessage" in prompt


def test_a_broadcast_with_no_reachable_agents_refuses():
    ok, detail = report.broadcast_rules([])
    assert ok is False
    assert "no reachable agents" in detail


def test_a_broadcast_without_the_rules_sends_nothing():
    """A REPORT.md that lost the section must stop the broadcast, not send an
    empty one -- an agent asked to save nothing would save the instructions."""
    ok, detail = report.broadcast_rules([], rules="")
    assert ok is False
    assert "missing from REPORT.md" in detail
