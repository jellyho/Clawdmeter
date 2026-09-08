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
};

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
    char    msg[SESSION_MSG_MAX];    // SESSION_MESSAGE body, UTF-8: ASCII
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
