"""dashboard.py: the state it keeps, the commands it publishes, and its HTTP endpoints."""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from afterglow import config
from afterglow.contracts import COVERAGE, CTRL, MODE, OBS_BLE, POSE, TARGET, TRACK, validate
from afterglow.dashboard import (OBS_IDS, Commands, PodPictures, State, make_handler, make_meta, pod_url,
                                 safe_file)
from afterglow.parkmap import ParkMap

T = 1_731_000_000_000


class StubBus:
    def __init__(self):
        self.out = []
        self.retained = []  # topics published with retain

    def publish(self, topic, msg, retain=False):
        self.out.append((topic, msg))
        if retain:
            self.retained.append(topic)


def pose(t, x, y, **kw):
    return {"t": t, "x": x, "y": y, "z": 3.0, "src": "sim", **kw}


# ---------- State ----------

def test_pose_trail_is_decimated_by_step():
    s = State(trail_step_m=1.0)
    for i, x in enumerate([0, 0.3, 0.6, 1.1, 1.2, 2.5]):
        s.apply(POSE, pose(T + i, x, 0))
    assert s.trails["pod"] == [[0, 0], [1.1, 0], [2.5, 0]]
    assert s.poses["pod"]["x"] == 2.5


def test_drone_field_keeps_poses_apart():
    s = State(0.5)
    s.apply(POSE, pose(T, 0, 0))
    s.apply(POSE, pose(T, 5, 5, drone="d2"))
    assert set(s.poses) == {"pod", "d2"}


def test_clock_follows_the_pose_not_a_stale_retained_message():
    s = State(0.5)
    s.apply(COVERAGE, {"t": T + 10_000_000, "pct": 0.5})  # retained, from an earlier run far ahead
    assert s.bus_t == T + 10_000_000  # before any pose, the newest t
    s.apply(POSE, pose(T, 0, 0))
    s.apply(COVERAGE, {"t": T + 20_000_000, "pct": 0.5})
    assert s.bus_t == T


def test_obs_summary_and_log():
    s = State(0.5)
    for i, r in enumerate([-80, -60, -70]):
        s.apply(OBS_BLE, {"t": T + i, "src": "omni", "id": "abcd1234", "cls": "findmy", "rssi": r})
    o = s.obs["abcd1234"]
    assert (o["n"], o["best"], o["rssi"], o["cls"], o["kind"]) == (3, -60, -70, "findmy", "ble")
    assert s.obs_log[0]["msg"]["rssi"] == -70 and len(s.obs_log) == 3


def test_obs_summary_drops_the_longest_silent_id_when_full():
    s = State(0.5)
    n = OBS_IDS + 5
    for i in range(n):
        s.apply(OBS_BLE, {"t": T + i, "src": "omni", "id": f"{i:08x}", "cls": "findmy", "rssi": -70})
    assert len(s.obs) == OBS_IDS
    assert f"{n - 1:08x}" in s.obs and f"{0:08x}" not in s.obs  # the newest is kept, the oldest went


def test_records_are_keyed():
    s = State(0.5)
    s.apply(TARGET, {"t": T, "pin": "P1", "status": "new"})
    s.apply(TARGET, {"t": T + 1, "pin": "P1", "status": "acknowledged"})
    s.apply(TRACK, {"t": T, "track": "T4"})
    s.apply("ag/sys", {"t": T, "cpu_c": 50})
    assert s.pins["P1"]["status"] == "acknowledged" and "T4" in s.tracks and s.latest["ag/sys"]["cpu_c"] == 50


def test_subscribe_starts_with_full_state_then_streams():
    s = State(0.5)
    s.on_msg(POSE, pose(T, 1, 2))
    q = s.subscribe()
    ev, data = q.get_nowait()
    assert ev == "state" and json.loads(data)["poses"]["pod"]["x"] == 1
    s.on_msg(POSE, pose(T + 1, 3, 4))
    ev, data = q.get_nowait()
    assert ev == "msg" and json.loads(data)["msg"]["x"] == 3


def test_a_stuck_browser_is_dropped():
    s = State(0.5)
    q = s.subscribe()
    for i in range(q.maxsize + 5):
        s.on_msg(POSE, pose(T + i, 0, 0))
    assert q not in s.clients
    assert q.get_nowait() == (None, None)  # tells the stream to end; the browser reconnects


# ---------- Commands: only clicks publish, and every message passes the contract ----------

@pytest.fixture
def cmds():
    bus = StubBus()
    return Commands(lambda: bus, {0: (0, 0), 1: (3, 0)}), bus


def test_ctrl_stamps_t_validates_and_strips_extras(cmds):
    c, bus = cmds
    code, out = c.ctrl({"cmd": "ack", "pin": "P1", "status": "confirmed", "t": 5})
    assert code == 200
    topic, msg = bus.out[-1]
    assert topic == CTRL and set(msg) == {"cmd", "pin", "t"} and msg["t"] > T and validate(CTRL, msg) == []
    assert bus.retained == []  # a click is a one-off; never replayed to a late subscriber


