# WiLoc — WiFi Indoor Localization
# Usage: just <command>

db := justfile_directory() / "wiloc_ble.db"
venv := "/tmp/wiloc-venv"
python := venv / "bin/python3"

# List available commands
default:
    @just --list

# Start the dashboard (http://localhost:8050)
dashboard port="8050":
    {{python}} dashboard/server.py --db {{db}} --port {{port}}

# Connect to all BLE anchors and start receiving CSI
connect:
    {{python}} wireless_serial/orin_receiver/bt_receiver.py --connect-all --db {{db}}

# Scan for WiLoc BLE devices
discover:
    {{python}} wireless_serial/orin_receiver/bt_receiver.py --discover

# Send a command to all anchors (e.g., just cmd start)
cmd command:
    {{python}} wireless_serial/orin_receiver/bt_commander.py --all --cmd "{{command}}"

# Show DB stats
stats:
    @echo "Database: {{db}}"
    @sqlite3 {{db}} "SELECT COUNT(*) || ' total packets' FROM csi_readings;"
    @sqlite3 {{db}} "SELECT anchor_id, COUNT(*) as pkts, ROUND(AVG(rssi),1) as avg_rssi FROM csi_readings GROUP BY anchor_id;"
    @sqlite3 {{db}} "SELECT 'Last packet: ' || MAX(collected_at) FROM csi_readings;"

# Install Python dependencies
setup:
    python3 -m venv {{venv}}
    {{venv}}/bin/pip install bleak dash plotly numpy scipy scikit-learn pyserial pyyaml

# Flash an ESP32 (auto-detects device from MAC + setup.json)
flash *args:
    source ~/esp-idf/export.sh 2>/dev/null && {{python}} setup/flash.py {{args}}

# View room in browser
view-room *args:
    {{python}} evaluation/view_room.py {{args}}
