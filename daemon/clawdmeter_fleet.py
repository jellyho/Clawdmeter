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
file, so pick local sessions or the remote fleet, not both. (This module now
holds a named single-instance lock, so a second copy of ITSELF exits quietly
rather than fighting the first over that file.)

--- What goes on the panel: attention, not a roster ------------------------

The listing is polled for one thing above all others: `requires_action`. That
is the only way the device learns an agent needs a human WITHOUT being asked,
and it is the most valuable thing this whole product does. Everything else the
listing returns -- idle machines, machines merely working -- is a roster, and a
roster is not information on a device you read at a glance. Nine rows, seven
idle, two of them idle for five and seven DAYS, is noise that crowds out the
one row that mattered.

So `attention_only` (config `fleet_attention_only`, on by default) keeps only
the rows in the WAITING bucket and drops the rest before they cost a byte.
Messages and agent reports are unaffected: they come from local disk, they are
unsolicited or explicitly asked for, and they are the other two things that
need a person.

--- Staleness: the failure this module used to hide ------------------------

The loop writes the handoff file ONLY WHEN SOMETHING CHANGES, so "no write" is
ambiguous between "the fleet is quiet" and "everything is broken". That
ambiguity cost a real nine-hour outage: a token expired, every poll took HTTP
401, this module correctly kept the last good payload rather than blanking the
panel -- and nothing on the device or in the UI distinguished seven genuinely
quiet agents from a list captured the previous night.

The device cannot break that tie from a timer (a quiet fleet legitimately
sends nothing for hours). The HOST can, because the host knows when it last
succeeded. So after `fleet_stale_after_s` (900 s) of continuous failure this
module DROPS every row that came from the listing and sends one host-minted
row in their place -- state 17, SESSION_HOST_STALE, the same pattern as the
report overflow marker. See ListingHealth below for why the threshold is what
it is and why a 401 must not shout immediately.

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
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

# The sidecar owns the wire format, the sort, the byte fitting and the atomic
# write. Reuse all of it rather than growing a second, drifting copy.
try:
    from . import autostart_windows as autostart
    from . import clawdmeter_sessions as cs
    from . import clawdmeter_inbox as inbox
except ImportError:  # run as a script, not a package
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import autostart_windows as autostart
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

# ---- Staleness ------------------------------------------------------------

# How long the listing must fail CONTINUOUSLY before the device is told. 900 s
# is 30 consecutive poll failures, and the number is chosen against the 401
# case specifically, because that is the common one: the OAuth token lives
# about five hours and Claude Code refreshes the credential file on its own,
# which this module re-reads every poll. A refresh therefore heals a 401 by
# itself, usually within a poll or two -- so shouting on the first 401 would
# mean shouting several times a day about nothing, and the marker would stop
# meaning anything. Fifteen minutes gives a refresh thirty chances to land and
# still leaves the panel honest inside a coffee break, against the NINE HOURS
# it was wrong for on the day this was written.
STALE_AFTER_S = 900

# Wire code 17, appended after the report overflow marker (16). Both are
# host-minted rows that are not agents; firmware/src/data.h says the codes are
# append-only because they cross the BLE boundary.
STATE_HOST_STALE = 17

# Its own sid, disjoint from every other producer's. Message and session sids
# are two hex characters; report sids are [g-y][0-9a-z]; the report overflow
# marker took "zz". "z0" collides with none of them, and there is only ever
# one of these rows.
STALE_SID = "z0"
STALE_LABEL = "FLEET DATA STALE"

# Why the listing is blind, in the host's own words, because the host is the
# only side that knows. Kept short: this is the card's body, drawn dim over
# two lines, and it has to survive `panel_label`-class folding untouched.
REASON_NO_TOKEN = "no login found - run claude"
REASON_AUTH = "auth expired - log in to claude"
REASON_FORBIDDEN = "listing refused - no access"
REASON_UNREACHABLE = "host cannot reach the listing"
REASON_SHAPE = "listing moved or changed shape"

# ---- Liveness -------------------------------------------------------------

