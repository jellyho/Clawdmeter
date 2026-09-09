#!/usr/bin/env python3
"""Claude Usage Tracker Daemon — Windows (Phase 2).

Reads the Claude OAuth token from the native-Windows credentials path and
polls the Anthropic API for rate-limit utilization data. BLE glue added in
later plans.
"""

import asyncio
import calendar
import datetime
import json
import logging
import logging.handlers
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import httpx
from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

try:
    # bleak >= 0.22 raises this precise BleakError subclass when a characteristic
    # is absent from the peer's GATT table — the normal case here, since only
    # boards with BOARD_HAS_SESSION_VIEWS build the SS characteristic at all.
    from bleak.exc import BleakCharacteristicNotFoundError
except ImportError:  # pragma: no cover - bleak < 0.22
    class BleakCharacteristicNotFoundError(BleakError):
        """Never raised by older bleak (it uses a plain BleakError); defined so
        the except clause below stays valid. The proactive services probe in
        Session.probe_session_support() covers those versions."""

DEVICE_NAME = "Clawdmeter"
SERVICE_UUID = "4c41555a-4465-7669-6365-000000000001"
RX_CHAR_UUID = "4c41555a-4465-7669-6365-000000000002"
# The device's own outbound channel. It has notified {"ack":true}/{"err":true}
# since the first firmware and nothing ever subscribed, so those notifications
# went into the void. Button events ride the same characteristic, told apart by
# an integer "ev" key that the ack/nack traffic does not carry (firmware/src/
# ble.h is the contract). OPTIONAL in practice: a board running firmware older
# than the buttons subscribes fine and simply never notifies an event, which
# must read as "quiet", not as a fault.
TX_CHAR_UUID = "4c41555a-4465-7669-6365-000000000003"
REQ_CHAR_UUID = "4c41555a-4465-7669-6365-000000000004"
# Live session rows (issue #135). OPTIONAL on the device: the firmware only
# creates it when BOARD_HAS_SESSION_VIEWS is set, so on most boards it is simply
# absent and this daemon must stay quiet rather than error every tick.
SS_CHAR_UUID = "4c41555a-4465-7669-6365-000000000005"

# Device button events (firmware/src/ble.h). Codes are APPEND-ONLY -- they
# cross the BLE boundary, so a released code is never renumbered or reused, and
# a code this daemon does not know is ignored rather than guessed at.
EVENT_REPORT = 1    # "tell me what the fleet is doing"
EVENT_GO_AHEAD = 2  # {"ev":2,"sid":"g4"} -- the owner tapped a NEEDS-YOU card
EVENT_DISMISS = 3   # {"ev":3,"sid":"g4"} -- the owner tapped a card away
EVENT_BROADCAST = 4 # {"ev":4} -- Settings > Teach agents

# The report dispatcher, run as a child process. Its own --timeout bounds the
# `claude -p` spawn (120 s) and its follow phase watches for replies for another
# 90 s, so a healthy round is ~20 s of work inside a ~210 s process. This is the
# outer bound on the whole thing: past it the child is killed, because a
# dispatcher that never exits would block every later press for good.
REPORT_SCRIPT = Path(__file__).resolve().parent / "clawdmeter_report.py"
REPORT_ROUND_TIMEOUT = 300.0
# A courier sends one message and exits; it has no replies to wait for, so it
# gets a fraction of a round's budget.
GO_AHEAD_TIMEOUT = 120.0
# A broadcast talks to every reachable agent rather than the five a round is
# capped at, and each of them has to open a file and answer. Generous, and
# still bounded.
BROADCAST_TIMEOUT = 300.0

POLL_INTERVAL = 60
TICK = 5
CONNECT_RETRIES = 3        # D-01: attempts before giving up on a device
CONNECT_RETRY_DELAY = 2.0  # D-01: seconds between failed connect attempts
ZOMBIE_BREAK_LIMIT = 1     # D-03: consecutive write failures before abandoning a half-open link
                           # N=1: breaks at T=60s, leaves ~60s headroom for reconnect+poll inside 120s SLA
                           # N=2 would bust the 120s budget before reconnect even begins
RECONNECT_BACKOFF_CAP = 8  # D-05: fast-reconnect cap (seconds); keeps stacked retries inside 120s SLA
                           # ~5–10s band per CONTEXT.md Claude's Discretion; 8 chosen as middle ground

# Optional reset chime.
# Optional clock display. 
# Config lives under the same Clawdmeter dir as daemon.log.
CONFIG_FILE = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Clawdmeter" / "config"

# Live session awareness (issue #135; daemon/SESSIONS.md). The clawdmeter_sessions.py
# sidecar owns this file and rewrites it — atomically — only when the fitted wire
# payload actually changes. Home-relative on purpose: it is the one path the sidecar,
# the bash daemon and this daemon all agree on without any config. Absent =
# sidecar not installed = feature off, which is the default and must cost nothing.
SESSIONS_FILE = Path.home() / ".clawdmeter" / "sessions.json"
# Written here, read by the fleet poller. One writer, one reader, so there is
# no lock: this daemon is the only process that learns about a dismissal (it
# owns the BLE link) and the poller is the only one that renders rows.
DISMISS_FILE = Path.home() / ".clawdmeter" / "dismissed.json"
DISMISS_TTL = 3600.0    # matches fleet.DISMISS_TTL_S
DISMISS_KEEP = 64       # the panel shows six rows; this is generous

API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS_TEMPLATE = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}
API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}


def _build_file_logger() -> logging.Logger | None:
    """Create a rotating file logger for field diagnostics, or None.

    Autostart launches the tray under pythonw.exe, which has no console — stdout
    is discarded (and is in fact None, making print() unsafe). A rotating file is
    then the ONLY trail when the daemon stalls in the field. Windows-only: on the
    Linux dev box / CI the console print() suffices, and gating to win32 keeps the
    pure-helper unit tests from writing stray log files.
    """
    if sys.platform != "win32":
        return None
    logger = logging.getLogger("clawdmeter.daemon")
    if logger.handlers:
        return logger  # idempotent across re-import (tray imports this module)
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    path = base / "Clawdmeter" / "daemon.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
        )
    except OSError:
        return None  # best-effort — logging setup must never stop the daemon
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


