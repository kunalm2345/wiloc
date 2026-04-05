"""
Wireless Serial Transport — Shared Protocol Definitions

Frame format (binary, big-endian):
    [SYNC_H][SYNC_L][LEN_H][LEN_L][TYPE][PAYLOAD...][CRC8]

    SYNC    = 0xAA55  (2 bytes) — frame synchronization marker
    LEN     = uint16  (2 bytes) — length of PAYLOAD only (0..65535)
    TYPE    = uint8   (1 byte)  — packet type enum
    PAYLOAD = LEN bytes
    CRC8    = uint8   (1 byte)  — CRC-8/MAXIM over [TYPE][PAYLOAD]

Total overhead per frame: 6 bytes (2 sync + 2 len + 1 type + 1 crc)
"""

import struct
from enum import IntEnum

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYNC_WORD = 0xAA55
SYNC_BYTES = struct.pack(">H", SYNC_WORD)  # b'\xaa\x55'

HEADER_SIZE = 5   # SYNC(2) + LEN(2) + TYPE(1)
CRC_SIZE = 1
MIN_FRAME_SIZE = HEADER_SIZE + CRC_SIZE  # 6 bytes, empty payload

MAX_PAYLOAD_SIZE = 4096  # practical limit for BT SPP MTU considerations


class PacketType(IntEnum):
    """Packet type identifiers."""
    CSI_DATA = 0x01    # CSI measurement from ESP32
    RSSI_DATA = 0x02   # RSSI-only measurement (lightweight)
    STATUS = 0x03      # Anchor status / diagnostics
    CMD_ACK = 0x04     # Command acknowledgement from ESP32
    HEARTBEAT = 0x05   # Keep-alive sent periodically
    CMD = 0x10         # Command from Orin Nano to ESP32
    CMD_START = 0x11   # Start CSI capture
    CMD_STOP = 0x12    # Stop CSI capture
    CMD_SET_CHANNEL = 0x13  # Set WiFi channel
    CMD_GET_STATUS = 0x14   # Request status report


# Commands that can be sent as text inside CMD packets
COMMANDS = {
    "start": PacketType.CMD_START,
    "stop": PacketType.CMD_STOP,
    "set_channel": PacketType.CMD_SET_CHANNEL,
    "get_status": PacketType.CMD_GET_STATUS,
}

# ---------------------------------------------------------------------------
# CRC-8 (MAXIM/Dallas 1-Wire, polynomial 0x31)
# ---------------------------------------------------------------------------

_CRC8_TABLE = None


def _build_crc8_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x31) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
        table.append(crc)
    return table


def crc8(data: bytes | bytearray) -> int:
    """Compute CRC-8/MAXIM over *data*. Returns a single byte 0..255."""
    global _CRC8_TABLE
    if _CRC8_TABLE is None:
        _CRC8_TABLE = _build_crc8_table()
    crc = 0x00
    for b in data:
        crc = _CRC8_TABLE[crc ^ b]
    return crc


# ---------------------------------------------------------------------------
# Frame encoding / decoding
# ---------------------------------------------------------------------------

def encode_frame(ptype: int, payload: bytes = b"") -> bytes:
    """Build a complete framed packet ready to send over BT SPP.

    Returns bytes: [SYNC_H][SYNC_L][LEN_H][LEN_L][TYPE][PAYLOAD][CRC8]
    """
    if len(payload) > MAX_PAYLOAD_SIZE:
        raise ValueError(f"Payload too large: {len(payload)} > {MAX_PAYLOAD_SIZE}")
    length = len(payload)
    type_and_payload = bytes([ptype & 0xFF]) + payload
    checksum = crc8(type_and_payload)
    return SYNC_BYTES + struct.pack(">H", length) + type_and_payload + bytes([checksum])


def decode_frame(buf: bytearray) -> tuple[int, bytes, int] | None:
    """Try to decode one frame from the front of *buf*.

    Returns (packet_type, payload, bytes_consumed) on success, or None if
    the buffer does not yet contain a complete valid frame.

    On sync errors the function advances past the bad sync byte so the
    caller can retry.
    """
    while len(buf) >= MIN_FRAME_SIZE:
        # Look for sync word
        if buf[0] != 0xAA or buf[1] != 0x55:
            # Skip one byte and try again
            del buf[0]
            continue

        # We have sync — check if enough data
        if len(buf) < HEADER_SIZE:
            return None

        length = struct.unpack(">H", buf[2:4])[0]
        frame_size = HEADER_SIZE + length + CRC_SIZE

        if len(buf) < frame_size:
            return None  # need more data

        ptype = buf[4]
        payload = bytes(buf[5:5 + length])
        received_crc = buf[5 + length]

        type_and_payload = bytes(buf[4:5 + length])
        expected_crc = crc8(type_and_payload)

        if received_crc != expected_crc:
            # Bad CRC — skip sync and resynchronize
            del buf[:2]
            continue

        # Valid frame — consume it
        del buf[:frame_size]
        return (ptype, payload, frame_size)

    return None


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------

