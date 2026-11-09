"""Run with:  cd code && python3 -m pytest -q"""
import math
import random

import pytest

from afterglow import alert, geo
from afterglow.bridge import Hasher, convert
from afterglow.contracts import COVERAGE, OBS_BLE, OBS_WIFI, SOS, TARGET, TRACK, validate
from afterglow.position import Position


# ---------- LoRa alert ----------

def sample_target(**kw):
    m = {"t": 1, "pin": "P3", "track": "T7", "x": 412.0, "y": -88.0, "r95": 12.0, "c": 0.82,
         "prio": 2, "cls": "findmy", "status": "new", "state": "localized",
         "flags": {"sos": False, "thermal": True, "radar": False, "camera": True},
         "br": 14, "sos": {"hurt": True, "walk": False, "count": 2}, "last_s": 4,
         "battery": None, "off_trail_m": 46}
    m.update(kw)
    return m


def test_alert_is_16_bytes_and_round_trips():
    m = sample_target()
    pkt = alert.encode(m, version=2)
    assert len(pkt) == 16
    d = alert.decode(pkt)
    assert d["pin"] == "P3" and "track" not in d  # the pin number, as on the dashboard; not the track
    assert d["x"] == 412.0 and d["y"] == -88.0 and d["r95"] == 12.0
    assert d["state"] == "localized" and d["prio"] == 2 and d["cls"] == "findmy"
    assert d["flags"] == m["flags"]
    assert abs(d["c"] - 0.82) < 1 / 255
    assert d["sos"] == {"hurt": True, "walk": False, "count": 2}
    assert d["br"] == 14 and d["last_s"] == 4 and d["off_trail_m"] == 46 and d["battery"] is None


def test_alert_v1_is_decimetres():
    d = alert.decode(alert.encode(sample_target(x=3.4, y=-1.2, r95=1.6), version=1))
    assert (d["x"], d["y"], d["r95"]) == (3.4, -1.2, 1.6)


def test_alert_clamps_out_of_range_values():
    d = alert.decode(alert.encode(sample_target(x=1e9, y=-1e9, r95=999, c=3, last_s=10**9, off_trail_m=999)))
    assert d["x"] == 32767 and d["y"] == -32768 and d["r95"] == 255
    assert d["c"] == 1.0 and d["last_s"] == 65535 and d["off_trail_m"] == 254


def test_alert_random_round_trip():
    rng = random.Random(1)
    for _ in range(500):
        m = sample_target(x=rng.uniform(-3000, 3000), y=rng.uniform(-3000, 3000),
                          r95=rng.uniform(0, 250), c=rng.random(), prio=rng.randint(1, 4))
        d = alert.decode(alert.encode(m))
        assert abs(d["x"] - m["x"]) <= 0.5 and abs(d["y"] - m["y"]) <= 0.5 and d["prio"] == m["prio"]


def test_alert_pin_number_wraps_past_255():
    pins = {p: alert.decode(alert.encode(sample_target(pin=p)))["pin"] for p in ("P1", "P255", "P256", "P600")}
    assert pins == {"P1": "P1", "P255": "P255", "P256": "P1", "P600": "P90"}
    assert alert.decode(alert.encode({"x": 0, "y": 0}, kind=alert.TYPE_HEARTBEAT))["pin"] == "P0"


def test_alert_rejects_wrong_length():
    with pytest.raises(ValueError):
        alert.decode(b"\x00" * 12)


# ---------- geo ----------

def test_geo_round_trip_and_scale():
    lat0, lon0 = 44.3386, -68.2733  # Acadia
    lat, lon = geo.to_latlon(412.0, -88.0, lat0, lon0)
    x, y = geo.to_xy(lat, lon, lat0, lon0)
    assert abs(x - 412.0) < 1e-6 and abs(y + 88.0) < 1e-6
    # 0.001 degree of latitude is about 111 m
    _, y2 = geo.to_xy(lat0 + 0.001, lon0, lat0, lon0)
    assert math.isclose(y2, 111.32, rel_tol=1e-3)


# ---------- bridge ----------

def test_bridge_hashes_and_never_leaks_address():
    h = Hasher("fixed-salt")
    line = '{"type":"ble","addr":"AA:BB:CC:DD:EE:FF","cls":"findmy","rssi":-71}'
    topic, msg = convert(line, "omni", h, t=123)
    assert topic == OBS_BLE and msg["id"] == h("aa:bb:cc:dd:ee:ff") and len(msg["id"]) == 8
    assert "addr" not in msg and "AA:BB" not in str(msg).upper()
    assert validate(topic, msg) == []


def test_bridge_keeps_apple_other_lines():
    # the scanner sends apple_other with REPORT_ALL on; the bridge used to drop it as an unknown class
    line = '{"type":"ble","addr":"AA:BB:CC:DD:EE:01","cls":"apple_other","rssi":-60}'
    topic, msg = convert(line, "omni", Hasher("s"), t=1)
    assert topic == OBS_BLE and msg["cls"] == "apple_other" and validate(topic, msg) == []
    assert alert.decode(alert.encode(sample_target(cls="apple_other")))["cls"] == "apple_other"


