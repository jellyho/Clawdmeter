#include "splash.h"
#include "splash_animations.h"
#include "splash_geometry.h"
#include "theme.h"
#include "usage_rate.h"
#include "hal/board_caps.h"
#include "hal/display_hal.h"
#include <Arduino.h>
#include <string.h>
#include <esp_heap_caps.h>

// 60×60 stage. CELL sized so the canvas fits the smaller display dimension —
// the canvas is square and centered, so on portrait or letterboxed panels
// it leaves vertical margin rather than cropping. On PSRAM-less boards the
// buffer is rendered tiny (cell == 1) and LVGL scales it up to fill the panel;
// the geometry decision lives in splash_compute_geometry() (splash_geometry.h).
//
// Animations are stored as bounding-box crops of the official 55×37 art stage
// (see tools/convert_official_clawd.js); compose_stage() places the current
// frame centered on the 60×60 stage. The oversized stage leaves room to later
// translate animations across the screen (walks, lurking).
#define GRID         SPLASH_GRID
static int  cell      = 8;         // recomputed in splash_init()
static int  canvas_w  = GRID * 8;
static int  canvas_h  = GRID * 8;

// Splash background: true black (matches THEME_BG and palette index 0
// emitted by tools/convert_official_clawd.js). Used for the stage margins
// and as palette fallback.
#define COL_EMPTY    0x0000

LV_FONT_DECLARE(font_styrene_28);

static lv_obj_t *splash_container = NULL;
static lv_obj_t *canvas = NULL;
static lv_obj_t *label_status = NULL;     // shown only when no animations loaded
static uint16_t *canvas_buf = NULL;        // 480x480 RGB565 (PSRAM)

static uint16_t cur_anim = 0;
static uint16_t cur_frame = 0;
static uint32_t frame_started_ms = 0;
static uint32_t last_pick_ms = 0;
static bool active = false;

// While splash is showing, auto-cycle to the next animation in the current
// rate-driven group every this many ms.
#define SPLASH_ROTATE_INTERVAL_MS 20000

// Usage-rate animation groups: 4 groups × up to 4 animations each.
// Filled at init by matching literal names from splash_anims[].
// (jumping is the only unassigned animation — still reachable via splash_next.)
#define GROUP_COUNT 4
#define GROUP_MAX   4
static int8_t  group_lists[GROUP_COUNT][GROUP_MAX];
static uint8_t group_size[GROUP_COUNT] = {0};
static uint8_t group_rotation[GROUP_COUNT] = {0};

static const char* GROUP_NAMES[GROUP_COUNT][GROUP_MAX] = {
    // Group 0 — idle / sleepy (calm, investigative). Magnifier first: it's
    // the boot pick, and lurking-first would boot to a near-empty screen.
    { "magnifier", "walking", "pointing", "lurking" },
    // Group 1 — normal pace
    { "crab walking", "waving", "trumpet", "basketball" },
    // Group 2 — active (typing along with you)
    { "laptop", "dancing", "skateboard", "soccer" },
    // Group 3 — heavy burn (high-energy rides + the most exuberant jump)
    { "racing car", "cloud", "sailing scene", "jumping happy" },
};

// Scratch stage: the current animation frame composed centered onto the full
// 60×60 grid (index 0 = background elsewhere). 3.6 KB of static RAM.
static uint8_t stage_cells[GRID * GRID];

// The official 55×37 art stage sits at a fixed anchor on the 60×60 grid, and
// every animation is placed at its authored stage offset (ox/oy) — never
// centered per-animation. All animations share one idle-Clawd position
// (x 15..38, y 21..36 in stage cells), so transitions between them are
// seamless; centering per-crop would make the still pose jump around.
#define STAGE_ANCHOR_X ((GRID - 55) / 2)
#define STAGE_ANCHOR_Y ((GRID - 37) / 2)

// ─── Playback: intro → loop → outro ─────────────────────────────────────────
// Every animation carries a loop region (converter-detected gait cycles and
// scene middles; whole file when nothing repeats). Playback holds the loop
// until released — walkers release on arrival at their target x, scenes after
// SCENE_LOOP_MS — then the outro (pack-away, gait exit) plays and the
// animation completes on its idle bookend. Rotation never hard-cuts: it
// releases the loop and switches after the outro, so transitions always
// happen from the shared idle pose.
static bool     pb_done = false;        // completed; holding idle frame 0
static bool     in_loop = false;
static bool     loop_release = false;
static uint32_t loop_entered_ms = 0;
static bool     pending_pick = false;   // rotate requested; honor at completion
#define SCENE_LOOP_MS 6000

// ─── Walk translation ────────────────────────────────────────────────────────
// The walk gaits animate in place; screen travel is ours, locked to the feet:
// per-frame movement equals the measured backward drift of the planted feet,
// so planted feet stay put on screen.
//   crab walking (8-frame scuttle loop [1..8]): surges of 1 cell entering
//     frames 4, 5, 8 and the cycle wrap — 4 cells / 640 ms (6.25 cells/s).
//   walking (5-frame waddle loop [2..6]): 1,1,1,1,2 cells → 6 cells / 450 ms
//     (~13.3 cells/s).
// walk_begin(target) plays intro → gait loop, clamps to land exactly on the
// target, then releases the loop so the gait exits and Clawd stands. When
// walking left the frame is mirrored (eyes lead); facing persists standing.
// DEMO: until the BLE-driven state machine exists, a choreography loops
// stand → right edge → off-screen left → re-enter home.
enum WalkKind { WALK_NONE, WALK_CRAB, WALK_FRONT };
static WalkKind walk_kind = WALK_NONE;
static bool    walk_active = false;
static int     walk_x = 0;         // stage x of the frame origin, may be < 0
static int     walk_dir = 0;       // -1 left, +1 right, 0 standing
static int     walk_target = 0;
static uint8_t walk_phase = 0;
static uint32_t walk_phase_started = 0;
static int     walk_home_x = 0;    // authored position to return to
static int     walk_face = +1;     // facing, kept while standing (-1 = left)

// Cells the body moves when the gait advances INTO `frame` (see banner).
static int walk_gait_cells_k(WalkKind kind, uint16_t frame, bool from_loop) {
    if (kind == WALK_CRAB) {
        if (frame == 1) return from_loop ? 1 : 0;     // cycle wrap, mid-surge
        return (frame == 4 || frame == 5 || frame == 8) ? 1 : 0;
    }
    if (kind == WALK_FRONT) {
        if (frame < 2 || frame > 6) return 0;         // idle / wind-up / outro
        if (frame == 2 && !from_loop) return 0;       // first plant
        return (frame == 6) ? 2 : 1;
    }
    return 0;
}
static int walk_gait_cells(uint16_t frame, bool from_loop) {
    return walk_gait_cells_k(walk_kind, frame, from_loop);
}

static void anim_reset(const splash_anim_def_t *a) {
    pb_done = false;
    in_loop = false;
    loop_release = false;
    pending_pick = false;
    walk_active = false;
    walk_kind = WALK_NONE;
    if (strcmp(a->name, "crab walking") == 0) walk_kind = WALK_CRAB;
    else if (strcmp(a->name, "walking") == 0) walk_kind = WALK_FRONT;
    else return;
    walk_active = true;
    walk_home_x = STAGE_ANCHOR_X + a->ox;
    walk_x = walk_home_x;
    walk_dir = 0;
    walk_face = +1;
    walk_phase = 0;
    walk_phase_started = millis();
    pb_done = true;    // walkers start standing; the choreography sets off
}

static const uint8_t* compose_stage(const splash_anim_def_t *a, uint16_t frame);
static void render_frame(const uint8_t *cells, const uint16_t *palette);

