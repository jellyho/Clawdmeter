#pragma once
#include <stdint.h>

// Board-agnostic ES8311 + I2S chime engine. A board's sound.cpp fills in a
// ChimeConfig (its I2S pins, codec address, volume, and a power-amp enable
// hook) and calls chime_init() once; chime_play() then streams the embedded
// reset clip (bell_pcm.h) to the codec in a one-shot FreeRTOS task so the LVGL
// loop never blocks. The embedded PCM is 44.1 kHz / 16-bit / stereo, so
// cfg.sample_rate must be 44100 for correct-pitch playback.
//
// amp_enable is the only part that differs structurally between boards: the
// 2.16 toggles a GPIO, the 1.8 drives an XCA9554 expander line. May be null.

typedef void (*amp_enable_fn)(bool on);

struct ChimeConfig {
    int          mclk, bclk, ws, dout, din;  // I2S GPIOs
    int          sample_rate;                // must match the embedded PCM (44100)
    uint8_t      es8311_addr;                // codec I2C address (0x18)
    uint8_t      volume;                     // 0..100
    amp_enable_fn amp_enable;                // power-amp on/off hook (nullable)
};

// Bring up I2S + the ES8311 codec. Returns true on success; false leaves the
// engine inert (chime_play becomes a no-op). Wire/I2C must already be up.
bool chime_init(const ChimeConfig& cfg);

// Queue one playback of the embedded clip (non-blocking). No-op if not ready
// or already playing.
void chime_play(void);

// Queue one playback of just the clip's FIRST NOTE (~170 ms), faded out over
// its last 20 ms so the cut does not click. This is the Volume row's preview:
// the whole clip is 2.4 s, far too long to hang off a button press, and long
// enough that a second press would land on top of the first. Non-blocking,
// and it shares chime_play()'s "already playing" guard — a press arriving
// mid-sample is dropped rather than queued, which at 170 ms is a press the
// user has effectively already been answered for.
void chime_play_preview(void);

// Set the codec's output volume, 0..100. Takes effect on the next playback
// (and immediately for a clip already streaming). No-op if the codec never
// came up, so a caller never has to ask whether the board has a speaker.
void chime_set_volume(uint8_t volume);

// Currently a no-op (playback runs in its own task); kept for HAL symmetry.
void chime_tick(void);
