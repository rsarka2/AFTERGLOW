"""Trail pathing for the TV dashboard: trails.py (ranking, routes), case.py (the open case),
the dashboard's /api/case, and the fake pod flying the case's trail."""
import json
import math
import random
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from afterglow import config, trails
from afterglow.case import CaseStore
from afterglow.contracts import POSE, validate
from afterglow.dashboard import Commands, State, make_handler, make_meta
from afterglow.parkmap import ParkMap

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from fake_bus import FakePod  # noqa: E402

PARK = config.load(config.DEFAULT.parent / "park.toml")
DEMO = config.load()


@pytest.fixture(scope="module")
def park_pm():
    return ParkMap.load(PARK)


# ---------- trails.py ----------

def test_start_point_is_the_car_park_or_the_entrance():
    xy, label = trails.start_point(PARK, "Gorham Mountain lot")
    assert label == "Gorham Mountain lot" and xy == [1150.0, 820.0]
    xy, label = trails.start_point(PARK, None)
    assert label == "Park entrance" and xy == PARK["search"]["entrance_xy"]
    assert trails.start_point(PARK, "No such lot")[1] == "Park entrance"


def test_ranking_prefers_popular_trails_near_the_car(park_pm):
    near_gorham = trails.rank(PARK, park_pm, [1150.0, 820.0])
    assert [r["name"] for r in near_gorham[:2]] == ["Ocean Path", "Gorham Mountain Trail"]
    near_quarry = trails.rank(PARK, park_pm, [150.0, 110.0])
    assert near_quarry[1]["name"] == "Quarry Trail"  # the nearby trail climbs the list
    assert near_quarry[1]["dist_m"] < 50
    for r in near_gorham:
        assert set(r) == {"name", "pct", "dist_m", "length_m", "pop"}
    assert sum(r["pct"] for r in trails.rank(PARK, park_pm, [1150.0, 820.0], top=100)) in range(98, 103)


def test_route_is_one_line_starting_at_the_end_nearer_the_start(park_pm):
    start = [1080.0, -250.0]  # Otter Point lot, by Ocean Path's south end
    pts = trails.route(park_pm, "Ocean Path", start)
    assert len(pts) > 50
    assert math.dist(pts[0], start) < math.dist(pts[-1], start)
    assert 2000 < trails.length(pts) < 3000  # the pieces joined, scraps left out
    assert all(math.dist(a, b) <= trails.MAX_GAP_M for a, b in zip(pts, pts[1:]))
    assert trails.route(park_pm, "No such trail", start) == []


def test_stretch_starts_by_the_car_and_runs_the_longer_way(park_pm):
    car = [1150.0, 820.0]  # Gorham Mountain lot, beside Ocean Path
    pts = trails.route(park_pm, "Ocean Path", car, max_m=600)
    assert abs(trails.length(pts) - 600) < 1
    assert math.dist(pts[0], car) < 40  # where the trail passes the car
    whole = trails.route(park_pm, "Ocean Path", car)
    assert trails.dist_to(whole, *pts[-1]) < 1  # still on the trail
    assert trails.route(park_pm, "Ocean Path", car, max_m=99999) and trails.length(trails.route(park_pm, "Ocean Path", car, max_m=99999)) < trails.length(whole)


def test_the_missing_phone_is_beside_the_stretch():
    """The park scenario: the hidden iPhone is off the trail but in earshot of the stretch from Gorham lot."""
    pm = ParkMap.load(PARK)
    pts = trails.route(pm, "Ocean Path", [1150.0, 820.0], max_m=PARK["search"]["segment_m"])
    ph = next(p for p in PARK["sim"]["phones"] if p["name"] == "dead_iphone")
    assert PARK["rules"]["offtrail_m"] < trails.dist_to(pts, ph["x"], ph["y"]) <= PARK["search"]["corridor_m"] + 10
    x0, y0, x1, y1 = PARK["locate"]["area_m"]
    assert x0 <= ph["x"] <= x1 and y0 <= ph["y"] <= y1  # and inside the sweep box


def test_progress_along_a_route():
    pts = [[0, 0], [100, 0], [100, 50]]
    assert trails.progress(pts, 50, 3) == (50.0, 150.0, 3.0)
    along, total, off = trails.progress(pts, 104, 25)
    assert (along, total, off) == (125.0, 150.0, 4.0)


# ---------- case.py ----------

