"""
Experiment Orchestrator — coordinates data collection across multiple RPis.

Run this on the Orin Nano (or any machine that can SSH to all RPis).

Usage:
    python run_experiment.py --config ../configs/room_experiment.yaml --phase grid
    python run_experiment.py --config ../configs/room_experiment.yaml --phase fixed
"""

import argparse
import subprocess
import time
import json
import sqlite3
from pathlib import Path
from datetime import datetime

import yaml


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def start_collectors(config: dict) -> list[subprocess.Popen]:
    """SSH into each RPi and start the serial_collector.py process."""
    procs = []
    db_name = f"{config['experiment']['name']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"

    for anchor in config["anchors"]:
        host = anchor["rpi_host"]
        port = anchor["usb_port"]
        aid = anchor["id"]

        cmd = (
            f"ssh {host} 'cd ~/wiloc/collector && "
            f"python3 serial_collector.py "
            f"--port {port} --anchor_id {aid} --db /tmp/{db_name}'"
        )
        print(f"Starting collector on {host} for {aid}...")
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        procs.append({"proc": proc, "host": host, "anchor_id": aid, "db_name": db_name})

    # Give collectors time to start
    time.sleep(2)
    print(f"All {len(procs)} collectors started.")
    return procs


def stop_collectors(procs: list[dict]):
    """Stop all collector processes."""
    for p in procs:
        p["proc"].terminate()
        print(f"Stopped collector on {p['host']} ({p['anchor_id']})")

    # Wait for graceful shutdown
    for p in procs:
        p["proc"].wait(timeout=5)


def collect_dbs(procs: list[dict], local_dir: str):
    """SCP all databases from RPis to local machine."""
    Path(local_dir).mkdir(parents=True, exist_ok=True)
    for p in procs:
        local_path = f"{local_dir}/{p['anchor_id']}_{p['db_name']}"
        cmd = f"scp {p['host']}:/tmp/{p['db_name']} {local_path}"
        print(f"Collecting DB from {p['host']}...")
        subprocess.run(cmd, shell=True)


def label_current_position(db_paths: list[str], label_x: float, label_y: float, label_z: float,
                            start_time: str, end_time: str):
    """Label all readings in the time window with the known position."""
    for db_path in db_paths:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """UPDATE csi_readings
               SET label_x = ?, label_y = ?, label_z = ?
               WHERE collected_at BETWEEN ? AND ?""",
            (label_x, label_y, label_z, start_time, end_time),
        )
        conn.commit()
        updated = conn.execute("SELECT changes()").fetchone()[0]
        conn.close()
        print(f"  Labeled {updated} readings in {db_path}")


def run_grid_phase(config: dict):
    """
    Interactive grid collection: guides you through placing AP at each grid point.
    """
    grid = config["data_collection"]["grid"]
    origin = grid["origin"]
    spacing = grid["spacing_m"]
    rows = grid["rows"]
    cols = grid["cols"]
    dwell = grid["dwell_time_s"]
    ap_height = grid["ap_height_m"]

    print("=" * 60)
    print("GRID COLLECTION PHASE")
    print(f"Grid: {rows}x{cols} = {rows*cols} points, {spacing}m spacing")
    print(f"Dwell time: {dwell}s per point")
    print(f"Total estimated time: {rows * cols * (dwell + 5) / 60:.1f} minutes")
    print("=" * 60)

    procs = start_collectors(config)

    try:
        point_num = 0
        for r in range(rows):
            for c in range(cols):
                x = origin[0] + c * spacing
                y = origin[1] + r * spacing
                point_num += 1

                print(f"\n--- Point {point_num}/{rows*cols}: ({x:.2f}, {y:.2f}) ---")
                input(f"Place AP at ({x:.2f}, {y:.2f}, {ap_height}) and press Enter...")

                start_time = datetime.utcnow().isoformat()
                print(f"Collecting for {dwell}s...")
                time.sleep(dwell)
                end_time = datetime.utcnow().isoformat()

                print(f"Done. Labeled ({x:.2f}, {y:.2f}, {ap_height})")
                # Labeling will happen after DB collection

    except KeyboardInterrupt:
        print("\nCollection interrupted by user.")
    finally:
        stop_collectors(procs)
        collect_dbs(procs, f"../data/{config['experiment']['name']}")

    print(f"\nGrid collection complete! Data in ../data/{config['experiment']['name']}/")


def run_fixed_phase(config: dict):
    """Collect data at specific fixed positions (longer duration, more samples)."""
    positions = config["data_collection"]["fixed_positions"]

    print("=" * 60)
    print("FIXED POSITION COLLECTION PHASE")
    print(f"{len(positions)} positions")
    print("=" * 60)

    procs = start_collectors(config)

    try:
        for i, pos in enumerate(positions):
            name = pos["name"]
            x, y, z = pos["position"]
            duration = pos["duration_s"]

            print(f"\n--- Position {i+1}/{len(positions)}: '{name}' ({x}, {y}, {z}) ---")
            input(f"Place AP at '{name}' ({x}, {y}, {z}) and press Enter...")

            print(f"Collecting for {duration}s...")
            time.sleep(duration)
            print("Done.")

    except KeyboardInterrupt:
        print("\nCollection interrupted.")
    finally:
        stop_collectors(procs)
        collect_dbs(procs, f"../data/{config['experiment']['name']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Experiment Orchestrator")
    parser.add_argument("--config", required=True)
    parser.add_argument("--phase", choices=["grid", "fixed"], required=True)
    args = parser.parse_args()

    config = load_config(args.config)
    print(f"Experiment: {config['experiment']['name']}")

    if args.phase == "grid":
        run_grid_phase(config)
    elif args.phase == "fixed":
        run_fixed_phase(config)
