"""locate.py (tier 1, Software): ag/pose + ag/track -> ag/coverage, the map's two overlays.

    python -m afterglow.locate                       # on the Pi (or the laptop, with --config park.toml)
    python tools/fake_bus.py --only pose,ble         # fake input, with the real tracker running too
    python tools/fake_bus.py --print --only pose,track --duration 600 \
        | python -m afterglow.locate --dry-run       # no broker: fake poses and fake tracks

Two grids of [locate] cell_m squares over [locate] area_m (empty: the map's "park" zone):
- covered: 1 once a drone has passed within detect_m of the cell's centre. Two poses of one drone
  in a row count the straight line between them, so 5 Hz poses and tapped waypoints leave no gaps
  (unless the jump is faster than MAX_SPEED_MPS: a GPS glitch, not a flight);
- p: 0-255, the beacon probability heatmap. Each track adds a Gaussian blob at its x, y with 95%
  inside r95, scaled by its confidence c; where blobs overlap, the higher wins. with_drone and
  baseline tracks are left out, and a track not updated for [locate] stale_s is dropped (the
  tracker dropped it).

new_search clears both grids, as the tracker clears its tracks. ag/coverage goes out about once a
second of bus time, retained. Its clock is the bus's `t`, so fake, sped-up and replayed data work.
"""
import argparse
import json
import logging
import math
import queue
import sys
import time
from collections import Counter

import numpy as np

from . import config as cfgmod
from .bus import Bus, PrintBus
from .contracts import COVERAGE, CTRL, POSE, TRACK, validate
from .parkmap import ParkMap

log = logging.getLogger("locate")

TICK_MS = 1000        # ag/coverage about once a second, in bus time
MIN_WALL_S = 0.5      # live: at most 2 grids a wall second, however fast fake_bus --speed runs
RESYNC_MS = 2000      # bus time this far behind ours means a new time source: follow it back
MAX_CELLS = 20_000    # one grid's cap (about 100 kB of JSON); past it, the cells grow
MAX_SPEED_MPS = 40.0  # two poses further apart than this allows aren't joined (a GPS jump)
SIGMA_95 = 2.448      # a 2-D Gaussian holds 95% within this many sigma
DEFAULT_C = 0.5       # a track with no c


def grid_area(cfg: dict, pm: ParkMap | None = None) -> tuple:
    """(x0, y0, x1, y1) the grids cover: [locate] area_m, else the map's "park" zone."""
    area = cfg["locate"].get("area_m") or (pm or ParkMap.load(cfg)).bounds()
    return tuple(round(float(v), 2) for v in area)  # the map's lon/lat round trip leaves float dust


def n_cells(w, h, cell):
    """(nx, ny) for a w x h metre area, at least 1 x 1; float dust doesn't add a column."""
    return max(1, math.ceil(w / cell - 1e-6)), max(1, math.ceil(h / cell - 1e-6))


def finite(*vals):
    try:
        out = tuple(float(v) for v in vals)
    except (TypeError, ValueError):
        return None
    return out if all(map(math.isfinite, out)) else None


