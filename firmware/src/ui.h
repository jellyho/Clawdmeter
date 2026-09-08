#pragma once
#include "data.h"
#include "ble.h"

// Screens are TABS. The user swipes horizontally to move between them:
// left = next, right = previous, wrapping at both ends. The ring is built at
// init from the board's capabilities (ui_init → build_tab_order), so a board
// that can't host the chat views simply never has SCREEN_SESSIONS in its ring
// and can't swipe onto an empty tab.
//
// The splash is part of the ring rather than a mode outside it: that keeps one
// navigation model, leaves every screen reachable by swipe alone (the LCD-4
// has a single button), and preserves the existing tap-to-toggle shortcut.
enum screen_t {
    SCREEN_SPLASH,
    SCREEN_USAGE,
    SCREEN_SESSIONS,   // live Claude Code sessions (BOARD_HAS_SESSION_VIEWS)
    SCREEN_SETTINGS,
    SCREEN_COUNT,
};

void ui_init(void);
void ui_update(const UsageData* data);
// Live session awareness (issue #135). The chat views live on SCREEN_SESSIONS;
// a session entering the "waiting" bucket can auto-jump the tab there (rising
// edge only, and only when the user's auto-jump setting is on).
// No-op on boards without BOARD_HAS_SESSION_VIEWS.
void ui_update_sessions(const SessionList* list);
void ui_tick_anim(void);
void ui_show_screen(screen_t screen);
// Step through the tab ring: +1 = next (swipe left), -1 = previous.
void ui_next_tab(int dir);
void ui_toggle_splash(void);
screen_t ui_get_current_screen(void);
void ui_update_ble_status(ble_state_t state, const char* name, const char* mac);
void ui_update_battery(int percent, bool charging);
