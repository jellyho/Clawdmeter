#pragma once
#include <stdint.h>
#include <lvgl.h>

// Initialize splash module. Creates the canvas widget inside `parent` and
// allocates the 480x480 pixel buffer (PSRAM).
void splash_init(lv_obj_t *parent);

// Advance animation frame if hold time elapsed. Call from main loop.
void splash_tick(void);

// Cycle to the next animation in the catalog.
void splash_next(void);

// Show/hide the splash container.
void splash_show(void);
void splash_hide(void);

// Pick the next animation matching the current usage-rate group.
// Called automatically by splash_show(); also exposed so other modules can
// trigger a re-pick when the rate group changes mid-display.
void splash_pick_for_current_rate(void);

// True when splash is currently rendering (used to gate re-picks).
bool splash_is_active(void);

// Root container (so ui.cpp can attach a click event).
lv_obj_t* splash_get_root(void);

// ---- The colony ----
// The splash stops being one Clawd and becomes the FLEET: one creature per
// agent, doing what its state says. Walking means working, waving means it is
// waiting for you, curled up means idle. The point is that you read the room
// from across the desk without reading a word -- which is what a splash screen
// is for, and what a list of names never manages.
//
// The states here are the SPLASH's, not the wire's. ui.cpp maps
// session_state_t onto them, so this module never learns the wire format and
// a new session code cannot break the art.
#define SPLASH_FLEET_MAX 16

enum splash_fleet_state_t : uint8_t {
    SPLASH_FLEET_WORKING = 0,   // busy; needs nothing
    SPLASH_FLEET_WAITING,       // stopped, and a person has to say something
    SPLASH_FLEET_IDLE,          // alive, doing nothing
    SPLASH_FLEET_DONE,          // finished
    SPLASH_FLEET_MESSAGE,       // said something to you
};

struct SplashFleetMember {
    char    label[16];          // the agent's name, already elided by the host
    uint8_t state;              // splash_fleet_state_t
};

// Hand the splash the current fleet. `n == 0` returns it to its single-Clawd
// behaviour, which is what a board with no host, or a quiet fleet, should
// still show -- the creature is the product's face, not a status widget, and
// an empty desk is not a reason to blank it.
//
// Cheap to call every payload: unchanged membership is detected and nothing is
// rebuilt, so the creatures keep their frames instead of restarting.
// `dropped` is how many live sessions did not fit; the last slot says so
// instead of drawing a creature. Silence there would make the screen a lie
// about how many agents there are, which is the one thing it must not be.
void splash_set_fleet(const SplashFleetMember *members, uint8_t n,
                      uint8_t dropped);

// Mini animated creature for embedding elsewhere (e.g. the idle screen).
// Renders the named official animation (e.g. "cloud") at ~px×px
// inside `parent`; returns the canvas object (position it with lv_obj_align) or
// NULL if the animation isn't found / allocation fails. Drive it with
// splash_mini_tick(). One mini creature at a time.
lv_obj_t* splash_mini_create(lv_obj_t *parent, const char *anim_name, int px);
void splash_mini_tick(void);

// Corner mascot (usage screen, PSRAM boards): the still Clawd idles in the
// logo slot, does occasional acts, and takes walk-off/lurk/walk-back trips.
// feet_y = px of the art's ground line; cell = px per art cell in the corner.
lv_obj_t* splash_mascot_create(lv_obj_t *parent, int slot_x, int feet_y, int cell);
void splash_mascot_tick(void);
void splash_mascot_set_visible(bool v);