class Locate:
    def __init__(self, cfg: dict, bus, pm: ParkMap | None = None):
        self.bus = bus
        loc = cfg["locate"]
        self.detect_m = float(loc["detect_m"])
        self.stale_ms = loc["stale_s"] * 1000
        x0, y0, x1, y1 = grid_area(cfg, pm)
        w, h = x1 - x0, y1 - y0
        if w <= 0 or h <= 0:
            raise ValueError(f"[locate] area_m is empty: {(x0, y0, x1, y1)}")
        cell = float(loc["cell_m"])
        nx, ny = n_cells(w, h, cell)
        if nx * ny > MAX_CELLS:
            grown = math.ceil(math.sqrt(w * h / MAX_CELLS) * 10) / 10
            while math.prod(n_cells(w, h, grown)) > MAX_CELLS:  # rounding up can still overshoot
                grown = round(grown + 0.1, 1)
            log.warning("%.0f x %.0f m at %.1f m cells is over %d cells: using %.1f m cells",
                        w, h, cell, MAX_CELLS, grown)
            cell = grown
            nx, ny = n_cells(w, h, cell)
        self.x0, self.y0, self.cell, self.nx, self.ny = x0, y0, cell, nx, ny
        self.cx = x0 + (np.arange(self.nx) + 0.5) * cell  # cell centres, west to east
        self.cy = y0 + (np.arange(self.ny) + 0.5) * cell  # south to north (row 0 is the south edge)
        self.covered = np.zeros((self.ny, self.nx), dtype=np.uint8)
        self.tracks = {}    # "T3" -> (t, x, y, r95, c)
        self.last_pose = {}  # drone -> (t, x, y), its last pose
        self.now = 0        # latest bus t
        self.now_mono = time.monotonic()
        self.last_pub = None
        self.stats = Counter()

    # ---------- input ----------

    def feed(self, topic, msg):
        if topic == POSE:
            self.handle_pose(msg)
        elif topic == TRACK:
            self.handle_track(msg)
        elif topic == CTRL:
            self.on_ctrl(topic, msg)

    def advance(self, t, follow_back=True):
        """Only poses may take the clock back: the tracker's ag/track lags them when it's busy."""
        if not isinstance(t, int) or isinstance(t, bool):
            return False
        if t > self.now:
            self.now, self.now_mono = t, time.monotonic()
        elif follow_back and self.now - t > RESYNC_MS:
            self.resync(t)
        return True

    def resync(self, t):
        """Bus time went back, so a new source took over (fake_bus --speed ran ahead, then the real
        pod). Follow it; never join a pose across the switch, and keep the tracks from going stale."""
        log.warning("bus time went back %.0f s: following it", (self.now - t) / 1000)
        self.stats["resync"] += 1
        self.now, self.now_mono, self.last_pub = t, time.monotonic(), None  # publish at once
        self.last_pose.clear()
        self.tracks = {k: (min(v[0], t),) + v[1:] for k, v in self.tracks.items()}

    def clock(self):
        """Bus time, run on by the wall clock since the last message (live mode only)."""
        return self.now + int((time.monotonic() - self.now_mono) * 1000)

    def handle_pose(self, msg):
        xy = finite(msg.get("x"), msg.get("y"))
        if xy is None or not self.advance(msg.get("t")):
            self.stats["bad_pose"] += 1
            return
        t = msg["t"]
        drone = msg.get("drone") or "pod"
        prev = self.last_pose.get(drone)
        start = xy
        if prev is not None:
            dt_s = (t - prev[0]) / 1000
            if dt_s > 0 and math.dist(prev[1:], xy) <= MAX_SPEED_MPS * dt_s:
                start = prev[1:]
            elif math.dist(prev[1:], xy) > 0:
                self.stats["pose_jump"] += 1
        self.mark(start, xy)
        self.last_pose[drone] = (t, *xy)

    def handle_track(self, tr):
        c = tr.get("c")
        vals = finite(tr.get("x"), tr.get("y"), tr.get("r95"), DEFAULT_C if c is None else c)
        if validate(TRACK, tr) or vals is None or not self.advance(tr["t"], follow_back=False):
            self.stats["bad_track"] += 1
            return
        name = tr["track"]
        if tr.get("with_drone") or tr.get("baseline"):  # its x, y is the pod's, or it's one of ours
            self.tracks.pop(name, None)
            return
        x, y, r95, c = vals
        self.tracks[name] = (tr["t"], x, y, max(r95, 0.0), min(max(c, 0.0), 1.0))

    def on_ctrl(self, topic, msg):
        if msg.get("cmd") != "new_search":
            return
        self.covered[:] = 0
        self.tracks.clear()
        self.last_pose.clear()
        self.last_pub = None  # the dashboard sees the empty grid at once
        log.info("new search: grids cleared")

    # ---------- grids ----------

    def span(self, lo, hi, origin, n):
        """The cell indices [i0, i1) whose centres can lie between lo and hi metres, or None."""
        i0 = max(0, math.floor((lo - origin) / self.cell))
        i1 = min(n, math.floor((hi - origin) / self.cell) + 1)
        return (i0, i1) if i0 < i1 else None

    def mark(self, a, b):
        """Cover every cell whose centre is within detect_m of the segment a-b."""
        r = self.detect_m
        cols = self.span(min(a[0], b[0]) - r, max(a[0], b[0]) + r, self.x0, self.nx)
        rows = self.span(min(a[1], b[1]) - r, max(a[1], b[1]) + r, self.y0, self.ny)
        if cols is None or rows is None:
            return
        px = self.cx[cols[0]:cols[1]][None, :]
        py = self.cy[rows[0]:rows[1]][:, None]
        dx, dy = b[0] - a[0], b[1] - a[1]
        seg2 = dx * dx + dy * dy
        f = np.clip(((px - a[0]) * dx + (py - a[1]) * dy) / seg2, 0, 1) if seg2 > 0 else 0.0
        d2 = (px - a[0] - f * dx) ** 2 + (py - a[1] - f * dy) ** 2
        self.covered[rows[0]:rows[1], cols[0]:cols[1]] |= (d2 <= r * r).astype(np.uint8)

    def prob(self):
        """0-255 per cell: the highest blob over it."""
        p = np.zeros((self.ny, self.nx), dtype=np.float64)
        for _, x, y, r95, c in self.tracks.values():
            s = max(r95 / SIGMA_95, self.cell)  # at least a cell, or a tight pin between centres looks faint
            reach = 3 * s
            cols = self.span(x - reach, x + reach, self.x0, self.nx)
            rows = self.span(y - reach, y + reach, self.y0, self.ny)
            if cols is None or rows is None or c <= 0:
                continue
            px = self.cx[cols[0]:cols[1]][None, :]
            py = self.cy[rows[0]:rows[1]][:, None]
            blob = 255 * c * np.exp(-((px - x) ** 2 + (py - y) ** 2) / (2 * s * s))
            win = p[rows[0]:rows[1], cols[0]:cols[1]]
            np.maximum(win, blob, out=win)
        return p.astype(np.uint8)  # truncates: 0-255

    # ---------- output ----------

    def message(self, t):
        n = self.nx * self.ny
        return {"t": t, "x0": self.x0, "y0": self.y0, "cell_m": self.cell, "nx": self.nx, "ny": self.ny,
                "covered": self.covered.ravel().tolist(), "p": self.prob().ravel().tolist(),
                "pct": round(int(self.covered.sum()) / n, 3)}

    def maybe_publish(self, now=None):
        """Publish if a second of bus time has passed since the last grid. Returns True if it did."""
        now = self.now if now is None else now
        if self.now == 0:  # no bus time yet: nothing to stamp it with
            return False
        if self.last_pub is not None and now - self.last_pub < TICK_MS:
            return False
        self.publish(now)
        return True

    def publish(self, now):
        self.last_pub = now
        for name in [k for k, v in self.tracks.items() if now - v[0] > self.stale_ms]:
            del self.tracks[name]
            self.stats["stale_tracks"] += 1
        msg = self.message(now)
        problems = validate(COVERAGE, msg)
        if problems:
            self.stats["invalid"] += 1
            log.warning("contract: %s", problems)
            return
        self.stats[COVERAGE] += 1
        self.bus.publish(COVERAGE, msg, retain=True)

    def summary(self):
        log.info("%d x %d cells of %.1f m, %.1f%% covered, %d tracks, stats: %s", self.nx, self.ny,
                 self.cell, 100 * self.covered.mean(), len(self.tracks), dict(self.stats))


