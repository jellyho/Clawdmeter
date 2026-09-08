#include "ble.h"
#include <Arduino.h>
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>
#include <Preferences.h>

// Raw NimBLE host headers for the Service Changed machinery further down:
// ble_gatts_find_chr() to locate 0x2A05, ble_store_read_cccd() to ask the stack
// whether an invalidation it queued has actually been confirmed, and the
// Service Changed UUID/entry point themselves. The guard mirrors the library's
// own (NimBLEServer.cpp); nimconfig.h defines USING_NIMBLE_ARDUINO_HEADERS.
#ifdef USING_NIMBLE_ARDUINO_HEADERS
# include "nimble/nimble/host/include/host/ble_gatt.h"
# include "nimble/nimble/host/include/host/ble_store.h"
# include "nimble/nimble/host/services/gatt/include/services/gatt/ble_svc_gatt.h"
#else
# include "host/ble_gatt.h"
# include "host/ble_store.h"
# include "services/gatt/ble_svc_gatt.h"
#endif

// NimBLEServer::sendServiceChangedIndication() only exists from NimBLE-Arduino
// 2.5.0, and lib_deps floors at ^2.1.1 — a contributor with an older resolved
// tree must still build. The wrapper is a one-line call into ble_svc_gatt_changed(),
// which has been there all through 2.x, so fall back to that.
#ifdef NIMBLE_CPP_VERSION_VAL
# if NIMBLE_CPP_VERSION >= NIMBLE_CPP_VERSION_VAL(2, 5, 0)
#  define CLAWD_NIMBLE_SVC_CHANGED_WRAPPER 1
# endif
#endif
#ifndef CLAWD_NIMBLE_SVC_CHANGED_WRAPPER
# define CLAWD_NIMBLE_SVC_CHANGED_WRAPPER 0
#endif

#define DEVICE_NAME "Clawdmeter"

// Custom GATT UUIDs for data channel
#define SERVICE_UUID        "4c41555a-4465-7669-6365-000000000001"
#define RX_CHAR_UUID        "4c41555a-4465-7669-6365-000000000002"  // host writes here
#define TX_CHAR_UUID        "4c41555a-4465-7669-6365-000000000003"  // device ack/nack notifies
#define REQ_CHAR_UUID       "4c41555a-4465-7669-6365-000000000004"  // device-initiated refresh request
#define SS_CHAR_UUID        "4c41555a-4465-7669-6365-000000000005"  // host writes session rows here

#define BLE_BUF_SIZE 512
// Session payloads carry up to SESSION_MAX_ROWS positional rows and get their
// own, larger budget (the device requests a 517-byte MTU; 1024 leaves headroom
// for a long write).
#define BLE_SS_BUF_SIZE 1024

// HID keyboard report descriptor (standard 6-KRO boot-protocol-compatible).
// Includes the LED output report (Num/Caps/Scroll Lock indicators) — without
// it macOS's Keyboard Setup Assistant flags the device as "unidentifiable"
// because the descriptor doesn't look like a complete keyboard.
static const uint8_t HID_REPORT_MAP[] = {
    0x05, 0x01,  // Usage Page (Generic Desktop)
    0x09, 0x06,  // Usage (Keyboard)
    0xA1, 0x01,  // Collection (Application)
    0x85, 0x01,  //   Report ID (1)
    0x05, 0x07,  //   Usage Page (Key Codes)
    0x19, 0xE0,  //   Usage Minimum (224) - Left Control
    0x29, 0xE7,  //   Usage Maximum (231) - Right GUI
    0x15, 0x00,  //   Logical Minimum (0)
    0x25, 0x01,  //   Logical Maximum (1)
    0x75, 0x01,  //   Report Size (1)
    0x95, 0x08,  //   Report Count (8)
    0x81, 0x02,  //   Input (Data, Variable, Absolute) - Modifier byte
    0x95, 0x01,  //   Report Count (1)
    0x75, 0x08,  //   Report Size (8)
    0x81, 0x01,  //   Input (Constant) - Reserved byte
    // LED output report — required for macOS to treat this as a full keyboard.
    0x95, 0x05,  //   Report Count (5)
    0x75, 0x01,  //   Report Size (1)
    0x05, 0x08,  //   Usage Page (LEDs)
    0x19, 0x01,  //   Usage Minimum (Num Lock)
    0x29, 0x05,  //   Usage Maximum (Kana)
    0x91, 0x02,  //   Output (Data, Variable, Absolute) - LED report
    0x95, 0x01,  //   Report Count (1)
    0x75, 0x03,  //   Report Size (3)
    0x91, 0x01,  //   Output (Constant) - LED report padding
    0x95, 0x06,  //   Report Count (6)
    0x75, 0x08,  //   Report Size (8)
    0x15, 0x00,  //   Logical Minimum (0)
    0x25, 0x65,  //   Logical Maximum (101)
    0x05, 0x07,  //   Usage Page (Key Codes)
    0x19, 0x00,  //   Usage Minimum (0)
    0x29, 0x65,  //   Usage Maximum (101)
    0x81, 0x00,  //   Input (Data, Array) - Key array (6 keys)
    0xC0,        // End Collection
};

