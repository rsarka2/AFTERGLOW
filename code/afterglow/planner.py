"""planner.py (tier 2, Software): pins, pose and mode -> proposed legs on ag/plan.

    python -m afterglow.planner                          # on the Pi (or the laptop, with --config park.toml)
    python -m afterglow.planner --dry-run < run.log      # `mosquitto_sub -v` lines in, plans printed

The drone never decides. The planner only proposes: the ranger accepts or rejects each plan on
the dashboard (accept_leg, reject_leg), and the observer reads each leg's heading and distance to
the pilot, who flies it by hand. Nothing here accepts a plan or changes a pin.

What it proposes:
- homing, when a pin's ag/target status turns "investigating" (so after geofence's guard): one
  crossing pass from the pod straight over the pin to [plan] cross_m past it, then a shrinking
  orbit: ORBIT_POINTS legs round the pin at its r95 (kept between min_orbit_m and cross_m), then
  again at half that;
- grid, when ag/mode turns "search": east-west lanes at most [plan] lane_m apart over the grids'
  area ([locate] area_m, else the park zone), from the corner nearest the pod;
- patrol, when ag/mode turns "patrol" or "night": the top-ranked trail from the pod (trails.py;
  [plan] patrol_m of it, 0 = all), cut into legs that stay within arrive_m of the trail.

A plan's status: proposed -> accepted or rejected, only by the ranger's click (reject_leg also
stops an accepted plan); accepted -> done once the pod reaches its last leg. An open plan is
dropped when a newer proposal replaces it (proposed), the ranger accepts another (accepted), its
pin stops being investigated (homing), or a new_search starts (which then proposes again for the
mode).

Nothing is proposed before the pod's first ag/pose: a mode or a pin that comes first (the retained
ag/mode always does) is planned when the pose arrives. The same mode again proposes again only if
it's a new click (a new `t`, not the retained copy after a reconnect) and no plan of its kind is open.

While a plan is open, legs[0]'s heading and distance are measured from the pod's latest pose.
Once accepted, each leg the pod gets within arrive_m of is taken off the front. Its clock is the
`t` of ag/pose only, so fake, sped-up and replayed data work, and the laptop's clock on ag/ctrl
and ag/mode (the Pi's may be minutes off it) never mixes in.
"""
import argparse
import copy
import json
import logging
import math
import queue
import sys
import time
from collections import Counter

from . import config as cfgmod
from . import trails
from .bus import Bus, PrintBus
from .contracts import CTRL, MODE, MODES, PLAN, POSE, TARGET, now_ms, validate
from .locate import finite, grid_area
from .parkmap import ParkMap, seg_dist

log = logging.getLogger("planner")

MODE_KIND = {"search": "grid", "patrol": "patrol", "night": "patrol"}  # investigate: homing comes from the pin
ORBIT_POINTS = 6     # legs per orbit round the pin
ORBITS = 2           # the first at the pin's r95, each next one half as wide
PUBLISH_MS = 1000    # an open plan's live first leg goes out at most this often (bus time)
REPEAT_S = 5.0       # open plans go out again this often (wall time): ag/plan isn't retained
RESYNC_MS = 60_000   # bus time this far behind ours: a new source (the real pod after fake_bus, a replay)
MIN_LEG_M = 0.1      # shorter legs are left out
OPEN = ("proposed", "accepted")


# ---------- geometry (local metres; x east, y north) ----------

def heading(dx: float, dy: float) -> float:
    """Compass degrees for a move of (dx, dy): 0 north, 90 east."""
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def leg(frm, to, alt: float) -> dict:
    dx, dy = to[0] - frm[0], to[1] - frm[1]
    return {"to_x": round(to[0], 1), "to_y": round(to[1], 1), "heading_deg": round(heading(dx, dy)) % 360,
            "dist_m": round(math.hypot(dx, dy), 1), "alt_m": alt}


def make_legs(frm, pts, alt: float) -> list[dict]:
    """Legs from frm through pts in order, each measured from the end of the one before."""
    out, prev = [], frm
    for p in pts:
        if math.dist(prev, p) < MIN_LEG_M:
            continue
        out.append(leg(prev, p, alt))
        prev = p
    return out