@pytest.mark.parametrize("body, problem", [
    ({"cmd": "ack"}, "needs a pin"),
    ({"cmd": "confirm", "pin": 3}, "needs a pin"),
    ({"cmd": "accept_leg"}, "needs a leg id"),
    ({"cmd": "waypoint", "n": 9}, "waypoint needs n"),
    ({"cmd": "self_destruct"}, "bad cmd"),
])
def test_bad_ctrl_is_refused(cmds, body, problem):
    c, bus = cmds
    code, out = c.ctrl(body)
    assert code == 400 and any(problem in p for p in out["problems"]) and bus.out == []


def test_mode(cmds):
    c, bus = cmds
    assert c.mode({"mode": "night"})[0] == 200 and bus.out[-1][0] == MODE
    assert validate(MODE, bus.out[-1][1]) == []
    assert bus.retained == [MODE]  # a restarted geofence or a second dashboard gets the mode at once
    assert c.mode({"mode": "party"})[0] == 400


def test_no_broker_is_503():
    assert Commands(lambda: None, {}).mode({"mode": "night"})[0] == 503


def test_state_never_publishes(cmds):
    # The dashboard only relays clicks: feeding it pins must not produce any ag/ctrl.
    c, bus = cmds
    s = State(0.5)
    s.on_msg(TARGET, {"t": T, "pin": "P1", "status": "new", "state": "pinned", "prio": 1})
    assert bus.out == []


def test_safe_file_blocks_escapes(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    assert safe_file(tmp_path, "a.txt") == tmp_path / "a.txt"
    assert safe_file(tmp_path, "../a.txt") is None
    assert safe_file(tmp_path, "nope.txt") is None


# ---------- HTTP ----------

@pytest.fixture
def server(tmp_path):
    cfg = config.load()
    meta = make_meta(cfg, ParkMap.load(cfg))
    state, bus = State(meta["trail_step_m"]), StubBus()
    cmds = Commands(lambda: bus, cfg["waypoints"])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state, cmds, meta, {"live": tmp_path, "snaps": tmp_path}))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", state, bus
    srv.shutdown()
    srv.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def post(url, body: bytes, ctype="application/json"):
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_page_and_map(server):
    base, _, _ = server
    code, ctype, body = get(base + "/")
    assert code == 200 and b"tv.js" in body and ctype.startswith("text/html")  # / is the operator TV
    _, _, body = get(base + "/api/map")
    meta = json.loads(body)
    assert meta["profile"] == "afterglow" and meta["map"]["zones"] and meta["waypoints"]["0"] == [0.0, 0.0]
    assert "tak" not in meta  # no TAK server here: the TV must not post rescue routes (tv.js checks meta.tak)
    assert meta["rssi"] == {"p0": -48.0, "n": 2.4}  # nearby.html's RSSI -> metres
    assert get(base + "/vendor/maplibre-gl-4.7.1/maplibre-gl.js")[0] == 200
    assert get(base + "/nearby.html")[0] == 200


def test_http_live_picture_has_its_age(server, tmp_path):
    # The TV compares Last-Modified with Date (both the server's clock) to spot a frozen feed.
    (tmp_path / "thermal.png").write_bytes(b"png")
    os.utime(tmp_path / "thermal.png", (1_700_000_000, 1_700_000_000))
    base, _, _ = server
    with urllib.request.urlopen(base + "/live/thermal.png", timeout=5) as r:
        assert r.read() == b"png" and r.headers.get("Cache-Control") == "no-store"
        lm, date = parsedate_to_datetime(r.headers["Last-Modified"]), parsedate_to_datetime(r.headers["Date"])
    assert lm.timestamp() == 1_700_000_000 and date > lm
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(base + "/live/camera.jpg", timeout=5)


class _SkewedPod(BaseHTTPRequestHandler):
    """A pod dashboard whose clock is an hour fast, serving a 3 s old camera picture."""
    SKEW, AGE = 3600, 3

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path != "/live/camera.jpg":
            return self.send_error(404)
        now = time.time() + self.SKEW
        self.send_response_only(200)
        self.send_header("Date", formatdate(now, usegmt=True))
        self.send_header("Last-Modified", formatdate(now - self.AGE, usegmt=True))
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", "3")
        self.end_headers()
        self.wfile.write(b"jpg")


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def laptop_and_pod(tmp_path):
    pod_srv = _serve(_SkewedPod)
    cfg = config.load()
    meta = make_meta(cfg, ParkMap.load(cfg))
    state, bus = State(meta["trail_step_m"]), StubBus()
    pod = PodPictures(f"http://127.0.0.1:{pod_srv.server_address[1]}")
    srv = _serve(make_handler(state, Commands(lambda: bus, cfg["waypoints"]), meta,
                              {"live": tmp_path, "snaps": tmp_path}, pod=pod))
    yield f"http://127.0.0.1:{srv.server_address[1]}", pod_srv
    for s in (srv, pod_srv):
        s.shutdown()
        s.server_close()