static NimBLEServer* server = nullptr;
static NimBLEHIDDevice* hid_dev = nullptr;
static NimBLEService* data_svc = nullptr;
static NimBLECharacteristic* input_kbd = nullptr;
static NimBLECharacteristic* tx_char = nullptr;
static NimBLECharacteristic* rx_char = nullptr;
static NimBLECharacteristic* req_char = nullptr;
#if BOARD_HAS_SESSION_VIEWS
static NimBLECharacteristic* ss_char = nullptr;
#endif

static ble_state_t state = BLE_STATE_INIT;
static bool need_advertise = false;

// One-shot supervision-timeout pushback (see onConnParamsUpdate). Written by
// NimBLE host-task callbacks, consumed by ble_tick() on the loop task.
static const uint16_t CONN_HANDLE_NONE  = 0xFFFF;
static const uint16_t DESIRED_TIMEOUT   = 600;   // ×10ms = 6s, matches PPCP
static volatile uint16_t param_fix_handle = CONN_HANDLE_NONE;  // pending retry
static volatile uint32_t param_fix_at_ms  = 0;                 // when to send it
static volatile uint16_t param_fix_spent  = CONN_HANDLE_NONE;  // one per connection
static char rx_buf[BLE_BUF_SIZE];
static volatile bool data_ready = false;
static volatile bool has_received_data = false;
#if BOARD_HAS_SESSION_VIEWS
// Only the ports that can render the chat views carry this: 1 KB of static
// internal SRAM plus the airtime of a write on every session change, charged
// otherwise to the two PSRAM-free C6 parts for a feature they compile out.
// With the characteristic absent the daemon's maybe_send_sessions() finds no
// SS UUID and stays quiet, which it already handles.
static char ss_buf[BLE_SS_BUF_SIZE];
static volatile bool ss_ready = false;
#endif
static char mac_str[18];

// --- Single-owner lock -----------------------------------------------------
//
// The board is a BLE peripheral that any central in range could connect to and
// write usage data to. To stop the display rotating to another machine's
// account, it locks to ONE owner: the identity address of the machine it is
// bonded to, persisted in NVS. Only that owner (over a bonded+encrypted link)
// may write usage data; a second machine that pairs is rejected so the board
// stays paired to a single machine. The hold-power bond-clear gesture resets
// the owner so the board can be handed to a different machine.
static Preferences prefs;
static char owner_addr[18] = {0};   // owner identity address, e.g. "aa:bb:cc:dd:ee:ff"
static bool owner_set = false;
static const char* ZERO_ADDR = "00:00:00:00:00:00";

static void save_owner() {
    prefs.begin("clawd", false);
    prefs.putString("owner", owner_addr);
    prefs.end();
}

static void clear_owner() {
    owner_set = false;
    owner_addr[0] = '\0';
    prefs.begin("clawd", false);
    prefs.remove("owner");
    prefs.end();
}

static void load_owner() {
    prefs.begin("clawd", true);
    String o = prefs.getString("owner", "");
    prefs.end();
    if (o.length() == 17) {  // "aa:bb:cc:dd:ee:ff"
        strncpy(owner_addr, o.c_str(), sizeof(owner_addr) - 1);
        owner_addr[sizeof(owner_addr) - 1] = '\0';
        owner_set = true;
        Serial.printf("BLE: owner loaded = %s\n", owner_addr);
    }
}

// Delete every stored bond that isn't the owner, so the board stays paired to
// exactly one machine. Removing a bond shifts the indices, so restart from 0.
static void prune_foreign_bonds() {
    if (!owner_set) return;
    bool removed;
    do {
        removed = false;
        int n = NimBLEDevice::getNumBonds();
        for (int i = 0; i < n; i++) {
            NimBLEAddress a = NimBLEDevice::getBondedAddress(i);
            if (strcmp(a.toString().c_str(), owner_addr) != 0) {
                Serial.printf("BLE: pruning non-owner bond %s\n", a.toString().c_str());
                NimBLEDevice::deleteBond(a);
                removed = true;
                break;
            }
        }
    } while (removed);
}

static void claim_owner(const std::string& id) {
    strncpy(owner_addr, id.c_str(), sizeof(owner_addr) - 1);
    owner_addr[sizeof(owner_addr) - 1] = '\0';
    owner_set = true;
    save_owner();
    Serial.printf("BLE: owner claimed = %s\n", owner_addr);
    prune_foreign_bonds();
}

