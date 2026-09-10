#!/usr/bin/env python3
"""Windows system-tray entry and state bridge for Clawdmeter — APP-01.

Provides:
  TrayState   — thread-safe scalar bridge (daemon loop writes, tray reads)
  header_text — pure helper producing the D-05 status-header string
  FleetSupervisor — notices the remote-fleet poller died and starts it again
  main()      — tray entry: builds per-state icons, runs the daemon loop in a
                bg thread, supervises the fleet poller, and runs pystray.Icon
                on the main thread

The daemon loop (claude_usage_daemon_windows.main) is UNCHANGED in logic;
this module injects only additive state-setter calls at existing branch points.

Usage::

    python tray_windows.py

Run: python -m pytest daemon/tests/test_windows_tray.py -x -q
"""

import os
import subprocess
import sys
import threading
import time

# Repo root = the directory that CONTAINS the `daemon` package (this file is
# <repo>/daemon/tray_windows.py). Resolve it from __file__ so the package
# imports below and the brand-logo asset load work no matter what the current
# working directory is — critical for logon autostart, where the HKCU\Run entry
# starts with cwd = System32, not the repo (APP-01 / SC#1).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Autostart launches us with the BASE interpreter's pythonw.exe, not the venv's
# (see autostart_windows._command — the venv pythonw redirector pops a console
# window). The base interpreter does NOT see the venv's site-packages, so add
# them here to resolve pystray/bleak/PIL. os.path.isdir guards the no-venv and
# already-inside-venv cases; site.addsitedir is a no-op on a missing dir anyway.
_VENV_SITE = os.path.join(_REPO_ROOT, ".venv", "Lib", "site-packages")
if os.path.isdir(_VENV_SITE):
    import site
    site.addsitedir(_VENV_SITE)

# ---------------------------------------------------------------------------
# TrayState — thread-safe scalar bridge (loop -> tray)
# ---------------------------------------------------------------------------

class TrayState:
    """Shared state object bridging the daemon asyncio loop to the tray.

    The daemon loop writes state via the set_* methods; the tray reads the
    scalar attributes.  No lock is needed — writes are atomic attribute
    assignments of simple Python scalars, and the tray only ever reads them.

    The loop populates `loop` and `stop_event` at startup (inside
    daemon_main()) so the tray's Quit handler can route through
    loop.call_soon_threadsafe (RESEARCH Pitfall 2 / Anti-Pattern).
    """

    def __init__(self) -> None:
        self.state: str = "scanning"       # "connected" | "scanning" | "error"
        self.reason: str = ""              # error reason string (D-04)
        self.last_sync: float | None = None  # time.time() of last successful write

        # Populated by daemon main() at startup:
        self.loop = None        # asyncio running loop (for call_soon_threadsafe)
        self.stop_event = None  # asyncio.Event (the existing clean-shutdown hook)

    def set_connected(self, ts: float) -> None:
        """Called after write_payload returns True.  ts = time.time()."""
        self.state = "connected"
        self.reason = ""
        self.last_sync = ts

    def set_scanning(self) -> None:
        """Called in scan/reconnect branches.  BLE churn stays Scanning (D-01)."""
        self.state = "scanning"
        self.reason = ""

    def set_error(self, why: str) -> None:
        """Called on token-expired / API auth failure (D-01 Error = actionable only)."""
        self.state = "error"
        self.reason = why


# ---------------------------------------------------------------------------
# header_text — pure D-05 status header string
# ---------------------------------------------------------------------------

def header_text(ts: TrayState) -> str:
    """Return the D-05 menu status-header string for the current TrayState.

    Shapes:
      "Connected · last update HH:MM"  (ts.last_sync is a float)
      "Connected · last update never"  (ts.last_sync is None)
      "Scanning…"
      "Error: {reason}"
    """
    if ts.state == "connected":
        if ts.last_sync is not None:
            when = time.strftime("%H:%M", time.localtime(ts.last_sync))
        else:
            when = "never"
        return f"Connected · last update {when}"
    if ts.state == "scanning":
        return "Scanning…"   # "Scanning…"
    return f"Error: {ts.reason}"


