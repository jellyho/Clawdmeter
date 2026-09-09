#include "settings.h"
#include "brightness.h"
#include <Preferences.h>
#include <Arduino.h>
#include <stdio.h>

// NVS namespace shared with brightness.cpp ("brt_idx" lives there too) — one
// namespace for everything the user can change on the device. Keys stay short:
// NVS caps them at 15 characters.
#define SETTINGS_NS "clawdmeter"

// Choice labels. One array per SETTING_KIND_CHOICE row, indexed by the stored
// value; entry 0 is the default and must be the "don't override anything"
// option wherever the firmware had a behaviour before the setting existed.
static const char* const CLOCK_CHOICES[] = { "Auto", "24h", "12h" };
static const char* const VOLUME_CHOICES[] = { "Low", "Med", "High" };

// ES8311 output-register levels behind those three labels. Med is the value
// every board's ChimeConfig used to hard-code, so an upgrade sounds the same.
// The three steps, as ES8311 volume percentages -- and they are NOT the evenly
// spaced 40/65/90 they look like they should be, because that scale is not a
// loudness. es8311_voice_volume_set turns a percentage into DAC register 0x32
// as `reg = pct * 256 / 100 - 1`, and the register is HALF A DECIBEL per step
// with 0 dB at 0xBF (191). So the old 40/65/90 was -45 dB, -13 dB and +19 dB:
// two settings barely audible, one of them a 32 dB jump into digital gain the
// bell has no headroom for -- its peak sits at -7.0 dBFS, so +19 dB clipped it
// by twelve.
//
//   pct -> reg -> dB          70 -> 179 -> -6.5
//                             75 -> 191 ->  0.0   (unity: what the file holds)
//                             80 -> 203 -> +6.0   (peak lands at -1 dBFS)
//
// Even 6 dB steps, and the loudest one still does not clip. Louder than this
// is an amplifier question, not a number question: every dB past +7 is one the
// bell does not have.
static const uint8_t VOLUME_LEVELS[] = { 70, 75, 80 };

// The single source of truth for the settings surface. One row per entry, in
// screen order; adding a setting is a line here plus a member in setting_id_t.
// key == NULL means the value is owned by another module (brightness) and is
// neither loaded nor stored by this one.
struct SettingSpec {
    setting_kind_t     kind;
    const char*        key;      // NVS key, or NULL if not ours to persist
    uint8_t            def;      // default raw value
    uint8_t            nvals;    // number of distinct values (BOOL: 2)
    const char* const* choices;  // nvals labels for CHOICE, NULL otherwise
    const char*        label;
    const char*        detail;
};

static const SettingSpec SPECS[] = {
    { SETTING_KIND_BOOL,   "snd_en",  1, 2, NULL, "Sound",
      "Chime when the session limit resets" },
    { SETTING_KIND_CHOICE, "snd_vol", 1, 3, VOLUME_CHOICES, "Volume",
      "How loud the chime is" },
    { SETTING_KIND_BOOL,   "jump_en", 1, 2, NULL, "Auto-jump",
      // Two things fire it now: a session in the waiting bucket and a message
      // from another Claude session. The old wording promised only the first.
      "Open sessions on alerts, messages" },
    { SETTING_KIND_BOOL,   "splash",  1, 2, NULL, "Boot splash",
      "Start on Clawd instead of usage" },
    // Defaults to Auto: the host's clock hint drove the title clock before
    // this row existed, and an upgrade must not silently reformat anyone's
    // clock. 24h / 12h are overrides, and they override in both directions.
    { SETTING_KIND_CHOICE, "clk_pref", CLOCK_PREF_AUTO, 3, CLOCK_CHOICES, "Clock",
      "Auto follows the host's format" },
    { SETTING_KIND_STEP,   NULL,      1, 2, NULL, "Brightness",
      "Step through the backlight levels" },
    // Last, and an ACTION rather than a value: it is the one row that reaches
    // off the device. Bottom of the list because it is housekeeping you do
    // once, not a preference you come back to.
    { SETTING_KIND_ACTION, NULL,      0, 1, NULL, "Teach agents",
      "Send the Clawdmeter rules to every agent" },
};

