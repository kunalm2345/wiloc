"""
AP Localizer — find the precise location of a single AP using 4 ESP32 anchors.

Three methods, increasing in sophistication:
1. RSSI-based trilateration (baseline, ~1-2m accuracy)
2. CSI phase-based ranging + trilateration (~10-30cm accuracy)
3. CSI fingerprint matching against simulated database (~10-20cm accuracy)

The key insight for 10cm accuracy: CSI phase difference across subcarriers
encodes time-of-flight (ToF), which gives distance. With 4 known anchor
positions and 4 distances, you get a heavily overdetermined system.
"""

import json
import numpy as np
from scipy.optimize import least_squares, minimize
from pathlib import Path

import sys
sys.path.insert(0, "..")
from processing.csi_features import raw_to_complex, amplitude, sanitize_phase


# ──────────────────────────────────────────────
# Method 1: RSSI-based trilateration
# ──────────────────────────────────────────────

def rssi_to_distance(rssi_dbm: float, tx_power_dbm: float = 20.0,
                     path_loss_exp: float = 2.0, ref_dist: float = 1.0) -> float:
    """
    Convert RSSI to distance using log-distance path loss model.
    d = ref_dist * 10^((tx_power - rssi) / (10 * n))

    path_loss_exp: 2.0 = free space, 2.5-3.5 = indoor with obstacles
    """
    return ref_dist * 10 ** ((tx_power_dbm - rssi_dbm) / (10 * path_loss_exp))


def trilaterate_lsq(anchors: np.ndarray, distances: np.ndarray) -> np.ndarray:
    """
    Least-squares trilateration.
    anchors: (N, 2) or (N, 3) — known positions of N anchors
    distances: (N,) — estimated distance from each anchor to AP

    Returns: estimated (x, y) or (x, y, z) position
    """
    dim = anchors.shape[1]

    def residuals(pos):
        pred_dists = np.linalg.norm(anchors - pos, axis=1)
        return pred_dists - distances

    # Initial guess: centroid of anchors
    x0 = anchors.mean(axis=0)
    result = least_squares(residuals, x0, method='lm')
    return result.x


def localize_rssi(anchor_positions: np.ndarray, rssi_values: np.ndarray,
                  path_loss_exp: float = 2.5) -> np.ndarray:
    """
    Full RSSI-based localization pipeline.
    anchor_positions: (4, 2) or (4, 3)
    rssi_values: (4,) — RSSI from each anchor in dBm
    """
    distances = np.array([rssi_to_distance(r, path_loss_exp=path_loss_exp) for r in rssi_values])
    return trilaterate_lsq(anchor_positions, distances)


# ──────────────────────────────────────────────
# Method 2: CSI phase-based ranging
# ──────────────────────────────────────────────

def csi_phase_to_distance(csi_complex: np.ndarray,
                          subcarrier_spacing: float = 312.5e3) -> float:
    """
    Extract distance from CSI phase slope across subcarriers.

    The phase of CSI across subcarriers is:
        phi(k) = -2*pi*f_k*tau + phi_0
    where tau = distance/c is the time of flight.

    The slope of phase vs subcarrier index gives tau:
        d(phi)/d(k) = -2*pi * subcarrier_spacing * tau
    So:
        tau = -slope / (2*pi * subcarrier_spacing)
        distance = tau * c
    """
    C = 3e8
    phases = np.unwrap(np.angle(csi_complex))

    # Fit linear model to phase vs subcarrier index
    k = np.arange(len(phases))
    # Weighted fit — weight by amplitude (strong subcarriers = more reliable phase)
    weights = np.abs(csi_complex)
    weights = weights / weights.sum()

    # Weighted linear regression
    k_mean = np.average(k, weights=weights)
    p_mean = np.average(phases, weights=weights)
    slope = (np.average(k * phases, weights=weights) - k_mean * p_mean) / \
            (np.average(k ** 2, weights=weights) - k_mean ** 2)

    tau = -slope / (2 * np.pi * subcarrier_spacing)

    # Distance can't be negative (phase wrapping artifacts)
    distance = abs(tau * C)

    return distance


