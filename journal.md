# WiLoc Development Journal

## 2026-04-06

### 17:00 — ESP32 chip identification

Connected 2 ESP32 dev boards to Mac via USB.

**Ports detected:**
- `/dev/tty.usbmodem101` — MAC: `3c:dc:75:99:07:f0`
- `/dev/tty.usbmodem1101` — MAC: `3c:dc:75:9b:d2:88`

**Chip: ESP32-C5 (revision v1.0)**
- Wi-Fi 6 (dual-band 2.4GHz + 5GHz)
- BT 5 (LE only) — NO Classic Bluetooth
- IEEE802.15.4 (Zigbee/Thread)
- Single Core + LP Core, 240MHz, RISC-V
- Crystal: 48MHz
- USB mode: USB-Serial/JTAG (built-in, no external UART chip)

**Impact on firmware:**
- Original firmware used Classic BT SPP (Serial Port Profile) — **won't work on C5**
- Must rewrite to use **BLE (GATT)** for wireless serial transport
- BLE 5 on C5 supports 2 Mbps PHY, practical ~800 Kbps — more than enough
  (our data is ~13 KB/s per anchor = ~104 kbps)
- Need ESP-IDF v5.4+ (C5 support added in v5.4)
- On the receiver side: switch from `pybluez` (Classic BT) to `bleak` (BLE, cross-platform)

**WiFi 6 CSI advantage:**
- ESP32-C5 supports 802.11ax (WiFi 6) which has OFDMA and more subcarriers
- Dual-band means we could potentially use 5GHz for CSI (less interference)
- CSI support on C5 needs verification in ESP-IDF docs

### 17:05 — Installing ESP-IDF v5.4

Started ESP-IDF installation for ESP32-C5 support. Using v5.4 branch.
Running on macOS (Apple Silicon M-series).

### 17:10 — Firmware rewrite plan

Switching from Classic BT SPP to BLE GATT:
- Use NimBLE stack (lighter than Bluedroid for BLE-only chips)
- GATT service with UUID for WiLoc
- TX characteristic (notify): ESP32 → receiver (CSI frames)
- RX characteristic (write): receiver → ESP32 (commands)
- Same binary frame protocol: `[0xAA][0x55][LEN_H][LEN_L][TYPE][PAYLOAD][CRC8]`
- On receiver: use `bleak` Python library (pip install, works everywhere)

### 17:30 — ESP-IDF setup issues

- ESP-IDF v5.4 cloned to `~/esp-idf`
- System Python is 3.9.6 (Apple shipped), ESP-IDF venv created with it had broken deps
- Fixed by recreating venv with Homebrew Python 3.12 + symlinking:
  `ln -s idf5.4_py3.12_env idf5.4_py3.9_env`
- ESP32-C5 is "preview" in v5.4 — need `idf.py --preview` flag for all commands

### 17:40 — Firmware build issues (ESP32-C5 API differences)

ESP32-C5 has different CSI and rx_ctrl structs than classic ESP32:

1. **`wifi_csi_config_t`** — on C5, this is `wifi_csi_acquire_config_t` with fields:
   `enable`, `acquire_csi_legacy`, `acquire_csi_force_lltf`, `acquire_csi_ht20`,
   `acquire_csi_ht40`, `acquire_csi_vht`, `acquire_csi_su`, `acquire_csi_mu`,
   `acquire_csi_dcm`, `acquire_csi_beamformed`, `acquire_csi_he_stbc_mode`, `val_scale_cfg`
   (old ESP32 had: `lltf_en`, `htltf_en`, `stbc_htltf2_en`, etc.)

2. **`wifi_pkt_rx_ctrl_t`** — on C5, no `.cwb` field. Use `.second` (secondary channel)

3. **`strncpy` warnings** — GCC 14 (RISC-V toolchain) treats stringop-truncation as error.
   Fixed by switching to `memcpy` with explicit length

4. **Partition table** — BLE + WiFi binary is ~1MB, exceeds default 1MB partition.
   Created custom `partitions.csv` with ~2MB app partition

5. **Chip revision mismatch** — boards are v1.0, IDF v5.4 targets v0.x.
   Used `--force` flag with esptool to flash anyway

### 17:55 — Both ESP32s flashed successfully

- `/dev/tty.usbmodem101` (MAC `3c:dc:75:99:07:f0`) → **anchor_00**
- `/dev/tty.usbmodem1101` (MAC `3c:dc:75:9b:d2:88`) → **anchor_01**

Both running: WiFi promiscuous mode (ch6, CSI capture) + BLE GATT server
- BLE service UUID: 0xFFE0
- TX char (notify): 0xFFE1
- RX char (write): 0xFFE2
- Device names: `WiLoc_anchor_00`, `WiLoc_anchor_01`