def test_case_open_update_close_and_persist(tmp_path, park_pm):
    path = tmp_path / "case.json"
    cs = CaseStore(PARK, park_pm, path=path)
    assert cs.view()["case"] is None and cs.view()["ranking"] == []
    seen = []
    cs.listeners.append(seen.append)
    problems, v = cs.open({"name": " Sam ", "age": 34, "details": "red jacket", "carrying": "iphone",
                           "car_park": "Gorham Mountain lot"})
    assert problems == []
    assert v["case"]["name"] == "Sam" and v["case"]["age"] == "34" and v["case"]["expect_cls"] == "findmy"
    assert v["start"]["label"] == "Gorham Mountain lot"
    assert v["trail"] == v["ranking"][0]["name"] == "Ocean Path"
    assert abs(trails.length(v["route"]) - PARK["search"]["segment_m"]) < 1  # just the stretch to search first
    assert json.loads(path.read_text())["name"] == "Sam"
    # the ranger picks another trail; the rest of the case stays
    problems, v = cs.open({"trail": "Gorham Mountain Trail"})
    assert problems == [] and v["trail"] == "Gorham Mountain Trail" and v["case"]["name"] == "Sam"
    # a restart reads it back
    assert CaseStore(PARK, park_pm, path=path).view()["case"]["trail"] == "Gorham Mountain Trail"
    v = cs.close()
    assert v["case"] is None and not path.exists()
    assert len(seen) == 3 and seen[-1]["case"] is None


def test_case_rejects_bad_input(tmp_path, park_pm):
    cs = CaseStore(PARK, park_pm, path=tmp_path / "c.json")
    assert cs.open({"age": "3"})[0] == ["a case needs a name"]
    problems, _ = cs.open({"name": "x" * 81, "carrying": "pigeon", "car_park": "Nowhere", "trail": "Nope", "details": ["a"]})
    assert len(problems) == 5
    assert cs.view()["case"] is None


def test_case_never_reaches_the_bus(tmp_path, park_pm):
    """Personal details stay on the laptop: CaseStore has no bus, and nothing it returns is a topic."""
    cs = CaseStore(PARK, park_pm, path=tmp_path / "c.json")
    assert not hasattr(cs, "bus")
    _, v = cs.open({"name": "Sam"})
    assert set(v) == {"case", "start", "ranking", "trail", "route"}


# ---------- dashboard /api/case ----------

@pytest.fixture
def server(tmp_path, park_pm):
    meta = make_meta(PARK, park_pm)
    state = State(meta["trail_step_m"])
    cmds = Commands(lambda: None, PARK["waypoints"])
    cases = CaseStore(PARK, park_pm, path=tmp_path / "case.json")
    cases.listeners.append(lambda v: state.broadcast("case", v))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state, cmds, meta, {"live": tmp_path, "snaps": tmp_path}, cases))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", state, meta
    srv.shutdown()
    srv.server_close()


def call(url, body=None):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_api_case_round_trip(server):
    base, state, meta = server
    assert [cp["name"] for cp in meta["search"]["car_parks"]][0] == "Gorham Mountain lot"
    assert call(f"{base}/api/case")[1]["case"] is None
    q = state.subscribe()
    code, out = call(f"{base}/api/case", {"name": "Sam", "car_park": "Otter Point lot", "carrying": "android"})
    assert code == 200 and out["ok"] and out["case"]["expect_cls"] == "findhub"
    assert out["start"]["label"] == "Otter Point lot" and out["ranking"][0]["name"] == "Ocean Path"
    events = [q.get_nowait() for _ in range(q.qsize())]
    assert any(ev == "case" and json.loads(data)["case"]["name"] == "Sam" for ev, data in events)
    assert call(f"{base}/api/case")[1]["case"]["name"] == "Sam"
    code, out = call(f"{base}/api/case", {"car_park": "Nowhere"})
    assert code == 400 and not out["ok"]
    code, out = call(f"{base}/api/case/close", {})
    assert code == 200 and out["case"] is None


def test_page_is_served(server):
    base, _, _ = server
    with urllib.request.urlopen(f"{base}/tv.html", timeout=5) as r:
        assert r.status == 200 and b"tv.js" in r.read()


# ---------- the fake pod flies the case's trail in "search" ----------

