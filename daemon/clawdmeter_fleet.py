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
import hashlib
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
    from . import clawdmeter_inbox as inbox
except ImportError:  # run as a script, not a package
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import clawdmeter_sessions as cs
    import clawdmeter_inbox as inbox

API_URL = "https://api.anthropic.com/v1/code/sessions"
API_TIMEOUT_S = 10
POLL_INTERVAL_S = 30      # what the official client uses for its own roster
# The loop ticks faster than it polls. Remote session status genuinely cannot
# be fresher than the 30 s listing, but a cross-session MESSAGE is already on
# disk the moment it arrives -- making it wait for the next listing would add
# up to 30 s of latency for nothing. On a 2 s tick a message reaches the
# handoff file almost immediately and the BLE daemon's own 5 s tick ships it,
# so the panel lights up within seconds. See _significant() for why this does
# not turn into a GATT write every two seconds.
TICK_S = 2
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


def fleet_sid(row_id):
    """2 hex chars keying a card's identity across polls.

    NOT cs.short_sid(): that takes the first two characters when they are
    already hex, which is right for local UUID session ids but degenerate
    here -- every server-side id in this listing is ULID-shaped and begins
    "01", so four different machines all came back as card "01" and the
    firmware's reorder animation could not tell them apart. Hash always.
    """
    return hashlib.md5((row_id or "").encode("utf-8")).hexdigest()[:2]


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
        fleet_sid(row.get("id") or ""),
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

def merge_rows(api_rows, exclude_ids=None, show_offline=False, inbox_rows=None):
    """Inbox rows FIRST (messages and agent reports), then the remote sessions.

    Messages jump the attention-first sort rather than being fed through it:
    a message is a person asking for something, which outranks any machine
    state, and the sort key it would need does not exist (state_bucket() lives
    in the sidecar and puts an unknown code in the idle bucket). Reports ride
    the same lane and are already rank-sorted among themselves by
    inbox.row_rank; a full report round can legitimately fill the payload and
    evict every session card, which is what pressing the report button asked
    for.

    Order matters for more than looks -- cs.fit_payload() drops from the TAIL
    when the byte budget runs out, so putting messages first is also what
    guarantees the message survives and a session card is what gets evicted.
    """
    return list(inbox_rows or []) + select_rows(api_rows, exclude_ids, show_offline)


def build_payload(api_rows, budget, exclude_ids=None, show_offline=False,
                  inbox_rows=None):
    return cs.fit_payload(
        merge_rows(api_rows, exclude_ids, show_offline, inbox_rows), budget)


def _significant(rows):
    """The part of a payload that means something changed.

    Every row carries `elapsed`, which ticks up on its own, so comparing whole
    payloads on a 2 s loop would look like a change every 2 s and turn into a
    BLE write every 5 s. Blank index 4 and compare the rest: a new message, a
    state change or a reorder is significant; a clock advancing is not.
    """
    return json.dumps([[0 if i == 4 else c for i, c in enumerate(r)] for r in rows],
                      separators=(",", ":"), ensure_ascii=False)


def poll_once(budget, show_offline=False, opener=None, token=None, watcher=None):
    """One cycle. Returns the wire payload, or None when nothing can be said."""
    inbox_rows = []
    if watcher is not None:
        watcher.poll()
        # The budget goes in explicitly: a report ROUND is fitted by the
        # watcher itself (inbox.fit_round), because cs.fit_payload below drops
        # from the tail and the tail of a round is the marker that says what
        # was dropped.
        inbox_rows = watcher.rows(budget=budget)
    token = token or read_token()
    if not token:
        log("no OAuth token found -- log in with `claude` first")
        # A message needs no token: it was read off local disk. Publishing
        # just the messages is better than publishing nothing.
        return build_payload([], budget, set(), show_offline, inbox_rows) \
            if inbox_rows else None
    api_rows = fetch_sessions(token, opener)
    if api_rows is None:
        return None       # transport or shape failure: keep the last good panel
    return build_payload(api_rows, budget, local_bridge_ids(), show_offline,
                         inbox_rows)


def run_loop(budget, show_offline=False, watcher=None, tick_s=TICK_S,
             poll_interval_s=POLL_INTERVAL_S, sessions_file=None,
             iterations=None, sleep_fn=time.sleep, now_fn=time.time):
    """The service loop: poll the listing slowly, the inbox quickly.

    Publishes only when something actually changed (see _significant), with a
    refresh no less often than the listing interval so `elapsed` on the panel
    does not freeze. Stays completely silent until it has something real to
    say, so a machine with no token and no messages never blanks a panel some
    other producer filled.
    """
    sessions_file = sessions_file or cs.DEFAULT_SESSIONS_FILE
    api_rows = []
    exclude = set()
    have_listing = False
    warned_token = False
    next_poll = 0.0
    last_payload = last_sig = None
    last_write = 0.0
    count = 0

    while iterations is None or count < iterations:
        count += 1
        now = now_fn()

        if now >= next_poll:
            next_poll = now + poll_interval_s
            token = read_token()
            if token:
                warned_token = False
                fetched = fetch_sessions(token)
                if fetched is not None:
                    api_rows = fetched
                    exclude = local_bridge_ids()
                    have_listing = True
            elif not warned_token:
                warned_token = True
                log("no OAuth token found -- remote sessions off "
                    "(messages, which need none, still work)")

        inbox_rows = []
        if watcher is not None:
            watcher.poll()
            inbox_rows = watcher.rows(now, budget=budget)
        rows = merge_rows(api_rows, exclude, show_offline, inbox_rows)

        # Stay quiet only until this loop has published something. The guard
        # exists so a machine with no token and no mail never blanks a panel
        # the hook sidecar filled -- but ONCE WE HAVE WRITTEN THE FILE WE OWN
        # IT, and an expired message has to be retractable. Without the
        # `last_payload is None` half, a run with no usable listing (no token,
        # or an endpoint that 404s -- two of the three transport states) would
        # publish a message and then never be able to take it back: the row
        # would ship forever, at a frozen age, and the sessions view could
        # never show a session again.
        if not rows and not have_listing and last_payload is None:
            sleep_fn(tick_s)          # nothing to say yet: do not blank anything
            continue

        payload = cs.fit_payload(rows, budget)
        sig = _significant(rows)
        stale = (now - last_write) >= poll_interval_s
        if payload != last_payload and (sig != last_sig or stale):
            cs.write_sessions_file(sessions_file, payload)
            last_payload, last_sig, last_write = payload, sig, now

        sleep_fn(tick_s)
    return last_payload


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
    parser.add_argument("--no-inbox", action="store_true",
                        help="do not show messages other Claude sessions send here")
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

    watcher = None
    if not args.no_inbox and inbox.enabled():
        watcher = inbox.watcher_from_config(budget)

    if args.once:
        payload = poll_once(budget, args.show_offline, watcher=watcher)
        if payload is None:
            return 1
        print(payload)
        return 0

    log(f"listing every {POLL_INTERVAL_S}s, inbox every {TICK_S}s "
        f"-> {cs.DEFAULT_SESSIONS_FILE}")
    if watcher is None:
        log("inbox off (set `inbox = on` in the config, or drop --no-inbox)")
    run_loop(budget, args.show_offline, watcher)


if __name__ == "__main__":
    sys.exit(main())