def guarded(loc, fn, *args):
    """Run one step, and log a failure instead of dying: a restart loses the coverage so far."""
    try:
        return fn(*args)
    except Exception:
        loc.stats["errors"] += 1
        log.exception("locate step failed; carrying on")
        return None


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
        loc = Locate(cfg, PrintBus())
        for line in sys.stdin:
            topic, _, payload = line.strip().partition(" ")
            try:
                msg = json.loads(payload)
            except ValueError:
                continue
            if isinstance(msg, dict):
                guarded(loc, loc.feed, topic, msg)
                guarded(loc, loc.maybe_publish)
        if loc.now:
            loc.publish(loc.now)
        loc.summary()
        return

    bus = Bus(a.broker or cfg["mqtt"]["host"], cfg["mqtt"]["port"], "locate")
    loc = Locate(cfg, bus)
    inbox = queue.Queue()  # handlers only queue, so paho's thread stays fast; this loop does the work
    for topic in (POSE, TRACK, CTRL):
        bus.subscribe(topic, lambda topic, msg: inbox.put((topic, msg)))
    last_log = last_pub = time.monotonic()
    try:
        while True:
            try:
                guarded(loc, loc.feed, *inbox.get(timeout=0.2))
            except queue.Empty:
                pass
            if time.monotonic() - last_pub >= MIN_WALL_S and guarded(loc, loc.maybe_publish, loc.clock()):
                last_pub = time.monotonic()
            if time.monotonic() - last_log > 30:
                last_log = time.monotonic()
                loc.summary()
    except KeyboardInterrupt:
        bus.close()


if __name__ == "__main__":
    main()
