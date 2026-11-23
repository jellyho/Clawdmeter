#pragma once

// Tracks short-term rate of change in session_pct (%/min) so the UI can react
// to *how heavily* Claude is being used right now, not just the current bucket
// level. Returns one of 4 group indices for the splash to pick animations from.

// Feed in the latest session percentage every time fresh BLE data arrives.
// Returns true when this sample is a session reset (pct dropped substantially
// vs the previous sample) — the caller uses this to chime the buzzer. Never
// true on the first sample after boot (no prior sample to compare against).
bool usage_rate_sample(float session_pct);

// Length of the window session_pct measures, in minutes (300 = the old
// 5-hour session, 10080 = a week). Tiers and the warm-up scale with it so
// "heavy" keeps meaning "on pace to exhaust the limit". A change clears the
// history. Call before usage_rate_sample().
void usage_rate_set_window(int mins);

// 0 = idle, 1 = normal, 2 = active, 3 = heavy.
// Defaults to 0 when the buffer doesn't have enough samples yet.
int usage_rate_group(void);