// Start walking toward `target` (stage x of the frame origin).
static void walk_begin(int target) {
    if (target == walk_x) return;          // already there; stay standing
    walk_target = target;
    walk_dir = (target > walk_x) ? +1 : -1;
    walk_face = walk_dir;
    cur_frame = 0;
    frame_started_ms = millis();
    pb_done = false;
    loop_release = false;
    in_loop = false;
}

// Demo choreography: advance phases whenever the current walk has completed.
static void walk_choreo(const splash_anim_def_t *a) {
    if (!pb_done) return;
    const uint32_t now = millis();
    switch (walk_phase) {
        case 0:  // standing at home
            if (now - walk_phase_started > 1200) { walk_phase = 1; walk_begin(GRID - a->w); }
            break;
        case 1:  // arrived at the right edge
            walk_phase = 2; walk_phase_started = now;
            break;
        case 2:  // standing at the edge
            if (now - walk_phase_started > 1200) { walk_phase = 3; walk_begin(-a->w); }
            break;
        case 3:  // fully off-screen left
            walk_phase = 4; walk_phase_started = now;
            break;
        case 4:  // hold off-screen (empty stage)
            if (now - walk_phase_started > 800) { walk_phase = 5; walk_begin(walk_home_x); }
            break;
        case 5:  // back home
            walk_phase = 0; walk_phase_started = now;
            break;
    }
}

static const uint8_t* compose_stage(const splash_anim_def_t *a, uint16_t frame) {
    memset(stage_cells, 0, sizeof(stage_cells));
    // Horizontal edge snap: art touching its canvas's left/right edge was
    // designed to hang off that edge (lurking peeks in from the left), so it
    // goes to the true screen edge instead of the anchored stage edge. Not
    // applied vertically — every animation touches the stage bottom, and
    // vertical placement should stay anchored (rounded panel corners).
    int ax = STAGE_ANCHOR_X + a->ox;
    if (a->ox == 0)           ax = 0;
    if (a->ox + a->w == 55)   ax = GRID - a->w;
    if (walk_active)          ax = walk_x;
    const bool mirror = walk_active && walk_face < 0;
    const int ay = STAGE_ANCHOR_Y + a->oy;
    const uint8_t *src = &a->frames[(size_t)frame * a->w * a->h];
    for (int r = 0; r < a->h; r++) {
        const int dy = ay + r;
        if (dy < 0 || dy >= GRID) continue;
        int c0 = 0, c1 = a->w;                 // clip for partial off-screen x
        if (ax + c0 < 0)     c0 = -ax;
        if (ax + c1 > GRID)  c1 = GRID - ax;
        if (c0 >= c1) continue;
        if (mirror) {
            for (int c = c0; c < c1; c++)
                stage_cells[dy * GRID + ax + c] = src[r * a->w + (a->w - 1 - c)];
        } else {
            memcpy(&stage_cells[dy * GRID + ax + c0], &src[r * a->w + c0], c1 - c0);
        }
    }
    return stage_cells;
}

static void resolve_group_lists(void) {
    for (int g = 0; g < GROUP_COUNT; g++) {
        group_size[g] = 0;
        for (int s = 0; s < GROUP_MAX; s++) {
            group_lists[g][s] = -1;
            const char* want = GROUP_NAMES[g][s];
            if (!want) continue;
            for (int i = 0; i < SPLASH_ANIM_COUNT; i++) {
                if (strcmp(splash_anims[i].name, want) == 0) {
                    group_lists[g][group_size[g]++] = (int8_t)i;
                    break;
                }
            }
        }
    }
}

static uint16_t *row_buf = NULL;   // scratch row, sized to canvas_w (PSRAM path)

// ─── Two render paths ────────────────────────────────────────────────────────
// PSRAM boards (S3) draw the pixel art into an LVGL canvas at native size and
// let LVGL flush it — they have the RAM and cores to spare, no transform needed.
//
// PSRAM-less boards (C6) can't hold a 480×480 canvas. The prior approach (tiny
// 20×20 canvas + LVGL image-scale) made LVGL software-transform the whole
// upscaled frame on every redraw — measured ~0.76 µs/output-px, i.e. 100–220 ms
// per frame on the single-core C6, and partial invalidation of a transformed
// image both fails to clip the transform and smears. Instead we upscale the
// stage cells ourselves with trivial nearest-neighbour replication and push only
// the *changed* cells straight to the panel via the display HAL, bypassing LVGL.
// That removes the transform cost (leaving just the QSPI flush) and the
// dirty-rect is exact, so no smearing.
#ifndef BOARD_HAS_PSRAM
#  define SPLASH_DIRECT_DRAW 1
#else
#  define SPLASH_DIRECT_DRAW 0
#endif

#if SPLASH_DIRECT_DRAW
static uint16_t*       strip_buf = NULL;   // one grid-row band: (GRID*scr_cell)×scr_cell
static int             scr_cell  = 24;     // on-screen px per grid cell
static int             scr_offx  = 0;      // centering offsets (square art on panel)
static int             scr_offy  = 0;
static uint8_t         prev_cells[GRID * GRID];
static const uint16_t* prev_palette = NULL;
static bool            prev_valid   = false;
static bool            force_full   = false;  // repaint everything on the next render

// Upscale grid cells [gx0..gx1]×[gy0..gy1] and push them to the panel, one
// grid-row band at a time so the scratch buffer stays (GRID*scr_cell × scr_cell).
static void blit_cells(const uint8_t* cells, const uint16_t* palette,
                       int gx0, int gy0, int gx1, int gy1) {
    if (!strip_buf) return;
    const int spc = scr_cell;
    const int bw  = (gx1 - gx0 + 1) * spc;          // band width, px
    const int px  = scr_offx + gx0 * spc;
    for (int gy = gy0; gy <= gy1; gy++) {
        for (int gx = gx0; gx <= gx1; gx++) {       // expand one source row across
            uint8_t code = cells[gy * GRID + gx];
            uint16_t color = (palette && code < SPLASH_PALETTE_SIZE) ? palette[code] : COL_EMPTY;
            uint16_t* p = &strip_buf[(gx - gx0) * spc];
            for (int i = 0; i < spc; i++) p[i] = color;
        }
        for (int dy = 1; dy < spc; dy++)             // replicate that row down
            memcpy(&strip_buf[dy * bw], strip_buf, bw * 2);
        display_hal_draw_bitmap(px, scr_offy + gy * spc, bw, spc, strip_buf);
    }
}

static void render_frame(const uint8_t *cells, const uint16_t *palette) {
    if (!strip_buf) return;
    if (!active) return;          // never draw to the panel while not shown
    bool full = force_full || !prev_valid || palette != prev_palette;
    force_full = false;

    int gx0 = 0, gy0 = 0, gx1 = GRID - 1, gy1 = GRID - 1;
    if (!full) {                                     // bounding box of changed cells
        gx0 = GRID; gy0 = GRID; gx1 = -1; gy1 = -1;
        for (int gy = 0; gy < GRID; gy++)
            for (int gx = 0; gx < GRID; gx++)
                if (cells[gy * GRID + gx] != prev_cells[gy * GRID + gx]) {
                    if (gx < gx0) gx0 = gx;
                    if (gx > gx1) gx1 = gx;
                    if (gy < gy0) gy0 = gy;
                    if (gy > gy1) gy1 = gy;
                }
        if (gx1 < 0) return;                         // identical frame, nothing to do
    }

    blit_cells(cells, palette, gx0, gy0, gx1, gy1);

    memcpy(prev_cells, cells, GRID * GRID);
    prev_palette = palette;
    prev_valid   = true;
}

#else  // ── PSRAM: LVGL canvas render (unchanged) ──