def test_live_pictures_come_from_the_pod_with_their_age_on_our_clock(laptop_and_pod):
    base, _ = laptop_and_pod
    with urllib.request.urlopen(base + "/live/camera.jpg", timeout=5) as r:
        assert r.read() == b"jpg" and r.headers["Content-Type"] == "image/jpeg"
        age = (parsedate_to_datetime(r.headers["Date"]) - parsedate_to_datetime(r.headers["Last-Modified"])).total_seconds()
    assert 2 <= age <= 5  # the Pi's hour of skew is gone; the 3 s age is kept
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/live/thermal.png", timeout=5)
    assert e.value.code == 404


def test_unreachable_pod_is_502(laptop_and_pod):
    base, pod_srv = laptop_and_pod
    pod_srv.shutdown()
    pod_srv.server_close()
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/live/camera.jpg", timeout=10)
    assert e.value.code == 502


@pytest.mark.parametrize("arg,broker,want", [
    (None, None, None),
    (None, "localhost", None),
    (None, "127.0.0.1:1883", None),
    (None, "10.0.0.5", "http://10.0.0.5:8080"),
    (None, "10.0.0.5:1884", "http://10.0.0.5:8080"),  # the MQTT port isn't the HTTP port
    ("off", "10.0.0.5", None),
    ("10.0.0.7", "10.0.0.5", "http://10.0.0.7:8080"),
    ("pod.local:9000", None, "http://pod.local:9000"),
])
def test_pod_url(arg, broker, want):
    assert pod_url(arg, broker, 8080) == want


def test_sites_are_the_same_in_both_profiles_and_match_their_ports():
    """The TV's location tabs: each site's port is its profile's [http] port, so the links land."""
    demo = config.load()
    park = config.load(config.DEFAULT.parent / "park.toml")
    assert demo["http"]["sites"] == park["http"]["sites"]
    by_profile = {s["profile"]: s for s in demo["http"]["sites"]}
    assert by_profile["afterglow"]["port"] == demo["http"]["port"] and by_profile["afterglow"]["pod"] is True
    assert by_profile["park"]["port"] == park["http"]["port"] and by_profile["park"]["pod"] is False
    assert demo["http"]["port"] != park["http"]["port"]  # both run on the laptop at once
    meta = make_meta(demo, ParkMap.load(demo))
    assert [s["name"] for s in meta["sites"]] == [s["name"] for s in demo["http"]["sites"]]
    assert meta["health"]["period_s"] == demo["health"]["period_s"]


def test_meta_has_the_building_and_the_check_in_time():
    cfg = config.load()
    meta = make_meta(cfg, ParkMap.load(cfg))
    assert meta["building"]["name"] == "60 State Street" and len(meta["building"]["ring"]) >= 4
    assert meta["search"]["checkin_s"] == cfg["search"]["checkin_s"] == 300.0
    assert meta["thermal"]["hot_min_c"] < meta["thermal"]["hot_max_c"]
    park = config.load(config.DEFAULT.parent / "park.toml")
    assert make_meta(park, ParkMap.load(park))["building"] is None


def test_http_ctrl(server):
    base, _, bus = server
    code, out = post(base + "/api/ctrl", json.dumps({"cmd": "waypoint", "n": 3}).encode())
    assert code == 200 and bus.out[-1][1]["n"] == 3
    assert post(base + "/api/ctrl", b"not json")[0] == 400
    assert post(base + "/api/ctrl", b"[1, 2]")[0] == 400


def test_http_ctrl_refuses_non_json_content_type(server):
    # A form or text/plain POST needs no CORS preflight, so any web page could send it.
    base, _, bus = server
    body = json.dumps({"cmd": "confirm", "pin": "P1"}).encode()
    assert post(base + "/api/ctrl", body, ctype="text/plain")[0] == 415
    assert post(base + "/api/mode", b'{"mode":"night"}', ctype="application/x-www-form-urlencoded")[0] == 415
    assert bus.out == []


def test_http_events_start_with_state(server):
    base, state, _ = server
    state.on_msg(POSE, pose(T, 7, 8))
    with urllib.request.urlopen(base + "/api/events", timeout=5) as r:
        assert r.headers.get("Content-Type") == "text/event-stream"
        seen = []
        for raw in r:
            line = raw.decode().strip()
            if line.startswith("event:"):
                seen.append(line.split(":", 1)[1].strip())
            if line.startswith("data:") and seen and seen[-1] == "state":
                assert json.loads(line[5:])["poses"]["pod"]["x"] == 7
                break
    assert "state" in seen