def encode_csi_payload(
    timestamp_ms: int,
    anchor_id: str,
    target_mac: str,
    rssi: int,
    channel: int,
    bandwidth: int,
    csi_data: list[int],
) -> bytes:
    """Pack a CSI_DATA payload.

    Layout:
        timestamp_ms : uint32  (4 bytes)
        anchor_id    : 8 bytes (zero-padded ASCII)
        target_mac   : 6 bytes (raw MAC)
        rssi         : int8    (1 byte, signed)
        channel      : uint8   (1 byte)
        bandwidth    : uint8   (1 byte)
        csi_len      : uint16  (2 bytes)
        csi_data     : csi_len x int8
    """
    mac_bytes = bytes(int(x, 16) for x in target_mac.split(":")) if ":" in target_mac else b"\x00" * 6
    aid = anchor_id.encode("ascii")[:8].ljust(8, b"\x00")
    header = struct.pack(">I", timestamp_ms & 0xFFFFFFFF)
    header += aid
    header += mac_bytes
    header += struct.pack(">bBBH", max(-128, min(127, rssi)), channel & 0xFF, bandwidth & 0xFF, len(csi_data))
    header += bytes(v & 0xFF for v in csi_data)
    return header


def decode_csi_payload(payload: bytes) -> dict:
    """Unpack a CSI_DATA payload into a dict."""
    if len(payload) < 22:
        raise ValueError(f"CSI payload too short: {len(payload)}")
    timestamp_ms = struct.unpack(">I", payload[0:4])[0]
    anchor_id = payload[4:12].rstrip(b"\x00").decode("ascii", errors="replace")
    mac_bytes = payload[12:18]
    target_mac = ":".join(f"{b:02x}" for b in mac_bytes)
    rssi, channel, bandwidth, csi_len = struct.unpack(">bBBH", payload[18:22])
    csi_raw = list(payload[22:22 + csi_len])
    return {
        "timestamp_ms": timestamp_ms,
        "anchor_id": anchor_id,
        "target_mac": target_mac,
        "rssi": rssi,
        "channel": channel,
        "bandwidth": bandwidth,
        "csi_len": csi_len,
        "csi_raw": csi_raw,
    }


def encode_rssi_payload(
    timestamp_ms: int,
    anchor_id: str,
    target_mac: str,
    rssi: int,
    channel: int,
) -> bytes:
    """Pack an RSSI_DATA payload (lightweight, no CSI)."""
    mac_bytes = bytes(int(x, 16) for x in target_mac.split(":")) if ":" in target_mac else b"\x00" * 6
    aid = anchor_id.encode("ascii")[:8].ljust(8, b"\x00")
    return struct.pack(">I", timestamp_ms & 0xFFFFFFFF) + aid + mac_bytes + struct.pack(">bB", rssi, channel)


def decode_rssi_payload(payload: bytes) -> dict:
    """Unpack an RSSI_DATA payload."""
    if len(payload) < 20:
        raise ValueError(f"RSSI payload too short: {len(payload)}")
    timestamp_ms = struct.unpack(">I", payload[0:4])[0]
    anchor_id = payload[4:12].rstrip(b"\x00").decode("ascii", errors="replace")
    target_mac = ":".join(f"{b:02x}" for b in payload[12:18])
    rssi, channel = struct.unpack(">bB", payload[18:20])
    return {
        "timestamp_ms": timestamp_ms,
        "anchor_id": anchor_id,
        "target_mac": target_mac,
        "rssi": rssi,
        "channel": channel,
    }


def encode_status_payload(anchor_id: str, uptime_s: int, free_heap: int, bt_connected: bool, wifi_channel: int) -> bytes:
    """Pack a STATUS payload."""
    aid = anchor_id.encode("ascii")[:8].ljust(8, b"\x00")
    flags = (1 if bt_connected else 0)
    return aid + struct.pack(">IIBb", uptime_s, free_heap, flags, wifi_channel)


def decode_status_payload(payload: bytes) -> dict:
    """Unpack a STATUS payload."""
    if len(payload) < 18:
        raise ValueError(f"Status payload too short: {len(payload)}")
    anchor_id = payload[0:8].rstrip(b"\x00").decode("ascii", errors="replace")
    uptime_s, free_heap, flags, wifi_channel = struct.unpack(">IIBb", payload[8:18])
    return {
        "anchor_id": anchor_id,
        "uptime_s": uptime_s,
        "free_heap": free_heap,
        "bt_connected": bool(flags & 1),
        "wifi_channel": wifi_channel,
    }


def encode_cmd_payload(cmd_text: str) -> bytes:
    """Encode a command string for CMD packets."""
    return cmd_text.encode("utf-8")


def decode_cmd_payload(payload: bytes) -> str:
    """Decode a CMD payload back to a string."""
    return payload.decode("utf-8", errors="replace")


def encode_heartbeat_payload(anchor_id: str, seq: int) -> bytes:
    """Heartbeat payload: anchor_id + sequence number."""
    aid = anchor_id.encode("ascii")[:8].ljust(8, b"\x00")
    return aid + struct.pack(">I", seq & 0xFFFFFFFF)


def decode_heartbeat_payload(payload: bytes) -> dict:
    if len(payload) < 12:
        raise ValueError(f"Heartbeat payload too short: {len(payload)}")
    anchor_id = payload[0:8].rstrip(b"\x00").decode("ascii", errors="replace")
    seq = struct.unpack(">I", payload[8:12])[0]
    return {"anchor_id": anchor_id, "seq": seq}
