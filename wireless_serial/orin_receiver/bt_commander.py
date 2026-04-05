"""
BLE Commander — send one-shot commands to ESP32 anchors.

Usage:
    python bt_commander.py --discover
    python bt_commander.py --target anchor_00 --cmd start
    python bt_commander.py --all --cmd stop
    python bt_commander.py --all --cmd "channel 6"
    python bt_commander.py --target anchor_01 --cmd status
"""

import argparse
import asyncio
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from protocol import (
    PacketType, encode_frame, decode_frame,
    decode_status_payload,
)

WILOC_SVC_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
WILOC_TX_UUID  = "0000ffe1-0000-1000-8000-00805f9b34fb"
WILOC_RX_UUID  = "0000ffe2-0000-1000-8000-00805f9b34fb"
WILOC_PREFIX = "WiLoc_"


async def discover() -> list[dict]:
    print("Scanning for WiLoc devices (5s)...")
    devices = await BleakScanner.discover(timeout=5.0)
    result = []
    for d in devices:
        name = d.name or ""
        if name.startswith(WILOC_PREFIX):
            aid = name.replace(WILOC_PREFIX, "")
            result.append({"address": d.address, "name": name, "anchor_id": aid})
            print(f"  {aid}: {d.address} (RSSI={d.rssi})")
    if not result:
        print("  No WiLoc devices found.")
    return result


async def send_cmd(address: str, anchor_id: str, cmd_str: str):
    """Connect, send one command, wait for response, disconnect."""
    parts = cmd_str.strip().split()
    cmd_name = parts[0].lower()

    if cmd_name == "start":
        ptype = PacketType.CMD_START
        payload = b""
    elif cmd_name == "stop":
        ptype = PacketType.CMD_STOP
        payload = b""
    elif cmd_name == "channel":
        ptype = PacketType.CMD_SET_CHANNEL
        ch = int(parts[1]) if len(parts) > 1 else 6
        payload = bytes([ch])
    elif cmd_name == "status":
        ptype = PacketType.CMD_GET_STATUS
        payload = b""
    else:
        print(f"Unknown command: {cmd_name}")
        print("Available: start, stop, channel <N>, status")
        return

    rx_buf = bytearray()
    response_received = asyncio.Event()

    def on_notify(sender, data):
        rx_buf.extend(data)
        result = decode_frame(rx_buf)
        if result:
            rtype, rpayload, _ = result
            if rtype == PacketType.CMD_ACK:
                print(f"[{anchor_id}] ACK: {rpayload.decode('utf-8', errors='replace')}")
            elif rtype == PacketType.STATUS:
                d = decode_status_payload(rpayload)
                print(f"[{anchor_id}] Status: uptime={d['uptime_s']}s "
                      f"heap={d['free_heap']} ch={d['wifi_channel']} "
                      f"ble={'yes' if d['bt_connected'] else 'no'}")
            response_received.set()

    try:
        async with BleakClient(address) as client:
            print(f"[{anchor_id}] Connected")
            await client.start_notify(WILOC_TX_UUID, on_notify)

            frame = encode_frame(ptype, payload)
            await client.write_gatt_char(WILOC_RX_UUID, frame, response=False)
            print(f"[{anchor_id}] Sent: {cmd_name}")

            try:
                await asyncio.wait_for(response_received.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                print(f"[{anchor_id}] No response (timeout)")

            await client.stop_notify(WILOC_TX_UUID)

    except Exception as e:
        print(f"[{anchor_id}] Error: {e}")


async def main_async():
    parser = argparse.ArgumentParser(description="WiLoc BLE Commander")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--target", help="Anchor ID (e.g., anchor_00)")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--cmd", help="Command: start, stop, channel <N>, status")
    args = parser.parse_args()

    if args.discover or (not args.target and not args.cmd and not args.all):
        await discover()
        return

    if not args.cmd:
        print("Specify --cmd. Options: start, stop, channel <N>, status")
        return

    devices = await discover()
    if not devices:
        return

    targets = devices
    if args.target:
        targets = [d for d in devices if d["anchor_id"] == args.target]
        if not targets:
            print(f"Device '{args.target}' not found")
            return

    for dev in targets:
        await send_cmd(dev["address"], dev["anchor_id"], args.cmd)


if __name__ == "__main__":
    asyncio.run(main_async())
