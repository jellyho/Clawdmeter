# Agent reports — asking the fleet what it is doing

The owner presses a button. A dispatcher asks every reachable Claude Code
agent to report in. The agents reply by cross-session message, the inbox
watcher recognises those replies, and the device draws them as status cards
instead of anonymous walls of text.

This file is the contract in the middle: **what an agent must say**, **the
exact words the dispatcher asks it in**, and **what the device does with the
answer**.

> **The button and the dispatcher do not exist yet.** This is the format and
> the rendering, built first so that when the dispatcher lands there is
> something worth showing. Everything below works today: write a reply that
> matches the contract from any Claude Code session, and the card appears.

---

## The insight: a report is not a new row kind

The wire row already carries a **state code**, and the firmware already
colours, sorts and auto-jumps on it — the waiting bucket turns terra-cotta,
sorts to the top of the Sessions tab, and pulls the panel to it once.

Before this change every message row went out as state `11`
(`SESSION_MESSAGE`) whatever it said, which threw all of that away: a peer
telling you it is blocked looked exactly like a peer saying hello.

So a report is a **message-shaped card whose state means something**. It keeps
the message *layout* — sender line, then the words — and takes its dot colour,
its sort bucket and its auto-jump from *which report state it is*. No new row
kind, no new wire field, no new parser on the device.

## The reply contract

One line. It is the agent's whole reply.

```
CLAWDMETER-REPORT/1 <STATE>: <summary>
```

| Part | Rule |
| --- | --- |
| `CLAWDMETER-REPORT/1` | Literal. Must open the **first non-empty line** of the message body. |
| `<STATE>` | Exactly one of `WORKING`, `NEEDS-YOU`, `BLOCKED`, `DONE`. |
| `:` | Optional — a space alone separates just as well. |
| `<summary>` | One line, non-empty, 40 characters or fewer. Any language. |

Real examples:

```
CLAWDMETER-REPORT/1 WORKING: building waveshare_amoled_216_c6
CLAWDMETER-REPORT/1 NEEDS-YOU: rebase done - force-push to main?
CLAWDMETER-REPORT/1 BLOCKED: permission prompt: allow Bash?
CLAWDMETER-REPORT/1 DONE: chezmoi apply finished clean
CLAWDMETER-REPORT/1 NEEDS-YOU: 빌드 끝났어요 머지할까요?
```

**Strict about the match, forgiving about whitespace.** Forgiven: leading and
trailing space, the case of the marker and of the state word, `NEEDS_YOU` and
`NEEDS YOU` for `NEEDS-YOU`, a missing colon, any spacing around the
separator. Not forgiven: a state word outside the four, an empty summary, or
prose before the marker.

**Anything that does not match stays an ordinary message row, unchanged.** A
malformed report is never dropped, never crashes anything, and never guessed
at — it draws as the message it always was, body and all, and the panel says
nothing it cannot support. Guessing would be the worse failure: an unknown
state word quietly becoming `WORKING` is the panel being confidently wrong
about a machine the owner cannot see.

### Why the marker must open the body

The dispatcher's *request* necessarily contains the marker — it is telling the
agent what to emit — and that request lands in the receiving transcript
exactly like any other cross-session message. If the parser accepted the
marker anywhere, **every dispatch would draw a bogus report card**.

Requiring it at the head of the body fixes that, and it is why the request
text below keeps the template inline inside a sentence and never at the start
of a line. Keep it that way if you reword it.

### The four states, and why there are four rather than three

A working / needs-you / done trio would be the obvious shape. It is one state
short, because of what comes next: **a later feature will put a button on a
report card that sends "go ahead" back to that agent.** That button only works
on some of them, and the device has to be able to tell which.

| State | Meaning | Would a "go ahead" message work? |
| --- | --- | --- |
| `WORKING` | Mid-task, needs nothing. | Pointless — it is already going. |
| `NEEDS-YOU` | Stopped, waiting for the human to say what to do next. | **Yes.** The message lands in its input queue and it carries on. This is the one valid target. |
| `BLOCKED` | Stopped at a permission prompt or another dialog only a human at that machine can clear. | **No.** The message queues *behind* the dialog and is read only after somebody clicks. Sending one looks like it worked and does nothing. |
| `DONE` | Finished, nothing running. | Reaches it, but "go ahead" with what? |

