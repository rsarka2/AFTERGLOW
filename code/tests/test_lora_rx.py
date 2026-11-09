"""lora_rx.py: XIAO #3 lines -> ag/alert, dropping other teams' packets; sketch matches the config."""
import json
import re
from pathlib import Path

from afterglow import alert, config
from afterglow.contracts import ALERT, validate
from afterglow.lora_rx import RADIO_KEYS, LoraRx

LCFG = config.load()["lora"]
SKETCH = Path(__file__).resolve().parents[2] / "firmware" / "lora_rx" / "lora_rx.ino"


class Collect:
    def __init__(self):
        self.out = []

    def publish(self, topic, msg, retain=False):
        self.out.append((topic, msg))


def target(**kw):
    m = {"pin": "P3", "track": "T1", "x": 18.0, "y": 9.0, "r95": 12.0, "c": 0.82, "prio": 2, "cls": "findmy",
         "state": "localized", "last_s": 4, "flags": {"thermal": True}, "off_trail_m": 46}
    m.update(kw)
    return m


def rx_line(payload: bytes, to=None, frm=None, rssi=-71, snr=9.5) -> str:
    header = bytes([LCFG["laptop_node"] if to is None else to, LCFG["pod_node"] if frm is None else frm, 7, 0])
    return json.dumps({"type": "rx", "rssi": rssi, "snr": snr, "hex": (header + payload).hex()})


def run(*lines):
    bus = Collect()
    rx = LoraRx(bus, LCFG)
    for line in lines:
        rx.handle_line(line, t=1731000000123)
    return rx, bus.out


def test_pod_alert_becomes_ag_alert():
    _, out = run(rx_line(alert.encode(target())))
    assert len(out) == 1
    topic, msg = out[0]
    assert topic == ALERT and validate(topic, msg) == []
    assert msg["pin"] == "P3" and msg["x"] == 18.0 and msg["prio"] == 2 and msg["cls"] == "findmy"
    assert msg["rssi"] == -71 and msg["snr"] == 9.5 and msg["kind"] == alert.TYPE_TRACK


def test_heartbeat_passes():
    _, out = run(rx_line(alert.encode({"x": 0, "y": 0}, kind=alert.TYPE_HEARTBEAT)))
    assert out and out[0][1]["kind"] == alert.TYPE_HEARTBEAT


def test_other_teams_and_bad_packets_are_dropped():
    good = alert.encode(target())
    rx, out = run(
        rx_line(good, frm=0x01),                 # someone else's sender
        rx_line(good, to=0xFF),                  # broadcast, not to us
        rx_line(good[:12]),                      # wrong length
        rx_line(bytes([0x91]) + good[1:]),       # unknown alert version 9
        json.dumps({"type": "rx", "hex": "zz"}),  # not hex
        json.dumps({"type": "rx", "hex": "a2a1"}),  # shorter than the header
        "ESP-ROM:esp32s3-20210327",              # boot chatter
    )
    assert out == []
    assert rx.stats["not_ours"] == 2 and rx.stats["bad_alert"] == 2
    assert rx.stats["bad_hex"] == 1 and rx.stats["short"] == 1 and rx.stats["junk"] == 1


def board_cfg(**kw):
    d = {"type": "cfg", "freq": LCFG["freq_mhz"], "sf": LCFG["sf"], "bw": LCFG["bw_hz"],
         "cr": LCFG["cr"], "pre": LCFG["preamble"]}
    d.update(kw)
    return json.dumps(d)


def test_radio_settings_check():
    rx, _ = run(board_cfg())
    assert rx.radio_ok is True
    rx, _ = run(board_cfg(sf=LCFG["sf"] + 2))
    assert rx.radio_ok is False


def test_sketch_matches_config():
    text = SKETCH.read_text()
    defines = dict(re.findall(r"^#define (\w+) ([\d.]+)", text, re.M))
    sketch_names = {"freq_mhz": "FREQ_MHZ", "sf": "SF", "bw_hz": "BW_HZ", "cr": "CR", "preamble": "PREAMBLE"}
    assert set(sketch_names) == set(RADIO_KEYS.values())
    for key, name in sketch_names.items():
        assert float(defines[name]) == float(LCFG[key]), f"{name} in lora_rx.ino != [lora] {key}"
