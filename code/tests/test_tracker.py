"""tracker.py: on the fake bus's raw data (pose, ble) and on small scripted scenes."""
import math
import random
import subprocess
import sys
from pathlib import Path

import pytest

from afterglow import config
from afterglow.contracts import CTRL, OBS_BLE, OBS_WIFI, PASS, POSE, TRACK, validate
from afterglow.parkmap import ParkMap
from afterglow.tracker import MAX_TRACK, PENDING_MS, Tracker, guarded

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE / "tools"))
from fake_bus import FakePod  # noqa: E402

T0 = 1_731_000_000_000


class Collect:
    def __init__(self):
        self.msgs = []

    def publish(self, topic, obj, retain=False):
        self.msgs.append((topic, obj))


def run_fake(profile, seconds, seed=7):
    """The fake pod's pose and adverts through the real tracker. Returns (cfg, pod, tracker, messages)."""
    cfg = config.load(CODE / profile)
    pod = FakePod(cfg, ParkMap.load(cfg), random.Random(seed), only={"pose", "ble"})
    bus = Collect()
    tr = Tracker(cfg, bus)
    for topic, msg, _ in pod.run(T0, seconds):
        tr.feed(topic, msg)
    tr.tick(tr.now)
    return cfg, pod, tr, bus.msgs


def latest(msgs, topic=TRACK):
    out = {}
    for t, m in msgs:
        if t == topic:
            out[m["track"]] = m
    return out


@pytest.fixture(scope="module")
def indoor():
    return run_fake("afterglow.toml", 1200)  # past every phone's first 15-minute key rotation


# ---------- fake bus, indoor (the demo profile) ----------

def test_every_message_validates(indoor):
    *_, msgs = indoor
    assert msgs and {t for t, _ in msgs} == {TRACK, PASS}
    for topic, msg in msgs:
        assert validate(topic, msg) == [], (topic, msg)
    assert all(m["state"] != "confirmed" for t, m in msgs if t == TRACK)  # only the ranger confirms


def test_one_track_per_phone_through_key_rotation(indoor):
    _, pod, tr, _ = indoor
    assert len(tr.tracks) == len(pod.phones)
    for ph in pod.phones:
        assert len(ph.ids) >= 2, f"{ph.name} should have rotated in 20 minutes"
        owners = {tr.by_id[i].name for i in ph.ids if i in tr.by_id}
        assert len(owners) == 1, f"{ph.name} split over {owners}"
        assert set(tr.tracks[owners.pop()].ids) == set(ph.ids)


def test_hidden_phones_are_found(indoor):
    _, pod, tr, msgs = indoor
    last = latest(msgs)
    for ph in pod.phones:
        if ph.with_drone:
            continue
        m = last[tr.by_id[ph.ids[0]].name]
        err = math.hypot(m["x"] - ph.x, m["y"] - ph.y)
        assert err <= 2.0, (ph.name, err, m)  # I6 wants 5 m
        assert err <= m["r95"], (ph.name, err, m)  # r95 is honest
        assert m["state"] in ("localized", "pinned") and m["passes"] >= 2 and 0.5 <= m["c"] <= 0.95
        assert not m["with_drone"] and not m["baseline"]


def test_team_phone_rides_on_the_pod(indoor):
    _, pod, tr, msgs = indoor
    team = next(ph for ph in pod.phones if ph.with_drone)
    name = tr.by_id[team.ids[0]].name
    m = latest(msgs)[name]
    assert m["with_drone"] and m["state"] == "heard" and m["c"] == 0.0
    assert not any(t == PASS and p["track"] == name for t, p in msgs)
    assert [n for n, x in latest(msgs).items() if x["with_drone"]] == [name]