def test_fake_pod_flies_the_trail_only_in_search(park_pm):
    pod = FakePod(PARK, park_pm, random.Random(1), only={"pose"})
    start = [1080.0, -250.0]
    route = trails.route(park_pm, "Ocean Path", start)
    pod.set_route(route)
    run = pod.run(0)
    poses = lambda n: [m for _, m, _ in (next(run) for _ in range(n))]  # noqa: E731
    lanes = poses(50)
    x0, y0, x1, y1 = pod.area
    assert all(x0 - 1 <= m["x"] <= x1 + 1 and y0 - 1 <= m["y"] <= y1 + 1 for m in lanes)  # still on its lanes
    pod.on_mode({"t": 0, "mode": "search"})
    flown = poses(5 * 400)  # 400 s at 5 Hz
    for m in flown:
        assert validate(POSE, m) == []
    steps = [math.dist((a["x"], a["y"]), (b["x"], b["y"])) for a, b in zip([lanes[-1]] + flown, flown)]
    assert max(steps) < PARK["sim"]["speed_mps"] / PARK["pose"]["publish_hz"] + 0.5  # no jumps: it flies over
    on_trail = [m for m in flown if trails.progress(route, m["x"], m["y"])[2] < 1.0]
    assert len(on_trail) > len(flown) / 2  # most of the time on the trail itself
    pod.on_mode({"t": 0, "mode": "patrol"})
    back = poses(5 * 400)
    assert trails.progress(route, back[-1]["x"], back[-1]["y"])[2] > 1.0  # back to the lanes


def test_demo_profile_has_trail_pathing_too(tmp_path):
    cs = CaseStore(DEMO, ParkMap.load(DEMO), path=tmp_path / "c.json")
    _, v = cs.open({"name": "Test", "car_park": "Car park A"})
    assert v["trail"] == "Taped trail" and len(v["route"]) >= 2


# ---------- checking a ping: the fake pod flies there, its stand-in sensors read the scene ----------

def test_investigate_flies_to_the_pin_and_the_sensors_find_the_person(park_pm):
    from afterglow.contracts import THERMAL, VISION
    pod = FakePod(PARK, park_pm, random.Random(2), only={"pose", "thermal", "vision"})
    person = next(p for p in PARK["sim"]["phones"] if p.get("person"))
    run = pod.run(0)
    take = lambda n: [next(run) for _ in range(n)]  # noqa: E731
    take(10)
    pod.on_target({"pin": "P1", "x": person["x"] + 6, "y": person["y"] - 4, "status": "new"})  # an estimate, a little off
    pod.on_ctrl({"cmd": "investigate", "pin": "P1"})
    msgs = take(5 * 400)
    for topic, msg, _ in msgs:
        assert validate(topic, msg) == [], (topic, msg)
    poses = [m for t, m, _ in msgs if t == POSE]
    last = poses[-50:]
    assert all(math.dist((m["x"], m["y"]), (person["x"] + 6, person["y"] - 4)) < pod.pather.orbit_m + 1 for m in last)  # circling it
    assert any(m["hot"] for t, m, _ in msgs[-500:] if t == THERMAL)
    assert any(m["kind"] == "person" for t, m, _ in msgs[-500:] if t == VISION)
    # Clear: back to the path, and the sensors go quiet
    pod.on_ctrl({"cmd": "clear", "pin": "P1"})
    later = take(5 * 300)
    assert not any(t == VISION for t, _, _ in later[-200:])
    assert all(not m["hot"] for t, m, _ in later[-200:] if t == THERMAL)


def test_the_decoy_is_not_a_person(park_pm):
    from afterglow.contracts import THERMAL, VISION
    pod = FakePod(PARK, park_pm, random.Random(4), only={"pose", "thermal", "vision"})
    decoy = next(p for p in PARK["sim"]["phones"] if p["name"] == "airtag_lost")
    assert not decoy.get("person")
    pod.on_target({"pin": "P2", "x": decoy["x"], "y": decoy["y"], "status": "new"})
    pod.on_ctrl({"cmd": "investigate", "pin": "P2"})
    run = pod.run(0)
    msgs = [next(run) for _ in range(5 * 400)][-500:]
    assert not any(t == VISION for t, _, _ in msgs)
    assert all(not m["hot"] for t, m, _ in msgs if t == THERMAL)


def test_emergency_mode_keeps_the_pod_on_the_ping(park_pm):
    pod = FakePod(PARK, park_pm, random.Random(5), only={"pose"})
    pod.on_target({"pin": "P1", "x": 1180.0, "y": 490.0, "status": "investigating"})
    pod.on_ctrl({"cmd": "investigate", "pin": "P1"})
    pod.on_mode({"mode": "investigate"})
    pod.on_ctrl({"cmd": "confirm", "pin": "P1"})
    run = pod.run(0)
    last = [m for _, m, _ in (next(run) for _ in range(5 * 300))][-20:]
    assert all(math.dist((m["x"], m["y"]), (1180.0, 490.0)) < pod.pather.orbit_m + 1 for m in last)


