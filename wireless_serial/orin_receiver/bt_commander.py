"""
Bluetooth Commander — send commands to ESP32 anchors from command line.

Quick one-shot commands without running the full receiver.

Usage:
    python bt_commander.py --discover
    python bt_commander.py --target anchor_00 --cmd start
    python bt_commander.py --all --cmd stop
    python bt_commander.py --all --cmd "channel 6"
    python bt_commander.py --target anchor_01 --cmd status
"""

import argparse
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from protocol import (
    PacketType, encode_frame, decode_frame,
    decode_status_payload, COMMANDS,
)

SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"
WILOC_PREFIX = "WiLoc_"


def discover() -> list[dict]:
    import bluetooth
    print("Scanning for WiLoc devices (8s)...")
    nearby = bluetooth.discover_devices(duration=8, lookup_names=True, flush_cache=True)
    devices = []
    for addr, name in nearby:
        if name and name.startswith(WILOC_PREFIX):
            aid = name.replace(WILOC_PREFIX, "")
            devices.append({"address": addr, "name": name, "anchor_id": aid})
            print(f"  {aid}: {addr}")
    return devices


def send_cmd(address: str, anchor_id: str, cmd_str: str):
    """Connect, send one command, wait for ACK, disconnect."""
    import bluetooth

    parts = cmd_str.strip().split()
    cmd_name = parts[0].lower()

    # Map command string to packet type
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

    # Connect
    try:
        services = bluetooth.find_service(uuid=SPP_UUID, address=address)
        port = services[0]["port"] if services else 1

        sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
        sock.connect((address, port))
        sock.settimeout(3.0)
        print(f"[{anchor_id}] Connected")
    except Exception as e:
        print(f"[{anchor_id}] Connection failed: {e}")
        return

    # Send
    frame = encode_frame(ptype, payload)
    sock.send(frame)
    print(f"[{anchor_id}] Sent: {cmd_name}")

    # Wait for response
    rx_buf = bytearray()
    deadline = time.time() + 3.0
    while time.time() < deadline:
        try:
            data = sock.recv(512)
            if data:
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
                              f"bt={'yes' if d['bt_connected'] else 'no'}")
                    break
        except Exception:
            pass

    sock.close()


def main():
    parser = argparse.ArgumentParser(description="WiLoc BT Commander")
    parser.add_argument("--discover", action="store_true", help="Scan for devices")
    parser.add_argument("--target", help="Anchor ID (e.g., anchor_00)")
    parser.add_argument("--all", action="store_true", help="Send to all devices")
    parser.add_argument("--cmd", help="Command: start, stop, channel <N>, status")
    args = parser.parse_args()

    if args.discover or (not args.target and not args.cmd):
        discover()
        return

    if not args.cmd:
        print("Specify --cmd. Options: start, stop, channel <N>, status")
        return

    devices = discover()
    if not devices:
        return

    targets = devices
    if args.target:
        targets = [d for d in devices if d["anchor_id"] == args.target]
        if not targets:
            print(f"Device '{args.target}' not found")
            return

    for dev in targets:
        send_cmd(dev["address"], dev["anchor_id"], args.cmd)


if __name__ == "__main__":
    main()