static void render_frame(const uint8_t *cells, const uint16_t *palette) {
    if (!row_buf || !canvas_buf) return;
    for (int gy = 0; gy < GRID; gy++) {
        for (int gx = 0; gx < GRID; gx++) {
            uint8_t code = cells[gy * GRID + gx];
            uint16_t color = (palette && code < SPLASH_PALETTE_SIZE) ? palette[code] : COL_EMPTY;
            uint16_t *p = &row_buf[gx * cell];
            for (int i = 0; i < cell; i++) p[i] = color;
        }
        for (int dy = 0; dy < cell; dy++) {
            memcpy(&canvas_buf[(gy * cell + dy) * canvas_w], row_buf, canvas_w * 2);
        }
    }
    if (canvas) lv_obj_invalidate(canvas);
}
#endif

// ---- Mini creature: a small animated creature for embedding in other screens
//      (e.g. the idle "sleeping" indicator). Self-contained — its own canvas and
//      buffer, independent of the full-screen splash above. ----
static lv_obj_t  *mini_canvas = NULL;
static uint16_t  *mini_buf = NULL;
static int        mini_cell = 0;
static int        mini_w = 0;      // canvas px, mini_anim->w * mini_cell
static int        mini_h = 0;
static const splash_anim_def_t *mini_anim = NULL;
static uint16_t   mini_frame = 0;
static uint32_t   mini_started = 0;

static void mini_render(void) {
    if (!mini_buf || !mini_anim) return;
    const int aw = mini_anim->w, ah = mini_anim->h;
    const uint8_t *cells = &mini_anim->frames[(size_t)mini_frame * aw * ah];
    const uint16_t *pal = mini_anim->palette;
    for (int gy = 0; gy < ah; gy++) {
        for (int gx = 0; gx < aw; gx++) {
            uint8_t code = cells[gy * aw + gx];
            uint16_t color = (pal && code < SPLASH_PALETTE_SIZE) ? pal[code] : COL_EMPTY;
            for (int dy = 0; dy < mini_cell; dy++) {
                uint16_t *dst = &mini_buf[(gy * mini_cell + dy) * mini_w + gx * mini_cell];
                for (int dx = 0; dx < mini_cell; dx++) dst[dx] = color;
            }
        }
    }
    if (mini_canvas) lv_obj_invalidate(mini_canvas);
}

lv_obj_t* splash_mini_create(lv_obj_t *parent, const char *anim_name, int px) {
    mini_anim = NULL;
    for (int i = 0; i < SPLASH_ANIM_COUNT; i++) {
        if (strcmp(splash_anims[i].name, anim_name) == 0) { mini_anim = &splash_anims[i]; break; }
    }
    if (!mini_anim) return NULL;
    const int amax = (mini_anim->w > mini_anim->h) ? mini_anim->w : mini_anim->h;
    mini_cell = px / amax;
    if (mini_cell < 1) mini_cell = 1;
    mini_w = mini_anim->w * mini_cell;
    mini_h = mini_anim->h * mini_cell;
#ifdef BOARD_HAS_PSRAM
    const uint32_t caps = MALLOC_CAP_SPIRAM;
#else
    const uint32_t caps = MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT;
#endif
    mini_buf = (uint16_t*)heap_caps_malloc(mini_w * mini_h * 2, caps);
    if (!mini_buf) return NULL;
    mini_canvas = lv_canvas_create(parent);
    lv_canvas_set_buffer(mini_canvas, mini_buf, mini_w, mini_h, LV_COLOR_FORMAT_RGB565);
    mini_frame = 0;
    mini_started = millis();
    mini_render();
    return mini_canvas;
}

void splash_mini_tick(void) {
    if (!mini_buf || !mini_anim || mini_anim->frame_count == 0) return;
    if (millis() - mini_started < mini_anim->holds[mini_frame]) return;
    mini_started = millis();
    mini_frame = (mini_frame + 1) % mini_anim->frame_count;
    mini_render();
}

// ─── Corner mascot (usage screen) ────────────────────────────────────────────
// The corner logo slot, alive: the still Clawd idles, occasionally does a
// small act (waving, dancing, pointing) in place, and every few acts walks
// off the left edge, does the full-size lurking animation over the screen,
// and walks back into the slot. PSRAM boards only (ui.cpp falls back to the
// static clawd_still.h icon on the C6); driven by splash_mascot_tick() from
// the main loop, independent of the splash screen itself.
static lv_obj_t *mas_img = NULL;
static lv_obj_t *mas_lurk_img = NULL;
static uint8_t  *mas_buf = NULL;       // planar RGB565A8, sized for largest act
static uint8_t  *mas_lurk_buf = NULL;
static lv_image_dsc_t mas_dsc, mas_lurk_dsc;
static int  mas_cell = 3;
static int  mas_slot_x = 0;            // px of the slot (walk-in target)
static int  mas_feet_y = 0;            // px feet line (all art is bottom-anchored)
static int  mas_lurk_cell = 8;
static int  mas_screen_w = 480;
static bool mas_visible = false;

enum MasMode { MAS_STILL, MAS_ACT, MAS_WALK_OFF, MAS_LURK, MAS_WALK_IN };
static MasMode mas_mode = MAS_STILL;
static const splash_anim_def_t *mas_anim = NULL;
static uint16_t mas_frame = 0;
static uint32_t mas_frame_started = 0;
static uint32_t mas_mode_started = 0;
static int  mas_x = 0;                 // widget x, px (may be off-screen)
static int  mas_face = +1;
static uint8_t mas_act_idx = 0;
static bool mas_from_loop = false;

// The corner mascot mirrors the splash's excitement: per usage-rate group,
// how long he idles between acts and which acts he does. "lurking" means the
// walk-off / full-size-lurk / walk-back trip. Acts must fit the 28×21-cell
// buffer (jumps are too tall for the corner).
static const char* MAS_ACTS_BY_RATE[4][4] = {
    { "pointing", "lurking", NULL,       NULL      },   // idle: sparse, sneaky
    { "waving",   "lurking", "pointing", NULL      },   // normal
    { "waving",   "dancing", "lurking",  NULL      },   // active
    { "dancing",  "waving",  "dancing",  "lurking" },   // heavy: can't sit still
};
static const uint16_t MAS_STILL_MS_BY_RATE[4] = { 10000, 7000, 5000, 3500 };

static const splash_anim_def_t* anim_by_name(const char *n) {
    for (int i = 0; i < SPLASH_ANIM_COUNT; i++)
        if (strcmp(splash_anims[i].name, n) == 0) return &splash_anims[i];
    return NULL;
}

// Render one frame into a planar RGB565A8 image (alpha 0 outside the art) and
// anchor the widget on the shared feet line.
static void mas_render(const splash_anim_def_t *a, uint16_t frame, bool mirror,
                       lv_image_dsc_t *dsc, uint8_t *buf, lv_obj_t *img,
                       int cell, int x, int feet_y) {
    const int w = a->w * cell, h = a->h * cell;
    uint16_t *color = (uint16_t*)buf;
    uint8_t  *alpha = buf + (size_t)w * h * 2;
    const uint8_t *src = &a->frames[(size_t)frame * a->w * a->h];
    for (int gy = 0; gy < a->h; gy++) {
        for (int gx = 0; gx < a->w; gx++) {
            uint8_t code = src[gy * a->w + (mirror ? a->w - 1 - gx : gx)];
            uint16_t c = (code && code < SPLASH_PALETTE_SIZE) ? a->palette[code] : 0;
            uint8_t  al = code ? 255 : 0;
            for (int dy = 0; dy < cell; dy++) {
                uint16_t *cp = &color[(gy * cell + dy) * w + gx * cell];
                uint8_t  *ap = &alpha[(gy * cell + dy) * w + gx * cell];
                for (int dx = 0; dx < cell; dx++) { cp[dx] = c; ap[dx] = al; }
            }
        }
    }
    dsc->header.w = w;
    dsc->header.h = h;
    dsc->header.cf = LV_COLOR_FORMAT_RGB565A8;
    dsc->header.stride = w * 2;
    dsc->data = buf;
    dsc->data_size = (size_t)w * h * 3;
    lv_image_set_src(img, dsc);
    lv_obj_set_pos(img, x, feet_y - h);
    lv_obj_invalidate(img);
}

