# Project context

ESP32-S3 / ESP32-C6 firmware for a desk-side Claude Code usage monitor. Each
supported board lives in its own `firmware/src/boards/<name>/` folder and is
selected via PlatformIO's `build_src_filter`. Adding a board means dropping in
a new folder + a new `[env:...]` block — `main.cpp`, `ui.cpp`, and `splash.cpp`
never see board-specific code. See [`docs/porting/adding-a-board.md`](docs/porting/adding-a-board.md).

Seven ports today (two SoC families, five panel sizes):

- `boards/waveshare_amoled_216/` — original Waveshare ESP32-S3-Touch-AMOLED-2.16 (CO5300, 480×480 square, CST9220 touch, IMU rotation). Build env: `waveshare_amoled_216`.
- `boards/waveshare_amoled_18/` — Waveshare ESP32-S3-Touch-AMOLED-1.8 (368×448 portrait, XCA9554 IO expander). Build env: `waveshare_amoled_18`. **Two panel revisions are auto-detected at boot** (`board_rev()` in `board_init.cpp`, enum in `board_rev.h`): original = SH8601 display + FT3168 touch (0x38); later = CO5300 display + CST816 touch (0x15). One binary drives both.
- `boards/waveshare_amoled_216_c6/` — Waveshare ESP32-C6-Touch-AMOLED-2.16 (SH8601, 480×480, CST9217 touch). Build env: `waveshare_amoled_216_c6`. ESP32-C6 SoC: single-core RISC-V, **no PSRAM**, BLE 5 only. **Audio is live**: ES8311 codec at 0x18 on the shared I2C bus with a hardwired amp (no enable line), pins from Waveshare's own 07_Audio_Test examples; the shared `chime.cpp` drives it. **Session views are on** here despite the missing PSRAM — same 480×480 geometry as the S3 2.16, paid for with `-DLV_MEM_SIZE=131072` out of internal SRAM (build sits at 51.5% RAM).
- `boards/waveshare_amoled_18_c6/` — Waveshare ESP32-C6-Touch-AMOLED-1.8 (368×448 portrait, SH8601, FT3168 touch, TCA9554 expander). Build env: `waveshare_amoled_18_c6`. Same panel as the S3 1.8 but on the C6 SoC. All subsystems (display, touch, BOOT + PWR buttons, battery, BLE) verified on hardware.
- `boards/waveshare_amoled_206/` — Waveshare ESP32-S3-Touch-AMOLED-2.06 (CO5300, 410×502 watch form factor, FT3168 touch, no IO expander, 32 MB flash, PCF85063 RTC, ES8311 codec). Build env: `waveshare_amoled_206`. Display, touch, battery, IMU init, and BLE verified on hardware; the ES8311 chime path is not wired up (`sound.cpp` no-ops).
- `boards/waveshare_lcd_154/` — Waveshare ESP32-S3-Touch-LCD-1.54 (ST7789, 240×240 square, CST816T touch @ 0x15). Build env: `waveshare_lcd_154`. **The first non-AMOLED port**: a plain 4-wire SPI TFT, not QSPI, and the panel has no brightness command — backlight is LEDC PWM on `LCD_BL`. **No PMU**: battery is an ADC divider on GPIO1 and `BAT_EN` (GPIO2) is a power-hold line that must be driven HIGH early in `board_init()` or the board browns out on battery. Three buttons (BOOT + GPIO5 + a PWR-role GPIO4); ES8311 chime wired up; QMI8658 populated but unused (fixed orientation, no rotation).
- `boards/waveshare_lcd_4/` — Waveshare ESP32-S3-Touch-LCD-4 (ST7701 RGB parallel, 480×480 square, GT911 touch). Build env: `waveshare_lcd_4`. **RGB-panel port**: Arduino_ESP32RGBPanel + bounce buffers (tearing fix). IO expander @ 0x24 (TCA9554 / CH32V003) must init before `gfx->begin()` or the panel stays dark; backlight is expander pin 2 (on/off only). No AXP2101 / IMU; KEY/PWR is hardware RST. Single BOOT button (GPIO 0 → Space/PTT).

Plus one non-hardware target: `boards/sim/` — **native desktop simulator** (SDL2 window, 480×480, `platform = native`). Build env: `sim`. See "Desktop simulator" below.

**C6 ports have no PSRAM** — shared code gates on `BOARD_HAS_PSRAM` (absent on C6) to use `MALLOC_CAP_INTERNAL` for LVGL/splash buffers, and the `screenshot` serial command is disabled (`LV_USE_SNAPSHOT=0`), so UI changes on a C6 board must be eyeballed on hardware, not auto-captured.

The shared code calls a small HAL (`firmware/src/hal/`) that each board implements: display, touch, input, power, IMU. Optional features are guarded by `BoardCaps` (runtime) and `BOARD_HAS_*` (compile-time) rather than `#ifdef BOARD_*`.

Connects to a host daemon over BLE; daemon polls Anthropic API for usage data. This file is for future Claude Code sessions to bootstrap quickly. Read this first.