def test_passes_peak_near_each_phone(indoor):
    _, pod, tr, msgs = indoor
    for ph in pod.phones:
        if ph.with_drone:
            continue
        name = tr.by_id[ph.ids[0]].name
        peaks = [m for t, m in msgs if t == PASS and m["track"] == name]
        assert len(peaks) >= 10  # 20 minutes is about 50 laps of the taped loop
        near = sorted(math.hypot(m["peak_x"] - ph.x, m["peak_y"] - ph.y) for m in peaks)
        assert near[len(near) // 2] <= 6.0, (ph.name, near)  # the pod's path passes within ~4 m


def test_track_messages_about_once_a_second(indoor):
    *_, msgs = indoor
    ts = sorted({m["t"] for t, m in msgs if t == TRACK})
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    assert 1000 <= min(gaps) and max(gaps) <= 1400


# ---------- fake bus, park profile ----------

def test_park_profile_runs_and_validates():
    _, pod, tr, msgs = run_fake("park.toml", 900)
    for topic, msg in msgs:
        assert validate(topic, msg) == [], (topic, msg)
    drone = [m for m in latest(msgs).values() if m["with_drone"]]
    assert len(drone) == 1


# ---------- scripted scenes ----------

def scene(steps, cfg=None):
    """steps: (seconds, pod x, pod y, [(id, rssi, cls)]) once a second. Returns (tracker, messages)."""
    bus = Collect()
    tr = Tracker(cfg or config.load(), bus)
    for s, x, y, heard in steps:
        t = T0 + int(s * 1000)
        tr.feed(POSE, {"t": t, "x": x, "y": y, "z": 3.0, "src": "waypoint"})
        for oid, rssi, cls in heard:
            if cls == "wifi":
                tr.feed(OBS_WIFI, {"t": t, "id": oid, "rand": False, "rssi": rssi, "ch": 6})
            else:
                tr.feed(OBS_BLE, {"t": t, "src": "omni", "id": oid, "cls": cls, "rssi": rssi})
    tr.tick(tr.now + 30_000)  # let every new id settle
    return tr, bus.msgs


def test_rotation_joins_the_old_track():
    steps = [(s, 0.0, 0.0, [("aaaa0001" if s < 30 else "bbbb0002", -60, "findmy")]) for s in range(60)]
    tr, _ = scene(steps)
    assert len(tr.tracks) == 1 and next(iter(tr.tracks.values())).ids == ["aaaa0001", "bbbb0002"]


def test_two_beacons_never_merge():
    # The old id is still heard after the new one appears: a second beacon, not a rotation.
    steps = [(s, 0.0, 0.0, [("aaaa0001", -60, "findmy")] + ([("bbbb0002", -60, "findmy")] if s >= 30 else []))
             for s in range(60)]
    tr, _ = scene(steps)
    assert sorted(t.ids[0] for t in tr.tracks.values()) == ["aaaa0001", "bbbb0002"]


def test_different_class_never_links():
    steps = [(s, 0.0, 0.0, [("aaaa0001", -60, "findmy") if s < 30 else ("bbbb0002", -60, "findhub")])
             for s in range(60)]
    tr, _ = scene(steps)
    assert len(tr.tracks) == 2


def test_baseline_marks_only_beacons_heard_during_it():
    bus = Collect()
    tr = Tracker(config.load(), bus)
    tr.feed(POSE, {"t": T0, "x": 0.0, "y": 0.0, "z": 3.0, "src": "waypoint"})
    tr.feed(CTRL, {"cmd": "baseline_start"})
    for s in range(10):
        tr.feed(OBS_BLE, {"t": T0 + s * 1000, "src": "omni", "id": "0000base", "cls": "findmy", "rssi": -70})
    tr.feed(CTRL, {"cmd": "baseline_stop"})
    for s in range(10, 30):
        for oid in ("0000base", "00000new"):
            tr.feed(OBS_BLE, {"t": T0 + s * 1000, "src": "omni", "id": oid, "cls": "findmy", "rssi": -70})
    tr.tick(tr.now + 30_000)
    marks = {m["ids"][0]: m["baseline"] for m in latest(bus.msgs).values()}
    assert marks == {"0000base": True, "00000new": False}


def test_new_search_clears_and_numbers_keep_counting():
    tr, _ = scene([(s, 0.0, 0.0, [("aaaa0001", -60, "findmy")]) for s in range(10)])
    assert list(tr.tracks) == ["T1"]
    tr.feed(CTRL, {"cmd": "new_search"})
    assert tr.tracks == {} and tr.by_id == {} and tr.pending == {}
    t = tr.now
    for s in range(1, 12):
        tr.feed(OBS_BLE, {"t": t + s * 1000, "src": "omni", "id": "cccc0003", "cls": "findmy", "rssi": -60})
    tr.tick(tr.now + 30_000)
    assert list(tr.tracks) == ["T2"]  # a fresh number, so an old pin or ATAK marker isn't reused


def test_track_numbers_wrap_after_255():
    tr = Tracker(config.load(), Collect())
    tr.n = 254
    assert [tr.new_track("findmy").name for _ in range(3)] == ["T255", "T1", "T2"]
    tr.n = 0
    assert tr.new_track("findmy").name == "T3"  # T1 and T2 are taken


def test_only_lost_device_classes_are_tracked():
    heard = [("live0001", -60, "apple_other"), ("othr0002", -60, "other"), ("wifi0003", -60, "wifi"),
             ("ref00004", -60, "ref")]
    tr, msgs = scene([(s, 0.0, 0.0, heard) for s in range(10)])
    assert sorted(t.cls for t in tr.tracks.values()) == ["ref", "wifi"]
    bus = Collect()
    tr = Tracker(config.load(), bus)
    tr.feed(POSE, {"t": T0, "x": 0.0, "y": 0.0, "z": 3.0, "src": "waypoint"})
    for s in range(10):  # randomized Wi-Fi addresses change too often to follow
        tr.feed(OBS_WIFI, {"t": T0 + s * 1000, "id": f"rand{s:04d}", "rand": True, "rssi": -60, "ch": 1})
    tr.tick(tr.now + 30_000)
    assert tr.tracks == {} and tr.stats["wifi_rand"] == 10


def test_nothing_is_tracked_before_the_first_pose():
    bus = Collect()
    tr = Tracker(config.load(), bus)
    for s in range(10):
        tr.feed(OBS_BLE, {"t": T0 + s * 1000, "src": "omni", "id": "aaaa0001", "cls": "findmy", "rssi": -60})
    tr.tick(tr.now + 30_000)
    assert tr.tracks == {} and tr.stats["no_pose"] == 10 and bus.msgs == []


def test_state_follows_r95_and_c_stays_in_range():
    tr = Tracker(config.load(), Collect())
    L, P = tr.L, tr.P
    assert [tr.state(r) for r in (P, (P + L) / 2, L, L + 0.1)] == ["pinned", "localized", "localized", "heard"]
    for r95 in (0.1, P, L, 100):
        for passes in (0, 1, 5):
            assert 0.2 <= tr.confidence(r95, passes) <= 0.95


def test_track_c_must_be_0_to_1():
    m = {"t": 1, "track": "T1", "cls": "findmy", "x": 0, "y": 0, "r95": 5, "state": "heard", "last_s": 0}
    assert validate(TRACK, {**m, "c": 0.5, "baseline": True}) == []
    assert validate(TRACK, {**m, "c": 1.5}) != []


def walk_over(tr, p0, seed, t0=T0, laps=6, oid="aaaa0001"):
    """The pod walks back and forth over a fixed phone at (0, 0), 3 m below it, at 1 m/s."""
    rnd, t = random.Random(seed), t0
    for lap in range(laps):
        for x in (range(-6, 7) if lap % 2 == 0 else range(6, -7, -1)):
            t += 1000
            tr.feed(POSE, {"t": t, "x": float(x), "y": 0.0, "z": 3.0, "src": "waypoint"})
            rssi = p0 - 24 * math.log10(math.hypot(x, 3.0)) + rnd.gauss(0, 4)
            tr.feed(OBS_BLE, {"t": t, "src": "omni", "id": oid, "cls": "findmy", "rssi": round(rssi)})
    return t


def test_clock_follows_bus_time_back():
    # fake_bus --speed left the clock 30 minutes ahead; then the real pod starts.
    bus = Collect()
    tr = Tracker(config.load(), bus)
    tr.feed(POSE, {"t": T0 + 30 * 60_000, "x": 0.0, "y": 0.0, "z": 3.0, "src": "sim"})
    walk_over(tr, -48, seed=0)
    assert tr.stats["resync"] == 1
    m = latest(bus.msgs)["T1"]
    assert m["passes"] >= 2 and m["last_s"] <= 1


def test_loud_fixed_phone_is_not_with_drone():
    # 4 dB louder than the model, right under the path: the old median check marked it 3 times in 20.
    for seed in range(20):
        bus = Collect()
        tr = Tracker(config.load(), bus)
        walk_over(tr, -44, seed)
        assert not any(m["with_drone"] for t, m in bus.msgs if t == TRACK), seed


def test_with_drone_clears_when_the_beacon_goes_quiet():
    tr = Tracker(config.load(), Collect())
    for s in range(40):  # loud all along while the pod walks 40 m: rides on the pod
        t = T0 + s * 1000
        tr.feed(POSE, {"t": t, "x": float(s), "y": 0.0, "z": 3.0, "src": "waypoint"})
        tr.feed(OBS_BLE, {"t": t, "src": "omni", "id": "aaaa0001", "cls": "findmy", "rssi": -40})
    tr.tick(tr.now)
    trk = tr.tracks["T1"]
    assert trk.with_drone
    for s in range(40, 52):
        tr.feed(OBS_BLE, {"t": T0 + s * 1000, "src": "omni", "id": "aaaa0001", "cls": "findmy", "rssi": -75})
    assert not trk.with_drone


def test_bad_values_are_skipped():
    bus = Collect()
    tr = Tracker(config.load(), bus)
    tr.feed(POSE, {"t": T0, "x": float("nan"), "y": 0.0, "z": 3.0, "src": "waypoint"})
    assert tr.pose is None and tr.stats["bad_pose"] == 1
    tr.feed(POSE, {"t": T0, "x": 0.0, "y": 0.0, "z": 3.0, "src": "waypoint"})
    for s in range(10):
        t = T0 + s * 1000
        tr.feed(OBS_BLE, {"t": t, "src": "omni", "id": "aaaa0001", "cls": ["findmy"], "rssi": -60})
        tr.feed(OBS_BLE, {"t": t, "src": "omni", "id": "bbbb0002", "cls": "findmy", "rssi": float("nan")})
        tr.feed(OBS_BLE, {"t": t, "src": "omni", "id": "cccc0003", "cls": "findmy",
                          "rssi": float("nan") if s == 5 else -60})
    tr.tick(tr.now + 30_000)
    assert tr.stats["ignored"] == 10 and tr.stats["no_rssi"] == 11
    assert [m["ids"] for m in latest(bus.msgs).values()] == [["cccc0003"]]
    assert all(math.isfinite(m["x"]) and math.isfinite(m["r95"]) for t, m in bus.msgs if t == TRACK)


def test_a_failed_step_is_logged_not_fatal():
    tr = Tracker(config.load(), Collect())
    guarded(tr, lambda: 1 / 0)
    assert tr.stats["errors"] == 1


def test_one_off_ids_never_become_tracks():
    tr, msgs = scene([(s, 0.0, 0.0, [("aaaa0001", -60, "findmy")]) for s in range(2)])
    assert tr.tracks == {} and "aaaa0001" in tr.pending and msgs == []
    tr.tick(tr.now + PENDING_MS + 1)
    assert tr.pending == {} and tr.stats["one_off"] == 1


def test_full_track_list_drops_the_quietest():
    tr = Tracker(config.load(), Collect())
    for i in range(MAX_TRACK):
        trk = tr.new_track("findmy")
        trk.ids, trk.last_t = [f"{i:08x}"], T0 + (5 if trk.name == "T7" else 10 + i) * 1000
        tr.by_id[trk.ids[0]] = trk
    assert tr.new_track("findmy").name == "T7"
    assert len(tr.tracks) == MAX_TRACK and f"{6:08x}" not in tr.by_id  # T7's id is let go
    assert all(tr.by_id[i] is trk for trk in tr.tracks.values() for i in trk.ids)


def test_dry_run_reads_fake_bus_lines():
    fake = subprocess.run([sys.executable, "tools/fake_bus.py", "--print", "--only", "pose,ble", "--duration", "120",
                           "--seed", "3"], cwd=CODE, capture_output=True, text=True, check=True)
    out = subprocess.run([sys.executable, "-m", "afterglow.tracker", "--dry-run"], cwd=CODE, input=fake.stdout,
                         capture_output=True, text=True, check=True)
    topics = {line.split(" ", 1)[0] for line in out.stdout.splitlines()}
    assert TRACK in topics and topics <= {TRACK, PASS}
    assert "stats:" in out.stderr
