# Wireless Serial Transport

Bluetooth SPP (Serial Port Profile) transport from ESP32 anchors to Orin Nano.
No USB cables needed — ESP32s run on battery/wall power and communicate wirelessly.

## Why Bluetooth, not WiFi?

ESP32 anchors use WiFi in **promiscuous mode** to capture CSI (Channel State
Information) from the target AP. If we also used WiFi for data transport, the
single 2.4GHz radio would time-share between CSI capture and data upload,
causing **packet loss on CSI measurements**.

Bluetooth Classic SPP coexists with WiFi via ESP32's hardware coexistence
controller. The radio time-shares between WiFi RX (CSI capture) and BT TX
(data upload) with minimal interference — ESP-IDF's `SW_COEXIST_PREFERENCE_BALANCE`
mode handles this automatically.

## Architecture

```
ESP32 anchor_00 ──BT SPP──┐
ESP32 anchor_01 ──BT SPP──┤
ESP32 anchor_02 ──BT SPP──├──> Orin Nano (bt_receiver.py)
ESP32 anchor_03 ──BT SPP──┘         │
                                     ├─> SQLite database
                                     └─> Commands back to ESP32s
```

## Frame Protocol

Binary framed protocol over BT SPP (see `protocol.py`):

```
[0xAA][0x55][LEN_H][LEN_L][TYPE][PAYLOAD...][CRC8]
  sync word    payload len   type  variable   checksum
```

| Packet Type | Code | Direction | Description |
|---|---|---|---|
| CSI_DATA | 0x01 | ESP32 → Orin | Full CSI measurement |
| RSSI_DATA | 0x02 | ESP32 → Orin | RSSI-only (lightweight) |
| STATUS | 0x03 | ESP32 → Orin | Device diagnostics |
| CMD_ACK | 0x04 | ESP32 → Orin | Command acknowledgement |
| HEARTBEAT | 0x05 | ESP32 → Orin | Keep-alive (every 5s) |
| CMD_START | 0x11 | Orin → ESP32 | Start CSI capture |
| CMD_STOP | 0x12 | Orin → ESP32 | Stop CSI capture |
| CMD_SET_CHANNEL | 0x13 | Orin → ESP32 | Change WiFi channel |
| CMD_GET_STATUS | 0x14 | Orin → ESP32 | Request status |

## Directory Layout

```
wireless_serial/
├── README.md
├── protocol.py                 # Shared protocol definitions (Python)
├── esp32_bt_serial/
│   ├── CMakeLists.txt          # ESP-IDF project file
│   ├── sdkconfig.defaults      # Build config (BT + WiFi CSI + coexistence)
│   └── main/
│       ├── CMakeLists.txt
│       └── main.c              # ESP-IDF firmware: WiFi CSI + BT SPP
└── orin_receiver/
    ├── bt_receiver.py          # Discover, connect, receive, store to SQLite
    └── bt_commander.py         # One-shot commands to ESP32 anchors
```

## Setup

### 1. Flash ESP32s

```bash
source ~/esp-idf/export.sh
cd wireless_serial/esp32_bt_serial

# Edit sdkconfig.defaults for each device:
#   CONFIG_WILOC_DEVICE_ID="anchor_00"  (change per device)
#   CONFIG_WILOC_WIFI_CHANNEL=6

idf.py set-target esp32
idf.py build
idf.py -p /dev/ttyUSB0 flash
# Repeat for each ESP32 with different DEVICE_ID
```

**Important**: Use original ESP32 (not S2/S3/C3) — only original has Classic BT.

### 2. Install Python dependencies on Orin Nano

```bash
sudo apt-get install bluetooth libbluetooth-dev
pip install pybluez
```

### 3. Discover and connect

```bash
cd wireless_serial/orin_receiver

# Scan for ESP32 anchors
python bt_receiver.py --discover

# Connect to all and start receiving
python bt_receiver.py --connect-all --db ../../data/session.db

# Interactive commands:
#   start       — start CSI capture on all anchors
#   stop        — stop capture
#   status      — request status from all
#   channel 6   — set WiFi channel
#   quit        — disconnect and exit
```

### 4. One-shot commands

```bash
python bt_commander.py --all --cmd start
python bt_commander.py --target anchor_00 --cmd "channel 11"
python bt_commander.py --all --cmd status
```

## Throughput

- BT SPP: ~2-3 Mbps practical throughput
- CSI packet: ~130 bytes per frame (52 subcarriers x 2 bytes + overhead)
- At 100 CSI packets/sec per anchor: ~13 KB/s per anchor
- 4 anchors: ~52 KB/s total — well within BT SPP capacity

## Troubleshooting

- **"No WiLoc devices found"**: Check ESP32 is powered. Run `hcitool scan`.
- **Connection drops**: Receiver auto-reconnects. Check ESP32 serial monitor.
- **CSI packet loss**: Run `status` — if `free_heap` is low, reduce CSI rate.
