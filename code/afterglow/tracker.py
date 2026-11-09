"""tracker.py (tier 1, Software): ag/obs/* + ag/pose -> ag/track (one per beacon) and ag/pass.

    python -m afterglow.tracker                      # on the Pi (or the laptop, with --config park.toml)
    python tools/fake_bus.py --only pose,ble         # fake input, with the fake's own tracker off
    python tools/fake_bus.py --print --only pose,ble --duration 600 | python -m afterglow.tracker --dry-run
    python -m afterglow.tracker --dry-run < runs/1432.log    # a recorded `mosquitto_sub -v` log

For each beacon it:
- tags every sighting with the latest pose (indoors, the last tapped waypoint);
- follows it through key rotation: a new id joins an old track when the old id has just gone
  quiet and the new one has the same class and is heard from about the same place at about the
  same strength;
- splits its sightings into passes (the signal rises, peaks and falls) and publishes each
  finished pass on ag/pass;
- estimates where it is on a grid, with the [track] RSSI model, and its 95% radius r95;
- marks it with_drone if it stays loud at both ends of a stretch the pod moved (our own phones;
  the mark clears if it turns quiet), and baseline if it was heard during the baseline (grey on
  the dashboard; geofence never pins it);
- sets state (heard, localized, pinned) from r95, and a confidence c.

An id needs MIN_PASS_N sightings to become a track, so a one-off passer-by never shows up.

It never sets "confirmed": only the ranger does, on ag/target. Its clock is the bus's `t`, never
the wall clock, so fake, sped-up and replayed data work. If bus time jumps back (fake_bus --speed
ran ahead, then the real pod), the clock follows it.
"""
import argparse
import json
import logging
import math
import queue
import statistics
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field

import numpy as np

from . import config as cfgmod
from .bus import Bus, PrintBus
from .contracts import ALWAYS_REPORTED, CTRL, OBS_BLE, OBS_WIFI, PASS, POSE, TRACK, validate

log = logging.getLogger("tracker")

TICK_MS = 1000       # ag/track about once a second per track, in bus time
EST_MS = 2000        # re-estimate a track's position at most this often
SMOOTH_N = 3         # sightings averaged when looking for a pass's peak
MIN_PASS_N = 3       # sightings a pass needs to count
LINK_N = 5           # an old track's last sightings, compared with a new id's for key rotation
DRONE_N = 10         # recent sightings checked for with_drone
BIN_CAP = 10         # one position bin weighs at most this many sightings: nearby RSSI errors are correlated
MAX_CELLS = 10_000   # cap on one estimate's grid; past it, the cells grow
MAX_BINS = 400       # cap on the position bins one estimate uses (the loudest)
MAX_TRACK = 255      # numbers wrap back to T1
RESYNC_MS = 2000     # bus time this far behind ours means a new time source: follow it back
PENDING_MS = 30 * 60_000  # a new id still short of MIN_PASS_N sightings is forgotten after this quiet
TRACKED = ALWAYS_REPORTED | {"wifi"}  # live devices (apple_other, other) are never tracked


@dataclass
class Sighting:
    t: int
    id: str
    cls: str
    x: float  # where the pod was
    y: float
    z: float
    rssi: float
    base: bool  # heard during the baseline


@dataclass
class Pass:
    rssi: list = field(default_factory=list)
    peak: tuple | None = None  # (smoothed RSSI, pod x, pod y) at the loudest point
    low: float | None = None   # once the signal has fallen drop_db below the peak: the lowest since


@dataclass
class Track:
    name: str
    cls: str
    ids: list
    last_t: int = 0
    bins: dict = field(default_factory=dict)  # pod position bin -> [n, sum rssi, sum x, sum y, sum z]
    recent: deque = field(default_factory=lambda: deque(maxlen=DRONE_N))
    smooth: deque = field(default_factory=lambda: deque(maxlen=SMOOTH_N))
    cur: Pass | None = None
    passes: int = 0  # finished passes that counted
    with_drone: bool = False
    baseline: bool = False
    est: tuple | None = None  # (x, y, r95)
    est_t: int = 0
    dirty: bool = False

    @property
    def n_passes(self):
        return self.passes + (1 if self.cur is not None and len(self.cur.rssi) >= MIN_PASS_N else 0)