static void mas_show_still(void) {
    mas_anim = anim_by_name("walking");     // frame 0 == the official still pose
    mas_frame = 0;
    mas_mode = MAS_STILL;
    mas_mode_started = millis();
    mas_x = mas_slot_x;
    mas_face = +1;
    if (mas_anim)
        mas_render(mas_anim, 0, false, &mas_dsc, mas_buf, mas_img,
                   mas_cell, mas_x, mas_feet_y);
}

lv_obj_t* splash_mascot_create(lv_obj_t *parent, int slot_x, int feet_y, int cell) {
    mas_cell = cell;
    mas_slot_x = slot_x;
    mas_feet_y = feet_y;
    mas_screen_w = board_caps().width;
    // Buffer for the largest act bbox (pointing, 28×21 cells).
    const size_t mas_bytes = (size_t)(28 * cell) * (21 * cell) * 3;
    const splash_anim_def_t *lurk = anim_by_name("lurking");
    const BoardCaps& c = board_caps();
    int mind = (c.width < c.height) ? c.width : c.height;
    mas_lurk_cell = mind / SPLASH_GRID;
    if (mas_lurk_cell < 1) mas_lurk_cell = 1;
    const size_t lurk_bytes = lurk ?
        (size_t)(lurk->w * mas_lurk_cell) * (lurk->h * mas_lurk_cell) * 3 : 0;
    mas_buf      = (uint8_t*)heap_caps_malloc(mas_bytes,  MALLOC_CAP_SPIRAM);
    mas_lurk_buf = lurk_bytes ? (uint8_t*)heap_caps_malloc(lurk_bytes, MALLOC_CAP_SPIRAM) : NULL;
    if (!mas_buf) return NULL;
    mas_img = lv_image_create(parent);
    if (mas_lurk_buf) {
        mas_lurk_img = lv_image_create(parent);
        lv_obj_add_flag(mas_lurk_img, LV_OBJ_FLAG_HIDDEN);
    }
    mas_show_still();
    return mas_img;
}

void splash_mascot_set_visible(bool v) {
    mas_visible = v;
    if (!mas_img) return;
    if (v) {
        lv_obj_clear_flag(mas_img, LV_OBJ_FLAG_HIDDEN);
        // The mascot walks over everything — keep him above later-created
        // siblings (battery icon, labels) whenever he's shown.
        lv_obj_move_foreground(mas_img);
        if (mas_lurk_img) lv_obj_move_foreground(mas_lurk_img);
        mas_show_still();                       // restart clean at the slot
    } else {
        lv_obj_add_flag(mas_img, LV_OBJ_FLAG_HIDDEN);
        if (mas_lurk_img) lv_obj_add_flag(mas_lurk_img, LV_OBJ_FLAG_HIDDEN);
    }
}

void splash_mascot_tick(void) {
    if (!mas_img || !mas_visible || !mas_anim) return;
    const uint32_t now = millis();

    if (mas_mode == MAS_STILL) {
        int g = usage_rate_group();
        if (g < 0 || g > 3) g = 0;
        if (now - mas_mode_started < MAS_STILL_MS_BY_RATE[g]) return;
        uint8_t count = 0;
        while (count < 4 && MAS_ACTS_BY_RATE[g][count]) count++;
        if (count == 0) { mas_mode_started = now; return; }
        const char *act = MAS_ACTS_BY_RATE[g][mas_act_idx++ % count];
        mas_frame = 0;
        mas_frame_started = now;
        mas_from_loop = false;
        if (strcmp(act, "lurking") == 0 && mas_lurk_img) {   // the lurk trip
            mas_anim = anim_by_name("walking");
            mas_face = -1;
            mas_mode = MAS_WALK_OFF;
        } else {
            const splash_anim_def_t *a = anim_by_name(act);
            if (!a) { mas_mode_started = now; return; }
            mas_anim = a;
            mas_face = +1;
            mas_mode = MAS_ACT;
        }
        return;
    }

    const splash_anim_def_t *a = mas_anim;
    if (now - mas_frame_started < a->holds[mas_frame]) return;
    mas_frame_started = now;

    uint16_t next = mas_frame + 1;
    const bool walking_mode = (mas_mode == MAS_WALK_OFF || mas_mode == MAS_WALK_IN);
    if (walking_mode && mas_frame == a->loop_end)
        next = a->loop_start;                       // walk: hold the gait loop

    if (next >= a->frame_count) {                   // act / lurk finished
        if (mas_mode == MAS_LURK) {
            lv_obj_add_flag(mas_lurk_img, LV_OBJ_FLAG_HIDDEN);
            mas_anim = anim_by_name("walking");
            mas_frame = 0;
            mas_from_loop = false;
            mas_face = -1;                          // he lurked on the right,
            mas_x = mas_screen_w;                   // so he re-enters from it
            mas_mode = MAS_WALK_IN;
            lv_obj_clear_flag(mas_img, LV_OBJ_FLAG_HIDDEN);
            return;
        }
        mas_show_still();                           // acts end on the idle pose
        return;
    }

    const bool from_loop = mas_from_loop;
    mas_frame = next;
    mas_from_loop = walking_mode &&
        mas_frame >= a->loop_start && mas_frame <= a->loop_end;

    if (walking_mode && mas_from_loop) {
        const int step = walk_gait_cells_k(WALK_FRONT, mas_frame, from_loop) * mas_cell;
        // Walk-off always exits left; walk-in heads toward the slot from
        // whichever side he's on (right, after the lurk trip).
        const int dir = (mas_mode == MAS_WALK_OFF) ? -1
                        : (mas_x < mas_slot_x ? +1 : -1);
        mas_face = (mas_mode == MAS_WALK_OFF) ? -1 : dir;
        mas_x += dir * step;
        if (mas_mode == MAS_WALK_OFF && mas_x <= -a->w * mas_cell) {
            // Fully off: hide the corner sprite, run the full-size lurk.
            lv_obj_add_flag(mas_img, LV_OBJ_FLAG_HIDDEN);
            const splash_anim_def_t *lurk = anim_by_name("lurking");
            if (lurk && mas_lurk_img && mas_lurk_buf) {
                mas_anim = lurk;
                mas_frame = 0;
                mas_mode = MAS_LURK;
                lv_obj_clear_flag(mas_lurk_img, LV_OBJ_FLAG_HIDDEN);
                lv_obj_move_foreground(mas_lurk_img);
                // He left stage left, so he peeks in from the RIGHT edge —
                // mirrored at render time (the art is authored left-edge).
                mas_render(lurk, 0, true, &mas_lurk_dsc, mas_lurk_buf,
                           mas_lurk_img, mas_lurk_cell,
                           mas_screen_w - lurk->w * mas_lurk_cell,
                           (STAGE_ANCHOR_Y + lurk->oy + lurk->h) * mas_lurk_cell);
            } else {
                mas_mode = MAS_WALK_IN;             // no lurk asset: turn back
                mas_face = +1;
            }
            return;
        }
        if (mas_mode == MAS_WALK_IN &&
            ((dir > 0 && mas_x >= mas_slot_x) || (dir < 0 && mas_x <= mas_slot_x))) {
            mas_show_still();                       // arrived: settle in the slot
            return;
        }
    }

    if (mas_mode == MAS_LURK) {
        mas_render(a, mas_frame, true, &mas_lurk_dsc, mas_lurk_buf, mas_lurk_img,
                   mas_lurk_cell, mas_screen_w - a->w * mas_lurk_cell,
                   (STAGE_ANCHOR_Y + a->oy + a->h) * mas_lurk_cell);
    } else {
        mas_render(a, mas_frame, mas_face < 0, &mas_dsc, mas_buf, mas_img,
                   mas_cell, mas_x, mas_feet_y);
    }
}

