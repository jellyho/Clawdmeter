#pragma once
#include <stdbool.h>

void idle_init(void);
void idle_tick(void);
void idle_note_activity(void);

// Set the "awake" brightness target (0..255). idle owns display brightness
// (it fades between this and 0), so user brightness control routes through
// here. Applied immediately if the screen is currently fully awake; otherwise
// picked up by the next fade-in. See brightness.{h,cpp}.
void idle_set_awake_brightness(uint8_t level);

// Returns true if this press was consumed as a wake-up (caller MUST skip the
// button's normal action). Returns false when already awake — also notes the
// activity, so callers don't need a separate idle_note_activity() call.
bool idle_consume_wake_press(void);

// How long since the last real input (touch, button, PWR). The idle module
// already keeps this to decide when to fade the panel; the UI borrows it so
// "nobody has touched this in a while" is answered in ONE place rather than by
// a second timer that could disagree with the one driving the backlight.
uint32_t idle_ms_since_activity(void);

// Touch should NOT count as activity (avoids accidental wakes from pets,
// sleeves, etc.). Callers use this to silently drop touch events while the
// panel is dark.
bool idle_is_asleep(void);