// --- GATT layout change → Service Changed ----------------------------------
//
// WHY THIS EXISTS: a bonded host is allowed by the Bluetooth spec to cache our
// entire attribute table and reuse it forever without ever re-discovering. So
// when a firmware update adds, removes or moves a characteristic, the host
// keeps talking to the OLD table — it still finds everything it already knew,
// so the link looks perfectly healthy, while anything new is simply invisible.
// That is how adding the session characteristic (…0005) silently half-broke
// already-bonded Windows and macOS hosts: usage payloads kept flowing, the
// daemon logged "Device has no session characteristic" forever, and only
// forgetting the pairing healed it. Toggling the radio does not help — the
// cache is per-bond, not per-connection — and no host-side flag substitutes
// either: the Windows daemon already asks for uncached discovery
// (claude_usage_daemon_windows.py, use_cached_services=False) and still gets
// the stale table.
//
// That is upstream issue #142, whose macOS report is the same failure one
// release earlier: the battery-level change moved a handle, so CoreBluetooth's
// CCCD write for REQ landed on a pre-update attribute and came back
// CBATTErrorDomain Code=3 while the payload writes (WRITE_NR) failed silently.
//
// It is NOT issue #88. That one is a stale BONDING KEY — the host's LTK no
// longer matches ours, so the link never encrypts at all. Service Changed can
// only travel over a bonded link that HAS encrypted: ble_gap_enc_event()
// returns immediately on a non-zero status and only reaches
// ble_gatts_bonding_restored() on a successful security restore. So this change
// does nothing for #88, which still needs its host-side unpair/re-pair
// self-heal. Do not close #88 on the strength of this.
//
// The only in-band way to tell a bonded host "throw your cache away" is an
// indication on the standard Service Changed characteristic (0x2A05 on the
// Generic Attribute service 0x1801). NimBLE registers that service for us
// inside server->start(), and it even auto-indicates — but only for services
// changed AFTER the server started. On a cold boot every service is built
// first, so its m_svcChanged flag is false and the indication never goes out.
// That gap is precisely the bug. We fire it ourselves.
//
// Firing it unconditionally on every boot would be worse than the bug: the
// host would tear down and re-discover the whole database on every reconnect.
// So we hash the layout the stack just handed us and remember the hash in NVS;
// the indication goes out only when this image's layout differs from what the
// bonded host was last told about, or when the stack's own bookkeeping says the
// last one was never confirmed (see gatt_change_unconfirmed). Existing users
// self-heal for free — a board bitten by #142 has no stored hash at all, so the
// first boot of this firmware mismatches and indicates with no user action.
//
// WARNING for future edits: the persisted per-peer subscription record the
// stack replays this on is keyed by the 0x2A05 value handle. 0x1801 is
// registered before any app service, so appending characteristics never moves
// it — but changing the GAP service composition (the MYNEWT_VAL_BLE_SVC_GAP_*
// build flags in platformio.ini) WOULD shift it, orphan every stored record,
// and leave re-pairing as the only cure. Treat those flags as breaking.

// Bump this by hand for a layout change the walk below cannot see. It hashes
// services and characteristics — handle, properties, UUID — but NimBLE's C++
// API exposes no descriptor vector (only NimBLECharacteristic::getDescriptorByUUID,
// which needs to be told what to look for), so a descriptor APPENDED to the last
// characteristic of the last service moves no hashed handle and would slip
// through silently. A descriptor added anywhere else is caught, because it
// shifts every handle downstream of it.
#define CLAWD_GATT_LAYOUT_EPOCH 1

// Set when this boot fired an invalidation and the owner still owes us one
// forced re-discovery (see gatt_arm_link_recycle). RAM only, one-shot per boot.
static volatile bool gatt_owe_link_recycle = false;
static volatile uint16_t gatt_recycle_handle = CONN_HANDLE_NONE;
static volatile uint32_t gatt_recycle_at_ms  = 0;
// Long enough that the Service Changed indication + the peer's ATT confirm have
// completed (one round trip, tens of ms at any negotiated interval), short
// enough that the user is not left staring at a half-healed device.
static const uint32_t GATT_RECYCLE_DELAY_MS = 1500;

static uint32_t sig_mix(uint32_t h, const void* data, size_t len) {
    const uint8_t* p = (const uint8_t*)data;
    while (len--) {           // FNV-1a: no table, no heap, ~40 bytes of code
        h ^= *p++;
        h *= 16777619u;
    }
    return h;
}

static uint32_t sig_mix_uuid(uint32_t h, const NimBLEUUID& uuid) {
    uint8_t bits = uuid.bitSize();          // 16 / 32 / 128, or 0 if unset
    h = sig_mix(h, &bits, sizeof(bits));
    const uint8_t* val = uuid.getValue();
    if (val) h = sig_mix(h, val, bits / 8);
    return h;
}

// Hash one service and every characteristic under it. Handles are the payload
// that matters: GAP and GATT are registered before any app service, so all of
// ours sit at the tail of the table and ANY structural change upstream of them
// shifts at least one. Properties come along to catch a same-shape change
// (WRITE gaining WRITE_NR), and UUIDs to catch a re-pointed characteristic
// that happens to land on the same handle.
static uint32_t sig_mix_service(uint32_t h, NimBLEService* svc) {
    if (!svc) return sig_mix(h, "\xFF", 1);  // a missing service is itself a fact
    uint16_t handle = svc->getHandle();
    h = sig_mix(h, &handle, sizeof(handle));
    h = sig_mix_uuid(h, svc->getUUID());
    for (NimBLECharacteristic* chr : svc->getCharacteristics()) {
        if (!chr) continue;
        uint16_t chr_handle = chr->getHandle();
        uint16_t chr_props  = chr->getProperties();
        h = sig_mix(h, &chr_handle, sizeof(chr_handle));
        h = sig_mix(h, &chr_props, sizeof(chr_props));
        h = sig_mix_uuid(h, chr->getUUID());
    }
    return h;
}

