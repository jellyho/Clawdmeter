# Remote fleet — your sessions on other machines

`clawdmeter_sessions.py` shows the Claude Code chats on **this** machine, fed
by hooks. This is the other half: the sessions you drive through **Remote
Control**, running on machines you are not sitting at.

It produces the same wire payload and writes the same handoff file, so the BLE
daemons ship it unchanged and **the firmware needs no changes at all** — a
remote session renders exactly like a local one, including the auto-jump when
one starts waiting on you.

```
Anthropic sessions API ──30 s poll──▶ clawdmeter_fleet.py
                                          │ bridge rows only
                                          │ this machine's own rows removed
                                          │ sorted attention-first, fitted
                                          ▼
                                ~/.clawdmeter/sessions.json
                                          │ on change
                        BLE daemon ──────▶ device SS characteristic
```

## Read this before you turn it on

The listing endpoint is **internal and undocumented**. It appears nowhere in
Anthropic's public API reference. Claude Code's own remote-fleet view is, as of
2.1.263, already stubbed out — `listRemoteSessions` returns an empty list
behind a disabled gate, with the row mapper it used sitting unused beside it.
That surface is visibly mid-refactor and can change or disappear without
notice.

The only *supported* way to reach the same rows is the in-session `ListAgents`
tool, which the cross-session-messaging docs describe as showing "your Remote
Control sessions on other machines". It costs a model turn per call, which is a
poor trade for a device whose whole job is watching your quota — hence the
direct poll, and hence the care below.

So every failure is treated as a **normal state**: a 401, a 404, a changed
response shape, or no network leaves the last good payload alone and logs once.
Nothing here can write a malformed handoff file, because the daemon ships that
file straight to hardware.

If it breaks, the device keeps working. You lose the sessions tab, not the
usage screen.

## Setup

1. **Log in interactively.** The endpoint is first-party only. An
   `ANTHROPIC_API_KEY` does not work, and neither does a `claude setup-token`
   credential — that one lacks the `user:sessions:claude_code` scope the
   listing requires. You need the token an interactive `claude` login writes.

2. **Turn it on** in `~/.config/claude-usage-monitor/config`:

   ```ini
   fleet = on
   ```

3. **Check what it sees**, without touching the device:

   ```bash
   python3 daemon/clawdmeter_fleet.py --once --force
   ```

   That prints the exact payload the daemon would ship, or exits non-zero and
   says why. `--force` ignores the config switch so you can try it before
   committing.

4. **Run it** alongside the BLE daemon:

   ```bash
   python3 daemon/clawdmeter_fleet.py
   ```

> **Run one producer at a time.** This and the hook sidecar write the same
> `~/.clawdmeter/sessions.json`. Pick local chats or the remote fleet — running
> both makes them fight over the file.

## What gets shown

| Rule | Why |
| --- | --- |
| `environment_kind == "bridge"` only | Remote Control on a real machine. Cloud/BYOC rows are a different product surface you did not start from a terminal. |
| Archived and failed rows dropped | Not live status. |
| This machine's own rows dropped | Matched against `bridgeSessionId` in `~/.claude/sessions/<pid>.json`. You are already looking at that screen. |
| Disconnected rows dropped | A disconnected bridge is a machine asleep or offline. On a device you read at a glance, a stale card is worse than no card. Pass `--show-offline` to keep them. |

## State mapping

| API `worker_status` | Device state | Notes |
| --- | --- | --- |
| `requires_action` + a named tool | needs permission | Renders as "allow Bash?" and fires the auto-jump |
| `requires_action`, no tool | needs input | Same waiting bucket |
| `running` + a named tool | running *tool* | |
| `running`, no tool | thinking | |
| `idle` | idle | |
| anything else | starting | Forward-compatible: a new status shows as a card rather than vanishing |

Fields this API does not expose — context fill, token count, model, todo and
subagent counts — go out as the documented "unknown" values, so the device
**hides** those elements rather than drawing a confident zero. The label is the
session `title`, falling back to the git repo name, then to "remote"; these
rows carry no usable working directory.

## Limits

- **Polling, not push.** 30 s, matching the interval the official client uses
  for its own roster. A session that starts waiting is on the panel within
  half a minute, not instantly.
- **No context bar.** The API does not report context usage, so remote cards
  show the name, the state and the elapsed time only.
- **Token expiry** is the platform daemon's problem; this module re-reads the
  credential file every poll rather than caching a token that goes stale in
  hours.
