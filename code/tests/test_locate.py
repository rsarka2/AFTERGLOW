"""locate.py: the coverage and probability grids, on small scripted scenes and on the fake pod
through the real tracker.

Most scenes use a 20 x 10 m grid of 1 m cells from (0, 0), with detect_m 2.
"""
import json
import math
import random
import subprocess
import sys
from pathlib import Path

import pytest

from afterglow import config
from afterglow.contracts import COVERAGE, CTRL, POSE, RETAINED, TRACK, validate
from afterglow.locate import MAX_CELLS, MAX_SPEED_MPS, Locate, grid_area
from afterglow.parkmap import ParkMap
from afterglow.tracker import Tracker

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE / "tools"))
from fake_bus import FakePod  # noqa: E402

T0 = 1_731_000_000_000
CFG = config.load(config.DEFAULT)
SMALL = dict(CFG, locate=dict(CFG["locate"], area_m=[0.0, 0.0, 20.0, 10.0], cell_m=1.0, detect_m=2.0, stale_s=30))


class Collect:
    def __init__(self):
        self.msgs = []

    def publish(self, topic, obj, retain=False):
        self.msgs.append((topic, obj, retain))


def make(cfg=SMALL):
    bus = Collect()
    return Locate(cfg, bus), bus


def pose(x, y, t=T0, **kw):
    return {"t": t, "x": x, "y": y, "z": 3.0, "src": "sim", **kw}


def track(name="T1", x=10.0, y=5.0, t=T0, **kw):
    m = {"t": t, "track": name, "ids": [f"{name}-a"], "cls": "findmy", "x": x, "y": y, "r95": 2.0,
         "state": "pinned", "passes": 2, "last_s": 0, "with_drone": False, "baseline": False, "c": 0.8}
    m.update(kw)
    return m