// Walking the live services (rather than the file-static characteristic
// pointers) keeps this free of every per-board conditional: whatever a board
// compiled in — the session characteristic included — is in the vector.
static uint32_t gatt_layout_signature() {
    uint32_t h = 2166136261u;  // FNV-1a offset basis
    uint8_t epoch = CLAWD_GATT_LAYOUT_EPOCH;
    h = sig_mix(h, &epoch, sizeof(epoch));
    // The HID report descriptor is cached next to the attribute table by the
    // host's HID stack, so editing it is a layout change by the same argument.
    h = sig_mix(h, HID_REPORT_MAP, sizeof(HID_REPORT_MAP));
    if (hid_dev) {
        h = sig_mix_service(h, hid_dev->getDeviceInfoService());
        h = sig_mix_service(h, hid_dev->getHidService());
        h = sig_mix_service(h, hid_dev->getBatteryService());
    }
    h = sig_mix_service(h, data_svc);
    return h;
}

// Ask the stack whether a Service Changed indication it queued is still owed to
// a bonded peer, and how many peers are subscribed at all.
//
// We do not have to guess at this, and we deliberately do not try. The stack
// keeps a "value_changed" bit beside each peer's Service Changed subscription:
// ble_gatts_chr_updated() sets and PERSISTS it the moment ble_svc_gatt_changed()
// runs, ble_store_config restores it from NVS inside NimBLEDevice::init(),
// ble_gatts_bonding_restored() replays the indication on that peer's next
// encrypted reconnect, and ble_gatts_rx_indicate_ack() clears it only on the
// peer's own ATT confirm. Reading it back is therefore the authoritative answer
// to "did the last invalidation actually land?" — no second bookkeeping key of
// ours to get out of step, and nothing inferred from connection events (an
// encrypted link is not a delivered indication, and a bonded peer is not
// necessarily the peer that was owed one).
//
// Why we have to ask at all: the indicated handle RANGE is not stored per peer.
// It is two file-static uint16_t inside ble_svc_gatt.c, BSS-zero at every boot,
// serialised verbatim when the peer finally reads the value. A replay still
// pending across a reboot where we did not re-arm would hand the host a
// meaningless 0x0000-0x0000 range; the host would confirm it, the stack would
// clear value_changed, and the cache would stay stale forever with nothing left
// to re-trigger it. Re-arming is idempotent, so we just do it again while the
// bit is set.
//
// Returns the number of peers still owed the indication, or -1 if 0x2A05 could
// not be located at all. out_subscribers gets the number of stored 0x2A05
// subscriptions, which is the one prerequisite of this whole mechanism that is
// otherwise invisible: with zero of them ble_gatts_chr_updated() finds no
// record, does nothing, and returns silently.
static int gatt_change_unconfirmed(int* out_subscribers) {
    if (out_subscribers) *out_subscribers = 0;

    ble_uuid16_t gatt_svc_uuid;
    gatt_svc_uuid.u.type = BLE_UUID_TYPE_16;
    gatt_svc_uuid.value  = BLE_GATT_SVC_UUID16;                     // 0x1801
    ble_uuid16_t sc_chr_uuid;
    sc_chr_uuid.u.type = BLE_UUID_TYPE_16;
    sc_chr_uuid.value  = BLE_SVC_GATT_CHR_SERVICE_CHANGED_UUID16;   // 0x2A05

    uint16_t sc_val_handle = 0;
    if (ble_gatts_find_chr(&gatt_svc_uuid.u, &sc_chr_uuid.u, nullptr, &sc_val_handle) != 0) {
        return -1;
    }

    struct ble_store_key_cccd key;
    struct ble_store_value_cccd val;
    memset(&key, 0, sizeof(key));       // all-zero peer_addr == BLE_ADDR_ANY: any peer
    key.chr_val_handle = sc_val_handle; // ...but only the Service Changed record

    int subs = 0, unconfirmed = 0;
    while (ble_store_read_cccd(&key, &val) == 0) {
        subs++;
        if (val.value_changed) unconfirmed++;
        key.idx++;
    }
    if (out_subscribers) *out_subscribers = subs;
    return unconfirmed;
}

static void gatt_send_service_changed() {
    // Always the full 0x0001–0xFFFF range ("discard everything"). The range is
    // stored in two globals inside the stack, NOT per peer, so a narrower range
    // sent while another peer's indication is still queued would silently apply
    // to both. Full range makes that class of bug impossible.
#if CLAWD_NIMBLE_SVC_CHANGED_WRAPPER
    server->sendServiceChangedIndication();
#else
    ble_svc_gatt_changed(0x0001, 0xFFFF);
#endif
}

