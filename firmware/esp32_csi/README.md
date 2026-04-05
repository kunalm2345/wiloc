# ESP32 CSI Firmware

## Prerequisites
- ESP-IDF v5.x (https://docs.espressif.com/projects/esp-idf/en/latest/)
- ESP-CSI component (https://github.com/espressif/esp-csi)

## Setup
```bash
# Install ESP-IDF
git clone --recursive https://github.com/espressif/esp-idf.git
cd esp-idf && ./install.sh && . ./export.sh

# Clone ESP-CSI into components
cd /path/to/wiloc/firmware/esp32_csi
git clone https://github.com/espressif/esp-csi.git components/esp-csi
```

## Flash
```bash
idf.py set-target esp32
idf.py build
idf.py -p /dev/ttyUSB0 flash monitor
```

## CSI Output Format
Each line over serial:
```
CSI_DATA,<timestamp>,<mac>,<rssi>,<channel>,<secondary_channel>,<sig_mode>,<bandwidth>,<len>,<data[0]>,<data[1]>,...
```
- data[] = interleaved imaginary/real parts per subcarrier
- 52 subcarriers for 20MHz HT, 114 for 40MHz HT
