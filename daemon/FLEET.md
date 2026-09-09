# Remote fleet — your sessions on other machines

`clawdmeter_sessions.py` shows the Claude Code chats on **this** machine, fed
by hooks. This is the other half: the sessions you drive through **Remote
Control**, running on machines you are not sitting at.

It produces the same wire payload and writes the same handoff file, so the BLE
daemons ship it unchanged and **a remote session needs no firmware changes at
all** — it renders exactly like a local one, including the auto-jump when one
starts waiting on you. (The message rows added below do need one new trailing
wire field; see [Wire format additions](#wire-format-additions).)

```
Anthropic sessions API ──30 s poll──▶ clawdmeter_fleet.py
                                          │ bridge rows only
~/.claude/projects/**/*.jsonl ─2 s tail─▶ │ this machine's own rows removed
  (messages other sessions sent here)     │ only rows that NEED A HUMAN
                                          │ messages first, then sessions
                                          │ sorted attention-first, fitted
                                          │ + a staleness card when blind
                                          ▼
                                ~/.clawdmeter/sessions.json
                                          │ on change
                        BLE daemon ──────▶ device SS characteristic
```

Two sources, one payload. The remote-session rows come from the API poll
below; the message rows come from `clawdmeter_inbox.py`, which is documented
in [its own section](#messages-from-other-claude-code-sessions).

A message whose body matches the contract in [REPORT.md](REPORT.md) is an
**agent report** — the same row, a different state code, and a card that says
what that agent is doing and whether it needs you. Everything below about how
mail is found, folded, deduped and budgeted applies to reports unchanged;
REPORT.md covers only what is different.

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
| **Everything that does not need a human dropped** | See below. Pass `--full-roster` (or `fleet_attention_only = off`) to keep them. |

## Attention, not a roster

**Only rows in the waiting bucket reach the panel.** An agent stopped at a
permission prompt, asking a question, waiting for input, or in error — those
are what the Sessions tab is for. An idle remote session does not appear. Nor
does one that is merely working.

The reason is a real payload, measured on the day this was written:

```
nine rows, seven of them idle
  ...two of those idle for FIVE and SEVEN DAYS
424 bytes of wire budget spent
  0 of those nine rows needed anybody
```

That is not information, it is a roster, and on a device you read at a glance a
roster crowds out the thing that matters. The firmware buys attention with a
fixed currency — the accent colour, the pulse, the top of the sort, one
auto-jump — and a list where nothing ever earns any of it teaches you to stop
looking.

Nothing is lost by dropping them. `requires_action` in the listing remains the
whole point of the poll: it is the only way the device learns an agent needs a
person **without being asked**, and that passive signal is the single most
valuable thing this device does. The filter does not weaken it; it removes
everything else that was standing next to it.

**What still counts as needing a human**, in full:

| Source | Shown | Why |
| --- | --- | --- |
| listing, `requires_action` | yes | A person unblocks it. This is the signal. |
| listing, `running` / `idle` / anything else | **no** | Nothing to do. |
| a message from another Claude Code session | yes | Somebody wrote to you, unbidden. |
| an agent report (any of the four states) | yes | You pressed the button that asked. A round is an answer to a question you just asked, so `WORKING` and `DONE` stay: the round's shape is the information. |
| the staleness card | yes | Not a session at all — see below. |

Messages and reports are not filtered because they do not come from the
listing: they are read from local disk, they carry their own expiry, and they
are attention-shaped by construction.

**What this frees.** The seven idle names were costing ~380 bytes of a 500-byte
payload. They now cost nine (`{"ss":[]}`), and the whole budget is available
to the rows that earn it — which matters most for a ten-agent report round,
where every byte reclaimed is another agent's summary that fits.

**Turning it off**: `fleet_attention_only = off` in the config, or
`--full-roster` for one run. Everything above about kind, archived rows, this
machine and disconnected bridges still applies.

### "Nothing needs you" is a screen, not an accident

With the filter on, an empty tab is the normal state and it is good news, so
the device says so plainly: a dim `Nothing needs you`, with
`only what needs you shows here` under it — the sub-line exists because a tab
that used to list nine machines and now lists none has to say that this is the
design and not a broken feed.

It deliberately does **not** borrow the usage screen's "Zzz" creature. That
treatment dresses up a *fault* (the host stopped talking) and putting it here
would make the calm, healthy state the loudest thing on the tab. When the link
is actually down, this same screen says `Host disconnected` instead, and the
sub-line goes away.

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

## When the host goes blind

The poller writes the handoff file **only when something changes**. So "no
write" is ambiguous between *the fleet is quiet* and *everything is broken* —
and that ambiguity is not theoretical. It cost a nine-hour outage: an OAuth
token expired, every poll took HTTP 401, the poller correctly kept the last
good payload rather than blanking the panel, and then its process died. The
device showed the previous night's list, all day, with nothing anywhere saying
so.

The device cannot break that tie on its own. A genuinely quiet fleet
legitimately sends nothing for hours, so a firmware-side timer would either cry
wolf on a calm desk or stay silent through a real outage. **The host knows when
it last succeeded, so the host says it.**

### The contract

| State | What the device gets |
| --- | --- |
| listing healthy | the rows, as always |
| listing failing, **under** `fleet_stale_after_s` | the last good rows, unchanged — a blip must not blank a panel |
| listing failing, **over** `fleet_stale_after_s` | **every listing row dropped**, replaced by one host-minted card |
| listing recovers | the card disappears on the next poll; the rows come back |

The card is wire state **17**, `SESSION_HOST_STALE` — the same pattern as the
report overflow marker (state 16): a row the host minted that is not an agent.
It is drawn dim, in the idle bucket, and is never in the notify set, because it
is a fault indication, not an alert. Nothing is waiting on you; the panel has
simply stopped knowing whether anything is.

```
FLEET DATA STALE                                               9h
auth expired - log in to claude
```

`elapsed` is the age of the last good listing, so the card dates itself. The
body is the reason in the host's own words:

| Failure | Body |
| --- | --- |
| HTTP 401 | `auth expired - log in to claude` |
| HTTP 403 | `listing refused - no access` |
| HTTP 404 / 410 | `listing moved or changed shape` |
| a shape that is not the documented one | `listing moved or changed shape` |
| network / DNS / timeout | `host cannot reach the listing` |
| no credential file at all | `no login found - run claude` |
| anything else | `listing error - HTTP <code>` |

### Why the rows are dropped rather than dimmed

Because of the filter above. After it, the only listing rows that survive at
all are `requires_action` ones — *this agent needs you now* — and a nine-hour
old one of those is worse than nothing: it is terra-cotta, it pulses, it sorts
to the top of the tab and it can pull the panel there, all to send you to a
machine where nobody is waiting. Keeping them and captioning them would be
defensible for a roster. It is not defensible for an alert.

The panel's own precedent agrees. The usage screen replaces stale numbers with
the idle screen rather than dimming them, for the reason written in the
firmware: *"we never render hours-old numbers as if they were live."*

**Messages and reports are not dropped.** They are read from local disk and are
still true. So the card means "the listing half of this tab is dark", not
"nothing here can be trusted" — and it renders as a footnote *under* the live
rows, exactly like the report round's `+N MORE`.

Its bytes and its row slot are reserved out of the inbox's allowance before a
report round is fitted, because both caps eat the tail: `fit_payload` drops
from it, and the firmware parses six rows and discards the rest. The cost is
that a full round shows one fewer agent while the listing is down, which is the
right way round — the round has its own footnote to say what it dropped, and
the staleness card is the only thing on the panel that says the other half of
the tab is blind.

### Why 900 seconds, and the 401 in particular

`fleet_stale_after_s` defaults to **900** — thirty consecutive failed polls.

The threshold exists almost entirely for the 401. The OAuth token lives about
five hours, Claude Code refreshes the credential file on its own, and the
poller re-reads that file on every poll — so a 401 normally heals by itself
within a poll or two. Shouting on the first one would mean a scary card several
times a day about nothing, and a card that cries wolf is a card you stop
reading.

Fifteen minutes gives a refresh thirty chances to land, and still leaves the
panel honest inside a coffee break rather than the nine hours it was wrong for.
The clock is anchored to the **last success**, not to the first failure of a
streak, because the number on the card is *how old this data is*.

A poller that has **never** succeeded runs the same clock from its own start: a
cold start with a bad credential is exactly as blind as an outage, and you
should hear about both.

Set `fleet_stale_after_s = 0` to mark the panel stale on the first failure —
useful for seeing the card:

```bash
python3 daemon/clawdmeter_fleet.py --once --force --stale-after 0
```

## Keeping it running

The other half of that nine-hour outage is that the poller **died and nothing
noticed**. Two mechanisms now cover it, and both are opt-in.

**1. An autostart entry.** A third `HKCU\...\Run` value, `ClawdmeterFleet`,
beside the tray's `Clawdmeter` and the hook sidecar's `ClawdmeterSessions`.
Three independent opt-ins, three value names, so turning one off never turns
another off.

```powershell
# turn it on (once)
python -c "import daemon.autostart_windows as a; a.enable_fleet()"
# ...or tick "Start fleet poller at login" in the tray menu, which also starts
# one immediately rather than waiting for the next logon.

# off again
python -c "import daemon.autostart_windows as a; a.disable_fleet()"
```

**2. A supervisor that notices it is gone.** The poller stamps
`%LOCALAPPDATA%\Clawdmeter\fleet.heartbeat` on every listing poll; the tray
checks it every 30 s and, after four missed stamps (120 s), logs, raises one
toast, and starts a poller again with capped backoff — the same treatment the
tray already gives its own daemon loop when that crashes.

It watches a **heartbeat**, not a child process, on purpose: the poller may
have been started by its Run value, by the installer, or by hand from a
terminal, and a supervisor that only knew about children it spawned itself
would have been watching nothing at all on the day this was needed. The
question it asks is "is *a* poller alive", not "is *my* poller alive".

It is armed only when the autostart entry is enabled — that entry is the
owner's statement that there *should* be a poller running, which is exactly the
claim a supervisor needs before it acts. With autostart off, the supervisor
does nothing at all.

A relaunch that races a poller which was merely slow is harmless: the poller
holds a named single-instance mutex (`Local\Clawdmeter-fleet-singleton`), so
the second copy exits instead of becoming a second producer of the handoff
file. `--once` never takes the lock, so you can still diagnose against a
running poller.

**Logs.** Under `pythonw.exe` started from a Run value there is no console at
all and `sys.stderr` is `None`, so the poller's `log()` is guarded and mirrored
into `%LOCALAPPDATA%\Clawdmeter\fleet.log`. This is not a nicety: an
unguarded `print(file=sys.stderr)` inside the HTTP-error handler would raise
`AttributeError` out of the except block and kill the poller **on its first
401** — which is a strong candidate for what actually happened.

## Messages from other Claude Code sessions

Claude Code sessions can message each other: one calls `SendMessage` naming a
peer from `ListAgents`. When a message arrives on this machine, the device
shows it — sender on the card, message text under it.

This is a **documented first-party feature**, which makes it a sounder
foundation than the undocumented listing endpoint the rest of this file has to
apologise for.

What the device does with it: a new message pulls the panel to the Sessions tab
once (the same auto-jump a session blocked on a permission prompt fires, and
the same Settings switch turns both off), holds it for the 10 s dwell, and then
returns to whatever screen you were on. The card itself stays for
`inbox_expire_s`, one swipe away — being *pulled* to the tab is the
notification, and a message is something to read, not something to keep
flashing at you, so it never gets the waiting card's accent or pulse.

### How the message is found

A received message is appended to the **receiving** session's transcript
(`~/.claude/projects/<munged-cwd>/<session-id>.jsonl`) **twice**, and both
records are read.

**1. When it arrives** — the instant it lands in that session's input queue,
whether or not the session is in any state to look at it:

```json
{"type":"queue-operation","operation":"enqueue",
 "timestamp":"2026-09-08T14:37:46.006Z","sessionId":"a5c6cdfd-…",
 "content":"<cross-session-message from=\"uds:…\" from-name=\"CLAWDMETER\" from-mode=\"prompting\">\n…the body…\n</cross-session-message>"}
```

**2. 207 ms later, when the session processes it** — as a `user` record with
`userType: "external"`, the body now nested under `message.content` and led by
a preamble sentence:

```
Another Claude session sent a message:
<cross-session-message from="uds:..." from-name="CLAWDMETER" from-mode="prompting">
...the body...
</cross-session-message>
```

Reading **(1)** is what makes the panel independent of the receiving session's
state: a message shows up while that session is busy, blocked, or parked at a
prompt with an undrained queue — and ~200 ms sooner even when it is not.
Reading **(2)** as well rather than instead is belt and braces: it is the shape
verified to carry the preamble framing, and a Claude Code version that emits
only one of the two must not make the feature go silent.

Note that `queue-operation` is *not* a message-only record type — your own
typed prompts are enqueued through it too. What separates mail from a prompt is
the `<cross-session-message>` tag, exactly as it does in a `user` record.

`clawdmeter_inbox.py` tails those files and reads only the bytes appended since
its last pass. Sender comes from `from-name`, the body from between the tags.
Parsing is deliberately loose: attribute order, the preamble wording, the
`operation` spelling and the string-vs-block-list shape of `content` are all
things a Claude Code release can change, so none of them is treated as a
contract.

#### One message, two records, one card

Two records for one message makes **deduplication mandatory**, and the identity
it dedupes on is `(receiving session, sender, body)` — who sent *what* to
*which* session. Two things are deliberately left out of it:

- **the timestamp**, because the two records disagree about it. 207 ms on the
  verified probe, and unbounded in general: a session parked at a prompt drains
  its queue whenever its human comes back. A coarse time bucket would only move
  the problem to the pair that straddles a bucket edge.
- **the `from` pipe id**. `cc-msg-572ec92ff3b152decf8ea5ffef7664ad` *looks*
  per-message and is not — on this machine's transcripts one such id spans 52
  records and several distinct messages, because it names the **sending**
  session's pipe. Keying on an attribute a future version might write in one
  shape and not the other would resurrect the double card this exists to
  prevent.

The card is dated from the **arrival** record, so the age on the panel is when
the message landed, not when its reader woke up to it. Message ids are
remembered past the expiry horizon (the most recent 512, whatever their age),
because otherwise a message queued now and processed hours later would be drawn
a second time at drain.

The one honest cost: a byte-identical body from the same sender to the same
session, twice inside that window, shows as one card. On a device you read at a
glance the second card would have been indistinguishable from the first anyway.

**Watching transcripts is the zero-cost approach**: there is no host-side spool
to read instead, and watching the sessions you already have open adds no session
and no model turns. A dedicated always-on "inbox" session would work too — and
now that the arrival record is read, such a session would not even have to take
a model turn for its mail to reach the panel. It could sit there and be a mail
drop.

Only **top-level** transcripts are watched — `<project>/<id>.jsonl`, no
recursion. `subagents/` and `workflows/` are excluded: those are an
orchestrator talking to its own children, not another person's session reaching
this machine, and there are far more of them (193 `.jsonl` files on the
development box, 16 of them real sessions).

### Non-ASCII, and why the panel does not just go blank

The firmware's fonts are the constraint, and the coverage is **not the same
for every field**:

| field | font | covers |
| --- | --- | --- |
| message **body** | `font_styrene_28` + `font_nanum_kr_28` fallback | ASCII 32..126 **and** the 2,350 KS X 1001 Hangul syllables |
| sender name | `font_styrene_20` / `24` | ASCII 32..126 |
| session label | `font_styrene_28` / `48` | ASCII 32..126 |

So Korean **bodies** go out as real Hangul, and everything else is folded
before it goes on the wire, most-faithful rule first:

| Input | On the panel | Why |
| --- | --- | --- |
| ASCII | unchanged | — |
| `—` `“ ”` `…` NBSP | `-` `" "` `...` space | Named ASCII twins; NFKD does not fold these |
| `café` | `cafe` | NFKD, combining marks dropped — a real transliteration |
| `안녕하세요` **in a body** | `안녕하세요` | The device has those glyphs |
| `안녕하세요` **in a name** | `annyeonghaseyo` | It does not have them *there* |
| `똠` (Hangul outside KS X 1001) | `ttom` | Not in the font, even in a body |
| `中文` `テスト` `😀` | `?` | Nothing faithful to fall back on |
| a body with none of it legible | `[non-ASCII msg]` | Says a message arrived and that the panel cannot show it |

The romanisation is **approximate**: RR's inter-syllable assimilation rules are
not applied, so `학년` comes out `haknyeon` where the standard spells it
`hangnyeon`. Readable, not authoritative — and it is now the fallback rather
than the normal path for a body. Set `inbox_hangul = off` to romanise bodies
too, which is what firmware older than the Korean font needs (it would draw
the Hangul as empty boxes). Set `inbox_translit = off` to drop non-ASCII
instead of romanising it at all.

Untranslatable characters collapse per **run**, not per character, so one `?`
stands in for a dropped phrase rather than `?????` drowning the words that did
survive.

**The sender name goes through the fold**, so the fallback actually holds: an
unrenderable body still tells you who it came from. (It did not always —
folding the body and not the label shipped `annyeonghaseyo …` under five tofu
boxes for a peer whose machine name is Korean.) A name that folds to nothing at
all becomes `peer`, and a long one is middle-elided to the device's 32-char
label buffer so the tail that distinguishes two machines survives.

**So do session labels.** A Korean project directory name used to reach the
panel unfolded and draw as empty boxes in the 48 px name font. The fold lives
in `clawdmeter_sessions.panel_label()` and is applied inside `fit_payload()`,
which is the one funnel both label producers (the hook sidecar and the remote
fleet) go through.

Korean costs **3 bytes per syllable** on the wire, so the message length cap
counts bytes, not characters — `budget // 4` bytes, which is 15 syllables at
the default 180-byte budget and 21 at `sessions_budget_bytes = 260`. See
[`docs/fonts.md`](../docs/fonts.md#byte-budget-honestly) for the full chain of
caps.

### What it costs on the wire

A message row is worth about **two session cards** at the default 180-byte
budget. Measured against `cs.fit_payload` with five remote sessions:

```
budget 180, 0 messages: 159 B — 3 session cards
budget 180, 1 message : 180 B — the message + 2 session cards
budget 260, 2 messages: 245 B — 2 messages   + 2 session cards
```

Messages are placed **first** and `fit_payload` drops from the tail, so what
gets evicted is the least urgent session card and never the message.

**Two message rows cost ~146 B whatever the text cap** (the per-message length
already halves when two are live), which at the 180-byte default leaves room
for *no session card at all* — a Sessions tab with no sessions on it, for the
whole expiry window. So the row count is derived from the budget as well as
from `inbox_max_rows`: below **200 bytes only the newest message is shown**,
and the poller logs the cap when it applies it. An older message has already
had its own card and its own notification, so the thing given up is smaller
than the thing protected.

Messages also **expire after 3 minutes** (`inbox_expire_s`): without a window
the panel would fill with mail and never show a session again. Three minutes
covers a glance cycle on a desk device; a message you have not noticed in three
minutes is one you will read on the computer.

If you would rather keep more session cards — and get two message rows —
raise `sessions_budget_bytes` to **260**. Mind the MTU note in SESSIONS.md: the
budget has to stay under the ATT MTU the link negotiates, and 260 needs an MTU
of at least 263. The firmware asks for 517 and its buffer is 1 KB, so a normal
stack is fine; the conservative 180 default exists for host stacks that
negotiate the 185-byte minimum.

### Latency

Messages do **not** wait for the 30 s listing poll. The loop ticks every 2 s,
re-reading only the transcript bytes that are new, and writes the handoff file
as soon as something meaningful changes — so a message is on the panel within
the BLE daemon's own 5 s tick.

They do not wait for the **receiving session** either, which used to be the
larger of the two delays: the arrival record above is written when the message
lands in the queue, so a session that is mid-tool-call, blocked on a permission
prompt, or simply sitting at an idle prompt no longer holds its own mail off
the panel.

"Meaningful" excludes the clock: every row carries an `elapsed` field that
advances on its own, and comparing whole payloads on a 2 s loop would look like
a change every 2 s and turn into a BLE write every 5 s forever. The loop
compares the payload with `elapsed` blanked, and refreshes anyway once per
listing interval so the ages on screen do not freeze.

**Retraction matters as much as publication.** The loop stays silent until it
has something to say — a machine with no token and no mail never blanks a panel
some other producer filled — but only *until*. Once it has written the file it
owns it, and an expired message must be writable back to `{"ss":[]}` even when
the listing is unavailable (no token, or an endpoint that has moved). Otherwise
the row ships forever at a frozen age: the sessions view could never show a
session again, and on the device the message would sit in the notify set with
no falling edge to hand the screen back.

### Privacy — this one is different

Every other row this project sends the device carries names, states and counts.
SESSIONS.md says so explicitly: *"Nothing from the payload text ever reaches the
device."*

**Message rows break that rule on purpose** — the message body is the feature.
It is read from local disk, folded to ASCII, truncated to ~40 characters, and
sent over your own BLE link to your own device on your own desk. Nothing leaves
the machine over the network. But if a desk device that can be read over your
shoulder is not somewhere you want message text, turn it off:

```ini
inbox = off
```

### First run

The watcher establishes a baseline instead of dumping backlog: on a cold start
it tail-reads each transcript and discards anything older than
`inbox_freshness_s` (120 s), so starting the poller never floods the panel with
this morning's mail. A cold pass over 16 transcripts takes ~40 ms.

That guarantee does **not** rest on the transcript timestamp format. A record
whose date is missing, `null`, or written some other way has an unknown age —
and unknown age inside a 256 KB tail of history is not news, so on a first
sight of a file (and on a re-read after rotation) those records are dropped
rather than dated "now". On a later pass they *are* dated "now", which is true:
they arrived in bytes that were not there before. Everything else in this
module treats Claude Code's transcript prose as non-contractual, and the one
guarantee that keeps old private message text off a desk panel should not be
the exception.

### Checking it without the device

`clawdmeter_inbox.py` runs standalone and read-only — it never writes the
handoff file:

```bash
python3 daemon/clawdmeter_inbox.py --once        # what is live right now
python3 daemon/clawdmeter_inbox.py --watch       # keep printing as mail arrives
python3 daemon/clawdmeter_inbox.py --once --freshness 86400 --expire 86400  # look back a day
```

Widen **both** windows to look back: `--freshness` decides what is read, and
`--expire` decides what is still live once it has been. `--freshness 86400` on
its own prints `{"ss":[]}`, because a message from this morning is accepted and
then immediately expired by the 180 s default.

### Config

| Key | Default | Meaning |
| --- | --- | --- |
| `inbox` | `on` | Set `off` to stop showing messages. Only ever active when `fleet = on`. |
| `inbox_expire_s` | `180` | How long a message stays on the panel. |
| `inbox_freshness_s` | `120` | How far back a fresh start looks. Also the cold-start baseline. |
| `inbox_max_rows` | `2` | Concurrent message rows. The per-message text shortens when two are live so both fit — and the count is capped to **1** while `sessions_budget_bytes` is under 200, because two rows there leave no room for a session card. |
| `inbox_translit` | `on` | `off` drops non-ASCII instead of transliterating it. |
| `inbox_hangul` | `on` | `off` romanises Korean message bodies instead of sending them as Hangul. Set it when the device is running firmware older than `font_nanum_kr_28`, which draws Hangul as empty boxes. Sender names and session labels are always romanised — their fonts have no Hangul fallback. |
| `reports` | `on` | `off` reads agent reports as ordinary messages. See [REPORT.md](REPORT.md#config). |
| `report_expire_s` | `300` | How long an agent report stays on the panel. |
| `fleet_attention_only` | `on` | `off` restores the full bridge roster. See [Attention, not a roster](#attention-not-a-roster). |
| `fleet_stale_after_s` | `900` | Seconds of continuous listing failure before the device is told. `0` = at once. See [When the host goes blind](#when-the-host-goes-blind). |

The watcher reads `config_dirs` (shared with the daemons) the same way the hook
sidecar does, so extra Claude config dirs are watched too.

## Wire format additions

A message row is the same positional row the firmware already parses, with one
new **trailing** field:

| # | Field | Value on a message row |
| - | --- | --- |
| 1 | `label` | the sender's `from-name` |
| 2 | `state` | **11** — new state code, `SESSION_MESSAGE`. Appended after `SESSION_ENDED = 10` in `firmware/src/data.h`; the codes are append-only because they cross the BLE boundary. Agent reports reuse this same row with codes **12–16** — see [REPORT.md](REPORT.md#wire-format) |
| 3, 11 | `ctx`, `tok` | `-1` — not applicable to a message |
| 4 | `elapsed_s` | age of the message |
| 12 | `remote` | `-1` (unknown) — Remote Control is meaningless for a message |
| **13** | **message text** | **new**: the folded, elided body |

Indices 0..12 are byte-identical to what they have always been, so firmware
that stops reading at index 12 sees a normal row and ignores the tail. Only
message rows carry index 13 — session rows stay 13 fields long, because a field
nobody reads is pure byte budget.

The **staleness card** is the same 14-field row again, with state **17**
(`SESSION_HOST_STALE`), sid `z0`, the fault in `label` and the reason in index
13. Its sid is disjoint from every other producer's: session and message sids
are two hex characters, report sids are `[g-y][0-9a-z]`, and `zz` belongs to
the report overflow marker.

Firmware older than state 17 draws it as a session card labelled
`FLEET DATA STALE` whose state line reads `busy` — not the intended card, but
the label still carries the meaning and nothing misbehaves. That is the price
of an append-only wire, and it is the right way round: the words that matter
are in the field every vintage reads.

## Limits

- **Polling, not push.** 30 s, matching the interval the official client uses
  for its own roster. A session that starts waiting is on the panel within
  half a minute, not instantly.
- **No context bar.** The API does not report context usage, so remote cards
  show the name, the state and the elapsed time only.
- **Token expiry** is the platform daemon's problem; this module re-reads the
  credential file every poll rather than caching a token that goes stale in
  hours. If nothing refreshes it, the staleness card says so after fifteen
  minutes.
- **A quiet tab is the normal tab.** With the attention filter on, most of the
  time there is nothing to show, and that is the feature. If you want to watch
  the fleet rather than be interrupted by it, `fleet_attention_only = off`.
