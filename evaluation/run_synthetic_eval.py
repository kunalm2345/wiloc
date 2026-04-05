"""
End-to-end synthetic evaluation — run this to validate everything works
before touching real hardware.

Usage:
    python run_synthetic_eval.py
"""

import sys
from pathlib import Path

# Add parent dirs to path
sys.path.insert(0, str(Path(__file__).parent.parent / "simulator"))
sys.path.insert(0, str(Path(__file__).parent.parent / "models"))
sys.path.insert(0, str(Path(__file__).parent.parent / "processing"))

import numpy as np
from room_model import create_default_room
from csi_simulator import synthesize_csi, compute_rssi, generate_grid_dataset
from ap_localizer import (
    localize_rssi, localize_csi_phase, localize_fused,
    build_fingerprint_db, localize_fingerprint,
)


def main():
    room = create_default_room()
    anchor_positions = np.array([[a.x, a.y] for a in room.anchors])

    print("=" * 70)
    print("WILOC SYNTHETIC EVALUATION")
    print("=" * 70)
    print(f"Room: {room.width}m × {room.depth}m × {room.height}m")
    print(f"Anchors: {len(room.anchors)} at corners")
    print(f"Obstacles: {[o.name for o in room.obstacles]}")

    # ── Step 1: Generate fingerprint database ──
    print("\n--- Generating fingerprint database (25cm grid) ---")
    grid_dataset = generate_grid_dataset(
        room, grid_spacing=0.25, n_samples_per_point=10, noise_std=0.005
    )
    fp_features, fp_positions = build_fingerprint_db(grid_dataset)
    print(f"Database: {len(fp_features)} positions, {fp_features.shape[1]} features each")

    # ── Step 2: Test localization at random positions ──
    print("\n--- Localization accuracy test (50 random positions) ---")
    rng = np.random.default_rng(42)
    n_tests = 50

    errors = {"rssi": [], "csi_linear": [], "csi_music": [], "fingerprint": [], "fused": []}

    for _ in range(n_tests):
        # Random position (not inside obstacles, not too close to walls)
        while True:
            true_x = rng.uniform(0.3, room.width - 0.3)
            true_y = rng.uniform(0.3, room.depth - 0.3)
            true_pos = np.array([true_x, true_y, 1.0])

            # Check not inside obstacle
            inside = False
            for obs in room.obstacles:
                mn = obs.min_corner
                mx = obs.max_corner
                if mn[0] <= true_x <= mx[0] and mn[1] <= true_y <= mx[1]:
                    inside = True
                    break
            if not inside:
                break

        # Simulate CSI measurements
        csi_list = []
        rssi_list = []
        csi_dict = {}
        rssi_dict = {}

        for anchor in room.anchors:
            rx = np.array([anchor.x, anchor.y, anchor.z])
            samples = [synthesize_csi(true_pos, rx, room, noise_std=0.01) for _ in range(20)]
            csi_avg = np.mean(samples, axis=0)
            csi_list.append(csi_avg)
            rssi_val = compute_rssi(csi_avg)
            rssi_list.append(rssi_val)
            csi_dict[anchor.id] = {"real": csi_avg.real.tolist(), "imag": csi_avg.imag.tolist()}
            rssi_dict[anchor.id] = rssi_val

        results = localize_fused(
            anchor_positions, csi_list, np.array(rssi_list),
            fp_features, fp_positions, csi_dict, rssi_dict,
        )

        for method, est in results.items():
            err = np.linalg.norm(est[:2] - true_pos[:2])
            errors[method].append(err)

    # ── Step 3: Print results ──
    print(f"\n{'Method':<20} {'Mean':>8} {'Median':>8} {'90th%':>8} {'Max':>8}")
    print("-" * 60)
    for method, errs in errors.items():
        errs = np.array(errs)
        print(f"{method:<20} {errs.mean():>7.3f}m {np.median(errs):>7.3f}m "
              f"{np.percentile(errs, 90):>7.3f}m {errs.max():>7.3f}m")

    # ── Step 4: Can we hit 10cm? ──
    fused_errs = np.array(errors["fused"])
    pct_under_10cm = np.mean(fused_errs < 0.10) * 100
    pct_under_20cm = np.mean(fused_errs < 0.20) * 100
    pct_under_50cm = np.mean(fused_errs < 0.50) * 100

    print(f"\n--- Fused method accuracy ---")
    print(f"  < 10cm: {pct_under_10cm:.0f}% of positions")
    print(f"  < 20cm: {pct_under_20cm:.0f}% of positions")
    print(f"  < 50cm: {pct_under_50cm:.0f}% of positions")

    if pct_under_10cm > 50:
        print("\n✓ 10cm accuracy looks achievable in this room!")
    elif pct_under_20cm > 50:
        print("\n~ 20cm accuracy is realistic. 10cm needs real CSI calibration.")
    else:
        print("\n✗ Accuracy limited by simulation fidelity. Real CSI data should be better.")


if __name__ == "__main__":
    main()
