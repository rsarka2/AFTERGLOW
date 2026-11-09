"""position.py's IMU walk: steps x stride along a straight line -> ag/pose src "imu"."""
import itertools

import pytest

from afterglow import config, geo
from afterglow.contracts import POSE, validate
from afterglow.position import ImuWalk, fake_walk

CFG = {"mission": {"origin_lat": 42.35889, "origin_lon": -71.05667},
       "pose": {"carry_height_m": 3.0, "publish_hz": 5},
       "imu": {"line_from": [0.0, 0.0], "line_to": [10.0, 0.0], "stride_m": 0.7, "hall_w_m": 3.0},
       "waypoints": {0: (0.0, 0.0), 2: (6.0, 0.0), 4: (9.0, 3.0), 8: (-3.0, 1.0), 9: (15.0, 0.0)}}


class Collect:
    def __init__(self):
        self.msgs = []

    def publish(self, topic, obj, retain=False):
        assert topic == POSE and validate(topic, obj) == [], obj
        self.msgs.append((topic, obj))


def walk(cfg=CFG):
    bus = Collect()
    return ImuWalk(cfg, bus), bus


def test_first_reading_is_the_zero():
    w, bus = walk()
    p = w.handle_imu(1234, t=1000)
    assert (p["x"], p["y"], p["z"]) == (0.0, 0.0, 3.0) and p["src"] == "imu"
    assert p["steps"] == 0 and p["dist_m"] == 0.0 and p["t"] == 1000
    assert len(bus.msgs) == 1


def test_distance_is_steps_times_stride_and_every_reading_publishes():
    w, bus = walk()
    w.handle_imu(100)
    p = w.handle_imu(110)
    assert p["x"] == pytest.approx(7.0) and p["y"] == 0.0 and p["dist_m"] == pytest.approx(7.0) and p["steps"] == 10
    w.handle_imu(110)  # standing still still publishes, so the dashboard doesn't call the pod lost
    assert len(bus.msgs) == 3 and bus.msgs[-1][1]["x"] == pytest.approx(7.0)


def test_clamped_to_both_ends_of_the_line():
    w, _ = walk()
    w.handle_imu(0)
    assert w.handle_imu(100)["x"] == 10.0  # past line_to
    w.on_ctrl("ag/ctrl", {"cmd": "waypoint", "n": 8})  # before line_from
    assert w.handle_imu(100)["x"] == 0.0
    w.on_ctrl("ag/ctrl", {"cmd": "waypoint", "n": 9})  # past line_to
    assert w.handle_imu(100)["x"] == 10.0


def test_diagonal_line_and_lat_lon():
    cfg = dict(CFG, imu=dict(CFG["imu"], line_from=[1.0, 1.0], line_to=[4.0, 5.0]))  # 5 m long
    w, _ = walk(cfg)
    w.handle_imu(0)
    p = w.handle_imu(5)  # 3.5 m along
    assert p["x"] == pytest.approx(1.0 + 3.5 * 0.6) and p["y"] == pytest.approx(1.0 + 3.5 * 0.8)
    lat, lon = geo.to_latlon(p["x"], p["y"], 42.35889, -71.05667)
    assert p["lat"] == pytest.approx(lat, abs=1e-7) and p["lon"] == pytest.approx(lon, abs=1e-7)


def test_counter_reset_re_zeros_and_keeps_the_distance():
    w, _ = walk()
    w.handle_imu(50)
    assert w.handle_imu(55)["x"] == pytest.approx(3.5)
    p = w.handle_imu(0)  # the sensor reset: no jump back to the start
    assert p["x"] == pytest.approx(3.5) and p["steps"] == 0
    assert w.handle_imu(2)["x"] == pytest.approx(4.9)


def test_waypoint_tap_re_anchors_the_walk():
    w, bus = walk()
    w.handle_imu(20)
    w.handle_imu(24)  # 2.8 m
    w.on_ctrl("ag/ctrl", {"cmd": "waypoint", "n": 2})
    p = bus.msgs[-1][1]
    assert p["x"] == 6.0 and p["src"] == "imu" and p["n"] == 2 and p["steps"] == 0
    assert w.handle_imu(26)["x"] == pytest.approx(7.4)
    w.on_ctrl("ag/ctrl", {"cmd": "waypoint", "n": 4})  # (9, 3) lands on its spot on the line
    assert bus.msgs[-1][1]["x"] == 9.0 and bus.msgs[-1][1]["y"] == 0.0
    w.on_ctrl("ag/ctrl", {"cmd": "waypoint", "n": 0})  # back to the start
    assert bus.msgs[-1][1]["x"] == 0.0
    n = len(bus.msgs)
    w.on_ctrl("ag/ctrl", {"cmd": "waypoint", "n": 99})
    w.on_ctrl("ag/ctrl", {"cmd": "lora_off"})
    assert len(bus.msgs) == n


def test_waypoint_tap_before_the_first_reading():
    w, bus = walk()
    w.on_ctrl("ag/ctrl", {"cmd": "waypoint", "n": 2})
    assert bus.msgs[-1][1]["x"] == 6.0
    w.handle_imu(500)  # becomes the base; the walk starts from waypoint 2
    assert w.handle_imu(501)["x"] == pytest.approx(6.7)


def test_heading_is_the_line_bearing_plus_the_turn():
    w, _ = walk()
    assert "heading" not in w.handle_imu(0)  # unknown yet
    assert w.handle_imu(1, yaw_deg=200.0)["heading"] == 90.0  # line runs east; first yaw is the reference
    assert w.handle_imu(2, yaw_deg=215.0)["heading"] == 105.0
    assert w.handle_imu(3, yaw_deg=170.0)["heading"] == 60.0
    assert w.handle_imu(0, yaw_deg=0.0)["heading"] == 60.0  # sensor reset: new frame, same heading


def test_fake_walk_moves_forward_and_pauses():
    readings = list(itertools.islice(fake_walk(hz=5, seed=1, sleep=lambda s: None), 5 * 60))
    steps = [s for s, _ in readings]
    assert steps == sorted(steps) and 60 < steps[-1] < 140  # about 1.7 steps/s, less the stops
    assert any(a == b for a, b in zip(steps[10:], steps[11:]))
    w, bus = walk()
    for s, yaw in readings:
        w.handle_imu(s, yaw)
    assert bus.msgs[-1][1]["x"] == 10.0 and all(-8 <= y <= 8 for _, y in readings)


def test_both_profiles_have_the_imu_keys():
    for name in ("afterglow.toml", "park.toml"):
        cfg = config.load(config.DEFAULT.parent / name)
        assert cfg["pose"]["source"] in ("gps", "imu", "waypoints")
        assert set(cfg["imu"]) == {"line_from", "line_to", "stride_m", "hall_w_m"}
        w, _ = walk(cfg)
        assert w.handle_imu(0)["src"] == "imu"
    assert config.load(config.DEFAULT)["pose"]["source"] == "imu"
