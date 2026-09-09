#pragma once
#include <stdint.h>

enum ble_state_t {
    BLE_STATE_INIT,
    BLE_STATE_ADVERTISING,
    BLE_STATE_CONNECTED,
    BLE_STATE_DISCONNECTED,
};

void ble_init(void);
void ble_tick(void);
ble_state_t ble_get_state(void);
const char* ble_get_device_name(void);
const char* ble_get_mac_address(void);
void ble_clear_bonds(void);
bool ble_has_bonds(void);
bool ble_has_data(void);
const char* ble_get_data(void);
// Session-awareness feed (issue #135) — mirrors ble_has_data/ble_get_data for
// the SS characteristic. Present on every board; boards without
// BOARD_HAS_SESSION_VIEWS simply never drain it.
bool ble_has_session_data(void);
const char* ble_get_session_data(void);
void ble_send_ack(void);
void ble_send_nack(void);
void ble_request_refresh(void);

// --- Device -> host button events (TX characteristic ...0003) --------------
//
// TX has notified {"ack":true} / {"err":true} since the first firmware and no
// host has ever subscribed to it. A button event rides the same characteristic
// with one extra key: an "ev" integer that the ack/nack traffic does not carry,
// so a subscriber dispatches on its presence and ignores everything else.
//
//   {"ev":1}                report round - "tell me what the fleet is doing"
//   {"ev":2,"sid":"g4"}     go ahead, to the agent on that card
//   {"ev":3,"sid":"g4"}     dismiss that card - stop sending me this row
//   {"ev":4}                broadcast the standing rules to every agent
//
// Codes are APPEND-ONLY. They cross the BLE boundary exactly like the session
// state codes in data.h, so a released code is never renumbered or reused.
//
// GO_AHEAD and DISMISS are the two halves of the same gesture: every card on
// the sessions tab is now a thing you TAP, and what the tap means depends on
// whether the card is waiting for a word from you (go ahead) or merely telling
// you something (dismiss). Both carry the sid of the card that was tapped,
// which is the only handle the device has on the row - the host minted it and
// the host is the only side that can turn it back into an agent to talk to.
//
// A DISMISS is advisory, not a delete. The device also suppresses the row
// locally the moment it is tapped, because the card has to leave under the
// finger and the round trip is a second away at best; the event exists so the
// host stops RE-SENDING it, which is what makes the dismissal outlive a
// reboot. If it never arrives, the local suppression still holds for as long
// as the device is up, and the row comes back on the next boot - annoying,
// not wrong.
enum ble_event_t {
    BLE_EVENT_REPORT   = 1,   // town hall button
    BLE_EVENT_GO_AHEAD = 2,   // carries a sid - "continue, you have my answer"
    BLE_EVENT_DISMISS  = 3,   // carries a sid - "I have read this, drop it"
    // Settings > Teach agents. Asks the host to tell every reachable agent how
    // Clawdmeter works and, more to the point, that none of its messages are
    // permission for anything (daemon/REPORT.md, "The standing rules"). One
    // turn per agent; the ones that already know do nothing.
    BLE_EVENT_BROADCAST = 4,
};

// Notify one button event to the BONDED OWNER only, on its encrypted link.
// `sid` is optional (NULL for BLE_EVENT_REPORT) and is sanitised before it goes
// on the wire. Returns true when the event was handed to the stack for at least
// one subscribed owner link; false means nobody is listening and nothing was
// sent - a normal state (no daemon, older daemon, link down), not an error.
bool ble_send_event(ble_event_t ev, const char* sid);

// The one line the panel's TOWN HALL button calls (ui.cpp). No hardware
// button reaches this any more -- a round spends quota on other people's
// machines, which is not something a side button should be able to do.
bool ble_send_report_request(void);

void ble_set_battery_level(int pct);

// BLE HID keyboard
void ble_keyboard_press(uint8_t key, uint8_t modifier);
void ble_keyboard_release(void);
