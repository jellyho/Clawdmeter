#!/usr/bin/env python3
"""Clawdmeter fleet poller — your Claude Code sessions on OTHER machines.

`clawdmeter_sessions.py` shows the chats on THIS machine, fed by Claude Code
hooks. This module answers the other half of the question: what are the
sessions I drive through Remote Control doing, on the machines I am not
sitting at?

It produces the SAME wire payload and writes the SAME handoff file
(`~/.clawdmeter/sessions.json`), so every BLE daemon ships it unchanged and
the firmware renders it with no changes at all. Only the source differs.

    Anthropic sessions API --30 s poll--> clawdmeter_fleet.py
                                              | filter, map, sort, fit
                                              v
                                    ~/.clawdmeter/sessions.json
                                              |
                        BLE daemon --------> device SS characteristic

OFF BY DEFAULT. Set `fleet = on` in ~/.config/claude-usage-monitor/config.
Run only one producer at a time: this and the hook sidecar write the same
file, so pick local sessions or the remote fleet, not both.

--- A warning that belongs in the source, not just the docs -----------------

The listing endpoint is INTERNAL AND UNDOCUMENTED. It appears nowhere in
Anthropic's public API reference. Claude Code's own remote-fleet view is, as
of 2.1.263, already stubbed out (`listRemoteSessions` returns an empty list
behind a disabled gate) while the row mapper it used sits unused beside it --
so this surface is visibly mid-refactor and can change or vanish without
notice. The only supported way to see this data is the in-session
`ListAgents` tool, which reaches the same rows but costs a model turn per
call, which is a poor trade for a device whose whole job is watching quota.

Therefore: every failure here is a NORMAL STATE, not an error. A 401, a 404,
a changed shape, or no network leaves the last good payload alone and logs
once. Nothing in this file may ever write a malformed handoff file -- the
daemon ships that straight to hardware.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# The sidecar owns the wire format, the sort, the byte fitting and the atomic
# write. Reuse all of it rather than growing a second, drifting copy.
try:
    from . import clawdmeter_sessions as cs
except ImportError:  # run as a script, not a package
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import clawdmeter_sessions as cs

API_URL = "https://api.anthropic.com/v1/code/sessions"
API_TIMEOUT_S = 10
POLL_INTERVAL_S = 30      # what the official client uses for its own roster
PAGE_LIMIT = 100
MAX_PAGES = 10            # the client's own cap
MAX_ROWS = 50             # ditto

CREDENTIALS_FILE = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
LOCAL_SESSIONS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "sessions")

# Only rows that run on a real machine behind Remote Control. The other kinds
# (anthropic_cloud, byoc, snap) are a different product surface; including them
# would put things on the panel the owner never started.
BRIDGE_KIND = "bridge"
DEAD_STATUSES = frozenset(("archived", "failed"))


def log(msg):
    print(f"[fleet] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def read_token(path=None):
    """The Claude Code OAuth access token, or None.

    This endpoint is first-party only: an ANTHROPIC_API_KEY does not work, and
    neither does a `claude setup-token` credential -- that one lacks the
    `user:sessions:claude_code` scope the listing requires. It has to be the
    token written by an interactive login.

    Read fresh on every poll rather than cached: the token carries an
    expiresAt only hours out, and the platform daemons refresh the file
    underneath us.
    """
    path = path or CREDENTIALS_FILE
    try:
        with open(path, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        return None
    oauth = blob.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    return token if isinstance(token, str) and token else None


def local_bridge_ids(sessions_dir=None):
    """Bridge ids belonging to sessions on THIS machine, prefix-stripped.

    Every local session writes <sessions>/<pid>.json, and a Remote-Control
    session records its server-side id there as `bridgeSessionId`. The listing
    returns those same sessions, so without this the owner's own laptop would
    fill the panel it is sitting in front of.
    """
    out = set()
    sessions_dir = sessions_dir or LOCAL_SESSIONS_DIR
    try:
        names = os.listdir(sessions_dir)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions_dir, name), "r", encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        bid = rec.get("bridgeSessionId")
        if isinstance(bid, str) and bid:
            out.add(strip_id_prefix(bid))
    return out


def strip_id_prefix(sid):
    """`session_01ABC` / `cse_01ABC` -> `01ABC`. The two sides of the same id
    are spelled differently depending on which surface produced it."""
    if not isinstance(sid, str):
        return ""
    for prefix in ("session_", "cse_"):
        if sid.startswith(prefix):
            return sid[len(prefix):]
    return sid


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _request(url, token, opener=None):
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("anthropic-client-platform", "claude_code_cli")
    open_fn = opener or urllib.request.urlopen
    with open_fn(req, timeout=API_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_sessions(token, opener=None):
    """Every session row the account can see, following cursors.

    Returns a list, or None to mean "could not tell" -- which is different from
    an empty list meaning "nothing is running". The caller must not publish an
    empty panel because the network blinked.
    """
    rows = []
    cursor = None
    for _ in range(MAX_PAGES):
        url = f"{API_URL}?limit={PAGE_LIMIT}"
        if cursor:
            url += f"&cursor={urllib.parse.quote(str(cursor))}"
        try:
            body = _request(url, token, opener)
        except urllib.error.HTTPError as exc:
            log(f"listing failed: HTTP {exc.code} (endpoint is undocumented; it may have moved)")
            return None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"listing failed: {type(exc).__name__}: {exc}")
            return None
        if not isinstance(body, dict):
            log("listing failed: response was not an object")
            return None
        page = body.get("data")
        if not isinstance(page, list):
            log("listing failed: no 'data' array (shape changed?)")
            return None
        rows.extend(r for r in page if isinstance(r, dict))
        if len(rows) >= MAX_ROWS:
            break
        cursor = body.get("next_cursor")
        if not cursor:
            break
    return rows[:MAX_ROWS]


# ---------------------------------------------------------------------------
# Map an API row onto the device's wire row
# ---------------------------------------------------------------------------

def _pending_action(row):
    meta = row.get("external_metadata")
    if not isinstance(meta, dict):
        return {}
    pa = meta.get("pending_action")
    return pa if isinstance(pa, dict) else {}


def _tool_of(row):
    pa = _pending_action(row)
    for key in ("display_tool_name", "tool_name"):
        name = pa.get(key)
        if isinstance(name, str) and name:
            code = cs.tool_code(name)
            if code:
                return code
    return 0


def row_state(row):
    """worker_status -> the device's state code.

    `requires_action` is the whole point of the feature: it is what puts a card
    in the waiting bucket, sorts it to the top, and fires the device's
    auto-jump. Split it by whether a tool is named so the panel can say
    "allow Bash?" rather than the vaguer "needs input".
    """
    status = row.get("worker_status")
    if status == "requires_action":
        return cs.STATE_WAITING_PERMISSION if _tool_of(row) else cs.STATE_WAITING_INPUT
    if status == "running":
        return cs.STATE_RUNNING_TOOL if _tool_of(row) else cs.STATE_THINKING
    if status == "idle":
        return cs.STATE_IDLE
    return cs.STATE_STARTING


def _epoch(value):
    """Best-effort timestamp -> epoch seconds. 0 when unreadable."""
    if isinstance(value, (int, float)):
        # Milliseconds if it is implausibly large for seconds.
        return float(value) / 1000.0 if value > 1e11 else float(value)
    if isinstance(value, str) and value:
        text = value.replace("Z", "+00:00")
        try:
            from datetime import datetime
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _label_of(row):
    title = row.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    # No usable cwd on these rows -- the official client falls back to the repo
    # URL, then to the literal word "remote". Do the same.
    config = row.get("config")
    if isinstance(config, dict):
        sources = config.get("sources")
        if isinstance(sources, list):
            for src in sources:
                if isinstance(src, dict):
                    url = src.get("url") or src.get("repository")
                    if isinstance(url, str) and url:
                        return url.rstrip("/").rsplit("/", 1)[-1] or "remote"
    return "remote"


def to_wire_row(row, now=None):
    """One API row -> the positional wire row the firmware parses.

    Fields this API cannot supply (context fill, token count, model, todo and
    subagent counts) go out as the documented "unknown" values rather than as
    zeroes, so the device hides those elements instead of drawing a confident
    lie.
    """
    now = time.time() if now is None else now
    last = _epoch(row.get("last_event_at") or row.get("updated_at") or row.get("created_at"))
    elapsed = int(max(0.0, now - last)) if last else 0
    return [
        cs.short_sid(strip_id_prefix(row.get("id") or "")),
        _label_of(row),
        row_state(row),
        -1,                 # ctx: not exposed by this API
        elapsed,
        0,                  # model: not exposed
        _tool_of(row),
        0, 0, 0, 0,         # ntools / nagents / tdone / ttotal: not exposed
        -1,                 # tok: not exposed
        cs.REMOTE_ON,       # every row here is a Remote Control session
    ]


def select_rows(api_rows, exclude_ids=None, show_offline=False):
    """Bridge rows worth showing, sorted attention-first."""
    exclude = exclude_ids if exclude_ids is not None else set()
    keep = []
    for row in api_rows:
        if row.get("environment_kind") != BRIDGE_KIND:
            continue
        if row.get("status") in DEAD_STATUSES:
            continue
        if strip_id_prefix(row.get("id") or "") in exclude:
            continue          # this machine; the owner is already looking at it
        # A disconnected bridge is a machine that is asleep or offline. It is
        # not live status, and a stale card is worse than no card on a device
        # you read at a glance -- so it is dropped unless asked for.
        if not show_offline and row.get("connection_status") == "disconnected":
            continue
        keep.append(row)

    now = time.time()
    keep.sort(key=lambda r: (
        cs.state_bucket(row_state(r)),
        -_epoch(r.get("last_event_at") or r.get("updated_at") or r.get("created_at")),
    ))
    return [to_wire_row(r, now) for r in keep]


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

def build_payload(api_rows, budget, exclude_ids=None, show_offline=False):
    rows = select_rows(api_rows, exclude_ids, show_offline)
    return cs.fit_payload(rows, budget)


def poll_once(budget, show_offline=False, opener=None, token=None):
    """One cycle. Returns the wire payload, or None when nothing can be said."""
    token = token or read_token()
    if not token:
        log("no OAuth token found -- log in with `claude` first")
        return None
    api_rows = fetch_sessions(token, opener)
    if api_rows is None:
        return None       # transport or shape failure: keep the last good panel
    return build_payload(api_rows, budget, local_bridge_ids(), show_offline)


def _enabled(config_path=None):
    value = cs.read_config_value("fleet", config_path)
    return str(value).strip().lower() in ("1", "on", "true", "yes") if value else False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true",
                        help="poll once, print the payload, exit")
    parser.add_argument("--force", action="store_true",
                        help="ignore the `fleet` config switch")
    parser.add_argument("--show-offline", action="store_true",
                        help="include bridge sessions whose machine is disconnected")
    parser.add_argument("--budget", type=int, default=None,
                        help=f"payload byte budget (default {cs.DEFAULT_BUDGET_BYTES})")
    args = parser.parse_args(argv)

    if not args.force and not _enabled():
        log("disabled: set `fleet = on` in ~/.config/claude-usage-monitor/config")
        return 0

    budget = args.budget
    if budget is None:
        raw = cs.read_config_value("sessions_budget_bytes")
        try:
            budget = int(raw) if raw else cs.DEFAULT_BUDGET_BYTES
        except ValueError:
            budget = cs.DEFAULT_BUDGET_BYTES

    if args.once:
        payload = poll_once(budget, args.show_offline)
        if payload is None:
            return 1
        print(payload)
        return 0

    log(f"polling every {POLL_INTERVAL_S}s -> {cs.DEFAULT_SESSIONS_FILE}")
    last = None
    while True:
        payload = poll_once(budget, args.show_offline)
        if payload is not None and payload != last:
            cs.write_sessions_file(cs.DEFAULT_SESSIONS_FILE, payload)
            last = payload
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main())