// Called once, right after server->start() — handles are assigned by then, and
// we are still off the air.
static void gatt_check_layout() {
    uint32_t sig = gatt_layout_signature();

    prefs.begin("clawd", true);
    uint32_t known = prefs.getUInt("gattsig", 0);
    prefs.end();

    bool changed = (sig != known);
    if (changed) {
        prefs.begin("clawd", false);
        prefs.putUInt("gattsig", sig);
        prefs.end();
    }

    int subscribers = 0;
    int unconfirmed = gatt_change_unconfirmed(&subscribers);

    if (!changed && unconfirmed <= 0) {
        Serial.printf("BLE: GATT layout unchanged (sig 0x%08X), bonded hosts keep their cache\n",
            (unsigned)sig);
        return;
    }
    if (changed) {
        Serial.printf("BLE: GATT layout signature 0x%08X (was 0x%08X) - attribute table changed\n",
            (unsigned)sig, (unsigned)known);
    } else {
        Serial.printf("BLE: GATT layout unchanged (sig 0x%08X) but %d peer(s) never confirmed the "
                      "last invalidation - re-arming\n", (unsigned)sig, unconfirmed);
    }

    gatt_send_service_changed();
    // From here the stack does the per-peer bookkeeping: a connected peer gets
    // the indication now, and for every bonded-but-absent peer it persists the
    // "value changed" flag and replays on that peer's next bonded reconnect,
    // clearing it only on that peer's ack. So there is deliberately no
    // per-connect delivery tracking of our own below.
    if (subscribers > 0) {
        gatt_owe_link_recycle = true;
        Serial.printf("BLE: Service Changed indicated over 0x0001-0xFFFF to %d subscribed peer(s)"
                      " - they will re-discover\n", subscribers);
    } else if (unconfirmed < 0) {
        Serial.println("BLE: Service Changed characteristic not found - cannot invalidate host caches");
    } else {
        // Not a failure on a fresh board (no bonds yet = nothing cached), but on
        // a bonded one it means the cure cannot be delivered and only forgetting
        // the pairing will heal it. Say so rather than logging a success that
        // never happened.
        Serial.printf("BLE: no host is subscribed to Service Changed (bonds=%d) - nothing to"
                      " invalidate\n", NimBLEDevice::getNumBonds());
    }
    if (data_svc) {
        const std::vector<NimBLECharacteristic*>& chrs = data_svc->getCharacteristics();
        Serial.printf("BLE: data service handle=%u with %u characteristics\n",
            (unsigned)data_svc->getHandle(), (unsigned)chrs.size());
    }
}

// Force exactly ONE reconnect after an invalidation, so the host re-enumerates.
//
// Invalidating the cache is not the same as the host acting on it. The
// indication is delivered during encryption restore of a connection the host
// has already begun enumerating — and the daemon reads its service list once,
// from the snapshot taken inside client.connect(), then holds that link for
// hours (probe_session_support() in claude_usage_daemon_windows.py runs once per
// connection). Lose that race and the daemon keeps logging "Device has no
// session characteristic" on a device that has already been healed, which looks
// exactly like the bug still being present. Dropping the link once, after the
// indication has been confirmed, makes the re-discovery deterministic and costs
// one reconnect the daemon already knows how to do.
//
// Armed only for the peer that was ALREADY the owner at boot: a first-ever
// pairing did a full discovery anyway, and a foreign peer is being un-bonded on
// the next line. One-shot per boot.
static void gatt_arm_link_recycle(uint16_t conn_handle) {
    if (!gatt_owe_link_recycle) return;
    gatt_owe_link_recycle = false;
    gatt_recycle_handle = conn_handle;
    gatt_recycle_at_ms  = millis() + GATT_RECYCLE_DELAY_MS;
}

static void start_advertising() {
    NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
    adv->reset();
    // Primary advertising packet (≤31 bytes):
    //   flags (3) + appearance (4) + HID service 0x1812 (4) + name "Clawdmeter" (12)
    //   = 23 bytes. macOS Bluetooth Settings only surfaces BLE-only devices
    //   that explicitly advertise the standard HID service UUID (0x1812) —
    //   without it the device is recognized internally but hidden from the
    //   GUI nearby-devices list.
    adv->setAppearance(HID_KEYBOARD);
    adv->addServiceUUID(NimBLEUUID((uint16_t)0x1812));  // BLE HID Service
    adv->setName(DEVICE_NAME);
    // Scan response carries the 128-bit custom data-service UUID for active
    // scanners (the host daemon scans actively).
    NimBLEAdvertisementData scanResp;
    scanResp.setCompleteServices(NimBLEUUID(SERVICE_UUID));
    adv->setScanResponseData(scanResp);
    adv->enableScanResponse(true);
    bool ok = adv->start();
    // Only reflect ADVERTISING in the UI state when no client is connected.
    // With MAX_CONNECTIONS=2, onConnect re-advertises to fill the second slot;
    // without this guard the UI would flip CONNECTED → ADVERTISING on every
    // first connect and never come back until a second client arrived.
    if (!server || server->getConnectedCount() == 0) {
        state = BLE_STATE_ADVERTISING;
    }
    Serial.printf("BLE: advertising start=%s (connected=%u)\n",
        ok ? "OK" : "FAILED",
        server ? (unsigned)server->getConnectedCount() : 0);
}

