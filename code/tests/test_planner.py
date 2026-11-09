"""planner.py: homing, grid and patrol legs, the plan statuses, and the one rule (it never accepts).

The indoor map (afterglow.toml): the trail runs along y = 0 from x = 0 to 9; the park zone's
bounds are x -2..11, y -5..7.
"""
import json
import math
import random
import subprocess
import sys
from pathlib import Path

import pytest

from afterglow import config
from afterglow.contracts import CTRL, MODE, PLAN, POSE, TARGET, validate
from afterglow.parkmap import ParkMap, seg_dist
from afterglow.planner import (ORBIT_POINTS, PUBLISH_MS, REPEAT_S, Planner, grid_pts, heading, homing_pts,
                               simplify)

CODE = Path(__file__).resolve().parent.parent
T0 = 1_731_000_000_000
CFG = config.load(config.DEFAULT)
PM = ParkMap.load(CFG)
PARK = config.load(CODE / "park.toml")


class Collect:
    def __init__(self):
        self.msgs = []

    def publish(self, topic, obj, retain=False):
        self.msgs.append((topic, json.loads(json.dumps(obj))))

    def plans(self):
        return [m for t, m in self.msgs if t == PLAN]

    def last(self, pid):
        return [m for m in self.plans() if m["id"] == pid][-1]


def make(cfg=CFG, pm=PM):
    bus = Collect()
    return Planner(cfg, bus, pm=pm), bus


def pose(pl, x, y, t=T0):
    pl.feed(POSE, {"t": t, "x": x, "y": y, "z": 3.0, "src": "sim"})


def target(pl, pin, x, y, status, r95=2.0, t=T0):
    pl.feed(TARGET, {"t": t, "pin": pin, "track": "T1", "x": x, "y": y, "r95": r95, "c": 0.7, "prio": 3,
                     "cls": "findmy", "status": status, "state": "localized", "last_s": 1})


def mode(pl, m, t=T0):
    pl.feed(MODE, {"t": t, "mode": m})


def ctrl(pl, cmd, t=T0, **kw):
    pl.feed(CTRL, {"t": t, "cmd": cmd, **kw})


def ends(plan):
    return [(lg["to_x"], lg["to_y"]) for lg in plan["legs"]]


def assert_valid(bus):
    for topic, m in bus.msgs:
        assert validate(topic, m) == [], m


# ---------- geometry ----------

def test_heading_is_compass_degrees():
    assert heading(0, 1) == 0
    assert heading(1, 0) == 90
    assert heading(0, -1) == 180
    assert heading(-1, 0) == 270


def test_homing_crosses_the_pin_then_shrinks_round_it():
    pts = homing_pts((0.0, 0.0), (10.0, 0.0), r95=2.5, cross_m=3.0, min_orbit_m=1.0)
    assert pts[0] == pytest.approx((13.0, 0.0))  # straight over the pin and 3 m past it
    radii = [round(math.dist(p, (10.0, 0.0)), 6) for p in pts[1:]]
    assert radii == [2.5] * (ORBIT_POINTS + 1) + [1.25] * (ORBIT_POINTS + 1)


def test_homing_orbit_stays_between_min_orbit_and_cross():
    wide = homing_pts((0.0, 0.0), (10.0, 0.0), r95=50.0, cross_m=3.0, min_orbit_m=1.0)
    assert max(math.dist(p, (10, 0)) for p in wide[1:]) == pytest.approx(3.0)
    tight = homing_pts((0.0, 0.0), (10.0, 0.0), r95=0.2, cross_m=3.0, min_orbit_m=1.0)
    assert len(tight) == 1 + ORBIT_POINTS + 1  # one orbit at min_orbit_m: a second would be the same
    assert all(math.dist(p, (10, 0)) == pytest.approx(1.0) for p in tight[1:])


def test_grid_lanes_cover_the_area_from_the_nearest_corner():
    pts = grid_pts((0.0, 0.0, 20.0, 10.0), 3.0, (19.0, 9.0))  # pod by the north-east corner
    ys = sorted({p[1] for p in pts})
    assert len(ys) == 4 and all(b - a <= 3.0 for a, b in zip(ys, ys[1:]))
    assert ys[0] <= 1.5 and ys[-1] >= 8.5  # no edge further than half a lane from one
    assert pts[0] == pytest.approx((20.0, ys[-1]))  # starts east, on the north lane
    assert {p[0] for p in pts} == {0.0, 20.0}


