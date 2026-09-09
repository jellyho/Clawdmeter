#pragma once
#include <Arduino.h>

struct UsageData {
    float session_pct;       // utilization 0-100 (5h window Pro/Max; spending % Enterprise)
    int session_reset_mins;  // minutes until reset
    float weekly_pct;        // 7-day utilization (Pro/Max only; 0 for Enterprise)
    int weekly_reset_mins;   // minutes until weekly reset (Pro/Max only)
    char status[16];         // "allowed", "limited", etc.
    bool chime;              // play the session-reset chime; false unless daemon opts in
    bool enterprise;         // true = Enterprise spending-limit account
    int time_pct;            // 0-100: fraction of billing period elapsed (Enterprise)
    int period_days;         // total billing period length in days (Enterprise)
    char reset_date[12];     // formatted reset date e.g. "Jul 1" (Enterprise)
    long clock_epoch;        // local wall-clock epoch (s) from daemon; 0 = not provided
    int  clock_fmt;          // 12 or 24 (hour format from daemon); defaults to 24
    bool ok;                 // data parse succeeded
    bool valid;              // false until first successful parse
};

// ---- Live session awareness (issue #135) ----
// Compile-time gate for the chat views. Set per-board in board.h AND as a
// -D build flag in platformio.ini (shared code can't see board.h — same
// mechanism as BOARD_HAS_PSRAM). Absent → the views aren't compiled.
#ifndef BOARD_HAS_SESSION_VIEWS
#define BOARD_HAS_SESSION_VIEWS 0
#endif

// Wire codes below are APPEND-ONLY: they cross the BLE boundary, so
// renumbering would desync any host/firmware pair of different vintages.

enum session_state_t : uint8_t {
    SESSION_STARTING           = 0,   // idle bucket
    SESSION_IDLE               = 1,   // idle bucket
    SESSION_THINKING           = 2,   // working bucket
    SESSION_RESPONDING         = 3,   // working bucket
    SESSION_RUNNING_TOOL       = 4,   // working bucket
    SESSION_COMPACTING         = 5,   // working bucket
    SESSION_WAITING_PERMISSION = 6,   // waiting bucket — accent + pulse
    SESSION_WAITING_QUESTION   = 7,   // waiting bucket — accent + pulse
    SESSION_WAITING_INPUT      = 8,   // waiting bucket — accent + pulse
    SESSION_ERROR              = 9,   // waiting bucket — accent + pulse
    SESSION_ENDED              = 10,  // never sent to the device
    // Not a session at all. Another Claude Code session sent THIS MACHINE a
    // message (the SendMessage / ListAgents peer channel); the host's inbox
    // watcher turns it into a row so the panel can show it. `label` carries
    // the sender, `msg` (wire index 13) the body, and every session-shaped
    // field arrives as "not applicable" (-1 / 0) to be hidden, not drawn as a
    // confident zero. Its own bucket in the UI: never the waiting pulse (a
    // message is something to read, not a chat blocked on you).
    SESSION_MESSAGE            = 11,