_FILE_LOGGER = _build_file_logger()


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    # Under pythonw sys.stdout is None and print() would raise — guard it so a
    # missing console can never crash the daemon thread (the silent-freeze mode).
    try:
        print(line, flush=True)
    except (OSError, ValueError, AttributeError, RuntimeError):
        pass
    if _FILE_LOGGER is not None:
        _FILE_LOGGER.info(msg)


class AuthError(Exception):
    """Raised by poll_api on a genuine 401/403 — the token really is expired or
    invalid and the user must re-run `claude login`. Distinct from a None return,
    which means a TRANSIENT failure (network/DNS, timeout, rate-limit, 5xx) that
    must NOT be mislabeled as a token problem (SC#5: a boot-time `getaddrinfo
    failed` DNS blip wrongly fired the 'token expired' toast)."""

def _read_config_text():
    """The config file's text, or "" — never an exception.

    Decoded as utf-8-sig so a BOM (which Notepad adds by default) is stripped
    rather than fatal, with errors replaced so a stray byte in a comment can
    never crash a background daemon the user cannot see.
    """
    try:
        return CONFIG_FILE.read_text(encoding="utf-8-sig", errors="replace")
    except (OSError, ValueError):
        return ""


def read_chime_setting() -> str:
    """Read the `chime` option from the config file. One of: off|on.

    Defaults to "off" so the device stays silent until the user opts in.
    """
    try:
        if CONFIG_FILE.exists():
            # utf-8-sig, not the locale default: Notepad writes a BOM by
            # default, and on a non-UTF-8 locale (cp949 here) read_text()
            # crashes on those three bytes and takes the daemon with it.
            for line in _read_config_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip().lower() == "chime":
                    val = val.strip().lower()
                    if val in ("off", "on"):
                        return val
    except OSError:
        pass
    return "off"


def read_clock_setting() -> str:
    """Read the `clock` option from the config file. One of: off|auto|12|24.

    Defaults to "off" so existing setups keep showing "Usage" until opted in.
    """
    try:
        if CONFIG_FILE.exists():
            # utf-8-sig, not the locale default: Notepad writes a BOM by
            # default, and on a non-UTF-8 locale (cp949 here) read_text()
            # crashes on those three bytes and takes the daemon with it.
            for line in _read_config_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip().lower() == "clock":
                    val = val.strip().lower()
                    if val in ("off", "auto", "12", "24"):
                        return val
    except OSError:
        pass
    return "off"


def add_chime_field(payload: dict) -> None:
    """Add "c":1 to the payload when the config opts in, so the firmware may
    sound the session-reset chime. Omitted entirely when chime is off."""
    if read_chime_setting() == "on":
        payload["c"] = 1


