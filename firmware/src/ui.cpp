#include "ui.h"
#include "ble.h"
#include "splash.h"
#include <lvgl.h>
#include <time.h>
#include "logo.h"
#include "clawd_still.h"
#include "icons.h"
#include "settings.h"
#include "hal/board_caps.h"

// Custom fonts (scaled for 314 PPI, ~1.9x from original 165 PPI)
LV_FONT_DECLARE(font_tiempos_56);
LV_FONT_DECLARE(font_tiempos_34);
LV_FONT_DECLARE(font_styrene_48);
LV_FONT_DECLARE(font_styrene_28);
LV_FONT_DECLARE(font_styrene_24);
LV_FONT_DECLARE(font_styrene_20);
LV_FONT_DECLARE(font_styrene_16);
LV_FONT_DECLARE(font_styrene_14);
LV_FONT_DECLARE(font_styrene_12);
LV_FONT_DECLARE(font_mono_32);
LV_FONT_DECLARE(font_mono_18);

// Layout values computed from the active board's geometry. Populated once
// in ui_init() and treated as const for the rest of the program. Adding a
// new display size means extending compute_layout() with another
// breakpoint — never editing the screen-builder functions below.
struct Layout {
    int16_t scr_w, scr_h;
    int16_t margin;
    int16_t overscroll_max;          // how far a list may rubber-band past an end
    int16_t title_y;
    int16_t content_y;
    int16_t content_w;

    // Usage screen
    int16_t usage_panel_h;
    int16_t usage_panel_gap;
    int16_t usage_bar_y;
    int16_t usage_reset_y;
    int16_t bar_h;
    int16_t panel_pad_x, panel_pad_y;
    int16_t pill_pad_x, pill_pad_y;
    const lv_font_t* title_font;     // screen title / clock
    const lv_font_t* pct_font;       // big percentage number
    const lv_font_t* ent_pct_font;   // enterprise spending number
    const lv_font_t* pill_font;      // "Current" / "Weekly" pill
    const lv_font_t* reset_font;     // "Resets in ..." line
    const lv_font_t* pace_font;      // enterprise "Under/On/Over pace" line
    const lv_font_t* anim_font;      // animated status line
    int16_t anim_y;                  // status line offset from bottom
    bool    small_icons;             // 40px logo + 24px battery (vs 80/48) on small screens
    int16_t title_nudge;             // title x-shift balancing the corner logo
    int16_t logo_y;                  // logo top edge
    int16_t batt_y;                  // battery icon top edge
    int16_t batt_w;                  // battery icon width, for position math

    // Pairing hint / idle screen
    int16_t pair_y1, pair_y2, pair_y3;
    int16_t idle_px;                 // sleeping-creature size on the idle screen

    // Bluetooth screen
    int16_t bt_info_panel_h;
    int16_t bt_reset_zone_h;
    const lv_font_t* bt_title_font;
    const lv_font_t* bt_status_font;
    const lv_font_t* bt_device_font;
    const lv_font_t* bt_credit_1_font;
    const lv_font_t* bt_credit_2_font;

    // Settings screen — one panel per setting, stacked from content_y.
    // set_detail_font == nullptr means the panel is too short for a subtitle
    // (240x240): the label alone is centered instead.
    int16_t set_row_h;
    int16_t set_row_gap;
    int16_t set_row_pad_y;
    const lv_font_t* set_label_font;
    const lv_font_t* set_detail_font;
    const lv_font_t* set_value_font;
};
static Layout L = {};

// Pick layout values from the active board's pixel dimensions. The two
// existing boards happen to land on the two breakpoints below; new ports
// inherit the closer one — visually OK, may need a polish pass for
// pixel-perfect alignment but never blocks the port from booting.
static void compute_layout(const BoardCaps& c) {
    L.scr_w = c.width;
    L.scr_h = c.height;
    L.margin = 20;
    L.title_y = 30;
    // The rubber band is a signal, not a journey: enough travel to read as
    // elastic, not enough to pull a whole card clear of the edge. Scaled with
    // the panel like everything else here, and clamped so a 240 px port still
    // gets a visible one and a 502 px port doesn't get a slack one.
    L.overscroll_max = c.height / 20;
    if (L.overscroll_max < 12) L.overscroll_max = 12;
    if (L.overscroll_max > 24) L.overscroll_max = 24;

    // Values shared by the two original breakpoints; the small branch below
    // overrides them wholesale.
    L.bar_h = 24;
    L.panel_pad_x = 16;
    L.panel_pad_y = 12;
    L.pill_pad_x = 18;
    L.pill_pad_y = 6;
    L.title_font   = &font_tiempos_56;
    L.pct_font     = &font_styrene_48;
    L.ent_pct_font = &font_tiempos_56;
    L.pill_font    = &font_styrene_28;
    L.reset_font   = &font_styrene_28;
    L.pace_font    = &font_styrene_16;
    L.anim_font    = &font_mono_32;
    L.anim_y = -15;
    L.small_icons = false;
    L.title_nudge = 16;
    L.logo_y = L.title_y - 10;
    L.batt_y = L.title_y;
    L.batt_w = ICON_BATTERY_W;
    L.pair_y1 = 40;
    L.pair_y2 = 120;
    L.pair_y3 = 160;
    L.idle_px = 160;

    if (c.height >= 460) {
        // Large layout — tuned for 480x480 (AMOLED-2.16).
        L.content_y = 100;
        L.usage_panel_h = 150;
        L.usage_panel_gap = 16;
        L.usage_bar_y = 56;
        L.usage_reset_y = 94;
        L.bt_info_panel_h = 160;
        L.bt_reset_zone_h = 110;
        L.bt_title_font    = &font_tiempos_56;
        L.bt_status_font   = &font_styrene_48;
        L.bt_device_font   = &font_styrene_28;
        L.bt_credit_1_font = &font_styrene_24;
        L.bt_credit_2_font = &font_styrene_20;
        // 5 rows: 100 + 5*(64+6) - 6 = 444, clear of the 480 bottom edge.
        L.set_row_h        = 64;
        L.set_row_gap      = 6;
        L.set_row_pad_y    = 7;
        L.set_label_font   = &font_styrene_28;
        L.set_detail_font  = &font_styrene_16;
        L.set_value_font   = &font_styrene_24;
    } else if (c.height >= 300) {
        // Compact layout — tuned for 368x448 (AMOLED-1.8).
        L.content_y = 85;
        L.usage_panel_h = 130;
        L.usage_panel_gap = 12;
        L.usage_bar_y = 48;
        L.usage_reset_y = 78;
        L.bt_info_panel_h = 140;
        L.bt_reset_zone_h = 90;
        L.bt_title_font    = &font_tiempos_34;
        L.bt_status_font   = &font_styrene_28;
        L.bt_device_font   = &font_styrene_20;
        L.bt_credit_1_font = &font_styrene_16;
        L.bt_credit_2_font = &font_styrene_14;
        // 5 rows: 85 + 5*(54+6) - 6 = 379, clear of the 448 bottom edge.
        L.set_row_h        = 54;
        L.set_row_gap      = 6;
        L.set_row_pad_y    = 6;
        L.set_label_font   = &font_styrene_24;
        L.set_detail_font  = &font_styrene_12;
        L.set_value_font   = &font_styrene_20;
    } else {
        // Small layout — tuned for 240x240 (LCD-1.54 and similar square TFTs).
        // Everything shrinks: fonts two steps down, panels ~half height, and
        // the corner logo/battery switch to the 40px/24px small assets.
        L.margin = 8;
        L.title_y = 4;
        L.content_y = 44;
        L.usage_panel_h = 74;
        L.usage_panel_gap = 6;
        L.usage_bar_y = 30;
        L.usage_reset_y = 46;
        L.bar_h = 12;
        L.panel_pad_x = 10;
        L.panel_pad_y = 6;
        L.pill_pad_x = 8;
        L.pill_pad_y = 2;
        L.title_font   = &font_tiempos_34;
        L.pct_font     = &font_styrene_24;
        L.ent_pct_font = &font_tiempos_34;
        L.pill_font    = &font_styrene_14;
        L.reset_font   = &font_styrene_14;
        L.pace_font    = &font_styrene_12;
        L.anim_font    = &font_mono_18;
        // Center the status line in the strip below the weekly panel; flush
        // against the bottom edge it reads as unevenly spaced.
        L.anim_y = -10;
        L.small_icons = true;
        L.title_nudge = 8;
        L.logo_y = 2;
        L.batt_y = 10;
        L.batt_w = ICON_BATTERY_SMALL_W;
        L.pair_y1 = 12;
        L.pair_y2 = 56;
        L.pair_y3 = 80;
        L.idle_px = 96;
        L.bt_info_panel_h = 90;
        L.bt_reset_zone_h = 60;
        L.bt_title_font    = &font_tiempos_34;
        L.bt_status_font   = &font_styrene_20;
        L.bt_device_font   = &font_styrene_14;
        L.bt_credit_1_font = &font_styrene_12;
        L.bt_credit_2_font = &font_styrene_12;
        // 5 rows: 44 + 5*(30+4) - 4 = 210 of 240. No room for a subtitle —
        // the label carries the row on its own here.
        L.set_row_h        = 30;
        L.set_row_gap      = 4;
        L.set_row_pad_y    = 4;
        L.set_label_font   = &font_styrene_14;
        L.set_detail_font  = nullptr;
        L.set_value_font   = &font_styrene_12;
    }

    L.content_w = L.scr_w - 2 * L.margin;
}

// Anthropic brand palette — design tokens live in theme.h
#include "theme.h"
#define COL_BG        THEME_BG
#define COL_PANEL     THEME_PANEL
#define COL_TEXT      THEME_TEXT
#define COL_DIM       THEME_DIM
#define COL_ACCENT    THEME_ACCENT
#define COL_GREEN     THEME_GREEN
#define COL_AMBER     THEME_AMBER
#define COL_RED       THEME_RED
#define COL_PURPLE    THEME_PURPLE
#define COL_BAR_BG    THEME_BAR_BG

// ---- Tab containers ----
// One per non-splash screen; ui_show_screen() is the only thing that toggles
// their HIDDEN flag. sessions_container is only built on boards that advertise
// the chat views (it stays null elsewhere, and the tab ring skips it).
static lv_obj_t* sessions_container = nullptr;
static lv_obj_t* settings_container = nullptr;
// The rows live in their own scroll region so the list can outgrow the panel.
// It already does: six rows do not fit 480x480, and a 240x240 board fits three.
static lv_obj_t* set_rows_cont = nullptr;

// ---- Usage screen widgets ----
static lv_obj_t* usage_container;
static lv_obj_t* lbl_title;
// Clock fed by the daemon: base epoch (local wall-clock seconds) + the lv_tick at
// which it landed, so the title ticks forward locally between 60s payloads.
static long     clock_base_epoch = 0;
static uint32_t clock_base_ms = 0;
static int      clock_fmt = 24;   // 12 or 24, set from the daemon payload
static int      clock_last_min = -1;   // last rendered minute; avoids redrawing the title every tick
static lv_obj_t* usage_group;   // the two usage panels — shown when connected
static lv_obj_t* pair_group;    // pairing hint — shown when disconnected
static lv_obj_t* bar_session;
static lv_obj_t* lbl_session_pct;
static lv_obj_t* lbl_session_label;
static lv_obj_t* lbl_session_reset;
static lv_obj_t* bar_weekly;
static lv_obj_t* lbl_weekly_pct;
static lv_obj_t* lbl_weekly_label;
static lv_obj_t* lbl_weekly_reset;
static lv_obj_t* panel_session = nullptr;
static lv_obj_t* panel_weekly = nullptr;
// Enterprise-only widgets inside panel_session
static lv_obj_t* lbl_session_pct_sym = nullptr;  // "%" in smaller font
static lv_obj_t* lbl_spending_desc = nullptr;     // "of your monthly budget"
static lv_obj_t* lbl_spending_status = nullptr;   // "Under pace" / "On pace" / "Over pace"
static lv_obj_t* lbl_anim;      // status line: connection state + whimsical idle

// ---- Battery indicator (shared, on top) ----
static lv_obj_t* battery_img;
static lv_obj_t* logo_img;
static lv_image_dsc_t battery_dscs[5];  // empty, low, medium, full, charging

// ---- Live-data freshness → which usage sub-view to show ----
// usage panels when data is flowing, an idle "Zzz" screen when the host is
// connected but no usage update landed within DATA_FRESH_MS, the pairing hint
// when BLE is down. Re-evaluated every loop in ui_tick_anim().
static lv_obj_t* idle_group;            // the "Zzz" idle screen
static uint32_t  last_data_ms = 0;      // lv_tick when the last valid usage update landed
static bool      data_received = false; // any valid update since boot
static bool      data_ok = true;        // last payload's ok flag; a {"ok":false} beat = "no fresh data"
// -1 unknown / 0 pair / 1 idle / 2 usage. States 3 and 4 (the chat views) used
// to live here too; they are now the SCREEN_SESSIONS tab and are resolved by
// update_session_view() instead — the usage screen no longer auto-selects them.
static int       view_state = -1;
static const uint32_t DATA_FRESH_MS = 90000;  // usage counts as "live" within this window (daemon sends ~60s)

// ---- Shared ----
static lv_image_dsc_t logo_dsc;
static screen_t current_screen = SCREEN_USAGE;
static bool     s_ble_connected = false;   // cached BLE connection state
static uint32_t connected_at_ms = 0;       // when we last entered CONNECTED ("Connected" dwell)

// ---- Tab model ----
// The swipe ring, built once in ui_init() from board_caps(). A screen that the
// board can't host is simply absent from the ring, so no swipe can ever land
// on an empty tab. ui_show_screen() stays the single place that shows/hides
// containers; the ring only decides *which* screen it is handed.
static screen_t tab_order[SCREEN_COUNT];
static uint8_t  tab_count = 0;

// Swipe → click disambiguation. LVGL sends LV_EVENT_CLICKED on release even
// when a gesture already fired during the same press, so a swipe would also
// toggle the splash. The flag is raised by the gesture handler and cleared on
// the next LV_EVENT_PRESSED — never inside a click handler, because a gesture
// that ends with the finger off the pressed object produces no CLICKED at all
// and a self-clearing flag would then eat the *following* tap.
static bool s_gesture_used = false;

// Auto-jump bookkeeping (requirement 4). s_auto_jumped means "the tab the user
// is looking at was chosen by the firmware, not by them" — it is dropped the
// moment they touch the panel or navigate, so the return trip below can never
// move the screen out from under a hand.
// The return trip is deliberately lazy: looking at a notification produces no
// touch, so handing the screen back the instant the payload clears would yank
// it out from under someone mid-read (and a waiting/clear/waiting burst would
// flip the panel twice). It waits for the dwell below, and any touch or manual
// navigation cancels it outright.
static bool     s_auto_jumped     = false;
static screen_t s_auto_jump_from  = SCREEN_USAGE;
static uint32_t s_auto_jump_ms    = 0;      // lv_tick when the jump happened
static bool     s_auto_return_due = false;  // notify set cleared; dwell running
#define AUTO_RETURN_DWELL_MS 10000u

// Animation state
static uint32_t anim_last_ms = 0;
static uint8_t anim_spinner_idx = 0;
static uint8_t anim_phase = 0;
static uint8_t anim_msg_idx = 0;
static uint32_t anim_msg_start = 0;
#define ANIM_MSG_MS     4000

static const char* const spinner_frames[] = {
    "\xC2\xB7", "\xE2\x9C\xBB", "\xE2\x9C\xBD",
    "\xE2\x9C\xB6", "\xE2\x9C\xB3", "\xE2\x9C\xA2",
};
#define SPINNER_COUNT 6
#define SPINNER_PHASES (2 * (SPINNER_COUNT - 1))  // 10: ping-pong 0..5..0

static const uint16_t spinner_ms[SPINNER_COUNT] = {
    260, 130, 130, 130, 130, 260,
};

static const char* const anim_messages[] = {
    "Accomplishing", "Elucidating", "Perusing",
    "Actioning", "Enchanting", "Philosophising",
    "Actualizing", "Envisioning", "Pondering",
    "Baking", "Finagling", "Pontificating",
    "Booping", "Flibbertigibbeting", "Processing",
    "Brewing", "Forging", "Puttering",
    "Calculating", "Forming", "Puzzling",
    "Cerebrating", "Frolicking", "Reticulating",
    "Channelling", "Generating", "Ruminating",
    "Churning", "Germinating", "Scheming",
    "Clauding", "Hatching", "Schlepping",
    "Coalescing", "Herding", "Shimmying",
    "Cogitating", "Honking", "Shucking",
    "Combobulating", "Hustling", "Simmering",
    "Computing", "Ideating", "Smooshing",
    "Concocting", "Imagining", "Spelunking",
    "Conjuring", "Incubating", "Spinning",
    "Considering", "Inferring", "Stewing",
    "Contemplating", "Jiving", "Sussing",
    "Cooking", "Manifesting", "Synthesizing",
    "Crafting", "Marinating", "Thinking",
    "Creating", "Meandering", "Tinkering",
    "Crunching", "Moseying", "Transmuting",
    "Deciphering", "Mulling", "Unfurling",
    "Deliberating", "Mustering", "Unravelling",
    "Determining", "Musing", "Vibing",
    "Discombobulating", "Noodling", "Wandering",
    "Divining", "Percolating", "Whirring",
    "Doing", "Wibbling",
    "Effecting", "Wizarding",
    "Working", "Wrangling",
};
#define ANIM_MSG_COUNT (sizeof(anim_messages) / sizeof(anim_messages[0]))

static lv_color_t pct_color(float pct) {
    if (pct >= 80.0f) return COL_RED;
    if (pct >= 50.0f) return COL_AMBER;
    return COL_GREEN;
}

static void format_reset_time(int mins, char* buf, size_t len) {
    if (mins < 0) {
        snprintf(buf, len, "---");
    } else if (mins < 60) {
        snprintf(buf, len, "Resets in %dm", mins);
    } else if (mins < 1440) {
        snprintf(buf, len, "Resets in %dh %dm", mins / 60, mins % 60);
    } else {
        snprintf(buf, len, "Resets in %dd %dh", mins / 1440, (mins % 1440) / 60);
    }
}

// A routine content refresh must not animate anything (§2.3) — and rewriting
// a label always invalidates it, so compare first. Shared by the chat cards
// and the settings rows, both of which re-render on a timer.
static void set_label_if_changed(lv_obj_t* lbl, const char* txt) {
    if (strcmp(lv_label_get_text(lbl), txt) != 0) lv_label_set_text(lbl, txt);
}

// Forward decls — callbacks defined near ui_show_screen below
static void global_click_cb(lv_event_t* e);
// The one place that shows/hides containers. `manual` distinguishes a user
// navigation (swipe / tap / button) from a firmware-initiated one (auto-jump).
static void show_screen(screen_t screen, bool manual);

// ---- Overscroll: a short rubber band, not a long one ----
// LVGL's elastic overscroll has no depth limit. It divides a drag past either
// end by a fixed factor and then lets you keep pulling, and a flick that
// reaches the end throws a long way beyond it before easing back — far enough
// on a 480 px panel to leave most of a card's worth of empty space showing.
//
// The bounce itself is worth keeping: it is what says "that was the last one",
// where a list that stops dead reads as a list that is stuck. It is the
// DISTANCE that is wrong, so this caps it rather than turning it off.
//
// It caps it by taking LVGL's own elastic away at the cap and giving it back
// on the way home, which is the one move that does not fight the scroller.
// Pulling the content back with lv_obj_scroll_by() — the obvious
// implementation, and the first one here — puts this in a feedback loop with
// LVGL's elastic and throw handling: measured on the C6 it turned a steady 2
// invalidations per frame into bursts of up to 594, past LV_INV_BUF_SIZE, at
// which point LVGL gives up and repaints the whole 480x480 screen. Frames of
// ~225 ms, a worse stutter than the long bounce it was fixing. With the flag
// cleared instead, lv_indev_scroll's elastic_diff() simply declines to move
// the content further out and the band holds where it is; nothing is scrolled
// from out here, so there is no loop to get into. Release still eases back to
// the edge — that path does not consult the flag.
//
// Called from ui_tick_anim(), so the state is read one frame after the drag
// that caused it: the band can overshoot the cap by a single frame's worth of
// give before it locks, which if anything reads softer than a hard wall.
static void clamp_overscroll(lv_obj_t* cont) {
    if (!cont) return;
    const int32_t max = L.overscroll_max;
    // Negative means "pulled past this end by that many pixels".
    const bool at_cap = lv_obj_get_scroll_top(cont)    < -max ||
                        lv_obj_get_scroll_bottom(cont) < -max;
    const bool elastic = lv_obj_has_flag(cont, LV_OBJ_FLAG_SCROLL_ELASTIC);
    if (at_cap && elastic)        lv_obj_remove_flag(cont, LV_OBJ_FLAG_SCROLL_ELASTIC);
    else if (!at_cap && !elastic) lv_obj_add_flag(cont, LV_OBJ_FLAG_SCROLL_ELASTIC);
}

