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
