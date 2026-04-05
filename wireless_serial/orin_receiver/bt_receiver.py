"""
Bluetooth SPP Receiver — runs on Orin Nano, connects to all ESP32 anchors.

Discovers WiLoc ESP32 anchors via Bluetooth, connects over SPP,
receives CSI/RSSI frames, and stores to SQLite.

Usage:
    python bt_receiver.py --discover              # scan for WiLoc devices
    python bt_receiver.py --connect-all           # connect to all found devices
    python bt_receiver.py --connect-all --db data.db
"""

import argparse
import json
import socket
import sqlite3
import struct
import threading
import time
from datetime import datetime
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from protocol import (
    PacketType, decode_frame, decode_csi_payload, decode_rssi_payload,
    decode_status_payload, decode_heartbeat_payload,
    encode_frame,
)

# Bluetooth SPP UUID
SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"

# WiLoc device name prefix
WILOC_PREFIX = "WiLoc_"


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS csi_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_ms INTEGER NOT NULL,
            anchor_id TEXT NOT NULL,
            target_mac TEXT,
            rssi INTEGER,
            channel INTEGER,
            bandwidth INTEGER,
            csi_len INTEGER,
            csi_raw TEXT,
            label_x REAL,
            label_y REAL,
            label_z REAL,
            collected_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON csi_readings(timestamp_ms)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_anchor ON csi_readings(anchor_id)")
    conn.commit()
    return conn


def discover_devices() -> list[dict]:
    """Scan for WiLoc ESP32 devices using classic BT discovery."""
    import bluetooth

    print("Scanning for Bluetooth devices (10s)...")
    nearby = bluetooth.discover_devices(duration=10, lookup_names=True,
                                         lookup_class=True, flush_cache=True)

    wiloc_devices = []
    for addr, name, dev_class in nearby:
        if name and name.startswith(WILOC_PREFIX):
            anchor_id = name.replace(WILOC_PREFIX, "")
            wiloc_devices.append({
                "address": addr,
                "name": name,
                "anchor_id": anchor_id,
                "class": dev_class,
            })
            print(f"  Found: {name} ({addr}) — anchor_id={anchor_id}")

    if not wiloc_devices:
        print("  No WiLoc devices found. Make sure ESP32s are powered and flashed.")
    else:
        print(f"\nFound {len(wiloc_devices)} WiLoc device(s).")

    return wiloc_devices


