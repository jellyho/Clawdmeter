# Windows Setup and Run Guide

This guide covers running the Clawdmeter Windows daemon on native Windows hardware.
It includes the turnkey `install-windows.ps1` bootstrap (tray icon + login autostart),
the manual-run fallback, and how to manage or remove autostart.

---

## Prerequisites

| Requirement | Details |
|-------------|---------|
| **Native Windows** | Must run on real Windows — not WSL. The script prints a warning and BLE will not work under WSL. |
| **Python 3.11+** | Download from [python.org](https://www.python.org/downloads/) if not already installed. Ensure "Add python.exe to PATH" is checked during install. |
| **Claude Code installed** | Install Claude Code and complete `claude login` so credentials exist on disk. |
| **Clawdmeter powered on** | The device must be powered on and in range before the daemon starts. |
| **Paired with Windows Bluetooth** | Pair the device once via **Settings → Bluetooth & devices → Add device** (see [Pair the device](#pair-the-device-one-time)). This is required — the device is a bonded BLE HID keyboard, so pairing enables its physical buttons and keeps a persistent connection that shows your last usage even when the daemon is stopped. |

### Where are my credentials?

`claude login` writes the OAuth token to (first match wins):

1. `%USERPROFILE%\.claude\.credentials.json` — primary path (confirmed by Claude Code docs)
2. `%LOCALAPPDATA%\Claude\.credentials.json` — fallback
3. `%APPDATA%\Claude\.credentials.json` — fallback

The daemon probes these paths in order. You can also set `CLAUDE_CREDENTIALS_PATH` to an
absolute path or `CLAUDE_CONFIG_DIR` to a directory to override the search entirely.

> **Security note:** The credentials file contains your OAuth token. Never share its contents
> or embed it in scripts. The daemon reads it from disk and uses it only as the API
> `Authorization` header — the token is never written to any log, tooltip, or notification.

---

## Pair the device (one time)

The Clawdmeter is a **bonded BLE HID keyboard** as well as a usage display — its firmware
enables bonding (`NimBLEDevice::setSecurityAuth`) and advertises the HID service so its
physical buttons act as a keyboard (Space / Shift+Tab). Pair it with Windows **once**,
before running the daemon:

1. Put the device on its Bluetooth waiting screen (powered on, not yet connected).
2. Open **Settings → Bluetooth & devices → Add device → Bluetooth**.
3. Select **Clawdmeter** and complete pairing.

**Why this is required:**

- **Keyboard buttons** — HID over BLE requires bonding on Windows. Without pairing, the
  device's buttons won't reach the PC.
- **Persistent point-in-time view** — once paired, Windows maintains the BLE link and
  auto-reconnects the device whenever it is in range. This is intentional: the device keeps
  showing your **last-synced** usage even after you Quit the daemon, as a glanceable
  point-in-time view. Quitting the daemon releases only its data connection — it does **not**
  drop the Windows pairing, so the device stays connected to Windows.

To undo, use **Settings → Bluetooth & devices → (device) → Remove device**. Removing the
pairing disables the keyboard buttons.

---

## Setup (one time)

Open a PowerShell terminal and `cd` to the repository root.

**1. Create a virtual environment**

```powershell
python -m venv .venv
```

**2. Activate it**

```powershell
.venv\Scripts\Activate.ps1
```

If you see a scripts-execution-policy error, run:
```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```
Then repeat the `Activate.ps1` step.

**3. Install dependencies**

```powershell
pip install -r daemon\requirements-windows.txt
```

This installs `bleak` (WinRT BLE) and `httpx` (async HTTP for the Anthropic API).

---

## Running the daemon

With the venv active and the Clawdmeter powered on:

```powershell
python daemon\claude_usage_daemon_windows.py
```

### Expected console output

```
[HH:MM:SS] === Claude Usage Tracker Daemon (BLE, Windows) ===
[HH:MM:SS] Poll interval: 60s
[HH:MM:SS] Scanning for 'Clawdmeter' (8.0s)...
[HH:MM:SS] Not advertising; connecting to bonded address XX:XX:XX:XX:XX:XX
[HH:MM:SS] Connecting to XX:XX:XX:XX:XX:XX...
[HH:MM:SS] Connected
[HH:MM:SS] Sending: {"s":42,"sr":180,"w":17,"wr":8820,"st":"active","ok":true}
```

- **The device must be paired with Windows first** (see [Pair the device](#pair-the-device-one-time)).
  The daemon then connects over that existing link via `BleakScanner` + `BleakClient`; it does
  not pop its own pairing dialog.
- **`Scanning…` → `Not advertising; connecting to bonded address …` is normal.** Once paired,
  Windows keeps the device connected, so it stops advertising and an advertisement scan can't
  see it. The daemon detects this and connects directly to the device's address (recovered from
  the Windows PnP table). The 8-second scan that precedes the fallback happens once per session.
  Set `CLAWDMETER_BLE_ADDRESS=AA:BB:CC:DD:EE:FF` to pin the address and skip PnP lookup.
- After `Connected`, the daemon polls the Anthropic API immediately and sends the first
  payload within a few seconds of connect (warm token path). With a valid, non-expired token
  the device should leave its waiting screen and show session + weekly percentages within
  about 10 seconds of launch.
- The daemon then re-polls every 60 seconds while connected. If the device fires a refresh
  request (e.g., after a button press), an immediate re-poll occurs without waiting for the
  60-second interval.
- If the device disconnects or goes out of range, the daemon logs `Device disconnected` and
  re-scans automatically with exponential backoff (starting at 1 second, capped at 60 seconds).

### Stopping

Press **Ctrl+C** in the terminal. The daemon logs `Daemon stopping` and exits cleanly.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `Warning: running under Linux/WSL` | Running in WSL, not native Windows | Run from a native PowerShell or Command Prompt on Windows |
| `Scanning for 'Clawdmeter'… Device not found` | Clawdmeter is off, out of range, or not yet paired | Power on the device, pair it once (see [Pair the device](#pair-the-device-one-time)), and ensure it is in range |
| `No token; skipping poll` | No credentials file found at any candidate path | Confirm `claude login` ran on this machine; check `%USERPROFILE%\.claude\.credentials.json` exists |
| `API HTTP 401` | Token expired | Re-run `claude login` in a terminal to refresh the token, then restart the daemon |
| `Connection failed` | WinRT BLE initialisation issue | Ensure Windows Bluetooth is on; try toggling Bluetooth off/on in Windows Settings |

---

## Tray icon, login autostart, and turnkey install

### One-command install (recommended)

> **Copy the repo to a native Windows path first.** Clone or copy this repository
> to a Windows location such as `%USERPROFILE%\Clawdmeter` — **not** a WSL share
> (`\\wsl$\...` or `\\wsl.localhost\...`). Installing from the WSL share would point
> the virtual environment and the login-autostart entry at a path that disappears when
> WSL shuts down, defeating the whole point of the Windows daemon. The installer
> detects a WSL path and refuses to run, telling you how to relocate.
>
> ```powershell
> Copy-Item -Recurse '\\wsl.localhost\Ubuntu\home\<you>\repos\Clawdmeter' "$env:USERPROFILE\Clawdmeter"
> cd "$env:USERPROFILE\Clawdmeter"
> ```

Run this once from the repository root in PowerShell (a native Windows path):

```powershell
powershell -ExecutionPolicy Bypass -File install-windows.ps1
```

The script does four things in order and logs progress at each step:

1. Creates a Python virtual environment at `.venv`.
2. Installs dependencies from `daemon\requirements-windows.txt` (bleak, httpx, pystray, Pillow).
3. Registers the tray app to launch automatically at login via `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` — per-user, no admin required.
4. Launches the tray app immediately (headless — no console window).

The script downloads nothing from the internet. It only installs the packages listed in
the in-repo `daemon\requirements-windows.txt`.

### Tray icon and status

After install, the Clawdmeter icon appears in the Windows notification area:

| State | Icon bubble | Tooltip |
|-------|-------------|---------|
| Connected | green | `Connected · last update HH:MM` |
| Scanning | amber | `Scanning…` |
| Error | red | `Error: token expired — run claude login` |

Hover over the icon to see the current status tooltip. A notification fires once when the
daemon first enters the Error state (e.g. after a token expiry).

### Tray menu

Right-click the tray icon for the menu:

- **Status header** (non-clickable) — live status + last data sync time.
- **Start at login** (checkable toggle) — enables or disables autostart at runtime.
  Reflects the current registry state each time the menu opens.
- **Quit** — stops the daemon cleanly and exits with no lingering process. It releases the
  daemon's own data connection but does **not** drop the Windows Bluetooth pairing — the
  device stays connected to Windows and keeps showing your last-synced usage (point-in-time
  view).

### Disabling or removing autostart

Use the tray menu toggle, or remove the registry value manually:

```powershell
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v Clawdmeter /f
```

### Live session awareness (optional)

Shows your open Claude Code chats on the device — what each is doing, how full
its context is, and which one is waiting on you. **Off by default**; it needs a
board with the session views in firmware, plus a hook sidecar on this machine.
Full design and wire format: [SESSIONS.md](SESSIONS.md).

```powershell
powershell -ExecutionPolicy Bypass -File install-windows.ps1 -Sessions
```

That adds three things to the normal install: `hook_port = 45999` in
`%LOCALAPPDATA%\Clawdmeter\config`, the Clawdmeter hook block in
`%USERPROFILE%\.claude\settings.json` (merged, never replacing what is there),
and a second autostart entry, `ClawdmeterSessions`, that runs
`daemon\clawdmeter_sessions.py` headlessly at logon.

The daemon then reads `%USERPROFILE%\.clawdmeter\sessions.json` on its existing
5 s tick and writes the payload to the device only when it changes. Without the
sidecar the file never exists and the daemon does nothing at all; on a board
whose firmware has no session characteristic it notices once, logs one line, and
stays quiet for the rest of the connection.

Logs: the sidecar runs under `pythonw.exe` (no console), so it writes
`%LOCALAPPDATA%\Clawdmeter\sessions.log`. To see the current payload live:

```powershell
python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:45999/').read().decode())"
```

To turn it off:

```powershell
python -c "import daemon.autostart_windows as a; a.disable_sessions()"
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v ClawdmeterSessions /f  # same thing, by hand
```

...and delete the `hook_port` line from the config, which is what actually stops
the sidecar from listening. The installed hooks can stay: they POST
asynchronously to a port nobody is listening on, which costs a session nothing.

### Remote fleet (optional)

Shows the Claude Code sessions you drive through **Remote Control** on *other*
machines — but only the ones that need a person. Full design:
[FLEET.md](FLEET.md). **Off by default**, and it is the alternative producer to
the hook sidecar above: run one or the other, never both.

```powershell
# 1. one interactive `claude` login on this machine (an API key does not work)
# 2. in %LOCALAPPDATA%\Clawdmeter\config:
#      fleet = on
#      sessions_budget_bytes = 500
# 3. see what it would send, without touching the device:
python daemon\clawdmeter_fleet.py --once --force
```

To have it start at logon **and be restarted if it dies** — tick
**Start fleet poller at login** in the tray menu, or:

```powershell
python -c "import daemon.autostart_windows as a; a.enable_fleet()"
```

That writes a third `HKCU\...\Run` value, `ClawdmeterFleet`, and arms the
tray's supervisor: the poller stamps
`%LOCALAPPDATA%\Clawdmeter\fleet.heartbeat` on every 30 s listing poll, and
the tray restarts it after four missed stamps. Both halves exist because the
poller once died mid-afternoon and the device showed a nine-hour-old list until
somebody noticed by eye.

Logs: `%LOCALAPPDATA%\Clawdmeter\fleet.log` (no console under `pythonw`).

To turn it off:

```powershell
python -c "import daemon.autostart_windows as a; a.disable_fleet()"
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v ClawdmeterFleet /f
```

...and set `fleet = off` in the config, which is what actually stops a running
poller from publishing. Disabling the autostart entry also disarms the
supervisor; it never restarts a poller the owner has not asked for.

### WSL independence

The daemon operates fully independently of WSL. The token is read from native Windows
credential paths (`%USERPROFILE%\.claude\.credentials.json` and fallbacks); BLE uses
the WinRT stack directly. Running `wsl --shutdown` does not affect the BLE link, and
the daemon starts correctly even in a fresh Windows session where WSL has never been
launched.

---

## What is NOT covered here

- PyInstaller / one-file `.exe` packaging — v2
- MAC-address cache / sleep-wake reconnect hardening — Phase 3