def homing_pts(pod, pin, r95: float, cross_m: float, min_orbit_m: float) -> list[tuple]:
    """A crossing pass over the pin from the pod's side, then orbits that shrink round it."""
    px, py = pin
    d = math.dist(pod, pin) if pod else 0.0
    ux, uy = ((px - pod[0]) / d, (py - pod[1]) / d) if d > MIN_LEG_M else (0.0, 1.0)  # no way in: north
    pts = [(px + ux * cross_m, py + uy * cross_m)]
    a0 = math.atan2(uy, ux)  # the orbits start on the far side, where the crossing pass ends
    r, last = min(max(r95, min_orbit_m), max(cross_m, min_orbit_m)), None
    for _ in range(ORBITS):
        if last is not None and r >= last - MIN_LEG_M:
            break  # already at the tightest orbit
        for k in range(ORBIT_POINTS + 1):
            a = a0 + 2 * math.pi * k / ORBIT_POINTS
            pts.append((px + r * math.cos(a), py + r * math.sin(a)))
        last, r = r, max(r / 2, min_orbit_m)
    return pts


def grid_pts(area, lane_m: float, pod) -> list[tuple]:
    """East-west lanes over area, evenly spaced and at most lane_m apart, from the corner nearest the pod."""
    x0, y0, x1, y1 = area
    n = max(1, math.ceil((y1 - y0) / lane_m - 1e-6))
    ys = [y0 + (i + 0.5) * (y1 - y0) / n for i in range(n)]
    if pod and abs(pod[1] - y1) < abs(pod[1] - y0):
        ys.reverse()  # start at the north edge
    east = not (pod and abs(pod[0] - x1) < abs(pod[0] - x0))  # start at the west edge unless the pod is east
    pts = []
    for y in ys:
        pts += [(x0, y), (x1, y)] if east else [(x1, y), (x0, y)]
        east = not east
    return pts


def simplify(pts, tol: float) -> list[tuple]:
    """Fewer points along the same line: none of the dropped ones is more than tol off it (Douglas-Peucker)."""
    pts = [tuple(p) for p in pts]
    if len(pts) < 3:
        return pts
    a, b = pts[0], pts[-1]
    i, d = max(((k, seg_dist(*pts[k], a, b)) for k in range(1, len(pts) - 1)), key=lambda kd: kd[1])
    if d <= tol:
        return [a, b]
    return simplify(pts[:i + 1], tol)[:-1] + simplify(pts[i:], tol)


def patrol_pts(cfg: dict, pm: ParkMap, start, patrol_m: float, tol: float) -> tuple[str | None, list]:
    """(trail name, points) for the top-ranked trail from start; (None, []) on a map with no trails."""
    ranked = trails.rank(cfg, pm, start, top=1)
    if not ranked:
        return None, []
    name = ranked[0]["name"]
    return name, simplify(trails.route(pm, name, start, max_m=patrol_m or None), tol)


# ---------- the module ----------