## Hardware (critical pins)

### AMOLED-2.16 (original)
- Display: **CO5300** AMOLED via QSPI (CS=12, SCLK=38, SDIO0..3=4..7, RST=2)
- Touch: **CST9220** via I2C (SDA=15, SCL=14, INT=11, addr=0x5A)
- PMU: **AXP2101** on same I2C bus (addr=0x34) — battery, USB VBUS, PWR button IRQ
- IMU: **QMI8658** on same I2C bus (addr=0x6B) — accelerometer for auto-rotation
- Buttons: GPIO 0 (left → Space/voice-mode), GPIO 18 (right → **report round**, `ble_send_report_request()` on the press edge; it used to send HID Shift+Tab and no longer sends HID at all), AXP PKEY (middle → cycle screens; on splash → cycle animations)

### AMOLED-1.8 (newer port)
**Two hardware revisions ship under this name; the firmware probes I2C at boot and picks drivers automatically (`board_rev()`):**
- Display: **SH8601** (original) or **CO5300** (later rev) AMOLED via QSPI (CS=12, **SCLK=11** ← different!, SDIO0..3=4..7, RST routed via XCA9554 EXIO1). Both are `Arduino_OLED` subclasses held behind one base pointer in `display.cpp`. The CO5300's 368-wide active area starts at GRAM column 16, so it gets `CO5300_COL_OFFSET 16` to center; SH8601 needs none.
- Touch: **FT3168** @ 0x38 (original) or **CST816** @ 0x15 (later rev), via I2C (SDA=15, SCL=14, INT=21). Both expose the same FocalTech-style data layout at regs 0x02..0x06, so one inline reader in `touch.cpp` serves both — only the address differs. Avoids vendoring the GPLv3 `Arduino_DriveBus` library. Revision is detected by which touch address ACKs (CST816 present ⇒ CO5300 panel).
- PMU: AXP2101 @ 0x34 (same chip as 2.16 — `XPowersLib` reused; battery is an optional kit add-on but PMU + charging circuitry are populated)
- IMU: QMI8658 @ 0x6B (same chip — initialized for I2C bus health, rotation logic disabled)
- IO expander: **XCA9554 / PCA9554** @ I2C 0x20. Gates LCD_RST, TP_RST, audio amp enable, and reads the PWR button. **`io_expander_init()` MUST run before `gfx->begin()` or `ft3168_init()`** — otherwise display/touch stay in reset and silently fail. PWR button is on EXIO4, active HIGH (verified empirically with the deleted `iox` serial debug command).
- Orientation: **fixed at 0°**. IMU auto-rotation is disabled; `rotate_strip()` / `handle_rotation_change()` are excluded via `#ifndef BOARD_AMOLED_18`.
- Buttons: GPIO 0 (BOOT → Space/voice-mode), XCA9554 EXIO4 (PWR → cycle screens; on splash → cycle animations). **No third button** (GPIO 18 button doesn't exist on this board).

### AMOLED-1.8 (C6) — `waveshare_amoled_18_c6`
ESP32-C6 sibling of the S3 1.8: same 368×448 SH8601 panel + FocalTech touch, different SoC and GPIO map. **All pins/edges below verified on hardware via temporary GPIO/IRQ scans, since Waveshare's wiki publishes no pin table and the third-party BSP's numbers were partly wrong.**
- Display: **SH8601** AMOLED via QSPI (CS=5, SCLK=0, SDIO0..3=1..4, no MCU reset pin — internal POR; effective reset is the TCA9554 power-cycle). Stock `Arduino_SH8601` init (no vendor-register patch — that's only needed on the C6 2.16).
- Touch: **FT3168** (some units FT6146) @ I2C 0x38, INT=15. Same inline FocalTech reader as the S3 1.8 (regs 0x02..0x06); no reset pin (gated by TCA9554 touch power).
- I2C bus: SDA=8, SCL=7 (shared by TCA9554, AXP2101, FT3168, QMI8658, PCF85063 RTC, ES8311 codec).
- IO expander: **TCA9554 / PCA9554** @ 0x20 — here it gates **power**, not reset: **P4 = display power, P5 = touch power, P7 = audio amp**. `io_expander_init()` runs the documented power-on sequence (P4/P5 LOW → 200 ms → HIGH) and **MUST run before `display_hal_init()`** or the panel stays unpowered. Amp (P7) left off (no audio path).
- PMU: AXP2101 @ 0x34 (owned by `power.cpp`, not `board_init` — LCD isn't on an ALDO rail here).
- IMU: QMI8658 @ 0x6B (init'd for bus health, rotation disabled).
- Orientation: **fixed at 0°**, no rotation (no PSRAM headroom).
- Buttons: **GPIO 9** (BOOT → Space/voice-mode, active LOW — *not* the docs' GPIO 0/9 guess; confirmed by scan), **AXP2101 PKEY** (PWR → cycle screens; on splash → cycle animations). The PKEY **SHORT-press IRQ fires on release** — that's the edge `power.cpp` acts on. No secondary button.

### AMOLED-2.06 (watch form factor) — `waveshare_amoled_206`
- Display: **CO5300** AMOLED via QSPI (CS=12, **SCLK=11** ← same as 1.8, SDIO0..3=4..7, RST=8 direct GPIO). 410×502 portrait. Requires **`col_offset1 = 23`** in the `Arduino_CO5300` constructor — the panel's visible viewport sits at a 22–23 column offset inside the controller's internal RAM. Without it, a vertical strip of stale/garbage content shows through on the right edge (23 was picked empirically for centering; Waveshare's reference library uses 22). The 2.16 dodges this because its 480×480 viewport fills the controller's RAM.
- Touch: **FT3168** via I2C (SDA=15, SCL=14, **INT=38, RST=9** direct GPIO, addr=0x38). Same inline FocalTech reader as the 1.8 port (no GPLv3 `Arduino_DriveBus` dependency). Coordinates verified end-to-end with the BLE reset zone.
- PMU: AXP2101 @ 0x34 (same chip as 2.16/1.8 — `XPowersLib` reused). PWR button routes through AXP PKEY IRQs (short / long / positive), same path as the 2.16 — no IO expander.
- IMU: QMI8658 @ 0x6B (initialized for I2C bus health; rotation logic disabled — fixed watch enclosure orientation).
- RTC: **PCF85063** on the same I2C bus, powered through AXP2101 for retention. Not used by Clawdmeter but present for future features.
- Audio codec: **ES8311** + ES7210 ADC on the same I2C bus. The amp path is unverified on this board, so `sound.cpp` no-ops (same posture as the C6 1.8) — the shared `chime.cpp` engine is ready to wire up once it's tested on hardware.
- **No IO expander** despite the Waveshare wiki FAQ implying one. The schematic shows Key3/PWR wired directly to AXP2101 PWRON; touch reset and display reset are direct GPIOs. `board_init()` pulses LCD_RESET (GPIO 8) and TP_RESET (GPIO 9) before display/touch HAL init.
- Buttons: GPIO 0 (BOOT → Space/voice-mode), AXP PKEY (PWR → cycle screens; hold-to-pair). **No third button**.
- Flash: 32 MB. Uses `default_32MB.csv` partition table.

### LCD-4 — `waveshare_lcd_4`
- Display: **ST7701** 480×480 RGB parallel (DE=40, VSYNC=39, HSYNC=38, PCLK=41, R0-4=46/3/8/18/17, G0-5=14/13/12/11/10/9, B0-4=5/45/48/47/21); ST7701 init via SW SPI (CS=42, SCK=2, MOSI=1).
- Touch: **GT911** via I2C (SDA=15, SCL=7), polled (wiki INT=GPIO 16 unused). Probe 0x5D then 0x14.
- IO expander: **addr 0x24** (fallback 0x20) on the same I2C bus — must init before `gfx->begin()` (output 0xFF, config 0x3A). Backlight is expander pin 2.
- No PMU / IMU. Buttons: GPIO 0 only (BOOT → Space/PTT). KEY/PWR is EN/RST (hardware reset). GPIO 18 is display R3.
- RGB tearing fix: pass `bounce_buffer_size_px = LCD_WIDTH * 10` to `Arduino_ESP32RGBPanel`. Do not call `rgbpanel->getFrameBuffer()` after `gfx->begin()`.

## Architecture

```text
firmware/src/
  hal/                      — board-agnostic interfaces shared code calls into
    board_caps.h            — runtime BoardCaps struct (W, H, button_count, has_* flags)
    display_hal.h           — init / begin / set_brightness / draw_bitmap / tick / round_area
    touch_hal.h             — init / read(&x, &y, &pressed)
    input_hal.h             — init / is_held(PRIMARY|SECONDARY)
    power_hal.h             — init / tick / battery_pct / is_charging / pwr_pressed (edge)
    imu_hal.h               — init / tick / rotation_quadrant
  boards/
    waveshare_amoled_216/   — CO5300 + CST9220 + AXP PKEY + QMI8658 rotation
    waveshare_amoled_18/    — SH8601 + FT3168 + AXP + XCA9554 (PWR via EXIO4), no rotation
    waveshare_amoled_216_c6/— C6: SH8601 + CST9217 + AXP PKEY + ES8311 audio, no PSRAM
    waveshare_amoled_18_c6/ — C6: SH8601 + FT3168 + AXP PKEY + TCA9554 (gates power), no PSRAM
    waveshare_amoled_206/   — CO5300 + FT3168 + AXP PKEY, no IO expander, 32 MB, no rotation
    waveshare_lcd_154/      — ST7789 SPI TFT + CST816T + ADC battery (no PMU), PWM backlight
    waveshare_lcd_4/         — ST7701 RGB parallel + GT911 + expander backlight, no PMU/IMU
    sim/                    — native desktop simulator: SDL2 + Arduino shims + scenario playback
    template/               — copy this to bootstrap a new port
  main.cpp                  — setup() + loop(): HAL calls only, zero #ifdef BOARD_*
  ui.{h,cpp}                — tabbed UI: splash / usage / sessions / settings. The tab ring is
                              built at init from board_caps(), so a board without has_session_views
                              never has that tab; swipe left/right moves between tabs (wrapping).
                              Session cards are TAP TARGETS: a tap SELECTS a card and raises the
                              action bar (GO AHEAD / DISMISS, plus WAIT which does nothing on
                              purpose). With no cards left, the empty view holds the town hall
                              button, which fires a report round.
                              compute_layout() picks fonts/positions from board_caps() (responsive
                              — current breakpoint: H >= 460 → large, else compact)
  settings.{h,cpp}          — NVS-backed user settings behind the settings tab. One generic row
                              table (SPECS[]) the screen loops over, so adding a setting is one line.
  splash.{h,cpp}            — 20×20 pixel-art engine. CELL = min(W,H)/20, centered.
  ble.{h,cpp}               — NimBLE peripheral: custom data service + HID keyboard
  data.h                    — UsageData struct
  icons.h                   — icon arrays. Battery (5×) are RGB565A8 with alpha; rest are raw RGB565.
  logo.h                    — 80×80 RGB565 logo
  font_*.c                  — pre-compiled LVGL 9 bitmap fonts (Tiempos 56/34, Styrene 48/28/24/20/16/14/12, Mono 32/18)
  font_nanum_kr_28.c        — NanumGothic (SIL OFL), 28px/1bpp, the 2350 KS X 1001 Hangul syllables. Not a
                              standalone face: it is font_styrene_28's LVGL `.fallback`, wired at runtime in
                              ui.cpp, and only the SESSION_MESSAGE body uses it. Generated by tools/ttf_to_lvgl.py
                              (Python; there is no Node on every dev machine). See docs/fonts.md.
  splash_animations.h       — generated, do not hand-edit
docs/porting/               — adding-a-board.md, hal-contract.md, capability-flags.md
```

Each board folder contains: `board.h` (pins, I2C addresses, `BOARD_HAS_*` flags),
`board_init.cpp` (Wire.begin + any IO expander), `display.cpp`, `touch.cpp`,
`input.cpp`, `power.cpp`, `imu.cpp`, `caps.cpp` (the `BoardCaps` instance), plus
any board-private hardware drivers (e.g. `io_expander.{h,cpp}` on AMOLED-1.8).
PlatformIO's `build_src_filter` includes shared code + one board's folder per env.

## Build / flash

```bash
pio run -d firmware -e waveshare_amoled_216                                     # build 2.16 (S3, default original)
pio run -d firmware -e waveshare_amoled_18                                      # build 1.8 (S3)
pio run -d firmware -e waveshare_amoled_216_c6                                  # build 2.16 (C6)
pio run -d firmware -e waveshare_amoled_18_c6                                   # build 1.8 (C6)
pio run -d firmware -e waveshare_amoled_206                                     # build 2.06 (S3, watch)
pio run -d firmware -e waveshare_lcd_154                                        # build 1.54 (S3, SPI TFT)
pio run -d firmware -e waveshare_lcd_4                                           # build LCD-4 (S3, RGB TFT)
pio run -d firmware -e waveshare_amoled_18 -t upload --upload-port /dev/cu.usbmodem101   # flash 1.8 on macOS
pio run -d firmware -e waveshare_amoled_216 -t upload --upload-port /dev/ttyACM0         # flash 2.16 on Linux
# C6 boards: same native USB-JTAG flashing; flag a chip mismatch ("This chip is ESP32-C6,
# not ESP32-S3") means you picked an S3 env — use a *_c6 env for C6 hardware.
```

If `pio` isn't on PATH: try `~/.platformio/penv/bin/pio` (Linux/macOS pio install) or `brew install platformio` on macOS.

Device path differs by OS: `/dev/cu.usbmodem*` on macOS, `/dev/ttyACM0` on Linux. Both expose the ESP32-S3 native USB-JTAG (no boot-mode dance needed).

## Desktop simulator (`-e sim`) — develop UI without hardware

```bash
sudo apt install libsdl2-dev   # once (macOS: brew install sdl2)
pio run -d firmware -e sim && (cd firmware && .pio/build/sim/program)
```

Windows works too: the `native` platform is GCC-only, so install a MinGW-w64
GCC with SDL2 (MSYS2 `pacman -S mingw-w64-ucrt-x86_64-{gcc,SDL2}`, or a
winlibs zip + the SDL2 mingw dev zip) and put its `bin` on PATH; see
`SIM-USAGE.md` § Windows. `firmware/sim/sdl2_flags.py` (an `extra_scripts`
pre-script) locates SDL2 per OS, so there is no `!sdl2-config` backtick in
`platformio.ini` any more; on Windows it also copies `SDL2.dll` next to
`program.exe`. Note any `platformio.ini` edit makes PlatformIO wipe
`.pio/build/` for every env on the next run (full rebuild).

An SDL2 window stands in for the 480×480 panel; the **full firmware loop runs
unmodified** — `main.cpp`, `ui.cpp`, `splash.cpp`, idle fade, pair gesture,
JSON parsing, usage-rate/chime logic. Only `ble.cpp`/`chime.cpp` are swapped
for stubs. How it works: `boards/sim/` implements the HAL against SDL2, thin
Arduino shims live in `boards/sim/shim/` (`millis`/`Serial`→stdio,
`heap_caps`→malloc, in-memory `Preferences`), and `ble_sim.cpp` plays back
daemon payloads from `firmware/sim/scenario.jsonl` (one JSON line per state +
optional `name`/`hold_ms`; override with `SIM_SCENARIO=<path>`).

Controls (full map in `boards/sim/board.h`): mouse = touch · space =
play/pause scenario · ←/→ = step · 1-9 = jump · d = BLE link toggle ·
b/n = BOOT/secondary buttons · p = PWR · c/-/= = charging/battery ·
s = screenshot BMP · esc = quit.

Headless screenshots (works in CI, no display):
`SDL_VIDEODRIVER=dummy SIM_AUTOSHOT_MS=6000 .pio/build/sim/program` saves
`sim-autoshot.bmp` (or `SIM_AUTOSHOT_PATH`) after 6 s and exits. Combine with
the boot-screen swap trick below to capture any screen. **The sim renders with
desktop LVGL and fake data — always do a final check on real hardware before
merging panel-related changes** (col offsets, rotation, rounding live in the
hardware boards, not shared code).

## QA your own UI changes — don't ask the user

The firmware ships a `screenshot` serial command that dumps the LVGL framebuffer. `./screenshot.sh out.png [port]` captures a PNG sized to the active display (480×480 or 368×448). **Use this on every UI iteration** — Read the PNG with the Read tool, verify the change visually, iterate. Script auto-picks the macOS/Linux default port and falls back to pio's bundled Python if pyserial isn't on the system Python.

The boot screen is `SCREEN_SPLASH` and only advances on a physical button press, so a fresh flash will sit on the splash. To screenshot the screen you're actually editing without asking the user to press a button, **temporarily change the default boot screen** in `main.cpp` (search for `ui_show_screen(SCREEN_SPLASH);`) to `SCREEN_USAGE` / `SCREEN_CONTROLLER` / `SCREEN_BLUETOOTH`, do your iteration, then revert before committing.

## Critical gotchas

1. **CO5300 cannot rotate.** Its MADCTL only supports axis flips, not column/row exchange. Rotation is done by **CPU pixel remapping inside `display_hal_draw_bitmap`** in `boards/waveshare_amoled_216/display.cpp`. We use **PARTIAL render mode with strip rotation** (small 480×40 strips, fast). On rotation change → AMOLED brightness flash → force redraw (handled inside `display_hal_tick`).
2. **OPI PSRAM** required: `board_build.arduino.memory_type = qio_opi` in platformio.ini. Without this, `MALLOC_CAP_SPIRAM` returns NULL and the screen is black.
3. **pioarduino platform required.** GFX Library for Arduino needs Arduino Core 3.x (`esp32-hal-periman.h`), not the 2.x that standard `espressif32` ships. We pin `pioarduino/platform-espressif32` 55.03.38-1.
4. **LVGL 9 font patching.** `lv_font_conv` outputs LVGL 8 format. Must remove `#if LVGL_VERSION_MAJOR >= 8` guards, drop `.cache` field, add `.release_glyph`, `.kerning`, `.static_bitmap`, `.fallback`, `.user_data`. Without patching, fonts render invisible. Full regeneration recipe: `docs/fonts.md`.
5. **Touch reading is centralized inside each board's `touch.cpp`.** The HAL `touch_hal_read()` is called once per loop from `my_touch_cb`; the board's implementation owns its latched `touch_pressed/x/y` state. Don't call the underlying controller from anywhere else — CST9220's `getPoint()` etc. do a full I2C transaction and concurrent callers consume each other's data.
6. **Even-aligned flush regions.** `display_hal_round_area` (called from `rounder_cb`) is what each board uses to enforce this. Required on CO5300, harmless on SH8601.
7. **Touch axis swap/mirror is per-board.** The 2.16's CST9220 needs `setSwapXY(true)` + `setMirrorXY(true, false)` — applied inside `boards/waveshare_amoled_216/touch.cpp::touch_hal_init()`. New ports apply their own.
8. **LVGL RGB565A8 is planar.** `w*h` RGB565 pixels followed by `w*h` alpha bytes; `data_size = w*h*3`, `stride = w*2`. Use `init_icon_dsc_rgb565a8()` for icons that overlap non-uniform backgrounds (e.g. battery over splash). Lucide source PNGs are black-on-transparent — converter must tint to white or icons render invisible. See `tools/png_to_lvgl.js`.
9. **Per-board pre-init is `board_init()`.** Each board's `board_init.cpp` brings up `Wire` and any reset-gating IO expander BEFORE `display_hal_init()`. Skipping the IO expander release on AMOLED-1.8 leaves SH8601 + FT3168 in reset and they silently fail to probe. Same for LCD-4: expander @ 0x24 must run before `gfx->begin()` or the ST7701 stays dark.
10. **No `#ifdef BOARD_*` in shared code.** The whole point of the refactor — if you're about to add one, you probably want a `BoardCaps` field or a per-board file instead. See `docs/porting/capability-flags.md`.
11. **LCD-4 RGB bounce buffers.** `Arduino_RGB_Display` DMA-scans PSRAM. Pass `bounce_buffer_size_px = LCD_WIDTH * 10` so ESP-IDF allocates SRAM bounce buffers. Do not call `rgbpanel->getFrameBuffer()` after `gfx->begin()` — it constructs a second RGB panel and crashes.
12. **LCD-4 has only one user button (GPIO 0 / BOOT).** GPIO 18 is display R3. KEY/PWR is EN/RST (hardware reset). Hold-to-pair and PWR-short animation/brightness cycling are unavailable; tap the panel to toggle splash ↔ usage.

## Icons

`tools/png_to_lvgl.js <input.png> <symbol> [W_MACRO] [H_MACRO] [--tint=RRGGBB | --no-tint]` converts an alpha PNG to RGB565A8. Default tint is white (`0xFFFFFF`) — necessary for Lucide PNGs. Splice output into `firmware/src/icons.h` and use `init_icon_dsc_rgb565a8()` in ui.cpp. Currently only the 5 battery icons use this format; the rest are still raw RGB565 baked over the panel background, fine because they live inside opaque zones.

## Splash animations

17 official Anthropic Clawd animations (core poses + persona scenes), archived
with full provenance in `research/clawd-official/`. Pipeline:

```bash
node tools/convert_official_clawd.js            # → firmware/src/splash_animations.h
node tools/convert_official_clawd.js --verify DIR   # + per-animation PNGs for eyeballing
```

Requires ImageMagick; Laptop and Soccer convert from their Lottie exports
(crisp) rather than GIFs. Frames are bounding-box crops on the official 55×37
art stage (ox/oy = stage offset — every animation shares one idle-Clawd
position, so transitions are seamless), one byte per cell into a per-animation
≤16-color RGB565 palette (index 0 = background, true black), per-frame hold ms
with duplicates collapsed (~400 KB total). The converter also: detects each
animation's **loop region** (gait cycles, scene middles; sailing scene's is
located by cross-matching the standalone sailing-loop asset, which is not
emitted), synthesizes the **eyes** (transparent holes in the source GIFs) as
`#141413` ink via border flood-fill, and applies two contrast recolors
(trumpet notes → ivory, magnifier fedora → gray) via component analysis.

The splash engine (`splash.cpp`) plays intro → loop → outro on a **60×60
stage** (`SPLASH_GRID`, cell = min(W,H)/60 → 8 px on 480, 6 px on 368, 4 px on
240): loops hold until released (walk arrival, scene timer, rotation), so
switches always pass through the shared idle pose. Walkers translate with
foot-locked per-frame schedules and mirror when heading left. Usage-rate
groups pick animations by name; the same rate drives the **corner mascot** on
the usage screen (`splash_mascot_*`, PSRAM boards; C6 falls back to the static
`clawd_still.h` icon) — idle stills, rate-scaled acts, and walk-off/lurk/
walk-back trips. Default boot screen.

**Where the animations come from / finding new ones:** all assets are plain
files under `https://claude.ai/images/clawd/{core,persona}/…` — static assets
are not Cloudflare-gated, only HTML routes are. The asset server returns a
real GIF for a valid filename and an HTML catch-all (both HTTP 200) otherwise,
so **name probing works**: fetch `Clawd-<Name>.gif` and check the magic bytes.
Seven current animations are referenced by no shipped bundle and were found
exactly this way (Anthropic stages seasonal drops — Soccer appeared for the
World Cup). To hunt for new ones: run `research/clawd-official/fetch.sh`
(extend its probe list), and grep a fresh desktop .deb's `ion-dist/` bundles
for `/images/` paths (`research/clawd-official/CLAUDE.md` documents the full
methodology, including the Lottie sources and the assets-proxy).


## User profile / preferences

See `~/.claude/projects/.../memory/` files for persistent context (user is an embedded-beginner senior dev, brand-conscious, prefers iterative UI refinement, dislikes me authoring my own art when third-party assets are intended). Always read those memory files at session start.

## Recent session highlights

- **The sessions tab became something you act on (2026-09-09).** A tap
  **selects** a card and raises an action bar: `GO AHEAD` on a
  `SESSION_REPORT_NEEDS_YOU` report, `DISMISS` on anything else, and `WAIT`
  on both. The first cut acted on the tap itself and had to be redone — it
  left no way to be *done* with a card you had decided to answer on a
  keyboard, which is the normal case. **WAIT sends nothing and clears
  nothing**, and neither does GO AHEAD remove its card: the row goes when the
  AGENT moves and the host stops sending it, so a card on screen always means
  a session that still needs somebody. Between the press and that moment the
  chip reads `go ahead sent` and the pulse stops (keyed on the row's content
  hash, so it lapses the instant the agent says anything new). The three other
  report states deliberately do *not* offer go-ahead — `BLOCKED` is parked on
  a permission dialog on another machine, where a message queues *behind* the
  dialog, and `WORKING`/`DONE` are not waiting for anything.
  Clearing the last card reveals the **town hall button** — a terracotta
  circle on the empty view that fires the same round the hardware button
  does. It exists only there: with cards on screen the meeting has already
  happened. Dismissal is keyed on the **words** at both ends (the firmware on
  a hash of sid+label+body+state, the host on the message id, itself a hash of
  session+sender+body), so the same agent saying something new comes back —
  suppressing by *name* would silence it for good. The device hides a card
  under the finger with no round trip; `~/.clawdmeter/dismissed.json` (one
  writer, one reader, hour TTL, 64 entries) only stops the host re-sending it.
  Also fixed here: **the volume curve was wrong by 60 dB of range.**
  `es8311_voice_volume_set` maps a percentage to DAC register 0x32 as
  `pct * 256 / 100 - 1`, and that register is **half a decibel per step** with
  0 dB at 0xBF — so the evenly-spaced-looking 40/65/90 was −45 / −13 / +19 dB,
  two settings barely audible and one clipping the bell (whose peak measures
  −7.0 dBFS) by twelve. Now 70/75/80 = −6.5 / 0 / +6.0 dB.
  And the console window that flashed on every press: the daemon spawns the
  dispatcher windowless, but the dispatcher's own two spawns did not pass
  `CREATE_NO_WINDOW`, and under `pythonw` a console child gets its own window.

- **Scroll performance on the C6, and three queued UI changes (2026-09-09).**
  A drag of the chat list ran at 8.7 fps (render 72.9 ms, transfer 33 ms,
  loop 114 ms). It now runs at 11.3 fps, render 48.5 ms mean / 53 ms worst,
  with no full-screen repaints at all. Two changes, both pixel-for-pixel
  invisible: (1) **one draw buffer instead of two.** The flush is blocking and
  calls `lv_display_flush_ready` immediately, so LVGL never overlapped a
  transfer with rendering and the second buffer was idle memory; the same
  38 KB spent on one 40-line strip halves the strip count (17 -> 9) and with
  it every per-strip cost. Worth ~19 ms. (2) **the idle tier is a palette, not
  an opacity layer.** `LV_OPA_60` on a card made LVGL alpha-blend the whole
  subtree, per pixel, per strip; the identical pixels come out of
  `lv_color_mix(colour, background, 60%)` drawn opaque (`card_col()` in
  ui.cpp). Worth 8.5 ms - the ablation now prices the tier at 0.3 ms.
  Also landed: the **report button** (`ble_send_report_request()` on the press
  edge; it no longer sends HID Shift+Tab into whatever window had focus), a
  **capped overscroll** (`clamp_overscroll()` takes LVGL's elastic flag away at
  the cap rather than scrolling against it), and a **volume preview** -
  changing the Sound level plays the chime's first note at the new level
  (`sound_hal_play_preview()` -> `chime_play_preview()`).
  Two measurement notes worth keeping. The 198 ms spikes and 343-invalidation
  bursts an earlier pass blamed on the pulse and the AUTO scrollbar were the
  harness's own screen-switch frames: in steady state both cost about nothing
  and a drag runs at exactly 2 invalidations per frame. And what is left is
  **text - 29 ms of the 48**, because LVGL 9.5 expands every 4bpp glyph to A8
  on every draw (`lv_font_get_bitmap_fmt_txt`); the zero-copy path wants
  `bpp = 8` plus `static_bitmap`, which costs font flash and is the obvious
  next move. Corner radius is 4.8 ms, the floor is 9 ms, and past ~20 fps the
  QSPI transfer binds.

- **Sessions tab became an attention surface (2026-09-09).** Three changes, one
  idea. (1) The remote fleet poller now ships only rows that need a *person* —
  `requires_action`, messages, agent reports — and drops idle/working remote
  sessions before they cost a byte (`fleet_attention_only`, on by default; a
  real payload went from nine rows / 424 B to `{"ss":[]}`). The empty tab is
  now the normal, healthy screen and says `Nothing needs you`. (2) New wire
  state **17 `SESSION_HOST_STALE`**: the host mints one dim card when its
  listing has been failing for `fleet_stale_after_s` (900 s) and drops the rows
  it can no longer vouch for. The device cannot infer this — the poller writes
  only on change, so silence is ambiguous — which is how a 9-hour-old list sat
  on the panel all day after a token expired. (3) The poller got an autostart
  entry (`ClawdmeterFleet`), a single-instance mutex, a heartbeat, and a tray
  supervisor that restarts it; its `log()` is now guarded, because
  `print(file=sys.stderr)` under a bare `pythonw` Run entry raises
  `AttributeError` — from inside the HTTP-error handler, which would kill the
  poller on its first 401. See `daemon/FLEET.md`.
- **AMOLED-1.8 chime verified on hardware + EXIO2 touch-kill fix (2026-07-13).** The 1.8's `amp_enable` hook drove both GPIO 46 and XCA9554 EXIO2 ("the unused one is harmless") — but pulling EXIO2 low takes the FT3168 off the I2C bus (chip stops ACKing; IDF reports it as `ESP_ERR_INVALID_STATE`, which reads like a driver wedge and cost a long I2S red-herring chase). Amp enable is GPIO 46 only; EXIO2 must stay HIGH. Chime, touch, buttons, and BLE bond persistence all verified on a real 1.8.
- **Device-abstraction refactor (2026-05-18).** All board-conditional code moved out of shared files into `boards/<name>/` and behind a HAL in `hal/`. ~30 `#ifdef BOARD_*` blocks went to zero. UI is responsive via `compute_layout()` driven by `board_caps()`. New ports add a folder + a PlatformIO env — no shared file edits.
- Added second board port: Waveshare AMOLED-1.8 (368×448 portrait, SH8601, FT3168, XCA9554 IO expander).
- Migrated from Panlee SC01 Plus (480×320 IPS) to Waveshare 2.16" AMOLED (480×480 square). Full hardware/library swap.
- Added IMU auto-rotation, battery indicator, USB-state-aware screen switching.
- Added splash screen with scraped pixel-art animations and 3-button physical input layout.
- Fonts and icons re-scaled ~1.9× for the higher-DPI panel.
- All UI margins widened to 20px to clear the rounded display corners.
- Battery icons converted to RGB565A8 alpha so they blend cleanly over the splash animations.

## Daemon / host side

Bash daemon (`daemon/claude-usage-daemon.sh`) reads OAuth token, polls Anthropic API, sends JSON over BLE GATT. Run with `systemctl --user start claude-usage-daemon`. The unit file's `ExecStart` is the absolute path to the script — repoint it when switching between the worktree and the main checkout.

**Session rows and agent reports:** `daemon/FLEET.md` covers the remote-fleet
poll and the cross-session message inbox; `daemon/REPORT.md` covers **agent
reports** — a one-line contract (`CLAWDMETER-REPORT/1 <STATE>: <summary>`) an
agent replies with, which the inbox turns into a message-shaped card whose
*state* (wire codes 12–16, `SESSION_REPORT_*` in `data.h`) drives its colour,
its sort bucket and the auto-jump. Report rounds want
`sessions_budget_bytes = 500`; the arithmetic is in REPORT.md.

**Discovery & resilience:**

- Connects by name (`"Clawdmeter"`) on first run, caches resolved MAC at `~/.config/claude-usage-monitor/ble-address`. ESP32 BLE addresses are factory-burned per-chip, so swapping any board invalidates the cache.
- On connect failure: cache is dropped AND device is removed from bluez (`bluetoothctl remove`) so the next scan won't re-pick a dead MAC. Multi-candidate scans pick `head -1` and let the failure cycle converge.
- `POLL_INTERVAL=60`, `TICK=5`. Inner loop wakes every 5s to detect disconnects fast; polls Anthropic when 60s elapsed OR when ESP fires a refresh request.

**GATT characteristics on service `4c41555a-...0001`:**

- `...0002` RX — daemon writes JSON usage payload here.
- `...0003` TX — firmware notifies ack/nack **and device button events**, to the
  bonded owner only. Events are `{"ev":<code>}` (plus an optional `"sid"`); the
  `ev` key is the discriminator the ack traffic does not carry, and codes are
  append-only. `1` = report round; `2` = go-ahead (carries a `sid`); `3` =
  dismiss (carries a `sid`). Every TX notify goes
  through `tx_notify_owner()` in `ble.cpp` — per-connection-handle, encrypted
  links only, owner address only — because NimBLE's bare `notify()` fans out to
  every subscribed peer. The Windows daemon subscribes and runs a report round
  as a child process off the poll tick; see `daemon/REPORT.md` § The button.
  A `sid` is two characters minted by the *poller*, not the daemon, so the
  sidecar's handoff file carries an `index` (`sid` → state / raw sender /
  message id) — without it a tap cannot be turned back into an agent to
  message.
- `...0004` REQ — firmware fires `0x01` notify in `onSubscribe` if `has_received_data` is false. Daemon subscribes via `setsid bash -c "stdbuf -oL dbus-monitor … | awk …"`; awk drops a flag file the inner loop picks up. See the `feedback_dbus_monitor_pipe` memory for the three subtle gotchas (pipe buffering, busctl-exits race, `wait` blocking on pipeline jobs).
