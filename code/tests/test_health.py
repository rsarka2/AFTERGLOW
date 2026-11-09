"""health.py: ag/sys from the Pi and from what's on the bus; never touches the hardware."""
from afterglow import config, health
from afterglow.contracts import OBS_BLE, OBS_WIFI, POSE, SYS, THERMAL, validate


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Collect:
    def __init__(self):
        self.out = []

    def publish(self, topic, msg, retain=False):
        self.out.append((topic, msg))


def make(tmp_path, gps=""):
    cfg = config.load()
    cfg["serial"]["gps"] = gps
    cfg["lora"]["off_file"] = str(tmp_path / "lora_off.json")
    cfg["http"]["live_dir"] = str(tmp_path / "live")
    clock, bus = Clock(), Collect()
    return health.Health(cfg, bus, clock=clock), clock, bus


def test_message_passes_the_contract(tmp_path):
    h, _, bus = make(tmp_path)
    m = h.publish()
    assert bus.out == [(SYS, m)] and validate(SYS, m) == []
    assert m["batt_pct"] is None and m["gps_fix"] is None  # no GPS in the indoor profile
    assert isinstance(m["disk_free_gb"], float)


def test_parts_count_as_working_only_while_fresh(tmp_path):
    h, clock, _ = make(tmp_path)
    assert h.sample()["omni"] is False
    h.handle_msg(OBS_BLE, {"t": 1})
    h.handle_msg(THERMAL, {"t": 1})
    m = h.sample()
    assert m["omni"] and m["thermal"] and not m["wifi"]
    clock.now += h.stale_s + 1
    assert h.sample()["omni"] is False
    h.handle_msg(OBS_WIFI, {"t": 2})
    assert h.sample()["wifi"] is True


def test_gps_fix_follows_gps_poses(tmp_path):
    h, clock, _ = make(tmp_path, gps="/dev/ttyAMA2")
    assert h.sample()["gps_fix"] is False
    h.handle_msg(POSE, {"t": 1, "src": "waypoint"})
    assert h.sample()["gps_fix"] is False
    h.handle_msg(POSE, {"t": 1, "src": "gps", "fix": 1})
    assert h.sample()["gps_fix"] is True
    clock.now += h.stale_s + 1
    assert h.sample()["gps_fix"] is False


def test_lora_false_when_silenced(tmp_path, monkeypatch):
    h, _, _ = make(tmp_path)
    monkeypatch.setattr(health, "lora_out_running", lambda: True)
    assert h.sample()["lora"] is True
    (tmp_path / "lora_off.json").write_text("{}")
    assert h.sample()["lora"] is False
    monkeypatch.setattr(health, "lora_out_running", lambda: False)
    (tmp_path / "lora_off.json").unlink()
    assert h.sample()["lora"] is False


def test_camera_fresh_from_the_file(tmp_path):
    h, _, _ = make(tmp_path)
    assert h.sample()["camera"] is False
    (tmp_path / "live").mkdir()
    (tmp_path / "live" / "camera.jpg").write_bytes(b"jpg")
    assert h.sample()["camera"] is True