class Planner:
    def __init__(self, cfg: dict, bus, pm: ParkMap | None = None):
        self.cfg, self.bus = cfg, bus
        self.pm = pm or ParkMap.load(cfg)
        p = cfg["plan"]
        self.alt_m, self.homing_alt_m = float(p["alt_m"]), float(p["homing_alt_m"])
        self.cross_m, self.min_orbit_m = float(p["cross_m"]), float(p["min_orbit_m"])
        self.lane_m, self.patrol_m, self.arrive_m = float(p["lane_m"]), float(p["patrol_m"]), float(p["arrive_m"])
        self.area = grid_area(cfg, self.pm)
        self.now = 0           # latest ag/pose t
        self.pose = None       # (x, y); nothing is proposed before the first
        self.mode = None       # the last ag/mode; nothing is proposed before the first
        self.mode_t = None     # its t: the same t again is the retained copy, not a click
        self.waiting = None    # the newest proposal asked for before the first pose: ("mode",) or ("homing", pin, xy, r95)
        self.status = {}       # "P1" -> its latest status, to see it turn "investigating"
        self.n = 0             # last plan number given out
        self.plans = {}        # "L1" -> the latest ag/plan
        self.sent = {}         # "L1" -> (bus t, wall time) it last went out
        self.unsent = set()    # plans whose first leg changed since they last went out
        self.stats = Counter()

    # ---------- input ----------

    def feed(self, topic, msg):
        if topic == POSE:
            self.advance(msg.get("t"))  # the pod's clock only: ag/ctrl and ag/mode carry the laptop's
            self.handle_pose(msg)
        elif topic == TARGET:
            self.handle_target(msg)
        elif topic == MODE:
            self.handle_mode(msg)
        elif topic == CTRL:
            self.on_ctrl(topic, msg)

    def advance(self, t):
        if not isinstance(t, int) or isinstance(t, bool):
            return
        if t > self.now:
            self.now = t
        elif self.now - t > RESYNC_MS:
            log.warning("bus time went back %.0f s: following it", (self.now - t) / 1000)
            self.now = t

    def stamp(self) -> int:
        return self.now or now_ms()

    def handle_pose(self, msg):
        xy = finite(msg.get("x"), msg.get("y"))
        if xy is None:
            self.stats["bad_pose"] += 1
            return
        first, self.pose = self.pose is None, xy
        if first and self.waiting:
            what, self.waiting = self.waiting, None
            if what[0] == "homing":
                self.plan_homing(*what[1:])
            else:
                self.plan_mode()
        self.follow()

    def handle_target(self, msg):
        pin, status = msg.get("pin"), msg.get("status")
        if not isinstance(pin, str):
            return
        before = self.status.get(pin)
        if status == "investigating" and before != "investigating":
            where = finite(msg.get("x"), msg.get("y"), msg.get("r95"))
            if where is None:
                self.stats["bad_target"] += 1
                return  # status not kept, so the next good copy still starts homing
            self.status[pin] = status
            self.plan_homing(pin, where[:2], where[2])
            return
        self.status[pin] = status
        if before == "investigating" and status != "investigating":
            if self.waiting and self.waiting[0] == "homing" and self.waiting[1] == pin:
                self.waiting = None
            for p in self.open():
                if p["kind"] == "homing" and p.get("pin") == pin:
                    self.set_status(p, "dropped")

    def handle_mode(self, msg):
        mode, t = msg.get("mode"), msg.get("t")
        if mode not in MODES:
            self.stats["bad_mode"] += 1
            return
        again, same_t = mode == self.mode, t == self.mode_t
        self.mode, self.mode_t = mode, t
        kind = MODE_KIND.get(mode)
        if kind is None:
            return  # investigate: homing comes from the pin turning "investigating"
        if again and (same_t or any(p["kind"] == kind for p in self.open())):
            return  # the retained copy after a reconnect, or a repeat click while its plan is still up
        self.plan_mode()

    def on_ctrl(self, topic, msg):
        cmd = msg.get("cmd")
        if cmd == "new_search":
            self.waiting = None
            for p in self.open():
                self.set_status(p, "dropped")
            if MODE_KIND.get(self.mode):
                self.plan_mode()  # the search starts again: so does the mode's plan
            return
        if cmd not in ("accept_leg", "reject_leg"):
            return
        p = self.plans.get(msg.get("id"))
        allowed = ("proposed",) if cmd == "accept_leg" else OPEN
        if p is None or p["status"] not in allowed:
            log.warning("ignored %s on %r: %s", cmd, msg.get("id"), p["status"] if p else "no such plan")
            self.stats["ignored_cmd"] += 1
            return
        if cmd == "reject_leg":
            self.set_status(p, "rejected")
            return
        for other in self.open("accepted"):  # one plan flown at a time: the ranger picked this one
            self.set_status(other, "dropped")
        self.set_status(p, "accepted")
        if self.pose:
            self.follow()  # the pod may already be at the first leg's end

    # ---------- plans ----------

    def open(self, *statuses):
        statuses = statuses or OPEN
        return [p for p in self.plans.values() if p["status"] in statuses]

    def plan_mode(self):
        """Propose the current mode's plan: a grid for search, a trail for patrol and night."""
        if self.pose is None:
            self.waiting = ("mode",)
            return
        if self.mode == "search":
            self.propose("grid", grid_pts(self.area, self.lane_m, self.pose), self.alt_m)
        elif self.mode in ("patrol", "night"):
            name, pts = patrol_pts(self.cfg, self.pm, self.pose, self.patrol_m, self.arrive_m)
            if name:
                self.propose("patrol", pts, self.alt_m, trail=name)

    def plan_homing(self, pin: str, xy, r95: float):
        if self.pose is None:
            self.waiting = ("homing", pin, xy, r95)
            return
        pts = homing_pts(self.pose, xy, r95, self.cross_m, self.min_orbit_m)
        self.propose("homing", pts, self.homing_alt_m, pin=pin)

    def propose(self, kind: str, pts, alt: float, **extra):
        legs = make_legs(self.pose, pts, alt)  # only called with a pose: legs[0] starts at the pod
        if not legs:
            self.stats["empty_plan"] += 1
            return None
        for p in self.open("proposed"):  # the ranger sees one proposal at a time: the newest
            self.set_status(p, "dropped")
        self.n += 1
        p = {"t": self.stamp(), "id": f"L{self.n}", "kind": kind, "legs": legs, "status": "proposed", **extra}
        self.plans[p["id"]] = p
        log.info("%s: %s, %d legs, %.0f m%s", p["id"], kind, len(legs), sum(lg["dist_m"] for lg in legs),
                 f" ({extra})" if extra else "")
        self.publish(p)
        return p

    def set_status(self, p, status: str):
        p["status"] = status
        p["t"] = self.stamp()
        log.info("%s %s", p["id"], status)
        self.publish(p)

    def follow(self):
        """Measure each open plan's first leg from the pod; take flown legs off an accepted one."""
        for p in self.open():
            legs, flown = p["legs"], False
            if p["status"] == "accepted":
                while legs and math.dist(self.pose, (legs[0]["to_x"], legs[0]["to_y"])) <= self.arrive_m:
                    legs.pop(0)
                    flown = True
                if not legs:
                    self.set_status(p, "done")
                    continue
            first = leg(self.pose, (legs[0]["to_x"], legs[0]["to_y"]), legs[0]["alt_m"])
            if first != legs[0]:
                legs[0] = first
                self.unsent.add(p["id"])
            if flown or (p["id"] in self.unsent and self.now - self.sent.get(p["id"], (0, 0))[0] >= PUBLISH_MS):
                p["t"] = self.stamp()
                self.publish(p)

    def tick(self, wall: float | None = None):
        """Send the open plans again every REPEAT_S, so a dashboard that (re)starts gets them."""
        wall = time.monotonic() if wall is None else wall
        for p in self.open():
            if wall - self.sent.get(p["id"], (0, 0))[1] >= REPEAT_S:
                self.publish(p, wall)

    # ---------- output ----------

    def publish(self, p, wall: float | None = None):
        problems = validate(PLAN, p)
        if problems:
            self.stats["invalid"] += 1
            log.warning("contract: %s", problems)
            return
        self.stats[PLAN] += 1
        self.sent[p["id"]] = (self.now, time.monotonic() if wall is None else wall)
        self.unsent.discard(p["id"])
        self.bus.publish(PLAN, copy.deepcopy(p))  # a copy: legs keep changing here


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--broker", help="MQTT host (default: the config's [mqtt] host)")
    ap.add_argument("--dry-run", action="store_true",
                    help="read `mosquitto_sub -v` lines from stdin, print instead of publishing")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = cfgmod.load(a.config)

    if a.dry_run:
        pl = Planner(cfg, PrintBus())
        for line in sys.stdin:
            topic, _, payload = line.strip().partition(" ")
            try:
                msg = json.loads(payload)
            except ValueError:
                continue
            if isinstance(msg, dict):
                pl.feed(topic, msg)
        log.info("%d plans, stats: %s", len(pl.plans), dict(pl.stats))
        return

    bus = Bus(a.broker or cfg["mqtt"]["host"], cfg["mqtt"]["port"], "planner")
    pl = Planner(cfg, bus)
    inbox = queue.Queue()  # handlers only queue, so paho's thread stays fast; this loop does the work
    for topic in (TARGET, POSE, MODE, CTRL):
        bus.subscribe(topic, lambda topic, msg: inbox.put((topic, msg)))
    last_log = time.monotonic()
    try:
        while True:
            try:
                pl.feed(*inbox.get(timeout=1.0))
            except queue.Empty:
                pass
            except Exception:  # one bad message mustn't stop the planner
                log.exception("failed on a message")
            pl.tick()
            if time.monotonic() - last_log > 30:
                last_log = time.monotonic()
                log.info("%d plans (%d open), mode %s, stats: %s", len(pl.plans), len(pl.open()), pl.mode, dict(pl.stats))
    except KeyboardInterrupt:
        bus.close()


if __name__ == "__main__":
    main()