class AnchorConnection(threading.Thread):
    """Manages a single BT SPP connection to one ESP32 anchor."""

    def __init__(self, address: str, name: str, anchor_id: str,
                 db_conn: sqlite3.Connection, db_lock: threading.Lock):
        super().__init__(daemon=True)
        self.address = address
        self.name = name
        self.anchor_id = anchor_id
        self.db_conn = db_conn
        self.db_lock = db_lock
        self.sock = None
        self.running = False
        self.connected = False
        self.packets_received = 0
        self.last_heartbeat = 0
        self._rx_buf = bytearray()

    def connect(self) -> bool:
        """Establish BT SPP connection."""
        import bluetooth

        try:
            print(f"  [{self.anchor_id}] Connecting to {self.address}...")

            # Find SPP service
            services = bluetooth.find_service(uuid=SPP_UUID, address=self.address)
            if not services:
                # Fallback: try channel 1 (most SPP servers use channel 1)
                port = 1
            else:
                port = services[0]["port"]

            self.sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
            self.sock.connect((self.address, port))
            self.sock.settimeout(2.0)
            self.connected = True
            print(f"  [{self.anchor_id}] Connected on RFCOMM channel {port}")
            return True

        except Exception as e:
            print(f"  [{self.anchor_id}] Connection failed: {e}")
            self.connected = False
            return False

    def disconnect(self):
        self.running = False
        self.connected = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

    def send_command(self, ptype: int, payload: bytes = b""):
        """Send a command frame to this anchor."""
        if not self.connected or not self.sock:
            print(f"  [{self.anchor_id}] Not connected, can't send command")
            return
        frame = encode_frame(ptype, payload)
        try:
            self.sock.send(frame)
        except Exception as e:
            print(f"  [{self.anchor_id}] Send failed: {e}")

    def run(self):
        """Main receive loop."""
        self.running = True
        commit_counter = 0

        while self.running:
            if not self.connected:
                # Reconnect
                print(f"  [{self.anchor_id}] Reconnecting in 3s...")
                time.sleep(3)
                if not self.connect():
                    continue

            try:
                data = self.sock.recv(1024)
                if not data:
                    self.connected = False
                    continue
                self._rx_buf.extend(data)
            except socket.timeout:
                continue
            except Exception as e:
                print(f"  [{self.anchor_id}] Recv error: {e}")
                self.connected = False
                continue

            # Decode all complete frames
            while True:
                result = decode_frame(self._rx_buf)
                if result is None:
                    break

                ptype, payload, _ = result
                self.packets_received += 1
                self._handle_packet(ptype, payload)

                commit_counter += 1
                if commit_counter >= 50:
                    with self.db_lock:
                        self.db_conn.commit()
                    commit_counter = 0

        # Final commit
        with self.db_lock:
            self.db_conn.commit()

    def _handle_packet(self, ptype: int, payload: bytes):
        now = datetime.utcnow().isoformat()

        if ptype == PacketType.CSI_DATA:
            try:
                d = decode_csi_payload(payload)
                with self.db_lock:
                    self.db_conn.execute(
                        """INSERT INTO csi_readings
                           (timestamp_ms, anchor_id, target_mac, rssi, channel,
                            bandwidth, csi_len, csi_raw, collected_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (d["timestamp_ms"], d["anchor_id"], d["target_mac"],
                         d["rssi"], d["channel"], d["bandwidth"], d["csi_len"],
                         json.dumps(d["csi_raw"]), now),
                    )
            except Exception as e:
                print(f"  [{self.anchor_id}] CSI parse error: {e}")

        elif ptype == PacketType.RSSI_DATA:
            try:
                d = decode_rssi_payload(payload)
                with self.db_lock:
                    self.db_conn.execute(
                        """INSERT INTO csi_readings
                           (timestamp_ms, anchor_id, target_mac, rssi, channel,
                            collected_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (d["timestamp_ms"], d["anchor_id"], d["target_mac"],
                         d["rssi"], d["channel"], now),
                    )
            except Exception as e:
                print(f"  [{self.anchor_id}] RSSI parse error: {e}")

        elif ptype == PacketType.HEARTBEAT:
            try:
                d = decode_heartbeat_payload(payload)
                self.last_heartbeat = time.time()
            except Exception:
                pass

        elif ptype == PacketType.STATUS:
            try:
                d = decode_status_payload(payload)
                print(f"  [{self.anchor_id}] Status: uptime={d['uptime_s']}s "
                      f"heap={d['free_heap']} ch={d['wifi_channel']}")
            except Exception as e:
                print(f"  [{self.anchor_id}] Status parse error: {e}")

        elif ptype == PacketType.CMD_ACK:
            msg = payload.decode("utf-8", errors="replace")
            print(f"  [{self.anchor_id}] ACK: {msg}")


class Receiver:
    """Manages connections to all ESP32 anchors."""

    def __init__(self, db_path: str):
        self.db_conn = init_db(db_path)
        self.db_lock = threading.Lock()
        self.connections: dict[str, AnchorConnection] = {}

    def connect_all(self, devices: list[dict]):
        """Connect to all discovered devices."""
        for dev in devices:
            conn = AnchorConnection(
                address=dev["address"],
                name=dev["name"],
                anchor_id=dev["anchor_id"],
                db_conn=self.db_conn,
                db_lock=self.db_lock,
            )
            if conn.connect():
                conn.start()
                self.connections[dev["anchor_id"]] = conn

        print(f"\nConnected to {len(self.connections)}/{len(devices)} anchors.")

    def send_command_all(self, ptype: int, payload: bytes = b""):
        """Send command to all connected anchors."""
        for aid, conn in self.connections.items():
            conn.send_command(ptype, payload)

    def send_command(self, anchor_id: str, ptype: int, payload: bytes = b""):
        """Send command to specific anchor."""
        if anchor_id in self.connections:
            self.connections[anchor_id].send_command(ptype, payload)
        else:
            print(f"Anchor {anchor_id} not connected")

    def status(self):
        """Print connection status."""
        print(f"\n{'Anchor':<15} {'Connected':<12} {'Packets':<10} {'Last HB':<15}")
        print("-" * 55)
        for aid, conn in self.connections.items():
            hb_ago = f"{time.time() - conn.last_heartbeat:.0f}s ago" if conn.last_heartbeat else "never"
            print(f"{aid:<15} {str(conn.connected):<12} {conn.packets_received:<10} {hb_ago:<15}")

    def run_interactive(self):
        """Interactive command loop."""
        print("\nCommands: start | stop | status | channel <N> | quit")

        while True:
            try:
                cmd = input("\nwiloc> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                break

            if cmd == "quit" or cmd == "q":
                break
            elif cmd == "start":
                self.send_command_all(PacketType.CMD_START)
            elif cmd == "stop":
                self.send_command_all(PacketType.CMD_STOP)
            elif cmd == "status":
                self.status()
                self.send_command_all(PacketType.CMD_GET_STATUS)
            elif cmd.startswith("channel "):
                try:
                    ch = int(cmd.split()[1])
                    self.send_command_all(PacketType.CMD_SET_CHANNEL, bytes([ch]))
                except (ValueError, IndexError):
                    print("Usage: channel <1-13>")
            else:
                print("Unknown command. Try: start, stop, status, channel <N>, quit")

        # Cleanup
        for conn in self.connections.values():
            conn.disconnect()
        self.db_conn.close()


def main():
    parser = argparse.ArgumentParser(description="WiLoc Bluetooth Receiver")
    parser.add_argument("--discover", action="store_true", help="Scan for WiLoc devices")
    parser.add_argument("--connect-all", action="store_true", help="Connect to all found devices")
    parser.add_argument("--db", default="wiloc_bt.db", help="SQLite database path")
    args = parser.parse_args()

    if args.discover and not args.connect_all:
        discover_devices()
        return

    if args.connect_all:
        devices = discover_devices()
        if not devices:
            return

        receiver = Receiver(args.db)
        receiver.connect_all(devices)
        receiver.run_interactive()


if __name__ == "__main__":
    main()