def test_bridge_new_salt_changes_ids():
    h = Hasher("a")
    first = h("aa:bb:cc:dd:ee:ff")
    h.new_salt("b")
    assert h("aa:bb:cc:dd:ee:ff") != first


def test_bridge_wifi_sos_and_junk():
    h = Hasher("s")
    topic, msg = convert('{"type":"wifi","addr":"da:a1:19:00:00:01","rand":true,"rssi":-80,"ch":6}', "wifi", h, t=1)
    assert topic == OBS_WIFI and validate(topic, msg) == []
    topic, msg = convert('{"type":"sos","hurt":true,"walk":false,"count":2,"rssi":-55}', "wifi", h, t=1)
    assert topic == SOS and msg["count"] == 2 and validate(topic, msg) == []
    assert convert("garbage{", "omni", h) is None
    assert convert('{"type":"ble"}', "omni", h) is None


# ---------- position ----------

CFG = {"mission": {"origin_lat": 0.0, "origin_lon": 0.0}, "pose": {"carry_height_m": 3.0, "publish_hz": 1000},
       "waypoints": {0: (0.0, 0.0), 3: (9.0, 0.0)}}

GGA = "$GNGGA,141530.00,4420.3160,N,06816.3980,W,1,14,0.9,61.2,M,-28.0,M,,*{}"
RMC = "$GNRMC,141530.00,A,4420.3160,N,06816.3980,W,8.0,87.5,071126,,,A*{}"


def with_checksum(s):
    body = s[1:s.index("*")]
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return s.format(f"{cs:02X}")


class Collect:
    def __init__(self):
        self.msgs = []

    def publish(self, topic, obj, retain=False):
        self.msgs.append((topic, obj))


def test_position_from_nmea():
    bus = Collect()
    p = Position(CFG, bus)
    assert p.handle_nmea(with_checksum(RMC)) is None
    pose = p.handle_nmea(with_checksum(GGA), t=1000)
    assert pose is not None and validate("ag/pose", pose) == []
    assert pose["x"] == 0.0 and pose["y"] == 0.0  # first fix becomes the origin
    assert abs(pose["lat"] - 44.3386) < 1e-4 and pose["lon"] < 0
    assert pose["sats"] == 14 and pose["fix"] == 1 and pose["src"] == "gps"
    assert abs(pose["speed_mps"] - 8.0 * 0.514444) < 0.01 and pose["course"] == 87.5


def test_position_ignores_no_fix_and_bad_checksum():
    p = Position(CFG, Collect())
    assert p.handle_nmea(with_checksum("$GNGGA,141530.00,,,,,0,00,99.9,,,,,,*{}")) is None
    assert p.no_fix == 1
    assert p.handle_nmea(GGA.format("00")) is None


def test_position_waypoint_tap():
    bus = Collect()
    p = Position(CFG, bus)
    p.on_ctrl("ag/ctrl", {"cmd": "waypoint", "n": 3})
    topic, pose = bus.msgs[-1]
    assert topic == "ag/pose" and (pose["x"], pose["y"], pose["z"]) == (9.0, 0.0, 3.0) and pose["src"] == "waypoint"


def test_target_contract_example_validates():
    m = sample_target()
    assert validate(TARGET, m) == []


def test_target_needs_state_and_last_s():
    m = sample_target()
    del m["state"], m["last_s"]
    assert any("missing" in p for p in validate(TARGET, m))
    assert validate(TARGET, sample_target(state="found")) != []
    assert validate(TARGET, sample_target(state="confirmed")) == []


def test_sos_only_pin_validates():
    m = sample_target(track="T0", rule="sos", status="new", state="heard", cls="wifi", c=0, last_s=0, r95=30.0, prio=2)
    assert validate(TARGET, m) == []
    assert alert.decode(alert.encode(m, kind=alert.TYPE_SOS))["kind"] == alert.TYPE_SOS
    assert validate(TARGET, sample_target(rule="sso")) != []


def test_track_can_never_be_confirmed():
    track = {"t": 1, "track": "T1", "cls": "findmy", "x": 0, "y": 0, "r95": 5, "state": "pinned", "last_s": 0}
    assert validate(TRACK, track) == []
    assert validate(TRACK, {**track, "state": "confirmed"}) != []  # only the ranger confirms


def test_coverage_grid_size_must_match():
    cov = {"t": 1, "x0": -2, "y0": -5, "cell_m": 1, "nx": 3, "ny": 2,
           "covered": [0, 1, 1, 0, 0, 1], "p": [0, 12, 255, 0, 0, 3], "pct": 0.5}
    assert validate(COVERAGE, cov) == []
    assert validate(COVERAGE, {**cov, "p": [0, 1]}) != []


def test_battery_null_is_unknown_on_the_wire():
    pkt = alert.encode(sample_target(battery=None))
    assert pkt[14] == 255 and alert.decode(pkt)["battery"] is None
    assert alert.decode(alert.encode(sample_target(battery=73)))["battery"] == 73
