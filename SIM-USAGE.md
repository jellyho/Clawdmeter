# Desktop simulator (`-e sim`) — usage

The native desktop simulator runs the full firmware loop in an SDL2 window
standing in for the 480×480 AMOLED — `main.cpp`, `ui.cpp`, `splash.cpp`, idle
fade, pair gesture, JSON parsing, and usage-rate/chime logic all run
unmodified. Only `ble.cpp`/`chime.cpp` are swapped for stubs. Sources live in
`firmware/src/boards/sim/` (HAL against SDL2 + Arduino shims in `shim/`), with
scenario data in `firmware/sim/`.

## Build & run

```bash
sudo apt install libsdl2-dev        # one-time (macOS: brew install sdl2)
pio run -d firmware -e sim
cd firmware && .pio/build/sim/program
```

Launch from the `firmware/` directory — the default scenario path
(`sim/scenario.jsonl`) is resolved relative to it.

### Windows

PlatformIO's `native` platform is GCC-only, so the sim needs a **MinGW-w64
GCC** with SDL2 installed into that toolchain. `firmware/sim/sdl2_flags.py`
finds SDL2 next to the `gcc` on PATH (or via `SDL2_DIR`), links it as a
console app without `SDL2main`, and copies `SDL2.dll` beside the executable.
Pick one:

- **MSYS2** (installer): `pacman -S mingw-w64-ucrt-x86_64-gcc
  mingw-w64-ucrt-x86_64-SDL2`, then put `C:\msys64\ucrt64\bin` on PATH.
- **No installer**: unzip a [winlibs](https://winlibs.com) GCC
  (`winlibs-x86_64-posix-seh-gcc-*-ucrt-*.zip`) to e.g.
  `%LOCALAPPDATA%\Programs\mingw64` and put its `bin` on PATH. Unzip
  `SDL2-devel-<ver>-mingw.zip` from the
  [SDL releases](https://github.com/libsdl-org/SDL/releases) and either set
  `SDL2_DIR` to its `x86_64-w64-mingw32` folder, or copy that folder's
  `include\SDL2`, `lib\libSDL2*.a`, and `bin\SDL2.dll` into the mingw64
  prefix.

Then, from a fresh terminal (PATH changes need one):

```powershell
pio run -d firmware -e sim
cd firmware; .pio\build\sim\program.exe
```

Headless capture on Windows: `$env:SDL_VIDEODRIVER='dummy';
$env:SIM_AUTOSHOT_MS='6000'; .pio\build\sim\program.exe`.

## Controls

| Key | Action |
|---|---|
| mouse / left-drag | touch (tap toggles splash ↔ usage) |
| `space` | play/pause scenario playback |
| `←` / `→` | step one scenario state (pauses playback) |
| `1`–`9` | jump to scenario state N (pauses playback) |
| `d` | toggle BLE connected/disconnected |
| `b` (hold) | PRIMARY button (BOOT — HID Space PTT on hardware) |
| `n` (hold) | SECONDARY button (HID Shift+Tab on hardware) |
| `p` | PWR button (short press; hold ~3s + release = pair gesture) |
| `c` | toggle charging |
| `-` / `=` | battery down / up 5% |
| `s` | save screenshot BMP to the current directory |
| `esc` / window close | quit |

Full, authoritative map: `firmware/src/boards/sim/board.h`.

## Scenarios

`firmware/sim/scenario.jsonl` plays in a loop — one JSON object per line, the
daemon payload plus two optional keys:

- `"name"` — shown in the window title
- `"hold_ms"` — time on this state (default 3000)

Lines starting with `#` are comments. Lines containing an `"ss"` array are
**session payloads** (issue #135 wire format) and go out on the session
characteristic path; everything else is a quota payload.

Session row format:

```
[sid, label, state, ctx%, elapsed_s, model, tool, ntools, nagents, tdone, ttotal, tok]
```

States: 0 starting · 1 idle · 2 thinking · 3 responding · 4 running-tool ·
5 compacting · 6 needs-permission · 7 asking-you · 8 needs-input · 9 error.
`tok` is context tokens in 1k units (190 = 190k); `-1`/absent = unknown.

Override the scenario file with `SIM_SCENARIO=<path>`. If the file is missing,
a small built-in state list is used.

## Headless screenshots (CI-friendly)

```bash
SDL_VIDEODRIVER=dummy SIM_AUTOSHOT_MS=6000 .pio/build/sim/program
```

Saves `sim-autoshot.bmp` (override with `SIM_AUTOSHOT_PATH`) after the given
delay and exits. Combine with `SIM_SCENARIO` pointing at a single-state file
to capture any specific screen.

## Caveat

The sim mirrors the S3 2.16 geometry but renders with desktop LVGL and fake
data. It's ideal for iterating UI layouts, but panel-level behavior — column
offsets, rotation, flush rounding — lives in the hardware board folders, so
always do a final check on real hardware before merging panel-related changes.