    // ---- Agent reports (daemon/REPORT.md) ----
    // Also not sessions. The owner presses a button, a dispatcher asks every
    // reachable Claude Code agent to report in, and each agent replies with
    // one contract line by cross-session message. The host's inbox watcher
    // recognises that line and mints one of the states below.
    //
    // A report is NOT a new row kind, on purpose: it is a message-shaped card
    // whose STATE means something. It keeps the message LAYOUT (sender line,
    // then the words) and takes its dot colour, its sort bucket and its
    // auto-jump from the state — all machinery this file's codes already
    // drive. `label` carries the agent, `msg` (index 13) the one-line
    // summary, every session-shaped field arrives as "not applicable".
    //
    // The split between NEEDS_YOU and BLOCKED is the one that earns its code.
    // A later feature will let a button on a report card send "go ahead" back
    // to the agent, and it only works on one of the two:
    //
    //   WORKING    busy, needs nothing               → nothing to send
    //   NEEDS_YOU  stopped, waiting for direction    → THE valid target: the
    //                                                  message reaches its
    //                                                  input queue and it goes
    //   BLOCKED    stopped at a permission prompt    → a message cannot help;
    //                                                  it queues BEHIND the
    //                                                  dialog until a human
    //                                                  clicks on that machine
    //   DONE       finished, nothing running         → nothing to continue
    //
    // So `state == SESSION_REPORT_NEEDS_YOU` is the whole predicate that
    // future button needs. NEEDS_YOU and BLOCKED are both the WAITING bucket
    // (accent + pulse + sorts first + trips the auto-jump); WORKING and DONE
    // are not, because nobody is blocked.
    SESSION_REPORT_WORKING     = 12,  // working bucket
    SESSION_REPORT_NEEDS_YOU   = 13,  // waiting bucket — and the resume target
    SESSION_REPORT_BLOCKED     = 14,  // waiting bucket — NOT the resume target
    SESSION_REPORT_DONE        = 15,  // working bucket, green dot
    // Host-minted, never sent by an agent: "N reports did not fit". Ten agents
    // can answer one dispatch and SESSION_MAX_ROWS is 6, so dropping is the
    // normal case — this row is what stops it being a silent one. Idle bucket
    // and never in the notify set: it is a footnote, not an alert.
    SESSION_REPORT_MORE        = 16,

    // Host-minted too, and the row that makes the rest of this tab
    // trustworthy. The remote listing is polled by the HOST, and the host
    // writes the handoff file only WHEN SOMETHING CHANGES -- so an unchanging
    // payload is ambiguous between "the fleet is quiet" and "the host has not
    // reached the listing since last night". The device cannot break that tie
    // from a timer: a genuinely quiet fleet legitimately sends nothing for
    // hours, and a firmware that guessed would either cry wolf on a calm desk
    // or stay silent through a real outage. So the HOST says it, because the
    // host is the only side that knows when it last succeeded.
    //
    // When its listing has been failing for longer than `fleet_stale_after_s`
    // (900 s by default -- 30 consecutive poll failures), the host DROPS every
    // row that came from that listing and sends this one in their place:
    // `label` names the fault, `msg` (index 13) says why in the host's own
    // words ("auth expired - log in to claude"), and `elapsed_s` is the age of
    // the last good listing, so the card dates itself. Rows from local disk
    // (messages, agent reports) are unaffected and keep flowing beside it --
    // the marker means the LISTING is blind, not that everything is.
    //
    // Idle bucket, dim, and never in the notify set, exactly like MORE: this
    // is a fault indication, not an alert. Nothing is waiting on the owner --
    // the panel has simply stopped knowing whether anything is.
    SESSION_HOST_STALE         = 17,
};

// A report card and a message card share the same widget anatomy (a sender
// line and the words under it) and the same wire field for their text. This
// is the test both of them answer yes to; the state itself still decides the
// colour and the bucket. Session rows answer no.
static inline bool session_state_has_words(uint8_t s) {
    return s >= SESSION_MESSAGE && s <= SESSION_HOST_STALE;
}

// ...and this is the subset the HOST wrote itself rather than relaying from
// an agent. Both are footnotes about the list rather than members of it, so
// neither gets the purple sender colour that means "another Claude is
// talking to you" -- their label is a count or a fault, not a peer's name.
static inline bool session_state_host_minted(uint8_t s) {
    return s == SESSION_REPORT_MORE || s == SESSION_HOST_STALE;
}

enum session_model_t : uint8_t {
    SESSION_MODEL_UNKNOWN = 0,
    SESSION_MODEL_OPUS    = 1,
    SESSION_MODEL_SONNET  = 2,
    SESSION_MODEL_HAIKU   = 3,
    SESSION_MODEL_FABLE   = 4,
};