static_assert(sizeof(SPECS) / sizeof(SPECS[0]) == SETTING_COUNT,
              "SPECS must have one entry per setting_id_t");

// Live values. Only slots this module owns are used; brightness is read
// straight from brightness_get() so there is never a second copy to drift.
static uint8_t vals[SETTING_COUNT];
static bool    defaults_applied = false;
// millis() of the last successful fire, per action row. 0 = never.
static uint32_t action_ms[SETTING_COUNT];
#define ACTION_ACK_MS 2500u

void settings_note_action(setting_id_t id) {
    if (id >= SETTING_COUNT) return;
    uint32_t t = millis();
    action_ms[id] = t ? t : 1;   // 0 is the never sentinel
}

static void apply_defaults(void) {
    for (uint8_t i = 0; i < SETTING_COUNT; i++) vals[i] = SPECS[i].def;
    defaults_applied = true;
}

// Getters may run before settings_init() (a board could ask during its own
// init); make that return the documented defaults rather than zeroes.
static inline void ensure_defaults(void) {
    if (!defaults_applied) apply_defaults();
}

// "This module stores the value itself" — true for every row except the ones
// another module owns (brightness).
static inline bool is_stored(setting_id_t id) {
    return id < SETTING_COUNT && SPECS[id].key != NULL;
}

static inline bool is_bool_setting(setting_id_t id) {
    return is_stored(id) && SPECS[id].kind == SETTING_KIND_BOOL;
}

static uint8_t get_raw(setting_id_t id) {
    if (!is_stored(id)) return 0;
    ensure_defaults();
    return vals[id];
}

static void persist(setting_id_t id) {
    if (!is_stored(id)) return;
    Preferences prefs;
    prefs.begin(SETTINGS_NS, false);
    prefs.putUChar(SPECS[id].key, vals[id]);
    prefs.end();
}

static void set_raw(setting_id_t id, uint8_t v) {
    if (!is_stored(id)) return;
    ensure_defaults();
    if (v >= SPECS[id].nvals) return;
    if (vals[id] == v) return;    // nothing to write; NVS wear is not free
    vals[id] = v;
    persist(id);
    if (SPECS[id].choices) {
        Serial.printf("Setting %s -> %s\n", SPECS[id].label, SPECS[id].choices[v]);
    } else {
        Serial.printf("Setting %s -> %s\n", SPECS[id].label, v ? "on" : "off");
    }
}

void settings_init(void) {
    apply_defaults();

    Preferences prefs;
    prefs.begin(SETTINGS_NS, true);
    for (uint8_t i = 0; i < SETTING_COUNT; i++) {
        const setting_id_t id = (setting_id_t)i;
        if (!is_stored(id)) continue;
        // 0xFF = "never written"; anything else out of range is treated the
        // same way, so a corrupt byte falls back to the default.
        uint8_t saved = prefs.getUChar(SPECS[i].key, 0xFF);
        if (saved < SPECS[i].nvals) vals[i] = saved;
    }
    prefs.end();

    Serial.printf("Settings init: sound=%s auto_jump=%s splash_boot=%s clock=%s\n",
                  vals[SETTING_SOUND]       ? "on" : "off",
                  vals[SETTING_AUTO_JUMP]   ? "on" : "off",
                  vals[SETTING_SPLASH_BOOT] ? "on" : "off",
                  CLOCK_CHOICES[vals[SETTING_CLOCK] < 3 ? vals[SETTING_CLOCK] : 0]);
}

// ---- Typed accessors ----

bool settings_sound_enabled(void)     { return get_raw(SETTING_SOUND) != 0; }

uint8_t settings_volume(void) {
    uint8_t i = get_raw(SETTING_VOLUME);
    if (i >= sizeof(VOLUME_LEVELS)) i = 1;   // corrupt byte -> Med
    return VOLUME_LEVELS[i];
}
bool settings_auto_jump_enabled(void) { return get_raw(SETTING_AUTO_JUMP) != 0; }
bool settings_splash_boot(void)       { return get_raw(SETTING_SPLASH_BOOT) != 0; }

