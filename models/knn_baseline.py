"""
Baseline k-NN models for WiFi fingerprinting.
Run after collecting labeled data with the collector.
"""

import sqlite3
import json
import numpy as np
from sklearn.neighbors import KNeighborsRegressor
from sklearn.model_selection import LeaveOneOut, cross_val_score
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

import sys
sys.path.insert(0, "..")
from processing.csi_features import raw_to_complex, amplitude, extract_features


def load_labeled_data(db_path: str) -> tuple[np.ndarray, np.ndarray]:
    """Load labeled CSI readings from the database."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """SELECT csi_raw, rssi, label_x, label_y
           FROM csi_readings
           WHERE label_x IS NOT NULL AND label_y IS NOT NULL"""
    ).fetchall()
    conn.close()

    features = []
    positions = []
    for csi_raw, rssi, x, y in rows:
        feat = extract_features(csi_raw)
        # Feature vector: amplitude per subcarrier + statistical features + RSSI
        fv = np.concatenate([
            feat["amplitude"],
            [feat["amp_mean"], feat["amp_std"], feat["amp_skew"],
             feat["amp_kurtosis"], feat["amp_range"], feat["phase_std"],
             rssi],
        ])
        features.append(fv)
        positions.append([x, y])

    return np.array(features), np.array(positions)


def evaluate_knn(features: np.ndarray, positions: np.ndarray, k: int = 5):
    """Evaluate weighted k-NN with leave-one-out cross-validation."""
    pipeline = Pipeline([
        ("scaler", StandardScaler()),
        ("knn", KNeighborsRegressor(n_neighbors=k, weights="distance")),
    ])

    # Manual LOO to compute distance error
    loo = LeaveOneOut()
    errors = []
    for train_idx, test_idx in loo.split(features):
        pipeline.fit(features[train_idx], positions[train_idx])
        pred = pipeline.predict(features[test_idx])
        true = positions[test_idx]
        err = np.linalg.norm(pred - true, axis=1)
        errors.extend(err.tolist())

    errors = np.array(errors)
    print(f"k={k}: Mean error = {errors.mean():.2f}m, "
          f"Median = {np.median(errors):.2f}m, "
          f"90th pct = {np.percentile(errors, 90):.2f}m")
    return errors


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="wiloc.db")
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()

    X, y = load_labeled_data(args.db)
    print(f"Loaded {len(X)} labeled samples")
    if len(X) < 5:
        print("Need more data! Collect at least 5 labeled positions.")
    else:
        for k in [3, 5, 7, 11]:
            evaluate_knn(X, y, k=k)
