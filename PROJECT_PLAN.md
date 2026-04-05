# WiLoc - WiFi Indoor Localization System

## Overview
Indoor localization system using WiFi CSI (Channel State Information) and RSSI,
with reinforcement learning for sim-to-real position prediction.

## Hardware
- 5x ESP32 (CSI extraction)
- 3x Raspberry Pi (data collection / edge processing)
- 1x NVIDIA Orin Nano (model training / inference)
- 1x GPS-grade antenna (outdoor ground truth calibration)

## Architecture

```
[Mobile ESP32] --WiFi frames--> [Anchor ESP32s]
                                      |
                                  USB Serial
                                      |
                                 [Raspberry Pis]
                                      |
                                   Network
                                      |
                                [Orin Nano] -- Model Inference --> Position Estimate
```

## Phases

### Phase 1: Data Collection (Weeks 1-2)
- Flash ESP32s with ESP-CSI firmware
- Deploy 3-4 anchors + 1 mobile node
- RPi collection pipeline (serial -> SQLite -> sync)
- Ground truth fingerprint grid (1-2m spacing)

### Phase 2: Signal Processing (Weeks 2-3)
- CSI amplitude/phase extraction
- Phase sanitization (CFO/SFO removal)
- Feature engineering (statistical, temporal)
- RSSI path-loss model fitting

### Phase 3: Baseline Models (Weeks 3-4)
- Weighted k-NN on RSSI fingerprints
- k-NN on CSI amplitude fingerprints
- CNN on CSI images (subcarrier x time)
- LSTM/Transformer on CSI sequences

### Phase 4: Simulation + RL (Weeks 4-8)
- Ray-tracing simulator (Sionna or custom)
- Simulated CSI/RSSI generation
- RL agent: state=observation, action=position adjustment, reward=-error
- Domain randomization + sim-to-real transfer

### Phase 5: Real-Time System (Weeks 6-8)
- End-to-end pipeline on Orin Nano
- Target: <1m accuracy, <500ms latency
- Web UI for floor plan visualization

## Directory Structure
```
wiloc/
  firmware/          # ESP32 firmware (ESP-IDF + CSI)
  collector/         # RPi data collection scripts
  processing/        # Signal processing & feature extraction
  models/            # ML/RL models
  simulator/         # WiFi environment simulator
  evaluation/        # Accuracy benchmarks & visualization
  webapp/            # Real-time visualization UI
  data/              # Collected datasets (gitignored)
  configs/           # Hardware & experiment configs
```
