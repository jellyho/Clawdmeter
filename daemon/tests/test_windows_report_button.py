#!/usr/bin/env python3
"""Unit tests for the report button — device → host, over the TX characteristic.

The device notifies a button event on TX (…0003), the characteristic that has
carried {"ack":true}/{"err":true} since the first firmware with nobody
subscribed. Subscribing to it is the new behaviour, so most of what is asserted
here is about what must NOT happen: an ack must never be read as a press, an
unknown code must never be guessed at, a board that notifies nothing must be
quiet rather than broken, and a round — which spawns `claude -p` and can run for
minutes — must never sit on the poll tick.

Both halves are mocked: the BleakClient (so no radio) and the child process (so
no `claude -p`, no quota, no five-minute rate limit to trip over).

Run: python -m pytest daemon/tests/test_windows_report_button.py -x -q
"""
import asyncio
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.exc import BleakError

import daemon.claude_usage_daemon_windows as mod
from daemon.claude_usage_daemon_windows import (
    EVENT_BROADCAST,
    EVENT_DISMISS,
    EVENT_GO_AHEAD,
    EVENT_REPORT,
    TX_CHAR_UUID,
    Session,
    connect_and_run,
    run_report_round,
)

REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_round_in_flight(monkeypatch):
    """The in-flight round is module state; no test may inherit another's."""
    monkeypatch.setattr(mod, "_report_round", None)


def fake_client(*, has_tx=True, notify_error=None):
    """A connected BleakClient stand-in with a controllable GATT table."""
    client = AsyncMock()
    client.is_connected = True
    client.connect = AsyncMock(return_value=None)
    client.disconnect = AsyncMock()
    client.write_gatt_char = AsyncMock(return_value=None)
    client.start_notify = AsyncMock(
        side_effect=notify_error if notify_error is not None else None
    )

    def lookup(uuid):
        if uuid == TX_CHAR_UUID:
            return MagicMock() if has_tx else None
        return None          # no session characteristic; irrelevant here

    client.services = MagicMock()
    client.services.get_characteristic = MagicMock(side_effect=lookup)
    return client


class FakeStdout:
    """The child's stdout: an async iterator over already-decided lines."""

    def __init__(self, lines):
        self._lines = [
            ln if isinstance(ln, bytes) else (ln + "\n").encode("utf-8")
            for ln in lines
        ]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class FakeProc:
    """A stand-in for the dispatcher child process.

    `gate` (an asyncio.Event) holds wait() open, which is how the slow-round
    tests stay deterministic — no sleeps, no wall-clock races.
    """

    def __init__(self, lines=(), rc=0, gate=None):
        self.stdout = FakeStdout(lines)
        self._rc = rc
        self._gate = gate
        self.killed = False

    async def wait(self):
        if self._gate is not None:
            await self._gate.wait()
        return self._rc

    def kill(self):
        self.killed = True


def exec_returning(proc, seen=None):
    """An exec_fn that hands back `proc` and records how it was invoked."""
    async def _exec(*argv, **kwargs):
        if seen is not None:
            seen.append((argv, kwargs))
        return proc
    return _exec


def notify(session, payload):
    """Deliver one TX notification, the way bleak delivers one."""
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    session._on_tx(None, bytearray(raw))


def logged(capsys):
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# _on_tx — telling a button press from the ack traffic already on the wire
# ---------------------------------------------------------------------------

def test_report_event_is_queued_and_wakes_the_tick():
    """An "ev" makes it an event; the tick's wait is woken so the round starts
    now rather than up to TICK seconds later."""
    async def go():
        session = Session(fake_client())
        notify(session, {"ev": EVENT_REPORT})
        assert list(session.events) == [{"ev": EVENT_REPORT}]
        assert session.event_pending.is_set()
    asyncio.run(go())


