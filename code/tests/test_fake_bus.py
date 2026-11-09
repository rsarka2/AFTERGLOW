"""tools/fake_bus.py: every message matches the contract, in both profiles."""
import random
import sys
from pathlib import Path

import pytest

from afterglow import config
from afterglow.contracts import COVERAGE, OBS_BLE, POSE, SYS, TARGET, THERMAL, TRACK, VISION, validate
from afterglow.parkmap import ParkMap

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from fake_bus import FakePod  # noqa: E402

PROFILES = {"indoor": (config.DEFAULT, 240), "park": (config.DEFAULT.parent / "park.toml", 2400)}


def run(profile, seconds=None, only=None, seed=7):
    path, default_s = PROFILES[profile]
    cfg = config.load(path)
    kw = {} if only is None else {"only": only}
    pod = FakePod(cfg, ParkMap.load(cfg), random.Random(seed), **kw)
    return cfg, list(pod.run(1_731_000_000_000, seconds or default_s))


@pytest.fixture(scope="module", params=list(PROFILES))
def out(request):
    return request.param, *run(request.param)


def test_every_message_validates(out):
    _, _, msgs = out
    for topic, msg, _ in msgs:
        assert validate(topic, msg) == [], (topic, msg)
    seen = {t for t, _, _ in msgs}
    assert {POSE, OBS_BLE, TRACK, TARGET, COVERAGE, THERMAL, SYS} <= seen <= {POSE, OBS_BLE, TRACK, TARGET, COVERAGE, THERMAL, VISION, SYS}


def test_docked_pod_waits_at_base_until_sent_out_and_flies_home():
    """local_stack's pod: on the ground at [sim] base_xy with the scanners off, then Send out, then home."""
    path, _ = PROFILES["park"]
    cfg = config.load(path)
    pod = FakePod(cfg, ParkMap.load(cfg), random.Random(7), docked=True)
    base = tuple(cfg["sim"]["base_xy"])
    it = pod.run(1_731_000_000_000)
    first = [next(it) for _ in range(200)]  # 40 s at 5 Hz
    poses = [m for t, m, _ in first if t == POSE]
    assert pod.flight == "docked" and all((p["x"], p["y"], p["z"]) == (*base, 0.0) for p in poses)
    assert not any(t == OBS_BLE for t, _, _ in first)
    assert [m["x"] for t, m, _ in first if t == POSE] and pod.plan()[0] in ([1000.0, 840.0], [1300.0, 840.0])
    pod.launch()
    flying = [next(it) for _ in range(600)]
    poses = [m for t, m, _ in flying if t == POSE]
    assert pod.flight == "out" and poses[-1]["z"] == cfg["sim"]["height_m"] and (poses[-1]["x"], poses[-1]["y"]) != base
    assert any(t == OBS_BLE for t, _, _ in flying)
    pod.go_home()
    for _ in range(20_000):
        t, m, _ = next(it)
        if pod.flight == "docked":
            break
    assert pod.flight == "docked"
    last = [m for t, m, _ in (next(it) for _ in range(50)) if t == POSE][-1]
    assert (last["x"], last["y"], last["z"]) == (*base, 0.0)


def test_send_out_plays_the_same_scenario_however_long_it_waited():
    path, _ = PROFILES["park"]
    cfg = config.load(path)

    def ble_after_launch(wait_steps):
        pod = FakePod(cfg, ParkMap.load(cfg), random.Random(7), docked=True)
        it = pod.run(1_731_000_000_000)
        for _ in range(wait_steps):
            next(it)
        pod.launch()
        out = []
        while len(out) < 40:
            t, m, _ = next(it)
            if t == OBS_BLE:
                out.append((m["cls"], m["rssi"]))
        return out

    assert ble_after_launch(30) == ble_after_launch(317)


def test_stand_in_health_every_period(out):
    """The TV's pod-components panel needs ag/sys in rehearsal too: every [health] period_s, all parts heard."""
    _, cfg, msgs = out
    sys_msgs = [m for t, m, _ in msgs if t == SYS]
    gaps = {b["t"] - a["t"] for a, b in zip(sys_msgs, sys_msgs[1:])}
    assert sys_msgs and gaps == {int(cfg["health"]["period_s"] * 1000)}
    m = sys_msgs[0]
    assert m["throttled"] == "0x0" and m["lora"] and all(m[k] for k in ("omni", "wifi", "thermal", "camera"))
    assert m["gps_fix"] is (None if not cfg["serial"]["gps"] else True)


def test_pose_is_marked_simulated(out):
    _, _, msgs = out
    assert all(m["src"] == "sim" for t, m, _ in msgs if t == POSE)


def test_only_coverage_is_retained(out):
    _, _, msgs = out
    assert all(retain == (t == COVERAGE) for t, _, retain in msgs)


def test_pins_never_change_status_on_their_own(out):
    # The drone never decides: only the ranger acks, investigates, clears or confirms.
    _, _, msgs = out
    pins = [m for t, m, _ in msgs if t == TARGET]
    assert pins and all(m["status"] == "new" and m["state"] != "confirmed" for m in pins)


def test_team_phone_is_with_drone_and_never_pinned(out):
    _, _, msgs = out
    tracks = {}
    for t, m, _ in msgs:
        if t == TRACK:
            tracks[m["track"]] = m
    drone = [m for m in tracks.values() if m["with_drone"]]
    assert len(drone) == 1
    assert drone[0]["track"] not in {m["track"] for t, m, _ in msgs if t == TARGET}


def test_every_hidden_phone_gets_pinned(out):
    profile, cfg, msgs = out
    last = {}
    for t, m, _ in msgs:
        if t == TARGET:
            last[m["pin"]] = m
    hidden = [p for p in cfg["sim"]["phones"] if "x" in p]
    a = cfg["locate"]["area_m"]
    if a:  # phones outside the sweep box (park: the Cadillac South Ridge hiker) are for trail pathing only
        hidden = [p for p in hidden if a[0] <= p["x"] <= a[2] and a[1] <= p["y"] <= a[3]]
    assert len(last) == len(hidden)
    assert all(m["state"] == "pinned" for m in last.values()), {k: m["state"] for k, m in last.items()}
    for m in last.values():  # the cheating estimate stays near a real phone
        assert min(((m["x"] - p["x"]) ** 2 + (m["y"] - p["y"]) ** 2) ** 0.5 for p in hidden) <= m["r95"] * 1.5


def test_coverage_grows(out):
    _, _, msgs = out
    pct = [m["pct"] for t, m, _ in msgs if t == COVERAGE]
    assert pct == sorted(pct) and pct[-1] > 0.5


def test_only_filters_topics():
    _, msgs = run("indoor", seconds=30, only={"pose", "ble"})
    assert {t for t, _, _ in msgs} == {POSE, OBS_BLE}
