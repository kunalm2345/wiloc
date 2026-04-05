#!/usr/bin/env bash
# ============================================================================
# WiLoc — Orin Nano Full Setup
# Run this on your Jetson Orin Nano (JetPack 6.x / Ubuntu 22.04)
#
# This sets up:
#   1. System dependencies
#   2. Python environment + WiLoc packages
#   3. ESP-IDF toolchain (for compiling ESP32 firmware)
#   4. Bluetooth serial tools (for wireless ESP32 communication)
#   5. NTP server (so all devices sync time)
#
# Usage:
#   chmod +x orin_nano_setup.sh
#   ./orin_nano_setup.sh
# ============================================================================

set -euo pipefail

WILOC_DIR="$HOME/wiloc"
ESP_IDF_DIR="$HOME/esp-idf"
VENV_DIR="$WILOC_DIR/.venv"

echo "============================================"
echo "WiLoc — Orin Nano Setup"
echo "============================================"
echo ""

# ── 1. System packages ──
echo "[1/6] Installing system dependencies..."
sudo apt-get update
sudo apt-get install -y \
    python3 python3-pip python3-venv \
    git wget curl \
    build-essential cmake ninja-build \
    libffi-dev libssl-dev \
    bluez bluetooth libbluetooth-dev \
    ntp ntpdate \
    screen minicom \
    libusb-1.0-0-dev \
    sqlite3 \
    net-tools

echo "  Done."

# ── 2. Python environment ──
echo ""
echo "[2/6] Setting up Python environment..."

if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

pip install --upgrade pip

pip install \
    numpy scipy scikit-learn \
    matplotlib plotly \
    pyserial pyyaml pandas \
    bleak          # BLE library for Python (cross-platform)

# PyTorch for Jetson (NVIDIA provides JetPack-specific wheels)
# Check if torch is already installed (JetPack often includes it)
if ! python3 -c "import torch" 2>/dev/null; then
    echo "  Installing PyTorch for Jetson..."
    # JetPack 6.x / L4T r36 uses these wheels
    pip install --no-cache-dir \
        torch torchvision torchaudio \
        --index-url https://developer.download.nvidia.com/compute/redist/jp/v60/pytorch/
    echo "  If the above fails, install PyTorch manually from:"
    echo "  https://forums.developer.nvidia.com/t/pytorch-for-jetson/"
fi

pip install gymnasium

echo "  Python environment ready at $VENV_DIR"

# ── 3. ESP-IDF (for flashing ESP32s) ──
echo ""
echo "[3/6] Setting up ESP-IDF toolchain..."

if [ ! -d "$ESP_IDF_DIR" ]; then
    echo "  Cloning ESP-IDF v5.3..."
    git clone --recursive --branch v5.3 \
        https://github.com/espressif/esp-idf.git "$ESP_IDF_DIR"
    cd "$ESP_IDF_DIR"
    ./install.sh esp32
    echo "  ESP-IDF installed."
else
    echo "  ESP-IDF already exists at $ESP_IDF_DIR"
fi

echo "  To activate: source $ESP_IDF_DIR/export.sh"

# ── 4. USB serial permissions ──
echo ""
echo "[4/6] Configuring USB serial permissions..."

# Add user to dialout group (for /dev/ttyUSB* access)
sudo usermod -aG dialout "$USER"

# udev rule for ESP32 USB (CP210x and CH340 chips)
sudo tee /etc/udev/rules.d/99-esp32.rules > /dev/null << 'UDEV'
# CP210x (most ESP32 dev boards)
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", MODE="0666", SYMLINK+="esp32_%n"
# CH340 (some cheaper ESP32 boards)
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", MODE="0666", SYMLINK+="esp32_%n"
UDEV

sudo udevadm control --reload-rules
echo "  USB serial permissions configured."
echo "  NOTE: Log out and back in for group changes to take effect."

# ── 5. Bluetooth setup ──
echo ""
echo "[5/6] Configuring Bluetooth..."

# Enable and start bluetooth service
sudo systemctl enable bluetooth
sudo systemctl start bluetooth

# Make discoverable
sudo hciconfig hci0 up 2>/dev/null || echo "  No Bluetooth adapter found (OK if using USB dongle later)"
sudo hciconfig hci0 piscan 2>/dev/null || true

echo "  Bluetooth service running."

# ── 6. NTP server (for time sync across all devices) ──
echo ""
echo "[6/6] Configuring NTP server..."

# Configure Orin Nano as local NTP server so ESP32s/RPis can sync
sudo tee /etc/ntp.conf > /dev/null << 'NTP'
# NTP server config for WiLoc
# Sync to internet time servers
server 0.ubuntu.pool.ntp.org iburst
server 1.ubuntu.pool.ntp.org iburst

# Serve time to local network
restrict 192.168.0.0 mask 255.255.0.0 nomodify notrap
restrict 10.0.0.0 mask 255.0.0.0 nomodify notrap

# Local clock as fallback
server 127.127.1.0
fudge 127.127.1.0 stratum 10
NTP

sudo systemctl restart ntp 2>/dev/null || sudo systemctl restart ntpd 2>/dev/null || \
    echo "  NTP service not found — install with: sudo apt install ntp"

echo "  NTP server configured."

# ── Summary ──
echo ""
echo "============================================"
echo "Setup complete!"
echo "============================================"
echo ""
echo "Next steps:"
echo ""
echo "  1. Activate the environment:"
echo "     source $VENV_DIR/bin/activate"
echo ""
echo "  2. Flash ESP32s (plug in via USB one at a time):"
echo "     source $ESP_IDF_DIR/export.sh"
echo "     cd $WILOC_DIR/firmware/esp32_csi"
echo "     idf.py set-target esp32"
echo "     idf.py build"
echo "     idf.py -p /dev/ttyUSB0 flash"
echo ""
echo "  3. Run synthetic evaluation (no hardware needed):"
echo "     cd $WILOC_DIR/evaluation"
echo "     python run_synthetic_eval.py"
echo ""
echo "  4. View your room:"
echo "     python view_room.py --ap 2.0 2.0 1.0"
echo ""
echo "  5. Once ESP32s are flashed with BT serial firmware:"
echo "     See wireless_serial/ folder for Bluetooth connectivity."
echo ""
