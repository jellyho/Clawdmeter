"""Keeps the daemon suite hermetic with respect to the session sidecar.

The Windows daemon now reads the sidecar's handoff file (~/.clawdmeter/sessions.json)
on every poll tick. On a developer machine that actually runs the sidecar, that
real file would leak into tests that know nothing about sessions — an extra GATT
write appearing in unrelated write assertions. Point the constant at a path that
cannot exist unless a test says otherwise.

The import is guarded: the Windows daemon needs httpx/bleak, which the Linux CI
box does not necessarily have, and a conftest that raises would take the whole
directory's collection down with it.
"""

import pytest

try:
    import daemon.claude_usage_daemon_windows as win_mod
except Exception:  # pragma: no cover - httpx/bleak absent
    win_mod = None

# Captured before any test can patch it, so a test can still assert on the
# real default (the path the sidecar and the daemon have to agree on).
ORIGINAL_SESSIONS_FILE = win_mod.SESSIONS_FILE if win_mod is not None else None


@pytest.fixture
def real_sessions_file():
    """The daemon's un-patched SESSIONS_FILE default."""
    return ORIGINAL_SESSIONS_FILE


@pytest.fixture(autouse=True)
def _isolate_sessions_file(tmp_path, monkeypatch):
    if win_mod is not None:
        monkeypatch.setattr(win_mod, "SESSIONS_FILE", tmp_path / "no-sessions.json")


try:
    import daemon.clawdmeter_fleet as fleet_mod
except Exception:  # pragma: no cover - defensive, same reason as above
    fleet_mod = None


@pytest.fixture(autouse=True)
def _isolate_fleet_heartbeat(tmp_path, monkeypatch):
    """Same hazard as the handoff file, one door along.

    run_loop() stamps a liveness file the tray supervisor reads, and its real
    path is %LOCALAPPDATA%\\Clawdmeter\\fleet.heartbeat. On a machine that
    actually runs the poller, a test exercising run_loop would stamp over the
    live one -- telling the supervisor a dead poller was healthy, which is the
    precise lie the heartbeat exists to prevent.
    """
    if fleet_mod is not None:
        beat = tmp_path / "fleet.heartbeat"
        monkeypatch.setattr(fleet_mod, "heartbeat_path",
                            lambda base=None: str(base or beat))


try:
    import daemon.clawdmeter_sessions as cs_mod
except Exception:  # pragma: no cover - defensive, same reason as above
    cs_mod = None


@pytest.fixture(autouse=True)
def _no_real_log_files(monkeypatch):
    r"""Keep the suite out of the owner's real logs.

    The Windows daemon builds its rotating file handler AT IMPORT
    (`_FILE_LOGGER = _build_file_logger()`), so merely importing it in a test
    attaches a writer to %LOCALAPPDATA%\Clawdmeter\daemon.log — and every
    log() a test provokes lands there, timestamped, indistinguishable from the
    real thing.

    That is not a tidiness problem. Diagnosing a live failure during this
    project meant reading that file, and three times the trail ran into test
    output: connection attempts to AA:BB:CC:DD:EE:FF, rounds that never
    happened, "Device not found" from a daemon that was connected. A log you
    have to second-guess is worse than no log.

    log() still prints, so capsys assertions are untouched; only the file
    handler goes away.
    """
    if win_mod is not None:
        monkeypatch.setattr(win_mod, "_FILE_LOGGER", None, raising=False)
    if cs_mod is not None:
        # enable_file_log() is NOT stubbed: it is called only from main(), it
        # honours LOCALAPPDATA, and one test legitimately exercises it against
        # a tmp path. Nulling the handler is enough -- that is where the
        # pollution came from.
        monkeypatch.setattr(cs_mod, "_FILE_LOGGER", None, raising=False)