@pytest.mark.parametrize("payload", [
    b'{"ack":true}',                 # the ack that has always been on this char
    b'{"err":true}',                 # ...and the nack
    b'{"ev":true}',                  # bool is an int in Python; this is not event 1
    b'{"ev":"1"}',                   # a string is not a code
    b'{"ev":null}',
    b'{"nope":1}',                   # a key from a firmware newer than this daemon
    b'[1,2,3]',                      # right JSON, wrong shape
    b'not json at all',
    b'',                             # an empty notification
    b'\xff\xfe\x00garbage',          # not even text
    b'{"ev":1',                      # truncated
])
def test_non_events_are_ignored_silently(payload, capsys):
    """Nothing but a JSON object with an integer "ev" is a button press.

    Silently matters as much as ignored: an ack goes out on every usage payload,
    so a log line per ack would bury everything else in the daemon log."""
    async def go():
        session = Session(fake_client())
        notify(session, payload)
        assert not session.events
        assert not session.event_pending.is_set()
        await session.handle_events()
    asyncio.run(go())
    assert logged(capsys) == ""


def test_unknown_event_code_is_ignored_and_named(capsys):
    """A code this daemon does not know does nothing — and says so.

    Guessing is the failure that matters: an unrecognised code quietly falling
    through to the report handler would spend the fleet's quota on a button
    nobody pressed."""
    started = []
    async def go():
        session = Session(fake_client())
        notify(session, {"ev": 99})
        with patch.object(mod, "run_report_round", side_effect=started.append):
            await session.handle_events()
    asyncio.run(go())
    assert started == []
    assert "Ignoring unknown device event 99" in logged(capsys)


def test_every_released_event_code_has_a_handler():
    """The table is the dispatch point, and the codes are append-only: a code
    the firmware can send with no entry here is a button that does nothing."""
    assert EVENT_REPORT in mod.EVENT_HANDLERS
    assert EVENT_GO_AHEAD in mod.EVENT_HANDLERS
    assert EVENT_DISMISS in mod.EVENT_HANDLERS
    assert EVENT_BROADCAST in mod.EVENT_HANDLERS


# ---------------------------------------------------------------------------
# Go ahead — the sid has to become an agent
# ---------------------------------------------------------------------------

def _index(tmp_path, monkeypatch, index):
    """Point the daemon at a sidecar handoff file carrying this sid index."""
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"ts": 1.0, "payload": "x", "index": index}),
                    encoding="utf-8")
    monkeypatch.setattr(mod, "SESSIONS_FILE", path)
    return path


def _run_event(session, doc, spawn):
    async def go():
        notify(session, doc)
        with patch.object(mod, "run_report_round", spawn):
            await session.handle_events()
    asyncio.run(go())


def test_go_ahead_turns_the_sid_into_the_agent_that_sent_the_report(
        tmp_path, monkeypatch, capsys):
    """The whole trick: two characters off a panel become a name to message."""
    _index(tmp_path, monkeypatch,
           {"g4": {"state": 13, "sender": "ACRFT-N", "mid": "abc12345"}})
    calls = []

    async def spawn(**kw):
        calls.append(kw)
        return 0

    _run_event(Session(fake_client()), {"ev": EVENT_GO_AHEAD, "sid": "g4"}, spawn)
    assert len(calls) == 1
    assert calls[0]["extra_args"] == ["--go-ahead", "ACRFT-N"]
    assert "GO AHEAD: g4 -> ACRFT-N" in logged(capsys)


def test_go_ahead_on_a_sid_the_index_does_not_know_sends_nothing_and_says_so(
        tmp_path, monkeypatch, capsys):
    """A row that expired, or a sidecar too old to write an index. Loud, because
    somebody is standing at the device having just answered an agent."""
    _index(tmp_path, monkeypatch, {"zz": {"state": 13, "sender": "X"}})

    async def spawn(**kw):
        raise AssertionError("must not spawn")

    _run_event(Session(fake_client()), {"ev": EVENT_GO_AHEAD, "sid": "g4"}, spawn)
    assert "GO AHEAD: no card g4" in logged(capsys)


