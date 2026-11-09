"""geofence.py: the rules per mode, the pin status guard, SOS answers, restarts, and the fake pod
through the real tracker.

The indoor map (afterglow.toml): the trail runs along y = 0 from x = 0 to 9, the campground is
x 5..9, y -4..-1, and offtrail_m is 3.
"""
import math
import os
import random
import sys
import time
from pathlib import Path

import pytest

from afterglow import config
from afterglow.contracts import CTRL, MODE, POSE, SOS, TARGET, THERMAL, TRACK, validate
from afterglow.geofence import STEPS, Geofence, prio
from afterglow.parkmap import ParkMap
from afterglow.tracker import Tracker

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE / "tools"))
from fake_bus import FakePod  # noqa: E402

T0 = 1_731_000_000_000
CFG = config.load(config.DEFAULT)
PM = ParkMap.load(CFG)
OFF_TRAIL = (2.0, 6.0)   # 6 m from the trail
NEAR_TRAIL = (4.5, 1.5)  # 1.5 m from the trail
IN_CAMP = (7.0, -2.5)


class Collect:
    def __init__(self):
        self.msgs = []

    def publish(self, topic, obj, retain=False):
        self.msgs.append((topic, dict(obj, flags=dict(obj["flags"]))))  # a copy: geofence keeps changing its own


NO_HOLD = dict(CFG, rules=dict(CFG["rules"], pin_hold_s=0))  # most tests pin on the first message


def make(pins_file=None, mode=None, cfg=NO_HOLD):
    bus = Collect()
    g = Geofence(cfg, bus, PM, pins_file=pins_file)
    if mode:
        g.feed(MODE, {"t": T0, "mode": mode})
    return g, bus


def track(name="T1", xy=OFF_TRAIL, t=T0, **kw):
    m = {"t": t, "track": name, "ids": [f"{name}-a"], "cls": "findmy", "x": xy[0], "y": xy[1], "r95": 1.5,
         "state": "pinned", "passes": 2, "last_s": 0, "with_drone": False, "baseline": False, "c": 0.85}
    m.update(kw)
    return m


def ctrl(g, cmd, pin, t=T0):
    g.feed(CTRL, {"t": t, "cmd": cmd, "pin": pin})


def latest(bus):
    out = {}
    for topic, m in bus.msgs:
        assert topic == TARGET
        assert validate(TARGET, m) == [], m
        out[m["pin"]] = m
    return out


# ---------- rules ----------

def test_patrol_is_the_default_and_pins_off_trail_after_two_passes():
    g, bus = make()
    g.feed(TRACK, track(passes=1))
    assert latest(bus) == {}
    g.feed(TRACK, track(passes=2))
    p = latest(bus)["P1"]
    assert (p["rule"], p["status"], p["zone"], p["off_trail_m"]) == ("day_offtrail", "new", "off_trail", 6)
    assert math.isclose(p["lat"], CFG["mission"]["origin_lat"] + 6 / 111_320, abs_tol=1e-6)


def test_patrol_ignores_beacons_near_the_trail_and_in_the_campground():
    g, bus = make()
    g.feed(TRACK, track("T1", NEAR_TRAIL, passes=5))
    g.feed(TRACK, track("T2", IN_CAMP, passes=5))
    assert latest(bus) == {}


def test_night_pins_everything_outside_the_campground():
    g, bus = make(mode="night")
    g.feed(TRACK, track("T1", NEAR_TRAIL, passes=1))
    g.feed(TRACK, track("T2", IN_CAMP))
    pins = latest(bus)
    assert [(p["track"], p["rule"]) for p in pins.values()] == [("T1", "night_outside_camp")]


def test_where_rules_wait_until_the_track_is_localized():
    # A pin stays once made, so day and night don't pin a beacon they can't place yet.
    g, bus = make(mode="night")
    g.feed(TRACK, track(state="heard", r95=12.0, c=0.3))
    assert latest(bus) == {}
    g.feed(TRACK, track(state="localized", r95=4.0, c=0.6))
    assert latest(bus)["P1"]["state"] == "localized"


