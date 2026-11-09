"""dashboard.py (tier 1, Software): the ranger's dashboard, one web server on [http] port.

    python -m afterglow.dashboard                       # demo profile, broker from the config
    python -m afterglow.dashboard --config park.toml    # park profile (realistic view)
    python -m afterglow.dashboard --broker 10.0.0.5     # on the laptop, against the pod's broker
                                                        # (the live pictures come from the pod too)
    python -m afterglow.dashboard --dry-run             # no broker: serves the page, prints commands

Live pictures: camera.py and thermal.py write live/ on the Pi, so the Pi runs its own dashboard
(afterglow@dashboard) to serve them. A laptop dashboard with --pod (default: the --broker host,
when that isn't this machine) fetches /live/ and /snaps/ from it, and moves Last-Modified onto
the laptop's clock, so a Pi clock that's off doesn't read as a frozen feed.

Then open http://<host>:8080. The server subscribes to ag/#, keeps the latest state and
streams it to every browser with Server-Sent Events (/api/events): the full state first,
on every (re)connect, then each message as it arrives. Buttons POST to /api/ctrl and
/api/mode; the server stamps t, runs validate() and publishes.

It never acts on its own: every ag/ctrl and ag/mode message it sends comes from a click.
ag/mode goes out retained, so a restarted geofence or a second dashboard knows the mode.
There's no login: anyone who can reach the port can click. Run it on the team's travel
router, never the venue Wi-Fi.

HTTP API (docs/CONTRACTS.md, "Dashboard HTTP"):
    GET  /api/map      profile, settings and the map in metres (once per page load)
    GET  /api/state    the full state as JSON (debugging)
    GET  /api/events   SSE: "state" (full), "msg" ({topic, msg}), "status" (every 2 s)
    POST /api/ctrl     {"cmd": "ack", "pin": "P1"} etc.; t is added here
    POST /api/mode     {"mode": "night"}
    GET  /api/case     the open missing-person case, its start point, ranked trails and route
    POST /api/case     open or update the case: {"name", "age", "details", "carrying", "car_park", "trail"}
    POST /api/case/close
    GET/POST /api/sim  simulation only (tools/local_stack.py): {"paused": bool, "speed": 1|2|4|10}, {"reset": true},
                       {"flight": "out" | "home"} (Send out / Return to base); GET adds "flight", "base", "plan"
    GET  /live/..., /snaps/...   image files from [http] live_dir and snaps_dir

One dashboard: / is the operator TV (web/tv.html, also at /tv.html). nearby.html is its Nearby alerts pane.
The case (case.py) is laptop-only personal data: it's saved under runs/ and streamed to the
browsers as an SSE "case" event, never published on the bus.
"""
import argparse
import json
import logging
import math
import mimetypes
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import config as cfgmod
from . import geo
from .bus import Bus, PrintBus
from .case import CaseStore
from .contracts import (ALL, CTRL, MODE, OBS_BLE, OBS_WIFI, PLAN, POSE, RETAINED, TARGET, TRACK,
                        now_ms, validate)
from .parkmap import ParkMap

log = logging.getLogger("dashboard")

WEB = Path(__file__).resolve().parent / "web"
PIN_CMDS = {"ack", "investigate", "clear", "confirm"}
LEG_CMDS = {"accept_leg", "reject_leg"}
MAX_BODY = 4096
OBS_IDS = 300  # raw-observation summaries kept (oldest dropped)
OBS_LOG = 60  # recent raw observations kept
TRAIL_MAX = 4000  # breadcrumb points per drone
QUEUE_MAX = 5000  # messages buffered per browser before it's dropped (it reconnects)
STATUS_S = 2.0
POD_TIMEOUT_S = 2.0  # a live picture from the pod's dashboard; the TV asks again in a second
LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