def cell(loc, x, y):
    """The flat index of the cell holding (x, y)."""
    return int((y - loc.y0) // loc.cell) * loc.nx + int((x - loc.x0) // loc.cell)


def grids(loc, t=None):
    m = loc.message(loc.now if t is None else t)
    return m["covered"], m["p"]


# ---------- the grid ----------

def test_message_validates_and_is_retained():
    loc, bus = make()
    loc.feed(POSE, pose(5.0, 5.0))
    loc.feed(TRACK, track())
    assert loc.maybe_publish()
    (topic, msg, retain), = bus.msgs
    assert topic == COVERAGE and retain and COVERAGE in RETAINED
    assert validate(COVERAGE, msg) == []
    assert (msg["x0"], msg["y0"], msg["cell_m"], msg["nx"], msg["ny"]) == (0.0, 0.0, 1.0, 20, 10)
    assert all(v in (0, 1) for v in msg["covered"]) and all(0 <= v <= 255 for v in msg["p"])


def test_row_0_is_the_south_edge():
    loc, _ = make()
    loc.feed(POSE, pose(0.5, 0.5))
    cov, _ = grids(loc)
    assert cov[0] == 1 and cov[19] == 0  # south-west corner covered, south-east not
    assert cov[cell(loc, 0.5, 2.5)] == 1 and cov[cell(loc, 0.5, 3.5)] == 0  # detect_m 2 northward


def test_pose_covers_within_detect_m():
    loc, _ = make()
    loc.feed(POSE, pose(10.0, 5.0))
    cov, _ = grids(loc)
    for i, v in enumerate(cov):
        cx, cy = (i % loc.nx) + 0.5, (i // loc.nx) + 0.5
        assert v == (math.hypot(cx - 10, cy - 5) <= 2.0)


def test_two_poses_cover_the_line_between():
    loc, _ = make()
    loc.feed(POSE, pose(2.0, 5.0, T0))
    loc.feed(POSE, pose(18.0, 5.0, T0 + 4000))  # 16 m in 4 s
    cov, _ = grids(loc)
    assert all(cov[cell(loc, x + 0.5, 5.5)] for x in range(0, 20))
    assert loc.message(T0)["pct"] == pytest.approx(sum(cov) / 200, abs=1e-3)


def test_a_jump_too_fast_to_fly_is_not_joined():
    loc, _ = make()
    loc.feed(POSE, pose(2.0, 5.0, T0))
    loc.feed(POSE, pose(18.0, 5.0, T0 + int(16 / MAX_SPEED_MPS * 1000) - 50))
    cov, _ = grids(loc)
    assert cov[cell(loc, 10.5, 5.5)] == 0
    assert cov[cell(loc, 2.5, 5.5)] and cov[cell(loc, 18.5, 5.5)]
    assert loc.stats["pose_jump"] == 1


def test_two_drones_are_never_joined():
    loc, _ = make()
    loc.feed(POSE, pose(2.0, 5.0, T0, drone="d1"))
    loc.feed(POSE, pose(18.0, 5.0, T0 + 4000, drone="d2"))
    loc.feed(POSE, pose(18.0, 4.0, T0 + 5000, drone="d2"))
    cov, _ = grids(loc)
    assert cov[cell(loc, 10.5, 5.5)] == 0
    assert cov[cell(loc, 2.5, 5.5)] and cov[cell(loc, 18.5, 5.5)]


def test_poses_off_the_grid_cover_only_what_is_on_it():
    loc, _ = make()
    loc.feed(POSE, pose(-50.0, -50.0))
    loc.feed(POSE, pose(21.0, 5.0, T0 + 1000))  # just past the east edge: still reaches 2 m in
    cov, _ = grids(loc)
    assert cov[cell(loc, 19.5, 5.5)] == 1


# ---------- probability ----------

def test_a_track_is_a_blob_at_its_estimate():
    loc, _ = make()
    loc.feed(TRACK, track(x=10.5, y=5.5, c=0.8))
    _, p = grids(loc)
    peak = max(range(len(p)), key=p.__getitem__)
    assert peak == cell(loc, 10.5, 5.5)
    assert p[peak] == int(255 * 0.8)
    assert p[cell(loc, 10.5 + 2.0, 5.5)] < p[peak]  # falls off with distance
    assert p[cell(loc, 0.5, 0.5)] == 0


def test_95_percent_of_the_blob_lies_within_r95():
    loc, _ = make(dict(SMALL, locate=dict(SMALL["locate"], cell_m=0.25)))
    loc.feed(TRACK, track(x=10.0, y=5.0, r95=3.0, c=1.0))
    _, p = grids(loc)
    inside = sum(v for i, v in enumerate(p)
                 if math.hypot(loc.cx[i % loc.nx] - 10, loc.cy[i // loc.nx] - 5) <= 3.0)
    assert inside / sum(p) == pytest.approx(0.95, abs=0.02)


def test_overlapping_blobs_take_the_higher():
    loc, _ = make()
    loc.feed(TRACK, track("T1", x=10.5, y=5.5, c=0.4))
    loc.feed(TRACK, track("T2", x=10.5, y=5.5, c=0.9))
    _, p = grids(loc)
    assert max(p) == int(255 * 0.9)


def test_with_drone_and_baseline_tracks_are_left_out():
    loc, _ = make()
    loc.feed(TRACK, track("T1", with_drone=True))
    loc.feed(TRACK, track("T2", x=3.0, baseline=True))
    _, p = grids(loc)
    assert max(p) == 0


def test_a_track_marked_with_drone_later_leaves_the_grid():
    loc, _ = make()
    loc.feed(TRACK, track("T1"))
    assert max(grids(loc)[1]) > 0
    loc.feed(TRACK, track("T1", t=T0 + 1000, with_drone=True, c=0.0))
    assert max(grids(loc)[1]) == 0


def test_a_track_follows_its_latest_estimate():
    loc, _ = make()
    loc.feed(TRACK, track("T1", x=3.5, y=5.5))
    loc.feed(TRACK, track("T1", x=15.5, y=5.5, t=T0 + 1000))
    _, p = grids(loc)
    assert max(range(len(p)), key=p.__getitem__) == cell(loc, 15.5, 5.5)
    assert p[cell(loc, 3.5, 5.5)] == 0


def test_a_track_with_no_c_still_shows():
    loc, _ = make()
    m = track()
    del m["c"]
    loc.feed(TRACK, m)
    assert max(grids(loc)[1]) > 0


def test_stale_tracks_drop_out():
    loc, bus = make()
    loc.feed(TRACK, track("T1"))
    loc.feed(TRACK, track("T2", x=3.0, t=T0 + 20_000))
    loc.publish(T0 + 31_000)
    assert set(loc.tracks) == {"T2"}
    assert loc.stats["stale_tracks"] == 1
    assert bus.msgs[-1][1]["p"][cell(loc, 10.5, 5.5)] == 0


# ---------- control, clock and bad input ----------

def test_new_search_clears_both_grids_and_publishes_at_once():
    loc, bus = make()
    loc.feed(POSE, pose(5.0, 5.0))
    loc.feed(TRACK, track())
    loc.maybe_publish()
    loc.feed(CTRL, {"cmd": "new_search"})
    assert loc.maybe_publish()  # no need to wait for the next second
    msg = bus.msgs[-1][1]
    assert sum(msg["covered"]) == 0 and max(msg["p"]) == 0 and msg["pct"] == 0
    loc.feed(POSE, pose(15.0, 5.0, T0 + 500))  # not joined to the pose from before the new search
    assert grids(loc)[0][cell(loc, 10.5, 5.5)] == 0


def test_other_commands_change_nothing():
    loc, _ = make()
    loc.feed(POSE, pose(5.0, 5.0))
    before = grids(loc)
    for cmd in ("baseline_start", "ack", "lora_off"):
        loc.feed(CTRL, {"cmd": cmd, "pin": "P1"})
    assert grids(loc) == before


def test_about_once_a_second_of_bus_time():
    loc, bus = make()
    assert not loc.maybe_publish()  # no bus time yet
    for i in range(50):  # 10 s of 5 Hz poses
        loc.feed(POSE, pose(5.0 + i * 0.2, 5.0, T0 + i * 200))
        loc.maybe_publish()
    assert len(bus.msgs) == 10


def test_clock_follows_bus_time_back():
    loc, bus = make()
    loc.feed(POSE, pose(2.0, 5.0, T0 + 600_000))  # fake_bus --speed ran ahead
    loc.maybe_publish()
    loc.feed(TRACK, track("T1", t=T0 + 600_000))
    loc.feed(POSE, pose(18.0, 5.0, T0 + 1000))    # then the real pod
    assert loc.now == T0 + 1000
    assert loc.maybe_publish()                    # at once, stamped with the new time
    assert bus.msgs[-1][1]["t"] == T0 + 1000
    assert grids(loc)[0][cell(loc, 10.5, 5.5)] == 0  # never joined across the switch
    loc.publish(T0 + 2000)
    assert "T1" in loc.tracks                       # not dropped as stale by the switch


def test_a_lagging_track_does_not_take_the_clock_back():
    loc, _ = make()
    loc.feed(POSE, pose(2.0, 5.0, T0 + 20_000))
    loc.feed(TRACK, track("T1", t=T0 + 11_000))  # the tracker is behind by 9 s
    loc.feed(POSE, pose(4.0, 5.0, T0 + 20_200))
    assert loc.now == T0 + 20_200 and loc.stats["resync"] == 0
    assert grids(loc)[0][cell(loc, 3.5, 5.5)] == 1  # the two poses still joined
    assert "T1" in loc.tracks


def test_bad_values_are_skipped():
    loc, _ = make()
    loc.feed(POSE, pose(float("nan"), 5.0))
    loc.feed(POSE, {"x": 5.0, "y": 5.0})            # no t
    loc.feed(POSE, pose("east", 5.0))
    loc.feed(TRACK, track(x=float("inf")))
    loc.feed(TRACK, track(c="high"))
    loc.feed(TRACK, {"t": T0, "track": "T9"})       # missing keys
    cov, p = grids(loc, T0)
    assert sum(cov) == 0 and max(p) == 0
    assert loc.stats["bad_pose"] == 3 and loc.stats["bad_track"] == 3


def test_a_big_area_grows_the_cells():
    cfg = dict(SMALL, locate=dict(SMALL["locate"], area_m=[0.0, 0.0, 2000.0, 2000.0], cell_m=1.0))
    loc, bus = make(cfg)
    assert loc.nx * loc.ny <= MAX_CELLS and loc.cell > 1.0
    loc.feed(POSE, pose(1000.0, 1000.0))
    loc.maybe_publish()
    assert validate(COVERAGE, bus.msgs[-1][1]) == []


@pytest.mark.parametrize("area", [[0.0, 0.0, 5000.0, 9.0], [0.0, 0.0, 30000.0, 1.5], [0.0, 0.0, 141.3, 141.3]])
def test_the_cell_cap_holds_for_any_shape(area):
    """Rounding the grown cell up used to leave a long thin area a few cells over the cap."""
    loc, _ = make(dict(SMALL, locate=dict(SMALL["locate"], area_m=area, cell_m=1.0)))
    assert loc.nx * loc.ny <= MAX_CELLS


def test_empty_area_uses_the_park_zone():
    pm = ParkMap.load(CFG)
    assert CFG["locate"]["area_m"] == []
    assert grid_area(CFG, pm) == tuple(round(v, 2) for v in pm.bounds())


def test_both_profiles_build():
    for name in ("afterglow.toml", "park.toml"):
        cfg = config.load(CODE / name)
        loc = Locate(cfg, Collect())
        assert loc.nx * loc.ny <= MAX_CELLS
    park = Locate(config.load(CODE / "park.toml"), Collect())
    assert (park.nx, park.ny, park.cell) == (30, 46, 10.0)


# ---------- the fake pod through the real tracker ----------

def run_fake(profile, seconds, seed=7):
    """The fake pod's pose and adverts through the real tracker, then locate."""
    cfg = config.load(CODE / profile)
    pod = FakePod(cfg, ParkMap.load(cfg), random.Random(seed), only={"pose", "ble"})
    bus = Collect()
    loc = Locate(cfg, bus)
    tracks = {}  # the tracker's latest message per track

    class ToLocate:
        def publish(self, topic, msg, retain=False):
            if topic == TRACK:
                tracks[msg["track"]] = msg
            loc.feed(topic, msg)

    tr = Tracker(cfg, ToLocate())
    for topic, msg, _ in pod.run(T0, seconds):
        tr.feed(topic, msg)
        loc.feed(topic, msg)
        loc.maybe_publish()
    return cfg, loc, tracks, [m for _, m, _ in bus.msgs]


@pytest.fixture(scope="module")
def indoor():
    return run_fake("afterglow.toml", 600)


def test_fake_run_validates_and_coverage_only_grows(indoor):
    *_, msgs = indoor
    assert len(msgs) >= 590
    assert all(validate(COVERAGE, m) == [] for m in msgs)
    pct = [m["pct"] for m in msgs]
    assert pct == sorted(pct) and pct[-1] > 0.3


def test_hidden_phones_show_on_the_heatmap(indoor):
    """Each hidden phone has its own blob: a track near it, bright at the track's cell."""
    cfg, loc, _, msgs = indoor
    p = msgs[-1]["p"]
    for ph in cfg["sim"]["phones"]:
        if "x" not in ph:
            continue
        near = [(x, y) for _, x, y, _, _ in loc.tracks.values()
                if math.hypot(x - ph["x"], y - ph["y"]) <= cfg["track"]["localized_r95_m"] / 2]
        assert len(near) == 1, ph["name"]
        assert p[cell(loc, *near[0])] > 150, ph["name"]


def test_team_phone_is_not_on_the_heatmap(indoor):
    _, loc, tracks, _ = indoor
    riding = {name for name, m in tracks.items() if m.get("with_drone")}
    assert riding  # the tracker caught the team phone
    assert not riding & set(loc.tracks)
    assert set(loc.tracks) == set(tracks) - riding


def test_fake_bus_coverage_is_the_real_grid():
    for name in ("afterglow.toml", "park.toml"):
        cfg = config.load(CODE / name)
        pm = ParkMap.load(cfg)
        pod = FakePod(cfg, pm, random.Random(1), only={"pose", "coverage"})
        cov = next(m for topic, m, _ in pod.run(T0, 5) if topic == COVERAGE)
        loc = Locate(cfg, Collect(), pm)
        assert (cov["x0"], cov["y0"], cov["cell_m"], cov["nx"], cov["ny"]) == \
            (loc.x0, loc.y0, loc.cell, loc.nx, loc.ny)


def test_dry_run_reads_fake_poses_and_tracks():
    """The docstring's no-broker pipeline: both grids fill."""
    fake = subprocess.run([sys.executable, "tools/fake_bus.py", "--print", "--only", "pose,track", "--duration", "60",
                           "--seed", "3"], cwd=CODE, capture_output=True, text=True, check=True)
    out = subprocess.run([sys.executable, "-m", "afterglow.locate", "--dry-run"], cwd=CODE, input=fake.stdout,
                         capture_output=True, text=True, check=True)
    lines = out.stdout.splitlines()
    assert lines and all(line.startswith(COVERAGE + " ") for line in lines)
    assert 55 <= len(lines) <= 63
    last = json.loads(lines[-1].partition(" ")[2])
    assert last["pct"] > 0 and max(last["p"]) > 0
    assert "covered" in out.stderr