def test_simplify_keeps_the_line_within_tolerance():
    line = [(x, 0.3 * math.sin(x)) for x in range(20)] + [(19, 10)]
    out = simplify(line, 0.5)
    assert out[0] == line[0] and out[-1] == line[-1] and len(out) < len(line)
    for p in line:
        assert min(seg_dist(*p, a, b) for a, b in zip(out, out[1:])) <= 0.5 + 1e-9


# ---------- proposals ----------

def test_investigating_proposes_homing_once():
    pl, bus = make()
    pose(pl, 0, 0)
    target(pl, "P1", 2, 6, "new")
    assert bus.plans() == []
    target(pl, "P1", 2, 6, "investigating")
    target(pl, "P1", 2, 6, "investigating", t=T0 + 1000)  # geofence repeats it every second
    plans = bus.plans()
    assert len(plans) == 1
    p = plans[0]
    assert p["kind"] == "homing" and p["pin"] == "P1" and p["status"] == "proposed"
    assert p["legs"][0]["dist_m"] == pytest.approx(math.hypot(2, 6) + CFG["plan"]["cross_m"], abs=0.2)
    assert all(lg["alt_m"] == CFG["plan"]["homing_alt_m"] for lg in p["legs"])
    assert_valid(bus)


def test_homing_dropped_when_the_pin_is_cleared():
    pl, bus = make()
    pose(pl, 0, 0)
    target(pl, "P1", 2, 6, "investigating")
    pid = bus.plans()[0]["id"]
    target(pl, "P1", 2, 6, "cleared")
    assert bus.last(pid)["status"] == "dropped"


def test_search_proposes_a_grid_and_patrol_a_trail_route():
    pl, bus = make()
    pose(pl, 0, 0)
    mode(pl, "search")
    grid = bus.plans()[-1]
    assert grid["kind"] == "grid" and len(grid["legs"]) >= 4
    mode(pl, "patrol")
    patrol = bus.plans()[-1]
    assert patrol["kind"] == "patrol" and patrol["trail"] == "Taped trail"
    for x, y in ends(patrol):
        assert PM.dist_to_trail(x, y) <= CFG["plan"]["arrive_m"] + 0.1
    assert bus.last(grid["id"])["status"] == "dropped"  # the newer proposal replaced it
    assert_valid(bus)


def test_the_same_mode_again_proposes_nothing_new():
    pl, bus = make()
    pose(pl, 0, 0)
    mode(pl, "search")
    mode(pl, "search", t=T0 + 1000)  # a repeat click while the grid is still up
    mode(pl, "investigate")
    assert [p["kind"] for p in bus.plans()] == ["grid"]


def test_a_new_click_proposes_again_once_the_plan_is_gone():
    pl, bus = make()
    pose(pl, 0, 0)
    mode(pl, "search")
    a = bus.plans()[-1]["id"]
    ctrl(pl, "reject_leg", id=a)
    mode(pl, "search")  # the retained copy again (same t) after a reconnect: not a click
    assert len(pl.plans) == 1
    mode(pl, "search", t=T0 + 1000)  # the ranger clicks search again
    b = bus.plans()[-1]
    assert b["kind"] == "grid" and b["id"] != a and b["status"] == "proposed"


def test_nothing_is_proposed_before_the_first_pose():
    """The retained ag/mode comes before any pose (indoors, a pose only comes with a waypoint click)."""
    pl, bus = make()
    mode(pl, "search")
    assert bus.plans() == []
    pose(pl, 10, 6, t=T0 + 5000)  # the pod is by the north-east corner
    grid = bus.plans()[-1]
    assert grid["kind"] == "grid"
    first = grid_pts(pl.area, CFG["plan"]["lane_m"], (10, 6))[0]
    assert ends(grid)[0] == pytest.approx(first)  # the lane nearest the pod, from its start
    assert grid["legs"][0]["dist_m"] == pytest.approx(math.dist((10, 6), first), abs=0.1)


def test_homing_waits_for_the_first_pose_and_keeps_its_crossing_pass():
    pl, bus = make()
    mode(pl, "search")
    target(pl, "P1", 2, 6, "investigating")  # the newest ask wins, as with proposals
    assert bus.plans() == []
    pose(pl, 0, 0, t=T0 + 1000)
    plans = bus.plans()
    assert [p["kind"] for p in plans] == ["homing"]
    assert plans[0]["legs"][0]["dist_m"] == pytest.approx(math.hypot(2, 6) + CFG["plan"]["cross_m"], abs=0.2)


def test_a_pin_cleared_before_the_first_pose_gets_no_homing():
    pl, bus = make()
    target(pl, "P1", 2, 6, "investigating")
    target(pl, "P1", 2, 6, "cleared")
    pose(pl, 0, 0)
    assert bus.plans() == []