def csi_phase_to_distance_music(csi_complex: np.ndarray,
                                 subcarrier_spacing: float = 312.5e3,
                                 search_range_m: float = 10.0,
                                 resolution_m: float = 0.01) -> float:
    """
    MUSIC algorithm for super-resolution distance estimation from CSI.
    More accurate than linear phase fitting, especially with multipath.
    """
    C = 3e8
    N = len(csi_complex)

    # Spatial smoothing for decorrelation
    L = N // 2  # smoothing window
    M = N - L + 1

    # Build smoothed covariance matrix
    R = np.zeros((L, L), dtype=complex)
    for i in range(M):
        segment = csi_complex[i:i+L]
        R += np.outer(segment, segment.conj())
    R /= M

    # Eigendecomposition
    eigenvalues, eigenvectors = np.linalg.eigh(R)

    # Sort by eigenvalue (descending)
    idx = np.argsort(eigenvalues)[::-1]
    eigenvectors = eigenvectors[:, idx]

    # Signal subspace = 1 (single direct path dominant)
    # Noise subspace = remaining eigenvectors
    n_signal = 1
    noise_subspace = eigenvectors[:, n_signal:]

    # MUSIC spectrum: scan over distances
    distances = np.arange(0.01, search_range_m, resolution_m)
    spectrum = np.zeros(len(distances))

    for di, d in enumerate(distances):
        tau = d / C
        # Steering vector
        k = np.arange(L)
        a = np.exp(-1j * 2 * np.pi * subcarrier_spacing * k * tau)

        # MUSIC pseudospectrum
        noise_proj = noise_subspace @ noise_subspace.conj().T @ a
        denom = np.abs(np.vdot(a, noise_proj))
        spectrum[di] = 1.0 / (denom + 1e-12)

    # Peak = estimated distance
    best_idx = np.argmax(spectrum)
    return distances[best_idx]


def localize_csi_phase(anchor_positions: np.ndarray,
                       csi_per_anchor: list[np.ndarray],
                       method: str = "music") -> tuple[np.ndarray, np.ndarray]:
    """
    CSI phase-based AP localization.

    anchor_positions: (4, 2) or (4, 3)
    csi_per_anchor: list of 4 complex CSI arrays
    method: "linear" for phase slope, "music" for MUSIC algorithm

    Returns: (estimated_position, estimated_distances)
    """
    if method == "music":
        distances = np.array([csi_phase_to_distance_music(csi) for csi in csi_per_anchor])
    else:
        distances = np.array([csi_phase_to_distance(csi) for csi in csi_per_anchor])

    position = trilaterate_lsq(anchor_positions, distances)
    return position, distances


# ──────────────────────────────────────────────
# Method 3: CSI fingerprint matching
# ──────────────────────────────────────────────

