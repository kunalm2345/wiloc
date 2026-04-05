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

### Status: anchor_03 working, need to reflash anchor_00/01/02 with IDF master
