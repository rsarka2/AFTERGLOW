"""The 16-byte LoRa alert (docs/CONTRACTS.md, "LoRa alert"). Pack on the pod, unpack on the laptop.

    pkt = encode(target_dict)          # bytes, len 16
    msg = decode(pkt)                  # dict with the same field names

Version 1 = decimetres (indoor demo), version 2 = metres (park scale).
"""
import struct

from .contracts import CLS_CODES, STATES

FMT = "<BBhhBBBBBBHBB"
SIZE = struct.calcsize(FMT)  # 16
assert SIZE == 16

TYPE_TRACK, TYPE_SOS, TYPE_CONFIRMED, TYPE_HEARTBEAT = 1, 2, 3, 4
FLAG_BITS = {"sos": 4, "thermal": 5, "radar": 6, "camera": 7}
CLS_NAMES = {v: k for k, v in CLS_CODES.items()}
SCALE = {1: 10.0, 2: 1.0}  # units per metre


def _clamp(v, lo, hi):
    return max(lo, min(hi, int(round(v))))


def _pin_num(pin) -> int:
    """'P7' -> 7. Past 255 it wraps to 1 (P256 -> 1); 0 is the heartbeat, which has no pin."""
    if isinstance(pin, str):
        pin = "".join(ch for ch in pin if ch.isdigit()) or "0"
    n = max(0, int(pin))
    return (n - 1) % 255 + 1 if n > 255 else n


def encode(m: dict, version: int = 2, kind: int = TYPE_TRACK) -> bytes:
    s = SCALE[version]
    state = STATES.index(m.get("state", "heard")) if m.get("state", "heard") in STATES else 0
    flags = m.get("flags") or {}
    sf = state & 0x0F
    for name, bit in FLAG_BITS.items():
        if flags.get(name):
            sf |= 1 << bit
    prio = _clamp(m.get("prio", 4), 1, 4)
    cls = CLS_CODES.get(m.get("cls", "other"), 0)
    sos = m.get("sos") or {}
    sos_byte = (1 if sos.get("hurt") else 0) | (2 if sos.get("walk") else 0) | (_clamp(sos.get("count", 0), 0, 63) << 2)
    off = m.get("off_trail_m")
    batt = m.get("battery")
    return struct.pack(
        FMT,
        ((version & 0x0F) << 4) | (kind & 0x0F),
        _pin_num(m.get("pin") or 0),
        _clamp(m["x"] * s, -32768, 32767),
        _clamp(m["y"] * s, -32768, 32767),
        _clamp(m.get("r95", 0) * s, 0, 255),
        sf,
        _clamp(m.get("c", 0) * 255, 0, 255),
        (prio << 4) | cls,
        _clamp(m.get("br") or 0, 0, 255),
        sos_byte,
        _clamp(m.get("last_s", 0), 0, 65535),
        255 if batt is None else _clamp(batt, 0, 100),
        255 if off is None else _clamp(off, 0, 254),
    )


def decode(pkt: bytes) -> dict:
    if len(pkt) != SIZE:
        raise ValueError(f"alert must be {SIZE} bytes, got {len(pkt)}")
    (vt, pin, x, y, r95, sf, conf, pc, br, sos, last_s, batt, off) = struct.unpack(FMT, pkt)
    version, kind = vt >> 4, vt & 0x0F
    if version not in SCALE:
        raise ValueError(f"unknown alert version {version}")
    s = SCALE[version]
    state = sf & 0x0F
    return {
        "version": version,
        "kind": kind,
        "pin": f"P{pin}",
        "x": x / s,
        "y": y / s,
        "r95": r95 / s,
        "state": STATES[state] if state < len(STATES) else "heard",
        "flags": {name: bool(sf & (1 << bit)) for name, bit in FLAG_BITS.items()},
        "c": conf / 255,
        "prio": pc >> 4,
        "cls": CLS_NAMES.get(pc & 0x0F, "other"),
        "br": br or None,
        "sos": {"hurt": bool(sos & 1), "walk": bool(sos & 2), "count": sos >> 2},
        "last_s": last_s,
        "battery": None if batt == 255 else batt,
        "off_trail_m": None if off == 255 else off,
    }
