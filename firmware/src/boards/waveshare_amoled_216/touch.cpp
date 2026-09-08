#include "../../hal/touch_hal.h"
#include "../../hal/imu_hal.h"
#include "board.h"
#include <Arduino.h>
#include <Wire.h>
#include <TouchDrvCSTXXX.hpp>

static TouchDrvCST92xx touch;

static volatile bool     touch_data_ready = false;
static volatile bool     touch_pressed = false;
static volatile uint16_t touch_x = 0;
static volatile uint16_t touch_y = 0;

static void IRAM_ATTR touch_isr(void) {
    touch_data_ready = true;
}

void touch_hal_init(void) {
    touch.setPins(TP_RST, TP_INT);
    if (!touch.begin(Wire, CST9220_ADDR, IIC_SDA, IIC_SCL)) {
        Serial.println("Touch init failed");
        return;
    }
    touch.setMaxCoordinates(LCD_WIDTH, LCD_HEIGHT);
    touch.setSwapXY(true);
    touch.setMirrorXY(true, false);
    pinMode(TP_INT, INPUT_PULLUP);
    attachInterrupt(TP_INT, touch_isr, FALLING);
    Serial.println("Touch init OK");
}

// Undo the display's software rotation so a reported point lands on the pixel
// the user actually touched.
//
// This board rotates by remapping pixels inside display_hal_draw_bitmap() and
// never tells LVGL, so LVGL keeps drawing in the unrotated frame while the
// panel shows a rotated image. The CST9220 reports panel coordinates. The
// inverse of rotate_strip()'s LVGL -> panel mapping (display.cpp) is:
//
//   quadrant 1 (90 CW):  panel = (S-1-ly, lx)      -> lx = py,       ly = S-1-px
//   quadrant 2 (180):    panel = (S-1-lx, S-1-ly)  -> lx = S-1-px,   ly = S-1-py
//   quadrant 3 (270 CW): panel = (ly, S-1-lx)      -> lx = S-1-py,   ly = px
//
// Without this, at 90/270 a physically horizontal swipe reaches LVGL as a
// vertical one (so the tab ring is unreachable) and every settings row taps a
// different row than the one under the finger; at 180 both are inverted. It
// costs nothing at quadrant 0, which is how the board sits on a desk.
static void unrotate_point(uint16_t px, uint16_t py, uint16_t* lx, uint16_t* ly) {
    const int32_t W = LCD_WIDTH  - 1;
    const int32_t H = LCD_HEIGHT - 1;
    switch (imu_hal_rotation_quadrant()) {
    case 1: *lx = (uint16_t)py;      *ly = (uint16_t)(H - px); break;
    case 2: *lx = (uint16_t)(W - px); *ly = (uint16_t)(H - py); break;
    case 3: *lx = (uint16_t)(W - py); *ly = (uint16_t)px;      break;
    default: *lx = px; *ly = py; break;
    }
}

void touch_hal_read(uint16_t* x, uint16_t* y, bool* pressed) {
    if (touch_data_ready) {
        touch_data_ready = false;
        int16_t tx[5], ty[5];
        uint8_t n = touch.getPoint(tx, ty, touch.getSupportTouchPoint());
        if (n > 0) {
            touch_pressed = true;
            touch_x = (uint16_t)tx[0];
            touch_y = (uint16_t)ty[0];
        } else {
            touch_pressed = false;
        }
    }
    // Rotate on read, not on capture: the quadrant can change while a finger
    // is down, and the last latched point must follow the image it refers to.
    unrotate_point(touch_x, touch_y, x, y);
    *pressed = touch_pressed;
}