So `state == SESSION_REPORT_NEEDS_YOU` is the entire predicate that button
needs, and it exists in the wire format from day one rather than being
retrofitted onto a state that had already conflated the two.

**`BLOCKED` is rarer than it looks, and that is honest.** An agent hard-stuck
behind a permission dialog cannot answer the dispatcher either — its reply is
*silence*, and the dispatcher can see who did not answer. `BLOCKED` is what an
agent says when it knows a human is needed at that machine but is still able
to say so: it has just come back from a denied tool call, something adjacent
is waiting on a dialog, or a subagent is. The state earns its code regardless,
because the *device* needs the distinction representable however rarely it is
sent.

## The dispatcher's request — exact wording

The agents are LLMs, so **the request text is the contract**. Send this, or
something that keeps every constraint in it:

> Clawdmeter status check. Reply to the session that sent this, and make your
> whole reply this one line: `CLAWDMETER-REPORT/1 <STATE>: <summary>` —
> nothing before it, nothing after it, no code fence, no backticks, no
> explanation.
>
> `<STATE>` is exactly one of these four words:
> `WORKING` (you are mid-task and need nothing from the human),
> `NEEDS-YOU` (you have stopped and are waiting for the human to tell you what
> to do next — a reply message would unblock you),
> `BLOCKED` (you are stopped at a permission prompt or another dialog only a
> human at your machine can clear — a reply message would NOT unblock you, it
> would just queue behind the dialog),
> `DONE` (you finished what you were asked and nothing is running).
>
> `<summary>` is one line of at most 40 characters. For `WORKING` or `DONE`,
> say what the work is. For `NEEDS-YOU` or `BLOCKED`, say what you are waiting
> for. No line breaks, no quotes, no markdown. Write it in the language you
> normally use with this user — Korean is fine.
>
> If it will not fit in 40 characters, shorten the words. Do not drop the
> format.

Three things in there are load-bearing and should survive any rewording:

1. **"make your whole reply this one line"**, twice over ("nothing before
   it"). The marker has to open the body — see above.
2. **"at most 40 characters"**, because that is what the byte budget buys at
   the recommended budget (see below). A longer summary is not rejected, it is
   head-elided with `...`, so the agent loses its own last words.
3. **"the language you normally use with this user"**. The panel's message
   font carries the 2,350 KS X 1001 Hangul syllables, so Korean summaries go
   out as real Hangul; the *state word* on the card is drawn by the firmware
   from the code, so it is English whatever language the summary is in, and
   the *agent name* is romanised (its font has no fallback). See
   [FLEET.md](FLEET.md#non-ascii-and-why-the-panel-does-not-just-go-blank).

## What the device does with it

A report card is the message card's anatomy — a sender line with the age on
the right, the words underneath at full brightness — plus **one chip**: the
state word, on the sender line, in the state's own colour. Having a chip is
what makes a report readable as a report at a glance; the chip's colour and
text separate the four from each other.

| State | Wire | Chip | Dot | Bucket |
| --- | --- | --- | --- | --- |
| `NEEDS-YOU` | 13 | `needs you`, terra-cotta, pulsing | terra-cotta, pulsing | **waiting** — sorts first, trips the auto-jump |
| `BLOCKED` | 14 | `blocked`, terra-cotta, pulsing | terra-cotta, pulsing | **waiting** |
| `DONE` | 15 | `done`, green | green | working (full brightness) |
| `WORKING` | 12 | `working`, dim | white | working |
| *(overflow)* | 16 | none | dim | idle (recedes) |
| *(a plain message)* | 11 | none | purple | its own |

Nothing here is a new visual vocabulary: terra-cotta + pulse has always meant
"a human is needed", white has always meant "running", dim has always meant
"nothing here", purple has always meant "another Claude is talking to you",
and green is the usage bar's own "fine". A report just gets to use them.

**The pulse moves.** On a session card the pulsing text is a three-word state
line you already know the shape of. On a report card that same label is a
whole sentence you have *not* read yet, and fading it in and out is a card you
cannot finish reading — so the words hold at full brightness and the **chip**
pulses instead, in the same phase and the same accent.

### Waiting bucket: yes. Waiting *level*: no.

A `NEEDS-YOU` report goes in the firmware's waiting bucket, which is the whole
point of giving reports states at all: accent, pulse, sorted to the top, and
one auto-jump that pulls the panel to the Sessions tab.

It does **not** join the waiting *level* (`s_any_notify`), and that is a
sharper call than it is for messages. The level is what the auto-return waits
on — it means "a session is *still* blocked on you" and it clears when the
host observes the session move on. **A report has no falling edge.** The host
cannot see the remote agent at all; it only knows what that agent said N
seconds ago, and the row goes away by *expiry*, never by resolution. Holding
the level would pin the panel on the Sessions tab for the whole report window,
and an agent that never reports again would pin it for good.

So a report gets the accent, the pulse, the top of the list and one
notification — and then hands the screen back after the 10 s dwell, exactly
like a message. The card stays for as long as the host sends it, one swipe
away.

**The rising edge still works across a state change**, which is the case that
matters: the sid is minted from the *agent*, so a card keeps its identity
while its state changes under it, and only the two waiting states join the
notify set. An agent going `WORKING` to `NEEDS-YOU` therefore *enters* the set
and fires exactly one jump; going back to `WORKING` leaves it, re-arming the
edge for next time. Re-reporting the same state produces no second jump.

## The byte budget — the hard constraint

Ten agents can be reachable at once. (Verified on the development machine with
`ListAgents`: 24 peers, 10 reachable, 13 offline.) Ten report rows do not fit
in anything, so the arithmetic decides what does.

**Measured**, not estimated — a report row encodes to:

```
 39 B  structure   ["xx","",13,-1,42,0,0,0,0,0,0,-1,-1,""] plus its comma
  +    agent name  12 B is typical (ORCHESTRATOR, WORKER-ALPHA)
  +    summary     41 B at the recommended budget (40 chars + slack)
 = 83-88 B per row, measured across a real ten-agent round
   54 B for the overflow marker (short label, short body)
    9 B for the {"ss":[]} envelope
```

**Two caps bind, and the row cap binds first.** The firmware parses
`SESSION_MAX_ROWS = 6` rows and discards the rest, so the most a round can
ever show is **5 agents plus the overflow marker**. That costs:

```
5 x 87 + 54 + 9 = 498 B
```

### Recommendation: `sessions_budget_bytes = 500`

```
 498 B   the largest round the device can display at all
 500 B   the budget                          <-- recommended
 503 B   500 + the 3-byte ATT write header
 517 B   the MTU the firmware requests (ble.cpp: NimBLEDevice::setMTU(517))
1023 B   the firmware's usable SS buffer (BLE_SS_BUF_SIZE - 1)
```

500 is the knee of the curve — measured by sweeping the budget against a real
ten-agent round, the row count steps from 5 to 6 at exactly 500 — and it still
lands in **one ATT write** at the requested MTU, with 14 bytes of slack and
without depending on the host stack's long-write path. It uses 49% of the
firmware's session buffer, so there is room to spare in the direction that
would actually hurt.

If your host stack negotiates a smaller MTU the write becomes a prepared
(long) write rather than failing: the SS characteristic's `max_len` is 1024,
so NimBLE accepts it. That is a performance footnote, not a correctness one.

**The `180` default does not move.** It is sized for the 185-byte minimum an
unlucky host stack may hand you, and the ordinary message-and-session path
lives inside it perfectly well. A report round at 180 degrades rather than
breaking — measured, same ten agents:

| Budget | What the panel shows |
| --- | --- |
| 180 (default) | 1 agent + `+9 MORE / 1 need you, 1 blocked, 1 done, 6 working` |
| 260 | 2 agents + `+8 MORE` |
| 360 | 3 agents + `+7 MORE` |
| **500** | **5 agents + `+5 MORE`** — the most the device can draw |

The summary length scales with the budget too (`report_text_max`: a twelfth of
it, floored at 24 B and capped at 64 B), so a small budget shortens the words
before it drops a card. At 500 that is 41 bytes — which is why the dispatcher
asks for **40 characters**: one byte of slack, so an on-spec ASCII summary is
never elided.

### What is dropped, and why you can see it

Rows are ranked before they are fitted, and dropping happens from the **tail**:

```
0  NEEDS-YOU   a human's answer unblocks it - the most actionable row there
               is, and therefore the last thing that may ever be dropped
1  BLOCKED     a human is needed too, but at the keyboard
2  message     somebody said something unbidden
3  DONE        finished. Rarer than WORKING after a dispatch and therefore
               more informative: a finished agent is news
4  WORKING     needs nothing. The expected answer, and the first to go
```

and whatever goes, **the last row says so**:

```
+5 MORE                                                        3s
1 blocked, 1 done, 6 working
```

A panel that silently showed the first five of ten would be lying about the
fleet. This row is deliberately the quietest thing on the screen — dim, idle
bucket, never in the notify set — because the urgent rows are the ones above
it and this is the footnote that says the list is not the whole story.

The fitting happens in `clawdmeter_inbox.fit_round`, **not** in
`cs.fit_payload` downstream, for a reason worth stating: that one also drops
from the tail, and once a marker is appended the tail *is* the marker. The one
row whose job is to report the dropping would have been the first thing
dropped.

A full round can legitimately evict every remote-session card. That is what
pressing the report button asked for.

## Wire format

Report rows are the same 14-field positional row a message row is
([FLEET.md](FLEET.md#wire-format-additions)) — same "not applicable" values,
the summary riding in the same index 13 a body does. One field differs, and it
is the whole trick:

| # | Field | Value on a report row |
| - | --- | --- |
| 1 | `label` | the agent's `from-name`, romanised and middle-elided |
| 2 | `state` | **12-16** — new codes, appended after `SESSION_MESSAGE = 11` |
| 3, 11 | `ctx`, `tok` | `-1` |
| 4 | `elapsed_s` | age of the report |
| 12 | `remote` | `-1` |
| **13** | **summary** | the folded, head-elided one-liner |

State codes are **append-only** (`firmware/src/data.h` says so) because they
cross the BLE boundary. 12-16 go after 11; nothing is renumbered.

Report **sids** live in their own two-character alphabet (`[g-y][0-9a-z]`),
disjoint from message and session sids, which are both two hex characters and
already share one 256-value key space. Adding a third hex producer would have
made that collision worse at exactly the moment there are ten of them. Ten
agents into 684 slots is about a 6% chance of one aliasing.

## Config

| Key | Default | Meaning |
| --- | --- | --- |
| `reports` | `on` | `off` reads report replies as ordinary messages. The words still reach the panel; only the state colouring goes away. Rides `inbox`, which rides `fleet`. |
| `report_expire_s` | `300` | How long a report stays on the panel. Longer than a message's 180 s because a report is a snapshot the owner asked for — but bounded, because the host cannot see the agent change its mind. The age on the card keeps it honest meanwhile. |
| `sessions_budget_bytes` | `180` | Raise to `500` for report rounds; see the arithmetic above. |

One card per **agent**: a newer report from the same agent replaces the older
one outright rather than stacking beside it, so a re-dispatch refreshes the
panel instead of doubling it. `inbox_max_rows` (a burst cap for unsolicited
mail) does not apply to reports — what bounds a round is the device's row cap
and the byte budget.

The privacy posture is the message rows' posture, unchanged: report text is
read from local disk and crosses your own BLE link to your own device. See
[FLEET.md](FLEET.md#privacy--this-one-is-different).

## Checking it without the device

`clawdmeter_inbox.py` runs standalone and read-only:

```bash
python3 daemon/clawdmeter_inbox.py --once --budget 500     # what is live now
python3 daemon/clawdmeter_inbox.py --watch --budget 500    # keep printing
python3 daemon/clawdmeter_inbox.py --once --no-reports     # as plain messages
```

Reports print with their state code in brackets. To see a round without a
dispatcher, send yourself a contract line from any Claude Code session with
`SendMessage`.