# ---------------------------------------------------------------------------
# FleetSupervisor — the remote-fleet poller, watched across a process boundary
# ---------------------------------------------------------------------------

# How often to look. The poller stamps its heartbeat once per 30 s listing
# poll, so checking on the same cadence costs nothing and still catches a death
# inside a couple of minutes.
FLEET_CHECK_S = 30


class Supervisor:
    r"""Notice that a background piece is gone, say so, and start it again.

    Written for the remote-fleet poller and now also carrying the report mail
    drop, which is why every seam -- armed, alive, launch, and the NOUN in the
    messages -- is injected. Two things that die the same way and are noticed
    the same way should not be two mechanisms; the second one would be the one
    nobody maintains.

    The tray already supervises its own daemon loop (see _run_daemon: catch,
    log, flip to an actionable error, restart with capped backoff). This is the
    same treatment for a process the tray does not own — because on the day
    this was written the fleet poller died mid-afternoon and NOTHING noticed:
    the device kept showing the list it had captured nine hours earlier, and
    the only reason anybody found out was that the list looked wrong.

    It watches a HEARTBEAT rather than a child-process handle, and that choice
    is the whole design. The poller may have been started by its HKCU\Run entry
    at logon, by the installer, or by hand from a terminal; a supervisor that
    only knew about children it had spawned itself would have been watching
    nothing at all in exactly the case that needed it. The question asked here
    is "is A poller alive", not "is MY poller alive".

    Two things keep it from being a nuisance:

      * it is ARMED ONLY when the owner has enabled the poller's autostart
        entry (autostart.is_fleet_enabled), so it turns nothing on that is off
        today and it stops trying the moment the owner turns the feature off;
      * the poller holds a named single-instance mutex, so a relaunch that
        races a poller which was merely slow is a no-op — the second copy
        exits instead of becoming a second producer of the handoff file.

    Every seam is injectable so the whole thing is testable without a registry,
    a heartbeat file or a process.
    """

    IDLE_BACKOFF_S = 5      # first retry, and the value reset() returns to
    MAX_BACKOFF_S = 300     # a poller that will not start must not be a spinner

    def __init__(self, is_enabled=None, is_alive=None, launch=None,
                 log_fn=None, notify=None, now_fn=time.time,
                 label="Fleet poller"):
        self._is_enabled = is_enabled or _fleet_autostart_enabled
        self._is_alive = is_alive or _fleet_is_alive
        self._launch = launch or launch_fleet_poller
        self._label = label
        self._log = log_fn or (lambda msg: None)
        self._notify = notify
        self._now = now_fn
        self.backoff = self.IDLE_BACKOFF_S
        self.next_try = 0.0
        self.down = False       # currently believed dead (one notice per outage)

    def _reset(self) -> None:
        self.backoff = self.IDLE_BACKOFF_S
        self.next_try = 0.0
        self.down = False

    def step(self, now=None) -> bool:
        """One check. True when this call started a poller.

        Total by construction: a supervisor that raises is a supervisor that
        stops supervising, which is the failure it exists to prevent.
        """
        now = self._now() if now is None else now
        try:
            if not self._is_enabled():
                self._reset()
                return False
            if self._is_alive(now):
                if self.down:
                    self._log(f"{self._label} is alive again")
                self._reset()
                return False
            if not self.down:
                # One notice per outage, on the falling edge — the same rule
                # the error toast follows (D-04: transitions, not ticks).
                self.down = True
                self._log(f"{self._label} is not running")
                if self._notify is not None:
                    try:
                        self._notify(f"{self._label} stopped — restarting it",
                                     "Clawdmeter")
                    except Exception:
                        pass
            if now < self.next_try:
                return False
            self.next_try = now + self.backoff
            self.backoff = min(self.backoff * 2, self.MAX_BACKOFF_S)
            self._log(f"Starting the {self._label.lower()}")
            self._launch()
            return True
        except Exception as e:          # last-resort guard, as above
            self._log(f"{self._label} supervisor error: {e!r}")
            return False


def _fleet_autostart_enabled() -> bool:
    """Default arming test: has the owner asked for a poller at logon?"""
    import daemon.autostart_windows as autostart
    return autostart.is_fleet_enabled()


