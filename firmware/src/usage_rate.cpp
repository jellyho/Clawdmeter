#include "usage_rate.h"
#include <Arduino.h>

// Thresholds in %/min for a 5-hour (300 min) window. 300 min ÷ 100% = 0.33
// %/min fills it at exactly the pace it resets — the user wants the "heavy"
// tier to start right there (filling in 4–5 hours).
//   < 0.10  →  Idle    (17h+ to fill, basically dormant)
//   < 0.20  →  Normal  (8–17h to fill, slow steady use)
//   < 0.33  →  Active  (5–8h, heavy but not yet pace-matching)
//   >=0.33  →  Heavy   (≤5h, matching or beating the session reset)
//
// Claude dropped the 5-hour window; slot 1 is now a WEEK, which fills 33.6x
// slower for the same work. So the tiers are kept as fractions of the window
// and everything time-based is stretched by window/300: "heavy" still means
// "on pace to use the whole limit before it resets".
#define RATE_THRESH_NORMAL  0.10f
#define RATE_THRESH_ACTIVE  0.20f
#define RATE_THRESH_HEAVY   0.33f
#define BASE_WINDOW_MINS    300

// Minimum span between oldest and newest sample before we trust the computed
// rate. The whole point of the ring buffer is to smooth out single-sample
// jitter — at 60s daemon polling, a 1% bump between two consecutive samples
// looks like 1 %/min (Heavy) but really just means you grew 1% in the last
// minute. We require ~4 min of accumulated history so the rate reflects a
// real trend, not one noisy delta. Side-effect: ~4 min warm-up after boot
// during which we report Idle. Scaled by window/300 like the thresholds — on a
// week that is ~2.2 h, because the host reports whole percents and one
// percent of a week is 100 minutes of pace-matching use.
#define MIN_WINDOW_MS       240000UL

#define RING_SIZE 6

struct Sample { uint32_t ms; float pct; };

static Sample ring[RING_SIZE];
static uint8_t count = 0;
static uint8_t head  = 0;  // index of next write slot

static int   window_mins = BASE_WINDOW_MINS;
static float scale       = 1.0f;     // window_mins / BASE_WINDOW_MINS
static bool  have_last   = false;
static float last_pct    = 0.0f;     // previous raw sample, for reset detection

static inline uint8_t oldest_idx(void) {
    return (head + RING_SIZE - count) % RING_SIZE;
}

static inline uint32_t min_window_ms(void) {
    return (uint32_t)((float)MIN_WINDOW_MS * scale);
}

// Six slots at one per 60 s poll span six minutes — fine for five hours,
// useless for a week. On a stretched window a sample only enters the ring
// once this much time has passed since the last one, so the ring spans the
// stretched MIN_WINDOW. Zero on the legacy window: every poll, as before.
static inline uint32_t ring_step_ms(void) {
    if (scale <= 1.0f) return 0;
    return min_window_ms() / (RING_SIZE - 2);
}

static void usage_rate_reset(void) {
    count = 0;
    head  = 0;
}

void usage_rate_set_window(int mins) {
    if (mins < BASE_WINDOW_MINS) mins = BASE_WINDOW_MINS;
    if (mins == window_mins) return;
    window_mins = mins;
    scale = (float)mins / (float)BASE_WINDOW_MINS;
    usage_rate_reset();   // samples taken against another window mean nothing
}

bool usage_rate_sample(float session_pct) {
    uint32_t now = millis();
    bool was_reset = false;

    // Limit reset: pct dropped substantially. Restart tracking.
    if (have_last && session_pct + 5.0f < last_pct) {
        usage_rate_reset();
        was_reset = true;
    }
    have_last = true;
    last_pct = session_pct;

    if (count > 0) {
        uint8_t latest = (head + RING_SIZE - 1) % RING_SIZE;
        if (now - ring[latest].ms < ring_step_ms()) return was_reset;
    }

    ring[head] = { now, session_pct };
    head = (head + 1) % RING_SIZE;
    if (count < RING_SIZE) count++;

    return was_reset;
}

int usage_rate_group(void) {
    if (count < 2) return 0;

    uint8_t o = oldest_idx();
    uint8_t l = (head + RING_SIZE - 1) % RING_SIZE;
    uint32_t dt = ring[l].ms - ring[o].ms;
    if (dt < min_window_ms()) return 0;

    float dp = ring[l].pct - ring[o].pct;
    if (dp < 0.0f) dp = 0.0f;
    // Normalised to the 5-hour scale, so one table of thresholds serves both.
    float rate = dp * 60000.0f / (float)dt * scale;

    if (rate < RATE_THRESH_NORMAL) return 0;
    if (rate < RATE_THRESH_ACTIVE) return 1;
    if (rate < RATE_THRESH_HEAVY)  return 2;
    return 3;
}