class State:
    """Everything the browser needs, kept by topic. store.js's reducer mirrors apply()."""

    def __init__(self, trail_step_m: float):
        self.lock = threading.Lock()
        self.clients: set[queue.Queue] = set()
        self.trail_step = trail_step_m
        self.clear()

    def clear(self):
        """Forget everything heard (the sim's reset). Browsers stay connected."""
        self.poses = {}  # drone -> latest ag/pose ("pod" until there's a drone field)
        self.trails = {}  # drone -> [[x, y], ...]
        self.pins = {}  # pin -> latest ag/target
        self.tracks = {}  # track -> latest ag/track
        self.plans = {}  # plan id -> latest ag/plan
        self.obs = {}  # hashed id -> summary of ag/obs/ble and ag/obs/wifi
        self.obs_log = []  # newest first: {"topic", "msg"}
        self.latest = {}  # every other topic -> latest message
        self.counts = {}  # topic -> messages seen
        # The dashboard's clock: the latest pose's t (fake and replayed time isn't wall time).
        # Before any pose, the newest t seen. Not the newest t overall: a retained message from
        # an earlier run (say a fake bus at --speed 40) can sit hours in the future.
        self.bus_t = 0
        self.pose_seen = False
        self.broker = False

    # Runs on paho's network thread: keep it fast.
    def on_msg(self, topic: str, msg: dict):
        data = json.dumps({"topic": topic, "msg": msg}, separators=(",", ":"))
        with self.lock:
            self.apply(topic, msg)
            for q in list(self.clients):
                try:
                    q.put_nowait(("msg", data))
                except queue.Full:  # a stuck browser: drop it; EventSource reconnects and gets full state
                    self.clients.discard(q)
                    q.queue.clear()
                    q.put_nowait((None, None))

    def apply(self, topic: str, msg: dict):
        self.counts[topic] = self.counts.get(topic, 0) + 1
        t = msg.get("t")
        if isinstance(t, (int, float)):
            if topic == POSE:
                self.bus_t, self.pose_seen = int(t), True
            elif not self.pose_seen and t > self.bus_t:
                self.bus_t = int(t)
        if topic == POSE:
            d = str(msg.get("drone", "pod"))
            self.poses[d] = msg
            trail = self.trails.setdefault(d, [])
            x, y = msg.get("x"), msg.get("y")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                if not trail or math.hypot(x - trail[-1][0], y - trail[-1][1]) >= self.trail_step:
                    trail.append([x, y])
                    del trail[:-TRAIL_MAX]
        elif topic in (OBS_BLE, OBS_WIFI):
            oid = msg.get("id")
            o = self.obs.get(oid)
            if o is None:
                if len(self.obs) >= OBS_IDS:  # make room first: drop the id silent the longest
                    del self.obs[min(self.obs, key=lambda k: self.obs[k].get("t") or 0)]
                o = self.obs[oid] = {"id": oid, "n": 0, "first": msg.get("t"), "best": -999}
            rssi = msg.get("rssi")
            o.update(kind="wifi" if topic == OBS_WIFI else "ble", cls=msg.get("cls", "wifi"),
                     rssi=rssi, t=msg.get("t"), src=msg.get("src"), n=o["n"] + 1)
            if isinstance(rssi, (int, float)) and rssi > o["best"]:
                o["best"] = rssi
            self.obs_log.insert(0, {"topic": topic, "msg": msg})
            del self.obs_log[OBS_LOG:]
        elif topic == TARGET and "pin" in msg:
            self.pins[msg["pin"]] = msg
        elif topic == TRACK and "track" in msg:
            self.tracks[msg["track"]] = msg
        elif topic == PLAN and "id" in msg:
            self.plans[msg["id"]] = msg
        else:
            self.latest[topic] = msg

    def broadcast(self, ev: str, obj):
        """Send one extra SSE event (the case) to every browser."""
        data = json.dumps(obj, separators=(",", ":"))
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait((ev, data))
                except queue.Full:
                    pass

    def snapshot(self) -> dict:
        return {"poses": self.poses, "trails": self.trails, "pins": self.pins, "tracks": self.tracks,
                "plans": self.plans, "obs": self.obs, "obs_log": self.obs_log, "latest": self.latest,
                "counts": self.counts, "bus_t": self.bus_t, "pose_seen": self.pose_seen, "broker": self.broker}

    def status(self) -> dict:
        with self.lock:
            return {"broker": self.broker, "bus_t": self.bus_t, "clients": len(self.clients),
                    "counts": dict(self.counts), "wall": now_ms()}

    def subscribe(self) -> queue.Queue:
        """A queue that starts with the full state; no message can fall between the two."""
        q = queue.Queue(maxsize=QUEUE_MAX)
        with self.lock:
            q.put_nowait(("state", json.dumps(self.snapshot(), separators=(",", ":"))))
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)