def _fleet_is_alive(now=None) -> bool:
    """Default liveness test: did a poller stamp its heartbeat recently?"""
    import daemon.clawdmeter_fleet as fleet
    return fleet.is_alive(now=now)


def _maildrop_autostart_enabled() -> bool:
    """Armed only when the owner asked for a mail drop to be kept up.

    Same rule as the poller's: a supervisor must not turn on a feature that is
    off today. `report_maildrop_autostart = on` is the switch, and it is what
    --create-maildrop already consults.
    """
    import daemon.clawdmeter_report as report
    return report.maildrop_autostart_from_config()


def _maildrop_is_alive(now=None) -> bool:
    """Is a session with the mail drop's name running on this machine?

    Not a heartbeat: the mail drop is a Claude Code session, not a process this
    project writes, so the roster it registers itself in IS the heartbeat. This
    is the same question ensure_maildrop() asks before every round, which is
    what makes a green answer here mean a round will actually dispatch.
    """
    del now
    import daemon.clawdmeter_report as report
    name = report.maildrop_from_config()
    return report.find_maildrop(name, report.live_local_sessions()) is not None


def launch_maildrop() -> None:
    """Start the mail drop, the same way --create-maildrop does.

    Blocking, and that is fine: it runs on the supervisor thread, which has
    nothing else to do for the next 30 seconds. It is also why the launch is
    behind the backoff -- a mail drop that cannot start must not be attempted
    every half minute forever.
    """
    import daemon.clawdmeter_report as report
    name = report.maildrop_from_config()
    rec, action, why = report.ensure_maildrop(name, create=True)
    if rec is None:
        raise RuntimeError(why or "could not start the mail drop")


def launch_fleet_poller() -> None:
    r"""Start the poller with the same command its Run value uses.

    Deliberately not a hand-rolled command line: a supervisor that guessed at
    the interpreter or the script path would drift from the autostart entry the
    first time either moved, and would then be restarting the wrong thing (or
    nothing) exactly when it mattered.

    DETACHED_PROCESS because the poller is not really the tray's child — it has
    its own autostart entry and its own lifetime, and it must outlive a tray
    restart. That leaves it with no usable stderr, which is why its log() is
    guarded and mirrored into %LOCALAPPDATA%\Clawdmeter\fleet.log.
    """
    import daemon.autostart_windows as autostart
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(autostart.fleet_command(), close_fds=True, **kwargs)


# ---------------------------------------------------------------------------
# single-instance guard (named kernel mutex — no stale-lock problem)
# ---------------------------------------------------------------------------
# NOTE: autostart_windows.acquire_single_instance() is the same mechanism,
# generalised over a name so the fleet poller can take a lock of its own. This
# copy stays because its unit tests patch THIS module's `sys` and `ctypes`
# seams; merging them would break a passing suite to save twenty lines.

# Per-session mutex name. "Local\\" scopes it to the interactive logon, which is
# exactly the granularity we want: one tray per signed-in user. Both the headless
# autostart (HKCU\Run pythonw) and an ARSO-restored console instance live in the
# same session, so this name catches the duplicate-launch collision that produced
# the "mystery console window fighting the headless tray over BLE" field bug.
_SINGLETON_MUTEX_NAME = "Local\\Clawdmeter-tray-singleton"
_ERROR_ALREADY_EXISTS = 183


def _acquire_single_instance():
    """Acquire the process-wide single-instance lock.

    Returns a truthy handle to keep alive for the process lifetime if this is
    the first/only tray, or None if another Clawdmeter tray already owns the
    lock (the caller must then exit immediately, before touching BLE).

    Uses a named kernel mutex: Windows releases it automatically when the owning
    process dies, so there is no stale-lock cleanup (unlike a pidfile). We never
    CloseHandle it — the handle lives until process exit, which is precisely the
    lock lifetime we want.

    Off-Windows (Linux dev box / unit tests) this is a no-op that always
    succeeds — the tray only ever runs on Windows, and the dev box must stay
    importable for the pure-helper tests.
    """
    if sys.platform != "win32":
        return object()  # no-op sentinel; never blocks off-Windows

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]

    handle = kernel32.CreateMutexW(None, True, _SINGLETON_MUTEX_NAME)
    if not handle:
        # Couldn't create the mutex at all — fail OPEN so a kernel quirk never
        # stops the tray from starting; single-instance is best-effort hardening.
        return object()
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        return None  # another instance already holds it
    return handle