def detect_hour_format() -> int:
    """Best-effort 12h/24h detection on Windows via the registry. Returns 12 or 24."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\International") as k:
            # iTime: "1" = 24-hour, "0" = 12-hour.
            val, _ = winreg.QueryValueEx(k, "iTime")
            return 24 if str(val).strip() == "1" else 12
    except (ImportError, OSError):
        return 24


def add_clock_fields(payload: dict) -> None:
    """Add "t" (local wall-clock epoch) + "tf" (12|24) when the config opts in."""
    clock = read_clock_setting()
    if clock == "off":
        return
    tf = 24 if clock == "24" else 12 if clock == "12" else detect_hour_format()
    payload["t"] = int(time.time()) + time.localtime().tm_gmtoff
    payload["tf"] = tf


async def poll_api(token: str) -> dict | None:
    headers = dict(API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.post(API_URL, headers=headers, json=API_BODY)
    except httpx.HTTPError as e:
        # Network/DNS/timeout — transient. Return None (no toast), retry next tick.
        log(f"API call failed: {e}")
        return None
    if resp.status_code in (401, 403):
        # Genuine auth rejection — the ONLY case that warrants the actionable
        # "run claude login" toast.
        log(f"API HTTP {resp.status_code}: {resp.text[:200]}")
        raise AuthError(resp.status_code)
    if resp.status_code >= 400:
        # Other 4xx/5xx (rate-limit, server error) — transient, not a token issue.
        log(f"API HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    def hdr(name: str, default: str = "0") -> str:
        return resp.headers.get(name, default)

    now = time.time()

    def reset_minutes(reset_ts: str) -> int:
        try:
            r = float(reset_ts)
        except ValueError:
            return 0
        mins = (r - now) / 60.0
        return int(round(mins)) if mins > 0 else 0

    def pct(util: str) -> int:
        try:
            return int(round(float(util) * 100))
        except ValueError:
            return 0

    if resp.headers.get("anthropic-ratelimit-unified-5h-utilization"):
        payload = {
            "s": pct(hdr("anthropic-ratelimit-unified-5h-utilization")),
            "sr": reset_minutes(hdr("anthropic-ratelimit-unified-5h-reset")),
            "w": pct(hdr("anthropic-ratelimit-unified-7d-utilization")),
            "wr": reset_minutes(hdr("anthropic-ratelimit-unified-7d-reset")),
            "st": hdr("anthropic-ratelimit-unified-5h-status", "unknown"),
            "acct": "pro",
            "ok": True,
        }
    else:
        reset_ts = hdr("anthropic-ratelimit-unified-overage-reset")
        payload = {
            "s": pct(hdr("anthropic-ratelimit-unified-overage-utilization")),
            "sr": reset_minutes(reset_ts),
            "w": 0,
            "wr": 0,
            "st": hdr("anthropic-ratelimit-unified-status", "unknown"),
            "acct": "ent",
            **_billing_period_info(now, reset_ts),
            "ok": True,
        }
    add_chime_field(payload)   # adds "c":1 iff the config opts in
    add_clock_fields(payload)   # adds "t" + "tf" iff the config opts in
    return payload


def _billing_period_info(now: float, reset_ts: str) -> dict:
    """Fraction of billing period elapsed (tp, 0-100) and period length in days (pd).

    Monthly window is assumed (headers expose only reset_ts, not period). Per the
    Claude Enterprise Admin API reference, spend-limit period's "only value today
    is monthly" — see the macOS daemon for the full note.
    """
    try:
        period_end = float(reset_ts)
    except ValueError:
        return {"tp": 0, "pd": 30, "rd": ""}
    if period_end <= 0:
        # reset_ts defaults to "0" whenever the overage-reset header is absent
        # (e.g. a 200 that simply carries no billing headers). fromtimestamp(0)
        # is 1970; stepping one month back lands in 1969, and datetime.timestamp()
        # raises OSError for pre-1970 dates on Windows — taking the whole poll
        # loop down. Bail out to the neutral default instead.
        return {"tp": 0, "pd": 30, "rd": ""}
    try:
        dt_end = datetime.datetime.fromtimestamp(period_end)
        prev_month = dt_end.month - 1 or 12
        prev_year = dt_end.year if dt_end.month > 1 else dt_end.year - 1
        prev_day = min(dt_end.day, calendar.monthrange(prev_year, prev_month)[1])
        dt_start = dt_end.replace(year=prev_year, month=prev_month, day=prev_day)
        period_start = dt_start.timestamp()
    except (OSError, OverflowError, ValueError):
        # Belt-and-braces beyond the <= 0 guard above (#104): Windows
        # datetime.timestamp()/fromtimestamp() also raise OSError(22)/
        # OverflowError/ValueError for out-of-range NON-zero values (e.g. a
        # far-future "99999999999999" header, which overflows fromtimestamp).
        # Garbage must never crash the daemon thread — degrade to the safe
        # default instead (field report: OSError(22) killed the poll loop).
        return {"tp": 0, "pd": 30, "rd": ""}
    period_len = period_end - period_start
    if period_len <= 0:
        return {"tp": 0, "pd": 30, "rd": ""}
    pct_val = (now - period_start) / period_len * 100
    return {
        "tp": max(0, min(100, int(round(pct_val)))),
        "pd": int(round(period_len / 86400)),
        "rd": f"{dt_end.strftime('%b')} {dt_end.day}",
    }


def _mac_from_pnp_instance_id(instance_id: str) -> str | None:
    """Recover a canonical BLE MAC ("AA:BB:CC:DD:EE:FF") from a PnP instance id.

    Windows encodes a paired BLE device's address in its PnP instance id as a
    12-hex run after a ``DEV_`` token, e.g.::

        BTHLE\\DEV_98A316A5D706\\7&B8081D1&0&98A316A5D706  ->  98:A3:16:A5:D7:06

    Returns None when no ``DEV_<12 hex>`` token is present. Pure — the
    subprocess that produces the instance id lives in discover_bonded_address().
    """
    m = re.search(r"DEV_([0-9A-Fa-f]{12})(?![0-9A-Fa-f])", instance_id)
    if not m:
        return None
    h = m.group(1).upper()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def discover_bonded_address() -> str | None:
    """Return the BLE address of the bonded Clawdmeter, or None.

    A device that is paired AND connected to Windows stops advertising, so
    BleakScanner can't see it (the steady state once paired — see
    README-windows.md). WinRT can still connect to it directly by address, so
    we recover that address from the OS:

    1. CLAWDMETER_BLE_ADDRESS env override (skips discovery — testing / pinning).
    2. Windows PnP table, filtered to the device's FriendlyName.

    Non-Windows or any failure returns None.
    """
    if override := os.environ.get("CLAWDMETER_BLE_ADDRESS"):
        return override.strip().upper()
    if sys.platform != "win32":
        return None
    command = (
        "Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.FriendlyName -eq '{DEVICE_NAME}' }} | "
        "Select-Object -ExpandProperty InstanceId"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as e:
        log(f"Bonded-address lookup failed: {e}")
        return None
    for line in result.stdout.splitlines():
        if mac := _mac_from_pnp_instance_id(line):
            return mac
    return None


def read_sid_index(path: Path | None = None) -> dict:
    """The sid -> {state, sender, mid} map the sidecar wrote with the payload.

    The panel talks back in sids, and a sid is two characters minted by the
    process that renders rows — not by this one. This is the only thing that
    turns a tap back into an agent to message. Total and quiet like its
    sibling: no sidecar, no file, an older sidecar that never wrote an index,
    or a half-written one all come back as {} and the caller says so.
    """
    path = path or SESSIONS_FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    index = doc.get("index") if isinstance(doc, dict) else None
    return index if isinstance(index, dict) else {}


def record_dismissal(mid: str, path: Path | None = None,
                     now: float | None = None) -> int:
    """Add one message id to the dismissed file. Returns how many it now holds.

    Pruned on every write rather than on a timer: entries older than
    DISMISS_TTL are dropped, and the newest DISMISS_KEEP survive whatever
    their age. Both bounds matter — a file that only grew would be a slow leak
    that the poller re-reads every two seconds, and an entry that never
    expired would be a permanent gag on an agent if a card were ever
    dismissed by mistake.
    """
    path = path or DISMISS_FILE
    now = time.time() if now is None else now
    items = []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("dismissed"), list):
            items = [d for d in doc["dismissed"]
                     if isinstance(d, dict) and isinstance(d.get("mid"), str)]
    except (OSError, ValueError):
        items = []          # absent or malformed: start clean, never raise
    items = [d for d in items if d.get("mid") != mid
             and now - float(d.get("ts") or 0) <= DISMISS_TTL]
    items.append({"mid": mid, "ts": round(now, 3)})
    items = items[-DISMISS_KEEP:]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"dismissed": items}, separators=(",", ":")),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log(f"Could not record the dismissal ({e}); the card will come back")
    return len(items)


def read_sessions_payload(path: Path | None = None) -> str | None:
    """Return the wire payload string from the sidecar's sessions.json, or None.

    None covers every "nothing to ship" case, all of them silent:
      * the file does not exist — the sidecar is not installed (the default);
      * it is unreadable or momentarily locked (Windows rename window);
      * it is half-written, truncated or otherwise not the shape we expect.

    The file holds {"ts": ..., "payload": "<wire string>"}; only the payload
    string goes on the wire, byte for byte, so the exact bytes the sidecar fitted
    to its budget are what the device receives. Pure and total — it never raises,
    because a malformed handoff file must not be able to kill the poll loop.
    """
    path = path or SESSIONS_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None  # absent / unreadable / undecodable
    try:
        doc = json.loads(raw)
    except ValueError:
        return None  # caught mid-write, or corrupt
    if not isinstance(doc, dict):
        return None
    payload = doc.get("payload")
    if not isinstance(payload, str) or not payload:
        return None
    return payload


# ---------------------------------------------------------------------------
# Report rounds — what the report button actually does
# ---------------------------------------------------------------------------

# The round in flight, if any. Module scope rather than per-Session on purpose:
# a round is host-side work with a life of its own, and it outlives the BLE link
# that started it. Hanging it off the Session would let a reconnect mid-round
# forget about it, and the next press would dispatch a second round on top of
# the first — which is precisely the double-spend the dispatcher's rate limit
# exists to stop.
_report_round = None


def report_round_in_flight() -> bool:
    return _report_round is not None and not _report_round.done()


async def run_report_round(script=None, timeout=REPORT_ROUND_TIMEOUT,
                           exec_fn=None, extra_args=None,
                           label="REPORT ROUND", done="REPORT: round finished",
                           child="dispatcher", prefix="report") -> int | None:
    """Run one report round in a child process. Never raises; never blocks.

    A round spawns `claude -p`, waits for it, then watches the inbox for
    replies — minutes of work in the worst case. Doing any of that inline would
    stop the usage payload flowing and the link would look dead, so it runs as
    a child process the poll loop never waits on.

    A CHILD PROCESS rather than a thread, and rather than calling
    clawdmeter_report.dispatch() in-process, for three reasons: it is genuinely
    asynchronous (asyncio owns the pipe, no executor, no thread of ours to
    supervise), the dispatcher's own failure modes cannot reach the daemon's
    event loop, and the CLI already prints exactly the prose a human needs. Its
    output is streamed here line by line as it arrives — not collected at exit —
    so a refusal shows up in the daemon log within a second of the press rather
    than three minutes later.

    Returns the child's exit code, or None if it could not be started or had to
    be killed. Every one of the dispatcher's refusals (rate limited, no mail
    drop, no reachable agents) is ITS decision, surfaced here verbatim: none of
    that logic is duplicated in this daemon.
    """
    path = Path(script) if script else REPORT_SCRIPT
    exec_fn = exec_fn or asyncio.create_subprocess_exec
    argv = [sys.executable, str(path)] + list(extra_args or ())
    kwargs = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.STDOUT,
    }
    if sys.platform == "win32":
        # Under the tray the daemon runs windowless; without this a round would
        # flash a console at the owner on every press.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = await exec_fn(*argv, **kwargs)
    except (OSError, ValueError, NotImplementedError) as e:
        # NotImplementedError is the honest one: a non-Proactor event loop has
        # no subprocess support on Windows. Say so rather than dying.
        log(f"{label} FAILED to start ({type(e).__name__}: {e})")
        return None

    try:
        rc, last = await asyncio.wait_for(_pump_round(proc, prefix), timeout=timeout)
    except asyncio.TimeoutError:
        log(f"{label} TIMED OUT after {int(timeout)}s; killing the {child}"
            f" (anything it had already sent stays sent)")
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass
        return None
    except (OSError, ValueError) as e:
        log(f"{label} FAILED while running ({type(e).__name__}: {e})")
        return None
    if rc == 0:
        log(done)
    else:
        # Loud on purpose. The owner pressed a button and is standing in front
        # of the device; a round that refused or failed in silence is worse than
        # one that never started, because nothing on the panel will change and
        # nothing says why.
        log(f"{label} DID NOT RUN ({child} exit {rc})"
            + (f": {last}" if last else ""))
    return rc


async def _pump_round(proc, prefix="report"):
    """Relay the dispatcher's output into the daemon log as it arrives.

    Returns (exit code, last non-empty line) — that last line is what the
    "did not run" log line quotes, because the CLI prints its reason
    ("refused: rate limited: 240s to go") and then stops.
    """
    last = ""
    stream = getattr(proc, "stdout", None)
    if stream is not None:
        async for raw in stream:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            log(f"{prefix}: {line}")
            last = line
    return await proc.wait(), last


async def acquire_target():
    """Return a connectable handle for the Clawdmeter, or None.

    Targets only the device bonded to THIS machine (via the PnP table /
    CLAWDMETER_BLE_ADDRESS) — it never scans for a nearby device by name, so it
    can't grab a stranger's or the wrong nearby unit. The device must be paired
    with Windows once first (the documented setup). Returns a BLEDevice or None.
    """
    address = discover_bonded_address()
    if not address:
        return None
    log(f"Not advertising; connecting to bonded address {address}")
    # CRITICAL: hand BleakClient a BLEDevice, not the bare address string. WinRT's
    # connect() resolves a bare string via an advertisement scan (find_device_by_address)
    # — which always fails for a bonded device that has stopped advertising, the very
    # case we are handling. A BLEDevice sets _device_info directly, so WinRT connects
    # via from_bluetooth_address_with_bluetooth_address_type_async and skips the scan.
    return BLEDevice(address, DEVICE_NAME, None)


class Session:
    def __init__(self, client: BleakClient) -> None:
        self.client = client
        self.refresh_requested = asyncio.Event()
        # Live-session shipping state, deliberately per-connection — the same
        # reset the bash daemon does (SS_CHAR_PATH + LAST_SESSIONS_SIG cleared on
        # every reconnect): a device that just came back has an empty Sessions tab
        # and needs the current payload resent even though the file never changed.
        self.ss_supported = True            # until this device says otherwise
        self.last_sessions_payload: str | None = None
        self._ss_write_logged = False       # at most one write-failure log per link
        # Button events arrive on a notification callback and are handled on the
        # poll tick, exactly like refresh_requested above: the callback stays a
        # few microseconds long, and everything that can be slow happens on the
        # loop where the rest of the daemon's work already lives.
        self.tx_supported = True            # until this device says otherwise
        self.events: deque = deque()
        self.event_pending = asyncio.Event()

    def _on_refresh(self, _char, _data: bytearray) -> None:
        log("Refresh requested by device")
        self.refresh_requested.set()

    async def setup_refresh_subscription(self) -> None:
        # The refresh subscription is optional — the 60s poll loop works without it.
        # WinRT's start_notify() CCCD write can raise a raw OSError/WinError (not
        # wrapped as BleakError) when the peer GATT server is transiently unavailable,
        # e.g. a just-power-cycled ESP32 whose server is not yet ready (G-03-01, SC#3).
        # Degrade gracefully instead of crashing the daemon so it stays single-process
        # across a power-cycle reconnect (SC#4, no restart).
        try:
            await self.client.start_notify(REQ_CHAR_UUID, self._on_refresh)
        except (BleakError, ValueError, OSError) as e:
            log(f"Refresh subscription unavailable: {e}")

    def _on_tx(self, _char, data: bytearray) -> None:
        """A TX notification. Most of them are not button events.

        TX has carried {"ack":true} and {"err":true} since the first firmware,
        and neither may ever be read as a press — a round costs real quota on
        somebody else's machine. So the ONLY thing treated as an event is a JSON
        object with an integer "ev"; an ack, a nack, a key from a firmware newer
        than this daemon, or bytes that are not JSON at all are dropped right
        here and silently, so a healthy link does not fill the log with acks.
        """
        try:
            doc = json.loads(bytes(data).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, TypeError):
            return
        if not isinstance(doc, dict):
            return
        code = doc.get("ev")
        # bool is an int in Python, and {"ev":true} is not event 1.
        if not isinstance(code, int) or isinstance(code, bool):
            return
        self.events.append(doc)
        self.event_pending.set()

    async def setup_event_subscription(self) -> None:
        """Subscribe to the device's button events. Optional, and quiet.

        Every board builds TX, but subscribing to it is new behaviour: a device
        running older firmware subscribes perfectly well and then never notifies
        an event, which is the normal, silent case and not a fault. A device
        that has no TX at all (or a WinRT CCCD write that fails the way
        setup_refresh_subscription() guards against) turns the feature off for
        this link and leaves everything else running.
        """
        try:
            found = self.client.services.get_characteristic(TX_CHAR_UUID)
        except (BleakError, AttributeError, OSError):
            found = None  # older bleak / no service collection: try anyway
        if found is None and self._services_readable():
            self.tx_supported = False
            log("Device has no event characteristic; buttons are off for this link")
            return
        try:
            await self.client.start_notify(TX_CHAR_UUID, self._on_tx)
        except BleakCharacteristicNotFoundError:
            self.tx_supported = False
            log("Device has no event characteristic; buttons are off for this link")
        except (BleakError, ValueError, OSError) as e:
            self.tx_supported = False
            log(f"Button-event subscription unavailable: {e}")

    def _services_readable(self) -> bool:
        """True when the client's service collection can actually be consulted.

        Without this, an older bleak (or a mock) whose lookup returns None for
        everything would be read as "this board has no TX" and the feature would
        switch itself off on a device that has it.
        """
        try:
            return self.client.services is not None
        except (BleakError, AttributeError, OSError):
            return False

    async def handle_events(self) -> None:
        """Act on whatever the device notified since the last tick.

        Rides the existing tick, like maybe_send_sessions(). Nothing here waits
        for a round: dispatching one hands off to a child process and returns.
        """
        self.event_pending.clear()
        while self.events:
            doc = self.events.popleft()
            code = doc.get("ev")
            handler = EVENT_HANDLERS.get(code)
            if handler is None:
                # A firmware newer than this daemon. Log it once per event so an
                # unrecognised button is visible in the field, and do nothing —
                # guessing at an unknown code is how a "go ahead" turns into a
                # report round nobody asked for.
                log(f"Ignoring unknown device event {code}")
                continue
            await handler(self, doc)

    async def on_report_event(self, _doc: dict) -> None:
        """The report button: ask the fleet what it is doing."""
        global _report_round
        if report_round_in_flight():
            # Not the dispatcher's 5-minute rate limit (that is its own, and it
            # still applies) — this is the narrower case of a press landing
            # while the previous round's child process is still alive.
            log("REPORT: a round is already running; ignoring this press")
            return
        log("REPORT: button pressed — dispatching a round")
        _report_round = asyncio.ensure_future(run_report_round())
        # Retrieve the result so a task nobody awaits cannot log
        # "exception was never retrieved" at shutdown.
        _report_round.add_done_callback(_report_round_done)

    async def on_go_ahead_event(self, doc: dict) -> None:
        """A NEEDS-YOU card was tapped: tell that agent to continue.

        The sid is all the device can send, so this is where it becomes an
        agent again — via the index the sidecar wrote beside the payload it
        shipped. Every failure below is LOUD, because somebody is standing at
        the device having just told an agent to carry on: a go-ahead that
        quietly went nowhere leaves them waiting on a machine that is waiting
        on them.
        """
        sid = doc.get("sid")
        if not isinstance(sid, str) or not sid:
            log("GO AHEAD: event carried no sid; ignoring it")
            return
        entry = read_sid_index().get(sid)
        if not isinstance(entry, dict):
            log(f"GO AHEAD: no card {sid} in the sidecar's index — either the "
                f"sidecar is older than this feature, or the row has expired")
            return
        sender = entry.get("sender")
        if not isinstance(sender, str) or not sender:
            log(f"GO AHEAD: card {sid} is not something an agent sent "
                f"(state {entry.get('state')}); there is nobody to answer")
            return
        log(f"GO AHEAD: {sid} -> {sender}")
        task = asyncio.ensure_future(
            run_report_round(extra_args=["--go-ahead", sender],
                             timeout=GO_AHEAD_TIMEOUT, label="GO AHEAD",
                             done=f"GO AHEAD: {sender} was told to continue",
                             child="courier", prefix="go ahead"))
        _side_tasks.add(task)
        task.add_done_callback(_side_tasks.discard)

    async def on_dismiss_event(self, doc: dict) -> None:
        """A card was tapped away. Stop sending that row.

        The DEVICE has already hidden it — that happens under the finger, with
        no round trip, and it is what makes the gesture feel like anything.
        This side only stops the row being re-sent, which is what makes the
        dismissal survive a reboot of the panel.
        """
        sid = doc.get("sid")
        if not isinstance(sid, str) or not sid:
            log("DISMISS: event carried no sid; ignoring it")
            return
        entry = read_sid_index().get(sid)
        mid = entry.get("mid") if isinstance(entry, dict) else None
        if not isinstance(mid, str) or not mid:
            # Not an error worth shouting about: fleet session rows have no
            # message id, and the device hid the card either way. Say it once
            # so a genuinely stale index is visible in the log.
            log(f"DISMISS: {sid} has no message id in the index; the device "
                f"hid it, but the host cannot make that outlive a reboot")
            return
        n = record_dismissal(mid)
        log(f"DISMISS: {sid} cleared ({n} held)")

    async def on_broadcast_event(self, _doc: dict) -> None:
        """Settings > Teach agents: tell the fleet how Clawdmeter works.

        Housekeeping rather than an alert, so unlike a round there is no rate
        limit and no five-agent cap — it produces no cards, and the agent left
        out would be exactly the one that goes on meeting Clawdmeter cold. The
        agents that already know answer NOOP and change nothing.
        """
        log("BROADCAST: teaching the fleet the standing rules")
        task = asyncio.ensure_future(
            run_report_round(extra_args=["--broadcast-rules"],
                             timeout=BROADCAST_TIMEOUT, label="BROADCAST",
                             done="BROADCAST: the fleet has been told",
                             child="courier", prefix="broadcast"))
        _side_tasks.add(task)
        task.add_done_callback(_side_tasks.discard)

    def probe_session_support(self) -> None:
        """Decide once per connection whether this device has the SS characteristic.

        Only boards built with BOARD_HAS_SESSION_VIEWS create it, so on most
        devices it is absent and every session write would fail identically —
        exactly the retry-spam this avoids. Services are already cached from
        connect-time discovery, so this is a table lookup, not I/O. If the lookup
        itself misbehaves (older bleak, a client that lost its service collection)
        the flag stays optimistic: maybe_send_sessions() catches the missing
        characteristic on the first write anyway."""
        try:
            found = self.client.services.get_characteristic(SS_CHAR_UUID)
        except (BleakError, AttributeError, OSError):
            return
        if found is None:
            self.ss_supported = False
            log("Device has no session characteristic; live sessions off for this link")

    async def maybe_send_sessions(self) -> None:
        """Ship the sidecar's session payload to the device when it has changed.

        Rides the existing TICK — no second timer, no extra thread. Three quiet
        no-ops by design: this device has no SS characteristic, the sidecar is not
        installed (no file), or the payload is byte-identical to the last one that
        went over the air."""
        if not self.ss_supported:
            return
        payload = read_sessions_payload()
        if payload is None or payload == self.last_sessions_payload:
            return
        log(f"Sending sessions: {payload}")
        try:
            await self.client.write_gatt_char(
                SS_CHAR_UUID, payload.encode("utf-8"), response=False
            )
        except BleakCharacteristicNotFoundError:
            # Firmware without the session views (every board but the flagship):
            # learn it once, then stay silent for the rest of this link.
            self.ss_supported = False
            log("Device has no session characteristic; live sessions off for this link")
            return
        except (BleakError, OSError, ValueError) as e:
            # Same WinRT reality as write_payload(): a raw OSError/WinError can come
            # out of a link that is going away. Log once per link, leave
            # last_sessions_payload untouched so the next tick retries, and keep the
            # zombie-link breaker out of it — session rows are a secondary feed and
            # must never be the thing that forces a reconnect. A genuinely dead link
            # still trips the breaker on the next usage write.
            if not self._ss_write_logged:
                self._ss_write_logged = True
                log(f"Session write failed: {e}")
            return
        self.last_sessions_payload = payload
        self._ss_write_logged = False

    async def write_payload(self, payload: dict) -> bool:
        data = json.dumps(payload, separators=(",", ":")).encode()
        log(f"Sending: {data.decode()}")
        try:
            await self.client.write_gatt_char(RX_CHAR_UUID, data, response=False)
            return True
        except (BleakError, OSError) as e:
            # WinRT can raise a raw OSError/WinError (NOT wrapped as BleakError)
            # when the peer GATT server goes transiently unavailable mid-write —
            # the same failure class setup_refresh_subscription() guards against.
            # Returning False trips the zombie-link break -> clean reconnect,
            # rather than an uncaught exception killing the daemon thread (the
            # silent-freeze failure mode, SC#2 field report).
            log(f"Write failed: {e}")
            return False


def _report_round_done(task) -> None:
    """Drain a finished round's result. Nothing here can raise into the loop."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:  # pragma: no cover - run_report_round catches its own
        log(f"REPORT ROUND CRASHED: {type(exc).__name__}: {exc}")


