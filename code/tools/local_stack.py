#!/usr/bin/env python3
"""The laptop stack in one process, with no MQTT broker: a source -> the real tracker, geofence
and locate -> the dashboard. For laptops without Mosquitto (Windows), and as a demo backup.

    python tools/local_stack.py --config park.toml --speed 5          # fake pod (fake_bus.py's [sim])
    python tools/local_stack.py --config park.toml --replay runs/1432.log --loop   # a recorded real run
    python tools/local_stack.py --config park.toml --feeds            # plus fake camera and thermal pictures

The fake pod waits at [sim] base_xy (on the ground, scanners off) until "Send out" on the TV, then
flies its lanes; "Return to base" brings it back. Both are POST /api/sim {"flight": "out" | "home"}.
    then open http://localhost:<[http] port> (the operator TV; park.toml 8081, afterglow.toml 8080)

Sources:
    fake (default)  tools/fake_bus.py's FakePod, publishing pose, ble and its stand-in thermal and camera. Its cheating
                    stand-ins stay off: the real tracker.py, geofence.py and locate.py make every
                    track, pin and coverage grid.
    --replay LOG    a `mosquitto_sub -v` recording, through tools/replay.py's parser. Only the raw
                    inputs are replayed (pose, obs, sos, sensors, sys), so the tracks, pins and
                    coverage are made again by today's tracker, geofence and locate. pose src becomes "replay".

Clicks on either page go through the dashboard's /api/ctrl to the real geofence, so Ack,
Investigate, Clear and Confirm change pins exactly as they do on the pod. Nothing here clicks
by itself: the drone never decides. Pins aren't saved (no pins_file), so each run starts at P1.
Trail pathing: with a case open on the TV and the mode on "search", the fake pod flies the
case's trail (tools/fake_bus.py, Pather).
"""
import argparse
import json
import logging
import random
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import paho.mqtt.client as mqtt

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS.parent))
sys.path.insert(0, str(TOOLS))
from afterglow import config as cfgmod  # noqa: E402
from afterglow.case import CaseStore  # noqa: E402
from afterglow.contracts import ALL, CTRL, MODE, POSE, SOS, TARGET, THERMAL, TRACK, now_ms  # noqa: E402
from afterglow.dashboard import Commands, State, make_handler, make_meta  # noqa: E402
from afterglow.geofence import Geofence  # noqa: E402
from afterglow.locate import MIN_WALL_S as LOCATE_MIN_WALL_S  # noqa: E402
from afterglow.locate import Locate  # noqa: E402
from afterglow.locate import guarded as locate_guarded  # noqa: E402
from afterglow.parkmap import ParkMap  # noqa: E402
from afterglow.planner import Planner  # noqa: E402
from afterglow.tracker import Tracker, guarded  # noqa: E402

log = logging.getLogger("local_stack")

# What a replay feeds in: the pod's raw outputs. Tracks and pins are made again here.
REPLAY_INPUTS = ("ag/pose", "ag/obs/#", "ag/sos", "ag/thermal", "ag/vision", "ag/radar", "ag/sys", "ag/alert")


class LocalBus:
    """In-process stand-in for afterglow.bus.Bus. publish() hands a copy of the message to every
    matching subscriber at once, under one lock, so modules see messages in a single order."""

    def __init__(self):
        self.handlers = []  # (pattern, fn); several per pattern, unlike Bus
        self.lock = threading.RLock()  # re-entrant: a handler may publish (tracker -> geofence)

    def subscribe(self, pattern, fn):
        self.handlers.append((pattern, fn))

    def publish(self, topic, obj, retain=False):
        payload = json.dumps(obj, separators=(",", ":"))  # a copy, as on the wire: no shared dicts
        with self.lock:
            for pattern, fn in list(self.handlers):
                if mqtt.topic_matches_sub(pattern, topic):
                    try:
                        fn(topic, json.loads(payload))
                    except Exception:
                        log.exception("handler for %s failed on %s", pattern, topic)

    def clear_retained(self, topic):
        pass

    def close(self):
        pass