# The tray supervises this process across the process boundary, so there has
# to be something to look at. Written next to the logs on every listing poll.
HEARTBEAT_NAME = "fleet.heartbeat"
# Missed four polls. Long enough that a slow listing call, a laptop resume or a
# clock nudge is not a "death", short enough that a real one is noticed inside
# a couple of minutes rather than a couple of hours.
HEARTBEAT_STALE_S = 120
# One poller per logon session. Without this, giving the poller an autostart
# entry would create the exact hazard FLEET.md warns about -- two producers
# fighting over ~/.clawdmeter/sessions.json -- the first time the owner
# started one by hand as well.
SINGLETON_MUTEX_NAME = "Local\\Clawdmeter-fleet-singleton"


def log(msg):
    # print() to a dead stream is NOT harmless here: under the pythonw.exe an
    # HKCU\Run entry starts, there is no console at all and sys.stderr is
    # None, so `print(file=sys.stderr)` raises AttributeError. That exception
    # would propagate out of fetch_sessions' own except block -- i.e. the
    # first HTTP 401 would kill the poller outright. Same guard as
    # clawdmeter_sessions.log(), for the same reason, and mirrored into the
    # rotating file log so an autostarted poller leaves a trail at all.
    try:
        print(f"[fleet] {msg}", file=sys.stderr, flush=True)
    except (OSError, ValueError, AttributeError, RuntimeError):
        pass
    cs.file_log(f"[fleet] {msg}")


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


def http_reason(code):
    """An HTTP status -> the sentence the device gets, if this keeps up.

    401 is called out on its own because it is the failure this module sees in
    the field: the token expires roughly every five hours. The words name the
    fix rather than the symptom -- an interactive `claude` login is what writes
    the credential the listing needs (FLEET.md, Setup step 1).
    """
    if code == 401:
        return REASON_AUTH
    if code == 403:
        return REASON_FORBIDDEN
    if code in (404, 410):
        return REASON_SHAPE
    return f"listing error - HTTP {code}"


def fetch_sessions(token, opener=None, health=None):
    """Every session row the account can see, following cursors.

    Returns a list, or None to mean "could not tell" -- which is different from
    an empty list meaning "nothing is running". The caller must not publish an
    empty panel because the network blinked.

    `health` is the optional ListingHealth this fetch reports into. Every exit
    path records one of ok() / fail(reason), because a failure that is not
    recorded is a failure the device will never hear about -- which is the bug
    this parameter exists to close.
    """
    def failed(reason, detail):
        log(f"listing failed: {detail}")
        if health is not None:
            health.fail(reason)
        return None

    rows = []
    cursor = None
    for _ in range(MAX_PAGES):
        url = f"{API_URL}?limit={PAGE_LIMIT}"
        if cursor:
            url += f"&cursor={urllib.parse.quote(str(cursor))}"
        try:
            body = _request(url, token, opener)
        except urllib.error.HTTPError as exc:
            return failed(http_reason(exc.code),
                          f"HTTP {exc.code} (endpoint is undocumented; it may have moved)")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return failed(REASON_UNREACHABLE, f"{type(exc).__name__}: {exc}")
        if not isinstance(body, dict):
            return failed(REASON_SHAPE, "response was not an object")
        page = body.get("data")
        if not isinstance(page, list):
            return failed(REASON_SHAPE, "no 'data' array (shape changed?)")
        rows.extend(r for r in page if isinstance(r, dict))
        if len(rows) >= MAX_ROWS:
            break
        cursor = body.get("next_cursor")
        if not cursor:
            break
    if health is not None:
        health.ok()
    return rows[:MAX_ROWS]


# ---------------------------------------------------------------------------
# Is the listing telling us the truth, and for how long has it not been?
# ---------------------------------------------------------------------------

