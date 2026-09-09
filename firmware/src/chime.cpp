#include "chime.h"
#include <Arduino.h>
#include "ESP_I2S.h"
#include "es8311.h"
#include "bell_pcm.h"   // const uint8_t bell_pcm[] / bell_pcm_len — 44.1 kHz 16-bit stereo

// Shared ES8311 chime engine. See chime.h. Adapted from the original 2.16
// sound.cpp so the 2.16, 1.8 (and any future ES8311 board) share one copy of
// the codec setup, the embedded PCM, and the non-blocking playback task.

static I2SClass      i2s;
static ChimeConfig   cfg;
static bool          ready   = false;
static volatile bool playing = false;
// Kept past init so the volume register stays reachable: the codec is the
// only place the level lives, and re-creating the handle would re-run the
// whole init sequence just to write one register.
static es8311_handle_t codec = nullptr;

static bool es8311_setup(void) {
    es8311_handle_t es = es8311_create(0, cfg.es8311_addr);   // I2C port 0 (shared Wire bus)
    if (!es) return false;
    codec = es;
    // mclk_inverted, sclk_inverted, mclk_from_mclk_pin, mclk_frequency, sample_frequency
    const es8311_clock_config_t clk = {
        false, false, true, cfg.sample_rate * 256, cfg.sample_rate
    };
    if (es8311_init(es, &clk, ES8311_RESOLUTION_16, ES8311_RESOLUTION_16) != ESP_OK) return false;
    es8311_sample_frequency_config(es, clk.mclk_frequency, clk.sample_frequency);
    es8311_microphone_config(es, false);
    es8311_voice_volume_set(es, cfg.volume, NULL);
    return true;
}

// ---- The volume preview ----------------------------------------------------
// A prefix of the same clip rather than a second asset. The bell opens with
// 48 ms of silence, one struck note that has decayed to a third of its attack
// by 150 ms, and a second note starting at 175 ms — so 170 ms is exactly one
// whole note and no fragment of the next. The last 20 ms ramp to zero: cutting
// PCM mid-cycle steps the DAC to silence in one sample, which is a click.
#define PREVIEW_MS      170
#define PREVIEW_FADE_MS 20
#define PCM_FRAME_BYTES 4          // 16-bit stereo

// The clip lives in .rodata (memory-mapped flash) and is byte-addressed, so
// samples are assembled a byte at a time: a uint16_t load off an odd address
// is not something to rely on across two CPU families.
static inline int16_t pcm_sample(const uint8_t* p) {
    return (int16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

// Stream `frames` frames from `src`, ramping the gain linearly to zero across
// the whole span. Chunked through a small stack buffer so the fade costs
// bounded stack rather than a 3.5 KB allocation on a board with none to spare.
static void write_faded(const uint8_t* src, uint32_t frames) {
    if (!frames) return;
    int16_t buf[128 * 2];
    uint32_t done = 0;
    while (done < frames) {
        uint32_t n = frames - done;
        if (n > 128) n = 128;
        for (uint32_t i = 0; i < n; i++) {
            const uint8_t* f = src + (uint32_t)(done + i) * PCM_FRAME_BYTES;
            const int32_t gain = (int32_t)(frames - (done + i));   // frames..1
            buf[i * 2]     = (int16_t)((int32_t)pcm_sample(f)     * gain / (int32_t)frames);
            buf[i * 2 + 1] = (int16_t)((int32_t)pcm_sample(f + 2) * gain / (int32_t)frames);
        }
        i2s.write((uint8_t*)buf, n * PCM_FRAME_BYTES);
        done += n;
    }
}

static void chime_task(void* arg) {
    const bool preview = (arg != nullptr);
    if (cfg.amp_enable) cfg.amp_enable(true);
    delay(8);                                  // let the amp settle (avoids turn-on pop)
    if (preview) {
        const uint32_t total = (uint32_t)cfg.sample_rate * PREVIEW_MS / 1000;
        const uint32_t fade  = (uint32_t)cfg.sample_rate * PREVIEW_FADE_MS / 1000;
        uint32_t frames = total;
        const uint32_t have = bell_pcm_len / PCM_FRAME_BYTES;
        if (frames > have) frames = have;      // a shorter clip than the prefix
        const uint32_t flat = frames > fade ? frames - fade : 0;
        if (flat) i2s.write((uint8_t*)bell_pcm, flat * PCM_FRAME_BYTES);
        write_faded(bell_pcm + flat * PCM_FRAME_BYTES, frames - flat);
    } else {
        i2s.write((uint8_t*)bell_pcm, bell_pcm_len);
    }
    delay(20);
    if (cfg.amp_enable) cfg.amp_enable(false);
    playing = false;
    vTaskDelete(nullptr);
}

bool chime_init(const ChimeConfig& c) {
    cfg = c;
    if (cfg.amp_enable) cfg.amp_enable(false);   // amp off until we play

    i2s.setPins(cfg.bclk, cfg.ws, cfg.dout, cfg.din, cfg.mclk);
    if (!i2s.begin(I2S_MODE_STD, cfg.sample_rate, I2S_DATA_BIT_WIDTH_16BIT,
                   I2S_SLOT_MODE_STEREO, I2S_STD_SLOT_BOTH)) {
        Serial.println("chime: I2S init failed");
        return false;
    }
    if (!es8311_setup()) {
        Serial.println("chime: ES8311 init failed");
        return false;
    }
    ready = true;
    Serial.println("chime: ES8311 ready");
    return true;
}

// The task argument is the whole difference between the two: non-null means
// "play the preview prefix". One task, one guard, one amp-enable sequence.
static void chime_start(bool preview) {
    if (!ready || playing) return;
    playing = true;
    if (xTaskCreatePinnedToCore(chime_task, "chime", 4096,
                                preview ? (void*)1 : nullptr, 1, nullptr, 0) != pdPASS)
        playing = false;   // couldn't spawn — stay silent rather than wedge the flag
}

void chime_play(void)         { chime_start(false); }
void chime_play_preview(void) { chime_start(true); }

void chime_set_volume(uint8_t volume) {
    if (!codec) return;                       // no codec on this board, or init failed
    if (volume > 100) volume = 100;
    cfg.volume = volume;                      // so a later re-init keeps the level
    es8311_voice_volume_set(codec, (int)volume, NULL);
}

void chime_tick(void) {}   // playback runs in chime_task; nothing to poll