def build_fingerprint_db(dataset: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """
    Build fingerprint database from synthetic dataset.
    dataset: output of csi_simulator.generate_grid_dataset()

    Returns: (features, positions) where
        features: (N, n_features) — averaged CSI amplitudes from all anchors
        positions: (N, 2) — x,y positions
    """
    features = []
    positions = []

    for entry in dataset:
        ap_pos = entry["ap_position"]
        positions.append(ap_pos[:2])  # x, y only

        # Average CSI amplitude across samples for each anchor
        anchor_ids = sorted(entry["samples"][0]["csi"].keys())
        entry_features = []

        for sample in entry["samples"]:
            sample_feat = []
            for aid in anchor_ids:
                real = np.array(sample["csi"][aid]["real"])
                imag = np.array(sample["csi"][aid]["imag"])
                csi_c = real + 1j * imag
                sample_feat.extend(np.abs(csi_c).tolist())
                sample_feat.append(sample["rssi"][aid])
            entry_features.append(sample_feat)

        # Average across samples
        avg_feat = np.mean(entry_features, axis=0)
        features.append(avg_feat)

    return np.array(features), np.array(positions)


def localize_fingerprint(query_csi: dict, query_rssi: dict,
                         fp_features: np.ndarray, fp_positions: np.ndarray,
                         k: int = 5) -> np.ndarray:
    """
    Localize AP by matching CSI fingerprint to database.

    query_csi: {anchor_id: {"real": [...], "imag": [...]}}
    query_rssi: {anchor_id: float}
    """
    anchor_ids = sorted(query_csi.keys())

    # Build query feature vector (same format as database)
    query_feat = []
    for aid in anchor_ids:
        real = np.array(query_csi[aid]["real"])
        imag = np.array(query_csi[aid]["imag"])
        csi_c = real + 1j * imag
        query_feat.extend(np.abs(csi_c).tolist())
        query_feat.append(query_rssi[aid])
    query_feat = np.array(query_feat)

    # Normalize
    fp_norm = fp_features / (np.linalg.norm(fp_features, axis=1, keepdims=True) + 1e-10)
    q_norm = query_feat / (np.linalg.norm(query_feat) + 1e-10)

    # Cosine similarity
    similarities = fp_norm @ q_norm
    top_k = np.argsort(similarities)[-k:]

    # Weighted average of top-k positions
    weights = similarities[top_k]
    weights = weights / weights.sum()
    estimated_pos = np.average(fp_positions[top_k], weights=weights, axis=0)

    return estimated_pos


# ──────────────────────────────────────────────
# Combined estimator (fuses all methods)
# ──────────────────────────────────────────────

def localize_fused(anchor_positions: np.ndarray,
                   csi_per_anchor: list[np.ndarray],
                   rssi_per_anchor: np.ndarray,
                   fp_features: np.ndarray = None,
                   fp_positions: np.ndarray = None,
                   csi_dict: dict = None,
                   rssi_dict: dict = None) -> dict:
    """
    Fuse all localization methods for best accuracy.
    Returns dict with each method's estimate and fused estimate.
    """
    results = {}

    # Method 1: RSSI
    pos_rssi = localize_rssi(anchor_positions[:, :2], rssi_per_anchor, path_loss_exp=2.0)
    results["rssi"] = pos_rssi

    # Method 2: CSI phase (both variants)
    pos_linear, dists_linear = localize_csi_phase(anchor_positions[:, :2], csi_per_anchor, "linear")
    pos_music, dists_music = localize_csi_phase(anchor_positions[:, :2], csi_per_anchor, "music")
    results["csi_linear"] = pos_linear
    results["csi_music"] = pos_music

    # Method 3: Fingerprint (if database provided)
    if fp_features is not None and csi_dict is not None and rssi_dict is not None:
        pos_fp = localize_fingerprint(csi_dict, rssi_dict, fp_features, fp_positions)
        results["fingerprint"] = pos_fp

    # Weighted fusion — CSI MUSIC gets highest weight
    weights = {"rssi": 0.1, "csi_linear": 0.2, "csi_music": 0.5}
    if "fingerprint" in results:
        weights["fingerprint"] = 0.2

    total_w = sum(weights.values())
    fused = np.zeros(2)
    for method, w in weights.items():
        if method in results:
            fused += (w / total_w) * results[method][:2]
    results["fused"] = fused

    return results


# ──────────────────────────────────────────────
# Evaluation on synthetic data
# ──────────────────────────────────────────────

def evaluate_synthetic(room=None):
    """Run localization on synthetic data to validate algorithms."""
    # Import here to avoid circular dependency when used standalone
    sys.path.insert(0, str(Path(__file__).parent.parent / "simulator"))
    from csi_simulator import synthesize_csi, compute_rssi

    if room is None:
        from room_model import create_default_room
        room = create_default_room()

    anchor_positions = np.array([[a.x, a.y] for a in room.anchors])

    # Test AP at various positions
    test_positions = [
        np.array([2.0, 2.0, 1.0]),   # center
        np.array([1.0, 1.0, 1.0]),   # near corner
        np.array([3.0, 1.5, 1.0]),   # off center
        np.array([1.5, 3.0, 1.0]),   # near back
        np.array([2.5, 2.5, 1.0]),   # between table and almirah
    ]

    print("=" * 70)
    print("AP LOCALIZATION — SYNTHETIC EVALUATION")
    print("=" * 70)
    print(f"Room: {room.width}x{room.depth}m, {len(room.anchors)} anchors")
    print(f"Anchors: {[(a.id, a.x, a.y) for a in room.anchors]}")
    print()

    for true_pos in test_positions:
        # Generate CSI from each anchor
        csi_list = []
        rssi_list = []
        csi_dict = {}
        rssi_dict = {}

        for anchor in room.anchors:
            rx = np.array([anchor.x, anchor.y, anchor.z])
            # Average multiple samples for stability
            csi_samples = [synthesize_csi(true_pos, rx, room, noise_std=0.005) for _ in range(20)]
            csi_avg = np.mean(csi_samples, axis=0)
            csi_list.append(csi_avg)
            rssi_val = compute_rssi(csi_avg)
            rssi_list.append(rssi_val)

            csi_dict[anchor.id] = {
                "real": csi_avg.real.tolist(),
                "imag": csi_avg.imag.tolist(),
            }
            rssi_dict[anchor.id] = rssi_val

        rssi_arr = np.array(rssi_list)

        # Localize
        results = localize_fused(
            np.array([[a.x, a.y] for a in room.anchors]),
            csi_list, rssi_arr,
        )

        print(f"True AP position: ({true_pos[0]:.2f}, {true_pos[1]:.2f})")
        for method, est in results.items():
            err = np.linalg.norm(est[:2] - true_pos[:2])
            print(f"  {method:15s}: ({est[0]:.3f}, {est[1]:.3f})  error = {err:.3f}m "
                  f"({err*100:.1f}cm)")
        print()


if __name__ == "__main__":
    evaluate_synthetic()