class ListingHealth(object):
    """Tracks how long the listing has been failing, and mints the marker.

    The contract, stated once:

      * a SUCCESS resets everything -- the clock, the streak, the reason;
      * a FAILURE inside the grace window changes nothing the device can see.
        The last good rows keep shipping, which is right: a thirty-second blip
        must not blank a panel, and the ages on those rows are computed from
        the events' own timestamps, so they stay honest while they age;
      * a failure streak older than `stale_after_s` arms the marker, and from
        that moment the caller drops every listing row and sends the marker in
        their place.

    Dropping the rows rather than dimming them is the deliberate half. After
    the attention filter the only listing rows that survive at all are
    `requires_action` ones -- "this agent needs you NOW" -- and a nine-hour-old
    one of those is worse than nothing: it is terra-cotta, it pulses, it sorts
    to the top and it can pull the panel to this tab, all to send the owner to
    a machine where nobody is waiting. The panel's own precedent agrees: the
    usage screen replaces stale numbers with the idle screen rather than
    dimming them ("we never render hours-old numbers as if they were live").

    Messages and agent reports are NOT dropped. They are read from local disk,
    they carry their own expiry, and they are still true while the listing is
    blind -- so the marker means "the listing half of this tab is dark", not
    "nothing here can be trusted".
    """

    def __init__(self, stale_after_s=STALE_AFTER_S, now=None, now_fn=time.time):
        # One clock for the whole object, injectable. fetch_sessions() records
        # into this from deep inside its own call stack and has no idea what
        # clock its caller is running on, so the clock has to live here or a
        # loop driven by a fake one would mix two time bases.
        self._now = now_fn
        self.stale_after_s = stale_after_s
        self.started = now_fn() if now is None else now
        self.last_ok = None
        self.reason = None        # None => not currently failing
        self.failures = 0
        self._shouted = False

    # -- transitions --------------------------------------------------------

    def ok(self, now=None):
        now = self._now() if now is None else now
        if self.failures:
            log(f"listing recovered after {self.failures} failure(s)")
        self.last_ok = now
        self.reason = None
        self.failures = 0
        self._shouted = False

    def fail(self, reason, now=None):
        now = self._now() if now is None else now
        first = self.failures == 0
        self.failures += 1
        self.reason = reason
        if first:
            log(f"listing failing ({reason}); keeping the last good rows for "
                f"{self.stale_after_s}s before telling the device")
        elif self.is_stale(now) and not self._shouted:
            self._shouted = True
            log(f"listing has been failing for {int(self.age(now))}s "
                f"({reason}) -- dropping its rows and marking the panel stale")

    # -- queries ------------------------------------------------------------

    def age(self, now=None):
        """Seconds since the last good listing.

        Falls back to this object's birth when there has never been a good one:
        a poller that has never reached the endpoint is exactly as blind as one
        that reached it last night, and the owner should hear about both.
        """
        now = self._now() if now is None else now
        since = self.started if self.last_ok is None else self.last_ok
        return max(0.0, now - since)

    def is_stale(self, now=None):
        return self.reason is not None and self.age(now) >= self.stale_after_s

    def stale_row(self, now=None):
        """The host-minted wire row, or None while the listing is trustworthy."""
        now = self._now() if now is None else now
        if not self.is_stale(now):
            return None
        return [
            STALE_SID,
            STALE_LABEL,
            STATE_HOST_STALE,
            -1,                     # ctx: not a session
            int(self.age(now)),     # the card dates itself
            0, 0, 0, 0, 0, 0,       # model/tool/counts: not applicable
            -1,                     # tok
            cs.REMOTE_UNKNOWN,      # remote: meaningless for a host notice
            self.reason,            # index 13: why, in words
        ]


# ---------------------------------------------------------------------------
# Heartbeat -- so something other than this process can notice it died
# ---------------------------------------------------------------------------

def heartbeat_path(base=None):
    """Where the liveness file lives: beside the logs on Windows, beside the
    handoff file elsewhere."""
    if base:
        return base
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(root, "Clawdmeter", HEARTBEAT_NAME)
    return os.path.join(os.path.expanduser("~"), ".clawdmeter", HEARTBEAT_NAME)