def build(cfg, bus):
    """Wire the dashboard state, the tracker, the geofence and locate onto the bus."""
    pm = ParkMap.load(cfg)
    meta = make_meta(cfg, pm)
    state = State(meta["trail_step_m"])
    tr, g = wire(cfg, bus, state, pm)
    return pm, meta, state, tr, g


def wire(cfg, bus, state, pm):
    """A fresh tracker, geofence, locate and planner on the bus, feeding the dashboard state."""
    state.broker = True  # there's no broker to lose: the in-process bus is always there
    bus.subscribe(ALL, state.on_msg)
    tr = Tracker(cfg, bus)
    for topic in (POSE, "ag/obs/#", CTRL):
        bus.subscribe(topic, lambda t, m: guarded(tr, tr.feed, t, m))
    g = Geofence(cfg, bus, pm=pm)  # no pins_file: nothing saved, every run starts at P1
    for topic in (TRACK, POSE, MODE, CTRL, SOS, THERMAL):
        bus.subscribe(topic, g.feed)
    loc = Locate(cfg, bus, pm=pm)
    last = [0.0]

    def to_locate(topic, msg):
        if not (topic == POSE and msg.get("src") == "sim" and msg.get("z") == 0):  # docked at base: searches nothing
            locate_guarded(loc, loc.feed, topic, msg)
        if time.monotonic() - last[0] >= LOCATE_MIN_WALL_S:  # at most 2 grids a wall second, like locate.py live
            if locate_guarded(loc, loc.maybe_publish):
                last[0] = time.monotonic()
    for topic in (POSE, TRACK, CTRL):
        bus.subscribe(topic, to_locate)
    pl = Planner(cfg, bus, pm=pm)  # proposes legs only; the fake pod doesn't fly them (yet)
    for topic in (TARGET, POSE, MODE, CTRL):
        bus.subscribe(topic, pl.feed)
    return tr, g


class SimClock:
    """The fake pod's clock: simulated time runs at `speed` times the wall clock, or stops while
    paused. The TV's pause and speed buttons set it (POST /api/sim). Sim only: the real pod
    can't be paused."""
    SPEEDS = (1, 2, 4, 10)

    def __init__(self, speed=1.0):
        self.lock = threading.Lock()
        self.speed, self.paused = float(speed), False
        self.sim_ms, self.last = 0.0, time.monotonic()
        self.listeners = []
        self.on_reset = None  # set by main(): back to time zero
        self.pod = None  # the fake pod now running (set by main(); replaced on a reset)
        self.base = None  # {"name", "x", "y"}: where it waits for "Send out"

    def restart(self):
        with self.lock:
            self.sim_ms, self.last, self.paused = 0.0, time.monotonic(), False

    def elapsed_ms(self):
        with self.lock:
            now = time.monotonic()
            if not self.paused:
                self.sim_ms += (now - self.last) * 1000 * self.speed
            self.last = now
            return self.sim_ms

    def view(self):
        """flight: "docked" (waiting at base), "out" (flying its path) or "returning"; plan: the lanes it flies."""
        pod = self.pod
        return {"paused": self.paused, "speed": self.speed, "flight": pod.flight if pod else None,
                "base": self.base, "plan": pod.plan() if pod else []}

    def changed(self):
        for fn in list(self.listeners):
            fn(self.view())

    def set(self, body):
        if body.get("reset") is True:  # the TV's Reset button: everything back to time zero
            if self.on_reset:
                self.on_reset()
            return []
        if "flight" in body:  # the ranger's "Send out" / "Return to base" (sim only: the pilot flies the real pod)
            if body["flight"] not in ("out", "home") or self.pod is None:
                return ['flight must be "out" or "home"']
            self.pod.launch() if body["flight"] == "out" else self.pod.go_home()
            log.info("sim: %s", "sent out" if body["flight"] == "out" else "returning to base")
            self.changed()
            return []
        problems = []
        speed = body.get("speed", self.speed)
        if not isinstance(speed, (int, float)) or isinstance(speed, bool) or speed not in self.SPEEDS:
            problems.append(f"speed must be one of {list(self.SPEEDS)}")
        paused = body.get("paused", self.paused)
        if not isinstance(paused, bool):
            problems.append("paused must be true or false")
        if problems:
            return problems
        self.elapsed_ms()  # bank the time run so far at the old speed
        with self.lock:
            self.speed, self.paused = float(speed), paused
        log.info("sim %s at %gx", "paused" if paused else "running", self.speed)
        self.changed()
        return []


