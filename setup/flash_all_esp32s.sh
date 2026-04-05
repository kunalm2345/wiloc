#!/usr/bin/env bash
# ============================================================================
# Flash all 5 ESP32s — plug them in one at a time via USB
#
# This flashes:
#   - 4x anchor firmware (CSI receiver + Bluetooth serial)
#   - 1x AP firmware (soft AP beacon broadcaster)
#
# Usage:
#   source ~/esp-idf/export.sh
#   ./flash_all_esp32s.sh
# ============================================================================

set -euo pipefail

WILOC_DIR="$HOME/wiloc"
FIRMWARE_DIR="$WILOC_DIR/firmware/esp32_csi"

echo "============================================"
echo "ESP32 Flashing Script"
echo "============================================"
echo ""
echo "You'll flash 5 ESP32s total:"
echo "  - 4 anchors (CSI receivers + BT serial)"
echo "  - 1 target AP (beacon broadcaster)"
echo ""

# Check ESP-IDF is sourced
if ! command -v idf.py &> /dev/null; then
    echo "ERROR: ESP-IDF not in PATH. Run:"
    echo "  source ~/esp-idf/export.sh"
    exit 1
fi

cd "$FIRMWARE_DIR"

flash_device() {
    local role="$1"
    local device_id="$2"
    local port="${3:-/dev/ttyUSB0}"

    echo ""
    echo "--- Flashing: $role ($device_id) ---"
    echo "Plug in the ESP32 via USB and press Enter..."
    read -r

    # Detect port
    if [ ! -e "$port" ]; then
        echo "Port $port not found. Available ports:"
        ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || echo "  None found!"
        echo "Enter port path:"
        read -r port
    fi

    echo "Flashing to $port..."

    # Set device ID and role via sdkconfig
    # (These get compiled into the firmware as build-time constants)
    echo "CONFIG_WILOC_DEVICE_ID=\"$device_id\"" >> sdkconfig.defaults
    echo "CONFIG_WILOC_DEVICE_ROLE=\"$role\"" >> sdkconfig.defaults

    idf.py -p "$port" flash

    # Remove override
    sed -i '/CONFIG_WILOC_DEVICE/d' sdkconfig.defaults

    echo "$role ($device_id) flashed successfully!"
    echo "Unplug this ESP32 and label it: $device_id"
    echo ""
}

# Flash anchors
for i in 0 1 2 3; do
    flash_device "anchor" "anchor_0${i}"
done

# Flash AP
flash_device "ap" "target_ap"

echo ""
echo "============================================"
echo "All ESP32s flashed!"
echo "============================================"
echo ""
echo "Placement:"
echo "  anchor_00 → corner (0, 0)    — bottom-left"
echo "  anchor_01 → corner (W, 0)    — bottom-right"
echo "  anchor_02 → corner (W, D)    — top-right"
echo "  anchor_03 → corner (0, D)    — top-left"
echo "  target_ap → wherever you want to localize"
echo ""
echo "All at the same height (~1.2m recommended)."