class Commands:
    """Turns a click into an ag/ctrl or ag/mode message: stamps t, validates, publishes."""

    def __init__(self, bus_ref, waypoints):
        self.bus_ref = bus_ref  # callable returning the Bus, or None while it's connecting
        self.waypoints = set(waypoints)

    def ctrl(self, body: dict) -> tuple[int, dict]:
        allowed = {"cmd", "pin", "id", "n"}
        msg = {k: v for k, v in body.items() if k in allowed}
        msg["t"] = now_ms()
        problems = validate(CTRL, msg)
        cmd = msg.get("cmd")
        if cmd in PIN_CMDS and not isinstance(msg.get("pin"), str):
            problems.append(f"{cmd} needs a pin")
        if cmd in LEG_CMDS and not isinstance(msg.get("id"), str):
            problems.append(f"{cmd} needs a leg id")
        if cmd == "waypoint" and (not isinstance(msg.get("n"), int) or msg["n"] not in self.waypoints):
            problems.append(f"waypoint needs n, one of {sorted(self.waypoints)}")
        return self._publish(CTRL, msg, problems)

    def mode(self, body: dict) -> tuple[int, dict]:
        msg = {"t": now_ms(), "mode": body.get("mode")}
        return self._publish(MODE, msg, validate(MODE, msg))

    def _publish(self, topic, msg, problems):
        if problems:
            return 400, {"ok": False, "problems": problems}
        bus = self.bus_ref()
        if bus is None:
            return 503, {"ok": False, "problems": ["not connected to the MQTT broker"]}
        bus.publish(topic, msg, retain=topic in RETAINED)
        log.info("operator: %s %s", topic, json.dumps(msg, separators=(",", ":")))
        return 200, {"ok": True, "msg": msg}


def make_meta(cfg: dict, pm: ParkMap) -> dict:
    m, http = cfg["mission"], cfg["http"]
    return {
        "profile": Path(cfg["_path"]).stem,
        "name": m.get("name", Path(cfg["_path"]).stem),
        "origin": [m["origin_lat"], m["origin_lon"]],
        "waypoints": {str(k): list(v) for k, v in sorted(cfg["waypoints"].items())},
        "radii": {"localized": cfg["track"]["localized_r95_m"], "pinned": cfg["track"]["pinned_r95_m"]},
        "offtrail_m": cfg["rules"]["offtrail_m"],
        "detect_m": cfg["locate"]["detect_m"],
        "cell_m": cfg["locate"]["cell_m"],
        "trail_step_m": trail_step(cfg),
        "live": {"thermal": "/live/thermal.png", "camera": "/live/camera.jpg"},
        "dirs": {"live": http["live_dir"], "snaps": http["snaps_dir"]},
        "map": pm.to_json(),
        "building": building(cfg),
        "thermal": {"hot_min_c": cfg["thermal"]["hot_min_c"], "hot_max_c": cfg["thermal"]["hot_max_c"]},
        # the tracker's RSSI model, so nearby.html turns RSSI into metres the same way
        "rssi": {"p0": cfg["track"]["rssi_p0"], "n": cfg["track"]["rssi_n"]},
        # the TV's location tabs, and how often health.py reports (its panel goes grey when ag/sys stops)
        "sites": [dict(s) for s in http.get("sites", [])],
        "health": {"period_s": cfg["health"]["period_s"], "stale_s": cfg["health"]["stale_s"]},
        # the indoor walk ([pose] source = "imu"): the TV draws a floor plan with this line instead of the satellite map
        "walk": ({"from": list(cfg["imu"]["line_from"]), "to": list(cfg["imu"]["line_to"]), "hall_w_m": cfg["imu"]["hall_w_m"]}
                 if cfg["pose"].get("source") == "imu" else None),
        "search": {"car_parks": cfg["search"]["car_parks"], "entrance_xy": cfg["search"]["entrance_xy"],
                   "corridor_m": cfg["search"]["corridor_m"], "checkin_s": cfg["search"]["checkin_s"],
                   # the box the sweep and the grids cover, for the map to zoom to
                   "area": [round(v, 1) for v in (cfg["locate"]["area_m"] or pm.bounds())]},
    }


