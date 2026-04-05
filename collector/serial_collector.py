"""
RPi Serial Collector — reads CSI data from ESP32 over USB serial,
parses it, and stores to SQLite.

Usage:
    python serial_collector.py --port /dev/ttyUSB0 --anchor_id anchor_01 --db wiloc.db
"""

import argparse
import sqlite3
import time
import json
from datetime import datetime

import serial


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
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
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_timestamp ON csi_readings(timestamp_ms)
    """)
    conn.commit()
    return conn


def parse_csi_line(line: str) -> dict | None:
    """Parse a CSI_DATA line from ESP32 serial output."""
    line = line.strip()
    if not line.startswith("CSI_DATA"):
        return None

    parts = line.split(",")
    if len(parts) < 10:
        return None

    try:
        return {
            "timestamp_ms": int(parts[1]),
            "target_mac": parts[2],
            "rssi": int(parts[3]),
            "channel": int(parts[4]),
            "bandwidth": int(parts[7]),
            "csi_len": int(parts[8]),
            "csi_raw": json.dumps([int(x) for x in parts[9:]]),
        }
    except (ValueError, IndexError):
        return None


def collect(port: str, baud: int, anchor_id: str, db_path: str):
    conn = init_db(db_path)
    ser = serial.Serial(port, baud, timeout=1)
    print(f"Collecting from {port} as anchor {anchor_id} -> {db_path}")

    count = 0
    try:
        while True:
            raw = ser.readline().decode("utf-8", errors="ignore")
            parsed = parse_csi_line(raw)
            if parsed is None:
                continue

            conn.execute(
                """INSERT INTO csi_readings
                   (timestamp_ms, anchor_id, target_mac, rssi, channel,
                    bandwidth, csi_len, csi_raw, collected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    parsed["timestamp_ms"],
                    anchor_id,
                    parsed["target_mac"],
                    parsed["rssi"],
                    parsed["channel"],
                    parsed["bandwidth"],
                    parsed["csi_len"],
                    parsed["csi_raw"],
                    datetime.utcnow().isoformat(),
                ),
            )
            count += 1
            if count % 100 == 0:
                conn.commit()
                print(f"  {count} packets collected")
    except KeyboardInterrupt:
        conn.commit()
        print(f"\nDone. {count} packets saved.")
    finally:
        ser.close()
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ESP32 CSI Serial Collector")
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--anchor_id", required=True)
    parser.add_argument("--db", default="wiloc.db")
    args = parser.parse_args()

    collect(args.port, args.baud, args.anchor_id, args.db)