def test_a_bad_target_doesnt_block_homing_for_that_pin():
    pl, bus = make()
    pose(pl, 0, 0)
    target(pl, "P1", 2, 6, "investigating", r95=None)
    assert bus.plans() == [] and pl.stats["bad_target"] == 1
    target(pl, "P1", 2, 6, "investigating", t=T0 + 1000)
    assert [p["kind"] for p in bus.plans()] == ["homing"]


def test_a_bad_pose_is_left_out():
    pl, bus = make()
    pose(pl, 0, 0)
    mode(pl, "search")
    n = len(bus.plans())
    pose(pl, float("nan"), 0, t=T0 + 2000)
    assert pl.pose == (0.0, 0.0) and pl.stats["bad_pose"] == 1 and len(bus.plans()) == n


def test_park_profile_plans_are_flyable():
    pm = ParkMap.load(PARK)
    pl, bus = make(PARK, pm)
    pose(pl, 1150, 820)
    mode(pl, "patrol")
    mode(pl, "search")
    target(pl, "P1", 1100, 700, "investigating", r95=25)
    plans = [p for p in bus.plans() if p["status"] == "proposed"]
    assert [p["kind"] for p in plans] == ["patrol", "grid", "homing"]
    for p in plans:
        assert 1 <= len(p["legs"]) <= 40
        assert all(lg["alt_m"] <= 120 for lg in p["legs"])
    assert plans[0]["trail"] == "Ocean Path"
    assert_valid(bus)


# ---------- the ranger's clicks ----------

def test_accept_and_reject_only_by_click():
    pl, bus = make()
    pose(pl, 0, 0)
    mode(pl, "search")
    a = bus.plans()[-1]["id"]
    ctrl(pl, "accept_leg", id=a)
    assert bus.last(a)["status"] == "accepted"
    ctrl(pl, "accept_leg", id=a)  # already accepted: ignored
    ctrl(pl, "accept_leg", id="L99")  # no such plan: ignored
    assert pl.stats["ignored_cmd"] == 2
    mode(pl, "patrol")
    b = bus.plans()[-1]["id"]
    assert bus.last(a)["status"] == "accepted"  # a new proposal never stops the plan being flown
    ctrl(pl, "reject_leg", id=b)
    assert bus.last(b)["status"] == "rejected"
    ctrl(pl, "accept_leg", id=b)  # rejected stays rejected
    assert bus.last(b)["status"] == "rejected"
    ctrl(pl, "reject_leg", id=a)  # the ranger can stop the accepted one too
    assert bus.last(a)["status"] == "rejected"


def test_accepting_another_plan_drops_the_one_being_flown():
    pl, bus = make()
    pose(pl, 0, 0)
    mode(pl, "search")
    a = bus.plans()[-1]["id"]
    ctrl(pl, "accept_leg", id=a)
    target(pl, "P1", 2, 6, "investigating")
    b = bus.plans()[-1]["id"]
    ctrl(pl, "accept_leg", id=b)
    assert bus.last(a)["status"] == "dropped" and bus.last(b)["status"] == "accepted"


def test_new_search_drops_every_open_plan_then_proposes_for_the_mode():
    pl, bus = make()
    pose(pl, 0, 0)
    mode(pl, "search")
    ctrl(pl, "accept_leg", id=bus.plans()[-1]["id"])
    mode(pl, "patrol")
    target(pl, "P1", 2, 6, "investigating")
    old = list(pl.plans)
    ctrl(pl, "new_search")
    assert {bus.last(pid)["status"] for pid in old} == {"dropped"}
    [p] = pl.open()
    assert p["id"] not in old and p["kind"] == "patrol" and p["status"] == "proposed"


def test_new_search_in_investigate_mode_proposes_nothing():
    pl, bus = make()
    pose(pl, 0, 0)
    mode(pl, "investigate")
    target(pl, "P1", 2, 6, "investigating")
    ctrl(pl, "new_search")
    assert pl.open() == []


def test_never_accepts_on_its_own():
    """The one rule: whatever the pod, the pins and the mode do, no plan is accepted without a click."""
    rnd = random.Random(7)
    pl, bus = make()
    statuses = ["new", "acknowledged", "investigating", "cleared", "confirmed"]
    for i in range(3000):
        t = T0 + i * 200
        r = rnd.random()
        if r < 0.6:
            pose(pl, rnd.uniform(-2, 11), rnd.uniform(-5, 7), t=t)
        elif r < 0.85:
            target(pl, f"P{rnd.randint(1, 4)}", rnd.uniform(-2, 11), rnd.uniform(-5, 7), rnd.choice(statuses), t=t)
        elif r < 0.97:
            mode(pl, rnd.choice(["patrol", "night", "investigate", "search"]), t=t)
        else:
            ctrl(pl, rnd.choice(["new_search", "ack", "investigate", "waypoint"]), pin="P1", n=1)
    assert bus.plans(), "the random run should have proposed something"
    assert all(p["status"] not in ("accepted", "done") for p in bus.plans())
    assert_valid(bus)