def test_go_ahead_on_a_row_no_agent_sent_refuses(tmp_path, monkeypatch, capsys):
    """Fleet session rows carry no sender: there is nobody to answer, and
    guessing would send the owner's go-ahead to whoever happened to hash near."""
    _index(tmp_path, monkeypatch, {"g4": {"state": 6}})

    async def spawn(**kw):
        raise AssertionError("must not spawn")

    _run_event(Session(fake_client()), {"ev": EVENT_GO_AHEAD, "sid": "g4"}, spawn)
    assert "there is nobody to answer" in logged(capsys)


# ---------------------------------------------------------------------------
# Dismiss — the row must stop being re-sent
# ---------------------------------------------------------------------------

def test_dismiss_records_the_message_id_not_the_sid(tmp_path, monkeypatch, capsys):
    """Keyed on the WORDS (the mid is a hash of session+sender+body), so the
    same agent saying something new comes back. A sid would silence it."""
    _index(tmp_path, monkeypatch,
           {"g4": {"state": 11, "sender": "peer", "mid": "deadbeef"}})
    dismissed = tmp_path / "dismissed.json"
    monkeypatch.setattr(mod, "DISMISS_FILE", dismissed)

    async def spawn(**kw):
        raise AssertionError("a dismissal spawns nothing")

    _run_event(Session(fake_client()), {"ev": EVENT_DISMISS, "sid": "g4"}, spawn)
    held = json.loads(dismissed.read_text(encoding="utf-8"))["dismissed"]
    assert [d["mid"] for d in held] == ["deadbeef"]
    assert "DISMISS: g4 cleared" in logged(capsys)


def test_the_dismissed_file_is_bounded_and_expires(tmp_path):
    """Two bounds, and both earn their place: unbounded growth is a file the
    poller re-reads every tick, and an entry that never expired would be a
    permanent gag on an agent."""
    path = tmp_path / "dismissed.json"
    old = [{"mid": f"old{i}", "ts": 0.0} for i in range(3)]
    path.write_text(json.dumps({"dismissed": old}), encoding="utf-8")
    mod.record_dismissal("fresh", path, now=mod.DISMISS_TTL + 10)
    held = json.loads(path.read_text(encoding="utf-8"))["dismissed"]
    assert [d["mid"] for d in held] == ["fresh"]        # the stale three are gone

    for i in range(mod.DISMISS_KEEP + 5):
        mod.record_dismissal(f"m{i}", path, now=mod.DISMISS_TTL + 11)
    held = json.loads(path.read_text(encoding="utf-8"))["dismissed"]
    assert len(held) == mod.DISMISS_KEEP
    assert held[-1]["mid"] == f"m{mod.DISMISS_KEEP + 4}"


def test_a_dismissal_survives_an_unreadable_file(tmp_path):
    """Never raises: a malformed file must not be able to kill the poll loop."""
    path = tmp_path / "dismissed.json"
    path.write_text("{ not json", encoding="utf-8")
    assert mod.record_dismissal("x", path) == 1


# ---------------------------------------------------------------------------
# handle_events — the press turns into a round
# ---------------------------------------------------------------------------

def test_report_event_dispatches_a_round(capsys):
    """The whole point: one press, one round."""
    calls = []

    async def fake_round():
        calls.append(1)

    async def go():
        session = Session(fake_client())
        notify(session, {"ev": EVENT_REPORT})
        with patch.object(mod, "run_report_round", new=fake_round):
            await session.handle_events()
            await mod._report_round
    asyncio.run(go())
    assert calls == [1]
    assert "button pressed" in logged(capsys)


def test_handle_events_clears_the_wakeup_flag():
    """Otherwise the tick's _wait_first would spin at full speed forever."""
    async def fake_round():
        return 0

    async def go():
        session = Session(fake_client())
        notify(session, {"ev": EVENT_REPORT})
        with patch.object(mod, "run_report_round", new=fake_round):
            await session.handle_events()
        assert not session.event_pending.is_set()
        assert not session.events
        await mod._report_round
    asyncio.run(go())


