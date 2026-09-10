// BLE stub + scenario playback. Implements ble.h without any transport: a
// JSONL scenario file stands in for the daemon, delivered through the same
// ble_has_data()/ble_get_data() path main.cpp uses on hardware — so JSON
// parsing, usage-rate tracking, and the chime trigger all run for real.
//
// Two channels, mirroring the two GATT characteristics on hardware: a line
// carrying an "ss" array is a session payload (issue #135) and drains
// through ble_has_session_data()/ble_get_session_data(); every other line is
// a quota payload on ble_has_data()/ble_get_data(). One scenario file
// interleaves both — the playback cursor is shared, only the drain differs.
#include "../../ble.h"
#include "sim_platform.h"
#include <Arduino.h>
#include <ArduinoJson.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_STATES 64
#define MAX_LINE   768   // session rows are chunky: 6 x ~48 chars + name/hold

struct SimState {
    char json[MAX_LINE];
    char name[32];
    uint32_t hold_ms;
    bool session;         // carries "ss" → session channel, not the quota one
};

static SimState states[MAX_STATES];
static int      n_states = 0;
static int      cur = 0;
static bool     playing = true;
static bool     connected = true;
static bool     pending = false;      // a state is queued for main's next poll
static uint32_t pending_ms = 0;       // when it was queued (undrained timeout)
static uint32_t delivered_ms = 0;

// One-off session payload fired by the 'w' key, independent of the scenario
// cursor so a notification can be raised on top of any state.
static char inject_json[MAX_LINE];
static bool inject_pending = false;
static int  alert_idx = 0;

static const char* FALLBACK[] = {
    "{\"name\":\"fresh\",\"s\":3.0,\"sr\":295,\"w\":12.0,\"wr\":9000,\"st\":\"allowed\",\"ok\":true}",
    "{\"name\":\"mid\",\"s\":48.0,\"sr\":150,\"w\":35.0,\"wr\":7200,\"st\":\"allowed\",\"ok\":true}",
    "{\"name\":\"high\",\"s\":92.0,\"sr\":30,\"w\":71.0,\"wr\":4600,\"st\":\"allowed\",\"ok\":true}",
    "{\"name\":\"reset+chime\",\"hold_ms\":4000,\"s\":2.0,\"sr\":298,\"w\":72.0,\"wr\":4500,\"st\":\"allowed\",\"c\":true,\"ok\":true}",
};

static void add_state(const char* line) {
    if (n_states >= MAX_STATES) return;
    size_t len = strlen(line);
    while (len && (line[len - 1] == '\n' || line[len - 1] == '\r')) len--;
    if (!len || line[0] == '#') return;   // blank lines / comments
    SimState* s = &states[n_states];
    if (len >= MAX_LINE) len = MAX_LINE - 1;
    memcpy(s->json, line, len);
    s->json[len] = 0;
    s->hold_ms = 3000;
    // Fallback classification if the line doesn't parse — main.cpp will
    // reject it either way, but it still routes to a plausible channel.
    // "fl" is the roster -- the colony's feed -- and rides the SAME
    // characteristic as the session rows, two payload kinds on one channel,
    // exactly as on hardware. A scenario line carrying either belongs there.
    s->session = strstr(s->json, "\"ss\"") != NULL ||
                 strstr(s->json, "\"fl\"") != NULL;
    snprintf(s->name, sizeof(s->name), "state %d", n_states + 1);
    // "name" and "hold_ms" ride along in the payload; main's parse_json /
    // parse_sessions ignore unknown keys so the line is delivered as-is.
    JsonDocument doc;
    if (deserializeJson(doc, s->json) == DeserializationError::Ok) {
        s->hold_ms = doc["hold_ms"] | 3000;
        const char* nm = doc["name"] | (const char*)NULL;
        if (nm) snprintf(s->name, sizeof(s->name), "%s", nm);
        s->session = !doc["ss"].isNull() || !doc["fl"].isNull();
    }
    n_states++;
}

static void load_scenario(void) {
    const char* tries[] = { getenv("SIM_SCENARIO"), "sim/scenario.jsonl",
                            "firmware/sim/scenario.jsonl", "../sim/scenario.jsonl" };
    FILE* f = NULL;
    for (const char* t : tries) {
        if (!t) continue;
        f = fopen(t, "r");
        if (f) { printf("[sim] scenario: %s\n", t); break; }
    }
    if (f) {
        char line[MAX_LINE];
        while (fgets(line, sizeof(line), f)) add_state(line);
        fclose(f);
    }
    if (!n_states) {
        printf("[sim] no scenario file found — using built-in states\n");
        for (const char* l : FALLBACK) add_state(l);
    }
}

static void refresh_title(void) {
    char t[128];
    snprintf(t, sizeof(t), "Clawdmeter sim — %s[%d/%d] %s%s %s",
             connected ? "" : "(disconnected) ",
             cur + 1, n_states,
             states[cur].session ? "SS " : "",   // session channel marker
             states[cur].name,
             playing ? "\xE2\x96\xB6" : "\xE2\x8F\xB8");
    sim_display_set_title(t);
}

static void queue_current(void) {
    pending = true;
    pending_ms = millis();
}

void ble_init(void) {
    load_scenario();
    queue_current();
    refresh_title();
}

// Headless counterpart of the 'w' key: SIM_ALERT_MS=<ms> fires one session
// alert after <ms>, so CI can capture the notification path (and whatever
// the UI does about it) with no keyboard. Pair with SIM_AUTOSHOT_MS.
static void alert_env_hook(void) {
    static long at_ms = -2;
    if (at_ms == -2) {
        const char* v = getenv("SIM_ALERT_MS");
        at_ms = v ? atol(v) : -1;
    }
    if (at_ms >= 0 && millis() >= (uint32_t)at_ms) {
        at_ms = -1;
        sim_session_alert();
    }
}

