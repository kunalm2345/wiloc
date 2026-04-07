#!/usr/bin/env python3
"""
Flash an ESP32 by auto-identifying it from setup.json.

Usage:
    python setup/flash.py                        # auto-detect setup.json
    python setup/flash.py --setup setup/2026-04-06/setup.json
    python setup/flash.py --port /dev/tty.usbmodem101
"""

import argparse
import json
import glob
import subprocess
import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
ANCHOR_BUILD = PROJECT_ROOT / "wireless_serial" / "esp32_bt_serial"
TARGET_BUILD = PROJECT_ROOT / "wireless_serial" / "esp32_target"


def find_setup_files() -> list[Path]:
    return sorted(PROJECT_ROOT.glob("setup/*/setup.json"))


def choose_setup(files: list[Path]) -> Path:
    if len(files) == 1:
        print(f"Using: {files[0]}")
        return files[0]
    print("Available setup files:")
    for i, f in enumerate(files):
        print(f"  [{i}] {f.relative_to(PROJECT_ROOT)}")
    choice = input("Choose [0]: ").strip()
    idx = int(choice) if choice else 0
    return files[idx]


def find_port() -> str:
    import serial.tools.list_ports
    ports = [p.device for p in serial.tools.list_ports.comports()
             if "usbmodem" in p.device.lower() or "ttyUSB" in p.device.lower()]
    if not ports:
        print("No USB serial ports found. Plug in an ESP32.")
        sys.exit(1)
    if len(ports) == 1:
        return ports[0]
    print("Multiple ports found:")
    for i, p in enumerate(ports):
        print(f"  [{i}] {p}")
    choice = input("Choose [0]: ").strip()
    return ports[int(choice) if choice else 0]


def get_mac(port: str) -> str:
    result = subprocess.run(
        ["python3", "-m", "esptool", "--port", port, "chip-id"],
        capture_output=True, text=True, timeout=15,
    )
    for line in result.stdout.splitlines():
        if "BASE MAC" in line:
            return line.split(":", 1)[1].strip().lower()
    print(f"Could not read MAC from {port}")
    print(result.stdout[-500:] if result.stdout else "")
    print(result.stderr[-500:] if result.stderr else "")
    sys.exit(1)


def lookup_device(mac: str, setup: dict) -> dict | None:
    for dev in setup.get("anchors", []):
        if dev["base_mac"].lower() == mac:
            return {**dev, "role": "anchor"}
    tgt = setup.get("target", {})
    if tgt.get("base_mac", "").lower() == mac:
        return {**tgt, "role": "target"}
    return None


def flash_anchor(port: str, device_id: str):
    print(f"\n  Building anchor firmware with ID={device_id}...")
    os.chdir(ANCHOR_BUILD)
    r = subprocess.run(
        ["idf.py", "--preview", "build", f"-DWILOC_DEVICE_ID={device_id}"],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        print("Build failed!")
        print(r.stdout[-1000:])
        sys.exit(1)

    print(f"  Flashing to {port}...")
    r = subprocess.run([
        "python3", "-m", "esptool", "--chip", "esp32c5",
        "-p", port, "-b", "460800",
        "--before", "default-reset", "--after", "hard-reset",
        "write-flash", "--flash-mode", "dio", "--flash-size", "2MB", "--flash-freq", "80m",
        "0x2000", "build/bootloader/bootloader.bin",
        "0x8000", "build/partition_table/partition-table.bin",
        "0x10000", "build/wiloc_anchor.bin",
    ], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print("Flash failed!")
        print(r.stdout[-500:])
        print(r.stderr[-500:])
        sys.exit(1)
    print(f"  Done! {device_id} flashed.")


def flash_target(port: str):
    print(f"\n  Building target firmware...")
    os.chdir(TARGET_BUILD)
    r = subprocess.run(
        ["idf.py", "--preview", "build"],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        print("Build failed!")
        print(r.stdout[-1000:])
        sys.exit(1)

    print(f"  Flashing to {port}...")
    r = subprocess.run([
        "python3", "-m", "esptool", "--chip", "esp32c5",
        "-p", port, "-b", "460800",
        "--before", "default-reset", "--after", "hard-reset",
        "write-flash", "--flash-mode", "dio", "--flash-size", "2MB", "--flash-freq", "80m",
        "0x2000", "build/bootloader/bootloader.bin",
        "0x8000", "build/partition_table/partition-table.bin",
        "0x10000", "build/wiloc_target.bin",
    ], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print("Flash failed!")
        print(r.stdout[-500:])
        sys.exit(1)
    print(f"  Done! Target flashed.")


def main():
    parser = argparse.ArgumentParser(description="Flash ESP32 by MAC auto-detection")
    parser.add_argument("--setup", help="Path to setup.json")
    parser.add_argument("--port", help="Serial port (auto-detect if omitted)")
    args = parser.parse_args()

    # Find setup.json
    if args.setup:
        setup_path = Path(args.setup)
    else:
        files = find_setup_files()
        if not files:
            print("No setup.json found in setup/*/")
            sys.exit(1)
        setup_path = choose_setup(files)

    with open(setup_path) as f:
        setup = json.load(f)

    # Find port
    port = args.port or find_port()
    print(f"Port: {port}")

    # Read MAC
    mac = get_mac(port)
    print(f"MAC:  {mac}")

    # Lookup device
    dev = lookup_device(mac, setup)
    if not dev:
        print(f"\nMAC {mac} not found in {setup_path}")
        print("Known devices:")
        for d in setup.get("anchors", []):
            print(f"  {d['device_id']:>8} — {d['base_mac']}")
        tgt = setup.get("target", {})
        if tgt:
            print(f"  {'target':>8} — {tgt.get('base_mac', '?')}")
        sys.exit(1)

    role = dev["role"]
    device_id = dev["device_id"]
    print(f"Device: {device_id} ({role})")

    if role == "anchor":
        flash_anchor(port, device_id)
    else:
        flash_target(port)


if __name__ == "__main__":
    main()
