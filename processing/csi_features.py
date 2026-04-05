"""
CSI Feature Extraction — converts raw CSI data into ML-ready features.
"""

import json
import numpy as np


def raw_to_complex(csi_raw: str) -> np.ndarray:
    """Convert raw CSI string (interleaved imag/real) to complex array."""
    values = json.loads(csi_raw)
    # ESP32 CSI format: [imag0, real0, imag1, real1, ...]
    imag = np.array(values[0::2], dtype=np.float64)
    real = np.array(values[1::2], dtype=np.float64)
    return real + 1j * imag


def amplitude(csi_complex: np.ndarray) -> np.ndarray:
    """CSI amplitude per subcarrier."""
    return np.abs(csi_complex)


def phase(csi_complex: np.ndarray) -> np.ndarray:
    """CSI phase per subcarrier (raw, unsanitized)."""
    return np.angle(csi_complex)


def sanitize_phase(phases: np.ndarray) -> np.ndarray:
    """
    Remove CFO/SFO-induced linear phase offset.
    Fits a line to the phase across subcarriers and subtracts it.
    """
    n = len(phases)
    x = np.arange(n)
    # Unwrap phase first
    unwrapped = np.unwrap(phases)
    # Linear fit: phase = a*subcarrier + b
    coeffs = np.polyfit(x, unwrapped, 1)
    linear_component = np.polyval(coeffs, x)
    return unwrapped - linear_component


def extract_features(csi_raw: str) -> dict:
    """Extract a full feature dictionary from a raw CSI string."""
    csi_c = raw_to_complex(csi_raw)
    amp = amplitude(csi_c)
    ph = sanitize_phase(phase(csi_c))

    return {
        "amplitude": amp,
        "phase_sanitized": ph,
        "amp_mean": np.mean(amp),
        "amp_std": np.std(amp),
        "amp_skew": float(_skewness(amp)),
        "amp_kurtosis": float(_kurtosis(amp)),
        "amp_max": np.max(amp),
        "amp_min": np.min(amp),
        "amp_range": np.max(amp) - np.min(amp),
        "phase_std": np.std(ph),
        "n_subcarriers": len(csi_c),
    }


def _skewness(x: np.ndarray) -> float:
    m = np.mean(x)
    s = np.std(x)
    if s == 0:
        return 0.0
    return np.mean(((x - m) / s) ** 3)


def _kurtosis(x: np.ndarray) -> float:
    m = np.mean(x)
    s = np.std(x)
    if s == 0:
        return 0.0
    return np.mean(((x - m) / s) ** 4) - 3.0