class Tracker:
    def __init__(self, cfg: dict, bus):
        self.bus = bus
        tc = cfg["track"]
        self.L, self.P = tc["localized_r95_m"], tc["pinned_r95_m"]
        self.gap_ms = tc["pass_gap_s"] * 1000
        self.rot_ms = tc["rotate_window_s"] * 1000
        self.rot_m = tc["rotate_match_m"]
        self.drone_rssi, self.drone_m = tc["with_drone_rssi"], tc["with_drone_move_m"]
        self.p0, self.p0_sd = tc["rssi_p0"], tc["rssi_p0_sd"]
        self.n_exp, self.sigma = tc["rssi_n"], tc["rssi_sigma"]
        self.drop_db = 1.5 * self.sigma  # a pass has peaked once its smoothed RSSI falls this far
        detect = cfg["locate"]["detect_m"]
        self.bin_m = detect / 4    # sightings are averaged per bin of pod position
        self.margin = 2 * detect   # the grid reaches this far past where the beacon was heard
        self.cell = self.P / 4     # grid cell, grown past MAX_CELLS
        self.pose = None           # (x, y, z)
        self.now = 0               # latest bus t
        self.now_mono = time.monotonic()
        self.last_tick = 0
        self.n = 0                 # last track number given out; keeps counting across new searches
        self.stats = Counter()
        self.reset()

    def reset(self):
        self.tracks = {}   # "T1" -> Track
        self.by_id = {}    # id -> Track
        self.pending = {}  # id -> [Sighting]: a new id waiting to join an old track or start one
        self.baseline_on = False

    # ---------- input ----------

    def feed(self, topic, msg):
        """Hand one bus message to its handler, then tick if a second of bus time has passed."""
        if topic == POSE:
            self.handle_pose(msg)
        elif topic in (OBS_BLE, OBS_WIFI):
            self.handle_obs(topic, msg)
        elif topic == CTRL:
            self.on_ctrl(topic, msg)
        else:
            return
        self.maybe_tick()

    def handle_pose(self, msg):
        self.advance(msg.get("t"))
        try:
            pose = (float(msg["x"]), float(msg["y"]), float(msg.get("z") or 0.0))
        except (KeyError, TypeError, ValueError):
            self.stats["bad_pose"] += 1
            return
        if all(map(math.isfinite, pose)):
            self.pose = pose
        else:
            self.stats["bad_pose"] += 1

    def handle_obs(self, topic, msg):
        self.advance(msg.get("t"))
        if topic == OBS_WIFI:
            if msg.get("rand"):  # randomized addresses change too often to follow
                self.stats["wifi_rand"] += 1
                return
            cls = "wifi"
        else:
            cls = msg.get("cls")
        rssi, t, oid = msg.get("rssi"), msg.get("t"), msg.get("id")
        if not isinstance(cls, str) or cls not in TRACKED or not isinstance(oid, str) or not isinstance(t, int):
            self.stats["ignored"] += 1
            return
        if not isinstance(rssi, (int, float)) or isinstance(rssi, bool) or not math.isfinite(rssi):
            self.stats["no_rssi"] += 1  # a NaN would spoil the track's estimate for good
            return
        if self.pose is None:  # can't place it yet (indoors: tap a waypoint first)
            self.stats["no_pose"] += 1
            return
        s = Sighting(t, oid, cls, *self.pose, float(rssi), self.baseline_on)
        tr = self.by_id.get(oid)
        if tr is not None:
            self.add(tr, s)
            return
        self.pending.setdefault(oid, []).append(s)  # resolve() decides at the next tick

    def on_ctrl(self, topic, msg):
        cmd = msg.get("cmd")
        if cmd == "new_search":  # bridge.py changes its salt, so every id changes too
            self.reset()
            log.info("new search: tracks cleared")
        elif cmd == "baseline_start":
            self.baseline_on = True
            log.info("baseline started: beacons heard from now on show grey")
        elif cmd == "baseline_stop":
            self.baseline_on = False
            log.info("baseline stopped")

    def advance(self, t):
        if not isinstance(t, int) or isinstance(t, bool):
            return
        if t > self.now:
            self.now, self.now_mono = t, time.monotonic()
        elif self.now - t > RESYNC_MS:
            self.resync(t)

    def resync(self, t):
        """Bus time went back, so a new source took over (say fake_bus --speed ran ahead, then the
        real pod). Follow it, and pull every later stamp back to t: otherwise the tracker waits for a
        future that isn't coming, every pass looks over at once and no track ever gets 2 passes."""
        log.warning("bus time went back %.0f s: following it", (self.now - t) / 1000)
        self.stats["resync"] += 1
        self.now, self.now_mono, self.last_tick = t, time.monotonic(), t - TICK_MS  # tick at once
        for tr in self.tracks.values():
            tr.last_t, tr.est_t = min(tr.last_t, t), min(tr.est_t, t)
        for ss in self.pending.values():
            for s in ss:
                s.t = min(s.t, t)

    def clock(self):
        """Bus time, run on by the wall clock since the last message (live mode only)."""
        return self.now + int((time.monotonic() - self.now_mono) * 1000)

    # ---------- tracks ----------

    def resolve(self, now):
        """Give each ready new id a track: an old one it continues (key rotation) or a new one.

        A new id needs MIN_PASS_N sightings, and waits pass_gap_s after its first before it's
        settled: a beacon that only seemed quiet (adverts come about every 1-2 s) is almost surely
        heard again by then, and drops out as a candidate. An id that stays short of MIN_PASS_N is
        forgotten after PENDING_MS without a sighting: one-off passers-by never become tracks.
        Every waiting id competes for the old tracks at once, cheapest match first; only the ready
        ones are settled now. A match is taken only if it's clear: when another id or track comes
        within rssi_sigma of it, the new id gets its own track. A duplicate track is better than
        merging two beacons, which spoils both estimates.

        New tracks are made one at a time, oldest id first, and the rest try again after each, so an
        old id and its successor that come due together still link."""
        for pid in [pid for pid, ss in self.pending.items()
                    if len(ss) < MIN_PASS_N and now - ss[-1].t > PENDING_MS]:
            del self.pending[pid]
            self.stats["one_off"] += 1
        while True:
            ready = {pid for pid, ss in self.pending.items()
                     if len(ss) >= MIN_PASS_N and now - ss[0].t >= self.gap_ms}
            if not ready:
                return
            pairs = sorted((cost, pid, tr.name) for pid, ss in self.pending.items() if len(ss) >= MIN_PASS_N
                           for tr in self.tracks.values()
                           if (cost := self.link_cost(ss, tr)) is not None)
            taken_ids, taken_tracks, linked = set(), set(), set()
            for cost, pid, name in pairs:
                if pid in taken_ids or name in taken_tracks:
                    continue
                taken_ids.add(pid)
                taken_tracks.add(name)
                if pid not in ready:
                    continue
                # pairs within rssi_sigma that share this id or this track (this pair counts once)
                if sum(1 for c, p, n in pairs if (p == pid or n == name) and c < cost + self.sigma) > 1:
                    log.info("%s: unclear whether %s is its new key, so it gets its own track", name, pid)
                    continue
                tr = self.tracks[name]
                log.info("%s: key rotated, %s follows %s", name, pid, tr.ids[-1])
                self.settle(pid, tr)
                linked.add(pid)
            rest = ready - linked
            if not rest:
                return
            oldest = min(rest, key=lambda pid: self.pending[pid][0].t)
            self.settle(oldest, self.new_track(self.pending[oldest][0].cls))

    def link_cost(self, ss, tr):
        """dB between a new id and an old track's last sightings, or None if it can't be a rotation.

        Only by time: if the key rotated while the pod was away (quiet longer than rotate_window_s),
        the new id gets a new track. Matching by place alone merged different beacons in the park sim."""
        first = ss[0]
        if tr.cls != first.cls or not tr.recent or len(ss) < MIN_PASS_N:  # one faint sighting fits anything
            return None
        if tr.last_t >= first.t:  # the old id was still heard after the new one appeared: two beacons
            return None
        if first.t - tr.last_t > self.rot_ms:
            return None
        last = tr.recent[-1]
        if math.hypot(first.x - last.x, first.y - last.y) > self.rot_m:
            return None
        old = list(tr.recent)[-LINK_N:]
        cost = abs(statistics.fmean(map(self.strength(tr), ss)) - statistics.fmean(map(self.strength(tr), old)))
        return cost if cost <= 3 * self.sigma else None  # loose, because rivals are checked

    def strength(self, tr):
        """A sighting's RSSI with the distance from the pod to the track's estimate taken out (the
        beacon's implied p0), so the pod moving toward or away from it between ids doesn't count."""
        if tr.est is None or tr.with_drone:
            return lambda s: s.rssi
        x, y, _ = tr.est
        return lambda s: s.rssi + 5 * self.n_exp * math.log10(max((s.x - x) ** 2 + (s.y - y) ** 2 + s.z ** 2, 1.0))

    def new_track(self, cls):
        if len(self.tracks) >= MAX_TRACK:  # every number is in use: drop the track quiet longest
            old = min(self.tracks.values(), key=lambda tr: tr.last_t)
            log.warning("%d tracks: dropping %s, quiet %d s, for a new one", len(self.tracks), old.name,
                        (self.now - old.last_t) // 1000)
            self.stats["dropped_tracks"] += 1
            del self.tracks[old.name]
            for i in old.ids:
                self.by_id.pop(i, None)
        for _ in range(MAX_TRACK):
            self.n = self.n % MAX_TRACK + 1
            if f"T{self.n}" not in self.tracks:
                break
        name = f"T{self.n}"
        self.tracks[name] = Track(name, cls, [])
        log.info("%s: new %s", name, cls)
        return self.tracks[name]

    def settle(self, pid, tr):
        tr.ids.append(pid)
        self.by_id[pid] = tr
        for s in self.pending.pop(pid):
            self.add(tr, s)

    def add(self, tr, s):
        tr.last_t = max(tr.last_t, s.t)
        tr.baseline = tr.baseline or s.base
        key = (round(s.x / self.bin_m), round(s.y / self.bin_m))
        b = tr.bins.setdefault(key, [0, 0.0, 0.0, 0.0, 0.0])
        b[0] += 1
        b[1] += s.rssi
        b[2] += s.x
        b[3] += s.y
        b[4] += s.z
        tr.recent.append(s)
        tr.dirty = True
        self.check_with_drone(tr)
        self.pass_step(tr, s)

    def check_with_drone(self, tr):
        """A beacon heard all along and loud at both ends of a stretch where the pod moved
        with_drone_move_m rides on the pod (our own phones). A fixed beacon under the path is loud
        only near it, so the ends count, not the median. The mark clears once the median falls below
        with_drone_rssi, so a wrong one can't hide a real find for good. A fixed beacon much louder
        than rssi_p0 can still be marked while the pod is near it: with_drone_rssi needs setting from
        the range test, well above what a beacon at carry height reads."""
        if len(tr.recent) < DRONE_N:
            return
        ss = list(tr.recent)
        if tr.with_drone:
            if statistics.median(s.rssi for s in ss) < self.drone_rssi:
                tr.with_drone, tr.dirty = False, True
                log.info("%s has gone quiet, so it isn't on the pod after all", tr.name)
            return
        if any(b.t - a.t >= self.gap_ms for a, b in zip(ss, ss[1:])):  # heard on separate passes
            return
        k = DRONE_N // 3
        head, tail = ss[:k], ss[-k:]
        moved = math.hypot(statistics.fmean(s.x for s in tail) - statistics.fmean(s.x for s in head),
                           statistics.fmean(s.y for s in tail) - statistics.fmean(s.y for s in head))
        loud = min(statistics.median(s.rssi for s in head), statistics.median(s.rssi for s in tail))
        if moved >= self.drone_m and loud >= self.drone_rssi:
            tr.with_drone = True
            log.info("%s moves with the pod (with_drone)", tr.name)

    # ---------- passes ----------

    def pass_step(self, tr, s):
        """Follow the smoothed RSSI: a pass peaks, falls drop_db, and ends when it rises drop_db again."""
        tr.smooth.append(s.rssi)
        sm = statistics.fmean(tr.smooth)
        if tr.cur is None:
            tr.cur = Pass()
        p = tr.cur
        p.rssi.append(s.rssi)
        if p.low is None:
            if p.peak is None or sm > p.peak[0]:
                p.peak = (sm, s.x, s.y)
            elif p.peak[0] - sm >= self.drop_db:
                p.low = sm
        else:
            p.low = min(p.low, sm)
            if sm - p.low >= self.drop_db:  # rising again: the next fly-by has begun
                self.close_pass(tr, s.t)
                tr.cur = Pass(rssi=[s.rssi], peak=(sm, s.x, s.y))

    def close_pass(self, tr, t):
        p, tr.cur = tr.cur, None
        if p is None or len(p.rssi) < MIN_PASS_N:
            return
        tr.passes += 1
        if tr.with_drone:
            return
        self.publish(PASS, {"t": t, "track": tr.name, "peak_x": round(p.peak[1], 2),
                            "peak_y": round(p.peak[2], 2),
                            "sharp": round(p.peak[0] - statistics.median(p.rssi), 1), "n": len(p.rssi)})

    # ---------- position ----------

    def estimate(self, tr):
        """(x, y, r95): the likeliest grid cell under the RSSI model, and the radius around it that
        holds 95% of the probability. Each cell gets the p0 (transmit strength) that fits it best,
        pulled toward rssi_p0, so the shape of the signal counts more than its loudness."""
        if tr.with_drone:
            last = tr.recent[-1]
            return last.x, last.y, 2 * self.L
        b = np.array(list(tr.bins.values()))
        if len(b) > MAX_BINS:  # heard over a wide area: the loudest bins say the most, and CPU stays bounded
            b = b[np.argsort(b[:, 1] / b[:, 0])[-MAX_BINS:]]
        n = b[:, 0]
        rssi, bx, by, bz = b[:, 1] / n, b[:, 2] / n, b[:, 3] / n, b[:, 4] / n
        x0, x1 = bx.min() - self.margin, bx.max() + self.margin
        y0, y1 = by.min() - self.margin, by.max() + self.margin
        cell = self.cell
        while math.ceil((x1 - x0) / cell) * math.ceil((y1 - y0) / cell) > MAX_CELLS:
            cell *= 1.25
        gx, gy = np.meshgrid(np.arange(x0 + cell / 2, x1, cell), np.arange(y0 + cell / 2, y1, cell))
        cx, cy = gx.ravel(), gy.ravel()
        d2 = (cx[:, None] - bx) ** 2 + (cy[:, None] - by) ** 2 + bz ** 2
        e = rssi + 5 * self.n_exp * np.log10(np.maximum(d2, 1.0))  # each bin's p0 if the beacon were in this cell
        w = np.minimum(n, BIN_CAP) / self.sigma ** 2
        k = 1 / self.p0_sd ** 2
        s1 = e @ w + self.p0 * k
        misfit = (e * e) @ w + self.p0 ** 2 * k - s1 * s1 / (w.sum() + k)  # with each cell's best p0
        p = np.exp(-0.5 * (misfit - misfit.min()))
        p /= p.sum()
        i = int(p.argmax())
        ex, ey = cx[i], cy[i]
        dist = np.hypot(cx - ex, cy - ey)
        order = np.argsort(dist)
        j = min(int(np.searchsorted(np.cumsum(p[order]), 0.95)), len(order) - 1)
        return float(ex), float(ey), float(max(dist[order][j], cell))

    def state(self, r95):
        return "pinned" if r95 <= self.P else "localized" if r95 <= self.L else "heard"

    def confidence(self, r95, passes):
        """0.2 to 0.95: mostly how tight r95 is, then how many passes back it up."""
        tight = min(1.0, max(0.0, (self.L - r95) / (self.L - self.P)))
        return 0.2 + 0.5 * tight + 0.25 * min(passes, 3) / 3

    # ---------- output ----------

    def maybe_tick(self, now=None):
        now = self.now if now is None else now
        if now - self.last_tick >= TICK_MS:
            self.tick(now)

    def tick(self, now):
        self.last_tick = now
        self.resolve(now)
        for tr in list(self.tracks.values()):
            if tr.cur is not None and now - tr.last_t >= self.gap_ms:  # gone quiet: the pass is over
                self.close_pass(tr, now)
                tr.smooth.clear()
            if tr.dirty and (tr.est is None or now - tr.est_t >= EST_MS):
                tr.est, tr.est_t, tr.dirty = self.estimate(tr), now, False
            if tr.est is not None:
                self.publish(TRACK, self.track_msg(tr, now))

    def track_msg(self, tr, now):
        x, y, r95 = tr.est
        passes = tr.n_passes
        st = "heard" if tr.with_drone else self.state(r95)
        c = 0.0 if tr.with_drone else self.confidence(r95, passes)
        return {"t": now, "track": tr.name, "ids": tr.ids[-4:], "cls": tr.cls,
                "x": round(x, 1), "y": round(y, 1), "r95": round(r95, 1), "state": st,
                "passes": passes, "last_s": max(0, (now - tr.last_t) // 1000),
                "with_drone": tr.with_drone, "baseline": tr.baseline, "c": round(c, 2)}

    def publish(self, topic, msg):
        problems = validate(topic, msg)
        if problems:
            self.stats["invalid"] += 1
            log.warning("contract: %s", problems)
            return
        self.stats[topic] += 1
        self.bus.publish(topic, msg)

    def summary(self):
        for tr in self.tracks.values():
            if tr.est is not None:
                m = self.track_msg(tr, self.now)
                log.info("%s %s at (%.1f, %.1f) ±%.1f m, %s, %d passes, c %.2f%s%s, ids %s", m["track"],
                         m["cls"], m["x"], m["y"], m["r95"], m["state"], m["passes"], m["c"],
                         ", with_drone" if m["with_drone"] else "", ", baseline" if m["baseline"] else "",
                         ",".join(tr.ids))
        log.info("stats: %s", dict(self.stats))


def guarded(tr, fn, *args):
    """Run one step, and log a failure instead of dying: a restart loses every track and starts the
    numbers again at T1, which may already be on a pin for another beacon."""
    try:
        fn(*args)
    except Exception:
        tr.stats["errors"] += 1
        log.exception("tracker step failed; carrying on")


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
        tr = Tracker(cfg, PrintBus())
        for line in sys.stdin:
            topic, _, payload = line.strip().partition(" ")
            try:
                msg = json.loads(payload)
            except ValueError:
                continue
            if isinstance(msg, dict):
                guarded(tr, tr.feed, topic, msg)
        tr.tick(tr.now)
        tr.summary()
        return

    bus = Bus(a.broker or cfg["mqtt"]["host"], cfg["mqtt"]["port"], "tracker")
    tr = Tracker(cfg, bus)
    inbox = queue.Queue()  # handlers only queue, so paho's thread stays fast; this loop does the work
    for topic in (POSE, "ag/obs/#", CTRL):
        bus.subscribe(topic, lambda topic, msg: inbox.put((topic, msg)))
    last_log = time.monotonic()
    try:
        while True:
            try:
                guarded(tr, tr.feed, *inbox.get(timeout=0.2))
            except queue.Empty:
                pass
            guarded(tr, tr.maybe_tick, tr.clock())  # keeps last_s counting when nothing is heard
            if time.monotonic() - last_log > 30:
                last_log = time.monotonic()
                log.info("%d tracks, stats: %s", len(tr.tracks), dict(tr.stats))
    except KeyboardInterrupt:
        bus.close()


if __name__ == "__main__":
    main()
