#!/usr/bin/env python3
"""Unit tests for live-session shipping on Windows (issue #135).

Two halves, both Windows-specific:

  * the daemon side — claude_usage_daemon_windows ships the sidecar's fitted
    wire payload to the SS characteristic on the existing poll tick, and stays
    quiet when the sidecar isn't installed or the device has no such
    characteristic;
  * the sidecar side — the Windows portability of clawdmeter_sessions.py
    (process liveness without /proc, port binding, the atomic replace, logging
    with no console).

Run: python -m pytest daemon/tests/test_windows_sessions.py -x -q
"""
import asyncio
import json
import os
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.exc import BleakError

import daemon.clawdmeter_sessions as sidecar
from daemon.claude_usage_daemon_windows import (
    SS_CHAR_UUID,
    BleakCharacteristicNotFoundError,
    Session,
    connect_and_run,
    read_sessions_payload,
)

PAYLOAD = '{"ss":[["a3","clawdmeter",6,42,12,1,0,0,0,0,0,84,1]]}'
PAYLOAD2 = '{"ss":[["a3","clawdmeter",1,43,0,1,0,0,0,0,0,86,1]]}'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_handoff(path, payload, ts=1700000000.0):
    """Write the file exactly as the sidecar's write_sessions_file() does."""
    path.write_text(
        json.dumps({"ts": ts, "payload": payload}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return path


def fake_client(*, has_ss=True, write=None):
    """A connected BleakClient stand-in with a controllable GATT table."""
    client = AsyncMock()
    client.is_connected = True
    client.connect = AsyncMock(return_value=None)
    client.disconnect = AsyncMock()
    client.start_notify = AsyncMock()
    client.write_gatt_char = write or AsyncMock(return_value=None)
    client.services.get_characteristic = MagicMock(
        return_value=(MagicMock() if has_ss else None)
    )
    return client


def ss_writes(client):
    """Every (uuid, data) pair written to the session characteristic."""
    return [
        call.args[:2]
        for call in client.write_gatt_char.await_args_list
        if call.args and call.args[0] == SS_CHAR_UUID
    ]


def run_ticks(session, n=1):
    """Drive maybe_send_sessions() n times, the way the poll loop does."""
    async def go():
        for _ in range(n):
            await session.maybe_send_sessions()
    asyncio.run(go())


# ---------------------------------------------------------------------------
# read_sessions_payload — the file contract with the sidecar
# ---------------------------------------------------------------------------

def test_windows_sessions_reads_the_payload_string_verbatim(tmp_path):
    """Only the payload string crosses the wire — never the {"ts":...} wrapper.

    The sidecar already fitted those exact bytes to the byte budget, so
    re-encoding them here could push the row past the device's buffer."""
    path = write_handoff(tmp_path / "sessions.json", PAYLOAD)
    assert read_sessions_payload(path) == PAYLOAD


def test_windows_sessions_absent_file_reads_as_nothing(tmp_path):
    """The default state — no sidecar installed — must be a silent no-op."""
    assert read_sessions_payload(tmp_path / "never-written.json") is None


@pytest.mark.parametrize("body", [
    "",                                        # empty (created, not yet written)
    "{",                                       # truncated mid-write
    '{"ts":1,"payload":',                       # half-written value
    "[]",                                      # right JSON, wrong shape
    '{"ts":1}',                                 # no payload key
    '{"ts":1,"payload":null}',                  # payload not a string
    '{"ts":1,"payload":123}',
    '{"ts":1,"payload":""}',                    # empty payload: nothing to send
    "\xff\xfe garbage",                        # not even text
])
def test_windows_sessions_malformed_file_never_raises(tmp_path, body):
    """A malformed or half-written handoff file must never reach the poll loop
    as an exception — it reads as "nothing to ship" and the next tick retries."""
    path = tmp_path / "sessions.json"
    path.write_text(body, encoding="utf-8", errors="surrogateescape")
    assert read_sessions_payload(path) is None


def test_windows_sessions_unreadable_file_reads_as_nothing(tmp_path):
    """A directory in place of the file (or any OSError) is not a crash."""
    (tmp_path / "sessions.json").mkdir()
    assert read_sessions_payload(tmp_path / "sessions.json") is None


def test_windows_sessions_file_default_matches_the_sidecar(real_sessions_file):
    """The two halves must agree on the handoff path with no config at all."""
    assert real_sessions_file is not None
    assert os.path.normcase(str(real_sessions_file)) == os.path.normcase(
        sidecar.DEFAULT_SESSIONS_FILE
    )


# ---------------------------------------------------------------------------
# Session.maybe_send_sessions — shipping on the existing tick
# ---------------------------------------------------------------------------

def test_windows_sessions_shipped_when_the_file_changes(tmp_path, monkeypatch):
    """A changed payload goes out on the SS characteristic, byte for byte."""
    import daemon.claude_usage_daemon_windows as mod
    path = write_handoff(tmp_path / "sessions.json", PAYLOAD)
    monkeypatch.setattr(mod, "SESSIONS_FILE", path)
    client = fake_client()
    session = Session(client)

    run_ticks(session)
    write_handoff(path, PAYLOAD2)
    run_ticks(session)

    assert ss_writes(client) == [
        (SS_CHAR_UUID, PAYLOAD.encode("utf-8")),
        (SS_CHAR_UUID, PAYLOAD2.encode("utf-8")),
    ]


def test_windows_sessions_not_reshipped_when_unchanged(tmp_path, monkeypatch):
    """Ten idle ticks over an unchanged file are ten no-ops — the device only
    hears about sessions when something actually changed."""
    import daemon.claude_usage_daemon_windows as mod
    path = write_handoff(tmp_path / "sessions.json", PAYLOAD)
    monkeypatch.setattr(mod, "SESSIONS_FILE", path)
    client = fake_client()
    session = Session(client)

    run_ticks(session, 10)
    assert len(ss_writes(client)) == 1

    # A rewrite with identical content (new ts) is still not a change.
    write_handoff(path, PAYLOAD, ts=1700009999.0)
    run_ticks(session)
    assert len(ss_writes(client)) == 1


def test_windows_sessions_silent_when_the_file_is_absent(tmp_path, monkeypatch, capsys):
    """No sidecar installed: no writes, no log lines, nothing at all."""
    import daemon.claude_usage_daemon_windows as mod
    monkeypatch.setattr(mod, "SESSIONS_FILE", tmp_path / "absent.json")
    client = fake_client()

    run_ticks(Session(client), 5)

    assert client.write_gatt_char.await_count == 0
    assert capsys.readouterr().out == ""


def test_windows_sessions_malformed_file_ships_nothing(tmp_path, monkeypatch):
    """Defence in depth: even though the sidecar writes atomically, a corrupt
    file must not put garbage on the wire or take the tick down."""
    import daemon.claude_usage_daemon_windows as mod
    path = tmp_path / "sessions.json"
    path.write_text('{"ts":17000000', encoding="utf-8")
    monkeypatch.setattr(mod, "SESSIONS_FILE", path)
    client = fake_client()
    session = Session(client)

    run_ticks(session, 3)
    assert client.write_gatt_char.await_count == 0

    write_handoff(path, PAYLOAD)  # ...and it recovers once the file is whole
    run_ticks(session)
    assert ss_writes(client) == [(SS_CHAR_UUID, PAYLOAD.encode("utf-8"))]


def test_windows_sessions_resent_on_a_new_connection(tmp_path, monkeypatch):
    """Reconnect state is per-Session, like the bash daemon's cleared
    SS_CHAR_PATH/LAST_SESSIONS_SIG: a device that just came back has an empty
    Sessions tab and must be resent the current payload."""
    import daemon.claude_usage_daemon_windows as mod
    path = write_handoff(tmp_path / "sessions.json", PAYLOAD)
    monkeypatch.setattr(mod, "SESSIONS_FILE", path)

    client = fake_client()
    run_ticks(Session(client), 3)
    run_ticks(Session(client), 3)      # the reconnect

    assert len(ss_writes(client)) == 2


def test_windows_sessions_payload_is_written_as_utf8_bytes(tmp_path, monkeypatch):
    """Byte-exactness matters: the bash daemon needed a dedicated od-based
    writer because per-character conversion corrupted multi-byte labels."""
    import daemon.claude_usage_daemon_windows as mod
    payload = '{"ss":[["a3","déjà-vu…",1,10,0,1,0,0,0,0,0,5,0]]}'
    path = write_handoff(tmp_path / "sessions.json", payload)
    monkeypatch.setattr(mod, "SESSIONS_FILE", path)
    client = fake_client()

    run_ticks(Session(client))

    (_, data), = ss_writes(client)
    assert data == payload.encode("utf-8")
    assert data.decode("utf-8") == payload


def test_windows_sessions_written_without_response(tmp_path, monkeypatch):
    """The firmware declares WRITE|WRITE_NR; write-without-response keeps the
    tick from blocking on an ack, exactly like the usage payload."""
    import daemon.claude_usage_daemon_windows as mod
    path = write_handoff(tmp_path / "sessions.json", PAYLOAD)
    monkeypatch.setattr(mod, "SESSIONS_FILE", path)
    client = fake_client()

    run_ticks(Session(client))

    call = client.write_gatt_char.await_args_list[0]
    assert call.kwargs.get("response") is False


# ---------------------------------------------------------------------------
# Devices without the characteristic — detect once, then be quiet
# ---------------------------------------------------------------------------

def test_windows_sessions_missing_characteristic_goes_quiet(tmp_path, monkeypatch, capsys):
    """Bleak raises BleakCharacteristicNotFoundError when the peer has no such
    characteristic (every board but the flagship). One attempt, one log line,
    then silence — even when the file keeps changing."""
    import daemon.claude_usage_daemon_windows as mod
    path = write_handoff(tmp_path / "sessions.json", PAYLOAD)
    monkeypatch.setattr(mod, "SESSIONS_FILE", path)
    client = fake_client(write=AsyncMock(
        side_effect=BleakCharacteristicNotFoundError(SS_CHAR_UUID)
    ))
    session = Session(client)

    run_ticks(session)
    for i in range(5):                       # the sidecar keeps publishing...
        write_handoff(path, PAYLOAD2 + " " * i)
        run_ticks(session)

    assert client.write_gatt_char.await_count == 1
    assert session.ss_supported is False
    out = capsys.readouterr().out
    assert out.count("no session characteristic") == 1


def test_windows_sessions_missing_characteristic_is_a_bleak_error_subclass():
    """It must stay catchable as a BleakError, and be distinguishable from one:
    a generic BleakError is a transient link problem, not an absent feature."""
    assert issubclass(BleakCharacteristicNotFoundError, BleakError)
    assert BleakCharacteristicNotFoundError is not BleakError


def test_windows_sessions_probe_disables_before_the_first_write(tmp_path, monkeypatch, capsys):
    """The GATT table is already cached from discovery, so a device without the
    characteristic is spotted at connect time — no failed write at all."""
    import daemon.claude_usage_daemon_windows as mod
    path = write_handoff(tmp_path / "sessions.json", PAYLOAD)
    monkeypatch.setattr(mod, "SESSIONS_FILE", path)
    client = fake_client(has_ss=False)
    session = Session(client)

    session.probe_session_support()
    run_ticks(session, 3)

    assert session.ss_supported is False
    assert client.write_gatt_char.await_count == 0
    assert capsys.readouterr().out.count("no session characteristic") == 1


def test_windows_sessions_probe_stays_optimistic_when_it_cannot_look(tmp_path, monkeypatch):
    """`client.services` raises when discovery hasn't run; that is not evidence
    the characteristic is missing, so shipping stays enabled and the write path
    makes the call."""
    import daemon.claude_usage_daemon_windows as mod
    path = write_handoff(tmp_path / "sessions.json", PAYLOAD)
    monkeypatch.setattr(mod, "SESSIONS_FILE", path)
    client = fake_client()
    type(client).services = property(
        lambda self: (_ for _ in ()).throw(BleakError("Service Discovery has not been performed yet"))
    )
    session = Session(client)
    try:
        session.probe_session_support()
        assert session.ss_supported is True
        run_ticks(session)
        assert len(ss_writes(client)) == 1
    finally:
        del type(client).services


def test_windows_sessions_probe_is_skipped_on_a_supported_device(tmp_path, monkeypatch):
    """A device that has the characteristic is left alone by the probe."""
    client = fake_client(has_ss=True)
    session = Session(client)
    session.probe_session_support()
    assert session.ss_supported is True


# ---------------------------------------------------------------------------
# Write failures — a secondary feed must never drive the link
# ---------------------------------------------------------------------------

def test_windows_sessions_write_failure_logs_once_and_retries(tmp_path, monkeypatch, capsys):
    """WinRT can raise a raw OSError from a link that is going away. Log it once
    per link (no 5 s log spam), keep retrying, and never mark the payload sent."""
    import daemon.claude_usage_daemon_windows as mod
    path = write_handoff(tmp_path / "sessions.json", PAYLOAD)
    monkeypatch.setattr(mod, "SESSIONS_FILE", path)
    client = fake_client(write=AsyncMock(side_effect=OSError("[WinError -2147023673]")))
    session = Session(client)

    run_ticks(session, 4)

    assert client.write_gatt_char.await_count == 4      # keeps trying
    assert session.ss_supported is True                 # not a missing feature
    assert session.last_sessions_payload is None        # never marked as sent
    assert capsys.readouterr().out.count("Session write failed") == 1


def test_windows_sessions_write_recovers_after_a_failure(tmp_path, monkeypatch, capsys):
    """Once a write lands, the payload is remembered and the failure log arms
    again for the next distinct outage."""
    import daemon.claude_usage_daemon_windows as mod
    path = write_handoff(tmp_path / "sessions.json", PAYLOAD)
    monkeypatch.setattr(mod, "SESSIONS_FILE", path)
    calls = {"n": 0}

    async def flaky(_uuid, _data, response=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BleakError("disconnected")

    client = fake_client(write=AsyncMock(side_effect=flaky))
    session = Session(client)

    run_ticks(session, 2)
    assert session.last_sessions_payload == PAYLOAD
    run_ticks(session, 2)                     # unchanged file -> no more writes
    assert client.write_gatt_char.await_count == 2


# ---------------------------------------------------------------------------
# Integration: it rides the existing poll loop, not a new timer
# ---------------------------------------------------------------------------

def test_windows_sessions_ship_from_the_existing_poll_loop(tmp_path, monkeypatch):
    """connect_and_run's own tick is what publishes sessions — no second timer,
    no extra thread, and the usage payload still goes out untouched."""
    import daemon.claude_usage_daemon_windows as mod
    path = write_handoff(tmp_path / "sessions.json", PAYLOAD)
    monkeypatch.setattr(mod, "SESSIONS_FILE", path)
    client = fake_client()
    device = MagicMock()
    device.address = "AA:BB:CC:DD:EE:FF"

    async def go():
        stop_event = asyncio.Event()

        async def fake_poll(_token):
            stop_event.set()                  # one pass through the loop
            return {"ok": True, "s": 1}

        with patch.object(mod, "BleakClient", return_value=client), \
             patch.object(mod, "read_token", return_value="tok"), \
             patch.object(mod, "poll_api", new=fake_poll):
            await connect_and_run(device, stop_event)

    asyncio.run(go())

    uuids = [c.args[0] for c in client.write_gatt_char.await_args_list]
    assert mod.RX_CHAR_UUID in uuids          # usage payload still shipped
    assert ss_writes(client) == [(SS_CHAR_UUID, PAYLOAD.encode("utf-8"))]


def test_windows_sessions_do_not_break_the_link_when_they_fail(tmp_path, monkeypatch):
    """A failing session write must not trip the zombie-link breaker: the usage
    write owns that, and a secondary feed forcing reconnects would be worse than
    an empty Sessions tab."""
    import daemon.claude_usage_daemon_windows as mod
    path = write_handoff(tmp_path / "sessions.json", PAYLOAD)
    monkeypatch.setattr(mod, "SESSIONS_FILE", path)
    ticks = {"n": 0}

    async def write(uuid, _data, response=False):
        if uuid == SS_CHAR_UUID:
            raise OSError("[WinError -2147023673]")

    client = fake_client(write=AsyncMock(side_effect=write))
    device = MagicMock()
    device.address = "AA:BB:CC:DD:EE:FF"

    async def go():
        stop_event = asyncio.Event()

        async def fake_poll(_token):
            ticks["n"] += 1
            if ticks["n"] >= 3:
                stop_event.set()
            return {"ok": True}

        with patch.object(mod, "BleakClient", return_value=client), \
             patch.object(mod, "read_token", return_value="tok"), \
             patch.object(mod, "poll_api", new=fake_poll), \
             patch.object(mod, "POLL_INTERVAL", 0), \
             patch.object(mod, "TICK", 0):
            return await connect_and_run(device, stop_event)

    assert asyncio.run(go()) is True          # link survived all three ticks
    assert ticks["n"] == 3


# ---------------------------------------------------------------------------
# The sidecar itself on Windows
# ---------------------------------------------------------------------------

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only path")


@windows_only
def test_windows_sidecar_pid_alive_sees_a_live_process():
    """No /proc here: liveness comes from OpenProcess + WaitForSingleObject."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert sidecar.pid_alive(child.pid) is True
    finally:
        child.kill()
        child.wait(timeout=10)
    assert sidecar.pid_alive(child.pid) is False


@windows_only
def test_windows_sidecar_pid_alive_matches_the_roster_start_time():
    """Claude Code records procStart as the creation FILETIME on Windows, so the
    same pid-reuse guard the /proc path gets works here too: the real start time
    is accepted, a different one is rejected."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        start = sidecar._win_proc_starttime(child.pid)
        assert start is not None
        assert sidecar.pid_alive(child.pid, start) is True
        assert sidecar.pid_alive(child.pid, str(start)) is True
        assert sidecar.pid_alive(child.pid, start - 10_000_000) is False   # pid reuse
    finally:
        child.kill()
        child.wait(timeout=10)


@windows_only
def test_windows_sidecar_pid_alive_tolerates_the_procstart_shape():
    """The roster writes procStart as a decimal string; an int must work the
    same, and a shape we don't recognise must not be read as pid reuse (that
    would drop a live session)."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        start = sidecar._win_proc_starttime(child.pid)
        assert sidecar.pid_alive(child.pid, str(start)) is True
        assert sidecar.pid_alive(child.pid, int(start)) is True
        assert sidecar.pid_alive(child.pid, f"  {start}  ") is True
        assert sidecar.pid_alive(child.pid, "not-a-filetime") is True
        assert sidecar.pid_alive(child.pid, None) is True
    finally:
        child.kill()
        child.wait(timeout=10)


@windows_only
def test_windows_sidecar_pid_alive_rejects_junk():
    assert sidecar.pid_alive(None) is False
    assert sidecar.pid_alive("not-a-pid") is False
    assert sidecar.pid_alive(0) is False
    assert sidecar.pid_alive(-1) is False
    assert sidecar.pid_alive(0x7FFFFFF0) is False     # nothing that high is live


@windows_only
def test_windows_sidecar_does_not_reuse_a_bound_port():
    """SO_REUSEADDR on Windows lets a second process bind a port that is already
    in use, and the two would then split the hook POSTs silently. A duplicate
    sidecar must fail to bind instead."""
    assert sidecar.HookServer.allow_reuse_address is False


@windows_only
def test_windows_sidecar_second_instance_fails_to_bind(tmp_path):
    table = sidecar.SessionTable(config_dirs=[])
    first = sidecar.HookServer(("127.0.0.1", 0), table, str(tmp_path / "s.json"), 180)
    port = first.server_address[1]
    try:
        with pytest.raises(OSError):
            sidecar.HookServer(("127.0.0.1", port), table, str(tmp_path / "s2.json"), 180)
    finally:
        first.server_close()


def test_windows_sidecar_replace_retries_a_locked_target(tmp_path, monkeypatch):
    """Windows MoveFileEx loses to a reader holding the destination open. The
    write is retried rather than reported as a lost publish."""
    attempts = {"n": 0}
    real_replace = os.replace

    def flaky_replace(src, dst):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError(32, "The process cannot access the file")
        return real_replace(src, dst)

    monkeypatch.setattr(sidecar.os, "replace", flaky_replace)
    monkeypatch.setattr(sidecar.time, "sleep", lambda _s: None)

    out = tmp_path / "sessions.json"
    sidecar.write_sessions_file(str(out), PAYLOAD)

    assert attempts["n"] == 3
    assert json.loads(out.read_text(encoding="utf-8"))["payload"] == PAYLOAD


def test_windows_sidecar_replace_gives_up_eventually(tmp_path, monkeypatch):
    """A target that stays locked surfaces as the OSError publish() already
    logs — it must not retry forever and stall the hook thread."""
    monkeypatch.setattr(sidecar.os, "replace",
                        MagicMock(side_effect=PermissionError(32, "locked")))
    monkeypatch.setattr(sidecar.time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        sidecar.write_sessions_file(str(tmp_path / "sessions.json"), PAYLOAD)


def test_windows_sidecar_log_survives_a_missing_console(monkeypatch):
    """Autostart runs the sidecar under pythonw.exe, where sys.stdout is None
    and print() raises. Logging must never be the thing that kills it."""
    monkeypatch.setattr(sys, "stdout", None)
    sidecar.log("hello from a windowless process")   # must not raise


def test_windows_sidecar_file_log_is_not_created_on_import(monkeypatch, tmp_path):
    """enable_file_log() is called from main(), never at import — importing the
    module as a library (or running these tests) writes no files."""
    monkeypatch.setattr(sidecar, "_FILE_LOGGER", None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert not (tmp_path / "Clawdmeter").exists()
    sidecar.enable_file_log()
    if sys.platform == "win32":
        assert (tmp_path / "Clawdmeter" / "sessions.log").exists()
        for handler in sidecar._FILE_LOGGER.handlers:
            handler.close()
        sidecar._FILE_LOGGER = None
    else:
        assert not (tmp_path / "Clawdmeter").exists()


@windows_only
def test_windows_sidecar_config_prefers_the_windows_daemon_location(monkeypatch, tmp_path):
    """hook_port belongs in the one config the Windows daemon already uses."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    win_config = tmp_path / "Clawdmeter" / "config"
    win_config.parent.mkdir(parents=True)
    win_config.write_text("hook_port = 45999\n", encoding="utf-8")
    assert sidecar.default_config_file() == str(win_config)
    assert sidecar.read_config_value("hook_port", str(win_config)) == "45999"


@windows_only
def test_windows_sidecar_config_falls_back_to_the_posix_path(monkeypatch, tmp_path):
    """A config carried over from a Linux box keeps working."""
    home = tmp_path / "home"
    posix = home / ".config" / "claude-usage-monitor" / "config"
    posix.parent.mkdir(parents=True)
    posix.write_text("hook_port = 45999\n", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(sidecar.os.path, "expanduser", lambda p: p.replace("~", str(home)))
    assert sidecar.default_config_file() == str(posix)


def test_windows_sidecar_signals_exist_on_this_platform():
    """main() installs handlers for these; SIGTERM and SIGINT exist on Windows
    (SIGBREAK is Windows-only and picked up by name when present)."""
    import signal
    assert hasattr(signal, "SIGTERM") and hasattr(signal, "SIGINT")
    if sys.platform == "win32":
        assert hasattr(signal, "SIGBREAK")


# ---------------------------------------------------------------------------
# End to end: the sidecar's file is exactly what the daemon ships
# ---------------------------------------------------------------------------

def test_windows_sessions_sidecar_file_round_trips_to_the_daemon(tmp_path, monkeypatch):
    """The real sidecar writer feeding the real daemon reader: whatever the
    fitter produced arrives at write_gatt_char unchanged."""
    import daemon.claude_usage_daemon_windows as mod
    table = sidecar.SessionTable(config_dirs=[])
    table.handle_event({"hook_event_name": "SessionStart",
                        "session_id": "a3f10c2e-0000-4000-8000-000000000001",
                        "cwd": r"c:\Users\dev\Clawdmeter"})
    table.handle_event({"hook_event_name": "UserPromptSubmit",
                        "session_id": "a3f10c2e-0000-4000-8000-000000000001"})
    payload = table.project(sidecar.DEFAULT_BUDGET_BYTES)

    path = tmp_path / "sessions.json"
    sidecar.write_sessions_file(str(path), payload)
    monkeypatch.setattr(mod, "SESSIONS_FILE", path)

    client = fake_client()
    run_ticks(Session(client))

    assert ss_writes(client) == [(SS_CHAR_UUID, payload.encode("utf-8"))]
    assert json.loads(payload)["ss"][0][1] == "Clawdmeter"   # label from the cwd


# ---------------------------------------------------------------------------
# Autostart: the sidecar is a separate, opt-in Run value
# ---------------------------------------------------------------------------

def _winreg_mock(*, query_raises=False):
    """A stand-in for the winreg module (same shape as test_windows_autostart)."""
    winreg = MagicMock()
    winreg.HKEY_CURRENT_USER = "HKEY_CURRENT_USER"
    winreg.KEY_SET_VALUE = 0x0002
    winreg.KEY_QUERY_VALUE = 0x0001
    winreg.REG_SZ = 1
    key = MagicMock()
    key.__enter__ = MagicMock(return_value=key)
    key.__exit__ = MagicMock(return_value=False)
    winreg.OpenKey = MagicMock(return_value=key)
    winreg.QueryValueEx = MagicMock(
        side_effect=FileNotFoundError("not found") if query_raises else None,
        return_value=None if query_raises else ("cmd", 1),
    )
    return winreg, key


def test_windows_sessions_autostart_uses_its_own_run_value():
    """Turning live sessions on must not touch the tray's autostart, and vice
    versa — two independent opt-ins, two registry values."""
    winreg, key = _winreg_mock()
    captured = []
    winreg.SetValueEx = MagicMock(
        side_effect=lambda k, name, reserved, kind, value: captured.append((name, value))
    )

    with patch("daemon.autostart_windows.winreg", winreg):
        import daemon.autostart_windows as auto
        auto.enable_sessions()

    (name, cmd), = captured
    assert name == "ClawdmeterSessions"
    assert name != auto._VALUE_NAME
    assert "clawdmeter_sessions.py" in cmd
    assert "pythonw.exe" in cmd          # headless: no console window at logon
    assert cmd.startswith('"')           # quoted for paths with spaces


def test_windows_sessions_autostart_is_off_until_asked_for():
    """is_sessions_enabled() reads the live registry; absent value = off."""
    winreg, _key = _winreg_mock(query_raises=True)
    with patch("daemon.autostart_windows.winreg", winreg):
        import daemon.autostart_windows as auto
        assert auto.is_sessions_enabled() is False
    assert winreg.QueryValueEx.call_args[0][1] == "ClawdmeterSessions"


def test_windows_sessions_autostart_disable_is_idempotent():
    """Removing an already-absent value is not an error."""
    winreg, _key = _winreg_mock()
    winreg.DeleteValue = MagicMock(side_effect=FileNotFoundError("not found"))
    with patch("daemon.autostart_windows.winreg", winreg):
        import daemon.autostart_windows as auto
        auto.disable_sessions()          # must not raise
    assert winreg.DeleteValue.call_args[0][1] == "ClawdmeterSessions"


def test_windows_sessions_autostart_passes_no_port():
    """`hook_port` in the config is the single source of truth — baking a port
    into the Run value would go stale the moment the config changed."""
    import daemon.autostart_windows as auto
    cmd = auto._sessions_command()
    assert "--port" not in cmd


def test_windows_sessions_autostart_defaults_to_the_shipped_sidecar():
    """No argument = the clawdmeter_sessions.py next to autostart_windows.py."""
    import daemon.autostart_windows as auto
    cmd = auto._sessions_command()
    expected = os.path.join(os.path.dirname(os.path.abspath(auto.__file__)),
                            "clawdmeter_sessions.py")
    assert os.path.normcase(expected) in os.path.normcase(cmd)