def make_pod(cfg, pm, bus, cases, seed):
    """The fake pod, following the ranger's mode and the open case's trail (sim only)."""
    from fake_bus import FakePod
    pod = FakePod(cfg, pm, random.Random(seed), only={"pose", "ble", "thermal", "vision", "sys"}, docked=True)
    pod.set_route(cases.view()["route"])

    def on_case(view):
        pod.set_route(view["route"] if view["case"] else None)
        if not view["case"]:
            pod.stop_watching()  # case closed: no more check-ins on the person
    cases.listeners.append(on_case)
    bus.subscribe(MODE, lambda t, m: pod.on_mode(m))
    bus.subscribe(TARGET, lambda t, m: pod.on_target(m))  # where each pin is, for Investigate
    bus.subscribe(CTRL, lambda t, m: pod.on_ctrl(m))  # Investigate flies there, Clear goes back
    return pod


def run_fake(pod, bus, clock, stop):
    t0 = now_ms()
    flight = pod.flight
    for topic, msg, retain in pod.run(t0):
        if pod.flight != flight:  # it landed back at base: tell the TVs
            flight = pod.flight
            clock.changed()
        while not stop.is_set():  # wait until simulated time reaches the message (forever while paused)
            ahead = msg["t"] - t0 - clock.elapsed_ms()
            if ahead <= 0:
                break
            stop.wait(min(0.1, ahead / 1000 / max(clock.speed, 0.1)))
        if stop.is_set():
            return
        bus.publish(topic, msg, retain=retain)


def run_replay(path, bus, speed, loop, stop):
    from replay import Opts, parse, schedule
    lines = Path(path).read_text(errors="replace").splitlines()
    o = Opts(speed=speed, topics=REPLAY_INPUTS)
    while not stop.is_set():
        start, sent = time.monotonic(), 0
        for offset, topic, msg, retain in schedule(parse(lines), o):
            wait = start + offset - time.monotonic()
            if wait > 0 and stop.wait(wait):
                return
            if "t" in msg:
                msg["t"] = now_ms()  # moved to now, keeping the gaps, as replay.py does
            bus.publish(topic, msg, retain=retain)
            sent += 1
        log.info("replay: %d messages", sent)
        if not loop or sent == 0:
            return


class _NoBus:
    """thermal.py's --feeds stand-in bus: the picture only (the source's own ag/thermal stays the reading)."""

    def publish(self, topic, msg, retain=False):
        pass


