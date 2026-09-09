#!/usr/bin/env python3
"""Unit tests for daemon/autostart_windows.py — APP-01.

Covers the winreg HKCU\\Run enable/disable/is_enabled login-autostart toggle.
winreg is NOT importable off-Windows; these tests patch it via
patch("daemon.autostart_windows.winreg", ...) so they run on any platform.

Run: python -m pytest daemon/tests/test_windows_autostart.py -x -q
"""
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers — build a fake winreg module with the attributes autostart_windows
# references.  Using a MagicMock as the module means all attribute accesses
# on it (HKEY_CURRENT_USER, KEY_SET_VALUE, etc.) automatically produce child
# MagicMocks, which is exactly what we want.
# ---------------------------------------------------------------------------

def _make_winreg_mock(*, query_raises=False):
    """Return a configured MagicMock that stands in for the winreg module."""
    winreg = MagicMock()

    # Constants — assign simple sentinel values so equality checks work.
    winreg.HKEY_CURRENT_USER = "HKEY_CURRENT_USER"
    winreg.KEY_SET_VALUE = 0x0002
    winreg.KEY_QUERY_VALUE = 0x0001
    winreg.REG_SZ = 1

    # OpenKey is used as a context manager; return a MagicMock key handle that
    # supports __enter__ / __exit__.
    key_handle = MagicMock()
    key_handle.__enter__ = MagicMock(return_value=key_handle)
    key_handle.__exit__ = MagicMock(return_value=False)
    winreg.OpenKey = MagicMock(return_value=key_handle)

    # QueryValueEx behaviour is configured by the caller.
    if query_raises:
        winreg.QueryValueEx = MagicMock(side_effect=FileNotFoundError("not found"))
    else:
        winreg.QueryValueEx = MagicMock(return_value=("some_command", 1))

    return winreg, key_handle


# ---------------------------------------------------------------------------
# test_enable_writes_run_value
# ---------------------------------------------------------------------------

def test_enable_writes_run_value():
    """enable() opens HKCU Run key with KEY_SET_VALUE and calls SetValueEx with
    value name 'Clawdmeter' and type REG_SZ."""
    winreg, key_handle = _make_winreg_mock()

    with patch("daemon.autostart_windows.winreg", winreg):
        import daemon.autostart_windows as mod
        mod.enable()

    # OpenKey must have been called with HKCU and the Run key path
    winreg.OpenKey.assert_called_once_with(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_SET_VALUE,
    )

    # SetValueEx must have been called with the correct value name and type
    winreg.SetValueEx.assert_called_once()
    args = winreg.SetValueEx.call_args[0]
    assert args[0] is key_handle, "SetValueEx first arg must be the opened key handle"
    assert args[1] == "Clawdmeter", "Value name must be 'Clawdmeter'"
    assert args[3] == winreg.REG_SZ, "Value type must be REG_SZ"


# ---------------------------------------------------------------------------
# test_command_uses_pythonw
# ---------------------------------------------------------------------------

def test_command_uses_pythonw():
    """The command string written by enable() contains 'pythonw.exe', does NOT
    contain a bare 'python.exe' token (D-08, no console), and is quoted (starts
    with a double-quote character)."""
    winreg, key_handle = _make_winreg_mock()

    captured_commands = []

    def capture_set_value_ex(key, name, reserved, reg_type, value):
        captured_commands.append(value)

    winreg.SetValueEx = MagicMock(side_effect=capture_set_value_ex)

    with patch("daemon.autostart_windows.winreg", winreg):
        import daemon.autostart_windows as mod
        mod.enable()

    assert len(captured_commands) == 1, "SetValueEx must have been called exactly once"
    cmd = captured_commands[0]

    # Must reference pythonw.exe (D-08)
    assert "pythonw.exe" in cmd, f"Command must contain 'pythonw.exe'; got: {cmd!r}"

    # Must NOT contain a bare 'python.exe' (without the 'w') as a standalone token
    # A command like '"...pythonw.exe" ...' is fine; '"...python.exe" ...' is not.
    import re
    assert not re.search(r'(?<![a-z])python\.exe', cmd), (
        f"Command must not reference a bare 'python.exe'; got: {cmd!r}"
    )

    # Must start with a double-quote (paths are quoted for space safety)
    assert cmd.startswith('"'), (
        f"Command must start with '\"' (quoted path); got: {cmd!r}"
    )


