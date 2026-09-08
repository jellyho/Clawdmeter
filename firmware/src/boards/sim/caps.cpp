#include "../../hal/board_caps.h"
#include "board.h"

static const BoardCaps caps = {
    .name = BOARD_NAME,
    .width = LCD_WIDTH,
    .height = LCD_HEIGHT,
    .button_count = 2,      // B and N keys stand in for BOOT + GPIO18
    .has_rotation = false,
    .has_battery = true,    // fake battery, adjustable with -/=
    .has_imu = false,
    .has_sound = (bool)BOARD_HAS_SOUND,
    // Mirrors the -DBOARD_HAS_SESSION_VIEWS in [env:sim]: the sim window is
    // the 2.16's 480×480, so the chat cards fit exactly as they do there.
    .has_session_views = (bool)BOARD_HAS_SESSION_VIEWS,
};

const BoardCaps& board_caps(void) { return caps; }