def building(cfg: dict) -> dict | None:
    """[rules] building_geojson in metres for the TV's Live tab: {"name", "approximate", "ring"}, or None."""
    rel = cfg["rules"].get("building_geojson")
    if not rel:
        return None
    try:
        f = json.loads(cfgmod.resolve(cfg, rel).read_text())["features"][0]
    except (OSError, ValueError, KeyError, IndexError) as e:
        log.warning("no building outline from %s: %s", rel, e)
        return None
    lat0, lon0 = cfg["mission"]["origin_lat"], cfg["mission"]["origin_lon"]
    ring = [[round(v, 2) for v in geo.to_xy(lat, lon, lat0, lon0)] for lon, lat in f["geometry"]["coordinates"][0]]
    props = f.get("properties", {})
    return {"name": props.get("name", ""), "approximate": bool(props.get("approximate")), "ring": ring}


def trail_step(cfg) -> float:
    return max(0.2, cfg["locate"]["cell_m"] / 2)


def safe_file(base: Path, rel: str) -> Path | None:
    """base/rel if it's a file inside base (no ../ escapes), else None."""
    try:
        p = (base / rel.lstrip("/")).resolve()
    except (OSError, ValueError):
        return None
    return p if p.is_relative_to(base.resolve()) and p.is_file() else None


