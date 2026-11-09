"""tools/pluto_rx.py: the Pluto receiver prints lines lora_rx.py accepts. The decode itself needs
GNU Radio (conda env "sdr"): run `python tools/pluto_rx.py --selftest` there."""
import io
import sys
from pathlib import Path

import pytest

from afterglow import alert, config
from afterglow.contracts import ALERT, validate
from afterglow.lora_rx import LoraRx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import pluto_rx  # noqa: E402

LCFG = config.load()["lora"]


class Collect:
    def __init__(self):
        self.out = []

    def publish(self, topic, msg, retain=False):
        self.out.append((topic, msg))


def serialized_symbol(body: bytes) -> bytes:
    return bytes([pluto_rx.PST_SYMBOL]) + len(body).to_bytes(2, "big") + body


def test_symbol_bytes_keeps_every_byte_value():
    body = bytes(range(256))[:200]
    assert pluto_rx.symbol_bytes(serialized_symbol(body)) == body
    assert pluto_rx.symbol_bytes(b"\x03\x00\x01x") is None      # not a symbol
    assert pluto_rx.symbol_bytes(serialized_symbol(b"abc")[:-1]) is None  # truncated


def test_lines_go_through_lora_rx_stdin():
    packet = bytes([LCFG["laptop_node"], LCFG["pod_node"], 7, 0]) + alert.encode(
        {"x": 1.5, "y": -2.0}, version=1, kind=alert.TYPE_HEARTBEAT)
    bus = Collect()
    rx = LoraRx(bus, LCFG)
    rx.read_lines(io.StringIO(pluto_rx.cfg_line(LCFG) + "\n" + pluto_rx.rx_line(packet) + "\n"))
    assert rx.radio_ok is True
    assert [t for t, _ in bus.out] == [ALERT]
    msg = bus.out[0][1]
    assert validate(ALERT, msg) == []
    assert msg["kind"] == alert.TYPE_HEARTBEAT and msg["rssi"] is None and (msg["x"], msg["y"]) == (1.5, -2.0)


def test_mismatched_settings_stop_lora_rx():
    bad = dict(LCFG, sf=LCFG["sf"] + 2)
    rx = LoraRx(Collect(), LCFG)
    with pytest.raises(SystemExit):
        rx.read_lines(io.StringIO(pluto_rx.cfg_line(bad) + "\n"))


def test_lora_params_map_the_config():
    p = pluto_rx.lora_params(LCFG, 1_000_000)
    assert p["cr"] == LCFG["cr"] - 4 and p["sf"] == LCFG["sf"] and p["sync_word"] == [0x12]
    with pytest.raises(SystemExit):
        pluto_rx.lora_params(LCFG, 1_000_001)


def test_selftest_decodes_a_heartbeat():
    pytest.importorskip("gnuradio.lora_sdr")
    assert pluto_rx.selftest(LCFG, 1_000_000) == 0
