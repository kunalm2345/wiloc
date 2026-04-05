"""
Quick visualization of CSI data — use this to verify your ESP32 setup
is producing usable data before doing anything else.

Usage:
    python plot_csi.py --db wiloc.db --anchor anchor_01 --n 200
"""

import sqlite3
import json
import argparse
import numpy as np
import matplotlib.pyplot as plt

import sys
sys.path.insert(0, "..")
from processing.csi_features import raw_to_complex, amplitude, sanitize_phase, phase


def plot_amplitude_heatmap(db_path: str, anchor_id: str, n: int):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """SELECT csi_raw, rssi, timestamp_ms
           FROM csi_readings
           WHERE anchor_id = ?
           ORDER BY timestamp_ms
           LIMIT ?""",
        (anchor_id, n),
    ).fetchall()
    conn.close()

    if not rows:
        print("No data found.")
        return

    # Build amplitude matrix
    amps = []
    rssis = []
    for csi_raw, rssi, _ in rows:
        csi_c = raw_to_complex(csi_raw)
        amps.append(amplitude(csi_c))
        rssis.append(rssi)

    amp_matrix = np.array(amps)  # shape: (n_packets, n_subcarriers)

    fig, axes = plt.subplots(2, 1, figsize=(12, 8))

    # Amplitude heatmap
    im = axes[0].imshow(amp_matrix.T, aspect="auto", cmap="viridis",
                         interpolation="nearest")
    axes[0].set_xlabel("Packet index")
    axes[0].set_ylabel("Subcarrier index")
    axes[0].set_title(f"CSI Amplitude Heatmap — Anchor: {anchor_id}")
    plt.colorbar(im, ax=axes[0], label="Amplitude")

    # RSSI over time
    axes[1].plot(rssis, "b-", alpha=0.7)
    axes[1].set_xlabel("Packet index")
    axes[1].set_ylabel("RSSI (dBm)")
    axes[1].set_title("RSSI over time")
    axes[1].grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig("csi_overview.png", dpi=150)
    plt.show()
    print("Saved to csi_overview.png")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="wiloc.db")
    parser.add_argument("--anchor", required=True)
    parser.add_argument("--n", type=int, default=200)
    args = parser.parse_args()

    plot_amplitude_heatmap(args.db, args.anchor, args.n)