# ---------------------------------------------------------------------------
# test_disable_idempotent
# ---------------------------------------------------------------------------

def test_disable_idempotent():
    """disable() calls DeleteValue; when DeleteValue raises FileNotFoundError,
    disable() swallows it and returns without raising (idempotent-on-missing)."""
    winreg, key_handle = _make_winreg_mock()
    winreg.DeleteValue = MagicMock(side_effect=FileNotFoundError("not found"))

    with patch("daemon.autostart_windows.winreg", winreg):
        import daemon.autostart_windows as mod
        # Must not raise even though DeleteValue raises FileNotFoundError
        mod.disable()  # no exception expected

    winreg.DeleteValue.assert_called_once()


def test_disable_calls_delete_value_with_correct_name():
    """disable() calls DeleteValue with the value name 'Clawdmeter'."""
    winreg, key_handle = _make_winreg_mock()
    winreg.DeleteValue = MagicMock()

    with patch("daemon.autostart_windows.winreg", winreg):
        import daemon.autostart_windows as mod
        mod.disable()

    winreg.DeleteValue.assert_called_once()
    args = winreg.DeleteValue.call_args[0]
    assert args[1] == "Clawdmeter", f"DeleteValue must target 'Clawdmeter'; got {args[1]!r}"


# ---------------------------------------------------------------------------
# test_is_enabled
# ---------------------------------------------------------------------------

def test_is_enabled_true_when_value_present():
    """is_enabled() returns True when QueryValueEx succeeds (value is present)."""
    winreg, key_handle = _make_winreg_mock(query_raises=False)

    with patch("daemon.autostart_windows.winreg", winreg):
        import daemon.autostart_windows as mod
        result = mod.is_enabled()

    assert result is True, "is_enabled() must return True when QueryValueEx succeeds"


def test_is_enabled_false_when_value_absent():
    """is_enabled() returns False when QueryValueEx raises FileNotFoundError."""
    winreg, key_handle = _make_winreg_mock(query_raises=True)

    with patch("daemon.autostart_windows.winreg", winreg):
        import daemon.autostart_windows as mod
        result = mod.is_enabled()

    assert result is False, "is_enabled() must return False when QueryValueEx raises FileNotFoundError"


# ---------------------------------------------------------------------------
# The remote-fleet poller's own Run value (daemon/FLEET.md)
# ---------------------------------------------------------------------------
# It died mid-afternoon and nothing started it again, so the device showed a
# nine-hour-old list. Autostart covers the reboot; the tray's FleetSupervisor
# covers the death. A THIRD value name, because the poller, the hook sidecar
# and the tray are three independent opt-ins and turning one off must never
# turn another off.


def test_enable_fleet_writes_its_own_run_value():
    winreg, key_handle = _make_winreg_mock()

    with patch("daemon.autostart_windows.winreg", winreg):
        import daemon.autostart_windows as mod
        mod.enable_fleet()

    winreg.SetValueEx.assert_called_once()
    args = winreg.SetValueEx.call_args[0]
    assert args[1] == "ClawdmeterFleet"
    assert args[3] == winreg.REG_SZ


def test_fleet_command_points_at_the_poller_under_pythonw():
    import re
    import daemon.autostart_windows as mod
    cmd = mod.fleet_command()
    assert "pythonw.exe" in cmd
    assert not re.search(r"(?<![a-z])python\.exe", cmd)
    assert cmd.startswith('"')
    assert "clawdmeter_fleet.py" in cmd


def test_fleet_command_is_what_enable_fleet_writes():
    """The supervisor relaunches with fleet_command(); if that drifted from the
    Run value, a restart would start the wrong thing exactly when it mattered."""
    winreg, key_handle = _make_winreg_mock()
    captured = []
    winreg.SetValueEx = MagicMock(
        side_effect=lambda k, n, r, t, v: captured.append(v))

    with patch("daemon.autostart_windows.winreg", winreg):
        import daemon.autostart_windows as mod
        mod.enable_fleet()
        assert captured == [mod.fleet_command()]


