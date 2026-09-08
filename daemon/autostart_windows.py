"""Login-autostart toggle for Clawdmeter — APP-01 / D-07.

Manages a per-user HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
registry value named "Clawdmeter" that launches the tray app headlessly via
pythonw.exe (no console window — D-08).

winreg is Windows stdlib; this module guards the import so it can be imported
on the Linux dev box (unit tests mock `daemon.autostart_windows.winreg`).

Public API:
  enable(tray_script=None)  -- write/overwrite the Run value
  disable()                 -- remove the Run value; idempotent when absent
  is_enabled()              -- True if the Run value is currently present

The session sidecar (issue #135, daemon/SESSIONS.md) gets its own Run value
under a separate name, so live session awareness can be turned on and off
without touching the tray autostart -- and stays OFF unless something asks for
it:

  enable_sessions(sidecar_script=None)
  disable_sessions()
  is_sessions_enabled()
"""

import os
import sys
import time

# Guard the import so the module is importable off-Windows.
# Unit tests replace this attribute via:
#   patch("daemon.autostart_windows.winreg", <MagicMock>)
try:
    import winreg as winreg  # type: ignore[import]
except ImportError:
    winreg = None  # type: ignore[assignment]

# Registry key (no leading backslash — OpenKey uses relative path under hive).
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "Clawdmeter"
# The session sidecar is a separate process with a separate opt-in, so it gets
# its own value: disabling one must never disable the other.
_SESSIONS_VALUE_NAME = "ClawdmeterSessions"


def log(msg: str) -> None:
    """Log in the daemon [HH:MM:SS] style."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _command(tray_script: str | None = None) -> str:
    """Build the headless launch command for the Run value.

    Uses the BASE interpreter's pythonw.exe — `sys.base_exec_prefix` points at
    the real Python install even inside a venv — NOT the venv's
    `Scripts\\pythonw.exe`.  The venv pythonw is a redirector stub that
    re-launches the CONSOLE `python.exe` build as a child process (a CPython
    venv-launcher bug, verified empirically on Python 3.13), which pops a black
    console window at logon and kills the tray when closed (field bug, SC#1).
    The base pythonw loads in-process and is genuinely windowless.

    The path is never hard-coded (D-08, CLAUDE.md "repoint ExecStart" lesson);
    both paths are quoted for space safety.  tray_windows.py adds the venv's
    site-packages to sys.path itself, so the venv's deps still resolve under the
    base interpreter.

    Args:
        tray_script: absolute path to the tray entry script.  Defaults to this
                     module's own path (useful when autostart_windows.py IS
                     the entry point, but callers should pass tray_windows.py).
    """
    pythonw = os.path.join(sys.base_exec_prefix, "pythonw.exe")
    script = os.path.abspath(tray_script if tray_script is not None else __file__)
    return f'"{pythonw}" "{script}"'


def _sessions_command(sidecar_script: str | None = None) -> str:
    """Build the headless launch command for the session sidecar.

    Same base-pythonw reasoning as _command().  The sidecar is stdlib-only, so
    unlike the tray it needs nothing from the venv.  No --port is passed on
    purpose: `hook_port` in the daemon config is the single source of truth, and
    an unset one makes the sidecar log why it is off and exit.
    """
    pythonw = os.path.join(sys.base_exec_prefix, "pythonw.exe")
    default = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "clawdmeter_sessions.py")
    script = os.path.abspath(sidecar_script if sidecar_script is not None else default)
    return f'"{pythonw}" "{script}"'


def _set_run_value(name: str, cmd: str) -> None:
    """Write (or overwrite) one HKCU Run value.  No admin elevation required —
    HKCU is per-user (D-07, ASVS V4)."""
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, cmd)


def _delete_run_value(name: str) -> bool:
    """Remove one HKCU Run value.  Idempotent — False when it was already gone.

    Mirrors the read_token() OSError-swallow pattern (daemon L201-208).
    """
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, name)
        return True
    except FileNotFoundError:
        return False  # already absent — idempotent


def _has_run_value(name: str) -> bool:
    """True if one HKCU Run value is currently present.

    Queries the live registry on every call so the state reflects external
    changes (e.g. the user deleting the value manually) — Pitfall 6 guard.
    """
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_QUERY_VALUE
        ) as key:
            winreg.QueryValueEx(key, name)
            return True
    except FileNotFoundError:
        return False


def enable(tray_script: str | None = None) -> None:
    """Write (or overwrite) the HKCU Run value pointing at pythonw.exe.

    Args:
        tray_script: path to the tray entry script (passed to _command()).
    """
    cmd = _command(tray_script)
    _set_run_value(_VALUE_NAME, cmd)
    log(f"Autostart enabled: {cmd}")


def disable() -> None:
    """Remove the HKCU Run value.  Idempotent — no error if already absent."""
    if _delete_run_value(_VALUE_NAME):
        log("Autostart disabled")


def is_enabled() -> bool:
    """Return True if the Run value is currently present, False otherwise."""
    return _has_run_value(_VALUE_NAME)


def enable_sessions(sidecar_script: str | None = None) -> None:
    """Start the session sidecar at logon (issue #135).  Opt-in: nothing calls
    this unless the user asked for live session awareness."""
    cmd = _sessions_command(sidecar_script)
    _set_run_value(_SESSIONS_VALUE_NAME, cmd)
    log(f"Session sidecar autostart enabled: {cmd}")


def disable_sessions() -> None:
    """Stop starting the session sidecar at logon.  Idempotent.  Leaves the
    installed Claude Code hooks alone — they POST to a port nobody is listening
    on, which is a no-op for the session (the hooks are async, 5 s timeout)."""
    if _delete_run_value(_SESSIONS_VALUE_NAME):
        log("Session sidecar autostart disabled")


def is_sessions_enabled() -> bool:
    """True if the session sidecar is registered to start at logon."""
    return _has_run_value(_SESSIONS_VALUE_NAME)