clock_pref_t settings_clock_pref(void) {
    return (clock_pref_t)get_raw(SETTING_CLOCK);
}

// ---- Generic access ----

uint8_t settings_count(void) {
    return (uint8_t)SETTING_COUNT;
}

bool settings_get_bool(setting_id_t id) {
    if (!is_bool_setting(id)) return false;
    return get_raw(id) != 0;
}

void settings_set_bool(setting_id_t id, bool on) {
    if (!is_bool_setting(id)) return;
    set_raw(id, on ? 1 : 0);
}

void settings_activate(setting_id_t id) {
    if (id >= SETTING_COUNT) return;
    // An action row holds nothing to advance. The action itself is performed
    // by the caller, which is the only side that knows whether it worked.
    if (SPECS[id].kind == SETTING_KIND_ACTION) return;
    if (is_stored(id)) {
        // BOOL and CHOICE both just advance to the next value and wrap.
        set_raw(id, (uint8_t)((get_raw(id) + 1) % SPECS[id].nvals));
        return;
    }
    // Only externally-owned stepped setting today. brightness_cycle() wraps,
    // applies through idle, and persists its own NVS key.
    if (id == SETTING_BRIGHTNESS) brightness_cycle();
}

bool settings_get_row(uint8_t index, SettingRow* out) {
    if (!out || index >= SETTING_COUNT) return false;
    ensure_defaults();

    const setting_id_t  id   = (setting_id_t)index;
    const SettingSpec&  spec = SPECS[index];

    out->id     = id;
    out->kind   = spec.kind;
    out->label  = spec.label;
    out->detail = spec.detail;

    if (spec.kind == SETTING_KIND_BOOL) {
        out->on        = vals[index] != 0;
        out->value     = out->on ? 1 : 0;
        out->value_min = 0;
        out->value_max = 1;
        snprintf(out->value_text, sizeof(out->value_text), "%s",
                 out->on ? "On" : "Off");
    } else if (spec.kind == SETTING_KIND_CHOICE) {
        const uint8_t v = vals[index] < spec.nvals ? vals[index] : 0;
        out->on        = v != 0;   // 0 is the "no override" floor
        out->value     = (int16_t)v;
        out->value_min = 0;
        out->value_max = (int16_t)(spec.nvals - 1);
        snprintf(out->value_text, sizeof(out->value_text), "%s", spec.choices[v]);
    } else if (spec.kind == SETTING_KIND_ACTION) {
        // The verb, or an acknowledgement for a couple of seconds after it
        // fired. `on` drives the accent colour, so the row lights up exactly
        // while it is saying "Sent" and settles back to a quiet "Send".
        const uint32_t at = action_ms[index];
        const bool recent = at && (millis() - at) < ACTION_ACK_MS;
        out->on        = recent;
        out->value     = 0;
        out->value_min = 0;
        out->value_max = 0;
        snprintf(out->value_text, sizeof(out->value_text), "%s",
                 recent ? "Sent" : "Send");
    } else {
        // Brightness: the PWM level brightness.cpp is currently applying.
        const int level = (int)brightness_get();
        out->on        = level > 0;
        out->value     = (int16_t)level;
        out->value_min = 0;
        out->value_max = 255;
        snprintf(out->value_text, sizeof(out->value_text), "%d%%",
                 (level * 100 + 127) / 255);
    }
    return true;
}

void settings_reset_defaults(void) {
    apply_defaults();

    Preferences prefs;
    prefs.begin(SETTINGS_NS, false);
    for (uint8_t i = 0; i < SETTING_COUNT; i++) {
        if (!is_stored((setting_id_t)i)) continue;
        prefs.putUChar(SPECS[i].key, vals[i]);
    }
    prefs.end();

    Serial.println("Settings reset to defaults");
}