def run_feeds(cfg, live_dir, stop, view=None):
    """--feeds: camera.py --fake and thermal.py --fake pictures in [http] live_dir, for the TV's camera
    and heat panels. view: a function giving the fake pod's {"hot", "person"}, so the heat blob and
    the person show only while the drone is over someone; None (replay) = their own loops."""
    from afterglow import camera, thermal
    person = (lambda: view()["person"]) if view else None
    warm = (lambda: view()["hot"]) if view else None
    cam = camera.Camera(cfg, live_dir)
    th = thermal.Thermal(cfg, _NoBus(), live_dir)

    def loop(name, frames, handle, fps):
        period, due = 1.0 / fps, time.monotonic()
        try:
            for f in frames:
                handle(f)
                due = max(due + period, time.monotonic() - period)
                if stop.wait(max(0.0, due - time.monotonic())):
                    return
        except Exception:  # noqa: BLE001 - a broken fake picture must not stop the stack
            log.exception("feeds: %s stopped", name)

    threading.Thread(target=loop, args=("camera", camera.fake_frames(cfg, person=person), lambda f: cam.handle_frame(*f),
                                        float(cfg["camera"]["fps"])), daemon=True, name="feed-camera").start()
    threading.Thread(target=loop, args=("thermal", thermal.fake_frames(person=warm), lambda f: th.handle_frame(*f),
                                        float(cfg["thermal"]["fps"])), daemon=True, name="feed-thermal").start()
    log.info("feeds: fake camera and thermal pictures in %s", live_dir)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="profile: afterglow.toml (default) or park.toml")
    ap.add_argument("--speed", type=float, default=1.0, help="time multiplier for the source (fake pod: 1, 2, 4 or 10; the TV can change it)")
    ap.add_argument("--seed", type=int, default=6, help="fake pod random seed (default 6: after Send out, the park demo's false signal at ~1:15, the person ~1 min later)")
    ap.add_argument("--replay", metavar="LOG", help="replay a mosquitto_sub -v log instead of the fake pod")
    ap.add_argument("--loop", action="store_true", help="with --replay: start again at the end")
    ap.add_argument("--host", default="127.0.0.1", help="address to serve on (0.0.0.0 = every interface)")
    ap.add_argument("--port", type=int, help="HTTP port (default: [http] port)")
    ap.add_argument("--feeds", action="store_true", help="also write fake camera and thermal pictures to live/ (the TV's panels)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    cfg = cfgmod.load(a.config)
    holder = {"bus": LocalBus()}  # replaced on a reset
    pm, meta, state, _, _ = build(cfg, holder["bus"])
    cmds = Commands(lambda: holder["bus"], cfg["waypoints"])
    dirs = {k: cfgmod.resolve(cfg, cfg["http"][f"{k}_dir"]) for k in ("live", "snaps")}
    cases = CaseStore(cfg, pm)
    to_browsers = lambda view: state.broadcast("case", view)  # noqa: E731
    cases.listeners.append(to_browsers)
    stop = threading.Event()
    clock = None
    run = {}
    if a.replay:
        threading.Thread(target=lambda: run_replay(a.replay, holder["bus"], a.speed, a.loop, stop), daemon=True, name="source").start()
    else:
        clock = SimClock(a.speed if a.speed in SimClock.SPEEDS else 1)
        clock.listeners.append(lambda view: state.broadcast("sim", view))

        bx, by = cfg["sim"]["base_xy"]
        clock.base = {"name": cfg["sim"]["base_name"], "x": bx, "y": by}

        def start_pod():
            run["stop"] = threading.Event()
            pod = make_pod(cfg, pm, holder["bus"], cases, a.seed)
            clock.pod = pod
            run["thread"] = threading.Thread(target=run_fake, args=(pod, holder["bus"], clock, run["stop"]), daemon=True, name="source")
            run["thread"].start()

        def reset():
            """Back to time zero: a new fake pod, tracker, geofence and locate on a new bus; no pins,
            no case, mode back to patrol (nothing retained). The browsers reload."""
            run["stop"].set()
            run["thread"].join(timeout=3)
            holder["bus"] = LocalBus()
            with state.lock:
                state.clear()
            wire(cfg, holder["bus"], state, pm)
            cases.listeners[:] = [to_browsers]
            cases.close()
            clock.restart()
            start_pod()
            log.info("reset: back to time zero")
            state.broadcast("reset", {})
        clock.on_reset = reset
        start_pod()
    if a.feeds:
        dirs["live"].mkdir(parents=True, exist_ok=True)
        idle = {"hot": False, "person": False}
        run_feeds(cfg, dirs["live"], stop, view=(lambda: clock.pod.view if clock.pod else idle) if clock else None)

    server = ThreadingHTTPServer((a.host, a.port or cfg["http"]["port"]), make_handler(state, cmds, meta, dirs, cases, clock))
    server.daemon_threads = True
    port = server.server_address[1]
    log.info("%s: %s -> tracker, geofence, locate -> dashboard, no broker", meta["name"],
             f"replay {a.replay}" if a.replay else "fake pod")
    log.info("dashboard: open http://localhost:%d", port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if run.get("stop"):
            run["stop"].set()
        server.server_close()


if __name__ == "__main__":
    main()