class ServerCallbacks : public NimBLEServerCallbacks {
    void onConnect(NimBLEServer* s, NimBLEConnInfo& info) override {
        state = BLE_STATE_CONNECTED;
        Serial.printf("BLE: connected from %s (active=%u)\n",
            info.getAddress().toString().c_str(),
            (unsigned)s->getConnectedCount());
        // Log negotiated link timing — the difference between guessing and
        // knowing when debugging disconnects (e.g. reason=520 supervision
        // timeouts are only explainable next to the negotiated timeout).
        // Units: interval ×1.25ms, timeout ×10ms, latency = skippable events.
        Serial.printf("BLE: connparams itvl=%u(%.2fms) lat=%u timeout=%u(%ums)\n",
            info.getConnInterval(), info.getConnInterval() * 1.25f,
            info.getConnLatency(), info.getConnTimeout(), info.getConnTimeout() * 10);
        // Keep advertising while a connection slot is still free so a second
        // central (e.g. the host daemon alongside an OS-held HID link) can
        // discover and connect. NimBLE auto-stops advertising on each accept.
        if (s->getConnectedCount() < CONFIG_BT_NIMBLE_MAX_CONNECTIONS) {
            need_advertise = true;
        }
    }

    void onDisconnect(NimBLEServer* s, NimBLEConnInfo& info, int reason) override {
        // Only flip the UI state to DISCONNECTED when the last client leaves.
        if (s->getConnectedCount() == 0) state = BLE_STATE_DISCONNECTED;
        need_advertise = true;
        // Drop any pending/spent param pushback for this handle — NimBLE
        // reuses conn handles, so stale state would leak onto the next link.
        if (param_fix_handle == info.getConnHandle()) param_fix_handle = CONN_HANDLE_NONE;
        if (param_fix_spent  == info.getConnHandle()) param_fix_spent  = CONN_HANDLE_NONE;
        // Same for the pending cache-recycle drop: if the link went away on its
        // own, the host will re-enumerate on reconnect anyway.
        if (gatt_recycle_handle == info.getConnHandle()) gatt_recycle_handle = CONN_HANDLE_NONE;
        Serial.printf("BLE: disconnected (reason=%d, remaining=%u)\n",
            reason, (unsigned)s->getConnectedCount());
    }

    // Centrals re-negotiate parameters mid-connection. Windows in particular
    // clamps the supervision timeout to 2s once an app GATT session goes
    // active (captured on hardware; it honors 9.6s while only its HID driver
    // holds the link) — and a 2s window is tight enough that ordinary radio
    // gaps kill the link (HCI 0x208 → reason=520), which was the constant
    // daemon reconnect churn. Push back ONCE per connection: schedule a
    // deferred LL connection-parameter request for the same interval range but
    // a 6s timeout (the mechanism Microsoft's accessory guidelines prescribe).
    // Deferred ~2s so it can't race the central's own in-flight update
    // transaction (Windows has a documented late-instant bug there), and
    // one-shot so a central that re-clamps doesn't trigger an update war.
    void onConnParamsUpdate(NimBLEConnInfo& info) override {
        Serial.printf("BLE: connparams update itvl=%u(%.2fms) lat=%u timeout=%u(%ums)\n",
            info.getConnInterval(), info.getConnInterval() * 1.25f,
            info.getConnLatency(), info.getConnTimeout(), info.getConnTimeout() * 10);
        if (info.getConnTimeout() < DESIRED_TIMEOUT &&
            info.getConnHandle() != param_fix_spent) {
            param_fix_handle = info.getConnHandle();
            param_fix_at_ms  = millis() + 2000;
        }
    }

    // Lock the board to a single owner machine. The first machine to bond
    // becomes the owner; any other machine that pairs is un-bonded and dropped
    // so the board never shows (or rotates to) a second machine's account.
    void onAuthenticationComplete(NimBLEConnInfo& info) override {
        std::string id = info.getIdAddress().toString();
        Serial.printf("BLE: auth complete peer=%s bonded=%d enc=%d\n",
            id.c_str(), info.isBonded() ? 1 : 0, info.isEncrypted() ? 1 : 0);
        // Bonded reconnects START at the central's clamped parameters (no
        // later update event fires), so the supervision-timeout pushback must
        // also arm here, not just in onConnParamsUpdate.
        if (info.getConnTimeout() < DESIRED_TIMEOUT &&
            info.getConnHandle() != param_fix_spent) {
            param_fix_handle = info.getConnHandle();
            param_fix_at_ms  = millis() + 2000;
        }
        if (id == ZERO_ADDR) return;
        if (!owner_set) {
            claim_owner(id);
        } else if (strcmp(id.c_str(), owner_addr) != 0) {
            Serial.printf("BLE: rejecting non-owner %s (owner=%s)\n", id.c_str(), owner_addr);
            NimBLEDevice::deleteBond(info.getIdAddress());
            server->disconnect(info);
        } else if (info.isBonded()) {
            // The owner is back on a restored bond, which is the event that
            // makes the stack replay the Service Changed indication (it calls
            // ble_gatts_bonding_restored() right after this callback returns).
            // Schedule the one forced re-discovery cycle; see the function.
            gatt_arm_link_recycle(info.getConnHandle());
        }
    }

};

