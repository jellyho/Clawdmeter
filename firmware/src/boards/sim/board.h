#pragma once

// Native desktop simulator "board" — an SDL2 window standing in for the
// 480×480 AMOLED so shared code (main.cpp, ui.cpp, splash.cpp) can be
// developed without hardware. No pins here, just geometry and the key map.
//
//   mouse / left-drag    touch (tap toggles splash <-> usage)
//   space                play/pause scenario playback
//   left / right         step one scenario state (pauses playback)
//   1..9                 jump to scenario state N (pauses playback)
//   d                    toggle BLE connected/disconnected
//   w                    fire a session notification: injects a one-off
//                        session payload whose top row is in the waiting
//                        bucket, cycling permission -> question -> input ->
//                        error on each press. Scenario playback is left
//                        alone, so this works on any state.
//   b (hold)             PRIMARY button  (BOOT — HID Space PTT on hardware)
//   n (hold)             SECONDARY button (HID Shift+Tab on hardware)
//   p                    PWR button (short press; hold ~3s + release = pair)
//   c                    toggle charging       - / =   battery down / up 5%
//   s                    save screenshot BMP to the current directory
//   esc / window close   quit
//
// Scenario: sim/scenario.jsonl (relative to the firmware/ dir), overridable
// with SIM_SCENARIO=<path>. One JSON object per line — the daemon payload
// plus optional "name" and "hold_ms" (default 3000). Lines starting with #
// are comments. Missing file → a small built-in state list.
//
// A line carrying an "ss" array is a *session* payload (issue #135 wire
// format) and is delivered on the session channel
// (ble_has_session_data()/ble_get_session_data()); every other line is a
// quota payload on ble_has_data()/ble_get_data(). One scenario can
// interleave both.
//
// Headless / CI: SDL_VIDEODRIVER=dummy SIM_AUTOSHOT_MS=<ms> saves a
// screenshot (SIM_AUTOSHOT_PATH, default sim-autoshot.bmp) after <ms> and
// exits. SIM_ALERT_MS=<ms> is the 'w' key without a keyboard: it fires one
// session notification after <ms>, so CI can capture the alert path.

#define BOARD_NAME  "Simulator 480x480"
#define LCD_WIDTH   480
#define LCD_HEIGHT  480

// Chat card views (issue #135). Same geometry as the S3 2.16, which ships
// them enabled — keep this in sync with -DBOARD_HAS_SESSION_VIEWS in the
// [env:sim] block (shared code can't see board.h).
#define BOARD_HAS_SESSION_VIEWS  1

// Speaker presence — mirrors the 2.16 as well; the sim's sound_hal_play_reset()
// prints the chime instead of playing it, so the Settings row is live here.
#define BOARD_HAS_SOUND          1
