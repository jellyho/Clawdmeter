#pragma once
#include <stdint.h>

// ---------------------------------------------------------------------------
// User settings: the value model + NVS persistence behind the Settings tab.
//
// No UI lives here — the settings screen loops over the generic row table
// below (settings_count() / settings_get_row()) and renders whatever it finds,
// so adding a setting is one line in SPECS[] plus one enum member, not a new
// getter + a new hard-coded row.
//
// Board-agnostic by design: every board carries every value (a speaker-less
// board still has a sound_enabled flag). The SCREEN decides which rows to hide
// using board_caps() — this module never looks at the board.
//
// Persistence follows brightness.cpp exactly: Arduino Preferences, namespace
// "clawdmeter", opened read-only for the one load in settings_init() and
// read-write for the moment of each write. Every setter persists immediately —
// toggles are rare and a desk device can lose power at any time.
//
// Storage is deliberately restricted to unsigned chars (putUChar/getUChar):
// that is all the simulator's in-memory Preferences shim implements, and it is
// enough for booleans and small enums.
// ---------------------------------------------------------------------------

// Ids are contiguous from 0 and double as the row order on screen, so
// settings_get_row(i) returns the row whose id == (setting_id_t)i.
enum setting_id_t : uint8_t {
    SETTING_SOUND = 0,     // gates the session-reset chime
    SETTING_VOLUME,        // how loud that chime is
    SETTING_AUTO_JUMP,     // jump to the sessions tab when a session needs you
    SETTING_SPLASH_BOOT,   // boot to the splash instead of the usage screen
    SETTING_CLOCK,         // title clock: follow the host, or force 24h / 12h
    SETTING_BRIGHTNESS,    // delegates to brightness.{h,cpp} — not stored here
    SETTING_BROADCAST,     // one-shot: tell the fleet the standing rules
    SETTING_COUNT,
};

enum setting_kind_t : uint8_t {
    SETTING_KIND_BOOL,     // on/off: render "On"/"Off", activate = toggle
    SETTING_KIND_CHOICE,   // small named enum: render the choice, activate = next
    SETTING_KIND_STEP,     // externally-owned stepped value (brightness)
    // A row that DOES something instead of holding something. It stores no
    // value, so there is nothing to render on the right except the verb — and
    // for a couple of seconds after it fires, an acknowledgement, because a
    // row that looks identical before and after a tap is a row you press
    // twice. The doing itself belongs to whoever owns the action (ui.cpp
    // sends it over BLE); this module only knows that it happened and when.
    SETTING_KIND_ACTION,
};

// Stamp an action row as just-fired, so it can say so. Called by whatever
// actually performed the action, and only when it succeeded.
void settings_note_action(setting_id_t id);

// Clock format preference. AUTO defers to the daemon's hint (UsageData.clock_fmt)
// exactly as the firmware did before there was a setting, so nobody's clock
// changes on upgrade; the other two are user overrides in both directions —
// forcing 12-hour has to work even when the host reports 24.
enum clock_pref_t : uint8_t {
    CLOCK_PREF_AUTO = 0,
    CLOCK_PREF_24H,
    CLOCK_PREF_12H,
};

// One renderable row. Everything the screen needs to draw a line is here;
// value_text is an inline buffer (not a pointer) so a caller may keep a copy
// of the struct for as long as it likes.
struct SettingRow {
    setting_id_t   id;
    setting_kind_t kind;
    const char*    label;        // short row title, e.g. "Sound"
    const char*    detail;       // one-line explanation, may be shown as a subtitle
    bool           on;           // BOOL: the value. Others: true unless at the floor.
    int16_t        value;        // BOOL: 0/1. CHOICE: the index. STEP: the raw value.
    int16_t        value_min;    // inclusive range for a bar/meter.
    int16_t        value_max;
    char           value_text[8];// ready to render: "On" / "Off" / "Auto" / "78%"
};

// Load every value from NVS and apply. Call once in setup(). Brightness is
// owned by brightness_init() — call that too (main.cpp already does); this
// function does not load or apply it.
void settings_init(void);

// ---- Typed accessors: what the rest of the firmware calls ----
bool         settings_sound_enabled(void);      // default true
uint8_t      settings_volume(void);             // codec level 0..100, default 75 (0 dB)
bool         settings_auto_jump_enabled(void);  // default true
bool         settings_splash_boot(void);        // default true
clock_pref_t settings_clock_pref(void);         // default CLOCK_PREF_AUTO

// ---- Generic access: what the settings screen calls ----
uint8_t settings_count(void);                              // == SETTING_COUNT
bool    settings_get_row(uint8_t index, SettingRow* out);  // false if out of range

bool settings_get_bool(setting_id_t id);            // false for non-bool ids
void settings_set_bool(setting_id_t id, bool on);   // persists; no-op for non-bool

// "The user activated this row": toggles a BOOL, advances a CHOICE to its next
// value (wrapping), advances a STEP through its owner's ramp (brightness cycles
// through brightness.cpp, which persists itself). Safe to call for any id.
void settings_activate(setting_id_t id);

// Restore every value owned by this module to its default and persist.
// Brightness is not touched (it is not ours to reset).
void settings_reset_defaults(void);
