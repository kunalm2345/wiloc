"""
BLE GATT Receiver — runs on Orin Nano (or Mac/any machine with BLE).

Discovers WiLoc ESP32-C5 anchors via BLE, connects to their GATT service,
subscribes to TX notifications (CSI data), writes commands to RX characteristic.

Uses `bleak` — cross-platform BLE library (pip install bleak).

Usage:
    python bt_receiver.py --discover              # scan for WiLoc devices
    python bt_receiver.py --connect-all           # connect to all found devices
    python bt_receiver.py --connect-all --db data.db
"""

import argparse
import asyncio
import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from bleak import BleakClient, BleakScanner

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from protocol import (
    PacketType, decode_frame, decode_csi_payload, decode_rssi_payload,
    decode_status_payload, decode_heartbeat_payload,
    encode_frame,
)

# BLE UUIDs (must match firmware)
WILOC_SVC_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
WILOC_TX_UUID  = "0000ffe1-0000-1000-8000-00805f9b34fb"  # notify
WILOC_RX_UUID  = "0000ffe2-0000-1000-8000-00805f9b34fb"  # write

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


async def discover_devices(scan_time: float = 8.0) -> list[dict]:
    """Scan for WiLoc ESP32 devices via BLE."""
    print(f"Scanning for BLE devices ({scan_time}s)...")

    discovered = await BleakScanner.discover(timeout=scan_time, return_adv=True)

    wiloc_devices = []
    for addr, (d, adv) in discovered.items():
        name = d.name or ""
        if name.startswith(WILOC_PREFIX):
            anchor_id = name.replace(WILOC_PREFIX, "")
            rssi = adv.rssi if adv else None
            wiloc_devices.append({
                "address": d.address,
                "name": name,
                "anchor_id": anchor_id,
                "rssi": rssi,
            })
            print(f"  Found: {name} ({d.address}) RSSI={rssi}dBm")

    if not wiloc_devices:
        print("  No WiLoc devices found. Check ESP32s are powered and flashed.")
    else:
        print(f"\nFound {len(wiloc_devices)} WiLoc device(s).")

    return wiloc_devices


class AnchorConnection:
    """Manages a BLE connection to one ESP32 anchor."""

    def __init__(self, address: str, name: str, anchor_id: str,
                 db_conn: sqlite3.Connection, db_lock: threading.Lock):
        self.address = address
        self.name = name
        self.anchor_id = anchor_id
        self.db_conn = db_conn
        self.db_lock = db_lock
        self.client: BleakClient | None = None
        self.connected = False
        self.packets_received = 0
        self.last_heartbeat = 0.0
        self._rx_buf = bytearray()
        self._commit_counter = 0

    def _notification_handler(self, sender, data: bytearray):
        """Called when ESP32 sends a BLE notification (CSI/RSSI/heartbeat/etc)."""
        self._rx_buf.extend(data)

        while True:
            result = decode_frame(self._rx_buf)
            if result is None:
                break

            ptype, payload, _ = result
            self.packets_received += 1
            self._handle_packet(ptype, payload)

            self._commit_counter += 1
            if self._commit_counter >= 50:
                with self.db_lock:
                    self.db_conn.commit()
                self._commit_counter = 0

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
                decode_heartbeat_payload(payload)
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

    async def connect(self) -> bool:
        """Establish BLE GATT connection."""
        try:
            print(f"  [{self.anchor_id}] Connecting to {self.address}...")
            self.client = BleakClient(self.address)
            await self.client.connect()

            if not self.client.is_connected:
                print(f"  [{self.anchor_id}] Connection failed")
                return False

            mtu = self.client.mtu_size
            print(f"  [{self.anchor_id}] Connected! MTU={mtu}")

            await self.client.start_notify(WILOC_TX_UUID, self._notification_handler)
            self.connected = True
            print(f"  [{self.anchor_id}] Subscribed to notifications")
            return True

        except Exception as e:
            print(f"  [{self.anchor_id}] Connection failed: {e}")
            self.connected = False
            return False

    async def disconnect(self):
        self.connected = False
        if self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(WILOC_TX_UUID)
                await self.client.disconnect()
            except Exception:
                pass

    async def send_command(self, ptype: int, payload: bytes = b""):
        """Send a command frame to this anchor via BLE write."""
        if not self.connected or not self.client:
            print(f"  [{self.anchor_id}] Not connected")
            return
        frame = encode_frame(ptype, payload)
        try:
            await self.client.write_gatt_char(WILOC_RX_UUID, frame, response=False)
        except Exception as e:
            print(f"  [{self.anchor_id}] Write failed: {e}")