class PodPictures:
    """The pod's live/ and snaps/ pictures, fetched from the dashboard running on the Pi."""

    def __init__(self, base_url: str, timeout: float = POD_TIMEOUT_S):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.ok = None  # last fetch reached the pod (None: not tried yet); logged only when it changes

    def _seen(self, ok: bool, why=""):
        if ok != self.ok:
            if ok:
                log.info("pictures from the pod at %s", self.base)
            else:
                log.warning("can't reach the pod's dashboard at %s for pictures (%s); is afterglow@dashboard running?",
                            self.base, why)
            self.ok = ok

    def fetch(self, path: str):
        """GET path ("/live/camera.jpg") from the pod -> (body, content type, mtime on our clock),
        None if the pod has no such picture, or raises OSError if the pod can't be reached."""
        try:
            with urllib.request.urlopen(self.base + path, timeout=self.timeout) as r:
                body, h = r.read(), r.headers
        except urllib.error.HTTPError as e:
            self._seen(True)
            if e.code == 404:
                return None
            raise OSError(f"HTTP {e.code}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            self._seen(False, getattr(e, "reason", e))
            raise OSError(str(e)) from e
        self._seen(True)
        # Last-Modified is on the Pi's clock: keep the picture's age (the Pi's Date minus it), not the time.
        try:
            age = max(0.0, (parsedate_to_datetime(h["Date"]) - parsedate_to_datetime(h["Last-Modified"])).total_seconds())
        except (TypeError, ValueError):  # a header missing or unreadable: call it fresh
            age = 0.0
        return body, h.get("Content-Type") or "application/octet-stream", time.time() - age


def pod_url(arg: str | None, broker: str | None, http_port: int) -> str | None:
    """--pod HOST[:PORT] (or "off"); without it, the --broker host when that isn't this machine."""
    if arg and arg.lower() == "off":
        return None
    host = arg or (broker.partition(":")[0] if broker else "")
    if not arg and host in LOCAL_HOSTS:
        return None
    if not host:
        return None
    if host.startswith(("http://", "https://")):
        return host
    return f"http://{host}" if ":" in host else f"http://{host}:{http_port}"


def make_handler(state: State, cmds: Commands, meta: dict, dirs: dict, cases: CaseStore | None = None, sim=None,
                 pod: PodPictures | None = None):
    """sim: tools/local_stack.py's SimClock (pause and speed for the fake pod), or None on the real system.
    pod: fetch /live/ and /snaps/ from the pod's dashboard instead of the local folders."""
    meta_json = json.dumps(meta, separators=(",", ":")).encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "afterglow-dashboard"

        def log_message(self, fmt, *args):  # quiet: one line per request is too much at 5 Hz
            log.debug("%s %s", self.address_string(), fmt % args)

        def send_json(self, code: int, obj):
            body = obj if isinstance(obj, bytes) else json.dumps(obj, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, path: Path | None, cache: bool):
            if path is None:
                self.send_error(404)
                return
            try:
                body, mtime = path.read_bytes(), path.stat().st_mtime
            except OSError:  # a live frame renamed away between the check and the read
                self.send_error(404)
                return
            self.send_bytes(body, mimetypes.guess_type(path.name)[0] or "application/octet-stream", mtime, cache)

        def send_pod(self, path: str):
            try:
                got = pod.fetch(path)
            except OSError:
                return self.send_error(502, "pod unreachable")
            if got is None:
                return self.send_error(404)
            self.send_bytes(*got, cache=False)

        def send_bytes(self, body: bytes, ctype: str, mtime: float, cache: bool):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            # with the Date header, the TV works out how old a live picture is (a frozen feed shows)
            self.send_header("Last-Modified", formatdate(mtime, usegmt=True))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/api/events":
                return self.events()
            if path == "/api/map":
                return self.send_json(200, meta_json)
            if path == "/api/case":
                return self.send_json(200, cases.view() if cases else {"case": None})
            if path == "/api/sim":
                return self.send_json(200, sim.view()) if sim else self.send_error(404)
            if path == "/api/state":
                with state.lock:
                    snap = json.dumps(state.snapshot(), separators=(",", ":")).encode()
                return self.send_json(200, snap)
            for prefix in ("live", "snaps"):
                if path.startswith(f"/{prefix}/"):
                    if pod:
                        return self.send_pod(path)
                    return self.send_file(safe_file(dirs[prefix], path[len(prefix) + 2:]), cache=False)
            if path == "/":
                path = "/tv.html"  # the one dashboard
            return self.send_file(safe_file(WEB, path), cache=path.startswith("/vendor/"))

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in ("/api/ctrl", "/api/mode", "/api/case", "/api/case/close", "/api/sim"):
                return self.send_error(404)
            # JSON only: a browser must ask (CORS preflight) before sending it cross-origin, and this
            # server never says yes, so another web page can't click Confirm for the ranger.
            if self.headers.get_content_type() != "application/json":
                return self.send_json(415, {"ok": False, "problems": ["Content-Type must be application/json"]})
            try:
                n = int(self.headers.get("Content-Length", 0))
                if not 0 <= n <= MAX_BODY:
                    raise ValueError("bad body length")
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
            except ValueError as e:
                return self.send_json(400, {"ok": False, "problems": [str(e)]})
            if path.startswith("/api/case"):
                return self.case(path, body)
            if path == "/api/sim":
                if sim is None:
                    return self.send_json(404, {"ok": False, "problems": ["not a simulation"]})
                problems = sim.set(body)
                return self.send_json(400 if problems else 200, {"ok": not problems, "problems": problems, **sim.view()})
            code, out = cmds.ctrl(body) if path == "/api/ctrl" else cmds.mode(body)
            self.send_json(code, out)

        def case(self, path, body):
            if cases is None:
                return self.send_json(503, {"ok": False, "problems": ["no case store on this server"]})
            if path == "/api/case/close":
                return self.send_json(200, {"ok": True, **cases.close()})
            problems, view = cases.open(body)
            if problems:
                return self.send_json(400, {"ok": False, "problems": problems})
            self.send_json(200, {"ok": True, **view})

        def events(self):
            q = state.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.wfile.write(b"retry: 1500\n\n")
                if cases:
                    self.wfile.write(f"event: case\ndata: {json.dumps(cases.view(), separators=(',', ':'))}\n\n".encode())
                if sim:
                    self.wfile.write(f"event: sim\ndata: {json.dumps(sim.view())}\n\n".encode())
                next_status = 0.0
                while True:
                    try:
                        ev, data = q.get(timeout=STATUS_S)
                    except queue.Empty:
                        ev, data = "status", None
                    if ev is None:  # dropped for falling behind
                        break
                    if ev == "status" or time.monotonic() >= next_status:
                        self.wfile.write(f"event: status\ndata: {json.dumps(state.status())}\n\n".encode())
                        next_status = time.monotonic() + STATUS_S
                    if data is not None:
                        self.wfile.write(f"event: {ev}\ndata: {data}\n\n".encode())
                    self.wfile.flush()
            except OSError:  # the browser went away (closed tab, laptop off Wi-Fi)
                pass
            finally:
                state.unsubscribe(q)

    return Handler


def connect(host, port, state: State, holder: dict, stop: threading.Event):
    """Connect to the broker, retrying every 2 s, so the page is up even when the broker isn't."""
    name = f"dashboard-{socket.gethostname()}-{os.getpid()}"  # unique: the Pi's and the laptop's can share a broker
    while not stop.is_set():
        try:
            bus = Bus(host, port, name)
            bus.subscribe(ALL, state.on_msg)
            holder["bus"] = bus
            log.info("connected to MQTT at %s:%d", host, port)
            break
        except OSError as e:
            log.warning("MQTT %s:%d: %s; retrying in 2 s", host, port, e)
            stop.wait(2)
    while not stop.is_set():  # keep the status light honest
        bus = holder.get("bus")
        state.broker = bool(bus and bus.c.is_connected())
        stop.wait(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--broker", help="MQTT host[:port], overrides [mqtt] (run the dashboard on the laptop)")
    ap.add_argument("--host", default="0.0.0.0", help="address to serve on (default: every interface)")
    ap.add_argument("--port", type=int, help="HTTP port (default: [http] port)")
    ap.add_argument("--pod", help="HOST[:PORT] of the pod's dashboard to fetch live/ and snaps/ pictures from "
                                  "(default: the --broker host, unless local; 'off' = this machine's folders)")
    ap.add_argument("--dry-run", action="store_true", help="no broker: serve the page, print commands")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    cfg = cfgmod.load(a.config)
    pm = ParkMap.load(cfg)
    meta = make_meta(cfg, pm)
    state = State(meta["trail_step_m"])
    holder, stop = {}, threading.Event()
    if a.dry_run:
        holder["bus"] = PrintBus()
    else:
        host, port = cfg["mqtt"]["host"], cfg["mqtt"]["port"]
        if a.broker:
            host, _, p = a.broker.partition(":")
            port = int(p) if p else port
        threading.Thread(target=connect, args=(host, port, state, holder, stop), daemon=True).start()
    cmds = Commands(lambda: holder.get("bus"), cfg["waypoints"])
    dirs = {k: cfgmod.resolve(cfg, cfg["http"][f"{k}_dir"]) for k in ("live", "snaps")}
    cases = CaseStore(cfg, pm)
    cases.listeners.append(lambda view: state.broadcast("case", view))

    url = pod_url(a.pod, a.broker, cfg["http"]["port"])
    pod = PodPictures(url) if url else None

    server = ThreadingHTTPServer((a.host, a.port or cfg["http"]["port"]),
                                 make_handler(state, cmds, meta, dirs, cases, pod=pod))
    server.daemon_threads = True
    log.info("dashboard (%s): open http://%s:%d", meta["name"],
             "localhost" if a.host == "0.0.0.0" else a.host, server.server_address[1])
    log.info("live pictures from %s", f"the pod's dashboard at {url}" if pod else dirs["live"])
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        if holder.get("bus"):
            holder["bus"].close()


if __name__ == "__main__":
    main()
