"""
CSI Simulator — generates synthetic CSI data using a simplified ray-tracing model.

This is the "good enough to start with" simulator before you set up full Sionna.
Uses image-source method for reflections + direct path to compute multipath CSI.

For a 4x4m room with known geometry, this gives realistic-ish CSI that you can
use to develop and test your localization algorithms before collecting real data.
"""

import json
import numpy as np
from dataclasses import dataclass
from pathlib import Path
from room_model import Room, Box, Anchor, create_default_room, RF_MATERIALS


# WiFi constants
FREQ_HZ = 2.4e9                    # 2.4 GHz
C = 3e8                            # speed of light
WAVELENGTH = C / FREQ_HZ           # ~0.125m
SUBCARRIERS = 52                    # 20MHz HT (802.11n)
SUBCARRIER_SPACING = 312.5e3       # Hz
BANDWIDTH = SUBCARRIERS * SUBCARRIER_SPACING  # ~16.25 MHz effective


@dataclass
class Ray:
    """A propagation path from TX to RX."""
    distance: float         # total path length in meters
    attenuation: float      # amplitude attenuation (linear)
    phase_shift: float      # additional phase shift from reflections
    n_reflections: int


def direct_path(tx: np.ndarray, rx: np.ndarray, obstacles: list[Box]) -> Ray | None:
    """Compute direct (line-of-sight) path, checking for obstacle occlusion."""
    d = np.linalg.norm(rx - tx)
    direction = (rx - tx) / d

    # Simple AABB ray intersection to check for blocking obstacles
    for obs in obstacles:
        mn = np.array(obs.min_corner)
        mx = np.array(obs.max_corner)
        if _ray_intersects_aabb(tx, direction, mn, mx, d):
            # Obstacle blocks LOS — attenuate heavily but don't kill
            # (signal diffracts around obstacles at 2.4 GHz)
            atten = free_space_loss(d) * 0.1  # 20dB additional loss through obstacle
            return Ray(d, atten, 0.0, 0)

    atten = free_space_loss(d)
    return Ray(d, atten, 0.0, 0)


def free_space_loss(distance: float) -> float:
    """Free-space path loss as linear amplitude ratio."""
    if distance < 0.01:
        distance = 0.01
    # Friis: Pr/Pt = (lambda / 4*pi*d)^2 — we return amplitude ratio (sqrt)
    return WAVELENGTH / (4 * np.pi * distance)


def _ray_intersects_aabb(origin, direction, box_min, box_max, max_dist):
    """Check if a ray intersects an axis-aligned bounding box."""
    t_min = 0.0
    t_max = max_dist

    for i in range(3):
        if abs(direction[i]) < 1e-10:
            if origin[i] < box_min[i] or origin[i] > box_max[i]:
                return False
        else:
            inv_d = 1.0 / direction[i]
            t1 = (box_min[i] - origin[i]) * inv_d
            t2 = (box_max[i] - origin[i]) * inv_d
            if t1 > t2:
                t1, t2 = t2, t1
            t_min = max(t_min, t1)
            t_max = min(t_max, t2)
            if t_min > t_max:
                return False
    return True


def image_source_reflections(
    tx: np.ndarray, rx: np.ndarray, room: Room, max_order: int = 2
) -> list[Ray]:
    """
    Image-source method for wall reflections.
    Generates virtual sources by mirroring TX across each wall,
    then computes paths from virtual source to RX.
    """
    # Wall planes: (normal, point_on_plane, material)
    walls = [
        (np.array([0, -1, 0]), np.array([0, 0, 0]),           room.wall_material),   # front (y=0)
        (np.array([0,  1, 0]), np.array([0, room.depth, 0]),   room.wall_material),   # back
        (np.array([-1, 0, 0]), np.array([0, 0, 0]),           room.wall_material),   # left (x=0)
        (np.array([1,  0, 0]), np.array([room.width, 0, 0]),  room.wall_material),   # right
        (np.array([0, 0, -1]), np.array([0, 0, 0]),           room.floor_material),  # floor
        (np.array([0, 0,  1]), np.array([0, 0, room.height]), room.ceiling_material),# ceiling
    ]

    rays = []

    # First-order reflections
    for normal, point, mat_key in walls:
        # Mirror TX across wall
        d = np.dot(tx - point, normal)
        tx_mirror = tx - 2 * d * normal

        # Path: TX -> wall reflection point -> RX = TX_mirror -> RX in distance
        path_len = np.linalg.norm(rx - tx_mirror)

        # Reflection coefficient (simplified Fresnel for normal incidence)
        eps_r = RF_MATERIALS[mat_key]["permittivity"]
        R = abs((np.sqrt(eps_r) - 1) / (np.sqrt(eps_r) + 1))

        atten = free_space_loss(path_len) * R
        phase = np.pi  # phase flip on reflection

        rays.append(Ray(path_len, atten, phase, 1))

    # Second-order: mirror each first-order image across other walls
    if max_order >= 2:
        for i, (n1, p1, m1) in enumerate(walls):
            d1 = np.dot(tx - p1, n1)
            tx_m1 = tx - 2 * d1 * n1
            for j, (n2, p2, m2) in enumerate(walls):
                if i == j:
                    continue
                d2 = np.dot(tx_m1 - p2, n2)
                tx_m2 = tx_m1 - 2 * d2 * n2

                path_len = np.linalg.norm(rx - tx_m2)
                eps1 = RF_MATERIALS[m1]["permittivity"]
                eps2 = RF_MATERIALS[m2]["permittivity"]
                R1 = abs((np.sqrt(eps1) - 1) / (np.sqrt(eps1) + 1))
                R2 = abs((np.sqrt(eps2) - 1) / (np.sqrt(eps2) + 1))

                atten = free_space_loss(path_len) * R1 * R2
                phase = 2 * np.pi  # two reflections

                rays.append(Ray(path_len, atten, phase, 2))

    return rays