void ble_tick(void) {
    alert_env_hook();
    if (!connected || !playing || n_states == 0) return;
    if (pending) {
        // A queued payload is normally drained the same loop iteration
        // main.cpp polls. If nobody drains it — a build with the session
        // views compiled out never reads the SS channel — time it out
        // instead of wedging playback on that line forever.
        if (millis() - pending_ms < states[cur].hold_ms) return;
        pending = false;
        delivered_ms = pending_ms;
    }
    if (millis() - delivered_ms >= states[cur].hold_ms) {
        cur = (cur + 1) % n_states;
        queue_current();
        refresh_title();
    }
}

ble_state_t ble_get_state(void) {
    return connected ? BLE_STATE_CONNECTED : BLE_STATE_DISCONNECTED;
}
const char* ble_get_device_name(void) { return "Clawdmeter (sim)"; }
const char* ble_get_mac_address(void) { return "00:51:4D:00:00:01"; }

void ble_clear_bonds(void) { printf("[sim] pair gesture completed — bonds cleared\n"); }
bool ble_has_bonds(void)   { return true; }

static const char* drain_current(void) {
    pending = false;
    delivered_ms = millis();
    return states[cur].json;
}

// Quota channel: every scenario line that is *not* a session payload.
bool ble_has_data(void) {
    return connected && pending && n_states && !states[cur].session;
}
const char* ble_get_data(void) { return drain_current(); }

// Session channel (issue #135) — the SS characteristic's stand-in. Serves
// the 'w'-key injection first, then session-carrying scenario lines.
bool ble_has_session_data(void) {
    if (!connected) return false;
    if (inject_pending) return true;
    return pending && n_states && states[cur].session;
}
const char* ble_get_session_data(void) {
    if (inject_pending) {
        inject_pending = false;
        return inject_json;
    }
    return drain_current();
}
void ble_send_ack(void)  {}
void ble_send_nack(void) { printf("[sim] payload NACKed — check the scenario JSON\n"); }
void ble_request_refresh(void) {}

// Button events (TX ...0003). The sim has no daemon to notify, so this is the
// print that stands in for one: pressing the report button in the SDL window
// shows the exact bytes the firmware would have put on the wire.
bool ble_send_event(ble_event_t ev, const char* sid) {
    if (sid && *sid) printf("[sim] BLE event {\"ev\":%d,\"sid\":\"%s\"}\n", (int)ev, sid);
    else             printf("[sim] BLE event {\"ev\":%d}\n", (int)ev);
    return connected;
}
bool ble_send_report_request(void) { return ble_send_event(BLE_EVENT_REPORT, nullptr); }
void ble_set_battery_level(int pct) { (void)pct; }

void ble_keyboard_press(uint8_t key, uint8_t modifier) {
    printf("[sim] HID press key=0x%02X mod=0x%02X\n", key, modifier);
}
void ble_keyboard_release(void) { printf("[sim] HID release\n"); }

// ---- Playback controls (called from the sim_platform event pump) ----
void sim_playback_toggle(void) {
    playing = !playing;
    delivered_ms = millis();   // restart the hold timer on resume
    refresh_title();
}
void sim_playback_step(int dir) {
    if (!n_states) return;
    playing = false;
    cur = (cur + dir + n_states) % n_states;
    queue_current();
    refresh_title();
}
void sim_playback_jump(int idx) {
    if (idx < 0 || idx >= n_states) return;
    playing = false;
    cur = idx;
    queue_current();
    refresh_title();
}
void sim_playback_toggle_link(void) {
    connected = !connected;
    refresh_title();
}

// 'w' — raise a session notification on demand, whatever the scenario is
// doing. Cycles the waiting bucket (§3: 6 permission, 7 question, 8 input,
// 9 error) so the auto-jump behaviour can be demonstrated repeatedly. The
// alerting chat is sent first, matching the host's attention-first sort.
void sim_session_alert(void) {
    static const struct {
        uint8_t     state;
        const char* what;
        const char* sid;
        const char* label;
        int         ctx;
        int         tok;
    } ALERTS[] = {
        { 6, "needs permission", "a1", "clawdmeter",     72, 144 },
        { 7, "asking you",       "b2", "raincheck-api",  38,  76 },
        { 8, "needs input",      "c3", "dotfiles",       11,  22 },
        { 9, "error",            "d4", "flight-tracker", 91, 182 },
    };
    const int n = (int)(sizeof(ALERTS) / sizeof(ALERTS[0]));
    const int i = alert_idx % n;
    alert_idx = (alert_idx + 1) % n;

    // [sid, label, state, ctx, elapsed_s, model, tool, ntools, nagents,
    //  tdone, ttotal, tok] — the wire format in daemon/SESSIONS.md.
    snprintf(inject_json, sizeof(inject_json),
             "{\"ss\":["
             "[\"%s\",\"%s\",%u,%d,4,1,0,0,0,2,5,%d],"
             "[\"e5\",\"usage-daemon\",4,44,17,2,1,1,0,3,6,88],"
             "[\"f6\",\"notes\",1,9,930,3,0,0,0,0,0,18]]}",
             ALERTS[i].sid, ALERTS[i].label, ALERTS[i].state,
             ALERTS[i].ctx, ALERTS[i].tok);
    inject_pending = true;

    printf("[sim] session alert: %s — %s (state %u)\n",
           ALERTS[i].label, ALERTS[i].what, ALERTS[i].state);
    char t[128];
    snprintf(t, sizeof(t), "Clawdmeter sim — SS alert: %s %s",
             ALERTS[i].label, ALERTS[i].what);
    sim_display_set_title(t);
}