def test_second_press_while_a_round_runs_is_ignored(capsys):
    """A round already in flight is the one case this daemon judges by itself.

    Everything else about "should this round happen" — the five-minute limit, no
    mail drop, no reachable agents — belongs to the dispatcher and is not
    duplicated here."""
    async def go():
        gate = asyncio.Event()
        proc = FakeProc(rc=0, gate=gate)
        session = Session(fake_client())

        with patch.object(asyncio, "create_subprocess_exec",
                          new=exec_returning(proc)):
            notify(session, {"ev": EVENT_REPORT})
            await session.handle_events()
            first = mod._report_round
            assert mod.report_round_in_flight()

            notify(session, {"ev": EVENT_REPORT})
            await session.handle_events()
            assert mod._report_round is first        # no second round

            gate.set()
            await first
    asyncio.run(go())
    out = logged(capsys)
    assert "a round is already running" in out


def test_a_finished_round_does_not_block_the_next_press():
    """The daemon adds no rate limit of its own. Two presses a second apart both
    dispatch; whether the SECOND one does anything is the dispatcher's call, and
    it answers with `refused: rate limited: ...` which this daemon relays."""
    async def go():
        spawns = []
        session = Session(fake_client())
        with patch.object(asyncio, "create_subprocess_exec",
                          new=exec_returning(FakeProc(rc=0), spawns)):
            for _ in range(2):
                notify(session, {"ev": EVENT_REPORT})
                await session.handle_events()
                await mod._report_round
        return spawns
    assert len(asyncio.run(go())) == 2


# ---------------------------------------------------------------------------
# A round must never sit on the poll tick
# ---------------------------------------------------------------------------

def test_a_slow_round_does_not_block_the_tick():
    """The round outlives many ticks and the loop keeps running through it.

    A round spawns `claude -p` and then watches the inbox for replies — minutes
    in the worst case. Awaiting it on the tick would stop the usage payload
    flowing and the link would look dead."""
    async def go():
        gate = asyncio.Event()          # the round finishes only when we say so
        proc = FakeProc(lines=["asked 3: A, B, C"], rc=0, gate=gate)
        session = Session(fake_client())

        with patch.object(asyncio, "create_subprocess_exec",
                          new=exec_returning(proc)):
            notify(session, {"ev": EVENT_REPORT})
            await session.handle_events()          # returns while the round runs
            assert mod.report_round_in_flight()

            for _ in range(5):                     # five more ticks, unimpeded
                await session.maybe_send_sessions()
                await session.handle_events()
                assert mod.report_round_in_flight()

            gate.set()
            assert await mod._report_round == 0
        assert not mod.report_round_in_flight()
    asyncio.run(go())


def test_round_that_never_exits_is_killed(capsys):
    """A dispatcher that hangs would block every later press for good."""
    async def go():
        proc = FakeProc(rc=0, gate=asyncio.Event())     # never set
        with patch.object(asyncio, "create_subprocess_exec",
                          new=exec_returning(proc)):
            return await run_report_round(timeout=0.05), proc
    rc, proc = asyncio.run(go())
    assert rc is None
    assert proc.killed
    assert "TIMED OUT" in logged(capsys)


# ---------------------------------------------------------------------------
# run_report_round — relaying what the dispatcher says
# ---------------------------------------------------------------------------

def test_dispatcher_output_is_relayed_as_it_arrives(capsys):
    """Streamed, not collected at exit: a refusal is printed before the spawn,
    so the owner sees it a second after the press rather than minutes later."""
    lines = ["asked 2: ACRFT-N, DLPS", "dispatcher exit=0 in 4.2s",
             "answered 2, 2 matched the contract, 0 did not"]
    async def go():
        with patch.object(asyncio, "create_subprocess_exec",
                          new=exec_returning(FakeProc(lines=lines, rc=0))):
            return await run_report_round()
    assert asyncio.run(go()) == 0
    out = logged(capsys)
    for line in lines:
        assert f"report: {line}" in out
    assert "round finished" in out