**Next**: Unplug USB, power from USB adapter, verify BLE shows up from Mac/Orin

### 18:30 — Boot failure diagnosis

All 4 boards boot-looped after flashing with ESP-IDF v5.4:
```
E (74) boot_comm: Image requires chip rev <= v0.99, but chip is v1.0
```
- `--force` in esptool bypassed the flash-time check, but the bootloader itself
  still rejected the app at runtime
- Patching `ESP32C5_REV_MAX_FULL` from 99→199 in Kconfig fixed the bootloader check
- But the firmware STILL crashed (WDT reset after coexist init) — the NimBLE/WiFi
  stack in v5.4 doesn't properly support C5 rev 1.0

### 18:45 — Fixed: ESP-IDF master branch

Switched from ESP-IDF v5.4 tag to master (HEAD):
- Master branch has native C5 rev 1.0 support (`ESP32C5_REV_MIN_100`)
- Build + flash succeeded
- Serial output shows: WiFi promiscuous CSI enabled, BLE advertising, no crashes
- BLE scan from Mac confirms: `WiLoc_anchor_03` visible at RSSI -43dBm

Also added CMake flag for device ID: `idf.py build -DWILOC_DEVICE_ID=anchor_03`
(the old sdkconfig.defaults approach didn't work because it's a C #define not a Kconfig)

### 19:00 — ID truncation fix

Device IDs `anchor_00`..`anchor_03` (9 chars) truncated to `anchor_0` in the
8-byte protocol field — all 4 devices looked identical. Shortened to `anc_00`..`anc_03`
(6 chars). Also added CMake `-DWILOC_DEVICE_ID=anc_XX` flag for per-device builds.

### 19:15 — All 4 anchors reflashed and verified

All built with ESP-IDF master + correct short IDs. Verified via serial + BLE:

| ID     | MAC                 | BLE Name       | Status |
|--------|---------------------|----------------|--------|
| anc_00 | 3c:dc:75:99:07:f0   | WiLoc_anc_00   | OK     |
| anc_01 | 3c:dc:75:9b:d2:88   | WiLoc_anc_01   | OK     |
| anc_02 | 3c:dc:75:9d:4e:3c   | WiLoc_anc_02   | OK     |
| anc_03 | 3c:dc:75:9d:4e:68   | WiLoc_anc_03   | OK     |

BLE CSI streaming confirmed: 18.5 packets/sec, 53 subcarriers, zero parse errors.

### Key learnings
- ESP32-C5 rev 1.0 requires ESP-IDF master (v5.4 only supports rev 0.x)
- NimBLE on IDF master advertises as "nimble" not the custom name — match by service UUID
- Protocol 8-byte ID field means device names must be <= 7 chars (+ null)

### ~03:30 IST — Timezone mismatch discovered

Dashboard showed "stale (22000s ago)" for data that was actually ~45 min old.
- Root cause: old receiver wrote timestamps with `datetime.utcnow()` (UTC)
- Dashboard compared with `datetime.now()` (IST = UTC+5:30)
- 5.5h offset made everything look 6 hours stale
- Fix: dashboard liveness now uses UTC consistently for age comparison
- All 159K packets were from a single 18-min session (21:15-21:33 UTC = 02:45-03:03 IST)

### ~03:45 IST — CSI not flowing in new sessions

Receiver connects to all 4 anchors via BLE. But only HEARTBEAT packets (type 0x05)
arrive — zero CSI_DATA packets (type 0x01).

ESP32 serial log shows `GATT notify` at ~20/sec — the firmware IS sending data.
But receiver only parses heartbeats. Likely cause: CSI packets are arriving as
BLE notifications but the frame parser isn't reassembling them, or they're a
different packet format than expected. Need more verbose debug logging to confirm.

## 2026-04-07

### ~04:30 IST — 5th ESP32 configured as localization target

New ESP32-C5 (MAC `d0:cf:13:e0:00:c4`) set up as the target to be localized.

**Design**: WiFi SoftAP mode broadcasting beacons on channel 6.
- SSID: `WiLoc_Target`
- AP MAC: `d0:cf:13:e0:00:c5` (AP MAC is base_mac + 1 on ESP32)
- Beacon interval: 100ms (10 beacons/sec)
- TX power: 20 dBm (maximum)
- BLE GATT: `WiLoc_tgt` for debug/control (start/stop/channel/status)

**How it works**: The target does NOT connect to the anchors. It IS a hotspot.
The 4 anchors are in promiscuous mode on channel 6 — they sniff ALL WiFi frames
on that channel and extract CSI. The target's beacons are just more WiFi frames
for the anchors to capture. Filter by AP MAC `d0:cf:13:e0:00:c5` on the
dashboard to see only the target's data.

**Dashboard target healthcheck added**: Shows whether the target is being seen
by the anchors — LIVE (3+ anchors, good rate), PARTIAL, STALE, or NOT SEEN.
Includes per-anchor rate breakdown and total packet count.

### ~04:45 IST — DB locking + path issues fixed

**Problem 1**: `sqlite3.OperationalError: database is locked` — receiver and dashboard
both writing/reading the same DB without WAL mode.
- Fix: `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout=5000` on all connections
- Also wrapped commit in try/except in BLE callback so locked DB doesn't crash receiver

**Problem 2**: "Empty DB" — dashboard and receiver used relative `wiloc_ble.db` path,
so running from different directories created separate empty DBs.
- Fix: both now resolve DB path relative to project root via `Path(__file__)`
- Added Justfile so commands always run from project root:
  `just dashboard`, `just connect`, `just discover`, `just stats`

### ~05:30 IST — Target ESP32 not visible to anchors

Target SoftAP was broadcasting beacons on ch6 but anchors captured zero CSI from it.
All router MACs visible, target MAC `d0:cf:13:e0:00:c5` absent.

**Root cause**: ESP32-C5 CSI callback only fires for **data frames** (HT-LTF preamble),
not **management frames** (beacons/probes). SoftAP beacons are management frames.

**Fix**: Rewrote target to use **ESP-NOW** (sends data frames) instead of SoftAP beacons.
- ESP-NOW broadcast at 10 packets/sec to `ff:ff:ff:ff:ff:ff`
- Critical: `esp_now_set_peer_rate_config()` with `WIFI_PHY_RATE_MCS0_LGI` + `WIFI_PHY_MODE_HT20`
  — this makes ESP-NOW use HT preamble, which anchors' `acquire_csi_ht20=true` captures
- Without the rate config, ESP-NOW used legacy rate with no HT-LTF → no CSI

**Also fixed anchor CSI config** to match Espressif's esp-csi example for C5:
- `acquire_csi_legacy=0` (was 1), `acquire_csi_force_lltf=0`
- `acquire_csi_su/mu/dcm/beamformed=0` (were all 1)
- `acquire_csi_he_stbc_mode=2`, `val_scale_cfg=0`

Target MAC changed from `d0:cf:13:e0:00:c5` (AP) to `d0:cf:13:e0:00:c4` (STA)
since target now runs in STA mode with ESP-NOW.

Result: all 4 anchors receiving target CSI at ~10 pkt/s/anchor. RSSI varies
by anchor (-23 to -52 dBm) reflecting different distances.

### ~06:00 IST — Dashboard rewrite: Dash → FastAPI + HTMX + Chart.js

Old Dash/Plotly dashboard returned valid JSON (confirmed via curl) but frontend
never rendered it — suspected React/Plotly rendering bug with large 3D scenes.

Rewrote as FastAPI + Jinja2 templates + Chart.js + raw Canvas 2D:
- `dashboard/server.py` — FastAPI backend with JSON API endpoints
- `dashboard/templates/index.html` — single-page HTML with HTMX polling
- Room 2D top-down canvas with anchors, AP position, distance lines
- RSSI timeline (Chart.js line), CSI waterfall (Canvas 2D heatmap)
- Target/anchor status cards, position estimate
- Settings modal: room dimensions + anchor positions, saved to settings.json
- `just dashboard` / `just flash` commands in Justfile

### ~06:30 IST — Localization approach assessment

RSSI trilateration works but accuracy is poor without tedious calibration
(path loss exponent, reference distance per room). Evaluated alternatives:

| Approach | How it works | Accuracy | Calibration |
|---|---|---|---|
| **RSSI trilateration** (current) | RSSI → distance → least-squares | ~2-5m | heavy |
| **CSI phase ranging** | phase slope across 53 subcarriers → ToF → distance | 20-50cm | none |
| **CSI fingerprinting** | collect CSI at grid, match via k-NN/ML | 10-30cm | grid walk |
| **Deep learning on CSI** | CNN/LSTM on raw CSI → position | 5-20cm | training data |

**AoA not feasible**: ESP32-C5 has single antenna. AoA needs antenna array.
BLE 5.1 AoA/AoD is supported on C5 but for BLE, not WiFi CSI.

**Decision**: Implement CSI phase ranging first (no calibration, uses existing data),
then fingerprinting later for better accuracy.

### Key sources
- [Espressif esp-csi sender/receiver](https://deepwiki.com/espressif/esp-csi/3.1-csi-sender-and-receiver-setup)
- [ESP-IDF WiFi CSI docs](https://docs.espressif.com/projects/esp-idf/en/stable/esp32c5/api-guides/wifi.html)