# ---------- following the pod ----------

def test_first_leg_follows_the_pod():
    pl, bus = make()
    pose(pl, 0, 0)
    mode(pl, "search")
    pid = bus.plans()[-1]["id"]
    to = ends(bus.last(pid))[0]
    n = len(bus.plans())
    pose(pl, 5, 5, t=T0 + 100)  # within PUBLISH_MS: kept, not sent yet
    assert len(bus.plans()) == n
    pose(pl, 5, 5, t=T0 + PUBLISH_MS + 100)
    first = bus.last(pid)["legs"][0]
    assert (first["to_x"], first["to_y"]) == to
    assert first["dist_m"] == pytest.approx(math.dist((5, 5), to), abs=0.1)
    assert first["heading_deg"] == round(heading(to[0] - 5, to[1] - 5)) % 360


def test_accepted_plan_drops_flown_legs_then_is_done():
    pl, bus = make()
    pose(pl, 0, 0)
    mode(pl, "patrol")  # one leg along the taped trail, to (9, 0)
    pid = bus.plans()[-1]["id"]
    n_legs = len(bus.last(pid)["legs"])
    pose(pl, 9, 0, t=T0 + 1)  # not accepted yet: nothing is flown
    assert len(bus.last(pid)["legs"]) == n_legs and bus.last(pid)["status"] == "proposed"
    ctrl(pl, "accept_leg", id=pid)  # the pod is already at the end
    assert bus.last(pid)["status"] == "done" and bus.last(pid)["legs"] == []


def test_homing_legs_come_off_as_the_pod_flies_them():
    pl, bus = make()
    pose(pl, 0, 0)
    target(pl, "P1", 4, 0, "investigating", r95=1.0)
    pid = bus.plans()[-1]["id"]
    ctrl(pl, "accept_leg", id=pid)
    path = ends(bus.last(pid))
    for i, (x, y) in enumerate(path, start=1):
        pose(pl, x + 0.1, y, t=T0 + i * 10)
        if i < len(path):
            assert len(bus.last(pid)["legs"]) == len(path) - i
    assert bus.last(pid)["status"] == "done"


def test_the_laptop_clock_on_clicks_doesnt_hold_back_live_legs():
    """The Pi's clock can be minutes behind the laptop's, which stamps ag/ctrl and ag/mode."""
    pl, bus = make()
    pose(pl, 0, 0)
    mode(pl, "search", t=T0 + 120_000)
    pid = bus.plans()[-1]["id"]
    ctrl(pl, "accept_leg", id=pid, t=T0 + 120_000)
    n = len(bus.plans())
    for i in range(1, 11):
        pose(pl, -0.2 * i, 0, t=T0 + i * PUBLISH_MS)
    assert len(bus.plans()) - n == 10  # one live first leg a second
    assert all(m["t"] <= T0 + 10 * PUBLISH_MS for m in bus.plans())  # stamped with the pod's clock


def test_tick_resends_open_plans():
    pl, bus = make()
    pose(pl, 0, 0)
    mode(pl, "search")
    n = len(bus.plans())
    pl.tick(wall=1e9)
    assert len(bus.plans()) == n + 1
    pl.tick(wall=1e9 + REPEAT_S / 2)
    assert len(bus.plans()) == n + 1


def test_dry_run_reads_bus_lines():
    lines = "\n".join([
        f'ag/pose {{"t":{T0},"x":0,"y":0,"z":3,"src":"sim"}}',
        f'ag/mode {{"t":{T0},"mode":"search"}}',
        f'ag/target {{"t":{T0},"pin":"P1","track":"T1","x":2,"y":6,"r95":2,"c":0.7,"prio":3,"cls":"findmy",'
        f'"status":"investigating","state":"localized","last_s":1}}',
    ])
    out = subprocess.run([sys.executable, "-m", "afterglow.planner", "--dry-run"], input=lines, text=True,
                         capture_output=True, cwd=CODE, check=True).stdout.splitlines()
    kinds = [json.loads(line.partition(" ")[2])["kind"] for line in out if line.startswith(PLAN)]
    assert kinds == ["grid", "grid", "homing"]  # the grid, dropped for the homing proposal