// Single-owner write guard shared by every host-writable characteristic (RX
// usage, SS sessions). Only accept data over a bonded+encrypted link, and only
// from the owner machine. Another machine's daemon in range is ignored so the
// display never rotates to a foreign account. The first encrypted writer
// claims ownership when none is set yet (e.g. a fresh pairing).
static bool write_allowed(NimBLEConnInfo& info, const char* what) {
    std::string id = info.getIdAddress().toString();
    if (!info.isEncrypted()) {
        Serial.printf("BLE: dropping %s write from unencrypted link\n", what);
        return false;
    }
    if (!owner_set && id != ZERO_ADDR) {
        claim_owner(id);
    }
    if (owner_set && strcmp(id.c_str(), owner_addr) != 0) {
        Serial.printf("BLE: dropping %s write from non-owner %s\n", what, id.c_str());
        return false;
    }
    return true;
}

class RxCallbacks : public NimBLECharacteristicCallbacks {
    void onWrite(NimBLECharacteristic* chr, NimBLEConnInfo& info) override {
        if (!write_allowed(info, "RX")) return;
        std::string val = chr->getValue();
        size_t len = std::min(val.length(), (size_t)(BLE_BUF_SIZE - 1));
        memcpy(rx_buf, val.c_str(), len);
        rx_buf[len] = '\0';
        data_ready = true;
        has_received_data = true;
    }
};

#if BOARD_HAS_SESSION_VIEWS
// Session rows (issue #135). Same guard as RX; separate buffer because the
// two feeds have unrelated cadences (quota every 60s, sessions on change) and
// must not clobber each other between main-loop polls.
class SsCallbacks : public NimBLECharacteristicCallbacks {
    void onWrite(NimBLECharacteristic* chr, NimBLEConnInfo& info) override {
        if (!write_allowed(info, "SS")) return;
        std::string val = chr->getValue();
        size_t len = std::min(val.length(), (size_t)(BLE_SS_BUF_SIZE - 1));
        memcpy(ss_buf, val.c_str(), len);
        ss_buf[len] = '\0';
        ss_ready = true;
    }
};
#endif

// When the daemon enables notifications on the refresh char, ask for data
// if we have none yet. Firing on subscribe (not on connect) ensures the
// notification isn't dropped before the daemon's CCCD write completes.
class ReqCallbacks : public NimBLECharacteristicCallbacks {
    void onSubscribe(NimBLECharacteristic* chr, NimBLEConnInfo& info, uint16_t subValue) override {
        Serial.printf("BLE: req_char onSubscribe subValue=%u has_data=%d\n", subValue, has_received_data ? 1 : 0);
        if (subValue != 0 && !has_received_data) {
            ble_request_refresh();
        }
    }
};

void ble_init(void) {
    NimBLEDevice::init(DEVICE_NAME);
    NimBLEDevice::setSecurityAuth(true, false, true);  // bonding, no MITM, SC
#if BOARD_HAS_SESSION_VIEWS
    // Session payloads want room for several rows in one write. The host
    // derives its row budget from the negotiated MTU, so ask for the max.
    // Quota payloads have always fit the default MTU, so boards without the
    // chat views keep it (and the smaller per-connection buffers).
    NimBLEDevice::setMTU(517);
#endif

    // Restore the locked owner (if any) and drop any stale non-owner bonds so
    // the board stays paired to a single machine across reboots.
    load_owner();
    prune_foreign_bonds();

    // Format MAC address
    NimBLEAddress addr = NimBLEDevice::getAddress();
    snprintf(mac_str, sizeof(mac_str), "%s", addr.toString().c_str());
    for (int i = 0; mac_str[i]; i++) {
        if (mac_str[i] >= 'a' && mac_str[i] <= 'f') mac_str[i] -= 32;
    }

    server = NimBLEDevice::createServer();
    static ServerCallbacks serverCb;
    server->setCallbacks(&serverCb);

    // --- HID keyboard service ---
    hid_dev = new NimBLEHIDDevice(server);
    hid_dev->setReportMap((uint8_t*)HID_REPORT_MAP, sizeof(HID_REPORT_MAP));
    hid_dev->setManufacturer("Anthropic");
    // PnP ID: (vendorIdSource, vendorId, productId, version).
    // Source 1 = Bluetooth SIG, vendor 0x02E5 = Espressif. Originally claimed
    // Apple's USB vendor 0x05AC + Magic Keyboard product 0x820A — macOS
    // validates Apple-claimed HIDs against known device IDs and silently
    // refuses to surface a Connect button for spoofers.
    hid_dev->setPnp(0x01, 0x02E5, 0x0001, 0x0100);
    // country=33 (US ANSI). Setting this to 0 ("not supported") causes macOS
    // to launch the Keyboard Setup Assistant on first pair asking the user
    // to identify the layout — we only ever send Space / Shift+Tab so the
    // physical layout is irrelevant; advertise a known one to skip the wizard.
    hid_dev->setHidInfo(33, 0x02);
    hid_dev->setBatteryLevel(100);
    input_kbd = hid_dev->getInputReport(1);  // report ID 1

    // --- Custom data service ---
    NimBLEService* svc = server->createService(SERVICE_UUID);
    data_svc = svc;  // hashed by gatt_check_layout() once handles are assigned

    rx_char = svc->createCharacteristic(
        RX_CHAR_UUID,
        NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR
    );
    static RxCallbacks rxCb;
    rx_char->setCallbacks(&rxCb);

    tx_char = svc->createCharacteristic(
        TX_CHAR_UUID,
        NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::NOTIFY
    );

    req_char = svc->createCharacteristic(
        REQ_CHAR_UUID,
        NIMBLE_PROPERTY::NOTIFY
    );
    static ReqCallbacks reqCb;
    req_char->setCallbacks(&reqCb);

#if BOARD_HAS_SESSION_VIEWS
    // Session rows (write-only from the host). max_len raised above NimBLE's
    // 512-byte attribute default so a long write can fill the SS buffer.
    ss_char = svc->createCharacteristic(
        SS_CHAR_UUID,
        NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR,
        BLE_SS_BUF_SIZE
    );
    static SsCallbacks ssCb;
    ss_char->setCallbacks(&ssCb);
#endif

    svc->start();
    server->start();
    // After start() (handles now assigned), before we go on air: tell bonded
    // hosts to drop a cached attribute table if this image's layout moved.
    gatt_check_layout();
    start_advertising();

    Serial.printf("BLE: init complete, MAC=%s\n", mac_str);
}