@pytest.mark.parametrize("mode", ["patrol", "night"])
def test_a_beacon_that_may_be_in_the_campground_waits(mode):
    # Just below the campground (bottom edge y = -4), 4.2 m off the trail. With r95 1 m its circle
    # reaches the campground, so it may be in it: no pin. Once r95 is tighter than 0.5 m, it's out.
    g, bus = make(mode=mode)
    g.feed(TRACK, track(xy=(7.0, -4.5), r95=1.0))
    assert latest(bus) == {}
    g.feed(TRACK, track(xy=(7.0, -4.5), r95=0.4))
    assert set(latest(bus)) == {"P1"}


def test_a_rule_must_hold_for_pin_hold_s():
    hold = CFG["rules"]["pin_hold_s"] * 1000
    g, bus = make(cfg=CFG)
    g.feed(TRACK, track(t=T0))
    g.feed(TRACK, track(t=T0 + hold - 1000))
    g.feed(TRACK, track(t=T0 + hold, state="heard"))  # the rule lapses: the hold starts again
    g.feed(TRACK, track(t=T0 + hold + 1000))
    g.feed(TRACK, track(t=T0 + 2 * hold))
    assert latest(bus) == {}
    g.feed(TRACK, track(t=T0 + 2 * hold + 1000))
    assert latest(bus)["P1"]["t"] == T0 + 2 * hold + 1000