def test_a_refused_round_is_logged_loudly(capsys):
    """The owner pressed a button and is standing there. A refusal reaches the
    daemon log twice: verbatim from the dispatcher, and again as a line that
    names the exit code, so it is legible in a log full of usage payloads."""
    refusal = "refused: rate limited: 240s to go (one round per 300s)"
    async def go():
        with patch.object(asyncio, "create_subprocess_exec",
                          new=exec_returning(FakeProc(lines=[refusal], rc=2))):
            return await run_report_round()
    assert asyncio.run(go()) == 2
    out = logged(capsys)
    assert f"report: {refusal}" in out
    assert "REPORT ROUND DID NOT RUN (dispatcher exit 2)" in out
    assert refusal in out.split("REPORT ROUND DID NOT RUN")[1]


@pytest.mark.parametrize("refusal, code", [
    ("refused: no reachable agents to ask", 2),
    ("could not set up the mail drop: no live session named clawdmeter-inbox", 2),
    ("DISPATCH FAILED: could not start the Claude Code CLI", 1),
])
def test_every_dispatcher_refusal_surfaces(refusal, code, capsys):
    """The dispatcher owns all of these decisions; this daemon owns none of
    them. What it owes the owner is that each one is visible."""
    async def go():
        with patch.object(asyncio, "create_subprocess_exec",
                          new=exec_returning(FakeProc(lines=[refusal], rc=code))):
            await run_report_round()
    asyncio.run(go())
    out = logged(capsys)
    assert refusal in out
    assert f"exit {code}" in out


def test_round_that_cannot_start_is_logged_not_raised(capsys):
    """No Python, no dispatcher script, a loop with no subprocess support — none
    of it may reach the poll loop as an exception."""
    async def boom(*_a, **_kw):
        raise FileNotFoundError("no such file")

    async def go():
        with patch.object(asyncio, "create_subprocess_exec", new=boom):
            return await run_report_round()
    assert asyncio.run(go()) is None
    assert "FAILED to start" in logged(capsys)


def test_round_runs_the_real_dispatcher_script():
    """The child is the CLI this repo ships, invoked with this interpreter."""
    seen = []
    async def go():
        with patch.object(asyncio, "create_subprocess_exec",
                          new=exec_returning(FakeProc(rc=0), seen)):
            await run_report_round()
    asyncio.run(go())
    (argv, kwargs), = seen
    assert argv[0] == mod.sys.executable
    assert Path(argv[1]) == mod.REPORT_SCRIPT
    assert mod.REPORT_SCRIPT.exists()
    assert kwargs["stdout"] is asyncio.subprocess.PIPE
    assert kwargs["stderr"] is asyncio.subprocess.STDOUT


# ---------------------------------------------------------------------------
# Subscribing — a device that notifies nothing must be quiet, not broken
# ---------------------------------------------------------------------------

def test_subscribes_to_tx_on_connect():
    async def go():
        client = fake_client()
        session = Session(client)
        await session.setup_event_subscription()
        assert session.tx_supported
        return [c.args[0] for c in client.start_notify.await_args_list]
    assert TX_CHAR_UUID in asyncio.run(go())


def test_device_without_tx_stays_quiet(capsys):
    """A board whose GATT table has no TX turns the feature off for that link
    and takes nothing else with it."""
    async def go():
        client = fake_client(has_tx=False)
        session = Session(client)
        await session.setup_event_subscription()
        assert not session.tx_supported
        assert client.start_notify.await_count == 0
        # ...and the tick keeps working
        await session.handle_events()
        await session.maybe_send_sessions()
    asyncio.run(go())
    out = logged(capsys)
    assert "no event characteristic" in out
    assert "error" not in out.lower()