# One entry per device event code. Adding the "go ahead" button is a new code
# and a new handler in this table — not a redesign of the channel.
# Strong references to the fire-and-forget children (a go-ahead, a dismissal
# that needed one). Without this the event loop is the only holder and the task
# can be collected mid-flight.
_side_tasks: set = set()

EVENT_HANDLERS = {
    EVENT_REPORT: Session.on_report_event,
    EVENT_GO_AHEAD: Session.on_go_ahead_event,
    EVENT_DISMISS: Session.on_dismiss_event,
    EVENT_BROADCAST: Session.on_broadcast_event,
}


def _extract_access_token(blob: str) -> str | None:
    """Pull the accessToken out of a credentials blob.

    Claude Code stores credentials as a JSON object; the blob may also be
    nested ({"claudeAiOauth": {"accessToken": "..."}}). Fall back to a
    regex match so unexpected shapes still work, and finally treat the
    blob as a raw token if nothing else matches.
    """
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        # direct: {"accessToken": "..."}
        tok = data.get("accessToken")
        if isinstance(tok, str) and tok.strip():
            return tok
        # nested: {"claudeAiOauth": {"accessToken": "..."}}
        for v in data.values():
            if isinstance(v, dict):
                tok = v.get("accessToken")
                if isinstance(tok, str) and tok.strip():
                    return tok
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    # Raw token (no JSON wrapper) — must look plausible (sk-ant-... etc.)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def _windows_credential_candidates() -> list[Path]:
    """Return the ordered list of credential file paths to probe (first hit wins).

    Priority:
    1. CLAUDE_CREDENTIALS_PATH env override (D-03, project-specific)
    2. CLAUDE_CONFIG_DIR env override (official Claude override)
    3. D-02 candidate list: home/.claude, LOCALAPPDATA/Claude, APPDATA/Claude
    """
    # Priority 1: project-specific env override (D-03)
    if override := os.environ.get("CLAUDE_CREDENTIALS_PATH"):
        return [Path(override)]
    # Priority 2: official CLAUDE_CONFIG_DIR env override
    if config_dir := os.environ.get("CLAUDE_CONFIG_DIR"):
        return [Path(config_dir) / ".credentials.json"]
    # Priority 3: D-02 candidate list — first hit wins
    home = Path.home()
    local_appdata = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    return [
        home / ".claude" / ".credentials.json",          # primary (confirmed by docs)
        local_appdata / "Claude" / ".credentials.json",  # fallback 2
        appdata / "Claude" / ".credentials.json",        # fallback 3
    ]