def synthesize_csi(
    tx_pos: np.ndarray,
    rx_pos: np.ndarray,
    room: Room,
    noise_std: float = 0.01,
    max_reflection_order: int = 2,
) -> np.ndarray:
    """
    Synthesize a CSI vector (complex, per subcarrier) for a TX-RX pair in a room.

    Returns: complex array of shape (SUBCARRIERS,)
    """
    # Collect all paths
    rays = []
    dp = direct_path(tx_pos, rx_pos, room.obstacles)
    if dp:
        rays.append(dp)
    rays.extend(image_source_reflections(tx_pos, rx_pos, room, max_reflection_order))

    # Subcarrier frequencies relative to center
    k = np.arange(SUBCARRIERS) - SUBCARRIERS // 2
    subcarrier_freqs = FREQ_HZ + k * SUBCARRIER_SPACING

    # Sum contributions from all paths at each subcarrier
    csi = np.zeros(SUBCARRIERS, dtype=complex)
    for ray in rays:
        # Phase per subcarrier = 2*pi*f*tau + reflection phase shift
        tau = ray.distance / C  # propagation delay
        phase_per_subcarrier = 2 * np.pi * subcarrier_freqs * tau + ray.phase_shift
        csi += ray.attenuation * np.exp(-1j * phase_per_subcarrier)

    # Add noise
    noise = noise_std * (np.random.randn(SUBCARRIERS) + 1j * np.random.randn(SUBCARRIERS))
    csi += noise

    return csi


def compute_rssi(csi: np.ndarray) -> float:
    """Convert CSI to RSSI (dBm), assuming 20dBm TX power."""
    tx_power_dbm = 20.0
    avg_power = np.mean(np.abs(csi) ** 2)
    if avg_power <= 0:
        return -100.0
    return tx_power_dbm + 10 * np.log10(avg_power)


def generate_dataset(
    room: Room,
    ap_position: np.ndarray,
    grid_spacing: float = 0.1,
    n_samples_per_point: int = 10,
    noise_std: float = 0.01,
) -> dict:
    """
    Generate a full synthetic dataset: CSI from each anchor for a grid of AP positions.

    This is for training: the AP is at ap_position, but we generate data as if
    the AP could be at many positions (for the model to learn from).

    For AP localization specifically, we generate data with AP at one known position
    and anchors receiving.
    """
    # For each anchor, simulate receiving CSI from the AP
    dataset = {
        "ap_position": ap_position.tolist(),
        "anchors": [{"id": a.id, "x": a.x, "y": a.y, "z": a.z} for a in room.anchors],
        "samples": [],
    }

    for _ in range(n_samples_per_point):
        sample = {"csi": {}, "rssi": {}}
        for anchor in room.anchors:
            rx_pos = np.array([anchor.x, anchor.y, anchor.z])
            csi = synthesize_csi(ap_position, rx_pos, room, noise_std)
            sample["csi"][anchor.id] = {
                "real": csi.real.tolist(),
                "imag": csi.imag.tolist(),
            }
            sample["rssi"][anchor.id] = compute_rssi(csi)
        dataset["samples"].append(sample)

    return dataset


def generate_grid_dataset(
    room: Room,
    grid_spacing: float = 0.25,
    ap_height: float = 1.0,
    n_samples_per_point: int = 20,
    noise_std: float = 0.01,
) -> list[dict]:
    """
    Generate CSI data for a grid of possible AP positions.
    Used to build a fingerprint database or train a localization model.
    """
    margin = 0.3  # stay away from walls
    xs = np.arange(margin, room.width - margin + 0.01, grid_spacing)
    ys = np.arange(margin, room.depth - margin + 0.01, grid_spacing)

    all_data = []
    total = len(xs) * len(ys)
    count = 0

    for x in xs:
        for y in ys:
            ap_pos = np.array([x, y, ap_height])

            # Skip if AP is inside an obstacle
            inside = False
            for obs in room.obstacles:
                mn = obs.min_corner
                mx = obs.max_corner
                if mn[0] <= x <= mx[0] and mn[1] <= y <= mx[1] and mn[2] <= ap_height <= mx[2]:
                    inside = True
                    break
            if inside:
                continue

            data = generate_dataset(room, ap_pos, n_samples_per_point=n_samples_per_point,
                                    noise_std=noise_std)
            all_data.append(data)
            count += 1
            if count % 50 == 0:
                print(f"  Generated {count}/{total} grid points...")

    print(f"Total: {len(all_data)} grid points, {len(all_data) * n_samples_per_point} samples")
    return all_data


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Synthetic CSI generator")
    parser.add_argument("--grid_spacing", type=float, default=0.25,
                        help="Grid spacing in meters (default 0.25 = 25cm)")
    parser.add_argument("--samples", type=int, default=20,
                        help="Samples per grid point")
    parser.add_argument("--noise", type=float, default=0.01)
    parser.add_argument("--out", default="synthetic_dataset.json")
    args = parser.parse_args()

    room = create_default_room()
    print(f"Room: {room.width}x{room.depth}x{room.height}m, "
          f"{len(room.obstacles)} obstacles, {len(room.anchors)} anchors")
    print(f"Grid spacing: {args.grid_spacing}m")

    dataset = generate_grid_dataset(
        room,
        grid_spacing=args.grid_spacing,
        n_samples_per_point=args.samples,
        noise_std=args.noise,
    )

    Path(args.out).write_text(json.dumps(dataset))
    print(f"Saved to {args.out}")