enum session_tool_t : uint8_t {
    SESSION_TOOL_NONE      = 0,   // other / none
    SESSION_TOOL_BASH      = 1,
    SESSION_TOOL_READ      = 2,
    SESSION_TOOL_EDIT      = 3,
    SESSION_TOOL_WRITE     = 4,
    SESSION_TOOL_GREP      = 5,
    SESSION_TOOL_GLOB      = 6,
    SESSION_TOOL_TASK      = 7,
    SESSION_TOOL_WEBFETCH  = 8,
    SESSION_TOOL_WEBSEARCH = 9,
};

// The panel fits 3.5 cards; rows past this cap are parsed only to be dropped.
// The host sorts before it sends, so what's dropped is what matters least.
#define SESSION_MAX_ROWS  6
#define SESSION_LABEL_MAX 32     // host middle-elides to fit the MTU budget;
                                 // the UI ellipsizes to the card width itself
// Message body (wire index 13, SESSION_MESSAGE rows only), in BYTES — which
// is not the same as characters any more and is the whole reason this number
// moved. The host head-elides ("first words...") to a character cap; ASCII
// spends one byte per character, Korean spends three, so the same 40-character
// cap is 40 B of Latin or 120 B of Hangul (docs/fonts.md § "Byte budget").
//
// 104 is sized off what the panel can actually DRAW, not off the host's cap:
// a list card gives the body two 422 px lines, and Hangul advances 26.31 px
// at 28 px, so two full lines are 32 syllables = 96 B, + "..." + NUL. At the
// old 48 the panel held 15 syllables — less than ONE of its two lines — so
// every Korean message arrived pre-truncated to half a card. ASCII is
// unaffected: the host still caps it well under this.
//
// It is a fixed field on EVERY row rather than a side pool: rows are filled
// positionally by index and matched by sid, and a pool would buy 4 rows'
// worth of bytes at the price of an indirection in the hottest render path.
// Static cost of the choice: 104 B x SESSION_MAX_ROWS = 624 B, taking the one
// SessionList instance (main.cpp) from 340 B to 964 B — measured on the
// PSRAM-free C6, where it is internal SRAM that is already carrying LVGL, and
// still under 0.3% of the 320 KB there.
#define SESSION_MSG_MAX   104

struct SessionRow {
    char    sid[3];                  // 2 hex chars + NUL, stable for the session's
                                     // life — keys the card identity / reorder slide
    char    label[SESSION_LABEL_MAX];
    uint8_t state;                   // session_state_t
    int8_t  ctx_pct;                 // context fill 0-100; -1 = unknown (bar hidden)
    int32_t elapsed_s;               // seconds in the current state
    uint8_t model;                   // session_model_t
    uint8_t tool;                    // session_tool_t (shown when state==RUNNING_TOOL)
    uint8_t ntools;                  // concurrent pending tools
    uint8_t nagents;                 // running subagents; badge hidden at 0
    uint8_t tdone;                   // todos done
    uint8_t ttotal;                  // todos total; badge hidden at 0
    int32_t tok;                     // context tokens used, in units of 1k
                                     // (190 = 190k, 1200 = 1.2M); -1 = unknown.
                                     // Wire index 11; absent (older host) → -1.
    char    msg[SESSION_MSG_MAX];    // the card's words — a SESSION_MESSAGE
                                     // body or a SESSION_REPORT_* summary
                                     // (session_state_has_words). UTF-8: ASCII
                                     // 32..126 plus the 2,350 KS X 1001
                                     // Hangul syllables the fallback font
                                     // carries (font_nanum_kr_28); everything
                                     // else the host folds away. Head-elided
                                     // by the host, and re-cut here on a
                                     // character boundary if it still
                                     // overflows. Wire index 13; empty on
                                     // session rows and on every host older
                                     // than the inbox.
};

struct SessionList {
    uint8_t    count;                // rows in use — host pre-sorted, render in order
    SessionRow rows[SESSION_MAX_ROWS];
};
