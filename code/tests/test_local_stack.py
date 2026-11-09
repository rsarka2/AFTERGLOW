"""tools/local_stack.py: the fake pod through the real tracker and geofence, with no broker,
and the live map feed's files."""
import random
import sys
from pathlib import Path

from afterglow import config
from afterglow.contracts import CTRL, TARGET, now_ms, validate
from afterglow.dashboard import Commands

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import local_stack  # noqa: E402
from fake_bus import FakePod  # noqa: E402

CODE = Path(__file__).resolve().parent.parent
WEB = CODE / "afterglow" / "web"


def run(cfg, seconds, seed=3):
    bus = local_stack.LocalBus()
    pm, meta, state, tr, g = local_stack.build(cfg, bus)
    seen = []
    bus.subscribe("ag/#", lambda t, m: seen.append((t, m)))
    pod = FakePod(cfg, pm, random.Random(seed), only={"pose", "ble"})
    for topic, msg, retain in pod.run(now_ms(), seconds):
        bus.publish(topic, msg, retain=retain)
    return bus, state, g, seen


def test_local_bus_matches_wildcards_and_copies():
    bus = local_stack.LocalBus()
    got = []
    bus.subscribe("ag/obs/#", lambda t, m: got.append((t, m)))
    bus.subscribe("ag/pose", lambda t, m: got.append((t, m)))
    msg = {"t": 1, "x": 0.0}
    bus.publish("ag/obs/ble", msg)
    bus.publish("ag/pose", msg)
    bus.publish("ag/track", msg)
    assert [t for t, _ in got] == ["ag/obs/ble", "ag/pose"]
    got[0][1]["x"] = 99.0  # each subscriber gets its own copy, as over MQTT
    assert msg["x"] == 0.0 and got[1][1]["x"] == 0.0


def test_park_run_makes_real_pins_and_clicks_change_them():
    cfg = config.load(CODE / "park.toml")
    bus, state, g, seen = run(cfg, 1500)
    pins = [m for t, m in seen if t == TARGET]
    assert pins, "the real geofence made no pin in 25 simulated minutes"
    assert all(not validate(t, m) for t, m in seen if t in (TARGET, "ag/track"))
    assert state.poses and state.tracks and state.pins  # the dashboard saw everything
    cov = state.latest.get("ag/coverage")  # from the real locate.py, not a stand-in
    assert cov and not validate("ag/coverage", cov) and cov["pct"] > 0
    assert any(tr.get("with_drone") for tr in state.tracks.values())  # the team phone on the pod
    pin = sorted(state.pins)[0]
    assert state.pins[pin]["status"] == "new"  # nothing moves a pin on its own
    cmds = Commands(lambda: bus, cfg["waypoints"])
    assert cmds.ctrl({"cmd": "confirm", "pin": pin})[0] == 200  # sent, but geofence refuses: not investigating
    assert state.pins[pin]["status"] == "new"
    for cmd, want in (("investigate", "investigating"), ("confirm", "confirmed")):
        code, out = cmds.ctrl({"cmd": cmd, "pin": pin})
        assert code == 200 and not validate(CTRL, out["msg"])
        assert state.pins[pin]["status"] == want
    assert state.pins[pin]["state"] == "confirmed"


def test_livemap_is_self_contained_and_draws_only():
    html = (WEB / "tv.html").read_text(encoding="utf-8")
    js = (WEB / "livemap.js").read_text(encoding="utf-8")
    assert 'src="vendor/maplibre-gl-4.7.1/maplibre-gl.js"' in html
    assert "from './livemap.js'" in (WEB / "tv.js").read_text(encoding="utf-8")
    assert (WEB / "vendor" / "maplibre-gl-4.7.1" / "maplibre-gl.js").stat().st_size > 500_000
    assert "import('./store.js')" in js  # the same reducer as the dashboard
    # It only draws: no commands, so the map can never act for the ranger
    assert "/api/ctrl" not in js and "/api/mode" not in js and "method: 'POST'" not in js
    # The methods the TV calls
    for name in ("constructor(el, meta", "update(state", "setLayer(name", "flyTo(x, y)", "fit()", "destroy()"):
        assert name in js
    assert "cdn." not in html and "unpkg" not in html
