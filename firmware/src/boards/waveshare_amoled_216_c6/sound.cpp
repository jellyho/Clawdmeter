#include "../../hal/sound_hal.h"
#include "board.h"

#if BOARD_HAS_SOUND

#include <Arduino.h>
#include "../../chime.h"

// C6 AMOLED-2.16: ES8311 codec + speaker, the same part the S3 2.16 carries,
// so all of the codec/I2S/playback work lives in the shared chime engine
// (../../chime.cpp) and this file only supplies pins.
//
// The one structural difference from the S3: this board has no power-amp
// enable line (Waveshare's own configs give pa = -1), so the amp is hardwired
// on and amp_enable is null. chime.cpp null-checks that hook on both the
// init and the playback path, so nothing else changes.

void sound_hal_init(void) {
    const ChimeConfig cfg = {
        SND_I2S_MCLK, SND_I2S_BCLK, SND_I2S_WS, SND_I2S_DOUT, SND_I2S_DIN,
        SND_SAMPLE_RATE, SND_ES8311_ADDR, 65, nullptr
    };
    chime_init(cfg);
}

void sound_hal_play_reset(void) { chime_play(); }
void sound_hal_set_volume(uint8_t v) { chime_set_volume(v); }
void sound_hal_tick(void)       { chime_tick(); }

#else   // keep the board linkable if the capability is flipped back off

void sound_hal_init(void)       {}
void sound_hal_tick(void)       {}
void sound_hal_play_reset(void) {}
void sound_hal_set_volume(uint8_t v) { (void)v; }

#endif  // BOARD_HAS_SOUND
