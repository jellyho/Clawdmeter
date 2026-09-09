#include "../../hal/sound_hal.h"
#include <stdio.h>

void sound_hal_init(void) {}
void sound_hal_tick(void) {}
void sound_hal_play_reset(void)   { printf("[sim] chime! (session reset)\n"); }
void sound_hal_play_preview(void) { printf("[sim] chime sample (volume preview)\n"); }
void sound_hal_set_volume(uint8_t v) { (void)v; }