static void show_placeholder() {
    // Solid dark background + centered status label. On the direct-draw path
    // there's no canvas; the black container is the background and the LVGL
    // label shows over it.
#if !SPLASH_DIRECT_DRAW
    if (canvas_buf) {
        for (int i = 0; i < canvas_w * canvas_h; i++) canvas_buf[i] = COL_EMPTY;
    }
    if (canvas) lv_obj_invalidate(canvas);
#endif
    if (label_status) lv_obj_clear_flag(label_status, LV_OBJ_FLAG_HIDDEN);
}

void splash_init(lv_obj_t *parent) {
    const BoardCaps& c = board_caps();

    // Shared full-screen black container — the splash background.
    splash_container = lv_obj_create(parent);
    lv_obj_set_size(splash_container, c.width, c.height);
    lv_obj_set_pos(splash_container, 0, 0);
    lv_obj_set_style_bg_color(splash_container, THEME_BG, 0);
    lv_obj_set_style_bg_opa(splash_container, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(splash_container, 0, 0);
    lv_obj_set_style_pad_all(splash_container, 0, 0);
    lv_obj_clear_flag(splash_container, LV_OBJ_FLAG_SCROLLABLE);

#if SPLASH_DIRECT_DRAW
    // Direct-to-panel path (no PSRAM): no LVGL canvas. Compute on-screen cell
    // size + centering, and a scratch band buffer sized for one grid-row strip
    // across the square art (GRID*scr_cell × scr_cell). On the C6 that's
    // 480×24×2 ≈ 23 KB of internal SRAM.
    int mind = (c.width < c.height) ? c.width : c.height;
    scr_cell = mind / GRID;
    int side = GRID * scr_cell;
    scr_offx = (c.width  - side) / 2;
    scr_offy = (c.height - side) / 2;
    strip_buf = (uint16_t*)heap_caps_malloc((size_t)side * scr_cell * 2,
                                            MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!strip_buf) {
        Serial.println("splash: strip buffer alloc failed");
        return;
    }
#else
    // PSRAM path: render into an LVGL canvas at native size (no transform).
    SplashGeometry geo = splash_compute_geometry(c.width, c.height, true);
    cell                = geo.cell;
    canvas_w            = geo.canvas_dim;
    canvas_h            = geo.canvas_dim;
    const int img_scale = geo.scale;

    canvas_buf = (uint16_t*)heap_caps_malloc(canvas_w * canvas_h * 2, MALLOC_CAP_SPIRAM);
    row_buf    = (uint16_t*)heap_caps_malloc(canvas_w * 2,            MALLOC_CAP_SPIRAM);
    if (!canvas_buf || !row_buf) {
        Serial.println("splash: failed to alloc canvas buffer");
        return;
    }

    canvas = lv_canvas_create(splash_container);
    lv_canvas_set_buffer(canvas, canvas_buf, canvas_w, canvas_h, LV_COLOR_FORMAT_RGB565);
    if (img_scale != SPLASH_SCALE_UNITY) {
        lv_image_set_antialias(canvas, false);
        lv_image_set_pivot(canvas, canvas_w / 2, canvas_h / 2);
        lv_image_set_scale(canvas, img_scale);
    }
    lv_obj_center(canvas);
#endif

    // Placeholder label (visible only when no animations are loaded)
    label_status = lv_label_create(splash_container);
    lv_label_set_text(label_status,
        "no animations loaded\n\n"
        "run tools/convert_official_clawd.js");
    lv_obj_set_style_text_font(label_status, &font_styrene_28, 0);
    lv_obj_set_style_text_color(label_status, lv_color_hex(0xb0aea5), 0);
    lv_obj_set_style_text_align(label_status, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_center(label_status);

    resolve_group_lists();

    if (SPLASH_ANIM_COUNT == 0) {
        show_placeholder();
    } else {
        lv_obj_add_flag(label_status, LV_OBJ_FLAG_HIDDEN);
#if !SPLASH_DIRECT_DRAW
        // PSRAM path pre-renders frame 0 into the canvas buffer. The direct
        // path draws nothing here — render_frame() bails while inactive, so the
        // splash never paints to the panel before it's actually shown.
        const splash_anim_def_t *a = &splash_anims[0];
        render_frame(compose_stage(a, 0), a->palette);
#endif
        frame_started_ms = millis();
    }

    lv_obj_add_flag(splash_container, LV_OBJ_FLAG_HIDDEN);
}

// ===========================================================================
// The colony
// ===========================================================================
//
// One creature per agent instead of one big one. Everything here is additive:
// the single-Clawd path above is untouched and still runs whenever the fleet
// is empty, which is a board with no host, a quiet desk, or any port that
// never calls splash_set_fleet().
//
// WHY IT DOES NOT REUSE compose_stage(). That composites ONE animation into a
// 60x60 grid of palette indices and hands render_frame() a single palette --
// and the whole point here is nine creatures from nine different animations,
// each with its own <=16-colour palette. Rather than merge palettes (which
// costs a second buffer and loses the exact authored colours), each actor is
// drawn INTO ITS OWN RECT with its own palette. Nothing overlaps, so nothing
// needs compositing.
//
// The rect is fixed for an actor's lifetime and the sprite is drawn with its
// background cells included, so redrawing a frame erases the previous one
// exactly. No clear pass, no dirty-rect arithmetic, and an actor whose frame
// has not advanced costs nothing at all.

LV_FONT_DECLARE(font_styrene_14);

#define FLEET_LABEL_H   20     // the name strip under each creature
#define FLEET_NAME_GAP  6      // air between the feet and the name
#define FLEET_TOP       18
#define FLEET_BOTTOM    6

struct FleetActor {
    uint8_t  anim;         // index into splash_anims
    uint16_t frame;
    uint32_t started;
    int      px, py;       // sprite top-left, in surface pixels
    int      w, h;         // sprite size, in surface pixels
    int      cell;         // surface px per art cell (shared by the fleet)
    bool     frozen;       // holds frame 0 -- the idle pose
};

// Wipe the whole surface. Needed exactly when the colony takes over from the
// single creature: that one is drawn from a 60x60 stage covering the screen,
// the colony only ever paints its own small rects, and without this the old
// creature stays underneath -- which on the first run left a full-size Clawd
// in a fedora standing behind the fleet.
static void fleet_repaint_labels(void);

static void fleet_clear_surface(void) {
#if SPLASH_DIRECT_DRAW
    if (!strip_buf) return;
    const BoardCaps& c = board_caps();
    const int band = scr_cell > 0 ? scr_cell : 8;
    for (int i = 0; i < c.width * band; i++) strip_buf[i] = COL_EMPTY;
    for (int y = 0; y < c.height; y += band) {
        int hh = (y + band <= c.height) ? band : (c.height - y);
        display_hal_draw_bitmap(0, y, c.width, hh, strip_buf);
    }
#else
    if (canvas_buf) memset(canvas_buf, 0, (size_t)canvas_w * canvas_h * 2);
#endif
    fleet_repaint_labels();
}

static SplashFleetMember fleet[SPLASH_FLEET_MAX];
static FleetActor        fleet_actor[SPLASH_FLEET_MAX];
static lv_obj_t*         fleet_label[SPLASH_FLEET_MAX];
static uint8_t           fleet_n = 0;
static uint8_t           fleet_dropped = 0;   // live sessions that did not fit

// The names are LVGL labels; the creatures are pixels pushed straight at the
// panel. That works because the two never share a rectangle -- except here,
// where the wipe covers the WHOLE surface and takes the labels with it. LVGL
// has no idea that happened (nothing it drew has changed), so it never
// repaints them and the fleet is left anonymous. Swiping back onto the splash
// was the reliable way to see it.
static void fleet_repaint_labels(void) {
    for (uint8_t i = 0; i < SPLASH_FLEET_MAX; i++)
        if (fleet_label[i] && !lv_obj_has_flag(fleet_label[i], LV_OBJ_FLAG_HIDDEN))
            lv_obj_invalidate(fleet_label[i]);
}

static bool              fleet_relayout = false;   // geometry/labels are stale
static bool              fleet_repaint  = false;   // creatures need a full draw

// Which creature plays which part. Varied by slot so nine working agents do
// not march in lockstep -- the fleet should look like a room, not a chorus
// line. Names are matched against the catalogue at layout time; anything the
// build does not carry falls back to the first animation.
// A creature's character comes from its NAME, not from where it happens to
// stand. Keying on the slot produced vertical stripes -- with three columns
// and three poses, `slot % 3` gave every column one pose -- and it also meant
// an agent changed character whenever the sort moved it. This is stable per
// agent and has nothing to do with the grid.
static uint32_t fleet_seed(const char* label) {
    uint32_t h = 2166136261u;
    for (const char* p = label; p && *p; p++) {
        h ^= (uint8_t)*p;
        h *= 16777619u;
    }
    return h;
}

static const char* fleet_anim_name(uint8_t state, uint32_t seed) {
    static const char* WORK[] = { "walking", "laptop", "crab walking" };
    // Idle is not "doing nothing", it is "doing nothing FOR YOU" -- so the
    // creature is off playing. These read as leisure at a glance and, more
    // to the point, they look nothing like the walking one next to them.
    static const char* IDLE[] = { "dancing", "basketball", "skateboard" };
    switch (state) {
    case SPLASH_FLEET_WAITING: return "waving";       // calling you over
    // IDLE CREATURES HOLD STILL, and that is the whole signal: motion on this
    // screen means something is happening. Making them animate (the second
    // cut, which fixed a different complaint) took that away -- nine creatures
    // all moving, and no way to tell who was working.
    //
    // But they must not hold the SAME pose, which is what the first cut did by
    // freezing frame 0. That was not bad luck: frame 0 of every animation is
    // the one shared idle position they all pass through, which is exactly
    // what makes switching between them seamless (see CLAUDE.md). Different
    // animation, identical first frame.
    //
    // So an idle creature freezes at a frame from INSIDE its loop, picked from
    // its name -- a Clawd holding a basketball, one standing on a skateboard,
    // one caught mid-dance. Still, distinct, and obviously not working.
    //
    // Not "lurking" (authored as a half-sprite that hangs off the screen edge,
    // so a slot enlarges it into a blob) and not "cloud" (41 cells wide -- it
    // dragged the fleet's shared cell size down and shrank everybody).
    case SPLASH_FLEET_IDLE:    return IDLE[seed % 3];
    case SPLASH_FLEET_DONE:    return "jumping happy";
    case SPLASH_FLEET_MESSAGE: return "pointing";     // telling you something
    default:                   return WORK[seed % 3];
    }
}

static uint8_t fleet_anim_index(const char* name) {
    for (uint16_t i = 0; i < SPLASH_ANIM_COUNT; i++)
        if (strcmp(splash_anims[i].name, name) == 0) return (uint8_t)i;
    return 0;
}

static lv_color_t fleet_label_color(uint8_t state) {
    switch (state) {
    case SPLASH_FLEET_WAITING: return THEME_ACCENT;
    case SPLASH_FLEET_DONE:    return THEME_GREEN;
    case SPLASH_FLEET_MESSAGE: return THEME_PURPLE;
    default:                   return THEME_DIM;
    }
}

// The surface the colony lays itself out on. On the PSRAM path that is the
// LVGL canvas (480x480 at 1:1 on every board that has the chat views); on the
// direct path it is the panel itself.
static void fleet_surface(int* w, int* h) {
#if SPLASH_DIRECT_DRAW
    const BoardCaps& c = board_caps();
    *w = c.width; *h = c.height;
#else
    *w = canvas_w; *h = canvas_h;
#endif
}

// Bigger creatures when there are fewer of them: two agents on a 480 panel
// should not be two stamps in the corner. The cell is in SURFACE pixels per
// art cell, and a core Clawd is ~24x18 art cells.
static void fleet_grid(uint8_t n, int* cols, int* cell) {
    if (n <= 2)       { *cols = 2; *cell = 8; }
    else if (n <= 4)  { *cols = 2; *cell = 6; }
    else if (n <= 6)  { *cols = 3; *cell = 5; }
    else if (n <= 9)  { *cols = 3; *cell = 4; }
    // Past nine the grid widens rather than the creatures shrinking inside a
    // three-column layout: at four columns sixteen still measure 48x36, where
    // cramming them into three would have taken them to 24x18. The cell here
    // is only a ceiling -- fleet_layout lowers it until every creature fits.
    else              { *cols = 4; *cell = 3; }
}

static void fleet_layout(void) {
    fleet_relayout = false;
    if (!splash_container) return;
    int sw, sh;
    fleet_surface(&sw, &sh);

    int cols, cell;
    fleet_grid(fleet_n, &cols, &cell);
    const int slots  = fleet_n + (fleet_dropped ? 1 : 0);
    const int rows   = (slots + cols - 1) / cols;
    const int slot_w = sw / cols;
    const int slot_h = (sh - FLEET_TOP - FLEET_BOTTOM) / (rows ? rows : 1);
    // The height every creature is scaled to. Derived from the slot rather
    // than from any one animation, so the fleet fills the room whatever mix
    // of poses it happens to be in.
    int art_h = slot_h - FLEET_LABEL_H - FLEET_NAME_GAP;
    if (art_h < 8) art_h = 8;

    // ONE cell for the whole fleet, not one per creature. The animations are
    // authored at different sizes -- a Clawd holding a laptop is 34x23 art
    // cells where a walking one is 24x18 -- and scaling each to a common
    // height would shrink the wide ones to fit their slot and leave the fleet
    // looking like a size chart. A shared cell keeps the authored proportions
    // (the one with the laptop really is bigger) and is chosen as the largest
    // that leaves EVERY creature inside its slot.
    for (uint8_t i = 0; i < fleet_n; i++) {
        const splash_anim_def_t* a = &splash_anims[fleet_anim_index(
            fleet_anim_name(fleet[i].state, fleet_seed(fleet[i].label)))];
        if (!a->w || !a->h) continue;
        int fit_w = (slot_w - 8) / a->w;
        int fit_h = art_h / a->h;
        int fit = fit_w < fit_h ? fit_w : fit_h;
        if (fit < cell) cell = fit;
    }
    if (cell < 1) cell = 1;

    // Rows are only as tall as the tallest creature needs, and the whole block
    // is centred. Bottom-anchoring inside full-height slots left the fleet
    // pinned to the floor of each row with a third of the panel empty above
    // it -- correct arithmetic, wrong picture.
    int tall = 0;
    for (uint8_t i = 0; i < fleet_n; i++) {
        const splash_anim_def_t* a = &splash_anims[fleet_anim_index(
            fleet_anim_name(fleet[i].state, fleet_seed(fleet[i].label)))];
        if (a->h * cell > tall) tall = a->h * cell;
    }
    const int row_h = tall + FLEET_NAME_GAP + FLEET_LABEL_H;
    int top = (sh - rows * row_h) / 2;
    if (top < FLEET_TOP) top = FLEET_TOP;

    for (uint8_t i = 0; i < SPLASH_FLEET_MAX; i++) {
        if (i >= fleet_n) {
            if (fleet_label[i]) lv_obj_add_flag(fleet_label[i], LV_OBJ_FLAG_HIDDEN);
            continue;
        }
        FleetActor* ac = &fleet_actor[i];
        const uint32_t seed = fleet_seed(fleet[i].label);
        ac->anim  = fleet_anim_index(fleet_anim_name(fleet[i].state, seed));
        const splash_anim_def_t* a = &splash_anims[ac->anim];
        ac->cell = cell;
        // Start each creature at a different point in its own cycle, for the
        // same reason the animations differ: a fleet in step looks mechanical.
        ac->frozen  = (fleet[i].state == SPLASH_FLEET_IDLE);
        if (!a->frame_count) {
            ac->frame = 0;
        } else if (ac->frozen) {
            // Inside the loop, never frame 0 -- that is the shared pose every
            // animation starts on, and holding it is what made them identical.
            const uint16_t lo = a->loop_start;
            const uint16_t hi = (a->loop_end > lo) ? a->loop_end : lo;
            ac->frame = (uint16_t)(lo + (seed >> 8) % (uint16_t)(hi - lo + 1));
        } else {
            // Out of phase, so a row of workers is not a chorus line.
            ac->frame = (uint16_t)(seed % a->frame_count);
        }
        ac->started = millis();
        ac->w = a->w * cell;
        ac->h = a->h * cell;

        const int sx = (i % cols) * slot_w;
        const int sy = top + (i / cols) * row_h;
        ac->px = sx + (slot_w - ac->w) / 2;
        // Feet on the slot's ground line, name underneath. Creatures are
        // authored bottom-anchored, so aligning feet is what makes a row of
        // different animations look like they are standing on one floor.
        // Feet on the row's ground line whatever the creature's height, so a
        // jumping one and a standing one share a floor instead of floating.
        ac->py = sy + tall - ac->h;
        if (ac->px < 0) ac->px = 0;
        if (ac->py < 0) ac->py = 0;

        if (!fleet_label[i]) {
            fleet_label[i] = lv_label_create(splash_container);
            lv_obj_set_style_text_font(fleet_label[i], &font_styrene_14, 0);
            lv_label_set_long_mode(fleet_label[i], LV_LABEL_LONG_CLIP);
            lv_obj_set_style_text_align(fleet_label[i], LV_TEXT_ALIGN_CENTER, 0);
        }
        lv_label_set_text(fleet_label[i], fleet[i].label);
        lv_obj_set_style_text_color(fleet_label[i], fleet_label_color(fleet[i].state), 0);
        lv_obj_set_width(fleet_label[i], slot_w);
        lv_obj_set_pos(fleet_label[i], sx, sy + tall + FLEET_NAME_GAP);
        lv_obj_remove_flag(fleet_label[i], LV_OBJ_FLAG_HIDDEN);
    }
    // The overflow note takes the slot after the last creature. It is a label
    // with nothing above it, which is exactly what it means: there are more
    // agents and they are not drawn. The host sorts attention-first, so what
    // is missing is what needed you least.
    if (fleet_dropped) {
        const uint8_t i = fleet_n;
        const int sx = (i % cols) * slot_w;
        const int sy = top + (i / cols) * row_h;
        if (!fleet_label[i]) {
            fleet_label[i] = lv_label_create(splash_container);
            lv_obj_set_style_text_font(fleet_label[i], &font_styrene_14, 0);
            lv_label_set_long_mode(fleet_label[i], LV_LABEL_LONG_CLIP);
            lv_obj_set_style_text_align(fleet_label[i], LV_TEXT_ALIGN_CENTER, 0);
        }
        char more[16];
        snprintf(more, sizeof(more), "+%u more", (unsigned)fleet_dropped);
        lv_label_set_text(fleet_label[i], more);
        lv_obj_set_style_text_color(fleet_label[i], THEME_DIM, 0);
        lv_obj_set_width(fleet_label[i], slot_w);
        lv_obj_set_pos(fleet_label[i], sx, sy + tall + FLEET_NAME_GAP);
        lv_obj_remove_flag(fleet_label[i], LV_OBJ_FLAG_HIDDEN);
    }
    fleet_repaint = true;
}

// Draw one creature's whole rect, background cells included -- which is what
// erases the frame before it without a separate clear.
static void fleet_draw(const FleetActor* ac) {
    const splash_anim_def_t* a = &splash_anims[ac->anim];
    if (!a->frame_count) return;
    const int cell = ac->cell > 0 ? ac->cell : 1;
    const uint8_t* src = &a->frames[(size_t)ac->frame * a->w * a->h];

#if SPLASH_DIRECT_DRAW
    if (!strip_buf) return;
    const int bw = a->w * cell;
    for (int r = 0; r < a->h; r++) {
        for (int c = 0; c < a->w; c++) {
            const uint8_t code = src[r * a->w + c];
            const uint16_t col = code ? a->palette[code] : COL_EMPTY;
            uint16_t* pcol = &strip_buf[c * cell];
            for (int i = 0; i < cell; i++) pcol[i] = col;
        }
        for (int dy = 1; dy < cell; dy++)
            memcpy(&strip_buf[dy * bw], strip_buf, (size_t)bw * 2);
        display_hal_draw_bitmap(ac->px, ac->py + r * cell, bw, cell, strip_buf);
    }
#else
    if (!canvas_buf) return;
    for (int r = 0; r < a->h; r++) {
        for (int dy = 0; dy < cell; dy++) {
            const int y = ac->py + r * cell + dy;
            if (y < 0 || y >= canvas_h) continue;
            uint16_t* row = &canvas_buf[(size_t)y * canvas_w];
            for (int c = 0; c < a->w; c++) {
                const uint8_t code = src[r * a->w + c];
                const uint16_t col = code ? a->palette[code] : COL_EMPTY;
                for (int dx = 0; dx < cell; dx++) {
                    const int x = ac->px + c * cell + dx;
                    if (x >= 0 && x < canvas_w) row[x] = col;
                }
            }
        }
    }
#endif
}

// Advance every creature on its own clock and redraw only the ones that moved.
static void fleet_tick(void) {
    if (fleet_relayout) fleet_layout();
    const uint32_t now = millis();
    if (fleet_repaint) fleet_clear_surface();
    bool drew = fleet_repaint;

    for (uint8_t i = 0; i < fleet_n; i++) {
        FleetActor* ac = &fleet_actor[i];
        const splash_anim_def_t* a = &splash_anims[ac->anim];
        if (!a->frame_count) continue;
        bool draw = fleet_repaint;
        if (!ac->frozen && now - ac->started >= a->holds[ac->frame]) {
            // Creatures stay in their LOOP: the intro and outro exist to enter
            // and leave a scene, and nothing here enters or leaves -- an agent
            // is working until the host says it is not.
            uint16_t next = ac->frame + 1;
            if (ac->frame >= a->loop_end || next >= a->frame_count)
                next = a->loop_start;
            ac->frame = next;
            ac->started = now;
            draw = true;
        }
        if (draw) { fleet_draw(ac); drew = true; }
    }
    fleet_repaint = false;

#if !SPLASH_DIRECT_DRAW
    if (drew && canvas) lv_obj_invalidate(canvas);
#else
    (void)drew;
#endif
}

void splash_set_fleet(const SplashFleetMember *members, uint8_t n,
                      uint8_t dropped) {
    if (n > SPLASH_FLEET_MAX) {
        dropped = (uint8_t)(dropped + (n - SPLASH_FLEET_MAX));
        n = SPLASH_FLEET_MAX;
    }
    // A note about what is missing needs a slot of its own, so when anything
    // was dropped the last creature gives one up. The same trade the card list
    // makes (six rows, one spent on the overflow marker) and for the same
    // reason: a fleet drawn one short is a smaller lie than a fleet that does
    // not admit it is incomplete. Without this the marker vanished at exactly
    // the size it exists for -- a full grid with more behind it.
    if (dropped && n == SPLASH_FLEET_MAX) {
        n--;
        dropped++;
    }
    // Unchanged membership must not restart the animations: this is called on
    // every payload, and a fleet that flinched every few seconds would be
    // worse than no animation at all.
    bool same = (n == fleet_n) && (dropped == fleet_dropped);
    for (uint8_t i = 0; same && i < n; i++)
        same = fleet[i].state == members[i].state &&
               strncmp(fleet[i].label, members[i].label, sizeof(fleet[i].label)) == 0;
    if (same) return;

    for (uint8_t i = 0; i < n; i++) fleet[i] = members[i];
    const bool was = fleet_n > 0;
    fleet_n = n;
    fleet_dropped = dropped;
    fleet_relayout = true;
    if (was != (fleet_n > 0)) {
        // Crossed between the colony and the single creature: the whole
        // surface changes meaning, so nothing on it can be trusted.
#if SPLASH_DIRECT_DRAW
        force_full = true;
#endif
        fleet_repaint = true;
        for (uint8_t i = 0; i < SPLASH_FLEET_MAX; i++)
            if (fleet_label[i]) lv_obj_add_flag(fleet_label[i], LV_OBJ_FLAG_HIDDEN);
    }
}

void splash_tick(void) {
    if (!active || SPLASH_ANIM_COUNT == 0) return;
    const uint32_t now = millis();

    // The fleet takes the screen when there is one. Everything below is the
    // single-creature splash, unchanged, and it is what a quiet desk still
    // gets.
    if (fleet_n > 0) {
#if SPLASH_DIRECT_DRAW
        // LVGL painted the container black on unhide; repaint the creatures
        // over it now, the same deferral the single-creature path uses.
        if (force_full) { force_full = false; fleet_repaint = true; }
#endif
        fleet_tick();
        return;
    }

#if SPLASH_DIRECT_DRAW
    // Deferred full repaint after a (re)show — runs now that LVGL has drawn the
    // black background this loop iteration.
    if (force_full) {
        const splash_anim_def_t *fa = &splash_anims[cur_anim];
        if (fa->frame_count) render_frame(compose_stage(fa, cur_frame), fa->palette);
    }
#endif

    const splash_anim_def_t *a = &splash_anims[cur_anim];
    if (a->frame_count == 0) return;

    if (walk_active) walk_choreo(a);

    // Scenes: hold the loop for SCENE_LOOP_MS, then let the outro play.
    if (!walk_active && in_loop && !loop_release &&
        now - loop_entered_ms >= SCENE_LOOP_MS)
        loop_release = true;

    // Auto-rotate — never a hard cut. Walkers switch only while standing at
    // home; everything else releases its loop and switches after the outro.
    if (now - last_pick_ms >= SPLASH_ROTATE_INTERVAL_MS) {
        if (walk_active) {
            if (walk_phase == 0 && pb_done) splash_pick_for_current_rate();
        } else {
            loop_release = true;
            pending_pick = true;
            last_pick_ms = now;    // don't re-fire while the outro plays
        }
    }

    if (pb_done) return;                       // holding the idle frame
    if (now - frame_started_ms < a->holds[cur_frame]) return;

    // Advance one frame through intro → loop → outro.
    const bool from_loop = in_loop;
    uint16_t next = cur_frame + 1;
    if (cur_frame == a->loop_end && !loop_release)
        next = a->loop_start;

    if (next >= a->frame_count) {              // completed the file
        if (pending_pick) {
            pending_pick = false;
            splash_pick_for_current_rate();
            return;
        }
        if (walk_active) {                     // walk finished: stand
            cur_frame = 0;
            frame_started_ms = now;
            pb_done = true;
            render_frame(compose_stage(a, 0), a->palette);
            return;
        }
        next = 0;                              // replay from the intro
        loop_release = false;
    }

    cur_frame = next;
    frame_started_ms = now;
    const bool now_in = cur_frame >= a->loop_start && cur_frame <= a->loop_end;
    if (now_in && !from_loop) loop_entered_ms = now;
    in_loop = now_in;

    // Walk translation, locked to gait frames; clamp to land exactly on the
    // target, then release the loop so the gait exits.
    if (walk_active && walk_dir != 0 && in_loop) {
        walk_x += walk_dir * walk_gait_cells(cur_frame, from_loop);
        if ((walk_dir > 0 && walk_x >= walk_target) ||
            (walk_dir < 0 && walk_x <= walk_target)) {
            walk_x = walk_target;
            walk_dir = 0;
            loop_release = true;
        }
    }

    render_frame(compose_stage(a, cur_frame), a->palette);
}

void splash_next(void) {
    // The colony owns the surface. These two are the ways the SINGLE creature
    // gets drawn from outside splash_tick() -- a usage rate-group change
    // (main.cpp) and the PWR button -- and both paint a full-stage creature
    // straight over the fleet AND over the LVGL name labels, which is what
    // made the names vanish and a big Clawd loom behind the colony after a
    // while. Nothing here has anything to pick while a fleet is up.
    if (fleet_n > 0) return;
    if (SPLASH_ANIM_COUNT == 0) return;
    cur_anim = (cur_anim + 1) % SPLASH_ANIM_COUNT;
    cur_frame = 0;
    frame_started_ms = millis();
    last_pick_ms = frame_started_ms;
    const splash_anim_def_t *a = &splash_anims[cur_anim];
    anim_reset(a);
    render_frame(compose_stage(a, 0), a->palette);
    Serial.printf("splash: -> %s\n", a->name);
}

void splash_pick_for_current_rate(void) {
    // The colony owns the surface. These two are the ways the SINGLE creature
    // gets drawn from outside splash_tick() -- a usage rate-group change
    // (main.cpp) and the PWR button -- and both paint a full-stage creature
    // straight over the fleet AND over the LVGL name labels, which is what
    // made the names vanish and a big Clawd loom behind the colony after a
    // while. Nothing here has anything to pick while a fleet is up.
    if (fleet_n > 0) return;
    if (SPLASH_ANIM_COUNT == 0) return;
    int g = usage_rate_group();
    if (g < 0 || g >= GROUP_COUNT) g = 0;
    if (group_size[g] == 0) return;

    uint8_t slot = group_rotation[g] % group_size[g];
    group_rotation[g]++;
    int8_t idx = group_lists[g][slot];
    if (idx < 0) return;

    cur_anim = (uint16_t)idx;
    cur_frame = 0;
    frame_started_ms = millis();
    last_pick_ms = frame_started_ms;
    const splash_anim_def_t *a = &splash_anims[cur_anim];
    anim_reset(a);
    render_frame(compose_stage(a, 0), a->palette);
}

bool splash_is_active(void) { return active; }

void splash_show(void) {
    splash_pick_for_current_rate();   // select animation; direct path defers the draw
    if (splash_container) lv_obj_clear_flag(splash_container, LV_OBJ_FLAG_HIDDEN);
    active = true;
#if SPLASH_DIRECT_DRAW
    // LVGL fills the container black once on unhide; that would erase a creature
    // drawn now. Defer the full repaint to the next splash_tick(), which runs
    // after lv_timer_handler() in the main loop.
    force_full = true;
#endif
}

void splash_hide(void) {
    if (splash_container) lv_obj_add_flag(splash_container, LV_OBJ_FLAG_HIDDEN);
    active = false;
}

lv_obj_t* splash_get_root(void) {
    return splash_container;
}
