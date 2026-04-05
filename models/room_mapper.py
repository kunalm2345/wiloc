"""
Room Mapper — infer room geometry (walls, obstacles) from CSI data.

The idea: if you move an AP to many positions in a room and measure CSI at
4 fixed anchors, the CSI patterns encode information about:
1. Wall positions (strong reflections = nearby wall)
2. Obstacle positions (signal attenuation / scattering)
3. Room shape (the "boundary" where you can/can't place the AP)

Methods:
1. Reflection-based wall detection: peaks in MUSIC spectrum = reflector distances
2. Attenuation mapping: where signal drops → obstacle
3. Boundary detection from fingerprint coverage
"""

import numpy as np
from scipy.signal import find_peaks
from scipy.ndimage import gaussian_filter
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
from ap_localizer import csi_phase_to_distance_music


# ──────────────────────────────────────────────
# Method 1: Wall detection via reflection peaks
# ──────────────────────────────────────────────

def detect_reflectors_music(csi_complex: np.ndarray,
                            subcarrier_spacing: float = 312.5e3,
                            max_range_m: float = 10.0,
                            resolution_m: float = 0.02) -> list[float]:
    """
    Use MUSIC algorithm to find ALL reflector distances (not just the direct path).
    Each peak in the MUSIC spectrum corresponds to a reflector at that distance.

    Returns: list of distances (meters) to detected reflectors.
    """
    C = 3e8
    N = len(csi_complex)

    # Spatial smoothing
    L = N // 2
    M = N - L + 1

    R = np.zeros((L, L), dtype=complex)
    for i in range(M):
        seg = csi_complex[i:i+L]
        R += np.outer(seg, seg.conj())
    R /= M

    eigenvalues, eigenvectors = np.linalg.eigh(R)
    idx = np.argsort(eigenvalues)[::-1]
    eigenvalues = eigenvalues[idx]
    eigenvectors = eigenvectors[:, idx]

    # Estimate number of signals using MDL criterion
    n_signals = _estimate_n_signals_mdl(eigenvalues, M)
    n_signals = max(1, min(n_signals, L // 2))

    noise_subspace = eigenvectors[:, n_signals:]

    # MUSIC spectrum
    distances = np.arange(0.01, max_range_m, resolution_m)
    spectrum = np.zeros(len(distances))

    for di, d in enumerate(distances):
        tau = d / C
        k = np.arange(L)
        a = np.exp(-1j * 2 * np.pi * subcarrier_spacing * k * tau)
        proj = noise_subspace.conj().T @ a
        denom = np.real(np.vdot(proj, proj))
        spectrum[di] = 1.0 / (denom + 1e-12)

    # Normalize
    spectrum = spectrum / spectrum.max()

    # Find peaks
    peaks, properties = find_peaks(spectrum, height=0.1, distance=int(0.2 / resolution_m),
                                    prominence=0.05)
    reflector_distances = distances[peaks].tolist()

    return reflector_distances


def _estimate_n_signals_mdl(eigenvalues: np.ndarray, n_snapshots: int) -> int:
    """Minimum Description Length criterion for model order selection."""
    N = len(eigenvalues)
    mdl_values = []

    for k in range(N - 1):
        noise_eigs = eigenvalues[k+1:]
        m = len(noise_eigs)
        if m == 0 or np.any(noise_eigs <= 0):
            break

        # Log-likelihood
        geo_mean = np.exp(np.mean(np.log(noise_eigs)))
        arith_mean = np.mean(noise_eigs)
        if geo_mean <= 0 or arith_mean <= 0:
            break

        log_likelihood = -n_snapshots * m * np.log(geo_mean / arith_mean)

        # Penalty
        penalty = 0.5 * k * (2 * N - k) * np.log(n_snapshots)

        mdl_values.append(log_likelihood + penalty)

    if not mdl_values:
        return 1
    return int(np.argmin(mdl_values))


def detect_walls_from_anchor(anchor_pos: np.ndarray, reflector_distances: list[float],
                             room_width: float, room_depth: float) -> list[dict]:
    """
    Given an anchor position and detected reflector distances,
    infer which walls the reflections come from.

    A wall at distance d from anchor at position (ax, ay) means the wall
    could be at x=ax-d, x=ax+d, y=ay-d, or y=ay+d.
    """
    walls = []
    for d in reflector_distances:
        # Check which wall this distance matches
        candidates = [
            {"axis": "x", "position": anchor_pos[0] - d, "type": "x_min"},
            {"axis": "x", "position": anchor_pos[0] + d, "type": "x_max"},
            {"axis": "y", "position": anchor_pos[1] - d, "type": "y_min"},
            {"axis": "y", "position": anchor_pos[1] + d, "type": "y_max"},
        ]

        for c in candidates:
            # Keep if within reasonable room bounds
            if -1 < c["position"] < room_width + 1 and c["axis"] == "x":
                walls.append({"distance": d, **c})
            elif -1 < c["position"] < room_depth + 1 and c["axis"] == "y":
                walls.append({"distance": d, **c})

    return walls


def estimate_room_bounds(all_anchor_walls: list[list[dict]],
                         anchor_positions: np.ndarray) -> dict:
    """
    Fuse wall detections from all anchors to estimate room boundaries.
    Uses RANSAC-like voting: wall positions that are consistent across
    multiple anchors are more likely to be real walls.
    """
    # Collect all wall position estimates
    x_walls = []  # (position, anchor_idx)
    y_walls = []

    for ai, walls in enumerate(all_anchor_walls):
        for w in walls:
            if w["axis"] == "x":
                x_walls.append(w["position"])
            else:
                y_walls.append(w["position"])

    # Cluster wall positions (walls within 20cm of each other are the same wall)
    x_clusters = _cluster_1d(x_walls, threshold=0.2)
    y_clusters = _cluster_1d(y_walls, threshold=0.2)

    # The room boundaries are the two most extreme clusters on each axis
    x_clusters.sort()
    y_clusters.sort()

    bounds = {
        "x_min": x_clusters[0] if x_clusters else 0,
        "x_max": x_clusters[-1] if x_clusters else 4,
        "y_min": y_clusters[0] if y_clusters else 0,
        "y_max": y_clusters[-1] if y_clusters else 4,
    }

    bounds["width"] = bounds["x_max"] - bounds["x_min"]
    bounds["depth"] = bounds["y_max"] - bounds["y_min"]

    return bounds


def _cluster_1d(values: list[float], threshold: float) -> list[float]:
    """Simple 1D clustering — merge values within threshold of each other."""
    if not values:
        return []
    values = sorted(values)
    clusters = [[values[0]]]
    for v in values[1:]:
        if v - clusters[-1][-1] < threshold:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return [np.mean(c) for c in clusters]


# ──────────────────────────────────────────────
# Method 2: Obstacle heatmap from signal attenuation
# ──────────────────────────────────────────────

def build_attenuation_map(grid_data: list[dict], room_width: float, room_depth: float,
                          resolution: float = 0.1) -> np.ndarray:
    """
    Build a 2D map of signal attenuation anomalies.
    Regions with unexpectedly low signal = likely obstacles.

    grid_data: list of {"ap_position": [x,y,z], "rssi": {anchor_id: val}, ...}
    """
    nx = int(room_width / resolution) + 1
    ny = int(room_depth / resolution) + 1

    # Average RSSI at each grid point
    rssi_map = np.full((ny, nx), np.nan)
    count_map = np.zeros((ny, nx))

    for entry in grid_data:
        x, y = entry["ap_position"][0], entry["ap_position"][1]
        ix = int(x / resolution)
        iy = int(y / resolution)
        if 0 <= ix < nx and 0 <= iy < ny:
            avg_rssi = np.mean(list(entry["rssi"].values()) if isinstance(entry["rssi"], dict)
                               else [s["rssi"] for s in entry.get("samples", [])])
            if np.isnan(rssi_map[iy, ix]):
                rssi_map[iy, ix] = avg_rssi
            else:
                rssi_map[iy, ix] += avg_rssi
            count_map[iy, ix] += 1

    # Normalize
    valid = count_map > 0
    rssi_map[valid] /= count_map[valid]

    # Expected RSSI based on distance to anchors (free space model)
    # Anomaly = actual - expected. Negative anomaly = more attenuation = obstacle
    # For now, smooth the map and find regions below the mean
    smoothed = gaussian_filter(np.nan_to_num(rssi_map, nan=-100), sigma=2)

    # Anomaly: how much weaker than local neighborhood
    heavily_smoothed = gaussian_filter(np.nan_to_num(rssi_map, nan=-100), sigma=5)
    anomaly = smoothed - heavily_smoothed  # negative = obstacle

    return anomaly


# ──────────────────────────────────────────────
# Visualization
# ──────────────────────────────────────────────

def visualize_room_map(room_bounds: dict, anomaly_map: np.ndarray,
                       anchor_positions: np.ndarray,
                       true_obstacles: list = None,
                       save_path: str = "room_map.png"):
    """Visualize detected room shape and obstacles."""
    import matplotlib.pyplot as plt
    import matplotlib.patches as patches

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    # Left: detected room bounds
    ax = axes[0]
    ax.set_xlim(-0.5, room_bounds.get("x_max", 4) + 0.5)
    ax.set_ylim(-0.5, room_bounds.get("y_max", 4) + 0.5)

    # Draw detected walls
    xmin, xmax = room_bounds["x_min"], room_bounds["x_max"]
    ymin, ymax = room_bounds["y_min"], room_bounds["y_max"]
    rect = patches.Rectangle((xmin, ymin), xmax - xmin, ymax - ymin,
                               linewidth=3, edgecolor='blue', facecolor='lightblue',
                               alpha=0.3, label="Detected room")
    ax.add_patch(rect)

    # Draw true room if known
    if true_obstacles is not None:
        true_rect = patches.Rectangle((0, 0), 4, 4,
                                       linewidth=2, edgecolor='green', facecolor='none',
                                       linestyle='--', label="True room")
        ax.add_patch(true_rect)

        for obs in true_obstacles:
            mn = obs.min_corner
            w = obs.width
            d = obs.depth
            obs_rect = patches.Rectangle((mn[0], mn[1]), w, d,
                                          linewidth=1, edgecolor='red', facecolor='red',
                                          alpha=0.3)
            ax.add_patch(obs_rect)
            ax.text(obs.x, obs.y, obs.name, ha='center', va='center', fontsize=8)

    # Anchors
    ax.scatter(anchor_positions[:, 0], anchor_positions[:, 1],
               marker='^', s=100, c='red', zorder=5, label='Anchors')

    ax.set_xlabel("X (m)")
    ax.set_ylabel("Y (m)")
    ax.set_title(f"Detected Room: {room_bounds['width']:.2f}m × {room_bounds['depth']:.2f}m")
    ax.legend()
    ax.set_aspect('equal')
    ax.grid(True, alpha=0.3)

    # Right: attenuation anomaly map
    ax2 = axes[1]
    im = ax2.imshow(anomaly_map, extent=[0, room_bounds.get("x_max", 4),
                                          0, room_bounds.get("y_max", 4)],
                     origin='lower', cmap='RdBu_r', aspect='equal')
    ax2.scatter(anchor_positions[:, 0], anchor_positions[:, 1],
                marker='^', s=100, c='black', zorder=5)
    ax2.set_xlabel("X (m)")
    ax2.set_ylabel("Y (m)")
    ax2.set_title("Signal Attenuation Anomaly\n(blue = obstacle, red = open)")
    plt.colorbar(im, ax=ax2, label="RSSI anomaly (dB)")

    plt.tight_layout()
    plt.savefig(save_path, dpi=150)
    print(f"Saved room map to {save_path}")


if __name__ == "__main__":
    # Quick test with synthetic data
    sys.path.insert(0, str(Path(__file__).parent.parent / "simulator"))
    from room_model import create_default_room
    from csi_simulator import synthesize_csi, compute_rssi

    room = create_default_room()
    anchor_positions = np.array([[a.x, a.y] for a in room.anchors])

    print("=" * 60)
    print("ROOM MAPPING — SYNTHETIC TEST")
    print("=" * 60)

    # Step 1: Detect walls from each anchor using a known AP position
    print("\n--- Wall Detection ---")
    ap_pos = np.array([2.0, 2.0, 1.0])

    all_anchor_walls = []
    for anchor in room.anchors:
        rx = np.array([anchor.x, anchor.y, anchor.z])
        csi = synthesize_csi(ap_pos, rx, room, noise_std=0.005)
        reflector_dists = detect_reflectors_music(csi)
        walls = detect_walls_from_anchor(
            np.array([anchor.x, anchor.y]), reflector_dists,
            room.width, room.depth
        )
        all_anchor_walls.append(walls)
        print(f"  {anchor.id} at ({anchor.x}, {anchor.y}): "
              f"reflectors at {[f'{d:.2f}m' for d in reflector_dists]}")

    bounds = estimate_room_bounds(all_anchor_walls, anchor_positions)
    print(f"\nDetected room bounds: {bounds['x_min']:.2f} to {bounds['x_max']:.2f} x "
          f"{bounds['y_min']:.2f} to {bounds['y_max']:.2f}")
    print(f"Detected room size: {bounds['width']:.2f}m × {bounds['depth']:.2f}m "
          f"(true: {room.width}m × {room.depth}m)")

    # Step 2: Build attenuation map from grid scan
    print("\n--- Obstacle Detection (grid scan) ---")
    grid_data = []
    for x in np.arange(0.3, room.width - 0.3, 0.25):
        for y in np.arange(0.3, room.depth - 0.3, 0.25):
            ap = np.array([x, y, 1.0])
            # Skip if inside obstacle
            skip = False
            for obs in room.obstacles:
                mn = obs.min_corner
                mx = obs.max_corner
                if mn[0] <= x <= mx[0] and mn[1] <= y <= mx[1]:
                    skip = True
                    break
            if skip:
                continue

            rssi_vals = {}
            for anchor in room.anchors:
                rx = np.array([anchor.x, anchor.y, anchor.z])
                csi = synthesize_csi(ap, rx, room, noise_std=0.005)
                rssi_vals[anchor.id] = compute_rssi(csi)

            grid_data.append({"ap_position": [x, y, 1.0], "rssi": rssi_vals})

    anomaly = build_attenuation_map(grid_data, room.width, room.depth, resolution=0.1)
    print(f"Attenuation map shape: {anomaly.shape}")

    try:
        visualize_room_map(bounds, anomaly, anchor_positions,
                           true_obstacles=room.obstacles, save_path="room_map.png")
    except ImportError:
        print("matplotlib not available — skipping visualization")