def read_token() -> str | None:
    """Read the Claude OAuth access token from the first available credential file."""
    for path in _windows_credential_candidates():
        try:
            return _extract_access_token(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return None


def _read_expiry() -> str:
    """Return human-readable expiry from the first-hit credentials file.

    Reads claudeAiOauth.expiresAt (epoch milliseconds — JS convention).
    Divides by 1000 before passing to fromtimestamp (Python expects seconds).
    Returns 'expiry unknown' on any parse failure.
    """
    for path in _windows_credential_candidates():
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            data = json.loads(raw)
            oauth = data.get("claudeAiOauth", {})
            expires_ms = oauth.get("expiresAt")
            if expires_ms is None:
                return "expiry unknown"
            # CRITICAL: expiresAt is JS-convention epoch milliseconds; divide by 1000
            # before fromtimestamp (Python expects seconds). Raw value -> year ~57000.
            dt = datetime.datetime.fromtimestamp(
                expires_ms / 1000, tz=datetime.timezone.utc
            )
            return dt.strftime("%Y-%m-%d %H:%M UTC")
        except (TypeError, ValueError, OSError, AttributeError, json.JSONDecodeError):
            return "expiry unknown"
    return "expiry unknown"


async def _wait_first(*events: asyncio.Event, timeout: float) -> None:
    """Return when any of `events` is set, or after `timeout` seconds.

    Lets the poll loop's TICK wait wake immediately on a stop signal (clean,
    responsive Quit) without losing the refresh-request wakeup — instead of
    waiting only on refresh_requested and re-checking stop_event up to TICK
    later. Cancels and drains the loser tasks so they don't warn.
    """
    tasks = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def connect_and_run(device, stop_event: asyncio.Event, tray_state=None) -> bool:
    """Connect to device and poll until disconnected or stopped.

    Returns True if at least one successful write occurred.

    `device` is a BLEDevice — either from an advertisement scan or built from the
    bonded address by acquire_target(). The getattr keeps the log line robust if a
    bare address string is ever passed in.
    """
    log(f"Connecting to {getattr(device, 'address', device)}...")
    # D-01: retry wrapper — defeats WinRT post-wake failure modes
    # (Could not get GATT services: Unreachable, stale is_connected).
    # Rebuild a fresh BleakClient each attempt (locked D-05 recipe).
    client = None
    for attempt in range(CONNECT_RETRIES):
        # D-05: pass BLEDevice (not address string), address_type="random" (NimBLE
        # static-random), use_cached_services=False (DIY firmware — WinRT GATT cache
        # may be stale after firmware reflash).
        client = BleakClient(
            device,
            address_type="random",
            use_cached_services=False,
        )
        try:
            await client.connect()
        except (BleakError, OSError, asyncio.TimeoutError, AssertionError) as e:
            # WinRT service discovery inside connect() can surface a raw OSError
            # (WinError) or even a bare AssertionError from bleak's FutureLike
            # (assert self._result) when the peer drops the link mid-discovery —
            # neither is wrapped as BleakError. Treat them as a normal failed
            # attempt so the D-01 retry loop handles them, instead of letting an
            # uncaught exception kill the daemon thread (the "daemon crashed"
            # tray toast + silent polling stop, field report).
            log(f"Connection attempt {attempt + 1}/{CONNECT_RETRIES} failed: {type(e).__name__}: {e}")
            try:
                await client.disconnect()
            except BleakError:
                pass
            if attempt < CONNECT_RETRIES - 1:
                await asyncio.sleep(CONNECT_RETRY_DELAY)
            continue

        if not client.is_connected:
            log(f"Connection attempt {attempt + 1}/{CONNECT_RETRIES} failed (not connected)")
            try:
                await client.disconnect()
            except BleakError:
                pass
            if attempt < CONNECT_RETRIES - 1:
                await asyncio.sleep(CONNECT_RETRY_DELAY)
            continue

        # Connected successfully
        break
    else:
        log(f"Connection failed after {CONNECT_RETRIES} attempts")
        return False

    log("Connected")
    session = Session(client)
    await session.setup_refresh_subscription()
    await session.setup_event_subscription()
    session.probe_session_support()

    last_poll = 0.0  # D-03: poll immediately on first connect
    used_successfully = False
    consecutive_failures = 0  # D-03: zombie-link break counter

    def note_write_failure() -> bool:
        """Count a failed device write toward the zombie-link breaker.

        Returns True when too many writes have failed in a row and the caller
        should abandon the (likely zombie) link so the outer loop reconnects.
        Applies to every device write — data payloads and no-data beats alike —
        so a dead link still trips the breaker even when the token is also dead.
        """
        nonlocal consecutive_failures
        consecutive_failures += 1
        if consecutive_failures >= ZOMBIE_BREAK_LIMIT:
            log(
                f"Zombie link detected ({consecutive_failures} consecutive"
                f" write failures); abandoning connection"
            )
            return True
        return False

    try:
        while client.is_connected and not stop_event.is_set():
            now = time.time()
            elapsed = now - last_poll
            if session.refresh_requested.is_set() or elapsed >= POLL_INTERVAL:
                session.refresh_requested.clear()
                # Pure free-ride: read whatever access token Claude Code currently
                # holds and NEVER refresh it ourselves. Claude Code (the token's owner)
                # does all refreshing; refreshing here would race its rotation and feed
                # the OAuth endpoint's rate limit (429). When the token is dead we just
                # show "No data" until the CLI re-seeds it.
                token = read_token()  # D-09: fresh each cycle
                if not token:
                    log("No token; signalling no-data to device")
                    if tray_state:
                        tray_state.set_error("token expired — run claude login")
                    if await session.write_payload({"ok": False}):
                        last_poll = time.time()
                        consecutive_failures = 0  # D-03: healthy link
                    elif note_write_failure():
                        break
                else:
                    payload = None
                    expired = False
                    try:
                        payload = await poll_api(token)
                    except AuthError:
                        # Pure free-ride: we never refresh. A 401/403 means Claude Code's
                        # token has expired and only Claude Code (its owner) can re-seed it.
                        expired = True
                        log("Token expired/invalid; signalling no-data — run `claude login` "
                            "or use the CLI to let Claude Code renew it")
                        if tray_state:
                            tray_state.set_error("token expired — run claude login")
                    if payload is not None:
                        if await session.write_payload(payload):
                            last_poll = time.time()
                            used_successfully = True
                            consecutive_failures = 0  # D-03: reset on success
                            if tray_state:
                                tray_state.set_connected(time.time())
                        elif note_write_failure():
                            break
                    elif expired:
                        # Token genuinely dead -> show "No data" now instead of stale numbers.
                        # Transient poll failures (payload None without expiry) stay silent.
                        log("No data (token dead); signalling idle to device")
                        if await session.write_payload({"ok": False}):
                            last_poll = time.time()
                            consecutive_failures = 0  # D-03: healthy link
                        elif note_write_failure():
                            break
                    # else: payload is None from a TRANSIENT failure (network/DNS,
                    # timeout, rate-limit, 5xx). poll_api already logged it; do NOT
                    # toast "token expired" — that mislabeled a boot-time DNS blip
                    # as an auth problem (SC#5). Leave tray state unchanged; the next
                    # tick retries and set_connected() recovers it.

            # Live session rows (issue #135) ride the same tick as everything else:
            # the sidecar has already done the work, so this is a file read that
            # usually finds nothing and, when it does, one small GATT write.
            await session.maybe_send_sessions()

            # Whatever the device's buttons notified since the last tick. A
            # report round hands off to a child process and returns, so this
            # costs the tick nothing even when a round takes three minutes.
            await session.handle_events()

            # Wake on a refresh request OR a stop, whichever comes first. Waking
            # promptly on stop_event is what lets the finally below run
            # client.disconnect() before the process exits, so the peer gets a
            # clean GATT disconnect (returns to its waiting screen) instead of
            # being left frozen on stale data after Quit (SC#3 graceful shutdown).
            await _wait_first(session.refresh_requested, session.event_pending,
                              stop_event, timeout=TICK)
    finally:
        # Clean GATT disconnect on the way out — this is what tells the peripheral
        # the link is gone. WinRT can surface a raw OSError (not BleakError) here,
        # so swallow both; the link tears down regardless once we exit.
        try:
            await client.disconnect()
        except (BleakError, OSError, AssertionError):
            # bleak's WinRT disconnect() also has bare asserts (e.g. assert char
            # while tearing down notifications on an already-gone peer); swallow
            # it too — the link tears down regardless once we exit.
            pass

    log("Device disconnected" if not stop_event.is_set() else "Stopping")
    return used_successfully


def _next_backoff(current: int, cap: int) -> int:
    """D-05: double current backoff value, clamped to cap.

    Pure helper — unit-testable without driving the main loop.
    Used by both slow-search (cap=60) and fast-reconnect (cap=RECONNECT_BACKOFF_CAP) regimes.
    """
    return min(current * 2, cap)


async def main(tray_state=None) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    # Populate the shared state object so the tray can route Quit through
    # loop.call_soon_threadsafe (RESEARCH Pitfall 2).  Additive — the existing
    # stop_event = asyncio.Event() line above is unchanged.
    if tray_state is not None:
        tray_state.loop = loop
        tray_state.stop_event = stop_event

    def _stop(*_args: object) -> None:
        log("Daemon stopping")
        stop_event.set()

    # OS signal handlers can only be installed from the main thread, and
    # loop.add_signal_handler is unsupported on Windows. When running under the
    # tray (04-03) the loop lives in a background thread and the tray owns clean
    # shutdown via stop_event (loop.call_soon_threadsafe), so skip silently there.
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except NotImplementedError:
                # Windows: add_signal_handler not supported; fall back to signal.signal
                try:
                    signal.signal(sig, _stop)
                except ValueError:
                    # Not the main thread of the main interpreter — tray owns shutdown.
                    pass

    log("=== Claude Usage Tracker Daemon (BLE, Windows) ===")
    log(f"Poll interval: {POLL_INTERVAL}s")

    # D-05: two distinct backoff regimes — slow-search (device absent) vs fast-reconnect (link dropped)
    search_backoff = 1     # caps at 60s — gentle, for a device that is genuinely absent/off
    reconnect_backoff = 1  # caps at RECONNECT_BACKOFF_CAP — fast, to clear the 120s SLA after a drop
    while not stop_event.is_set():
        device = await acquire_target()
        if not device:
            # Slow-search regime: device was not found by scan — back off gently
            if tray_state:
                tray_state.set_scanning()
            log(f"Device not found, retrying in {search_backoff}s...")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=search_backoff)
            except asyncio.TimeoutError:
                pass
            search_backoff = _next_backoff(search_backoff, 60)
            continue

        ok = await connect_and_run(device, stop_event, tray_state)
        if not ok:
            # Fast-reconnect regime: had/attempted a link that dropped — retry quickly
            if tray_state:
                tray_state.set_scanning()
            log(f"Connection lost, reconnecting in {reconnect_backoff}s...")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=reconnect_backoff)
            except asyncio.TimeoutError:
                pass
            reconnect_backoff = _next_backoff(reconnect_backoff, RECONNECT_BACKOFF_CAP)
        else:
            # Successful session — reset reconnect counter to floor; search_backoff also reset
            reconnect_backoff = 1
            search_backoff = 1


if __name__ == "__main__":
    if sys.platform != "win32":
        print(
            "Warning: running under Linux/WSL — WinRT BLE will not be available.",
            file=sys.stderr,
        )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