def test_disable_fleet_is_idempotent_and_targets_only_its_own_value():
    winreg, key_handle = _make_winreg_mock()
    winreg.DeleteValue = MagicMock(side_effect=FileNotFoundError("not found"))

    with patch("daemon.autostart_windows.winreg", winreg):
        import daemon.autostart_windows as mod
        mod.disable_fleet()          # must not raise

    assert winreg.DeleteValue.call_args[0][1] == "ClawdmeterFleet"


def test_is_fleet_enabled_reads_the_live_registry():
    winreg, key_handle = _make_winreg_mock(query_raises=True)
    with patch("daemon.autostart_windows.winreg", winreg):
        import daemon.autostart_windows as mod
        assert mod.is_fleet_enabled() is False
    assert winreg.QueryValueEx.call_args[0][1] == "ClawdmeterFleet"

    winreg, key_handle = _make_winreg_mock(query_raises=False)
    with patch("daemon.autostart_windows.winreg", winreg):
        import daemon.autostart_windows as mod
        assert mod.is_fleet_enabled() is True


@pytest.mark.parametrize("fn,name", [
    ("enable", "Clawdmeter"),
    ("enable_sessions", "ClawdmeterSessions"),
    ("enable_fleet", "ClawdmeterFleet"),
])
def test_the_three_autostarts_never_share_a_value_name(fn, name):
    winreg, key_handle = _make_winreg_mock()
    with patch("daemon.autostart_windows.winreg", winreg):
        import daemon.autostart_windows as mod
        getattr(mod, fn)()
    assert winreg.SetValueEx.call_args[0][1] == name


# ---------------------------------------------------------------------------
# acquire_single_instance — the other half of an autostart entry
# ---------------------------------------------------------------------------
# A Run value plus a supervisor that may relaunch means the same program can be
# asked to start twice, and two fleet pollers would fight over
# ~/.clawdmeter/sessions.json (FLEET.md: run one producer at a time).

_ERROR_ALREADY_EXISTS = 183


def _fake_kernel32(last_error, handle):
    fake_kernel32 = MagicMock()
    fake_kernel32.CreateMutexW.return_value = handle
    fake_ctypes = MagicMock()
    fake_ctypes.WinDLL.return_value = fake_kernel32
    fake_ctypes.get_last_error.return_value = last_error
    return fake_ctypes


def test_single_instance_is_a_noop_off_windows():
    with patch("daemon.autostart_windows.sys") as mock_sys:
        mock_sys.platform = "linux"
        import daemon.autostart_windows as mod
        assert mod.acquire_single_instance("Local\\x") is not None


def test_first_instance_gets_the_handle():
    fake = _fake_kernel32(0, 0xBEEF)
    with patch("daemon.autostart_windows.sys") as mock_sys, \
         patch.dict("sys.modules", {"ctypes": fake, "ctypes.wintypes": MagicMock()}):
        mock_sys.platform = "win32"
        import daemon.autostart_windows as mod
        assert mod.acquire_single_instance("Local\\x") == 0xBEEF


def test_second_instance_is_told_to_leave():
    fake = _fake_kernel32(_ERROR_ALREADY_EXISTS, 0xBEEF)
    with patch("daemon.autostart_windows.sys") as mock_sys, \
         patch.dict("sys.modules", {"ctypes": fake, "ctypes.wintypes": MagicMock()}):
        mock_sys.platform = "win32"
        import daemon.autostart_windows as mod
        assert mod.acquire_single_instance("Local\\x") is None


def test_a_kernel_quirk_fails_open():
    """Single-instance is hardening, not correctness: never block a start."""
    fake = _fake_kernel32(_ERROR_ALREADY_EXISTS, 0)
    with patch("daemon.autostart_windows.sys") as mock_sys, \
         patch.dict("sys.modules", {"ctypes": fake, "ctypes.wintypes": MagicMock()}):
        mock_sys.platform = "win32"
        import daemon.autostart_windows as mod
        assert mod.acquire_single_instance("Local\\x") is not None


def test_the_poller_uses_a_name_of_its_own():
    """Sharing the tray's mutex would make the tray and the poller exclude
    each other, which is the opposite of what either lock is for."""
    import daemon.clawdmeter_fleet as fleet
    import daemon.tray_windows as tray
    assert fleet.SINGLETON_MUTEX_NAME != tray._SINGLETON_MUTEX_NAME