void ble_tick(void) {
    if (need_advertise) {
        need_advertise = false;
        start_advertising();
    }
    // Deferred one-shot cache-recycle drop (see gatt_arm_link_recycle). Runs
    // before the param pushback below: the link is about to be replaced, so
    // renegotiating its timing first would be wasted, and onDisconnect clears
    // the pending pushback for this handle.
    if (gatt_recycle_handle != CONN_HANDLE_NONE &&
        (int32_t)(millis() - gatt_recycle_at_ms) >= 0) {
        uint16_t h = gatt_recycle_handle;
        gatt_recycle_handle = CONN_HANDLE_NONE;
        if (server && server->getConnectedCount() > 0) {
            Serial.println("BLE: cycling the link once so the host re-discovers the new attribute table");
            server->disconnect(h);
        }
    }
    // Deferred one-shot supervision-timeout pushback (see onConnParamsUpdate).
    if (param_fix_handle != CONN_HANDLE_NONE &&
        (int32_t)(millis() - param_fix_at_ms) >= 0) {
        uint16_t h = param_fix_handle;
        param_fix_handle = CONN_HANDLE_NONE;
        param_fix_spent  = h;
        if (server && server->getConnectedCount() > 0) {
            Serial.println("BLE: requesting 6s supervision timeout");
            server->updateConnParams(h, 12, 24, 0, DESIRED_TIMEOUT);
        }
    }
}

ble_state_t ble_get_state(void) {
    return state;
}

const char* ble_get_device_name(void) {
    return DEVICE_NAME;
}

const char* ble_get_mac_address(void) {
    return mac_str;
}

void ble_clear_bonds(void) {
    NimBLEDevice::deleteAllBonds();
    clear_owner();  // release ownership so the board can be handed to another machine
    Serial.println("BLE: bonds cleared");
    if (state == BLE_STATE_CONNECTED) {
        server->disconnect(server->getPeerInfo(0).getConnHandle());
    }
    need_advertise = true;
}

bool ble_has_bonds(void) {
    return NimBLEDevice::getNumBonds() > 0;
}

bool ble_has_data(void) {
    return data_ready;
}

const char* ble_get_data(void) {
    data_ready = false;
    return rx_buf;
}

#if BOARD_HAS_SESSION_VIEWS
bool ble_has_session_data(void) {
    return ss_ready;
}

const char* ble_get_session_data(void) {
    ss_ready = false;
    return ss_buf;
}
#else
// No characteristic, nothing to drain — the API stays total (boards/sim's
// ble_sim.cpp does the same) so main.cpp's gate is the only #if that matters.
bool ble_has_session_data(void) { return false; }
const char* ble_get_session_data(void) { return ""; }
#endif

void ble_send_ack(void) {
    if (state == BLE_STATE_CONNECTED && tx_char) {
        tx_char->setValue("{\"ack\":true}");
        tx_char->notify();
    }
}

void ble_send_nack(void) {
    if (state == BLE_STATE_CONNECTED && tx_char) {
        tx_char->setValue("{\"err\":true}");
        tx_char->notify();
    }
}

void ble_set_battery_level(int pct) {
    if (!hid_dev || pct < 0) return;
    if (pct > 100) pct = 100;
    hid_dev->setBatteryLevel((uint8_t)pct, state == BLE_STATE_CONNECTED);
}

void ble_request_refresh(void) {
    if (state == BLE_STATE_CONNECTED && req_char) {
        uint8_t v = 0x01;
        req_char->setValue(&v, 1);
        req_char->notify();
        Serial.println("BLE: refresh requested");
    }
}

void ble_keyboard_press(uint8_t key, uint8_t modifier) {
    if (state != BLE_STATE_CONNECTED || !input_kbd) return;
    // HID report: [modifier, reserved, key1, key2, key3, key4, key5, key6]
    uint8_t report[8] = {modifier, 0, key, 0, 0, 0, 0, 0};
    input_kbd->setValue(report, sizeof(report));
    input_kbd->notify();
}

void ble_keyboard_release(void) {
    if (state != BLE_STATE_CONNECTED || !input_kbd) return;
    uint8_t report[8] = {0};
    input_kbd->setValue(report, sizeof(report));
    input_kbd->notify();
}