def test_case_links_a_ping(tmp_path, park_pm):
    cs = CaseStore(PARK, park_pm, path=tmp_path / "c.json")
    cs.open({"name": "Sam"})
    assert cs.open({"ping": "P3"})[1]["case"]["ping"] == "P3"
    assert cs.open({"ping": ""})[1]["case"]["ping"] == ""
    assert cs.open({"ping": "drop table"})[0] == ["ping must look like P3, or be empty"]


# ---------- team on the way: back to the sweep, with check-ins on the person ----------

def test_team_on_the_way_sweeps_and_checks_in(park_pm):
    pod = FakePod(PARK, park_pm, random.Random(6), only={"pose"})
    person = (1180.0, 490.0)
    pod.on_target({"pin": "P1", "x": person[0], "y": person[1], "status": "investigating"})
    pod.on_ctrl({"cmd": "investigate", "pin": "P1"})
    pod.on_mode({"mode": "investigate"})
    pod.on_target({"pin": "P1", "x": person[0], "y": person[1], "status": "confirmed"})
    pod.on_ctrl({"cmd": "confirm", "pin": "P1"})
    run = pod.run(0)
    [next(run) for _ in range(5 * 60)]
    pod.on_mode({"mode": "patrol"})  # team on the way: back to the day sweep
    hz, every, dwell = PARK["pose"]["publish_hz"], PARK["search"]["checkin_s"], PARK["sim"]["checkin_dwell_s"]
    poses = [m for _, m, _ in (next(run) for _ in range(int(hz * (every * 2.5 + 200))))]
    near = [math.dist((m["x"], m["y"]), person) < pod.pather.orbit_m + 1 for m in poses]
    away = [math.dist((m["x"], m["y"]), person) > 60 for m in poses]
    assert sum(away) > len(poses) / 3  # mostly back on the sweep
    visits = sum(1 for a, b in zip(near, near[1:]) if b and not a)
    assert visits >= 2  # and it keeps coming back
    assert sum(near) >= hz * dwell  # circling for a while each time
    pod.stop_watching()
    later = [m for _, m, _ in (next(run) for _ in range(int(hz * every * 2)))]
    assert not any(math.dist((m["x"], m["y"]), person) < pod.pather.orbit_m + 1 for m in later[int(hz * 60):])


def test_case_records_the_team_and_the_sweep_before(tmp_path, park_pm):
    cs = CaseStore(PARK, park_pm, path=tmp_path / "c.json")
    v = cs.open({"name": "Sam", "prior_mode": "night"})[1]
    assert v["case"]["prior_mode"] == "night"
    v = cs.open({"team": True})[1]
    assert isinstance(v["case"]["team_t"], int)
    assert cs.open({"prior_mode": "investigate"})[0] == ["prior_mode must be patrol or night"]


def test_the_false_signal_comes_first_on_the_stretch(park_pm):
    pts = trails.route(park_pm, "Ocean Path", [1150.0, 820.0], max_m=PARK["search"]["segment_m"])
    phones = {p["name"]: p for p in PARK["sim"]["phones"]}
    lost, person = phones["airtag_lost"], phones["dead_iphone"]
    assert not lost.get("person") and person.get("person")
    a_lost = trails.progress(pts, lost["x"], lost["y"])[0]
    a_person = trails.progress(pts, person["x"], person["y"])[0]
    assert a_lost < a_person - 100  # the trail search reaches the false signal well before the person
    assert PARK["rules"]["offtrail_m"] < trails.dist_to(pts, lost["x"], lost["y"]) <= PARK["search"]["corridor_m"] + 10


def test_reset_brings_the_local_stack_back_to_time_zero(tmp_path):
    import local_stack
    clock = local_stack.SimClock(4)
    called = []
    clock.on_reset = lambda: called.append(1)
    clock.sim_ms = 5000
    assert clock.set({"reset": True}) == [] and called == [1]
    clock.restart()
    assert clock.sim_ms == 0 and not clock.paused
    s = State(1.0)
    s.apply(POSE, {"t": 1, "x": 0, "y": 0, "z": 1, "src": "sim"})
    s.clear()
    assert s.poses == {} and s.pins == {} and s.bus_t == 0