# ---------------------------------------------------------------------------
# main() — tray entry (pystray on main thread, daemon loop in bg thread)
# ---------------------------------------------------------------------------

def main() -> None:
    """Tray entry point: build icons, start daemon bg thread, run pystray.

    `import pystray` is intentionally INSIDE this function (not at module top)
    so the module can be imported on a GTK-less Linux dev box for unit tests
    of the pure helpers (TrayState, header_text) without pystray failing.
    """
    # Single-instance guard FIRST — before icons, the daemon thread, or any BLE
    # work. If another tray already owns the session mutex (e.g. ARSO restored a
    # console instance and the headless autostart also fired), exit silently.
    # Under pythonw there is no console to print to, so this is a quiet return.
    _instance_lock = _acquire_single_instance()
    if _instance_lock is None:
        return

    import asyncio as _asyncio
    import pystray
    from pystray import Menu, MenuItem

    import daemon.autostart_windows as autostart
    from daemon.claude_usage_daemon_windows import main as daemon_main, log as daemon_log
    from daemon.icon_assets import load_logo_rgba, build_state_icons

    # Build per-state icons once at startup; swap icon.icon per tick (never recomposite).
    base = load_logo_rgba(os.path.join(_REPO_ROOT, "firmware", "src", "logo.h"))
    images = build_state_icons(base)

    ts = TrayState()
    icon = pystray.Icon("Clawdmeter", images["scanning"], "Clawdmeter")

    # Set by the Quit handler so the supervisor below knows a clean stop was
    # requested and must NOT resurrect the loop.
    _quit_requested = threading.Event()

    # --- background thread: asyncio loop (supervised, auto-restart on crash) ---
    def _run_daemon() -> None:
        # A crash used to END this daemon=True thread for good: polling stopped
        # silently and the tray froze on its last state until the user manually
        # relaunched (the field "frozen tray" / "sticky error" mode — one transient
        # bleak assert turned into an outage). Now we SUPERVISE it: log the
        # traceback, flip the tray to an actionable error, then restart the loop
        # with capped backoff. A CLEAN return means Quit was requested (main()'s
        # loop exited on stop_event), so we stay down and do NOT restart.
        backoff = 2
        while not _quit_requested.is_set():
            try:
                _asyncio.run(daemon_main(tray_state=ts))
                return  # clean exit == Quit requested; stay down
            except Exception as e:  # last-resort thread guard
                import traceback
                daemon_log(f"Daemon thread crashed: {e!r}")
                daemon_log(traceback.format_exc())
                ts.set_error(f"daemon crashed: {type(e).__name__}")
            if _quit_requested.is_set():
                return
            daemon_log(f"Restarting daemon loop in {backoff}s after crash")
            # Interruptible sleep: a Quit during backoff wakes us immediately.
            if _quit_requested.wait(timeout=backoff):
                return
            backoff = min(backoff * 2, 30)

    daemon_thread = threading.Thread(target=_run_daemon, daemon=True)
    daemon_thread.start()

    # --- background thread: the fleet poller, watched across a process
    # boundary. Idle and harmless until the owner enables the poller's
    # autostart entry (nothing here turns that on).
    fleet_sup = Supervisor(log_fn=daemon_log, notify=icon.notify)
    # The mail drop dies the same way and was noticed the same way: not at
    # all, until a press produced 'no live local session named
    # clawdmeter-inbox' and the round refused. Same supervisor, different
    # seams.
    drop_sup = Supervisor(log_fn=daemon_log, notify=icon.notify,
                          label="Mail drop",
                          is_enabled=_maildrop_autostart_enabled,
                          is_alive=_maildrop_is_alive,
                          launch=launch_maildrop)

    def _run_fleet_supervisor() -> None:
        while not _quit_requested.wait(timeout=FLEET_CHECK_S):
            fleet_sup.step()
            drop_sup.step()

    threading.Thread(target=_run_fleet_supervisor, daemon=True).start()

    # --- menu ---
    def _on_quit(icon_ref, _item) -> None:
        # NEVER call ts.stop_event.set() directly from the tray thread;
        # asyncio.Event is NOT thread-safe (RESEARCH Pitfall 2).
        #
        # After signalling, WAIT for the daemon thread to finish its graceful
        # shutdown (the loop's finally: client.disconnect()) BEFORE we stop the
        # icon and let the process exit. Without this join the daemon=True thread
        # is killed mid-flight, the peer never gets a clean GATT disconnect, and
        # the device sits frozen on stale data instead of returning to its waiting
        # screen (SC#3 field report). The timeout caps the block so Quit can never
        # hang if a WinRT disconnect wedges (rare) — we exit anyway as a fallback.
        #
        # Set _quit_requested FIRST so the supervisor loop in _run_daemon never
        # resurrects the daemon after we signal stop (Quit must be final).
        _quit_requested.set()
        if ts.loop is not None and ts.stop_event is not None:
            ts.loop.call_soon_threadsafe(ts.stop_event.set)
            daemon_thread.join(timeout=6.0)
        icon_ref.stop()

    def _on_toggle(_icon_ref, _item) -> None:
        if autostart.is_enabled():
            autostart.disable()
        else:
            # Pass THIS file explicitly — without it enable() defaults the Run
            # value to autostart_windows.py (which has no entry point and starts
            # nothing), silently breaking menu-enabled autostart.
            autostart.enable(tray_script=os.path.abspath(__file__))
        icon.update_menu()

    def _on_toggle_fleet(_icon_ref, _item) -> None:
        # The remote-fleet poller (daemon/FLEET.md) is a separate opt-in with a
        # separate Run value, so this never touches the tray's own autostart.
        # Enabling it also arms the supervisor above, which is the point: the
        # owner is saying "there should be a poller running", and that is
        # exactly the claim a supervisor needs in order to act on it.
        if autostart.is_fleet_enabled():
            autostart.disable_fleet()
        else:
            autostart.enable_fleet()
            fleet_sup.step()      # start one now, not at the next logon
        icon.update_menu()

    icon.menu = Menu(
        # Non-clickable status header; text updates via update_menu() on state change.
        MenuItem(lambda _item: header_text(ts), None, enabled=False),
        # Start-at-login toggle: checked= is a CALLABLE for live query (Pitfall 6).
        MenuItem("Start at login", _on_toggle, checked=lambda _item: autostart.is_enabled()),
        MenuItem("Start fleet poller at login", _on_toggle_fleet,
                 checked=lambda _item: autostart.is_fleet_enabled()),
        MenuItem("Quit", _on_quit),
    )

    # --- setup callback (runs in pystray's setup thread, 1s poll) ---
    prev_state: dict = {"state": None, "last_sync": None}

    def _refresh(_icon: pystray.Icon) -> None:
        _icon.visible = True
        while _icon._running:  # type: ignore[attr-defined]
            current = ts.state
            last_sync = ts.last_sync
            state_changed = current != prev_state["state"]
            # Refresh the tooltip/menu when last_sync advances too — not only on
            # state change. A healthy "connected" daemon polling a flat usage
            # value never changes state, so a transition-only refresh froze the
            # "last update HH:MM" tooltip and read as a dead daemon (SC#2 field
            # report: device + tooltip both looked stuck while polling was fine).
            if state_changed or last_sync != prev_state["last_sync"]:
                if state_changed:
                    _icon.icon = images[current]  # icon image depends on state only
                _icon.title = header_text(ts)
                # D-04: toast ONLY on transition INTO error, not on every error tick.
                if current == "error" and prev_state["state"] != "error":
                    _icon.notify(ts.reason or "Clawdmeter error", "Clawdmeter")
                prev_state["state"] = current
                prev_state["last_sync"] = last_sync
                _icon.update_menu()
            time.sleep(1.0)

    # Blocks the main thread until icon.stop() is called from _on_quit.
    icon.run(setup=_refresh)


if __name__ == "__main__":
    main()
