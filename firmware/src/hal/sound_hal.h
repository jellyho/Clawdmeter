#pragma once
#include <stdint.h>

// Optional audio output (a passive piezo buzzer driven by LEDC PWM). Used to
// chime when the Claude session limit resets. Boards without a buzzer — the
// AMOLED-1.8 and the C6, whose stock hardware has no speaker output — no-op on
// init/tick and ignore play requests.
//
// Playback is non-blocking: sound_hal_play_reset() only *queues* the chime and
// returns immediately; sound_hal_tick() (called every loop) advances the notes
// so the LVGL render loop never stalls.

void sound_hal_init(void);
void sound_hal_tick(void);
void sound_hal_play_reset(void);

// Set output volume, 0..100. Boards with no speaker ignore it, so shared
// code can call this unconditionally; whether the row is even offered is a
// separate question answered by BoardCaps::has_sound.
void sound_hal_set_volume(uint8_t volume);