static lv_obj_t* make_panel(lv_obj_t* parent, int x, int y, int w, int h) {
    lv_obj_t* panel = lv_obj_create(parent);
    lv_obj_set_pos(panel, x, y);
    lv_obj_set_size(panel, w, h);
    lv_obj_set_style_bg_color(panel, COL_PANEL, 0);
    lv_obj_set_style_bg_opa(panel, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(panel, 8, 0);
    lv_obj_set_style_border_width(panel, 0, 0);
    lv_obj_set_style_pad_left(panel, L.panel_pad_x, 0);
    lv_obj_set_style_pad_right(panel, L.panel_pad_x, 0);
    lv_obj_set_style_pad_top(panel, L.panel_pad_y, 0);
    lv_obj_set_style_pad_bottom(panel, L.panel_pad_y, 0);
    lv_obj_clear_flag(panel, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(panel, LV_OBJ_FLAG_EVENT_BUBBLE);
    return panel;
}

static lv_obj_t* make_bar(lv_obj_t* parent, int x, int y, int w, int h) {
    lv_obj_t* bar = lv_bar_create(parent);
    lv_obj_set_pos(bar, x, y);
    lv_obj_set_size(bar, w, h);
    lv_bar_set_range(bar, 0, 100);
    lv_bar_set_value(bar, 0, LV_ANIM_OFF);
    lv_obj_set_style_bg_color(bar, COL_BAR_BG, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(bar, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_radius(bar, 6, LV_PART_MAIN);
    lv_obj_set_style_bg_color(bar, COL_GREEN, LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(bar, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_obj_set_style_radius(bar, 6, LV_PART_INDICATOR);
    return bar;
}

static void init_icon_dsc_rgb565a8(lv_image_dsc_t* dsc, int w, int h, const uint8_t* data) {
    dsc->header.w = w;
    dsc->header.h = h;
    dsc->header.cf = LV_COLOR_FORMAT_RGB565A8;
    dsc->header.stride = w * 2;
    dsc->data = data;
    dsc->data_size = w * h * 3;
}

static lv_obj_t* make_pill(lv_obj_t* parent, const char* text) {
    lv_obj_t* lbl = lv_label_create(parent);
    lv_label_set_text(lbl, text);
    lv_obj_set_style_text_font(lbl, L.pill_font, 0);
    lv_obj_set_style_text_color(lbl, COL_TEXT, 0);
    lv_obj_set_style_bg_color(lbl, COL_BAR_BG, 0);
    lv_obj_set_style_bg_opa(lbl, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(lbl, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_pad_left(lbl, L.pill_pad_x, 0);
    lv_obj_set_style_pad_right(lbl, L.pill_pad_x, 0);
    lv_obj_set_style_pad_top(lbl, L.pill_pad_y, 0);
    lv_obj_set_style_pad_bottom(lbl, L.pill_pad_y, 0);
    return lbl;
}

static void init_battery_icons(void) {
    if (L.small_icons) {
        init_icon_dsc_rgb565a8(&battery_dscs[0], ICON_BATTERY_SMALL_W, ICON_BATTERY_SMALL_H, icon_battery_small_data);
        init_icon_dsc_rgb565a8(&battery_dscs[1], ICON_BATTERY_LOW_SMALL_W, ICON_BATTERY_LOW_SMALL_H, icon_battery_low_small_data);
        init_icon_dsc_rgb565a8(&battery_dscs[2], ICON_BATTERY_MEDIUM_SMALL_W, ICON_BATTERY_MEDIUM_SMALL_H, icon_battery_medium_small_data);
        init_icon_dsc_rgb565a8(&battery_dscs[3], ICON_BATTERY_FULL_SMALL_W, ICON_BATTERY_FULL_SMALL_H, icon_battery_full_small_data);
        init_icon_dsc_rgb565a8(&battery_dscs[4], ICON_BATTERY_CHARGING_SMALL_W, ICON_BATTERY_CHARGING_SMALL_H, icon_battery_charging_small_data);
        return;
    }
    init_icon_dsc_rgb565a8(&battery_dscs[0], ICON_BATTERY_W, ICON_BATTERY_H, icon_battery_data);
    init_icon_dsc_rgb565a8(&battery_dscs[1], ICON_BATTERY_LOW_W, ICON_BATTERY_LOW_H, icon_battery_low_data);
    init_icon_dsc_rgb565a8(&battery_dscs[2], ICON_BATTERY_MEDIUM_W, ICON_BATTERY_MEDIUM_H, icon_battery_medium_data);
    init_icon_dsc_rgb565a8(&battery_dscs[3], ICON_BATTERY_FULL_W, ICON_BATTERY_FULL_H, icon_battery_full_data);
    init_icon_dsc_rgb565a8(&battery_dscs[4], ICON_BATTERY_CHARGING_W, ICON_BATTERY_CHARGING_H, icon_battery_charging_data);
}

// ======== Usage Screen ========

static lv_obj_t* make_usage_panel(lv_obj_t* parent, int y, const char* pill_text,
                                  lv_obj_t** out_pct, lv_obj_t** out_pill,
                                  lv_obj_t** out_bar, lv_obj_t** out_reset) {
    lv_obj_t* panel = make_panel(parent, L.margin, y, L.content_w, L.usage_panel_h);

    *out_pct = lv_label_create(panel);
    lv_label_set_text(*out_pct, "---%");
    lv_obj_set_style_text_font(*out_pct, L.pct_font, 0);
    lv_obj_set_style_text_color(*out_pct, COL_TEXT, 0);
    lv_obj_set_pos(*out_pct, 0, 0);

    *out_pill = make_pill(panel, pill_text);
    lv_obj_align(*out_pill, LV_ALIGN_TOP_RIGHT, 0, 1);

    *out_bar = make_bar(panel, 0, L.usage_bar_y,
                        L.content_w - 2 * L.panel_pad_x, L.bar_h);

    *out_reset = lv_label_create(panel);
    lv_label_set_text(*out_reset, "---");
    lv_obj_set_style_text_font(*out_reset, L.reset_font, 0);
    lv_obj_set_style_text_color(*out_reset, COL_DIM, 0);
    lv_obj_set_pos(*out_reset, 0, L.usage_reset_y);

    return panel;
}

// Pairing hint — shown when disconnected so the screen isn't empty and the
// user knows how to (re)pair. Wording matches the 3-second release gesture.
static void build_pair_group(lv_obj_t* parent) {
    pair_group = lv_obj_create(parent);
    lv_obj_set_size(pair_group, L.scr_w, L.scr_h - L.content_y);
    lv_obj_set_pos(pair_group, 0, L.content_y);
    lv_obj_set_style_bg_opa(pair_group, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(pair_group, 0, 0);
    lv_obj_set_style_pad_all(pair_group, 0, 0);
    lv_obj_clear_flag(pair_group, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(pair_group, LV_OBJ_FLAG_EVENT_BUBBLE);

    lv_obj_t* l1 = lv_label_create(pair_group);
    lv_label_set_text(l1, "To pair");
    lv_obj_set_style_text_font(l1, L.bt_status_font, 0);
    lv_obj_set_style_text_color(l1, COL_TEXT, 0);
    lv_obj_align(l1, LV_ALIGN_TOP_MID, 0, L.pair_y1);

    lv_obj_t* l2 = lv_label_create(pair_group);
    lv_label_set_text(l2, "hold the power button");
    lv_obj_set_style_text_font(l2, L.bt_device_font, 0);
    lv_obj_set_style_text_color(l2, COL_DIM, 0);
    lv_obj_align(l2, LV_ALIGN_TOP_MID, 0, L.pair_y2);

    lv_obj_t* l3 = lv_label_create(pair_group);
    lv_label_set_text(l3, "for 3 seconds, then release");
    lv_obj_set_style_text_font(l3, L.bt_device_font, 0);
    lv_obj_set_style_text_color(l3, COL_DIM, 0);
    lv_obj_align(l3, LV_ALIGN_TOP_MID, 0, L.pair_y3);

    lv_obj_add_flag(pair_group, LV_OBJ_FLAG_HIDDEN);  // ui_update_ble_status decides
}

// Idle "Zzz" screen — shown when the host is connected but no usage update has
// landed recently (token expired, daemon down, host asleep…). Full-screen, like
// the pairing hint, so we never render hours-old numbers as if they were live.
static void build_idle_group(lv_obj_t* parent) {
    idle_group = lv_obj_create(parent);
    lv_obj_set_size(idle_group, L.scr_w, L.scr_h - L.content_y);
    lv_obj_set_pos(idle_group, 0, L.content_y);
    lv_obj_set_style_bg_opa(idle_group, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(idle_group, 0, 0);
    lv_obj_set_style_pad_all(idle_group, 0, 0);
    lv_obj_clear_flag(idle_group, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(idle_group, LV_OBJ_FLAG_EVENT_BUBBLE);

    // A shrunk-down resting creature (the official cloud-ride animation)
    // sits between the header and the status line; the animated "Listening…"
    // status line carries the words, so no extra text is needed here.
    lv_obj_t* creature = splash_mini_create(idle_group, "cloud", L.idle_px);
    if (creature) lv_obj_align(creature, LV_ALIGN_CENTER, 0, -20);

    lv_obj_add_flag(idle_group, LV_OBJ_FLAG_HIDDEN);  // update_view_state decides
}

// ======== Live session awareness (issue #135) ========
// The chat card renderer from the live-sessions work, unchanged. What changed
// around it is navigation: ONE-CHAT (§1.3) and SEVERAL-CHATS (§1.4) used to be
// auto-selected sub-views of the usage screen. They are now the two sub-views
// of their own tab, SCREEN_SESSIONS, which the user reaches by swiping (or is
// carried to by the auto-jump on a rising notification edge). Compiled only on
// boards whose panel can host them (BOARD_HAS_SESSION_VIEWS).

static void update_view_state(void);       // usage-screen resolver, below ui_update
static void update_session_view(void);     // sessions-tab resolver

#if BOARD_HAS_SESSION_VIEWS

// How long the chat view is held after the last live chat disappears before
// returning to RESTING (§2.1). A waiting chat pins the view past this timer.
#ifndef CHAT_LINGER_MS
#define CHAT_LINGER_MS (10u * 60u * 1000u)
#endif

// Geometry — the capability gate limits these views to 480×480-class panels
// (the S3 2.16 and the sim); other geometries need a layout pass before their
// flag can flip, so these are tuned constants, not breakpoints.
// Quota strip + vertical rhythm ported from PR #129's combined 5h/7d row
// (the visual language issue #135 credits): a 30px band at content_y with
// dim styrene_20 tags, styrene_24 percentages in a fixed right-aligned
// column, 10px bars, and a 14px(+4) gap down to the first card.
#define CHAT_ROW_H        30    // strip band height; text/bar vcentered in it
#define CHAT_ROW_BAR_H    10
#define CHAT_ROW_LBL_W    40    // "5h"/"7d" column
#define CHAT_ROW_PCT_W    72    // percentage column (right-aligned, fixed)
#define CHAT_ROW_COL_GAP  12    // label|bar|pct column gap
#define CHAT_ROW_HALF_GAP 20    // between the 5h and 7d halves
#define CHAT_ROW_GAP      14    // strip band ↓ card list (plus 4, per #129)
// Card height and gap are sized so that THREE WHOLE CARDS plus a sliver of the
// fourth fit the viewport: 3*104 + 2*4 = 320 of the 332 px between the quota
// strip and the bottom edge, leaving 12 px for the gap and the peek. Before
// this the pitch was 118 and the third card was cut 12 px short with its state
// line inside the fade band, while rows 4-6 were drawn nowhere at all.
#define CHAT_CARD_H       104
#define CHAT_CARD_GAP     4
#define CHAT_CARD_PITCH   (CHAT_CARD_H + CHAT_CARD_GAP)
#define CHAT_CARD_PAD_Y   8
// Chat cards (and the ONE-CHAT quota box) bleed to the physical left/right
// edges — the card's own corner radius is the relief at the glass edge. The
// inner side padding keeps text at the same 20px inset the old screen margin
// provided, clear of the panel's rounded corners.
#define CHAT_CARD_PAD_X   20
// Bottom fade: tapers the peeking next card into the panel edge. It is short
// on purpose — it must never reach the last WHOLE card's state line and timer,
// which is the row the user came to this tab to read — and it is hidden
// entirely when nothing is below the fold (chat_fade_update).
#define CHAT_FADE_H       16
// ONE-CHAT: two boxes — the 5h quota panel (exact RESTING "Current" panel)
// on top, the chat card below it.
#define FOCUS_CARD_H      176
#define FOCUS_PANEL_GAP   16    // 5h panel ↔ chat card

// One chat card's widget set. Cards keep stable identity: each card widget is
// bound to a chat (keyed by sid), not to a slot, so a reorder moves the widget
// instead of mutating every row's contents (§2.3).
struct ChatCard {
    lv_obj_t* card;
    lv_obj_t* lbl_name;
    lv_obj_t* lbl_ctx;      // ctx% top-right (list cards only; focus has the big pct)
    lv_obj_t* bar;          // context bar — hidden entirely when ctx is unknown
    lv_obj_t* dot;          // state indicator; pulses when waiting
    lv_obj_t* lbl_state;
    lv_obj_t* img_todo;
    lv_obj_t* lbl_todo;
    lv_obj_t* img_agents;
    lv_obj_t* lbl_agents;
    lv_obj_t* lbl_elapsed;
    const lv_font_t* name_font;  // font ON lbl_name now — the firmware-side
                                 // ellipsis measures with it, so it tracks the
                                 // anatomy (session name vs message sender)
    int  name_w;
    char sid[3];
    int  target_y;          // slide destination (list cards)
    bool used;
    bool waiting;
    bool claimed;           // per-update matching scratch
    // Is this card on the receding IDLE tier? Carried on the card because the
    // tier lives in the COLOURS now, not in an opacity layer (see card_col),
    // so every colour the card paints has to know about it.
    bool dim;
    // The card has two anatomies now — session and message (§ chat_card_apply
    // _kind). Cards are pooled by sid, so the same widget set can be a session
    // card one payload and a message card the next: the session geometry has
    // to be restorable, which means remembering what build_chat_card chose
    // rather than recomputing constants in two places.
    bool focus;                  // built as the ONE-CHAT card
    bool is_msg;                 // current anatomy
    const lv_font_t* name_font_base;  // session name font (restore target)
    const lv_font_t* line_font;  // session state/badge/timer font
    int  content_h;              // card height minus its own padding
    int  dot_sz;
    int  dot_dy;                 // session dot align offset (BOTTOM_LEFT)
    int  text_dy;                // session state-line align offset
    // What the card is showing, so a TAP knows what it means (card_tap_cb).
    // The host's row is gone by then -- nothing retains it past the update --
    // and the state is the whole predicate: NEEDS_YOU is the one card a tap
    // can ANSWER, everything else is a card a tap can only CLEAR.
    uint8_t  state;
    uint32_t sig;                // row_sig() of the row on screen; what a
                                 // dismissal remembers, so the same words stay
                                 // gone and new ones come back
    // The sig this card was ANSWERED at, or 0. A go-ahead does not remove the
    // card -- the agent has to actually move first, and the host will stop
    // sending the row when it does -- so between the press and that moment the
    // card has to say that its word is already on its way, or the owner
    // presses again and interrupts the agent twice. Cleared by the agent
    // saying anything new, because that changes the sig.
    uint32_t answered_sig;
};

static lv_obj_t* focus_group = nullptr;   // ONE-CHAT (§1.3)
static lv_obj_t* chats_group = nullptr;   // SEVERAL-CHATS (§1.4)
static lv_obj_t* empty_group = nullptr;   // "Nothing needs you" — the tab is
                                          // reachable at any time now, so it
                                          // needs something to say when calm
static lv_obj_t* cards_cont  = nullptr;   // scrolling viewport for the card list
static lv_obj_t* chat_fade   = nullptr;   // "more below the fold" gradient
static lv_obj_t* empty_lbl   = nullptr;   // what the empty tab says
static lv_obj_t* empty_hint  = nullptr;   // …and, when the link is up, why
static ChatCard  chat_cards[SESSION_MAX_ROWS];
static ChatCard  focus_card;
static lv_obj_t* focus_lbl_model = nullptr;
static lv_obj_t* focus_lbl_ctx   = nullptr;   // context % (left, on its own row)
static lv_obj_t* focus_lbl_tok   = nullptr;   // token counter (right of the % row)

// Full-size 5h quota panel on the ONE-CHAT view — same anatomy and weight as
// the RESTING "Current" panel. The 7d row is deliberately absent here: it
// moves on a scale of days, stays one glance away on RESTING / SEVERAL-CHATS,
// and a slim extra row would crowd the chat card against the status line.
static lv_obj_t* f5_pct, *f5_pill, *f5_bar, *f5_reset;

// One-line quota strip (chats view)
static lv_obj_t* cq_tag[2], *cq_bar[2], *cq_pct[2];

static lv_image_dsc_t icon_todo_dsc, icon_todo_small_dsc;
static lv_image_dsc_t icon_agents_dsc, icon_agents_small_dsc;

// Resolver inputs (§2.1), fed by ui_update_sessions()
static uint8_t  s_live_count    = 0;      // rows in the last received list
// The LEVEL: a session still needs a human. Messages are deliberately not part
// of it — see note_notify_set() — so the auto-return can hand the screen back
// after the dwell instead of parking the panel here for the message's life.
static bool     s_any_notify    = false;
// The notification edge is per SESSION, not global. "Something is waiting" is a
// level, and one parked permission prompt can hold it for an hour — during
// which a SECOND session hitting its own prompt would produce no edge and no
// notification, which is precisely the multi-session case this tab exists for.
// So the notify set is remembered by sid, and a jump fires for any sid that
// has just entered it — messages included (note_notify_set).
//
// A sid is only unique WITHIN its kind. Message sids are 2 hex chars of an md5
// of the message id; session sids are 2 hex chars of a different hash of a
// different id. Two independent namespaces, one 256-value key space: with 5
// rows on screen there is a ~2% chance per payload that a message collides
// with a session. Keyed on the string alone, that collision silently swallows
// the notification (the sid is "already in the set", so no rising edge, and
// the message expires unseen) — the worst failure mode a notifier has. So the
// kind travels with the sid, here and in the card pool.
static char     s_notify_sids[SESSION_MAX_ROWS][3];   // committed set
static bool     s_notify_kind[SESSION_MAX_ROWS];      // …is that sid a message?
static uint8_t  s_notify_n      = 0;
static char     s_notify_now[SESSION_MAX_ROWS][3];    // this payload's set
static bool     s_notify_now_kind[SESSION_MAX_ROWS];
static uint8_t  s_notify_now_n  = 0;
static bool     s_new_notify    = false;  // a sid entered the set this payload
static bool     s_new_notify_msg = false; // …and one of them is a message
static bool     s_new_notify_report = false; // …or an agent report needing you
static bool     s_focus_waiting = false;  // rows[0] waiting → drives the pulse
static bool     s_chats_linger  = false;  // holding a chat view after the last chat closed
static uint32_t s_chats_gone_ms = 0;
// Sessions-tab sub-view: 0 = empty, 1 = ONE-CHAT, 2 = SEVERAL-CHATS. These are
// the old view_state 3/4 renumbered now that they own a tab instead of sharing
// the usage screen's resolver.
static int      session_view    = -1;
static int      s_linger_view   = 1;
static UsageData s_usage_cache  = {};     // latest quota payload, for the mini bars

enum {
    SESSION_BUCKET_IDLE    = 0,
    SESSION_BUCKET_WORKING = 1,
    SESSION_BUCKET_WAITING = 2,
    // A message from another Claude session is not a session at all, so it
    // gets its own bucket rather than being filed under the closest lie. It
    // is deliberately NOT the waiting bucket: the accent + pulse mean "this
    // chat is blocked on you", and a message is something to read.
    SESSION_BUCKET_MESSAGE = 3,
};

// Membership is (sid, kind) — a message "79" and a session "79" are two
// different things that happen to hash the same (see s_notify_sids).
static bool sid_in_set(const char set[][3], const bool kind[], uint8_t n,
                       const char* sid, bool is_msg) {
    for (uint8_t i = 0; i < n; i++)
        if (kind[i] == is_msg && strcmp(set[i], sid) == 0) return true;
    return false;
}

// ---- Tap to clear: the dismissal store ----
// Every card on this tab is now something you TAP, and the tap has to make the
// card LEAVE. That is a harder promise than it looks, because the device does
// not own the list: the host re-sends the same rows every few seconds, so a
// card removed from the screen is back before the finger is off the glass
// unless something remembers the dismissal. This is that something.
//
// KEYED ON THE WORDS, NOT ON THE SID. A report sid is derived from the AGENT,
// so the same agent's next report reuses it; a fleet sid is derived from the
// session, so it survives every state that session passes through. Suppressing
// a sid outright would therefore silence an agent, or a session, for good --
// exactly the failure a notifier must not have. The key is a hash of what the
// card SAID (label + words + state), so dismissing "waiting: submit anchor-off
// arm?" hides that sentence and nothing else. The same agent saying something
// new is a new card, and it comes back.
//
// A RING, NOT A SET. Twelve entries, oldest overwritten. The bound is what
// makes it safe to keep on a C6: a store that only grew would be a slow leak
// fed by the host. Overflow costs a card coming back after twelve later
// dismissals, which is indistinguishable from the host re-sending it.
//
// The host is told too (BLE_EVENT_DISMISS), because only the host can make a
// dismissal outlive a reboot. This side does not wait for it and does not care
// whether it lands -- see the note in ble.h.
#define DISMISS_MAX 12

struct DismissedRow {
    uint32_t sig;
    bool     used;
};
static DismissedRow s_dismissed[DISMISS_MAX];
static uint8_t      s_dismiss_w = 0;   // next slot to overwrite

// FNV-1a over the fields a human actually reads. `state` is in it so a session
// that MOVES (waiting -> working -> waiting again) is a new card rather than a
// permanently silenced one, and `sid` is in it so two agents that happen to
// say the same sentence are still two cards.
static uint32_t row_sig(const SessionRow* r) {
    uint32_t h = 2166136261u;
    auto mix = [&h](const char* p, size_t cap) {
        for (size_t i = 0; i < cap && p[i]; i++) {
            h ^= (uint8_t)p[i];
            h *= 16777619u;
        }
        h ^= 0xffu;              // field terminator: "ab"+"c" != "a"+"bc"
        h *= 16777619u;
    };
    mix(r->sid, sizeof(r->sid));
    mix(r->label, sizeof(r->label));
    mix(r->msg, sizeof(r->msg));
    h ^= r->state;
    h *= 16777619u;
    return h;
}

static bool row_dismissed(const SessionRow* r) {
    const uint32_t sig = row_sig(r);
    for (const auto& d : s_dismissed)
        if (d.used && d.sig == sig) return true;
    return false;
}

static void remember_dismissed(uint32_t sig) {
    for (const auto& d : s_dismissed)
        if (d.used && d.sig == sig) return;      // already gone; don't burn a slot
    s_dismissed[s_dismiss_w].sig  = sig;
    s_dismissed[s_dismiss_w].used = true;
    s_dismiss_w = (uint8_t)((s_dismiss_w + 1) % DISMISS_MAX);
}

// The list the tab is actually SHOWING: the host's rows minus the dismissed
// ones. Kept because a tap has to re-render the list immediately -- waiting for
// the host's next payload would leave a hole where the card was for up to five
// seconds, and would not flip the tab to EMPTY (and so would not reveal the
// town hall button) until then.
static SessionList s_shown = {};

// Safe to call in place (out == in): the count is snapshotted first, and the
// write index never runs ahead of the read index.
static void filter_dismissed(const SessionList* in, SessionList* out) {
    const int n = in ? in->count : 0;
    int kept = 0;
    for (int i = 0; i < n && kept < SESSION_MAX_ROWS; i++) {
        if (row_dismissed(&in->rows[i])) continue;
        if (&out->rows[kept] != &in->rows[i]) out->rows[kept] = in->rows[i];
        kept++;
    }
    out->count = (uint8_t)kept;
}

// ---- The toast ----
// A tap on a card sends something over a radio to a machine somewhere else, and
// then the card disappears. Without a word from the device, "it went" and "it
// went nowhere" look identical -- and the failure case is common and boring
// (the daemon is not running, the link is down), not exotic. So every tap says
// what it did, for a second and a half, in a pill at the bottom of the tab.
//
// One widget, reused. It is a child of the tab container rather than of either
// sub-view, so it survives the view flipping to EMPTY underneath it -- which is
// exactly what happens when the tap clears the LAST card.
static lv_obj_t* toast_obj = nullptr;
static lv_obj_t* toast_lbl = nullptr;
static uint32_t  s_toast_until = 0;
#define TOAST_MS 1500

static void session_toast(const char* text, lv_color_t col) {
    if (!toast_obj) return;
    lv_label_set_text(toast_lbl, text);
    lv_obj_set_style_text_color(toast_lbl, col, 0);
    lv_obj_remove_flag(toast_obj, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(toast_obj);
    s_toast_until = lv_tick_get() + TOAST_MS;
}


// ---- The town hall button ----
// The tab's empty state stopped being a dead end. When every card is gone --
// answered, cleared, or never there -- the space they occupied holds the one
// control that puts cards back: press it and the host asks every reachable
// agent to report in (daemon/REPORT.md). It is the same round the hardware
// button fires, moved to where the owner is already looking when they want it.
//
// It lives ONLY on the empty view, and that is the design rather than a
// placement: with cards on screen there is nothing to call a meeting about --
// the meeting already happened, and its minutes are what you are reading.
static lv_obj_t* th_ring = nullptr;   // bezel; pulses while a round is out
static lv_obj_t* th_btn  = nullptr;   // the face
static lv_obj_t* th_lbl  = nullptr;   // the word on it
static lv_obj_t* th_cap  = nullptr;   // the line under it
static uint32_t  s_th_called_ms = 0;  // 0 = idle, else the tick of the press

// A round takes ~20 s end to end and costs real quota. This is not a rate
// limit -- the host has one of those, anchored on the attempt -- it is the
// button declining to look pressable while it is already working, which is the
// only honest thing the device can say about a round it cannot see.
#define TOWN_HALL_COOLDOWN_MS 30000

static void town_hall_tap_cb(lv_event_t* e);


// ---- The action bar ----
// Tapping a card does not DO anything. It SELECTS the card and offers what can
// be done with it, in a bar along the bottom of the tab:
//
//   [ GO AHEAD ]  [ WAIT ]     on a report that says an agent is waiting
//   [ DISMISS  ]  [ WAIT ]     on anything else
//
// The first cut acted on the tap itself, and it was wrong in a way worth
// writing down. A tap that sent the owner's "go ahead" left no way to be
// simply DONE with a card you had decided to answer yourself -- and answering
// it yourself is the normal case, because the panel is four inches wide and
// the session is on a keyboard somewhere. So the tap asks, and WAIT is a real
// answer: it sends nothing, clears nothing, and leaves the card exactly where
// it is.
//
// WHICH MEANS THE CARD OUTLIVES THE GESTURE. It goes when the AGENT moves and
// the host stops sending the row -- not when the owner has finished looking at
// it. That is the whole point of the tab: it mirrors what is true elsewhere,
// so a card that is still there means a session that still needs somebody.
static lv_obj_t* act_bar    = nullptr;
static lv_obj_t* act_do     = nullptr;   // GO AHEAD / DISMISS
static lv_obj_t* act_do_lbl = nullptr;
static lv_obj_t* act_wait   = nullptr;
static ChatCard* s_sel      = nullptr;   // the selected card, or none

#define ACT_BAR_H   64
#define ACT_BTN_H   48

static void card_select(ChatCard* c);
static void card_deselect(void);
static void act_do_cb(lv_event_t* e);
static void act_wait_cb(lv_event_t* e);
static void chat_card_mark_answered(ChatCard* c);

static void card_tap_cb(lv_event_t* e);

static int session_bucket(uint8_t state) {
    switch (state) {
    case SESSION_MESSAGE:
        return SESSION_BUCKET_MESSAGE;
    // An agent that says NEEDS-YOU or BLOCKED is a chat blocked on a human,
    // which is exactly what the waiting bucket means — so it gets the accent,
    // the pulse and the top of the list, and the auto-jump fires for it. That
    // is the entire point of giving reports their own states instead of
    // filing every reply under SESSION_MESSAGE. (What it does NOT get is the
    // waiting LEVEL that holds the panel on this tab; see note_notify_set.)
    case SESSION_REPORT_NEEDS_YOU:
    case SESSION_REPORT_BLOCKED:
        return SESSION_BUCKET_WAITING;
    // Working and done both need nothing from the owner. Done is separated by
    // its dot colour, not by a bucket: dimming it would put the rarer, more
    // informative answer behind the expected one.
    case SESSION_REPORT_WORKING:
    case SESSION_REPORT_DONE:
        return SESSION_BUCKET_WORKING;
    // The overflow footnote recedes like an idle chat: the rows it is a
    // footnote to are the ones worth looking at. The staleness marker
    // recedes for the opposite reason — there is nothing above it to look
    // at, and shouting would say "an agent needs you" when the truth is the
    // quieter "the host stopped being able to tell".
    case SESSION_REPORT_MORE:
    case SESSION_HOST_STALE:
        return SESSION_BUCKET_IDLE;
    default: break;
    }
    if (state >= SESSION_WAITING_PERMISSION && state <= SESSION_ERROR)
        return SESSION_BUCKET_WAITING;
    if (state >= SESSION_THINKING && state <= SESSION_COMPACTING)
        return SESSION_BUCKET_WORKING;
    if (state <= SESSION_IDLE)
        return SESSION_BUCKET_IDLE;
    return SESSION_BUCKET_WORKING;  // unknown future codes render neutral, never alarming
}

// Message cards and report cards share one anatomy (see chat_card_apply_kind)
// and one sid namespace in the card pool; sessions are the other kind.
static bool session_is_message(const SessionRow* r) {
    return session_state_has_words(r->state);
}

// The word that makes a report card readable as a report at a glance. Drawn
// in the state's own colour on the sender line, where a session card puts
// nothing — so "has a chip" is what separates a report from plain mail, and
// the chip's colour and text separate the four reports from each other.
// Empty means no chip: an ordinary message, and the overflow row, whose own
// label already carries its count.
static const char* session_report_chip(uint8_t state) {
    switch (state) {
    case SESSION_REPORT_WORKING:   return "working";
    case SESSION_REPORT_NEEDS_YOU: return "needs you";
    case SESSION_REPORT_BLOCKED:   return "blocked";
    case SESSION_REPORT_DONE:      return "done";
    default:                       return "";
    }
}

// The indicator colour for a card that has words. Reports borrow the session
// card's own vocabulary rather than inventing one: accent = a human is
// needed, COL_TEXT = running, dim = nothing here. Purple stays what it has
// always been — another Claude talking to you — and green is the palette's
// "finished" colour, already the usage bar's.
static lv_color_t session_words_color(uint8_t state) {
    switch (state) {
    case SESSION_REPORT_NEEDS_YOU:
    case SESSION_REPORT_BLOCKED:   return COL_ACCENT;
    case SESSION_REPORT_WORKING:   return COL_TEXT;
    case SESSION_REPORT_DONE:      return COL_GREEN;
    case SESSION_REPORT_MORE:
    case SESSION_HOST_STALE:       return COL_DIM;
    default:                       return COL_PURPLE;   // SESSION_MESSAGE
    }
}

// The chip's own colour, which is NOT always the dot's. A session card draws
// its indicator in COL_TEXT while it works and its state line in COL_DIM —
// the dot says "alive", the words stay quiet — and the chip is words. So
// "working" recedes to dim while its dot stays bright, and only the two
// states that want your eye (accent) and the one that is news (green) are
// allowed to be louder than the sender beside them.
static lv_color_t session_chip_color(uint8_t state) {
    switch (state) {
    case SESSION_REPORT_NEEDS_YOU:
    case SESSION_REPORT_BLOCKED:   return COL_ACCENT;
    case SESSION_REPORT_DONE:      return COL_GREEN;
    default:                       return COL_DIM;
    }
}

static const char* const session_tool_names[] = {
    "tool", "Bash", "Read", "Edit", "Write",
    "Grep", "Glob", "Task", "WebFetch", "WebSearch",
};

static void session_state_text(const SessionRow* r, char* buf, size_t n) {
    switch (r->state) {
    // A message row's "state line" is the message itself — the one place on
    // the card where a session says what it is doing is where a message says
    // what it says. Body text arrives host-folded and head-elided; what it is
    // folded TO is ASCII 32..126 plus the KS X 1001 Hangul the message body's
    // fallback font carries, so this string is UTF-8, not ASCII (see
    // MSG_BODY_FONT and docs/fonts.md). Empty means a host that sends state 11
    // without index 13: say so rather than draw a blank line.
    case SESSION_MESSAGE:
    // A report's summary lives in the same field and on the same line, for
    // the same reason: where a session says what it is doing, a report says
    // what its agent said it is doing. The difference is the chip beside the
    // sender, not the words.
    case SESSION_REPORT_WORKING:
    case SESSION_REPORT_NEEDS_YOU:
    case SESSION_REPORT_BLOCKED:
    case SESSION_REPORT_DONE:
    case SESSION_REPORT_MORE:
    // And so does the staleness marker: the host wrote a sentence saying why
    // its listing is blind, and this is the line that sentence goes on.
    case SESSION_HOST_STALE:
        snprintf(buf, n, "%s", r->msg[0] ? r->msg : "(no message text)");
        return;
    case SESSION_STARTING:   snprintf(buf, n, "starting");   return;
    case SESSION_IDLE:       snprintf(buf, n, "idle");       return;
    case SESSION_THINKING:   snprintf(buf, n, "thinking");   return;
    case SESSION_RESPONDING: snprintf(buf, n, "responding"); return;
    case SESSION_RUNNING_TOOL:
        if (r->ntools > 1) snprintf(buf, n, "%d tools", r->ntools);
        else snprintf(buf, n, "running %s",
                      session_tool_names[r->tool <= SESSION_TOOL_WEBSEARCH ? r->tool : 0]);
        return;
    case SESSION_COMPACTING:         snprintf(buf, n, "compacting");       return;
    case SESSION_WAITING_PERMISSION:
        // Name the tool when the host knows it: "needs permission" tells you
        // to go look, "allow Bash?" tells you what you are being asked. Tool
        // code 0 is the generic "other/none", so fall back rather than
        // print "allow tool?". Happens to be shorter than the old string for
        // every tool name but WebSearch.
        if (r->tool != SESSION_TOOL_NONE && r->tool <= SESSION_TOOL_WEBSEARCH)
            snprintf(buf, n, "allow %s?", session_tool_names[r->tool]);
        else
            snprintf(buf, n, "needs permission");
        return;
    case SESSION_WAITING_QUESTION:   snprintf(buf, n, "asking you");       return;
    case SESSION_WAITING_INPUT:      snprintf(buf, n, "needs input");      return;
    case SESSION_ERROR:              snprintf(buf, n, "error");            return;
    default:                         snprintf(buf, n, "busy");             return;
    }
}

// Render elapsed_s as-received: 8s / 40s / 1m / 4m / 2h / 3d.
static void session_elapsed_text(int32_t s, char* buf, size_t n) {
    if (s < 0) s = 0;
    if (s < 60)          snprintf(buf, n, "%ds", (int)s);
    else if (s < 3600)   snprintf(buf, n, "%dm", (int)(s / 60));
    else if (s < 86400)  snprintf(buf, n, "%dh", (int)(s / 3600));
    else                 snprintf(buf, n, "%dd", (int)(s / 86400));
}

// Render a token count (1k units): 412 → "412K", 1000 → "1.0M", 1234 → "1.2M".
static void session_tok_text(int32_t tok_k, char* buf, size_t n) {
    if (tok_k < 1000) snprintf(buf, n, "%dK", (int)tok_k);
    else              snprintf(buf, n, "%d.%dM", (int)(tok_k / 1000),
                               (int)((tok_k % 1000) / 100));
}

// Back a byte count up to a UTF-8 character boundary. Both elide helpers
// below shrink a byte count until the text fits and then cut there, which was
// safe while the wire was ASCII 32..126. Korean is three bytes per character,
// so an unaligned cut hands LVGL a dangling continuation byte — it decodes as
// U+FFFD-ish garbage and draws a run of placeholder boxes *after* the text
// that did fit, i.e. the elide makes the message less readable, not more.
static size_t utf8_floor(const char* s, size_t n) {
    while (n > 0 && ((unsigned char)s[n] & 0xC0) == 0x80) n--;
    return n;
}

// Ellipsize in firmware: measure, then middle-elide with "..." (three dots),
// keeping the label's trailing characters — that tail is the host's session
// discriminator ("clawdmeter-36" vs "clawdmeter-2c" must stay distinct, §5).
// LVGL's LONG_DOT places its dots via the label's line-box math, which parks
// them on the (hidden) wrapped second line when the box is exactly one line
// tall — so the truncation is done deterministically here instead.
static void label_set_ellipsized(lv_obj_t* lbl, const char* txt,
                                 const lv_font_t* font, int max_w) {
    lv_point_t sz;
    lv_text_get_size(&sz, txt, font, 0, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
    if (sz.x <= max_w) {
        set_label_if_changed(lbl, txt);
        return;
    }
    char buf[SESSION_LABEL_MAX + 4];
    size_t len = strlen(txt);
    if (len >= SESSION_LABEL_MAX) len = utf8_floor(txt, SESSION_LABEL_MAX - 1);
    // The tail is 4 bytes of the ORIGINAL string, so it has to start on a
    // character boundary too — grow it to the next one (at most 6 bytes,
    // which `buf` still holds) rather than splitting a syllable.
    size_t tail = len > 8 ? 4 : 0;
    if (tail) tail = len - utf8_floor(txt, len - tail);
    size_t head = len - tail;
    while (head > 0) {
        head = utf8_floor(txt, head);
        memcpy(buf, txt, head);
        buf[head] = '\0';
        strcat(buf, "...");
        memcpy(buf + head + 3, txt + len - tail, tail);
        buf[head + 3 + tail] = '\0';
        lv_text_get_size(&sz, buf, font, 0, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
        if (sz.x <= max_w) break;
        head--;
    }
    set_label_if_changed(lbl, buf);
}

// The message-body sibling of label_set_ellipsized: same reason (LVGL's
// LONG_DOT places its dots by line-box math and drops them outside a
// tight box — on a two-line message body it truncated mid-word with no
// ellipsis at all), same technique (measure, shrink, re-measure), different
// rule at the ends. A name is elided in the MIDDLE to keep the tail that
// tells two sessions apart; a message is elided at the TAIL, because a
// message's information is front-loaded — which is also how the host elides
// it before it ever reaches the wire.
//
// Wraps to `max_lines` at `max_w` and returns the number of lines actually
// used, so the caller can center the block instead of leaving a hole where
// the third line would have been.
static int label_set_clamped(lv_obj_t* lbl, const char* txt,
                             const lv_font_t* font, int max_w, int max_lines) {
    const int32_t ls = lv_obj_get_style_text_letter_space(lbl, LV_PART_MAIN);
    const int32_t lsp = lv_obj_get_style_text_line_space(lbl, LV_PART_MAIN);
    const int line_h = lv_font_get_line_height(font);
    const int max_h = max_lines * line_h + (max_lines - 1) * lsp;

    lv_point_t sz;
    lv_text_get_size(&sz, txt, font, ls, lsp, max_w, LV_TEXT_FLAG_NONE);
    if (sz.y <= max_h) {
        set_label_if_changed(lbl, txt);
        return sz.y > line_h ? (sz.y + lsp) / (line_h + lsp) : 1;
    }

    char buf[SESSION_MSG_MAX + 4];
    size_t len = strlen(txt);
    if (len >= SESSION_MSG_MAX) len = SESSION_MSG_MAX - 1;
    len = utf8_floor(txt, len);
    while (len > 0) {
        len--;
        // Don't leave the ellipsis floating after a space ("both  ...").
        while (len > 0 && txt[len - 1] == ' ') len--;
        len = utf8_floor(txt, len);
        memcpy(buf, txt, len);
        strcpy(buf + len, "...");
        lv_text_get_size(&sz, buf, font, ls, lsp, max_w, LV_TEXT_FLAG_NONE);
        if (sz.y <= max_h) break;
    }
    set_label_if_changed(lbl, buf);
    return sz.y > line_h ? (sz.y + lsp) / (line_h + lsp) : 1;
}

// ---- The pulse (§2.3) ----
// One module-level animation drives a shared value every waiting indicator
// reads, so two chats waiting at once pulse in unison. Only waiting states
// pulse; the text never does.
static int32_t pulse_val = (int32_t)LV_OPA_COVER;

// On a session card the pulsing text is the STATE LINE — three words that
// stay legible at 30% because you already know what they say. On a card with
// words the same label is a whole sentence you have not read yet, and fading
// it in and out is a card you cannot finish reading. So the words hold at
// full brightness and the chip beside the sender pulses instead: same phase,
// same accent, same "act on this", one thing you can actually read.
static void pulse_card(ChatCard* c, int32_t v) {
    lv_obj_set_style_bg_opa(c->dot, (lv_opa_t)v, 0);
    lv_obj_set_style_text_opa(c->is_msg ? c->lbl_agents : c->lbl_state,
                              (lv_opa_t)v, 0);
}

static void pulse_exec_cb(void* var, int32_t v) {
    (void)var;
    pulse_val = v;
    // Indicator and state text pulse together, in one shared phase.
    for (auto& c : chat_cards)
        if (c.used && c.waiting) pulse_card(&c, v);
    if (s_focus_waiting && focus_card.dot) pulse_card(&focus_card, v);
}

// ---- Card motion callbacks (§2.3) ----
static void card_y_anim_cb(void* obj, int32_t v)   { lv_obj_set_y((lv_obj_t*)obj, v); }
// Plain opa, never opa_layered: the recursive style lookup in the draw path
// fades the whole subtree without allocating a composite buffer (§7).
static void card_opa_anim_cb(void* obj, int32_t v) { lv_obj_set_style_opa((lv_obj_t*)obj, (lv_opa_t)v, 0); }
static void card_fadeout_done_cb(lv_anim_t* a)     { lv_obj_add_flag((lv_obj_t*)a->var, LV_OBJ_FLAG_HIDDEN); }

// ---- The idle tier, without an opacity layer (§3) ----
// An idle card recedes to 60%. That used to be plain `opa` on the CARD, which
// LVGL resolves recursively down the draw path — so the panel fill, every
// glyph, the context bar and both icons were each ALPHA-BLENDED against what
// was behind them, on every strip of every frame the list scrolls. Measured on
// the C6: 8.5 ms of a 73 ms render, for pixels that are reachable with no
// blending at all.
//
// Drawing colour X at opacity a over background B lands on the same pixel as
// drawing lv_color_mix(X, B, a) opaque — and it still holds at the antialiased
// edge of a glyph, where coverage c gives a·c·X + (1-a·c)·B either way. So the
// tier is baked into the COLOURS: identical output, plain fills.
//
// What it costs is that a colour can no longer be written straight onto a card
// widget — it goes through card_col() — and that the build-time defaults have
// to be re-applied whenever a card crosses the tier boundary, which is
// chat_card_refresh_tier()'s whole job.
#define CARD_DIM_OPA LV_OPA_60

static bool session_tier_is_dim(uint8_t state) {
    return session_bucket(state) == SESSION_BUCKET_IDLE;
}

// The card's own fill, pre-blended against the screen behind it.
static lv_color_t card_bg_col(const ChatCard* c) {
    return c->dim ? lv_color_mix(COL_PANEL, COL_BG, CARD_DIM_OPA) : COL_PANEL;
}

// Anything drawn ON the card blends against that already-dimmed fill.
static lv_color_t card_col(const ChatCard* c, lv_color_t col) {
    return c->dim ? lv_color_mix(col, card_bg_col(c), CARD_DIM_OPA) : col;
}

// Re-state every colour the card was BUILT with, at the current tier. Only the
// ones that change per update (the dot, the state line, a message's sender and
// report chip) go through card_col() where they are set; these are the ones
// build_chat_card wrote once and nothing has touched since, so crossing the
// tier boundary is the only moment they are wrong.
static void chat_card_refresh_tier(ChatCard* c) {
    if (!c->card) return;
    lv_obj_set_style_bg_color(c->card, card_bg_col(c), 0);
    lv_obj_set_style_text_color(c->lbl_name,
        card_col(c, c->is_msg ? COL_PURPLE : COL_TEXT), 0);
    if (c->lbl_ctx) lv_obj_set_style_text_color(c->lbl_ctx, card_col(c, COL_TEXT), 0);
    lv_obj_set_style_bg_color(c->bar, card_col(c, COL_BAR_BG), LV_PART_MAIN);
    lv_obj_set_style_bg_color(c->bar, card_col(c, COL_DIM), LV_PART_INDICATOR);
    lv_obj_set_style_image_recolor(c->img_todo,   card_col(c, COL_ACCENT), 0);
    lv_obj_set_style_image_recolor(c->img_agents, card_col(c, COL_PURPLE), 0);
    lv_obj_set_style_text_color(c->lbl_todo,    card_col(c, COL_ACCENT), 0);
    lv_obj_set_style_text_color(c->lbl_elapsed, card_col(c, COL_DIM), 0);
    // On a message card lbl_agents is the report chip and carries the state's
    // own colour, set per update; on a session card it is the subagent count.
    if (!c->is_msg)
        lv_obj_set_style_text_color(c->lbl_agents, card_col(c, COL_PURPLE), 0);
    if (c->focus) {
        // ONE-CHAT extras — they hang off focus_card.card, so they dim with it.
        if (focus_lbl_model) {
            lv_obj_set_style_text_color(focus_lbl_model, card_col(c, COL_TEXT), 0);
            lv_obj_set_style_bg_color(focus_lbl_model, card_col(c, COL_BAR_BG), 0);
        }
        if (focus_lbl_ctx) lv_obj_set_style_text_color(focus_lbl_ctx, card_col(c, COL_TEXT), 0);
        if (focus_lbl_tok) lv_obj_set_style_text_color(focus_lbl_tok, card_col(c, COL_TEXT), 0);
    }
}

// ---- Card construction ----

static lv_obj_t* make_badge_icon(lv_obj_t* parent, const lv_image_dsc_t* dsc, lv_color_t col) {
    lv_obj_t* img = lv_image_create(parent);
    lv_image_set_src(img, dsc);
    // Runtime recolor of the white-tinted RGB565A8 glyph — the badge color
    // rides on the same draw path as the labels, so the idle card's 60%
    // dimming still applies. Position comes from layout_badge_cluster().
    lv_obj_set_style_image_recolor(img, col, 0);
    lv_obj_set_style_image_recolor_opa(img, LV_OPA_COVER, 0);
    lv_obj_add_flag(img, LV_OBJ_FLAG_HIDDEN);
    return img;
}

static lv_obj_t* make_card_label(lv_obj_t* parent, const lv_font_t* font, lv_color_t col) {
    lv_obj_t* lbl = lv_label_create(parent);
    lv_label_set_text(lbl, "");
    lv_obj_set_style_text_font(lbl, font, 0);
    lv_obj_set_style_text_color(lbl, col, 0);
    return lbl;
}

// Build one chat card. Both anatomies are §1.1's three lines — name row,
// context bar, state line — the focus variant is just bigger and swaps the
// top-right ctx% for the model name + a big percentage.
static void build_chat_card(ChatCard* c, lv_obj_t* parent, int x, int y, bool focus) {
    const lv_font_t* f_name = focus ? &font_styrene_48 : &font_styrene_28;
    const lv_font_t* f_line = focus ? &font_styrene_24 : &font_styrene_24;
    const int h = focus ? FOCUS_CARD_H : CHAT_CARD_H;

    c->card = make_panel(parent, x, y, L.scr_w, h);   // full-bleed (see CHAT_CARD_PAD_X)
    lv_obj_set_style_pad_left(c->card, CHAT_CARD_PAD_X, 0);
    lv_obj_set_style_pad_right(c->card, CHAT_CARD_PAD_X, 0);
    if (!focus) {
        lv_obj_set_style_pad_top(c->card, CHAT_CARD_PAD_Y, 0);
        lv_obj_set_style_pad_bottom(c->card, CHAT_CARD_PAD_Y, 0);
    }

    // The card is the tap target (card_tap_cb). Bound once, at build, to the
    // pooled ChatCard rather than to a row: the pool keeps a widget with its
    // chat across reorders, so this pointer stays right for the widget's life
    // and the handler reads the CURRENT state off it.
    //
    // A pressed tint is the only affordance this tab has room for -- there is
    // no chevron, no button, nothing that says "touch me" on a card that
    // otherwise reads as a status line. It is deliberately faint: the cards
    // are also something you just read.
    lv_obj_add_event_cb(c->card, card_tap_cb, LV_EVENT_CLICKED, c);
    lv_obj_set_style_bg_color(c->card, lv_color_hex(0x2e2e2c), LV_STATE_PRESSED);

    const int cw = L.scr_w - 2 * CHAT_CARD_PAD_X;

    // Name width starts at the full row; every content update re-budgets it
    // against the measured width of the actual right-side neighbor (the model
    // pill on the focus card, the token label on list cards) so a long name
    // ellipsizes right up to its neighbor instead of a worst-case gap.
    c->name_font = f_name;
    c->name_w = cw;
    c->lbl_name = make_card_label(c->card, f_name, COL_TEXT);
    lv_obj_set_width(c->lbl_name, cw);
    // One line, exactly — the ellipsis itself is applied firmware-side (see
    // label_set_ellipsized); the fixed box just guards against any wrap.
    lv_obj_set_height(c->lbl_name, lv_font_get_line_height(f_name));
    lv_obj_align(c->lbl_name, LV_ALIGN_TOP_LEFT, 0, 0);

    if (!focus) {
        c->lbl_ctx = make_card_label(c->card, f_name, COL_TEXT);
        lv_obj_align(c->lbl_ctx, LV_ALIGN_TOP_RIGHT, 0, 0);
    } else {
        c->lbl_ctx = nullptr;
    }

    c->bar = make_bar(c->card, 0, 0, cw, focus ? 12 : 8);
    lv_obj_set_style_bg_color(c->bar, COL_DIM, LV_PART_INDICATOR);  // context stays neutral (§1.3)
    lv_obj_align(c->bar, LV_ALIGN_BOTTOM_LEFT, 0, focus ? -40 : -38);

    // State line — everything shares one visual center line, `line_c` px above
    // the card content's bottom edge. The dot sits flush with the card's left
    // content edge (same x as the name and the bar above it).
    const int dot_sz  = focus ? 14 : 14;
    const int line_h  = lv_font_get_line_height(f_line);
    const int line_dy = focus ? -2 : 0;   // base line, from content bottom
    // Dot center = label line-box center (measured: Styrene's lowercase
    // x-height band centers on its line box). On list cards the text rides
    // 1 px lower than the box math — user-tuned against hardware.
    const int dot_dy  = line_dy - (line_h - dot_sz) / 2;
    const int text_dy = focus ? line_dy : line_dy + 1;

    c->dot = lv_obj_create(c->card);
    lv_obj_set_size(c->dot, dot_sz, dot_sz);
    lv_obj_set_style_radius(c->dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(c->dot, COL_DIM, 0);
    lv_obj_set_style_bg_opa(c->dot, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(c->dot, 0, 0);
    lv_obj_set_style_pad_all(c->dot, 0, 0);
    lv_obj_add_flag(c->dot, LV_OBJ_FLAG_EVENT_BUBBLE);
    lv_obj_align(c->dot, LV_ALIGN_BOTTOM_LEFT, 0, dot_dy);
    c->lbl_state = make_card_label(c->card, f_line, COL_DIM);
    lv_label_set_long_mode(c->lbl_state, LV_LABEL_LONG_DOT);  // state ellipsizes before a badge drops (§5)
    lv_obj_set_width(c->lbl_state, focus ? 200 : 200);
    // One text line, exactly: with a free-growing height an over-long state
    // would wrap to a second line instead of taking the DOT ellipsis.
    lv_obj_set_height(c->lbl_state, line_h);
    lv_obj_align(c->lbl_state, LV_ALIGN_BOTTOM_LEFT, dot_sz + 8, text_dy);

    // Badges + timer form a right-aligned cluster (timer rightmost); their x
    // positions are recomputed per update in layout_badge_cluster().
    // Badge colors: todo = terra-cotta accent, subagents = the palette's
    // muted purple; icon and count share the color so each badge reads as
    // one unit.
    c->img_todo = make_badge_icon(c->card, focus ? &icon_todo_dsc : &icon_todo_small_dsc,
                                  COL_ACCENT);
    c->lbl_todo = make_card_label(c->card, f_line, COL_ACCENT);
    c->img_agents = make_badge_icon(c->card, focus ? &icon_agents_dsc : &icon_agents_small_dsc,
                                    COL_PURPLE);
    c->lbl_agents = make_card_label(c->card, f_line, COL_PURPLE);

    c->lbl_elapsed = make_card_label(c->card, f_line, COL_DIM);
    lv_obj_align(c->lbl_elapsed, LV_ALIGN_BOTTOM_RIGHT, 0, text_dy);

    c->sid[0] = 0;
    c->target_y = -1;
    c->used = c->waiting = c->claimed = false;
    c->dim  = false;   // built at full strength; chat_card_set_row decides the tier
    // Remembered so chat_card_apply_kind can put the session anatomy back
    // exactly as it is here after a message card has borrowed the widgets.
    c->focus     = focus;
    c->is_msg    = false;
    c->name_font_base = f_name;
    c->line_font = f_line;
    // The two card types pad differently (list cards override the panel's
    // pad_y); read it once instead of re-deriving the constants downstream.
    lv_obj_update_layout(c->card);
    c->content_h = lv_obj_get_content_height(c->card);
    c->dot_sz    = dot_sz;
    c->dot_dy    = dot_dy;
    c->text_dy   = text_dy;
}

// ---- Korean in the message body ----
// The brand faces are generated over ASCII 32..126, so Hangul in a message
// used to draw as nothing at all, and the host romanised Korean before it
// reached the wire. font_nanum_kr_28 is NanumGothic (SIL OFL) over the 2,350
// KS X 1001 syllables at 28 px / 1 bpp (plus the nine punctuation marks a
// Korean IME emits that ASCII has no twin for); LVGL 9 resolves a codepoint
// the primary font lacks through lv_font_t::fallback, so ASCII still comes
// out of Styrene and only Hangul falls through.
//
// The host half ships with it: clawdmeter_inbox passes a body's Hangul
// through instead of romanising it, but ONLY the body — see MSG_FROM_FONT
// below. A syllable outside those 2,350 keeps romanising, because it is not
// in this font either. See docs/fonts.md.
LV_FONT_DECLARE(font_nanum_kr_28);

// A runtime COPY of font_styrene_28 rather than an edit to
// font_styrene_28.c's `.fallback`: the generated fonts are `const` (.rodata,
// i.e. memory-mapped flash on ESP32 — a const_cast store there faults), the
// generated files are meant to be regenerable, and font_styrene_28 is shared
// with the usage screen and the splash, which have no Korean to render.
// Costs one lv_font_t (~40 B of .bss). Composed lazily so it cannot depend on
// ui_init() having run first.
static const lv_font_t* msg_body_font(void) {
    static lv_font_t kr;                    // font_styrene_28 + Hangul fallback
    if (!kr.get_glyph_dsc) {
        kr = font_styrene_28;
        kr.fallback = &font_nanum_kr_28;
    }
    return &kr;
}

// ---- The two card anatomies ----
// Message-card metrics. The hierarchy inverts against the session card: there
// the identity is the headline and the status is the footnote, here the words
// are the point. So the sender drops to a metadata line (the focus card's 48px
// name font would shout the wrong word) and the BODY is the largest, brightest
// text on the card — COL_TEXT at full opacity where a state line is COL_DIM.
//
// Sizes, plainly, because "the size the name gets" would not be true: the
// body is styrene_28 on BOTH card kinds — the session card's name size — over
// a styrene_24 / styrene_20 sender. One fixed size is not a stylistic choice:
// there is exactly one Hangul face and it is 28 px, and 28 px Hangul (ink 24
// px above the baseline) does not fit a styrene_24 line box (20 px of ascent)
// without colliding with the line above it. A second Hangul face at 24 px is
// a measured 150,729 B of flash for a 4 px difference.
// It costs the list card nothing: 88 px of content less a 21 px sender row
// and the 4 px gap leaves 63 px, which is still two 30 px lines.
//
// MSG_FROM_FONT gets NO Hangul fallback, and that is deliberate rather than
// an omission: a Korean sender would need faces at 20 and 24 px, and the
// session card's own name font would need 48 as well — 785,424 B measured,
// four times the font already here, for a machine name. The host romanises
// those fields instead (clawdmeter_inbox.panel_sender /
// clawdmeter_sessions.panel_label). See docs/fonts.md § "Fallback fonts".
#define MSG_FROM_FONT(c) ((c)->focus ? &font_styrene_24 : &font_styrene_20)
#define MSG_BODY_FONT(c) msg_body_font()
#define MSG_DOT_SZ   10    // a marker on a metadata line, not a status dot
#define MSG_GUTTER   18    // dot column; the body hangs under the sender text
#define MSG_ROW_GAP  4     // sender row ↓ body
#define MSG_MAX_LINES 3    // past three the card is a wall of text
#define MSG_CHIP_GAP  14   // report chip ↔ age, on the sender row

// A message is not a session with holes in it, so it does not borrow the
// session composition and hide four of its five parts. It restacks the same
// widgets into a notification: a small sender line with the indicator in a
// left gutter and the age on the right, and under it the message itself,
// wrapped over as many lines as the card has room for and given the full
// brightness the session card reserves for the chat's NAME (see the MSG_*
// block above for what that does and does not mean about size).
//
// Called from chat_card_set_row and cheap: it re-lays out only when the kind
// actually changes, which for a pooled card is when it is first claimed by a
// message sid (or claimed back by a session sid after the message expires).
static void chat_card_apply_kind(ChatCard* c, bool msg) {
    if (c->is_msg == msg) return;
    c->is_msg = msg;

    const int cw = L.scr_w - 2 * CHAT_CARD_PAD_X;
    const int line_h = lv_font_get_line_height(c->line_font);

    if (!msg) {
        // Back to the session anatomy build_chat_card laid down.
        c->name_font = c->name_font_base;
        lv_obj_set_style_text_font(c->lbl_name, c->name_font, 0);
        lv_obj_set_style_text_color(c->lbl_name, card_col(c, COL_TEXT), 0);
        lv_obj_set_height(c->lbl_name, lv_font_get_line_height(c->name_font));
        lv_obj_align(c->lbl_name, LV_ALIGN_TOP_LEFT, 0, 0);

        lv_obj_set_size(c->dot, c->dot_sz, c->dot_sz);
        lv_obj_align(c->dot, LV_ALIGN_BOTTOM_LEFT, 0, c->dot_dy);

        lv_obj_set_style_text_font(c->lbl_state, c->line_font, 0);
        lv_obj_set_width(c->lbl_state, 200);
        lv_obj_set_height(c->lbl_state, line_h);
        lv_obj_align(c->lbl_state, LV_ALIGN_BOTTOM_LEFT, c->dot_sz + 8, c->text_dy);

        lv_obj_set_style_text_font(c->lbl_elapsed, c->line_font, 0);
        lv_obj_align(c->lbl_elapsed, LV_ALIGN_BOTTOM_RIGHT, 0, c->text_dy);

        // lbl_agents was a report chip: put the subagent-count badge back
        // whole. Font, colour AND opacity — a card left mid-pulse would
        // otherwise show a permanently half-faded count (layout_badge_cluster
        // repositions it, so only the styling has to be undone here).
        lv_obj_set_style_text_font(c->lbl_agents, c->line_font, 0);
        lv_obj_set_style_text_color(c->lbl_agents, card_col(c, COL_PURPLE), 0);
        lv_obj_set_style_text_opa(c->lbl_agents, LV_OPA_COVER, 0);
        return;
    }

    // Message anatomy. The sender is metadata one step under the line font
    // (the focus card's 48px name would shout the wrong word); the body is
    // the card's largest text. Fonts and colors only — the
    // vertical placement depends on how many lines the body actually needs,
    // so it is done per update in msg_card_layout().
    c->name_font = MSG_FROM_FONT(c);
    lv_obj_set_style_text_font(c->lbl_name, c->name_font, 0);
    lv_obj_set_style_text_color(c->lbl_name, card_col(c, COL_PURPLE), 0);
    lv_obj_set_height(c->lbl_name, lv_font_get_line_height(c->name_font));

    lv_obj_set_size(c->dot, MSG_DOT_SZ, MSG_DOT_SZ);

    lv_obj_set_style_text_font(c->lbl_state, MSG_BODY_FONT(c), 0);
    lv_obj_set_width(c->lbl_state, cw - MSG_GUTTER);

    // The age rides on the sender line at the sender's size: a message's
    // "elapsed" is how long ago it arrived, which belongs with the sender,
    // not in the badge cluster the card no longer has.
    lv_obj_set_style_text_font(c->lbl_elapsed, c->name_font, 0);

    // lbl_agents is the subagent COUNT on a session card and the report CHIP
    // here — the same widget restacked, exactly as the other four are. It
    // borrows the sender's font and sits on the sender's row; its icon
    // (img_agents) stays hidden, since a word is its own badge.
    lv_obj_set_style_text_font(c->lbl_agents, c->name_font, 0);
}

// How many body lines this card has room for under its sender row. Derived,
// not tabulated: the list card and the ONE-CHAT card differ in height, in
// padding and in both fonts, and a future panel geometry will differ again.
static int msg_max_lines(ChatCard* c) {
    const int from_h = lv_font_get_line_height(c->name_font);
    const int body_h = lv_font_get_line_height(MSG_BODY_FONT(c));
    const int lsp    = lv_obj_get_style_text_line_space(c->lbl_state, LV_PART_MAIN);
    const int avail  = c->content_h - from_h - MSG_ROW_GAP;
    int n = (avail + lsp) / (body_h + lsp);
    if (n < 1) n = 1;
    if (n > MSG_MAX_LINES) n = MSG_MAX_LINES;
    return n;
}

// Place the message block. The sender row and the body are one unit, centered
// in the card: a two-line message in a card sized for three would otherwise
// leave a hole at the bottom exactly where a session card puts its state line,
// which is the "session card with parts missing" look this anatomy exists to
// avoid. Runs per update because `used_lines` is a property of the text.
static void msg_card_layout(ChatCard* c, int used_lines) {
    const lv_font_t* f_from = c->name_font;
    const lv_font_t* f_body = MSG_BODY_FONT(c);
    const int from_h = lv_font_get_line_height(f_from);
    const int body_h = lv_font_get_line_height(f_body);
    const int lsp    = lv_obj_get_style_text_line_space(c->lbl_state, LV_PART_MAIN);
    const int body_block = used_lines * body_h + (used_lines - 1) * lsp;

    lv_obj_set_height(c->lbl_state, body_block);

    int top = (c->content_h - (from_h + MSG_ROW_GAP + body_block)) / 2;
    if (top < 0) top = 0;

    lv_obj_align(c->lbl_name,    LV_ALIGN_TOP_LEFT,  MSG_GUTTER, top);
    lv_obj_align(c->lbl_elapsed, LV_ALIGN_TOP_RIGHT, 0,          top);
    lv_obj_align(c->dot,         LV_ALIGN_TOP_LEFT,  0,
                 top + (from_h - MSG_DOT_SZ) / 2);
    lv_obj_align(c->lbl_state,   LV_ALIGN_TOP_LEFT,  MSG_GUTTER,
                 top + from_h + MSG_ROW_GAP);
    // The chip rides the sender row, right-aligned against the age — the
    // same cluster logic a session card's badges use, minus the chaining,
    // because there is only ever one of them.
    if (!lv_obj_has_flag(c->lbl_agents, LV_OBJ_FLAG_HIDDEN)) {
        lv_obj_update_layout(c->card);
        lv_obj_align_to(c->lbl_agents, c->lbl_elapsed,
                        LV_ALIGN_OUT_LEFT_MID, -MSG_CHIP_GAP, 0);
    }
}

// Right-align the badge cluster: timer rightmost, subagent badge to its left,
// todo badge left of that, evenly spaced. Chained from the timer so hidden
// badges leave no gap. Runs after the labels' texts are set (layout works on
// hidden subtrees too — layout_update_core doesn't skip LV_OBJ_FLAG_HIDDEN).
static void layout_badge_cluster(ChatCard* c) {
    const int gap = 20;       // between cluster items
    const int icon_gap = 6;   // icon ↔ its count
    lv_obj_update_layout(c->card);
    lv_obj_t* anchor = c->lbl_elapsed;
    if (!lv_obj_has_flag(c->img_agents, LV_OBJ_FLAG_HIDDEN)) {
        lv_obj_align_to(c->lbl_agents, anchor, LV_ALIGN_OUT_LEFT_MID, -gap, 0);
        lv_obj_align_to(c->img_agents, c->lbl_agents, LV_ALIGN_OUT_LEFT_MID, -icon_gap, 0);
        anchor = c->img_agents;
    }
    if (!lv_obj_has_flag(c->img_todo, LV_OBJ_FLAG_HIDDEN)) {
        lv_obj_align_to(c->lbl_todo, anchor, LV_ALIGN_OUT_LEFT_MID, -gap, 0);
        lv_obj_align_to(c->img_todo, c->lbl_todo, LV_ALIGN_OUT_LEFT_MID, -icon_gap, 0);
    }
}

// Fill a card from a row. Content only — no motion here (§2.3).
static void chat_card_set_row(ChatCard* c, const SessionRow* r) {
    const int bucket = session_bucket(r->state);
    c->waiting = (bucket == SESSION_BUCKET_WAITING);
    // What a tap on this card will mean, and what a dismissal will remember.
    c->state = r->state;
    c->sig   = row_sig(r);
    // Answered, and the agent has not said anything since -- the go-ahead is
    // on its way but nothing has moved yet. The card stays (only the agent
    // moving can remove it) and stops asking: no pulse, and the chip says so.
    // The instant the agent reports again the sig changes and this lapses by
    // itself, which is the whole reason it is keyed on the sig and not a bool.
    const bool answered = (c->answered_sig != 0 && c->answered_sig == c->sig);
    if (answered) c->waiting = false;
    // The tier is the first thing decided, because it is an input to every
    // colour written below it (card_col) — including the ones inside
    // chat_card_apply_kind.
    const bool want_dim = session_tier_is_dim(r->state);
    const bool tier_changed = (want_dim != c->dim);
    c->dim = want_dim;
    // The anatomy follows the WORDS, not the bucket: a report is a card with
    // a sender and a sentence exactly like a message, and it is in the
    // waiting bucket precisely so the rest of the card can take its colour
    // and its sort from that. The two questions came apart the moment a
    // message-shaped row stopped always being state 11.
    chat_card_apply_kind(c, session_state_has_words(r->state));
    // apply_kind writes the colours its own anatomy owns and already reads the
    // tier through card_col; this restates the rest of the card's palette,
    // which is only ever wrong on the payload where the tier flipped.
    if (tier_changed) chat_card_refresh_tier(c);

    // Body text can be a whole message now, so the buffer is sized for one.
    char sbuf[SESSION_MSG_MAX + 8];
    char buf[24];

    if (c->is_msg) {
        // Everything a session card measures — context, tokens, todos,
        // subagents — is "not applicable" on a message, and the host says so
        // with -1 / 0. Hidden outright, the same way an unknown ctx hides the
        // bar instead of drawing an empty one that reads as 0%.
        lv_obj_add_flag(c->bar, LV_OBJ_FLAG_HIDDEN);
        if (c->lbl_ctx) lv_obj_add_flag(c->lbl_ctx, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(c->img_todo, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(c->lbl_todo, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(c->img_agents, LV_OBJ_FLAG_HIDDEN);

        const lv_color_t wcol = session_words_color(r->state);
        // The sender is purple on every card that has one, because every one
        // of them IS another Claude. The host-minted rows are the exception:
        // "+5 MORE" is a count this host wrote and "FLEET DATA STALE" is a
        // fault it noticed, not peers, and giving either the sender colour
        // would claim a machine of that name reported in.
        lv_obj_set_style_text_color(c->lbl_name,
            card_col(c, session_state_host_minted(r->state) ? COL_DIM : COL_PURPLE), 0);

        // The report chip: the one thing on the card that says this is a
        // status and not mail. An empty chip (a plain message, or the
        // overflow footnote whose label already carries its count) hides the
        // widget instead.
        const char* chip = answered ? "go ahead sent"
                                    : session_report_chip(r->state);
        const lv_color_t chip_col = answered ? COL_DIM
                                             : session_chip_color(r->state);
        int chip_w = 0;   // rendered width + gap; 0 when there is no chip
        if (chip[0]) {
            set_label_if_changed(c->lbl_agents, chip);
            lv_obj_set_style_text_color(c->lbl_agents, card_col(c, chip_col), 0);
            lv_obj_set_style_text_opa(c->lbl_agents,
                                      c->waiting ? (lv_opa_t)pulse_val : LV_OPA_COVER, 0);
            lv_obj_clear_flag(c->lbl_agents, LV_OBJ_FLAG_HIDDEN);
            lv_point_t csz;
            lv_text_get_size(&csz, chip, c->name_font, 0, 0,
                             LV_COORD_MAX, LV_TEXT_FLAG_NONE);
            chip_w = csz.x + MSG_CHIP_GAP;
        } else {
            lv_obj_add_flag(c->lbl_agents, LV_OBJ_FLAG_HIDDEN);
        }

        // Age first, so the sender's ellipsis budget can be measured against
        // the width actually rendered next to it (same trick as the token
        // label on a session card).
        session_elapsed_text(r->elapsed_s, buf, sizeof(buf));
        set_label_if_changed(c->lbl_elapsed, buf);
        lv_point_t sz;
        lv_text_get_size(&sz, buf, c->name_font, 0, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
        const int cw = L.scr_w - 2 * CHAT_CARD_PAD_X;
        int nw = cw - MSG_GUTTER - sz.x - chip_w - 12 /*min gap*/;
        // On a panel narrow enough that the three of them cannot share the
        // row, the CHIP is what goes. The dot already carries the state in
        // colour and the words say the rest, whereas a sender elided to "O..."
        // stops telling you which machine reported — and only the sender can
        // do that job. Today's session-view boards are all 480 px wide so this
        // never fires; it is here so a 368 or 240 port turning the views on
        // degrades instead of drawing a negative-width label.
        if (chip_w && nw < cw / 3) {
            lv_obj_add_flag(c->lbl_agents, LV_OBJ_FLAG_HIDDEN);
            nw += chip_w;
            chip_w = 0;
        }
        if (nw < 1) nw = 1;
        if (nw != c->name_w) {
            c->name_w = nw;
            lv_obj_set_width(c->lbl_name, nw);
        }
        // Sender keeps the session label's middle-elide: it is a name, and
        // the host does not shorten it (SESSION_LABEL_MAX still applies).
        label_set_ellipsized(c->lbl_name, r->label, c->name_font, c->name_w);

        session_state_text(r, sbuf, sizeof(sbuf));
        const int body_w = cw - MSG_GUTTER;
        int lines = label_set_clamped(c->lbl_state, sbuf, MSG_BODY_FONT(c),
                                      body_w, msg_max_lines(c));
        msg_card_layout(c, lines);
        // The words are the point — full brightness, the way a chat's NAME is
        // bright on a session card. The exception is a HOST-MINTED footnote:
        // "+5 MORE" and "FLEET DATA STALE" are statements ABOUT the list, not
        // members of it, and the whole card recedes so the rows it is a
        // footnote to keep the eye. That is what REPORT.md already promises
        // of the overflow row ("the quietest thing on the screen") and what
        // makes the staleness marker calm rather than another alarm.
        lv_obj_set_style_text_color(c->lbl_state,
            card_col(c, session_state_host_minted(r->state) ? COL_DIM : COL_TEXT), 0);
        lv_obj_set_style_text_opa(c->lbl_state, LV_OPA_COVER, 0);
        // Purple on a message (another Claude talking to you, as on the
        // subagents badge); the state's own colour on a report, so a card
        // that needs you carries the same terra-cotta a waiting chat does and
        // reads as urgent from across the room.
        lv_obj_set_style_bg_color(c->dot, card_col(c, wcol), 0);
        lv_obj_set_style_bg_opa(c->dot,
                                c->waiting ? (lv_opa_t)pulse_val : LV_OPA_COVER, 0);
        return;
    }

    // Top-right label (list cards): token count when the host sends one,
    // ctx% as the older-host fallback, hidden when both are unknown. Set
    // BEFORE the name so the name's ellipsis budget can track the rendered
    // width of its actual neighbor (a hidden label gives the name the row).
    if (c->lbl_ctx) {
        const int cw = L.scr_w - 2 * CHAT_CARD_PAD_X;
        bool shown = true;
        if (r->tok >= 0) {
            session_tok_text(r->tok, buf, sizeof(buf));
        } else if (r->ctx_pct >= 0) {
            snprintf(buf, sizeof(buf), "%d%%", r->ctx_pct);
        } else {
            shown = false;
        }
        int nw = cw;
        if (shown) {
            set_label_if_changed(c->lbl_ctx, buf);
            lv_obj_clear_flag(c->lbl_ctx, LV_OBJ_FLAG_HIDDEN);
            lv_point_t sz;
            lv_text_get_size(&sz, buf, c->name_font, 0, 0,
                             LV_COORD_MAX, LV_TEXT_FLAG_NONE);
            nw = cw - sz.x - 12;   // min gap between name end and neighbor
        } else {
            lv_obj_add_flag(c->lbl_ctx, LV_OBJ_FLAG_HIDDEN);
        }
        if (nw != c->name_w) {
            c->name_w = nw;
            lv_obj_set_width(c->lbl_name, nw);
        }
    }

    label_set_ellipsized(c->lbl_name, r->label, c->name_font, c->name_w);

    // Context bar: hidden entirely when the percentage is unknown — an empty
    // bar reads as "0% used" (§5).
    if (r->ctx_pct < 0) {
        lv_obj_add_flag(c->bar, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_clear_flag(c->bar, LV_OBJ_FLAG_HIDDEN);
        lv_bar_set_value(c->bar, r->ctx_pct, LV_ANIM_OFF);
    }

    session_state_text(r, sbuf, sizeof(sbuf));
    set_label_if_changed(c->lbl_state, sbuf);
    lv_obj_set_style_text_color(c->lbl_state, card_col(c, c->waiting ? COL_ACCENT : COL_DIM), 0);
    // Waiting text pulses in phase with the indicator; anything else is solid.
    lv_obj_set_style_text_opa(c->lbl_state, c->waiting ? (lv_opa_t)pulse_val : LV_OPA_COVER, 0);

    // Indicator: dim when idle, neutral when working, accent + pulse when
    // the session needs a human (§1.1).
    lv_obj_set_style_bg_color(c->dot, card_col(c,
        c->waiting ? COL_ACCENT :
        (bucket == SESSION_BUCKET_WORKING) ? COL_TEXT : COL_DIM), 0);
    lv_obj_set_style_bg_opa(c->dot, c->waiting ? (lv_opa_t)pulse_val : LV_OPA_COVER, 0);

    // Badges are hidden when they'd say nothing (§1.1).
    if (r->ttotal > 0) {
        snprintf(buf, sizeof(buf), "%d/%d", r->tdone, r->ttotal);
        set_label_if_changed(c->lbl_todo, buf);
        lv_obj_clear_flag(c->img_todo, LV_OBJ_FLAG_HIDDEN);
        lv_obj_clear_flag(c->lbl_todo, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(c->img_todo, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(c->lbl_todo, LV_OBJ_FLAG_HIDDEN);
    }
    if (r->nagents > 0) {
        snprintf(buf, sizeof(buf), "%d", r->nagents);
        set_label_if_changed(c->lbl_agents, buf);
        lv_obj_clear_flag(c->img_agents, LV_OBJ_FLAG_HIDDEN);
        lv_obj_clear_flag(c->lbl_agents, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(c->img_agents, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(c->lbl_agents, LV_OBJ_FLAG_HIDDEN);
    }

    session_elapsed_text(r->elapsed_s, buf, sizeof(buf));
    set_label_if_changed(c->lbl_elapsed, buf);

    layout_badge_cluster(c);
}

static void focus_set_content(const SessionRow* r) {
    static const char* const model_names[] = { "", "opus", "sonnet", "haiku", "fable" };
    // A message has no model, no context and no token count. The host sends 0
    // / -1 for all three and the branches below hide them on that alone; the
    // explicit test is here so a host that ever fills a field it shouldn't
    // can't put "opus" on a message card.
    const char* model = (!session_is_message(r) && r->model <= SESSION_MODEL_FABLE)
                        ? model_names[r->model] : "";
    set_label_if_changed(focus_lbl_model, model);
    // Budget the name against the pill actually rendered (its text width +
    // padding + a 12px gap) — not a worst case — so a long name runs right up
    // to the pill. An empty pill would render as a bare chip: hide it with
    // its text and give the name the full row.
    const int cw = L.scr_w - 2 * CHAT_CARD_PAD_X;
    int nw = cw;
    if (model[0]) {
        lv_obj_clear_flag(focus_lbl_model, LV_OBJ_FLAG_HIDDEN);
        lv_point_t sz;
        lv_text_get_size(&sz, model, &font_styrene_20, 0, 0,
                         LV_COORD_MAX, LV_TEXT_FLAG_NONE);
        nw = cw - (sz.x + 2 * 12 /*pill pad*/) - 12 /*gap*/;
    } else {
        lv_obj_add_flag(focus_lbl_model, LV_OBJ_FLAG_HIDDEN);
    }
    if (nw != focus_card.name_w) {
        focus_card.name_w = nw;
        lv_obj_set_width(focus_card.lbl_name, nw);
    }

    chat_card_set_row(&focus_card, r);
    s_focus_waiting = focus_card.waiting;

    // Context row: percentage (spelled out — it doubles as onboarding for
    // the terse multi-chat bars) on the left, token counter on the right.
    char buf[32];
    if (r->ctx_pct < 0 || session_is_message(r)) {
        lv_obj_add_flag(focus_lbl_ctx, LV_OBJ_FLAG_HIDDEN);
    } else {
        snprintf(buf, sizeof(buf), "%d%% of context used", r->ctx_pct);
        set_label_if_changed(focus_lbl_ctx, buf);
        lv_obj_clear_flag(focus_lbl_ctx, LV_OBJ_FLAG_HIDDEN);
    }
    if (r->tok < 0 || session_is_message(r)) {
        lv_obj_add_flag(focus_lbl_tok, LV_OBJ_FLAG_HIDDEN);
    } else {
        session_tok_text(r->tok, buf, sizeof(buf));
        set_label_if_changed(focus_lbl_tok, buf);
        lv_obj_clear_flag(focus_lbl_tok, LV_OBJ_FLAG_HIDDEN);
    }

    // The idle tier rides in the colours now (card_col), so the card itself
    // is always fully opaque — this only cancels an in-flight fade.
    lv_anim_delete(focus_card.card, card_opa_anim_cb);
    lv_obj_set_style_opa(focus_card.card, LV_OPA_COVER, 0);
}

// The bottom fade is an affordance for content below the fold, so it is drawn
// only while there IS content below the fold. Shown unconditionally it dims
// the last card's own state line and timer — exactly the reading it exists to
// protect — and promises rows that aren't there.
static void chat_fade_update(void) {
    if (!chat_fade || !cards_cont) return;
    if (lv_obj_get_scroll_bottom(cards_cont) > 0)
        lv_obj_remove_flag(chat_fade, LV_OBJ_FLAG_HIDDEN);
    else
        lv_obj_add_flag(chat_fade, LV_OBJ_FLAG_HIDDEN);
}

static void cards_scroll_cb(lv_event_t* e) { (void)e; chat_fade_update(); }

// ---- Card pool: identity-stable matching + the reorder slide (§2.3) ----

// Matched on (sid, anatomy), not on the sid alone: message sids and session
// sids come from two independent hashes into the same 256-value space, and a
// collision would otherwise alias a message row and a session row onto one
// pooled card — the message flipping a session's card to the message anatomy
// while the session fades in on a fresh one, and which row claims which card
// changing from payload to payload. `is_msg` is what build_chat_card and
// chat_card_apply_kind already maintain, so this costs one comparison.
static ChatCard* chat_card_by_sid(const char* sid, bool want_msg) {
    for (auto& c : chat_cards)
        if (c.used && !c.claimed && c.is_msg == want_msg && strcmp(c.sid, sid) == 0)
            return &c;
    return nullptr;
}

static ChatCard* chat_card_alloc(void) {
    for (auto& c : chat_cards)   // prefer a fully retired card…
        if (!c.used && lv_obj_has_flag(c.card, LV_OBJ_FLAG_HIDDEN)) return &c;
    for (auto& c : chat_cards) { // …else steal one mid-fade-out
        if (!c.used) { lv_anim_delete(c.card, NULL); return &c; }
    }
    return nullptr;              // unreachable: pool size == max rows
}

static void chats_set_content(const SessionList* list) {
    // Motion only while the list is on screen: entering the view (or updating
    // it while another view — or another tab — is up) positions everything
    // instantly, so a swipe onto the sessions tab never lands mid-slide.
    const bool animate = (current_screen == SCREEN_SESSIONS && session_view == 2);

    for (auto& c : chat_cards) c.claimed = false;

    for (int i = 0; i < list->count; i++) {
        const SessionRow* r = &list->rows[i];
        const int target_y = i * CHAT_CARD_PITCH;
        ChatCard* c = chat_card_by_sid(r->sid, session_is_message(r));
        if (c) {
            c->claimed = true;
            chat_card_set_row(c, r);
            lv_anim_delete(c->card, card_opa_anim_cb);   // cancel a stale fade before restyling
            lv_obj_set_style_opa(c->card, LV_OPA_COVER, 0);
            if (c->target_y != target_y) {
                c->target_y = target_y;
                lv_anim_delete(c->card, card_y_anim_cb);  // re-sort mid-flight: restart from here
                if (animate) {
                    lv_anim_t a;
                    lv_anim_init(&a);
                    lv_anim_set_var(&a, c->card);
                    lv_anim_set_exec_cb(&a, card_y_anim_cb);
                    lv_anim_set_values(&a, lv_obj_get_y(c->card), target_y);
                    lv_anim_set_duration(&a, 260);
                    lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
                    lv_anim_start(&a);
                } else {
                    lv_obj_set_y(c->card, target_y);
                }
            }
        } else {
            c = chat_card_alloc();
            if (!c) continue;
            snprintf(c->sid, sizeof(c->sid), "%s", r->sid);
            c->used = true;
            c->claimed = true;
            c->target_y = target_y;
            lv_anim_delete(c->card, NULL);
            lv_obj_set_y(c->card, target_y);
            chat_card_set_row(c, r);
            lv_obj_clear_flag(c->card, LV_OBJ_FLAG_HIDDEN);
            // Cards fade IN from transparent and OUT to it; the receding
            // idle tier is not an opacity any more, so the destination is
            // simply "fully drawn" (chat_card_set_row already picked the
            // dimmed palette if this row is on that tier).
            const lv_opa_t tier = LV_OPA_COVER;
            if (animate) {
                // New chat: fade in at its slot (§2.3)
                lv_obj_set_style_opa(c->card, LV_OPA_TRANSP, 0);
                lv_anim_t a;
                lv_anim_init(&a);
                lv_anim_set_var(&a, c->card);
                lv_anim_set_exec_cb(&a, card_opa_anim_cb);
                lv_anim_set_values(&a, LV_OPA_TRANSP, tier);
                lv_anim_set_duration(&a, 260);
                lv_anim_start(&a);
            } else {
                lv_obj_set_style_opa(c->card, tier, 0);
            }
        }
    }

    // A shrinking list can leave the viewport scrolled past its last card;
    // pull it back so the top of the list is never dead space.
    const int content_h  = list->count * CHAT_CARD_PITCH - CHAT_CARD_GAP;
    const int max_scroll = content_h - lv_obj_get_height(cards_cont);
    if (lv_obj_get_scroll_y(cards_cont) > max_scroll)
        lv_obj_scroll_to_y(cards_cont, max_scroll > 0 ? max_scroll : 0, LV_ANIM_OFF);

    // Chats that closed: fade out; their slot is reclaimed by the slide (§2.3).
    for (auto& c : chat_cards) {
        if (!c.used || c.claimed) continue;
        // The selection cannot outlive its card -- the pool is about to hand
        // this widget to a different chat, and a bar still pointing at it
        // would act on whatever moved in.
        if (s_sel == &c) card_deselect();
        c.used = false;
        c.answered_sig = 0;
        c.waiting = false;
        c.sid[0] = 0;
        c.target_y = -1;
        lv_anim_delete(c.card, NULL);
        if (animate) {
            lv_anim_t a;
            lv_anim_init(&a);
            lv_anim_set_var(&a, c.card);
            lv_anim_set_exec_cb(&a, card_opa_anim_cb);
            lv_anim_set_values(&a, lv_obj_get_style_opa(c.card, LV_PART_MAIN), LV_OPA_TRANSP);
            lv_anim_set_duration(&a, 260);
            lv_anim_set_completed_cb(&a, card_fadeout_done_cb);
            lv_anim_start(&a);
        } else {
            lv_obj_add_flag(c.card, LV_OBJ_FLAG_HIDDEN);
        }
    }

    chat_fade_update();
}

// Draw whatever s_shown currently holds. Two callers: a payload from the host
// (after filtering) and a tap that just removed a card. Splitting it out is
// what lets the tap re-render IMMEDIATELY -- deferring to the next payload
// would leave a hole where the card was for up to five seconds and, worse,
// would not flip the tab to EMPTY, which is where the town hall button lives.
static void sessions_render(void) {
    s_live_count = s_shown.count;
    if (s_shown.count > 0) focus_set_content(&s_shown.rows[0]);
    chats_set_content(&s_shown);          // count 0 releases every card
    update_session_view();
}

// ---- What a tap on a card means ----
// It SELECTS the card. It does not act on it -- see the note on the action bar
// above for why the first cut, which acted on the tap itself, was wrong.
static void card_tap_cb(lv_event_t* e) {
    // LVGL still delivers CLICKED on the release that ended a swipe. The tab
    // has already changed underneath the finger by then; the flag the splash
    // toggle uses for exactly this reason serves here too.
    if (s_gesture_used) return;
    ChatCard* c = (ChatCard*)lv_event_get_user_data(e);
    if (!c || !c->used) return;
    if (s_sel == c) { card_deselect(); return; }   // a second tap closes it
    card_select(c);
}

// Is this card one an agent is waiting on? The whole action-bar vocabulary
// turns on it (see the note on SESSION_REPORT_NEEDS_YOU in data.h). The other
// three report states are excluded on purpose: BLOCKED is parked on a
// permission dialog on another machine, where a message queues BEHIND the
// dialog and changes nothing, and WORKING/DONE are not waiting for anything.
static bool card_can_resume(const ChatCard* c) {
    return c && c->state == SESSION_REPORT_NEEDS_YOU &&
           c->answered_sig != c->sig;
}

static void card_select(ChatCard* c) {
    s_sel = c;
    if (act_do_lbl)
        lv_label_set_text(act_do_lbl, card_can_resume(c) ? "GO AHEAD" : "DISMISS");
    if (act_bar) {
        lv_obj_remove_flag(act_bar, LV_OBJ_FLAG_HIDDEN);
        lv_obj_move_foreground(act_bar);
    }
    // Give the list a bar's worth of extra scroll, so the card the bar covers
    // can still be brought into view instead of being unreachable while a
    // selection is open.
    if (cards_cont) lv_obj_set_style_pad_bottom(cards_cont, ACT_BAR_H, 0);
    // The selected card says so. A border rather than a fill: the card's own
    // colours already carry its state, and a highlight that changed them would
    // make a waiting card look like a different kind of waiting.
    if (c->card) {
        lv_obj_set_style_border_color(c->card, COL_ACCENT, 0);
        lv_obj_set_style_border_opa(c->card, LV_OPA_COVER, 0);
        lv_obj_set_style_border_width(c->card, 2, 0);
    }
}

static void card_deselect(void) {
    if (s_sel && s_sel->card) lv_obj_set_style_border_width(s_sel->card, 0, 0);
    s_sel = nullptr;
    if (act_bar) lv_obj_add_flag(act_bar, LV_OBJ_FLAG_HIDDEN);
    if (cards_cont) lv_obj_set_style_pad_bottom(cards_cont, 0, 0);
}

// Repaint one card as answered right now. The next payload would do it anyway
// (chat_card_set_row reads answered_sig), but the next payload is up to five
// seconds away and the owner has just pressed a button.
static void chat_card_mark_answered(ChatCard* c) {
    if (!c || !c->card) return;
    c->waiting = false;
    if (c->lbl_agents) {
        lv_obj_remove_flag(c->lbl_agents, LV_OBJ_FLAG_HIDDEN);
        set_label_if_changed(c->lbl_agents, "go ahead sent");
        lv_obj_set_style_text_color(c->lbl_agents, card_col(c, COL_DIM), 0);
        lv_obj_set_style_text_opa(c->lbl_agents, LV_OPA_COVER, 0);
    }
    // The pulse leaves the dot and the words wherever it was mid-cycle;
    // put them back to fully drawn.
    if (c->dot) lv_obj_set_style_bg_opa(c->dot, LV_OPA_COVER, 0);
    if (c->lbl_state) lv_obj_set_style_text_opa(c->lbl_state, LV_OPA_COVER, 0);
}

// WAIT. Sends nothing, clears nothing, and leaves the card where it is --
// which is the point: the owner is going to answer that session on a keyboard,
// and the card should go when the SESSION moves, not when they have finished
// reading it.
static void act_wait_cb(lv_event_t* e) {
    (void)e;
    if (s_gesture_used) return;
    card_deselect();
}

static void act_do_cb(lv_event_t* e) {
    (void)e;
    if (s_gesture_used) return;
    ChatCard* c = s_sel;
    if (!c || !c->used) { card_deselect(); return; }

    if (card_can_resume(c)) {
        // GO AHEAD. The card is NOT removed: the agent has to actually move
        // first, and the host stops sending the row when it does. Marking it
        // answered is what stops a second press interrupting the agent twice
        // while it gets going.
        if (!ble_send_event(BLE_EVENT_GO_AHEAD, c->sid)) {
            session_toast("No host - not sent", COL_AMBER);
            card_deselect();
            return;
        }
        c->answered_sig = c->sig;
        session_toast("Go ahead sent", COL_GREEN);
        card_deselect();
        chat_card_mark_answered(c);
        return;
    }

    // DISMISS. Only for the cards nothing is waiting on -- mail already read,
    // a report that says an agent is busy, the overflow footnote. These have
    // no "the session moved" moment to wait for, so the owner is the only
    // thing that can end them.
    ble_send_event(BLE_EVENT_DISMISS, c->sid);   // advisory; see ble.h
    session_toast("Cleared", COL_DIM);
    remember_dismissed(c->sig);
    card_deselect();
    // The owner cleared this themselves, so the linger -- which exists to stop
    // the view snapping away when a chat closes on its own -- would be exactly
    // wrong here: they are waiting for the card to go.
    s_chats_linger = false;
    filter_dismissed(&s_shown, &s_shown);
    sessions_render();
}

// Idle face, or the face of a round that is out. The two differ in colour and
// in one word, and nothing else moves -- the ring keeps its geometry so the
// change reads as the same control in a different state rather than as a new
// screen arriving.
static void town_hall_set_calling(bool calling) {
    if (!th_btn) return;
    if (calling) {
        lv_obj_set_style_bg_color(th_btn, COL_PANEL, 0);
        lv_obj_set_style_bg_color(th_btn, COL_PANEL, LV_STATE_PRESSED);
        lv_obj_set_style_text_color(th_lbl, COL_ACCENT, 0);
        lv_label_set_text(th_lbl, "CALLING");
        set_label_if_changed(th_cap, "waiting for the fleet");
    } else {
        lv_obj_set_style_bg_color(th_btn, COL_ACCENT, 0);
        lv_obj_set_style_bg_color(th_btn, lv_color_hex(0xa85639), LV_STATE_PRESSED);
        lv_obj_set_style_text_color(th_lbl, COL_BG, 0);
        lv_label_set_text(th_lbl, "TOWN\nHALL");
        set_label_if_changed(th_cap, "call every agent in");
        lv_obj_set_style_border_opa(th_ring, LV_OPA_40, 0);
    }
}

static void town_hall_tap_cb(lv_event_t* e) {
    (void)e;
    if (s_gesture_used) return;              // the release that ended a swipe
    if (s_th_called_ms != 0) return;         // a round is already out
    // The event is the whole feature and it can simply fail to go: no daemon,
    // an older daemon that never subscribed to TX, a link that dropped since
    // the last payload. Saying so is the difference between a button that did
    // nothing and a button that looks broken.
    if (!ble_send_report_request()) {
        session_toast("No host - not sent", COL_AMBER);
        return;
    }
    s_th_called_ms = lv_tick_get();
    if (s_th_called_ms == 0) s_th_called_ms = 1;   // 0 is the idle sentinel
    town_hall_set_calling(true);
    session_toast("Calling the fleet", COL_ACCENT);
    Serial.println("Town hall: report round requested from the panel");
}

// Runs from sessions_tick(). Ends the calling state on whichever comes first:
// the replies (which take the tab off the empty view entirely, so the button
// is behind them) or the cooldown. Both paths land back on the idle face, so a
// round that produced nothing leaves a button you can press again rather than
// a "CALLING" that never resolves.
static void town_hall_tick(void) {
    if (s_th_called_ms == 0) return;
    if (session_view != 0 ||
        lv_tick_get() - s_th_called_ms > TOWN_HALL_COOLDOWN_MS) {
        s_th_called_ms = 0;
        town_hall_set_calling(false);
        return;
    }
    // The bezel breathes while the round is out. It borrows the tab's existing
    // pulse rather than starting a second animation: one timer, one phase, and
    // the button is in step with any waiting card that arrives beside it.
    if (th_ring) lv_obj_set_style_border_opa(th_ring, (lv_opa_t)pulse_val, 0);
}

// Refresh the chat views' quota widgets from the cached usage payload. Values
// match the RESTING panels; enterprise accounts map spending → slot 1,
// period → slot 2.
static void session_quota_refresh(void) {
    if (!focus_group || !s_usage_cache.valid) return;
    const UsageData* d = &s_usage_cache;

    const int s_pct = (int)(d->session_pct + 0.5f);
    const int w_pct = d->enterprise ? d->time_pct : (int)(d->weekly_pct + 0.5f);
    const lv_color_t col0 = pct_color(d->session_pct);
    const lv_color_t col1 = pct_color((float)w_pct);

    char pct0[8], pct1[8], buf[48];
    snprintf(pct0, sizeof(pct0), "%d%%", s_pct);
    snprintf(pct1, sizeof(pct1), "%d%%", w_pct);

    // ONE-CHAT 5h panel — same treatment as the RESTING "Current" panel.
    set_label_if_changed(f5_pct, pct0);
    set_label_if_changed(f5_pill, d->enterprise ? "Spending" : "Current");
    lv_bar_set_value(f5_bar, s_pct, LV_ANIM_OFF);
    lv_obj_set_style_bg_color(f5_bar, col0, LV_PART_INDICATOR);
    if (d->enterprise) {
        lv_obj_add_flag(f5_reset, LV_OBJ_FLAG_HIDDEN);  // spending has no reset clock
    } else {
        format_reset_time(d->session_reset_mins, buf, sizeof(buf));
        set_label_if_changed(f5_reset, buf);
        lv_obj_clear_flag(f5_reset, LV_OBJ_FLAG_HIDDEN);
    }

    // SEVERAL-CHATS one-line strip.
    set_label_if_changed(cq_tag[0], d->enterprise ? "$"  : "5h");
    set_label_if_changed(cq_tag[1], d->enterprise ? "pd" : "7d");
    set_label_if_changed(cq_pct[0], pct0);
    set_label_if_changed(cq_pct[1], pct1);
    lv_bar_set_value(cq_bar[0], s_pct, LV_ANIM_OFF);
    lv_bar_set_value(cq_bar[1], w_pct, LV_ANIM_OFF);
    lv_obj_set_style_bg_color(cq_bar[0], col0, LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(cq_bar[1], col1, LV_PART_INDICATOR);
}

// Transparent full-screen group, same pattern as usage_group / pair_group.
static lv_obj_t* make_session_group(lv_obj_t* parent) {
    lv_obj_t* g = lv_obj_create(parent);
    lv_obj_set_size(g, L.scr_w, L.scr_h);
    lv_obj_set_pos(g, 0, 0);
    lv_obj_set_style_bg_opa(g, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(g, 0, 0);
    lv_obj_set_style_pad_all(g, 0, 0);
    lv_obj_clear_flag(g, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(g, LV_OBJ_FLAG_EVENT_BUBBLE);
    lv_obj_add_flag(g, LV_OBJ_FLAG_HIDDEN);   // update_view_state decides
    return g;
}

static void build_session_views(lv_obj_t* parent) {
    init_icon_dsc_rgb565a8(&icon_todo_dsc, ICON_LIST_TODO_W, ICON_LIST_TODO_H, icon_list_todo_data);
    init_icon_dsc_rgb565a8(&icon_todo_small_dsc, ICON_LIST_TODO_SMALL_W, ICON_LIST_TODO_SMALL_H, icon_list_todo_small_data);
    init_icon_dsc_rgb565a8(&icon_agents_dsc, ICON_USERS_ROUND_W, ICON_USERS_ROUND_H, icon_users_round_data);
    init_icon_dsc_rgb565a8(&icon_agents_small_dsc, ICON_USERS_ROUND_SMALL_W, ICON_USERS_ROUND_SMALL_H, icon_users_round_small_data);

    // ---- ONE-CHAT (§1.3): two boxes ----
    // The 5h quota panel (exact RESTING "Current" panel — it carries the most
    // minute-to-minute value) on top, the chat card below it. One box per
    // concern, so a future multi-account build gets a box per account. The
    // 7d row is dropped here (see the f5_* rationale).
    focus_group = make_session_group(parent);
    lv_obj_t* p5 = make_usage_panel(focus_group, L.content_y, "Current",
                                    &f5_pct, &f5_pill, &f5_bar, &f5_reset);
    // Full-bleed like the chat card below it. make_usage_panel stays shared
    // with the untouched RESTING view, so the width/pad/bar adjustments are
    // applied here instead: text keeps a 20px inset from the glass edge.
    lv_obj_set_pos(p5, 0, L.content_y);
    lv_obj_set_size(p5, L.scr_w, L.usage_panel_h);
    lv_obj_set_style_pad_left(p5, CHAT_CARD_PAD_X, 0);
    lv_obj_set_style_pad_right(p5, CHAT_CARD_PAD_X, 0);
    lv_obj_set_width(f5_bar, L.scr_w - 2 * CHAT_CARD_PAD_X);
    const int focus_card_y = L.content_y + L.usage_panel_h + FOCUS_PANEL_GAP;
    build_chat_card(&focus_card, focus_group, 0, focus_card_y, true);

    // Chat card extras: model pill (quota-pill treatment at the chat card's
    // own text size) + the context line.
    focus_lbl_model = make_card_label(focus_card.card, &font_styrene_24, COL_TEXT);
    lv_obj_set_style_bg_color(focus_lbl_model, COL_BAR_BG, 0);
    lv_obj_set_style_bg_opa(focus_lbl_model, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(focus_lbl_model, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_pad_left(focus_lbl_model, 12, 0);
    lv_obj_set_style_pad_right(focus_lbl_model, 12, 0);
    lv_obj_set_style_pad_top(focus_lbl_model, 4, 0);
    lv_obj_set_style_pad_bottom(focus_lbl_model, 4, 0);
    lv_obj_align(focus_lbl_model, LV_ALIGN_TOP_RIGHT, 0, 0);
    focus_lbl_ctx = make_card_label(focus_card.card, &font_styrene_24, COL_TEXT);
    lv_obj_align(focus_lbl_ctx, LV_ALIGN_TOP_LEFT, 0, 64);
    focus_lbl_tok = make_card_label(focus_card.card, &font_styrene_24, COL_TEXT);
    lv_obj_align(focus_lbl_tok, LV_ALIGN_TOP_RIGHT, 0, 64);

    // ---- SEVERAL-CHATS (§1.4): one-line quota strip + the card list ----
    chats_group = make_session_group(parent);
    // Combined 5h/7d row, PR #129 geometry: [dim tag | bar | big pct] twice,
    // each element vertically centered in the CHAT_ROW_H band.
    const int half = (L.content_w - CHAT_ROW_HALF_GAP) / 2;
    const int strip_bar_w = half - CHAT_ROW_LBL_W - CHAT_ROW_PCT_W - 2 * CHAT_ROW_COL_GAP;
    const int tag_y = L.content_y +
        (CHAT_ROW_H - lv_font_get_line_height(&font_styrene_24)) / 2;
    const int pct_y = L.content_y +
        (CHAT_ROW_H - lv_font_get_line_height(&font_styrene_28)) / 2;
    for (int i = 0; i < 2; i++) {
        const int x0 = L.margin + i * (half + CHAT_ROW_HALF_GAP);
        cq_tag[i] = make_card_label(chats_group, &font_styrene_24, COL_DIM);
        lv_obj_set_width(cq_tag[i], CHAT_ROW_LBL_W);
        lv_obj_set_pos(cq_tag[i], x0, tag_y);
        cq_bar[i] = make_bar(chats_group,
                             x0 + CHAT_ROW_LBL_W + CHAT_ROW_COL_GAP,
                             L.content_y + (CHAT_ROW_H - CHAT_ROW_BAR_H) / 2,
                             strip_bar_w, CHAT_ROW_BAR_H);
        cq_pct[i] = make_card_label(chats_group, &font_styrene_28, COL_TEXT);
        lv_obj_set_width(cq_pct[i], CHAT_ROW_PCT_W);
        lv_obj_set_style_text_align(cq_pct[i], LV_TEXT_ALIGN_RIGHT, 0);
        lv_obj_set_pos(cq_pct[i],
                       x0 + CHAT_ROW_LBL_W + 2 * CHAT_ROW_COL_GAP + strip_bar_w,
                       pct_y);
    }

    // Card viewport: runs from the strip to the PHYSICAL bottom edge — this
    // sub-view alone drops the bottom margin. Side margins stay.
    //
    // Three cards fit it exactly (see CHAT_CARD_GAP). SESSION_MAX_ROWS is 6, so
    // rows 4-6 have to be reachable: the viewport SCROLLS, VERTICALLY ONLY.
    // That direction restriction is what keeps the tab ring alive — LVGL picks
    // a scroll object per drag axis (lv_indev_find_scroll_obj), so a horizontal
    // swipe finds none here, no scroll starts, and the gesture reaches
    // screen_gesture_cb untouched. A vertical drag scrolls the list and
    // suppresses the gesture, which is what it should do.
    const int list_y = L.content_y + CHAT_ROW_H + CHAT_ROW_GAP + 4;  // #129 ch_list_y
    const int list_h = L.scr_h - list_y;
    cards_cont = lv_obj_create(chats_group);
    lv_obj_set_pos(cards_cont, 0, list_y);              // full-bleed card column
    lv_obj_set_size(cards_cont, L.scr_w, list_h);
    lv_obj_set_style_bg_opa(cards_cont, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(cards_cont, 0, 0);
    lv_obj_set_style_pad_all(cards_cont, 0, 0);
    lv_obj_add_flag(cards_cont, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_scroll_dir(cards_cont, LV_DIR_VER);
    // AUTO, which in LVGL means "whenever there is content off-screen" (not
    // "only while dragging") — so the bar is the standing, unambiguous cue
    // that rows 4-6 exist, and it vanishes the moment the list fits. It rides
    // in the cards' right-hand padding, clear of every label.
    lv_obj_set_scrollbar_mode(cards_cont, LV_SCROLLBAR_MODE_AUTO);
    lv_obj_set_style_bg_color(cards_cont, COL_DIM, LV_PART_SCROLLBAR);
    lv_obj_set_style_bg_opa(cards_cont, LV_OPA_50, LV_PART_SCROLLBAR);
    lv_obj_set_style_width(cards_cont, 4, LV_PART_SCROLLBAR);
    lv_obj_set_style_radius(cards_cont, LV_RADIUS_CIRCLE, LV_PART_SCROLLBAR);
    lv_obj_set_style_pad_right(cards_cont, 6, LV_PART_SCROLLBAR);
    lv_obj_set_style_pad_top(cards_cont, 6, LV_PART_SCROLLBAR);
    lv_obj_set_style_pad_bottom(cards_cont, 6, LV_PART_SCROLLBAR);
    lv_obj_add_flag(cards_cont, LV_OBJ_FLAG_EVENT_BUBBLE);
    lv_obj_add_event_cb(cards_cont, cards_scroll_cb, LV_EVENT_SCROLL, NULL);
    lv_obj_add_event_cb(cards_cont, cards_scroll_cb, LV_EVENT_SCROLL_END, NULL);

    for (auto& c : chat_cards) {
        build_chat_card(&c, cards_cont, 0, 0, false);
        lv_obj_add_flag(c.card, LV_OBJ_FLAG_HIDDEN);
    }

    // Bottom fade: whatever pokes below the fold tapers into the panel's
    // bottom edge instead of ending in a hard cut + dead black band. A pure
    // style gradient — per-end background opacity, no intermediate buffers —
    // so PSRAM-free ports can enable it as-is. Created after the card pool:
    // it's a later sibling of cards_cont, so reorder slides and pool churn
    // inside the container can never draw above it. Input-transparent (not
    // clickable), so the tap-anywhere splash toggle works through it.
    chat_fade = lv_obj_create(chats_group);
    lv_obj_set_pos(chat_fade, 0, L.scr_h - CHAT_FADE_H);
    lv_obj_set_size(chat_fade, L.scr_w, CHAT_FADE_H);
    lv_obj_set_style_radius(chat_fade, 0, 0);
    lv_obj_set_style_border_width(chat_fade, 0, 0);
    lv_obj_set_style_pad_all(chat_fade, 0, 0);
    lv_obj_set_style_bg_color(chat_fade, COL_BG, 0);
    lv_obj_set_style_bg_grad_color(chat_fade, COL_BG, 0);
    lv_obj_set_style_bg_grad_dir(chat_fade, LV_GRAD_DIR_VER, 0);
    lv_obj_set_style_bg_opa(chat_fade, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_main_opa(chat_fade, LV_OPA_TRANSP, 0);  // top: see-through
    lv_obj_set_style_bg_grad_opa(chat_fade, LV_OPA_COVER, 0);   // bottom: panel black
    lv_obj_clear_flag(chat_fade, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_clear_flag(chat_fade, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(chat_fade, LV_OBJ_FLAG_HIDDEN);   // chat_fade_update() decides

    // ---- EMPTY: nothing needs a human ----
    // New with the tab model. When the chat views were auto-selected the
    // resolver simply never picked them with nothing to show; now the user can
    // swipe here whenever they like, so the tab has to answer for itself.
    //
    // Since the host started filtering the roster down to what needs a person
    // (daemon/FLEET.md § "Attention, not roster"), this is no longer the rare
    // screen — it is the screen the owner sees most of the time, and it is
    // GOOD NEWS. So it stays a single dim line rather than borrowing the usage
    // screen's "Zzz" creature: that treatment dresses up a FAULT (the host
    // stopped talking) and would make the calm, healthy state the loudest
    // thing on the tab, competing with the corner mascot the usage screen is
    // already animating two swipes away.
    //
    // The sub-line is the price of the filter. A tab that used to list nine
    // remote sessions and now lists none has to say that this is the design
    // and not a broken feed; it is hidden when the link is down, because then
    // the headline above it is the whole explanation.
    empty_group = make_session_group(parent);

    // Sized off the SHORT side so the portrait boards get a circle rather than
    // an ellipse's worth of ambition, and clamped at both ends: below ~110 px
    // the word stops fitting, above ~200 px it stops reading as a control and
    // starts reading as a plate.
    const int th_d0 = (L.scr_w < L.scr_h ? L.scr_w : L.scr_h) * 2 / 5;
    const int th_d  = th_d0 < 110 ? 110 : (th_d0 > 200 ? 200 : th_d0);
    const int th_dy = 26;     // the stack's optical centre, not its arithmetic one

    // Bezel: a ring with air around the face, the way a control that matters
    // gets separated from its background. Not a shadow -- LVGL blurs those per
    // frame, and the pulse below would pay for it sixty times a second.
    th_ring = lv_obj_create(empty_group);
    lv_obj_set_size(th_ring, th_d + 22, th_d + 22);
    lv_obj_set_style_radius(th_ring, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_opa(th_ring, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_color(th_ring, COL_ACCENT, 0);
    lv_obj_set_style_border_width(th_ring, 3, 0);
    lv_obj_set_style_border_opa(th_ring, LV_OPA_40, 0);
    lv_obj_set_style_pad_all(th_ring, 0, 0);
    lv_obj_clear_flag(th_ring, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_clear_flag(th_ring, LV_OBJ_FLAG_CLICKABLE);   // the face takes the tap
    lv_obj_align(th_ring, LV_ALIGN_CENTER, 0, th_dy);

    // Face: terracotta with the panel's own black on it. Claude's button, not
    // a red alarm -- this calls a meeting, it does not report a fire.
    th_btn = lv_obj_create(empty_group);
    lv_obj_set_size(th_btn, th_d, th_d);
    lv_obj_set_style_radius(th_btn, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(th_btn, COL_ACCENT, 0);
    lv_obj_set_style_bg_opa(th_btn, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(th_btn, 0, 0);
    lv_obj_set_style_pad_all(th_btn, 0, 0);
    // Pressed: the face darkens. Deliberately not a scale transform -- LVGL
    // would re-render the circle at a new size every frame of the press, and
    // this panel has better uses for those milliseconds.
    lv_obj_set_style_bg_color(th_btn, lv_color_hex(0xa85639), LV_STATE_PRESSED);
    lv_obj_clear_flag(th_btn, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(th_btn, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(th_btn, town_hall_tap_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_align(th_btn, LV_ALIGN_CENTER, 0, th_dy);

    th_lbl = lv_label_create(th_btn);
    lv_label_set_text(th_lbl, "TOWN\nHALL");
    lv_obj_set_style_text_font(th_lbl, L.bt_device_font, 0);
    lv_obj_set_style_text_color(th_lbl, COL_BG, 0);
    lv_obj_set_style_text_align(th_lbl, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_line_space(th_lbl, 2, 0);
    lv_obj_center(th_lbl);

    // The headline keeps its job -- say which kind of nothing this is -- and
    // moves above the button, where it reads as the reason the button is
    // being offered.
    empty_lbl = lv_label_create(empty_group);
    lv_label_set_text(empty_lbl, "Nothing needs you");
    lv_obj_set_style_text_font(empty_lbl, L.bt_credit_2_font, 0);
    lv_obj_set_style_text_color(empty_lbl, COL_DIM, 0);
    lv_obj_align(empty_lbl, LV_ALIGN_CENTER, 0, th_dy - th_d / 2 - 44);

    // What the button does, in the words of somebody deciding whether to press
    // it. It replaces the old "only what needs you shows here" note: that line
    // existed to explain an empty list, and an empty list with a control in
    // the middle of it no longer reads as a broken feed.
    th_cap = lv_label_create(empty_group);
    lv_label_set_text(th_cap, "call every agent in");
    lv_obj_set_style_text_font(th_cap, L.bt_credit_2_font, 0);
    lv_obj_set_style_text_color(th_cap, COL_DIM, 0);
    lv_obj_set_style_text_opa(th_cap, LV_OPA_50, 0);
    lv_obj_set_style_text_align(th_cap, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(th_cap, LV_ALIGN_CENTER, 0, th_dy + th_d / 2 + 34);
    empty_hint = th_cap;   // the resolver still hides this line when the link is down

    // The action bar. Parented on the TAB rather than on a card, because the
    // cards live in a scrolling viewport: a bar inside it would scroll away
    // from the selection it belongs to, and one anchored to a card would have
    // to move every time the list re-sorts underneath.
    act_bar = lv_obj_create(parent);
    lv_obj_set_size(act_bar, L.scr_w, ACT_BAR_H);
    lv_obj_set_style_bg_color(act_bar, COL_BG, 0);
    lv_obj_set_style_bg_opa(act_bar, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(act_bar, 0, 0);
    lv_obj_set_style_border_width(act_bar, 0, 0);
    lv_obj_set_style_pad_all(act_bar, 0, 0);
    lv_obj_clear_flag(act_bar, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_align(act_bar, LV_ALIGN_BOTTOM_MID, 0, 0);
    lv_obj_add_flag(act_bar, LV_OBJ_FLAG_HIDDEN);

    {
        // Two buttons, and the split is not even: the one that SENDS gets the
        // room and the colour, the one that does nothing is an outline. A bar
        // where both looked equally like the thing to press would make WAIT --
        // the safe answer, and the common one -- as easy to hit by accident as
        // interrupting an agent.
        const int gap  = 12;
        const int side = L.margin;
        const int w    = L.scr_w - 2 * side - gap;
        const int wide = w * 3 / 5;

        act_do = lv_obj_create(act_bar);
        lv_obj_set_size(act_do, wide, ACT_BTN_H);
        lv_obj_set_style_radius(act_do, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(act_do, COL_ACCENT, 0);
        lv_obj_set_style_bg_color(act_do, lv_color_hex(0xa85639), LV_STATE_PRESSED);
        lv_obj_set_style_bg_opa(act_do, LV_OPA_COVER, 0);
        lv_obj_set_style_border_width(act_do, 0, 0);
        lv_obj_set_style_pad_all(act_do, 0, 0);
        lv_obj_clear_flag(act_do, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_event_cb(act_do, act_do_cb, LV_EVENT_CLICKED, NULL);
        lv_obj_align(act_do, LV_ALIGN_LEFT_MID, side, 0);
        act_do_lbl = lv_label_create(act_do);
        lv_obj_set_style_text_font(act_do_lbl, L.bt_credit_2_font, 0);
        lv_obj_set_style_text_color(act_do_lbl, COL_BG, 0);
        lv_obj_center(act_do_lbl);

        act_wait = lv_obj_create(act_bar);
        lv_obj_set_size(act_wait, w - wide, ACT_BTN_H);
        lv_obj_set_style_radius(act_wait, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_opa(act_wait, LV_OPA_TRANSP, 0);
        lv_obj_set_style_bg_color(act_wait, COL_PANEL, LV_STATE_PRESSED);
        lv_obj_set_style_bg_opa(act_wait, LV_OPA_COVER, LV_STATE_PRESSED);
        lv_obj_set_style_border_color(act_wait, COL_DIM, 0);
        lv_obj_set_style_border_width(act_wait, 1, 0);
        lv_obj_set_style_border_opa(act_wait, LV_OPA_40, 0);
        lv_obj_set_style_pad_all(act_wait, 0, 0);
        lv_obj_clear_flag(act_wait, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_event_cb(act_wait, act_wait_cb, LV_EVENT_CLICKED, NULL);
        lv_obj_align(act_wait, LV_ALIGN_RIGHT_MID, -side, 0);
        lv_obj_t* wl = lv_label_create(act_wait);
        lv_label_set_text(wl, "WAIT");
        lv_obj_set_style_text_font(wl, L.bt_credit_2_font, 0);
        lv_obj_set_style_text_color(wl, COL_DIM, 0);
        lv_obj_center(wl);
    }

    // The toast. Parented on the TAB, above both sub-views, so a tap that
    // clears the last card can still be acknowledged by a screen that has just
    // become the empty one.
    toast_obj = lv_obj_create(parent);
    lv_obj_set_style_bg_color(toast_obj, COL_PANEL, 0);
    lv_obj_set_style_bg_opa(toast_obj, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(toast_obj, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_border_width(toast_obj, 0, 0);
    lv_obj_set_style_pad_hor(toast_obj, 18, 0);
    lv_obj_set_style_pad_ver(toast_obj, 8, 0);
    lv_obj_clear_flag(toast_obj, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_clear_flag(toast_obj, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(toast_obj, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    toast_lbl = lv_label_create(toast_obj);
    lv_obj_set_style_text_font(toast_lbl, L.bt_credit_2_font, 0);
    lv_obj_center(toast_lbl);
    lv_obj_align(toast_obj, LV_ALIGN_BOTTOM_MID, 0, -L.margin);
    lv_obj_add_flag(toast_obj, LV_OBJ_FLAG_HIDDEN);

    // The shared pulse: LV_OPA_COVER ↔ LV_OPA_30, 700 ms each way, forever.
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, &pulse_val);
    lv_anim_set_exec_cb(&a, pulse_exec_cb);
    lv_anim_set_values(&a, LV_OPA_COVER, LV_OPA_30);
    lv_anim_set_duration(&a, 700);
    lv_anim_set_playback_duration(&a, 700);
    lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
    lv_anim_start(&a);
}

// ---- The sessions tab's own resolver ----
// The chat views' half of the old update_view_state(), migrated intact: one
// chat → ONE-CHAT, several → SEVERAL-CHATS, none → EMPTY. The CHAT_LINGER_MS
// timer came with it and still earns its keep — it holds the last cards
// (content frozen, waiting treatment dropped) instead of snapping to "No
// active sessions" the instant a chat closes. What did NOT come along is the
// s_any_notify override: pinning mattered when a waiting chat had to fight
// the usage screen for the panel, and it is now expressed as the auto-jump
// below, which brings the user to this tab and then leaves them in control.
static void update_session_view(void) {
    if (!focus_group) return;
    const uint32_t now = lv_tick_get();
    int v;
    // Connection first — the same test the usage screen's resolver has always
    // made first. Every row on this tab arrived over the link and is only as
    // true as the link is; s_live_count is written in exactly one place
    // (ui_update_sessions), so a host that sleeps or dies would otherwise leave
    // a "needs permission" card frozen on screen, elapsed timer and all,
    // forever — and the tab is where the auto-jump may have parked the user.
    if (!s_ble_connected)       v = 0;
    else if (s_live_count == 1) v = 1;
    else if (s_live_count >= 2) v = 2;
    else if (s_chats_linger && (now - s_chats_gone_ms) < CHAT_LINGER_MS) {
        v = s_linger_view;      // hold the chat view after the last chat closed
    } else {
        s_chats_linger = false; // linger expired (or never armed)
        v = 0;
    }
    // Say which kind of nothing this is: a calm desk reads differently from a
    // host that stopped talking. And note what this screen no longer means —
    // "nothing needs you" is a claim about the HOST's last word, which is why
    // the host has to send SESSION_HOST_STALE rather than let this screen
    // quietly stand in for an outage.
    if (v == 0 && empty_lbl)
        set_label_if_changed(empty_lbl, s_ble_connected ? "Nothing needs you"
                                                        : "Host disconnected");
    if (v == 0 && empty_hint) {
        if (s_ble_connected) lv_obj_clear_flag(empty_hint, LV_OBJ_FLAG_HIDDEN);
        else                 lv_obj_add_flag(empty_hint, LV_OBJ_FLAG_HIDDEN);
    }
    // A round is dispatched by the HOST. With the link down there is nobody to
    // ask, so the button goes rather than sitting there inviting a press that
    // can only produce an apology.
    if (th_btn && th_ring) {
        if (v == 0 && s_ble_connected) {
            lv_obj_remove_flag(th_btn,  LV_OBJ_FLAG_HIDDEN);
            lv_obj_remove_flag(th_ring, LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_add_flag(th_btn,  LV_OBJ_FLAG_HIDDEN);
            lv_obj_add_flag(th_ring, LV_OBJ_FLAG_HIDDEN);
        }
    }
    if (v == session_view) return;
    if (v == 0) card_deselect();     // nothing left to act on
    session_view = v;
    lv_obj_add_flag(focus_group, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(chats_group, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(empty_group, LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(v == 1 ? focus_group : v == 2 ? chats_group : empty_group,
                      LV_OBJ_FLAG_HIDDEN);
}

// ---- Auto-jump on notification (requirement 4) ----
// Fill this payload's NOTIFY set and flag any sid that was not in it last
// time. Called once per payload, before the resolver and the jump.
//
// Two different things earn a notification, and they share one edge detector
// because they have the same answer — put this in front of the owner, once:
//
//   · a session that has entered the waiting bucket (it is blocked on a human)
//   · a message another Claude Code session sent this machine
//
// A message is if anything the better fit for the machinery than the case it
// was built for. The host mints the sid from the message id, so it is stable
// across polls (one rising edge, not one per 5 s tick), and a message that
// never surfaces is a message the owner has to go looking for, which is the
// whole feature.
//
// It joins the SET but not the LEVEL (s_any_notify), so the auto-return still
// works: the panel visits this tab, dwells, and goes back to what the owner
// was looking at. Holding the level would pin the screen here for the message's
// entire life — inbox_expire_s, three minutes by default, against a 10 s dwell
// — and would make any host-side failure to retract a message a permanently
// stuck panel rather than one stale card.
//
// What a message deliberately does NOT get is the waiting bucket itself: no
// accent, no pulse (see SESSION_BUCKET_MESSAGE). Being pulled to the tab once
// is the notification; a card that keeps pulsing for three minutes is nagging
// about something already read.
static void note_notify_set(const SessionList* list) {
    s_notify_now_n = 0;
    s_new_notify   = false;
    s_new_notify_msg = false;
    s_new_notify_report = false;
    bool any_waiting = false;
    for (int i = 0; list && i < list->count && s_notify_now_n < SESSION_MAX_ROWS; i++) {
        const SessionRow* r = &list->rows[i];
        const int bucket = session_bucket(r->state);
        if (bucket != SESSION_BUCKET_WAITING && bucket != SESSION_BUCKET_MESSAGE) continue;
        // The kind bit is the CARD ANATOMY, not the bucket: a report is in
        // the waiting bucket and still shares the message card's sid
        // namespace, so that is what has to travel with the sid here (see
        // s_notify_sids on why a sid alone is not an identity).
        const bool has_words = session_state_has_words(r->state);
        const bool is_report = has_words && r->state != SESSION_MESSAGE;
        snprintf(s_notify_now[s_notify_now_n], sizeof(s_notify_now[0]), "%s", r->sid);
        s_notify_now_kind[s_notify_now_n] = has_words;
        if (!sid_in_set(s_notify_sids, s_notify_kind, s_notify_n, r->sid, has_words)) {
            s_new_notify = true;
            if (is_report)       s_new_notify_report = true;
            else if (has_words)  s_new_notify_msg = true;
        }
        // A REPORT does not hold the level either, and for a sharper reason
        // than a message does. The level is what the auto-return waits on: it
        // means "a session is STILL blocked on you", and it clears when the
        // host says the session moved. A report has no such falling edge —
        // the host cannot see the remote agent at all, it only knows what
        // that agent said N seconds ago, and the row goes away by EXPIRY. So
        // holding the level would pin the panel on this tab for the whole
        // report window, and an agent that never reports again would pin it
        // for good. It gets the accent, the pulse, the top of the list and
        // one auto-jump; it does not get to own the screen.
        if (!has_words) any_waiting = true;
        s_notify_now_n++;
    }
    // A message joins the set (so it gets its one rising edge, and only one)
    // but NOT the level. The level answers "is a session still blocked on
    // you?", and it is what holds the panel on this tab: a message that held
    // it would keep the screen off the usage view for its whole life — three
    // minutes by default, against a 10 s auto-return dwell — for something
    // already read. Being pulled here once is the notification; the card
    // stays for as long as the host sends it, one swipe away.
    s_any_notify = any_waiting;
}

// EDGES ONLY, and the rising edge is PER SESSION. "Something is waiting" is a
// level a single parked permission prompt can hold for an hour: jumping on the
// level would re-yank the tab on every payload and make the device impossible
// to navigate, while edging on the level as a whole (the first cut of this)
// dropped the notification for a second session that started waiting while the
// first still was — the multi-session case the tab is named for. So: one jump
// per sid that enters the notify set.
//
// It deliberately does NOT fire while the user is on the settings tab. They
// are mid-edit on a screen whose every row is a tap target; moving the panel
// under a descending finger would mistap a chat card, and unlike the usage
// screen the settings tab is somewhere you only ever are on purpose. The
// notification is not lost — the sessions tab is one swipe away and the cards
// are already rendered behind it.
//
// The return trip: when the LAST waiting session clears — or right away for a
// jump only a message caused, since a message is never "still waiting" and no
// falling edge is ever coming for it (arming it at the jump also stops the
// return from waiting on the next payload, which the host may not send for
// 30 s) — a tab we jumped to ourselves hands the screen back
// to where the user actually was. It is armed
// here and executed by sessions_tick() once AUTO_RETURN_DWELL_MS has passed
// since the jump — reading a notification produces no touch, so returning on
// the very next payload (the daemon ticks every ~5 s) would take the screen
// away from someone still looking at it, and a waiting/clear/waiting burst
// would flip the panel twice. Any touch or manual navigation clears
// s_auto_jumped (see screen_press_cb / show_screen) and disarms the return.
static void maybe_auto_jump(void) {
    const bool rising  = s_new_notify;
    const bool falling = !s_any_notify && s_notify_n > 0;
    memcpy(s_notify_sids, s_notify_now, sizeof(s_notify_sids));
    memcpy(s_notify_kind, s_notify_now_kind, sizeof(s_notify_kind));
    s_notify_n = s_notify_now_n;

    if (rising) {
        s_auto_return_due = false;                       // whatever was pending
        if (!settings_auto_jump_enabled()) return;
        if (current_screen == SCREEN_SESSIONS) return;   // already there
        if (current_screen == SCREEN_SETTINGS) return;   // never mid-edit
        s_auto_jump_from = current_screen;               // splash or usage
        // The alerting chat is row 0 by the host's sort: make sure the list is
        // showing the top, not wherever it was last scrolled to.
        if (cards_cont) lv_obj_scroll_to_y(cards_cont, 0, LV_ANIM_OFF);
        show_screen(SCREEN_SESSIONS, false);             // not manual: keeps the claim
        s_auto_jumped  = true;
        s_auto_jump_ms = lv_tick_get();
        Serial.println(s_new_notify_report
            ? "An agent reported that it needs you — auto-jump to the sessions tab"
            : s_new_notify_msg
            ? "Message from another Claude session — auto-jump to the sessions tab"
            : "Session needs you — auto-jump to the sessions tab");
        // Nothing is WAITING, so this jump was mail or an agent report —
        // neither of which will ever produce a falling edge. Arm the return
        // now; sessions_tick() still holds the dwell, and any touch disarms it.
        if (!s_any_notify) s_auto_return_due = true;
    } else if (falling && s_auto_jumped) {
        s_auto_return_due = true;                        // sessions_tick() finishes it
    }
}

// Runs every UI tick, not only when a payload lands, so the dwell is measured
// against the clock rather than against the daemon's cadence.
static void sessions_tick(void) {
    town_hall_tick();
    if (toast_obj && s_toast_until && lv_tick_get() > s_toast_until) {
        s_toast_until = 0;
        lv_obj_add_flag(toast_obj, LV_OBJ_FLAG_HIDDEN);
    }
    if (!s_auto_return_due) return;
    if (!s_auto_jumped || current_screen != SCREEN_SESSIONS) {
        s_auto_return_due = false;   // they touched it, or navigated away
        return;
    }
    if (lv_tick_get() - s_auto_jump_ms < AUTO_RETURN_DWELL_MS) return;
    s_auto_return_due = false;
    s_auto_jumped = false;
    show_screen(s_auto_jump_from, false);
    Serial.println("Sessions clear — returning to the previous tab");
}

// The link went away. Everything this tab knows arrived over it, so drop the
// lot: the resolver falls to "Host disconnected", the frozen cards stop
// pulsing, and emptying the notify set re-arms the edge so a session
// that is STILL waiting when the host comes back notifies again instead of
// being swallowed as "no rising edge".
static void sessions_link_lost(void) {
    card_deselect();      // the bar offers actions the link cannot carry
    s_live_count      = 0;
    s_any_notify      = false;
    s_notify_n        = 0;     // re-arms the edge for every sid still in the set
    s_notify_now_n    = 0;
    s_new_notify      = false;
    s_new_notify_msg  = false;
    s_new_notify_report = false;
    s_focus_waiting   = false;
    s_chats_linger    = false;
    s_auto_jumped     = false;
    s_auto_return_due = false;
    for (auto& c : chat_cards) c.waiting = false;
    update_session_view();
}

#else   // !BOARD_HAS_SESSION_VIEWS

static void update_session_view(void) {}
static void sessions_tick(void) {}
static void sessions_link_lost(void) {}
static void card_deselect(void) {}

#endif  // BOARD_HAS_SESSION_VIEWS

static void init_usage_screen(lv_obj_t* scr) {
    usage_container = lv_obj_create(scr);
    lv_obj_set_size(usage_container, L.scr_w, L.scr_h);
    lv_obj_set_pos(usage_container, 0, 0);
    lv_obj_set_style_bg_opa(usage_container, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(usage_container, 0, 0);
    lv_obj_set_style_pad_all(usage_container, 0, 0);
    lv_obj_clear_flag(usage_container, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_event_cb(usage_container, global_click_cb, LV_EVENT_CLICKED, NULL);

    lbl_title = lv_label_create(usage_container);
    lv_label_set_text(lbl_title, "Usage");
    lv_obj_set_style_text_font(lbl_title, L.title_font, 0);
    lv_obj_set_style_text_color(lbl_title, COL_TEXT, 0);
    // The nudge balances the corner logo on the left; smaller on small
    // screens where the logo is 40px and the battery icon sits closer.
    lv_obj_align(lbl_title, LV_ALIGN_TOP_MID, L.title_nudge, L.title_y);

    // Usage panels (shown when connected) live in a transparent full-size group
    // so they can be toggled against the pairing hint as one unit.
    usage_group = lv_obj_create(usage_container);
    lv_obj_set_size(usage_group, L.scr_w, L.scr_h);
    lv_obj_set_pos(usage_group, 0, 0);
    lv_obj_set_style_bg_opa(usage_group, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(usage_group, 0, 0);
    lv_obj_set_style_pad_all(usage_group, 0, 0);
    lv_obj_clear_flag(usage_group, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(usage_group, LV_OBJ_FLAG_EVENT_BUBBLE);

    panel_session = make_usage_panel(usage_group, L.content_y, "Current",
                     &lbl_session_pct, &lbl_session_label,
                     &bar_session, &lbl_session_reset);

    // Enterprise-only overlays inside panel_session — hidden until enterprise data arrives
    lbl_session_pct_sym = lv_label_create(panel_session);
    lv_label_set_text(lbl_session_pct_sym, "%");
    lv_obj_set_style_text_font(lbl_session_pct_sym, L.reset_font, 0);
    lv_obj_set_style_text_color(lbl_session_pct_sym, COL_TEXT, 0);
    lv_obj_add_flag(lbl_session_pct_sym, LV_OBJ_FLAG_HIDDEN);

    lbl_spending_desc = lv_label_create(panel_session);
    lv_label_set_text(lbl_spending_desc, "of your monthly budget");
    lv_obj_set_style_text_font(lbl_spending_desc, L.reset_font, 0);
    lv_obj_set_style_text_color(lbl_spending_desc, COL_DIM, 0);
    lv_obj_set_pos(lbl_spending_desc, 0, L.usage_reset_y);
    lv_obj_add_flag(lbl_spending_desc, LV_OBJ_FLAG_HIDDEN);

    lbl_spending_status = lv_label_create(panel_session);
    lv_label_set_text(lbl_spending_status, "");
    lv_obj_set_style_text_font(lbl_spending_status, L.pace_font, 0);
    lv_obj_set_pos(lbl_spending_status, 0, L.usage_reset_y + 20);
    lv_obj_add_flag(lbl_spending_status, LV_OBJ_FLAG_HIDDEN);

    panel_weekly = make_usage_panel(usage_group,
                     L.content_y + L.usage_panel_h + L.usage_panel_gap, "Weekly",
                     &lbl_weekly_pct, &lbl_weekly_label,
                     &bar_weekly, &lbl_weekly_reset);
    // Recolor enabled so enterprise period box can color pace and reset separately
    lv_label_set_recolor(lbl_weekly_reset, true);

    build_pair_group(usage_container);
    build_idle_group(usage_container);

    // Status line — always visible on the usage view. Driven by ui_tick_anim().
    //
    // §2.3's rule ("the ✻ line yields when it has nothing to say": no room on
    // SEVERAL-CHATS, nothing to say once the focused chat is waiting on you)
    // used to be a runtime check, apply_anim_visibility(), because the chat
    // views shared this container. With the chat views moved to their own tab
    // the rule is structural instead: lbl_anim is a child of usage_container
    // only, so it is simply not present on SCREEN_SESSIONS, and there is
    // nothing left for it to yield to here.
    lbl_anim = lv_label_create(usage_container);
    lv_label_set_text(lbl_anim, "");
    lv_obj_set_style_text_font(lbl_anim, L.anim_font, 0);
    lv_obj_set_style_text_color(lbl_anim, COL_ACCENT, 0);
    lv_obj_align(lbl_anim, LV_ALIGN_BOTTOM_MID, 0, L.anim_y);
}

// ======== Tab headers ========

// Pick the largest title font whose text still clears the corner mascot and
// the battery icon. The usage screen gets away with L.title_font everywhere
// because "Usage" is short; the tab names are not, and on the 368-wide panel
// "Settings" in Tiempos 56 runs straight through both corner glyphs. Measuring
// beats another breakpoint: it holds for the five shipping panel widths and
// for whatever a new port turns out to be.
static const lv_font_t* tab_title_font(const char* text) {
    const int logo_w = L.small_icons ? LOGO_SMALL_HEIGHT : LOGO_HEIGHT;
    const int left   = L.margin + logo_w;                    // mascot's right edge
    const int right  = L.scr_w - L.margin - L.batt_w;        // battery's left edge
    const int center = L.scr_w / 2 + L.title_nudge;          // where the label sits
    int half = center - left;
    if (right - center < half) half = right - center;
    const int avail = 2 * (half - 12);                       // breathing room

    const lv_font_t* const chain[] = {
        L.title_font, &font_tiempos_34, &font_styrene_28,
    };
    for (const lv_font_t* f : chain) {
        lv_point_t sz;
        lv_text_get_size(&sz, text, f, 0, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
        if (sz.x <= avail) return f;
    }
    return &font_styrene_28;
}

static lv_obj_t* make_tab_title(lv_obj_t* parent, const char* text) {
    const lv_font_t* f = tab_title_font(text);
    lv_obj_t* t = lv_label_create(parent);
    lv_label_set_text(t, text);
    lv_obj_set_style_text_font(t, f, 0);
    lv_obj_set_style_text_color(t, COL_TEXT, 0);
    // A stepped-down title centers inside the header band the full-size one
    // would have occupied, so every tab's header sits on the same axis.
    const int dy = (lv_font_get_line_height(L.title_font) -
                    lv_font_get_line_height(f)) / 2;
    lv_obj_align(t, LV_ALIGN_TOP_MID, L.title_nudge, L.title_y + dy);
    return t;
}

// ======== Sessions screen (tab) ========

#if BOARD_HAS_SESSION_VIEWS
static void init_sessions_screen(lv_obj_t* scr) {
    sessions_container = lv_obj_create(scr);
    lv_obj_set_size(sessions_container, L.scr_w, L.scr_h);
    lv_obj_set_pos(sessions_container, 0, 0);
    lv_obj_set_style_bg_opa(sessions_container, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(sessions_container, 0, 0);
    lv_obj_set_style_pad_all(sessions_container, 0, 0);
    lv_obj_clear_flag(sessions_container, LV_OBJ_FLAG_SCROLLABLE);
    // Deliberately NO global_click_cb, for the same reason the settings tab has
    // none: this is a reading surface made of card-shaped things, and a stray
    // tap that swapped the list for the splash would be a trap. The card list
    // scrolls; the swipe ring is the way out. (CLAUDE.md's tap-to-splash
    // rationale is the LCD-4's single button, and the LCD-4 has no chat views.)

    // The chat views used to sit under the usage screen's title/clock. They
    // keep a header here — but it names the tab, because with manual
    // navigation the title is the only thing telling you where you are.
    make_tab_title(sessions_container, "Sessions");

    build_session_views(sessions_container);
    lv_obj_add_flag(sessions_container, LV_OBJ_FLAG_HIDDEN);
}
#endif

// ======== Settings screen (tab) ========

// One built row. The screen never hard-codes a setting: it walks the generic
// table in settings.h, so a new setting appears here as soon as it is added
// there — one line in settings.cpp's SPECS[] and one enum member, no edit
// needed in this file.
struct SettingsRowUi {
    lv_obj_t*    panel;
    lv_obj_t*    value;
    setting_id_t id;
};
static SettingsRowUi set_rows[SETTING_COUNT];
static uint8_t       set_row_count = 0;

// Which rows this board can act on. board_caps() is the arbiter for all of
// them — a speaker is as much a runtime fact as a panel that fits chat cards,
// and shared code cannot see any board.h, so a compile-time guess here would
// show a dead Sound row on the four ports whose sound_hal_play_reset() no-ops.
// No board names here.
static bool setting_row_visible(setting_id_t id) {
    switch (id) {
    case SETTING_SOUND:     return board_caps().has_sound;
    case SETTING_VOLUME:    return board_caps().has_sound;
    case SETTING_AUTO_JUMP: return board_caps().has_session_views;
    default:                return true;
    }
}

static void settings_row_paint(SettingsRowUi* r) {
    SettingRow s;
    if (!settings_get_row((uint8_t)r->id, &s)) return;
    set_label_if_changed(r->value, s.value_text);
    // A boolean reads as a state, so it takes the accent when it is on and
    // recedes to the dim tier when it is off. A stepped or named value is never
    // "off" in that sense — it stays primary text.
    const lv_color_t want =
        s.kind == SETTING_KIND_BOOL ? (s.on ? COL_ACCENT : COL_DIM) : COL_TEXT;
    // Only on a real change: lv_obj_set_style_text_color() invalidates the
    // object unconditionally, and settings_refresh() runs every tick while this
    // tab is up — unguarded, that re-flushes five chips forever on a screen
    // that is static 99.9% of the time (a permanent cost on the C6 boards).
    if (lv_color_to_u32(lv_obj_get_style_text_color(r->value, LV_PART_MAIN))
        != lv_color_to_u32(want))
        lv_obj_set_style_text_color(r->value, want, 0);
}

static void settings_refresh(void) {
    for (uint8_t i = 0; i < set_row_count; i++) settings_row_paint(&set_rows[i]);
}

static void settings_row_click_cb(lv_event_t* e) {
    if (s_gesture_used) return;   // the press was a swipe, not a tap
    SettingsRowUi* r = (SettingsRowUi*)lv_event_get_user_data(e);
    if (!r) return;
    settings_activate(r->id);
    settings_row_paint(r);
}

static void init_settings_screen(lv_obj_t* scr) {
    settings_container = lv_obj_create(scr);
    lv_obj_set_size(settings_container, L.scr_w, L.scr_h);
    lv_obj_set_pos(settings_container, 0, 0);
    lv_obj_set_style_bg_opa(settings_container, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(settings_container, 0, 0);
    lv_obj_set_style_pad_all(settings_container, 0, 0);
    lv_obj_clear_flag(settings_container, LV_OBJ_FLAG_SCROLLABLE);
    // Deliberately NO global_click_cb here: every row is a tap target, so a
    // stray tap that also toggled the splash would be a trap. Swipe out.

    make_tab_title(settings_container, "Settings");

    // Text budget: the row's inner width less the widest the value chip can
    // ever get ("100%" plus its pill padding) and a gap. Without this the
    // subtitle runs under the chip on the narrower large-layout panels — the
    // 410-wide 2.06 is only 70 px of slack away from the 480 boards.
    int pill_w;
    {
        lv_point_t sz;
        lv_text_get_size(&sz, "100%", L.set_value_font, 0, 0,
                         LV_COORD_MAX, LV_TEXT_FLAG_NONE);
        pill_w = sz.x + 2 * L.pill_pad_x;
    }
    const int text_w = L.content_w - 2 * L.panel_pad_x - pill_w - 12;

    // Scrollable row column below the title, mirroring the chat list on the
    // sessions tab: vertical only, so a horizontal swipe is still delivered as
    // a gesture and keeps changing tabs. The AUTO scrollbar is the standing cue
    // that there are more rows, and it disappears when they all fit.
    set_rows_cont = lv_obj_create(settings_container);
    lv_obj_set_pos(set_rows_cont, 0, L.content_y);
    lv_obj_set_size(set_rows_cont, L.scr_w, L.scr_h - L.content_y);
    lv_obj_set_style_bg_opa(set_rows_cont, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(set_rows_cont, 0, 0);
    lv_obj_set_style_pad_all(set_rows_cont, 0, 0);
    lv_obj_add_flag(set_rows_cont, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_scroll_dir(set_rows_cont, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(set_rows_cont, LV_SCROLLBAR_MODE_AUTO);
    lv_obj_set_style_bg_color(set_rows_cont, COL_DIM, LV_PART_SCROLLBAR);
    lv_obj_set_style_bg_opa(set_rows_cont, LV_OPA_50, LV_PART_SCROLLBAR);
    lv_obj_set_style_width(set_rows_cont, 4, LV_PART_SCROLLBAR);
    lv_obj_set_style_radius(set_rows_cont, LV_RADIUS_CIRCLE, LV_PART_SCROLLBAR);
    lv_obj_set_style_pad_right(set_rows_cont, 6, LV_PART_SCROLLBAR);
    lv_obj_set_style_pad_top(set_rows_cont, 6, LV_PART_SCROLLBAR);
    lv_obj_set_style_pad_bottom(set_rows_cont, 6, LV_PART_SCROLLBAR);
    lv_obj_add_flag(set_rows_cont, LV_OBJ_FLAG_EVENT_BUBBLE);

    int y = 0;
    set_row_count = 0;
    for (uint8_t i = 0; i < settings_count(); i++) {
        SettingRow s;
        if (!settings_get_row(i, &s)) continue;
        if (!setting_row_visible(s.id)) continue;

        SettingsRowUi* r = &set_rows[set_row_count];
        r->id = s.id;
        r->panel = make_panel(set_rows_cont, L.margin, y, L.content_w, L.set_row_h);
        lv_obj_set_style_pad_top(r->panel, L.set_row_pad_y, 0);
        lv_obj_set_style_pad_bottom(r->panel, L.set_row_pad_y, 0);
        // The row owns its click; nothing above it needs to hear about it.
        lv_obj_clear_flag(r->panel, LV_OBJ_FLAG_EVENT_BUBBLE);
        lv_obj_add_event_cb(r->panel, settings_row_click_cb, LV_EVENT_CLICKED, r);

        lv_obj_t* lbl = lv_label_create(r->panel);
        lv_label_set_text(lbl, s.label);
        lv_obj_set_style_text_font(lbl, L.set_label_font, 0);
        lv_obj_set_style_text_color(lbl, COL_TEXT, 0);
        lv_label_set_long_mode(lbl, LV_LABEL_LONG_DOT);
        lv_obj_set_width(lbl, text_w);
        lv_obj_set_height(lbl, lv_font_get_line_height(L.set_label_font));

        if (L.set_detail_font) {
            lv_obj_align(lbl, LV_ALIGN_TOP_LEFT, 0, 0);
            lv_obj_t* det = lv_label_create(r->panel);
            lv_label_set_text(det, s.detail);
            lv_obj_set_style_text_font(det, L.set_detail_font, 0);
            lv_obj_set_style_text_color(det, COL_DIM, 0);
            lv_label_set_long_mode(det, LV_LABEL_LONG_DOT);
            lv_obj_set_width(det, text_w);
            lv_obj_set_height(det, lv_font_get_line_height(L.set_detail_font));
            lv_obj_align(det, LV_ALIGN_TOP_LEFT, 0,
                         lv_font_get_line_height(L.set_label_font) + 2);
        } else {
            // 240x240: no subtitle fits, so the label centers on its own.
            lv_obj_align(lbl, LV_ALIGN_LEFT_MID, 0, 0);
        }

        // Value chip — the quota pill's treatment at the settings text size,
        // so "On"/"Off"/"78%" reads as the control rather than as a caption.
        r->value = lv_label_create(r->panel);
        lv_label_set_text(r->value, s.value_text);
        lv_obj_set_style_text_font(r->value, L.set_value_font, 0);
        lv_obj_set_style_bg_color(r->value, COL_BAR_BG, 0);
        lv_obj_set_style_bg_opa(r->value, LV_OPA_COVER, 0);
        lv_obj_set_style_radius(r->value, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_pad_left(r->value, L.pill_pad_x, 0);
        lv_obj_set_style_pad_right(r->value, L.pill_pad_x, 0);
        lv_obj_set_style_pad_top(r->value, L.pill_pad_y, 0);
        lv_obj_set_style_pad_bottom(r->value, L.pill_pad_y, 0);
        lv_obj_align(r->value, LV_ALIGN_RIGHT_MID, 0, 0);

        settings_row_paint(r);
        set_row_count++;
        y += L.set_row_h + L.set_row_gap;
    }

    lv_obj_add_flag(settings_container, LV_OBJ_FLAG_HIDDEN);
}

// ======== Tab navigation ========

// The swipe ring. Order is fixed; membership is not — a screen the board can't
// host never enters the list, which is what keeps a swipe from landing on an
// empty tab (requirement 1). The splash leads because it is the boot screen.
static void build_tab_order(void) {
    tab_count = 0;
    tab_order[tab_count++] = SCREEN_SPLASH;
    tab_order[tab_count++] = SCREEN_USAGE;
#if BOARD_HAS_SESSION_VIEWS
    if (board_caps().has_session_views && sessions_container)
        tab_order[tab_count++] = SCREEN_SESSIONS;
#endif
    tab_order[tab_count++] = SCREEN_SETTINGS;
}

void ui_next_tab(int dir) {
    if (tab_count == 0) return;
    int idx = 0;
    for (uint8_t i = 0; i < tab_count; i++)
        if (tab_order[i] == current_screen) { idx = i; break; }
    idx = (idx + dir + (int)tab_count) % (int)tab_count;   // wraps both ways
    show_screen(tab_order[idx], true);
}

// Every press starts a clean slate: the swipe flag is cleared here (see its
// declaration for why not in the click handler), and the auto-jump loses its
// claim on the screen the moment a finger lands on the panel.
static void screen_press_cb(lv_event_t* e) {
    (void)e;
    s_gesture_used = false;
    s_auto_jumped  = false;
}

// LVGL raises this once per press, mid-drag, after gesture_min_distance px.
// It reaches us through the indev rather than an object so it cannot be
// swallowed by whatever happens to be under the finger.
static void screen_gesture_cb(lv_event_t* e) {
    (void)e;
    lv_indev_t* indev = lv_indev_active();
    if (!indev) return;
    // Any gesture suppresses the click that LVGL still sends on release —
    // including a vertical one, which must not toggle the splash either.
    s_gesture_used = true;
    switch (lv_indev_get_gesture_dir(indev)) {
    case LV_DIR_LEFT:  ui_next_tab(+1); break;
    case LV_DIR_RIGHT: ui_next_tab(-1); break;
    default: break;   // vertical gestures are unassigned
    }
}

// ======== Public API ========

void ui_init(void) {
    compute_layout(board_caps());

    lv_obj_t* scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, COL_BG, 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    // Screens are scrollable by default. Nothing here scrolls, and a live
    // scroll would suppress gesture detection entirely (LVGL bails out of
    // indev_gesture() as soon as it has a scroll object), so take it away.
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

#ifndef BOARD_HAS_PSRAM
    // Static corner mascot (see clawd_still.h) — the animated one needs PSRAM.
    if (L.small_icons) init_icon_dsc_rgb565a8(&logo_dsc, CLAWD_STILL_SMALL_W, CLAWD_STILL_SMALL_H, clawd_still_small_data);
    else               init_icon_dsc_rgb565a8(&logo_dsc, CLAWD_STILL_W, CLAWD_STILL_H, clawd_still_data);
#endif
    init_battery_icons();

    init_usage_screen(scr);
#if BOARD_HAS_SESSION_VIEWS
    if (board_caps().has_session_views) init_sessions_screen(scr);
#endif
    init_settings_screen(scr);
    build_tab_order();
    splash_init(scr);

    if (splash_get_root()) {
        lv_obj_add_event_cb(splash_get_root(), global_click_cb, LV_EVENT_CLICKED, NULL);
    }

    // Swipe navigation. The handlers hang off the input device, not off an
    // object: LVGL sends LV_EVENT_PRESSED / LV_EVENT_GESTURE to the indev's
    // own event list regardless of which widget the finger landed on, so no
    // container, card or label can quietly eat a swipe. (Gestures still need
    // a hit object to originate from, which every screen here provides — the
    // full-bleed tab containers are clickable by default.) main.cpp creates
    // the indev before calling us.
    for (lv_indev_t* indev = lv_indev_get_next(NULL); indev;
         indev = lv_indev_get_next(indev)) {
        if (lv_indev_get_type(indev) != LV_INDEV_TYPE_POINTER) continue;
        lv_indev_add_event_cb(indev, screen_press_cb, LV_EVENT_PRESSED, NULL);
        lv_indev_add_event_cb(indev, screen_gesture_cb, LV_EVENT_GESTURE, NULL);
    }

    // Corner mascot in the old logo slot. The still Clawd is shorter than the
    // 80/40 px slot the spark logo used; center it vertically in that slot.
    {
        const int slot  = L.small_icons ? LOGO_SMALL_HEIGHT : LOGO_HEIGHT;
        const int art_h = L.small_icons ? CLAWD_STILL_SMALL_H : CLAWD_STILL_H;
        const int top   = L.logo_y + (slot - art_h) / 2;
#ifdef BOARD_HAS_PSRAM
        // Animated: idles, does acts, and takes walk-off/lurk trips.
        splash_mascot_create(scr, L.margin, top + art_h, L.small_icons ? 2 : 3);
#else
        logo_img = lv_image_create(scr);
        lv_image_set_src(logo_img, &logo_dsc);
        lv_obj_set_pos(logo_img, L.margin, top);
#endif
    }

    battery_img = lv_image_create(scr);
    lv_image_set_src(battery_img, &battery_dscs[0]);
    lv_obj_set_pos(battery_img, L.scr_w - L.batt_w - L.margin, L.batt_y);
    // Boards without battery telemetry never show the indicator (per the HAL
    // contract; previously every board drew the empty-battery glyph).
    if (!board_caps().has_battery) {
        lv_obj_del(battery_img);
        battery_img = nullptr;
    }
}

void ui_update(const UsageData* data) {
    if (!data->valid) return;
    data_ok = data->ok;
    if (!data->ok) return;          // a {"ok":false} "no data" beat → fall through to idle, keep last numbers
    last_data_ms = lv_tick_get();   // a real usage update just landed
    data_received = true;

    if (data->clock_epoch > 0) {    // daemon supplied wall-clock time → drive the title clock
        clock_base_epoch = data->clock_epoch;
        clock_base_ms = last_data_ms;
        clock_fmt = data->clock_fmt;
    } else if (clock_base_epoch != 0) {   // clock turned off daemon-side → revert title to "Usage"
        clock_base_epoch = 0;
        clock_last_min = -1;
        lv_label_set_text(lbl_title, "Usage");
    }

    int s_pct = (int)(data->session_pct + 0.5f);

    if (data->enterprise) {
        // Spending box: big number-only label + small "%" symbol + desc + pace
        lv_obj_set_style_text_font(lbl_session_pct, L.ent_pct_font, 0);
        lv_label_set_text(lbl_session_label, "Spending");
        lv_obj_add_flag(lbl_session_reset, LV_OBJ_FLAG_HIDDEN);
        lv_obj_clear_flag(lbl_session_pct_sym, LV_OBJ_FLAG_HIDDEN);
        lv_obj_clear_flag(lbl_spending_desc,   LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(lbl_spending_status,   LV_OBJ_FLAG_HIDDEN);
        if (panel_weekly) lv_obj_clear_flag(panel_weekly, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_set_style_text_font(lbl_session_pct, L.pct_font, 0);
        lv_label_set_text(lbl_session_label, "Current");
        lv_obj_clear_flag(lbl_session_reset, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(lbl_session_pct_sym, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(lbl_spending_desc,   LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(lbl_spending_status, LV_OBJ_FLAG_HIDDEN);
        if (panel_weekly) lv_obj_clear_flag(panel_weekly, LV_OBJ_FLAG_HIDDEN);
    }

    char buf[48];

    // Pace vars used in both enterprise blocks below
    const char* pace_text = "Under pace";
    lv_color_t  pace_color = COL_GREEN;
    const char* pace_hex   = "788c5d";   // matches THEME_GREEN
    if (data->session_pct > (float)data->time_pct + 15.0f) {
        pace_text = "Over pace";  pace_color = COL_RED;   pace_hex = "c0392b";
    } else if (data->session_pct > (float)data->time_pct - 15.0f) {
        pace_text = "On pace";    pace_color = COL_AMBER; pace_hex = "d97757";
    }

    if (data->enterprise) {
        lv_label_set_text_fmt(lbl_session_pct, "%d", s_pct);
        lv_obj_align_to(lbl_session_pct_sym, lbl_session_pct,
                        LV_ALIGN_OUT_RIGHT_TOP, 4, 12);
    } else {
        lv_label_set_text_fmt(lbl_session_pct, "%d%%", s_pct);
        format_reset_time(data->session_reset_mins, buf, sizeof(buf));
        lv_label_set_text(lbl_session_reset, buf);
    }

    lv_bar_set_value(bar_session, s_pct, LV_ANIM_ON);
    lv_obj_set_style_bg_color(bar_session, pct_color(data->session_pct), LV_PART_INDICATOR);

    if (data->enterprise) {
        // Period box: time % + dynamic pace color + "Resets <date>" label
        lv_label_set_text(lbl_weekly_label, "Period");
        lv_label_set_text_fmt(lbl_weekly_pct, "%d%%", data->time_pct);
        lv_bar_set_value(bar_weekly, data->time_pct, LV_ANIM_ON);
        lv_color_t bar_pace = (data->session_pct <= (float)data->time_pct) ? COL_GREEN :
                              (data->session_pct <= (float)data->time_pct + 15.0f) ? COL_AMBER :
                              COL_RED;
        lv_obj_set_style_bg_color(bar_weekly, bar_pace, LV_PART_INDICATOR);
        snprintf(buf, sizeof(buf), "#%s %s# - #faf9f5 Resets %s#",
                 pace_hex, pace_text, data->reset_date);
        lv_label_set_text(lbl_weekly_reset, buf);
    } else {
        int w_pct = (int)(data->weekly_pct + 0.5f);
        lv_label_set_text_fmt(lbl_weekly_pct, "%d%%", w_pct);
        lv_bar_set_value(bar_weekly, w_pct, LV_ANIM_ON);
        lv_obj_set_style_bg_color(bar_weekly, pct_color(data->weekly_pct), LV_PART_INDICATOR);
        format_reset_time(data->weekly_reset_mins, buf, sizeof(buf));
        lv_label_set_text(lbl_weekly_reset, buf);
    }

#if BOARD_HAS_SESSION_VIEWS
    // The chat views carry their own mini quota widgets (§1.3/§1.4) — keep
    // them in step with the panels above.
    if (board_caps().has_session_views) {
        s_usage_cache = *data;
        session_quota_refresh();
    }
#endif
}

// The usage screen's view resolver (§2.1) — run every tick; nothing else
// chooses its sub-view. Pairing hint (BLE down), the idle "Zzz" screen
// (connected but data stale), or the live quota panels (RESTING).
//
// It used to also auto-select ONE-CHAT / SEVERAL-CHATS, including an override
// that pinned a waiting chat over everything else. Those belong to the
// sessions tab now (update_session_view), and the pin became the auto-jump —
// a usage screen that silently turned into a chat list is exactly the
// no-navigation model the tabs replace. Only re-lays-out on an actual change.
static void update_view_state(void) {
    if (!usage_group || !pair_group || !idle_group) return;
    const uint32_t now = lv_tick_get();
    const bool fresh = data_received && (now - last_data_ms) < DATA_FRESH_MS;
    int v;
    if (!s_ble_connected)  v = 0;  // pairing hint
    else if (!fresh)       v = 1;  // idle / Zzz
    else                   v = 2;  // RESTING — live quota panels
    if (v == view_state) return;
    view_state = v;
    lv_obj_add_flag(pair_group, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(idle_group, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(usage_group, LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(v == 0 ? pair_group : v == 1 ? idle_group : usage_group,
                      LV_OBJ_FLAG_HIDDEN);
}

void ui_tick_anim(void) {
    // Both scroll regions get their rubber band capped here rather than in
    // their own scroll events — see clamp_overscroll for why that matters.
    // The card list only exists where the session views are compiled in; the
    // settings rows are on every board.
#if BOARD_HAS_SESSION_VIEWS
    clamp_overscroll(cards_cont);
#endif
    clamp_overscroll(set_rows_cont);

    // Both resolvers run on every tick regardless of the visible tab, so a
    // swipe arrives at a sub-view that is already correct rather than one
    // frame stale — and the sessions tab's linger timer keeps expiring while
    // the user is somewhere else.
    update_view_state();
    update_session_view();
    sessions_tick();

    // Brightness is also reachable from the PWR button, so the settings rows
    // are repainted from the module rather than only where they were tapped.
    if (current_screen == SCREEN_SETTINGS) settings_refresh();

    if (current_screen != SCREEN_USAGE) return;
    if (view_state == 1) splash_mini_tick();   // animate the sleeping creature on the idle screen

    uint32_t now = lv_tick_get();

    // Clock format. Auto (the default) follows the daemon's hint exactly as the
    // firmware did before this row existed, so an upgrade never reformats
    // anyone's clock; 24h and 12h are overrides that win in both directions,
    // including over a host that reports 24. A change has to force a re-render
    // — the title is only rewritten when the minute rolls over.
    const clock_pref_t pref = settings_clock_pref();
    const int fmt = pref == CLOCK_PREF_24H ? 24
                  : pref == CLOCK_PREF_12H ? 12
                  : clock_fmt;
    static int last_fmt = -1;
    if (fmt != last_fmt) {
        last_fmt = fmt;
        clock_last_min = -1;
    }

    // Title clock: once the daemon has sent wall-clock time, replace "Usage" with
    // the live time, advanced locally so it ticks every minute between payloads.
    if (clock_base_epoch > 0) {
        time_t cur = (time_t)(clock_base_epoch + (now - clock_base_ms) / 1000);
        struct tm tmv;
        gmtime_r(&cur, &tmv);   // epoch is already local wall-clock → gmtime keeps it as-is
        if (tmv.tm_min != clock_last_min) {   // only rewrite the title when the minute changes
            clock_last_min = tmv.tm_min;
            char tbuf[12];
            if (fmt == 12) {
                int h12 = tmv.tm_hour % 12;
                if (h12 == 0) h12 = 12;
                snprintf(tbuf, sizeof(tbuf), "%d:%02d %s", h12, tmv.tm_min,
                         tmv.tm_hour < 12 ? "AM" : "PM");
            } else {
                snprintf(tbuf, sizeof(tbuf), "%02d:%02d", tmv.tm_hour, tmv.tm_min);
            }
            lv_label_set_text(lbl_title, tbuf);
        }
    }

    if (now - anim_msg_start >= ANIM_MSG_MS) {
        anim_msg_idx = (anim_msg_idx + 1) % ANIM_MSG_COUNT;
        anim_msg_start = now;
    }

    if (now - anim_last_ms < spinner_ms[anim_spinner_idx]) return;
    anim_last_ms = now;
    anim_phase = (anim_phase + 1) % SPINNER_PHASES;
    anim_spinner_idx = (anim_phase < SPINNER_COUNT) ? anim_phase
                                                    : (SPINNER_PHASES - anim_phase);

    // Status text by priority. Whimsical messages only when connected & settled.
    const char* text;
    if (!s_ble_connected) {
        text = "Waiting";              // advertising / waiting for a host connection
    } else if (view_state == 1) {      // idle — alternate so it reads as alive AND data-less
        text = (anim_msg_idx & 1) ? "No data" : "Listening";
    } else if (now - connected_at_ms < 5000) {
        text = "Connected";
    } else {
        text = anim_messages[anim_msg_idx];
    }

    // All states share the whimsical style: "<glyph> <Title-case word>…"
    static char buf[80];
    snprintf(buf, sizeof(buf), "%s %s\xE2\x80\xA6",
             spinner_frames[anim_spinner_idx], text);
    lv_label_set_text(lbl_anim, buf);
}

static screen_t prev_non_splash_screen = SCREEN_USAGE;
static void apply_battery_visibility(void) {
    if (!battery_img) return;
    if (current_screen == SCREEN_SPLASH) lv_obj_add_flag(battery_img, LV_OBJ_FLAG_HIDDEN);
    else                                  lv_obj_clear_flag(battery_img, LV_OBJ_FLAG_HIDDEN);
}

static void global_click_cb(lv_event_t* e) {
    (void)e;
    // A swipe also produces a CLICKED on release; that must not toggle the
    // splash on top of changing tabs.
    if (s_gesture_used) return;
    if (current_screen == SCREEN_SPLASH) show_screen(prev_non_splash_screen, true);
    else                                  show_screen(SCREEN_SPLASH, true);
}

// The single place that shows and hides tab containers — every entry point
// (swipe, tap, button, auto-jump) funnels through here.
static void show_screen(screen_t screen, bool manual) {
    // A board without the chat views has no sessions container; nothing should
    // ever ask for it (the tab ring skips it), but a stray request lands on the
    // usage screen rather than a blank panel.
    if (screen == SCREEN_SESSIONS && !sessions_container) screen = SCREEN_USAGE;
    // Same for settings: if LVGL's pool ran dry while building it, a swipe onto
    // the tab must degrade to the usage screen, not dereference NULL.
    if (screen == SCREEN_SETTINGS && !settings_container) screen = SCREEN_USAGE;

    // The user just chose this screen — the auto-jump no longer has a claim on
    // it, so the return trip won't move it back under them.
    if (manual) s_auto_jumped = false;

    // Leaving the sessions tab closes the action bar. Coming back to a
    // selection made minutes ago, about a card the host may have replaced
    // since, is a bar whose buttons no longer mean what they said.
    if (screen != SCREEN_SESSIONS) card_deselect();

    lv_obj_add_flag(usage_container, LV_OBJ_FLAG_HIDDEN);
    if (sessions_container) lv_obj_add_flag(sessions_container, LV_OBJ_FLAG_HIDDEN);
    if (settings_container) lv_obj_add_flag(settings_container, LV_OBJ_FLAG_HIDDEN);
    splash_hide();

    switch (screen) {
    case SCREEN_SPLASH:   splash_show(); break;
    case SCREEN_USAGE:    lv_obj_clear_flag(usage_container, LV_OBJ_FLAG_HIDDEN); break;
    case SCREEN_SESSIONS: lv_obj_clear_flag(sessions_container, LV_OBJ_FLAG_HIDDEN); break;
    case SCREEN_SETTINGS: lv_obj_clear_flag(settings_container, LV_OBJ_FLAG_HIDDEN); break;
    default: break;
    }

    splash_mascot_set_visible(screen != SCREEN_SPLASH);
    if (logo_img) {
        if (screen == SCREEN_SPLASH) lv_obj_add_flag(logo_img, LV_OBJ_FLAG_HIDDEN);
        else                          lv_obj_clear_flag(logo_img, LV_OBJ_FLAG_HIDDEN);
    }

    if (screen != SCREEN_SPLASH) prev_non_splash_screen = screen;
    current_screen = screen;
    apply_battery_visibility();
    if (screen == SCREEN_SETTINGS) settings_refresh();
}

void ui_show_screen(screen_t screen) { show_screen(screen, true); }

void ui_toggle_splash(void) {
    if (current_screen == SCREEN_SPLASH) show_screen(prev_non_splash_screen, true);
    else                                  show_screen(SCREEN_SPLASH, true);
}

screen_t ui_get_current_screen(void) {
    return current_screen;
}

void ui_update_ble_status(ble_state_t state, const char* name, const char* mac) {
    (void)name; (void)mac;
    bool was_connected = s_ble_connected;
    s_ble_connected = (state == BLE_STATE_CONNECTED);

    if (s_ble_connected && !was_connected) connected_at_ms = lv_tick_get();
    if (!s_ble_connected && was_connected) sessions_link_lost();
    // pair / idle / usage — picked from connection + data freshness.
    update_view_state();
}

#if BOARD_HAS_SESSION_VIEWS
void ui_update_sessions(const SessionList* list) {
    if (!list || !focus_group || !board_caps().has_session_views) return;

    const uint8_t prev_count = s_live_count;
    // Rows the owner has already tapped away never reach the rest of this
    // function -- not the notify set (a cleared card must not yank the panel
    // back to this tab on the next payload), not the card pool, not the
    // resolver. From here down, s_shown IS the list.
    filter_dismissed(list, &s_shown);
    s_live_count = s_shown.count;
    note_notify_set(&s_shown);

    if (s_shown.count == 0) {
        if (prev_count > 0 && (session_view == 1 || session_view == 2)) {
            // The last live chat disappeared → hold the current view for
            // CHAT_LINGER_MS (§2.1). Cards keep their final content, but the
            // waiting treatment is dropped: a chat that ended can't need you,
            // and the pulse must keep meaning "come here".
            s_chats_linger = true;
            s_chats_gone_ms = lv_tick_get();
            s_linger_view = session_view;
            s_focus_waiting = false;
            if (focus_card.dot) {
                lv_obj_set_style_bg_opa(focus_card.dot, LV_OPA_COVER, 0);
                lv_obj_set_style_text_opa(focus_card.lbl_state, LV_OPA_COVER, 0);
            }
            for (auto& c : chat_cards) {
                c.waiting = false;
                if (c.used) {
                    lv_obj_set_style_bg_opa(c.dot, LV_OPA_COVER, 0);
                    lv_obj_set_style_text_opa(c.lbl_state, LV_OPA_COVER, 0);
                }
            }
        }
        update_session_view();
        maybe_auto_jump();   // may be a falling edge: hand the screen back
        return;
    }

    s_chats_linger = false;
    sessions_render();
    // Last, so the cards are already rendered and the sub-view already
    // resolved when the tab switches — the user arrives at a finished screen,
    // and chats_set_content's "am I visible?" animate test saw the truth.
    maybe_auto_jump();
}
#else
// Boards without session views compile to today's behavior; the call sites
// in main.cpp are gated too, so this stub only keeps the public API total.
void ui_update_sessions(const SessionList* list) { (void)list; }
#endif

void ui_update_battery(int percent, bool charging) {
    if (!battery_img) return;
    int idx;
    if (charging) {
        idx = 4;
    } else if (percent < 0) {
        idx = 0;
    } else if (percent <= 10) {
        idx = 0;
    } else if (percent <= 35) {
        idx = 1;
    } else if (percent <= 75) {
        idx = 2;
    } else {
        idx = 3;
    }
    lv_image_set_src(battery_img, &battery_dscs[idx]);
    apply_battery_visibility();
}