class Receiver:
    """Manages BLE connections to all ESP32 anchors."""

    def __init__(self, db_path: str):
        self.db_conn = init_db(db_path)
        self.db_lock = threading.Lock()
        self.connections: dict[str, AnchorConnection] = {}

    async def connect_all(self, devices: list[dict]):
        for dev in devices:
            conn = AnchorConnection(
                address=dev["address"],
                name=dev["name"],
                anchor_id=dev["anchor_id"],
                db_conn=self.db_conn,
                db_lock=self.db_lock,
            )
            self.connections[dev["anchor_id"]] = conn
            await conn.connect()

        ok = sum(1 for c in self.connections.values() if c.connected)
        print(f"\nConnected to {ok}/{len(devices)} anchors.")

    async def send_command_all(self, ptype: int, payload: bytes = b""):
        for conn in self.connections.values():
            await conn.send_command(ptype, payload)

    def status(self):
        print(f"\n{'Anchor':<15} {'Connected':<12} {'Packets':<10} {'Last HB':<15}")
        print("-" * 55)
        for aid, conn in self.connections.items():
            hb_ago = f"{time.time() - conn.last_heartbeat:.0f}s ago" if conn.last_heartbeat else "never"
            print(f"{aid:<15} {str(conn.connected):<12} {conn.packets_received:<10} {hb_ago:<15}")

    async def run_interactive(self):
        """Interactive command loop."""
        print("\nCommands: start | stop | status | channel <N> | quit")

        loop = asyncio.get_event_loop()

        while True:
            try:
                cmd = await loop.run_in_executor(None, lambda: input("\nwiloc> ").strip().lower())
            except (EOFError, KeyboardInterrupt):
                break

            if cmd in ("quit", "q"):
                break
            elif cmd == "start":
                await self.send_command_all(PacketType.CMD_START)
            elif cmd == "stop":
                await self.send_command_all(PacketType.CMD_STOP)
            elif cmd == "status":
                self.status()
                await self.send_command_all(PacketType.CMD_GET_STATUS)
            elif cmd.startswith("channel "):
                try:
                    ch = int(cmd.split()[1])
                    await self.send_command_all(PacketType.CMD_SET_CHANNEL, bytes([ch]))
                except (ValueError, IndexError):
                    print("Usage: channel <1-13>")
            else:
                print("Unknown command. Try: start, stop, status, channel <N>, quit")

        for conn in self.connections.values():
            await conn.disconnect()
        with self.db_lock:
            self.db_conn.commit()
        self.db_conn.close()


async def main_async():
    parser = argparse.ArgumentParser(description="WiLoc BLE Receiver")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--connect-all", action="store_true")
    parser.add_argument("--db", default="wiloc_ble.db")
    parser.add_argument("--scan-time", type=float, default=8.0)
    args = parser.parse_args()

    if args.discover and not args.connect_all:
        await discover_devices(args.scan_time)
        return

    if args.connect_all:
        devices = await discover_devices(args.scan_time)
        if not devices:
            return
        receiver = Receiver(args.db)
        await receiver.connect_all(devices)
        await receiver.run_interactive()


if __name__ == "__main__":
    asyncio.run(main_async())