def test_firmware_that_never_notifies_is_simply_silent(capsys):
    """The ordinary case for every board already in the field: subscribing
    succeeds, no event ever arrives, and nothing is logged about it."""
    async def go():
        session = Session(fake_client())
        await session.setup_event_subscription()
        capsys.readouterr()                     # drop the subscribe bookkeeping
        for _ in range(20):
            await session.handle_events()
    asyncio.run(go())
    assert logged(capsys) == ""


@pytest.mark.parametrize("err", [
    BleakError("GATT unavailable"),
    OSError("[WinError -2147483629] The object has been closed"),
    ValueError("characteristic not found"),
])
def test_failed_subscription_degrades_quietly(err, capsys):
    """WinRT's CCCD write can raise a raw OSError on a just-power-cycled peer —
    the same failure setup_refresh_subscription() already tolerates. It must not
    take the connection down."""
    async def go():
        session = Session(fake_client(notify_error=err))
        await session.setup_event_subscription()
        assert not session.tx_supported
        await session.handle_events()
    asyncio.run(go())
    assert "subscription unavailable" in logged(capsys)


def test_missing_service_collection_does_not_disable_the_feature():
    """An older bleak with no usable service collection must stay optimistic and
    let start_notify decide — otherwise the feature switches itself off on a
    device that has TX."""
    async def go():
        client = fake_client()
        client.services = None
        session = Session(client)
        await session.setup_event_subscription()
        assert session.tx_supported
        return [c.args[0] for c in client.start_notify.await_args_list]
    assert TX_CHAR_UUID in asyncio.run(go())


# ---------------------------------------------------------------------------
# The poll loop — one tick, everything on it
# ---------------------------------------------------------------------------

def test_connect_and_run_subscribes_and_still_ships_usage():
    """No second timer and no extra thread: the events ride the tick that was
    already there, and the usage payload is untouched."""
    client = fake_client()
    device = MagicMock()
    device.address = "AA:BB:CC:DD:EE:FF"

    async def go():
        stop_event = asyncio.Event()

        async def fake_poll(_token):
            stop_event.set()                      # one pass through the loop
            return {"ok": True, "s": 1}

        with patch.object(mod, "BleakClient", return_value=client), \
             patch.object(mod, "read_token", return_value="tok"), \
             patch.object(mod, "poll_api", new=fake_poll):
            await connect_and_run(device, stop_event)

    asyncio.run(go())
    subscribed = [c.args[0] for c in client.start_notify.await_args_list]
    assert TX_CHAR_UUID in subscribed
    assert mod.REQ_CHAR_UUID in subscribed        # the old one still there
    assert mod.RX_CHAR_UUID in [c.args[0] for c in client.write_gatt_char.await_args_list]


# ---------------------------------------------------------------------------
# The wire contract, checked against the firmware that produces it
# ---------------------------------------------------------------------------

def test_event_codes_match_the_firmware():
    """These numbers cross the BLE boundary. If ble.h and this daemon ever
    disagree, a press does nothing and nothing says why."""
    src = (REPO / "firmware" / "src" / "ble.h").read_text(encoding="utf-8")
    found = dict(
        (name, int(val))
        for name, val in re.findall(r"BLE_EVENT_(\w+)\s*=\s*(\d+)", src)
    )
    assert found.get("REPORT") == EVENT_REPORT
    assert found.get("GO_AHEAD") == EVENT_GO_AHEAD


def test_firmware_emits_the_ev_key_this_daemon_dispatches_on():
    """The discriminator itself: no "ev", no event. Asserting it against the
    encoder keeps the two ends from drifting into a channel with no shape."""
    src = (REPO / "firmware" / "src" / "ble.cpp").read_text(encoding="utf-8")
    assert '\\"ev\\":%d' in src            # the event payloads carry it
    assert '{\\"ack\\":true}' in src       # ...and the ack traffic does not
    assert '{\\"err\\":true}' in src