def write_heartbeat(path=None, now=None, health=None):
    """Stamp liveness. Best-effort and total: a supervisor's convenience must
    never be able to take down the thing it is supervising."""
    path = heartbeat_path(path)
    now = time.time() if now is None else now
    doc = {
        "pid": os.getpid(),
        "ts": now,
        "last_ok": None if health is None else health.last_ok,
        "failures": 0 if health is None else health.failures,
        "reason": None if health is None else health.reason,
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, path)
    except OSError:
        pass
    return doc


def read_heartbeat(path=None):
    """The last stamp as a dict, or None when there is not one to read."""
    path = heartbeat_path(path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        # A torn read (we are mid-replace) or a hand-mangled file must not read
        # as death -- fall back to the mtime, which is the only fact that
        # actually matters to a supervisor.
        try:
            return {"ts": os.path.getmtime(path), "pid": None}
        except OSError:
            return None
    return doc if isinstance(doc, dict) else None


def heartbeat_age(path=None, now=None):
    """Seconds since the poller last stamped, or None if it never has."""
    doc = read_heartbeat(path)
    if not doc:
        return None
    ts = doc.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    now = time.time() if now is None else now
    return max(0.0, now - float(ts))


def is_alive(path=None, now=None, max_age_s=HEARTBEAT_STALE_S):
    """True when a poller stamped recently enough to be believed."""
    age = heartbeat_age(path, now)
    return age is not None and age <= max_age_s


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


def needs_a_human(state):
    """The filter, in one predicate: does this row want a PERSON?

    Yes for the whole waiting bucket -- an agent stopped at a permission
    prompt, asking a question, wanting input, or in error. Those are the rows
    the panel exists to surface, and `requires_action` in the listing is the
    only way the device ever learns about one without the owner asking.

    No for idle and no for merely running. A remote agent quietly working is
    not news, and a remote agent idle for seven days is anti-news: it is a name
    occupying a card that the one row that mattered could have had. The
    firmware buys attention with the same currency -- accent, pulse, sort
    order, one auto-jump -- so filling the tab with rows that never earn any of
    it is what made the plain list meaningless.

    Reports and messages do not come through here at all; they are minted by
    clawdmeter_inbox from local disk and are attention-shaped by construction
    (somebody wrote to you, or you pressed the button that asked).
    """
    return state in cs.WAITING_STATES


def select_rows(api_rows, exclude_ids=None, show_offline=False,
                attention_only=True):
    """Bridge rows worth showing, sorted attention-first.

    `attention_only` is the roster/attention switch (config
    `fleet_attention_only`, on by default). Off, this returns the full bridge
    roster it always did.
    """
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
        # Last, and cheapest to reason about: everything above decides whether
        # the row is REAL, this decides whether it is worth a card.
        if attention_only and not needs_a_human(row_state(row)):
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

def merge_rows(api_rows, exclude_ids=None, show_offline=False, inbox_rows=None,
               attention_only=True, stale=None):
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

    `stale` is the ListingHealth marker when the listing has been failing long
    enough to matter. It REPLACES the listing rows rather than joining them:
    see ListingHealth for why keeping a nine-hour-old "allow Bash?" would be
    worse than showing nothing. The inbox rows stay -- they are read from local
    disk and are still true.
    """
    rows = list(inbox_rows or [])
    if stale is not None:
        return rows + [stale]
    return rows + select_rows(api_rows, exclude_ids, show_offline, attention_only)


def build_payload(api_rows, budget, exclude_ids=None, show_offline=False,
                  inbox_rows=None, attention_only=True, stale=None):
    return cs.fit_payload(
        merge_rows(api_rows, exclude_ids, show_offline, inbox_rows,
                   attention_only, stale), budget)


def row_cost(row):
    """Bytes one row adds to an encoded payload, its separating comma included."""
    empty = len(cs.encode_payload([]).encode("utf-8"))
    return len(cs.encode_payload([row]).encode("utf-8")) - empty + 1


def inbox_rows_for(watcher, now, budget, stale=None):
    """Message and report rows, with room reserved for the staleness marker.

    The marker is appended LAST, because it is a footnote about the list in the
    same way the report overflow row is, and a footnote belongs under the
    things it is a footnote to. But BOTH caps eat the tail first --
    cs.fit_payload() drops from it, and the firmware parses only
    SESSION_MAX_ROWS rows and discards the rest -- so a footnote left to
    compete at the tail is the first thing thrown away, which would put the
    silence straight back where it was.

    So when the marker is armed, its bytes and its row slot come out of the
    inbox's allowance BEFORE the round is fitted, rather than after. The cost,
    stated plainly: while the listing is stale a full report round shows one
    fewer agent and cuts its summaries a few characters shorter. That is the
    right way round -- an agent card is one of several and the round has its
    own "+N MORE" footnote to say so, while the marker is the only thing on the
    panel that says the other half of this tab is blind.
    """
    if watcher is None:
        return []
    if stale is None:
        return watcher.rows(now, budget=budget)
    return watcher.rows(now, budget=max(1, budget - row_cost(stale)),
                        max_rows=max(1, inbox.DEVICE_MAX_ROWS - 1))


def _significant(rows):
    """The part of a payload that means something changed.

    Every row carries `elapsed`, which ticks up on its own, so comparing whole
    payloads on a 2 s loop would look like a change every 2 s and turn into a
    BLE write every 5 s. Blank index 4 and compare the rest: a new message, a
    state change or a reorder is significant; a clock advancing is not.
    """
    return json.dumps([[0 if i == 4 else c for i, c in enumerate(r)] for r in rows],
                      separators=(",", ":"), ensure_ascii=False)


def poll_once(budget, show_offline=False, opener=None, token=None, watcher=None,
              attention_only=True, health=None, now=None):
    """One cycle. Returns the wire payload, or None when nothing can be said.

    A fresh ListingHealth starts with a zero-length failure streak, so a
    one-shot `--once` run that fails prints nothing rather than a marker --
    which is right, since one failure is not an outage. Pass `--stale-after 0`
    (or a health object) to see what the marker would look like.
    """
    now = time.time() if now is None else now
    if health is None:
        health = ListingHealth(now=now)
    if watcher is not None:
        # poll() only reads files; rows() is deferred until the listing's
        # health is known, because that is what decides the inbox's budget.
        watcher.poll()

    token = token or read_token()
    api_rows = None
    if token:
        api_rows = fetch_sessions(token, opener, health)
    else:
        log("no OAuth token found -- log in with `claude` first")
        health.fail(REASON_NO_TOKEN, now)

    stale = health.stale_row(now)
    inbox_rows = inbox_rows_for(watcher, now, budget, stale)
    if api_rows is None and stale is None:
        # Inside the grace window with no listing. A message needs no token --
        # it was read off local disk -- so publishing just the messages beats
        # publishing nothing; with nothing at all to say, keep the last good
        # panel rather than blanking it.
        return build_payload([], budget, set(), show_offline, inbox_rows,
                             attention_only) if inbox_rows else None
    return build_payload(api_rows or [], budget,
                         local_bridge_ids() if api_rows is not None else set(),
                         show_offline, inbox_rows, attention_only, stale)


def run_loop(budget, show_offline=False, watcher=None, tick_s=TICK_S,
             poll_interval_s=POLL_INTERVAL_S, sessions_file=None,
             iterations=None, sleep_fn=time.sleep, now_fn=time.time,
             attention_only=True, stale_after_s=STALE_AFTER_S,
             heartbeat=None, health=None, dismiss_file=None):
    """The service loop: poll the listing slowly, the inbox quickly.

    Publishes only when something actually changed (see _significant), with a
    refresh no less often than the listing interval so `elapsed` on the panel
    does not freeze. Stays completely silent until it has something real to
    say, so a machine with no token and no messages never blanks a panel some
    other producer filled.
    """
    sessions_file = sessions_file or cs.DEFAULT_SESSIONS_FILE
    dismiss_file = dismiss_file or DISMISS_FILE
    api_rows = []
    exclude = set()
    have_listing = False
    next_poll = 0.0
    last_payload = last_sig = None
    last_roster = None
    last_write = 0.0
    count = 0
    if health is None:
        health = ListingHealth(stale_after_s, now_fn(), now_fn=now_fn)
    write_heartbeat(heartbeat, now_fn(), health)

    while iterations is None or count < iterations:
        count += 1
        now = now_fn()

        if now >= next_poll:
            next_poll = now + poll_interval_s
            token = read_token()
            if token:
                fetched = fetch_sessions(token, health=health)
                if fetched is not None:
                    api_rows = fetched
                    exclude = local_bridge_ids()
                    have_listing = True
            else:
                # A missing credential is a listing failure like any other, and
                # has to be one: it was the once-per-process `warned_token` log
                # line and nothing else that used to stand between the owner
                # and a silently frozen panel.
                health.fail(REASON_NO_TOKEN, now)
            # Stamped on the poll, not the tick: four missed stamps is the
            # supervisor's death test, and a 2 s stamp would make that 8 s.
            write_heartbeat(heartbeat, now, health)

        # Evaluated every TICK rather than every poll -- it is a clock
        # comparison, and the marker should appear the moment it is due rather
        # than up to 30 s later.
        stale = health.stale_row(now)
        if watcher is not None:
            # What the owner has cleared on the device, re-read every tick. The
            # BLE daemon owns that file; this process only ever reads it, so
            # there is one writer and one reader and no locking to get wrong.
            watcher.set_dismissed(read_dismissed(dismiss_file, now))
            watcher.poll()
        # A report card the owner answered by hand, on the machine itself, is
        # no longer true — and only the listing can tell us. Dropped before the
        # rows are built so it never reaches the panel at all.
        if watcher is not None and api_rows:
            cards = watcher.reports_by_key()
            for key, why in answered_elsewhere(cards, api_rows, now).items():
                log(f"retracting {key}: {why}")
                watcher.retract_report(key)
        inbox_rows = inbox_rows_for(watcher, now, budget, stale)
        rows = merge_rows(api_rows, exclude, show_offline, inbox_rows,
                          attention_only, stale)

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
        # Renamed off `stale`, which now means the staleness MARKER a few lines
        # up. This one is the refresh clock: rewrite at least once per listing
        # interval so the ages on the panel do not freeze, even when nothing
        # meaningful changed.
        refresh_due = (now - last_write) >= poll_interval_s
        # The roster rides in the same handoff file but as its OWN payload:
        # the BLE daemon writes it separately, because five report cards
        # already fill one write's budget and a shared one would mean an
        # arriving round truncating the roster or the roster truncating the
        # round.
        roster = json.dumps(roster_payload(api_rows, exclude),
                            separators=(",", ":"), ensure_ascii=False)
        if ((payload != last_payload and (sig != last_sig or refresh_due))
                or roster != last_roster):
            cs.write_sessions_file(sessions_file, payload,
                                   build_index(rows, watcher), roster)
            last_payload, last_sig, last_write = payload, sig, now
            last_roster = roster

        sleep_fn(tick_s)
    return last_payload


DISMISS_FILE = os.path.join(os.path.dirname(cs.DEFAULT_SESSIONS_FILE),
                            "dismissed.json")

# How long a dismissal is honoured. Long enough to outlive the card -- a report
# expires in three minutes, a message in three -- and short enough that the
# file cannot become a permanent gag on an agent if a write is ever lost.
DISMISS_TTL_S = 3600


def read_dismissed(path=None, now=None):
    """Message ids the owner cleared on the device, as a set.

    Total and quiet, like every other read on this path: a missing file is the
    normal case (nothing has been dismissed), and a malformed one must not be
    able to stop the panel updating. Entries older than DISMISS_TTL_S are
    ignored here rather than deleted -- the daemon owns the file and prunes it
    on its next write; this side never touches it.
    """
    path = path or DISMISS_FILE
    now = time.time() if now is None else now
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return set()
    if not isinstance(doc, dict):
        return set()
    out = set()
    for item in doc.get("dismissed") or ():
        if not isinstance(item, dict):
            continue
        mid = item.get("mid")
        ts = item.get("ts")
        if not isinstance(mid, str) or not mid:
            continue
        if isinstance(ts, (int, float)) and now - ts > DISMISS_TTL_S:
            continue
        out.add(mid)
    return out


# How far after a report an agent's own activity has to land before it counts
# as "somebody answered this". The agent is still MID-TURN when it sends the
# report -- the SendMessage is part of that turn -- so the listing legitimately
# says `running` for a moment afterwards, and a zero margin would retract every
# card seconds after it appeared.
ANSWERED_MARGIN_S = 20


def answered_elsewhere(reports, api_rows, now=None):
    """Reports whose agent has gone back to work: {panel sender -> why}.

    The case this exists for is the owner walking over to the machine and
    ANSWERING THE SESSION THEMSELVES. Nothing tells the panel that happened --
    no button was pressed, no report was sent -- but the listing shows it,
    because a session somebody has just typed into is `running` and its
    `last_event_at` moves.

    Only the WAITING states are eligible: a card that says an agent needs a
    person is the only kind this can make untrue. A WORKING or DONE card is
    already saying the agent is busy, and retracting it on the evidence that
    the agent is busy would be circular.
    """
    now = time.time() if now is None else now
    if not reports or not api_rows:
        return {}
    # title -> (worker_status, last activity). Titles are what a report's
    # sender is: the same name the go-ahead courier addresses.
    live = {}
    for row in api_rows:
        if not isinstance(row, dict):
            continue
        title = row.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        live[title.strip()] = (row.get("worker_status"),
                               _epoch(row.get("last_event_at")
                                      or row.get("updated_at")
                                      or row.get("created_at")))
    out = {}
    for key, msg in reports.items():
        if msg.report_state not in inbox.REPORT_WAITING_STATES:
            continue
        entry = live.get(msg.sender) or live.get(key)
        if entry is None:
            continue
        status, last = entry
        if status != "running":
            continue
        if not last or last <= (msg.ts or 0) + ANSWERED_MARGIN_S:
            continue   # still finishing the turn it reported in
        out[key] = f"answered on its own machine ({int(now - last)}s ago)"
    return out


# The colony on the splash screen draws WHO IS ALIVE, which is not the question
# the cards answer. `attention_only` exists so a card means "a person is
# needed"; it also means a quietly working agent never reaches the device at
# all. So the roster is built from the SAME listing with that filter OFF, and
# shipped as its own small payload.
ROSTER_MAX = 16          # matches ROSTER_MAX in firmware/src/data.h
ROSTER_LABEL_MAX = 15    # 15 + NUL fits ROSTER_LABEL_MAX there


def roster_payload(api_rows, exclude_ids=None, max_members=ROSTER_MAX):
    """{"fl":[[name,state],...],"more":N} for the live fleet, or None.

    Cheap on purpose -- a name and a state and nothing else. It is sorted the
    way the cards are (attention first, then most recently active), so when
    there are more agents than the panel can draw, what gets dropped is what
    needed a person least, and the count of them still goes on the wire.
    """
    exclude = exclude_ids if exclude_ids is not None else set()
    keep = []
    for row in api_rows or ():
        if not isinstance(row, dict):
            continue
        if row.get("environment_kind") != BRIDGE_KIND:
            continue
        if row.get("status") in DEAD_STATUSES:
            continue
        if strip_id_prefix(row.get("id") or "") in exclude:
            continue
        # A disconnected bridge is a machine that is asleep. Drawing a creature
        # for it would say "this agent is here" about one that is not.
        if row.get("connection_status") == "disconnected":
            continue
        keep.append(row)

    keep.sort(key=lambda r: (
        cs.state_bucket(row_state(r)),
        -_epoch(r.get("last_event_at") or r.get("updated_at") or r.get("created_at")),
    ))
    members = []
    for row in keep[:max_members]:
        label = cs.elide_label(_label_of(row) or "?", ROSTER_LABEL_MAX)
        members.append([label, int(row_state(row))])
    doc = {"fl": members}
    dropped = len(keep) - len(members)
    if dropped > 0:
        doc["more"] = dropped
    return doc


def build_index(rows, watcher):
    """{sid: {...}} for the rows about to ship -- the panel's reply address.

    Only the fields a tap needs: the STATE (so the daemon can refuse a go-ahead
    on a card that never had one) and, for anything the inbox minted, the RAW
    sender and the message id. Raw, not the panel's transliterated and elided
    label -- that one is shaped for a 32-character font and cannot be used to
    address anybody.
    """
    index = {}
    for row in rows or ():
        if not row:
            continue
        sid = row[0]
        entry = {"state": row[2] if len(row) > 2 else 0}
        msg = watcher.find_by_sid(sid) if watcher is not None else None
        if msg is not None:
            entry["sender"] = msg.sender
            entry["mid"] = msg.mid
        index[sid] = entry
    return index


def _enabled(config_path=None):
    value = cs.read_config_value("fleet", config_path)
    return str(value).strip().lower() in ("1", "on", "true", "yes") if value else False


def _config_flag(key, default, config_path=None):
    raw = cs.read_config_value(key, config_path)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "on", "true", "yes")


def _config_int(key, default, config_path=None):
    raw = cs.read_config_value(key, config_path)
    if raw is None:
        return default
    try:
        val = int(str(raw).strip())
    except ValueError:
        return default
    return val if val >= 0 else default


def attention_only_from_config(config_path=None):
    """`fleet_attention_only = off` restores the full bridge roster."""
    return _config_flag("fleet_attention_only", True, config_path)


def stale_after_from_config(config_path=None):
    """`fleet_stale_after_s` -- 0 marks the panel stale on the first failure."""
    return _config_int("fleet_stale_after_s", STALE_AFTER_S, config_path)


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
    parser.add_argument("--full-roster", action="store_true",
                        help="show every reachable session, not only the ones "
                             "that need a human (overrides fleet_attention_only)")
    parser.add_argument("--stale-after", type=int, default=None,
                        metavar="S",
                        help="seconds of continuous listing failure before the "
                             f"device is told (default {STALE_AFTER_S}; 0 = at once)")
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

    attention_only = not args.full_roster and attention_only_from_config()
    stale_after_s = (args.stale_after if args.stale_after is not None
                     else stale_after_from_config())

    watcher = None
    if not args.no_inbox and inbox.enabled():
        watcher = inbox.watcher_from_config(budget)

    if args.once:
        payload = poll_once(budget, args.show_offline, watcher=watcher,
                            attention_only=attention_only,
                            health=ListingHealth(stale_after_s))
        if payload is None:
            return 1
        print(payload)
        return 0

    # From here on this is the long-running service, and both of the next two
    # lines exist because of how it gets started. Under the pythonw.exe an
    # HKCU\Run entry launches there is no console at all -- so mirror the log
    # to a file, or a poller that dies at logon leaves no trace whatsoever.
    # `--once` skips it: it is run from a terminal by definition, and two
    # processes sharing one rotating handler is a rotation race for nothing.
    cs.enable_file_log("fleet.log", "clawdmeter.fleet")

    # Only the LOOP takes the lock: `--once` writes nothing and must stay
    # runnable for diagnosis while the real poller is up (FLEET.md tells the
    # owner to do exactly that).
    lock = autostart.acquire_single_instance(SINGLETON_MUTEX_NAME)
    if lock is None:
        log("another fleet poller is already running -- exiting so the two do "
            "not fight over the handoff file")
        return 0

    log(f"listing every {POLL_INTERVAL_S}s, inbox every {TICK_S}s "
        f"-> {cs.DEFAULT_SESSIONS_FILE}")
    log("showing only what needs a human"
        if attention_only else "showing the full roster (fleet_attention_only off)")
    log(f"panel marked stale after {stale_after_s}s of listing failure")
    log(f"heartbeat -> {heartbeat_path()}")
    if watcher is None:
        log("inbox off (set `inbox = on` in the config, or drop --no-inbox)")
    run_loop(budget, args.show_offline, watcher,
             attention_only=attention_only, stale_after_s=stale_after_s)


if __name__ == "__main__":
    sys.exit(main())