def test_search_waits_too_so_our_own_phone_can_be_marked_first():
    hold = CFG["rules"]["pin_hold_s"] * 1000
    g, bus = make(mode="search", cfg=CFG)
    g.feed(TRACK, track("T1", t=T0, state="heard"))
    g.feed(TRACK, track("T1", t=T0 + hold // 2, state="heard", with_drone=True))  # it rides on the pod
    g.feed(TRACK, track("T1", t=T0 + 2 * hold, state="heard", with_drone=True))
    assert latest(bus) == {}


def test_the_hold_starts_again_once_a_with_drone_mark_clears():
    hold = CFG["rules"]["pin_hold_s"] * 1000
    g, bus = make(mode="search", cfg=CFG)
    g.feed(TRACK, track(t=T0, state="heard"))
    g.feed(TRACK, track(t=T0 + 1000, state="heard", with_drone=True))
    g.feed(TRACK, track(t=T0 + hold + 5000, state="heard"))  # the mark clears: its estimate is fresh again
    assert latest(bus) == {}
    g.feed(TRACK, track(t=T0 + 2 * hold + 5000, state="heard"))
    assert set(latest(bus)) == {"P1"}


def test_bus_time_going_back_is_followed():
    # fake_bus --speed ran an hour ahead, then the real pod took over with geofence still running
    hold = CFG["rules"]["pin_hold_s"] * 1000
    g, bus = make(cfg=CFG)
    g.feed(TRACK, track(t=T0 + 3_600_000))  # T1's hold starts in the old source's time
    g.feed(TRACK, track(t=T0))
    g.feed(TRACK, track(t=T0 + hold))  # the reused T1 isn't stuck waiting an hour
    ctrl(g, "ack", "P1")
    p = latest(bus)["P1"]
    assert (p["status"], p["t"]) == ("acknowledged", T0 + hold)  # not stamped an hour ahead


def test_search_pins_lost_classes_anywhere_even_unplaced():
    g, bus = make(mode="search")
    g.feed(TRACK, track("T1", IN_CAMP, cls="findhub", state="heard", passes=0, c=0.2))
    g.feed(TRACK, track("T2", OFF_TRAIL, cls="ref"))   # our reference beacon isn't a lost device
    g.feed(TRACK, track("T3", OFF_TRAIL, cls="wifi"))
    pins = latest(bus)
    assert [(p["track"], p["rule"], p["prio"]) for p in pins.values()] == [("T1", "search", 4)]


def test_investigate_keeps_the_last_other_modes_rule():
    g, bus = make(mode="night")
    g.feed(MODE, {"t": T0, "mode": "investigate"})
    g.feed(TRACK, track("T1", NEAR_TRAIL))
    assert latest(bus)["P1"]["rule"] == "night_outside_camp"


@pytest.mark.parametrize("mode", ["patrol", "night", "search"])
def test_with_drone_baseline_and_wifi_are_never_pinned(mode):
    g, bus = make(mode=mode)
    g.feed(TRACK, track("T1", with_drone=True, state="heard", c=0.0))
    g.feed(TRACK, track("T2", baseline=True))
    g.feed(TRACK, track("T3", cls="wifi"))
    assert latest(bus) == {}


# ---------- filling a pin ----------

def test_pin_follows_its_track():
    g, bus = make()
    g.feed(TRACK, track())
    g.feed(TRACK, track(t=T0 + 1000, xy=(2.5, 6.5), r95=1.0, state="pinned", c=0.9, last_s=3))
    p = latest(bus)["P1"]
    assert (p["t"], p["x"], p["y"], p["r95"], p["c"], p["state"], p["last_s"]) == (T0 + 1000, 2.5, 6.5, 1.0, 0.9, "pinned", 3)
    assert p["prio"] == 2 and p["flags"] == {"sos": False, "thermal": False, "radar": False, "camera": False}


def test_pins_stay_when_the_mode_changes_or_the_estimate_moves_onto_the_trail():
    # The drone never decides, so it doesn't take a pin back either: only the ranger clears it.
    g, bus = make()
    g.feed(TRACK, track())
    g.feed(MODE, {"t": T0, "mode": "search"})
    g.feed(TRACK, track(t=T0 + 1000, xy=NEAR_TRAIL, cls="ref"))
    p = latest(bus)["P1"]
    assert (p["x"], p["y"], p["rule"], p["zone"]) == (4.5, 1.5, "day_offtrail", "trail_corridor")


def test_a_pin_stays_put_while_its_track_is_with_drone():
    # Picked up, or the pod right over it: the tracker then gives the pod's position, heard, c 0.
    g, bus = make()
    g.feed(TRACK, track())
    n = len(bus.msgs)
    g.feed(TRACK, track(t=T0 + 1000, xy=(5.0, 0.0), with_drone=True, state="heard", c=0.0, r95=10.0))
    assert len(bus.msgs) == n
    g.feed(TRACK, track(t=T0 + 2000, xy=(2.1, 6.0)))  # the mark clears: it follows its track again
    p = latest(bus)["P1"]
    assert (p["x"], p["state"], p["prio"]) == (2.1, "pinned", 2)


@pytest.mark.parametrize("p,want", [
    ({"state": "pinned", "c": 0.8}, 2),
    ({"state": "pinned", "c": 0.7}, 3),
    ({"state": "pinned", "c": 0.3}, 3),
    ({"state": "localized", "c": 0.3}, 3),
    ({"state": "heard", "c": 0.5}, 3),
    ({"state": "heard", "c": 0.4}, 4),
    ({"state": "heard", "c": 0.0, "rule": "sos"}, 2),
    ({"state": "heard", "c": 0.0, "sos": {"hurt": True}}, 1),
    ({"state": "confirmed", "c": 0.2, "status": "confirmed"}, 1),
])
def test_prio(p, want):
    assert prio(p) == want


# ---------- the ranger's clicks ----------

ALLOWED = {
    ("ack", "new"): "acknowledged",
    ("investigate", "new"): "investigating",
    ("investigate", "acknowledged"): "investigating",
    ("clear", "new"): "cleared",
    ("clear", "acknowledged"): "cleared",
    ("clear", "investigating"): "cleared",
    ("confirm", "investigating"): "confirmed",
}
STATUSES = ["new", "acknowledged", "investigating", "confirmed", "cleared"]


@pytest.mark.parametrize("cmd", list(STEPS))
@pytest.mark.parametrize("start", STATUSES)
def test_status_changes_only_by_the_allowed_steps(cmd, start):
    g, bus = make()
    g.feed(TRACK, track())
    g.pins["P1"]["status"] = start
    n = len(bus.msgs)
    ctrl(g, cmd, "P1")
    want = ALLOWED.get((cmd, start))
    if want is None:
        assert len(bus.msgs) == n and g.pins["P1"]["status"] == start  # ignored, nothing published
    else:
        assert bus.msgs[-1][1]["status"] == want  # published at once, so the dashboard stops waiting


def test_confirm_only_from_investigating_and_it_sticks():
    g, bus = make()
    g.feed(TRACK, track(state="localized", c=0.5))
    ctrl(g, "confirm", "P1")
    assert latest(bus)["P1"]["status"] == "new"
    ctrl(g, "investigate", "P1")
    ctrl(g, "confirm", "P1")
    p = latest(bus)["P1"]
    assert (p["status"], p["state"], p["prio"]) == ("confirmed", "confirmed", 1)
    g.feed(TRACK, track(t=T0 + 1000, state="pinned", xy=(2.2, 6.1)))  # still follows the track...
    p = latest(bus)["P1"]
    assert (p["x"], p["state"], p["prio"]) == (2.2, "confirmed", 1)  # ...but stays confirmed


def test_a_cleared_pin_freezes_and_its_track_never_gets_another():
    g, bus = make()
    g.feed(TRACK, track())
    ctrl(g, "clear", "P1")
    n = len(bus.msgs)
    g.feed(TRACK, track(t=T0 + 1000, xy=(3.0, 6.0)))
    assert len(bus.msgs) == n
    assert set(latest(bus)) == {"P1"} and latest(bus)["P1"]["x"] == 2.0


def test_a_click_on_a_quiet_pin_keeps_when_it_was_last_heard():
    # The dashboard and lora_out take t - last_s as when it was last heard: a click isn't a sighting.
    g, bus = make()
    g.feed(TRACK, track(last_s=2))
    g.feed(CTRL, {"t": T0, "cmd": "new_search"})  # its track is gone, so the pin goes quiet
    g.feed(POSE, {"t": T0 + 60_000, "x": 1.0, "y": 1.0, "z": 3.0, "src": "waypoint"})
    ctrl(g, "investigate", "P1")
    p = latest(bus)["P1"]
    assert p["t"] == T0 + 60_000 and p["t"] - p["last_s"] * 1000 == T0 - 2000
    sos(g, hurt=True, t=T0 + 90_000)  # nor is an SOS answer, which came over Wi-Fi
    p = latest(bus)["P1"]
    assert p["t"] == T0 + 90_000 and p["t"] - p["last_s"] * 1000 == T0 - 2000


def test_commands_for_unknown_pins_are_ignored():
    g, bus = make()
    ctrl(g, "ack", "P9")
    assert bus.msgs == []


# ---------- SOS answers ----------

def sos(g, hurt, t=T0, walk=False, count=1):
    g.feed(SOS, {"t": t, "hurt": hurt, "walk": walk, "count": count, "rssi": -60})


def test_sos_goes_on_the_pin_investigated_last():
    g, bus = make()
    g.feed(TRACK, track("T1"))
    g.feed(TRACK, track("T2", (3.0, 6.0), state="localized", c=0.6))
    ctrl(g, "investigate", "P2", t=T0)
    g.feed(POSE, {"t": T0 + 5000, "x": 1.0, "y": 1.0, "z": 3.0, "src": "waypoint"})
    ctrl(g, "investigate", "P1")
    sos(g, hurt=False, t=T0 + 6000, count=2)
    pins = latest(bus)
    assert pins["P1"]["sos"] == {"hurt": False, "walk": False, "count": 2} and pins["P1"]["flags"]["sos"]
    assert "sos" not in pins["P2"] and pins["P1"]["prio"] == 2
    sos(g, hurt=True, t=T0 + 7000)
    assert latest(bus)["P1"]["prio"] == 1


def test_sos_with_nothing_investigated_opens_one_sos_pin_at_the_pod():
    g, bus = make()
    g.feed(POSE, {"t": T0, "x": 3.0, "y": 3.0, "z": 3.0, "src": "waypoint"})
    sos(g, hurt=False)
    p = latest(bus)["P1"]
    assert (p["track"], p["rule"], p["cls"], p["state"], p["c"], p["last_s"], p["prio"]) == \
        ("T0", "sos", "wifi", "heard", 0.0, 0, 2)
    assert (p["x"], p["y"], p["r95"], p["status"]) == (3.0, 3.0, CFG["rules"]["sos_r95_m"], "new")
    sos(g, hurt=True, t=T0 + 1000)  # a second answer updates the same pin
    assert set(latest(bus)) == {"P1"} and latest(bus)["P1"]["prio"] == 1
    ctrl(g, "clear", "P1")
    sos(g, hurt=False, t=T0 + 2000)  # once it's cleared, a new answer opens a new one
    assert latest(bus)["P2"]["rule"] == "sos"


def test_a_new_search_leaves_the_old_sos_pin_alone():
    g, bus = make()
    sos(g, hurt=False)
    g.feed(CTRL, {"t": T0, "cmd": "new_search"})
    sos(g, hurt=True, t=T0 + 1000)
    pins = latest(bus)
    assert pins["P1"]["sos"]["hurt"] is False and pins["P2"]["rule"] == "sos" and pins["P2"]["prio"] == 1


# ---------- thermal flag ----------

def heat(g, hot=True, t=T0):
    g.feed(THERMAL, {"t": t, "max_c": 34.0 if hot else 18.0, "hot": hot, "conf": 0.8, "src": "ht203u"})


def pod(g, xy, t=T0):
    g.feed(POSE, {"t": t, "x": xy[0], "y": xy[1], "z": 3.0, "src": "waypoint"})


def test_thermal_flags_the_investigated_pin_once_the_pod_is_over_it():
    g, bus = make()
    g.feed(TRACK, track("T1"))  # P1 at (2, 6), r95 1.5
    pod(g, (2.0, 6.0))
    heat(g)
    assert not latest(bus)["P1"]["flags"]["thermal"]  # not under investigation yet
    ctrl(g, "investigate", "P1")
    pod(g, (2.0, 6.0 - 1.5 - CFG["rules"]["thermal_near_m"] - 1), t=T0 + 1000)
    heat(g, t=T0 + 1000)
    assert not latest(bus)["P1"]["flags"]["thermal"]  # on the way there: that heat is elsewhere
    pod(g, (2.0, 5.0), t=T0 + 2000)
    heat(g, hot=False, t=T0 + 2000)
    assert not latest(bus)["P1"]["flags"]["thermal"]
    n = len(bus.msgs)
    heat(g, t=T0 + 3000)
    p = latest(bus)["P1"]
    assert p["flags"]["thermal"] and p["status"] == "investigating" and p["state"] == "pinned"
    assert (p["t"], p["last_s"]) == (T0 + 3000, 3)  # restamped; when it was last heard stays put
    heat(g, hot=False, t=T0 + 4000)
    heat(g, t=T0 + 5000)
    assert len(bus.msgs) == n + 1 and latest(bus)["P1"]["flags"]["thermal"]  # set once, and it stays


def test_thermal_goes_on_the_pin_investigated_last_and_needs_a_pose():
    g, bus = make()
    g.feed(TRACK, track("T1"))
    g.feed(TRACK, track("T2", (3.0, 6.0), state="localized", c=0.6))
    ctrl(g, "investigate", "P1")
    heat(g)
    assert not latest(bus)["P1"]["flags"]["thermal"]  # no pose: can't tell the pod is there
    pod(g, (2.5, 6.0), t=T0 + 1000)  # near both
    ctrl(g, "investigate", "P2", t=T0 + 1000)
    heat(g, t=T0 + 1000)
    pins = latest(bus)
    assert pins["P2"]["flags"]["thermal"] and not pins["P1"]["flags"]["thermal"]


# ---------- new searches and restarts ----------

def test_new_search_unlinks_tracks_but_keeps_the_pins():
    g, bus = make()
    g.feed(TRACK, track("T1"))
    g.feed(CTRL, {"t": T0, "cmd": "new_search"})
    g.feed(TRACK, track("T2", (3.0, 6.0), ids=["new-a"]))
    assert set(latest(bus)) == {"P1", "P2"} and latest(bus)["P1"]["x"] == 2.0


def test_key_rotation_keeps_the_pin_but_a_reused_track_name_gets_a_new_one():
    g, bus = make()
    g.feed(TRACK, track("T1", ids=["a"]))
    g.feed(TRACK, track("T1", t=T0 + 1000, ids=["a", "b"]))  # the tracker followed a key rotation
    g.feed(TRACK, track("T1", t=T0 + 2000, ids=["b"]))
    assert set(latest(bus)) == {"P1"}
    g.feed(TRACK, track("T1", (3.0, 6.0), t=T0 + 3000, ids=["z"]))  # tracker restarted: T1 is another beacon
    pins = latest(bus)
    assert set(pins) == {"P1", "P2"} and pins["P1"]["x"] == 2.0 and pins["P2"]["x"] == 3.0


def test_pins_survive_a_restart(tmp_path):
    f = tmp_path / "pins.json"
    g, _ = make(pins_file=f, mode="night")
    g.feed(TRACK, track("T1"))
    g.feed(TRACK, track("T2", NEAR_TRAIL, ids=["T2-a"]))
    ctrl(g, "investigate", "P2")
    g.feed(MODE, {"t": T0, "mode": "investigate"})

    g2, bus2 = make(pins_file=f)
    g2.restore()
    pins = latest(bus2)  # republished at once, so a restarted dashboard has them too
    assert set(pins) == {"P1", "P2"} and pins["P2"]["status"] == "investigating"
    g2.feed(MODE, {"t": T0, "mode": "investigate"})  # the retained mode arrives
    g2.feed(TRACK, track("T2", NEAR_TRAIL, t=T0 + 1000, ids=["T2-a"]))
    g2.feed(TRACK, track("T3", (8.0, 5.0), t=T0 + 1000))  # still the night rule
    pins = latest(bus2)
    assert pins["P2"]["t"] == T0 + 1000 and pins["P3"]["rule"] == "night_outside_camp"
    sos(g2, hurt=True, t=T0 + 2000)
    assert latest(bus2)["P2"]["prio"] == 1


def test_a_new_pin_is_saved_whole(tmp_path):
    # Found live: the save ran before the pin was filled in, and a restart republished half a pin.
    f = tmp_path / "pins.json"
    g, _ = make(pins_file=f)
    g.feed(TRACK, track("T1"))
    g2, bus2 = make(pins_file=f)
    g2.restore()
    assert latest(bus2)["P1"]["x"] == 2.0  # latest() checks every message against the contract


def test_old_saved_pins_are_ignored(tmp_path):
    f = tmp_path / "pins.json"
    g, _ = make(pins_file=f)
    g.feed(TRACK, track("T1"))
    old = time.time() - CFG["rules"]["pins_max_age_min"] * 60 - 10
    os.utime(f, (old, old))
    g2, bus2 = make(pins_file=f)
    g2.restore()
    g2.feed(TRACK, track("T5", (3.0, 6.0)))
    assert set(latest(bus2)) == {"P1"} and latest(bus2)["P1"]["track"] == "T5"


def test_a_broken_save_file_starts_fresh(tmp_path):
    f = tmp_path / "pins.json"
    f.write_text("{not json")
    g, bus = make(pins_file=f)
    g.restore()
    g.feed(TRACK, track())
    assert set(latest(bus)) == {"P1"}


# ---------- the fake pod through the real tracker ----------

@pytest.fixture(scope="module")
def tracks():
    """ag/pose and ag/track from 20 indoor minutes of the fake pod, through the real tracker."""
    pod = FakePod(CFG, PM, random.Random(7), only={"pose", "ble"})
    out = []

    class Keep:
        def publish(self, topic, msg, retain=False):
            if topic == TRACK:
                out.append((topic, msg))

    tr = Tracker(CFG, Keep())
    for topic, msg, _ in pod.run(T0, 1200):
        tr.feed(topic, msg)
        if topic == POSE:
            out.append((topic, msg))
    return out


def pinned_phones(msgs, mode):
    g, bus = make(mode=mode, cfg=CFG)
    for topic, msg in msgs:
        g.feed(topic, msg)
    hidden = {p["name"]: (p["x"], p["y"]) for p in CFG["sim"]["phones"] if "x" in p}
    names = {}
    for p in latest(bus).values():
        name = min(hidden, key=lambda n: math.dist(hidden[n], (p["x"], p["y"])))
        assert name not in names, f"two pins for {name}"
        names[name] = p["rule"]
    return names


def test_the_demo_script_on_the_fake_pod(tracks):
    # demo step 4: patrol pins the off-trail phone; step 8: night ignores the campground tag
    assert pinned_phones(tracks, "patrol") == {"dead_iphone": "day_offtrail"}
    assert pinned_phones(tracks, "night") == {"dead_iphone": "night_outside_camp",
                                              "pixel_off": "night_outside_camp"}
    assert pinned_phones(tracks, "search") == {"dead_iphone": "search", "airtag_camp": "search",
                                               "pixel_off": "search"}
