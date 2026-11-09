"""geofence.py (tier 1, Software): ag/track + the park map + ag/mode -> ag/target, one record per pin.

    python -m afterglow.geofence                     # on the Pi (or the laptop, with --config park.toml)
    python -m afterglow.geofence --fresh             # forget the saved pins and start again at P1
    python tools/fake_bus.py --print --only pose,ble --duration 600 | python -m afterglow.tracker --dry-run \
        | python -m afterglow.geofence --dry-run    # no broker: the fake pod through the real tracker

It proposes pins and guards the ranger's clicks; it never confirms anything itself:
- picks pins by the rule for the mode (ag/mode; patrol until the first click). Investigate has no
  rule of its own and keeps the last other mode's rule. A pin, once made, stays, so the rules are
  careful: every rule must hold for [rules] pin_hold_s, and the ones about where a beacon is (day,
  night) also need the track at least localized, with its 95% circle clear of every campground (a
  permitted place, by day or night);
- fills each pin from its track (x, y, r95, c, state, last_s) and the map (off_trail_m, zone),
  and sets prio. Only the ranger's Confirm sets state "confirmed";
- changes a pin's status only by the allowed steps (docs/CONTRACTS.md, "Pin states and priority")
  and ignores anything else. A cleared pin freezes and its track never gets another. A pin also
  keeps its place while its track is with_drone (the tracker then reports the pod's position);
- attaches ag/sos answers to the pin under investigation, or opens an SOS-only T0 pin at the pod;
- sets flags.thermal on the pin under investigation when ag/thermal is hot with the pod within
  [rules] thermal_near_m of the pin's 95% circle. It stays set: a sign for the ranger, not a confirm.

Pins outlive a restart: they're saved to [rules] pins_file (pins and links only, no addresses), so a
restarted geofence doesn't hand out P1 again. Its clock is the bus's `t`, so fake, sped-up and
replayed data work.
"""
import argparse
import json
import logging
import math
import os
import queue
import sys
import time
from collections import Counter

from . import config as cfgmod
from . import geo
from .bus import Bus, PrintBus
from .contracts import ALWAYS_REPORTED, CTRL, MODE, MODES, POSE, SOS, TARGET, THERMAL, TRACK, validate
from .parkmap import ParkMap

log = logging.getLogger("geofence")

LOST = {"findmy", "findhub", "samsung_off"}  # the search rule's classes (not our ref beacon)
PLACED = {"localized", "pinned"}  # day and night rules need to know where the beacon is
# cmd -> (statuses it may come from, status it leads to)
STEPS = {
    "ack": ({"new"}, "acknowledged"),
    "investigate": ({"new", "acknowledged"}, "investigating"),
    "clear": ({"new", "acknowledged", "investigating"}, "cleared"),
    "confirm": ({"investigating"}, "confirmed"),
}
NO_FLAGS = {"sos": False, "thermal": False, "radar": False, "camera": False}
RESYNC_MS = 60_000  # bus time this far behind ours: a new source (the real pod after fake_bus, a replay)


def prio(p: dict) -> int:
    """1 red, 2 orange, 3 yellow, 4 grey (docs/CONTRACTS.md, "Pin states and priority")."""
    if p.get("status") == "confirmed" or (p.get("sos") or {}).get("hurt"):
        return 1
    if p.get("rule") == "sos":
        return 2
    c = p.get("c") or 0
    if p.get("state") == "pinned" and c >= 0.8:
        return 2
    if p.get("state") in PLACED or c >= 0.5:  # grey is for heard only
        return 3
    return 4


class Geofence:
    def __init__(self, cfg: dict, bus, pm: ParkMap | None = None, pins_file=None):
        self.bus = bus
        self.pm = pm or ParkMap.load(cfg)
        r = cfg["rules"]
        self.offtrail_m, self.min_passes, self.sos_r95 = r["offtrail_m"], r["min_passes"], r["sos_r95_m"]
        self.thermal_near_m = float(r["thermal_near_m"])
        self.max_age_s = r["pins_max_age_min"] * 60
        self.hold_ms = r["pin_hold_s"] * 1000
        self.origin = cfg["mission"]["origin_lat"], cfg["mission"]["origin_lon"]
        self.pins_file = pins_file  # None: nothing saved (tests, --dry-run)
        self.mode = "patrol"       # with no ag/mode yet
        self.rule_mode = "patrol"  # the last mode other than investigate
        self.now = 0               # latest bus t
        self.pose = None           # (x, y)
        self.n = 0                 # last pin number given out
        self.pins = {}             # "P1" -> the latest ag/target record
        self.links = {}            # "T3" -> "P1": which pin each track feeds
        self.ids = {}              # "T3" -> the track's last ids: a tracker restart reuses T1 for another beacon
        self.inv_t = {}            # "P1" -> when it was set to investigating: SOS goes on the latest
        self.sos_pin = None        # the SOS-only pin taking further answers
        self.since = {}            # "T3" -> since when its rule has held (bus t), for pin_hold_s
        self.stats = Counter()

    # ---------- input ----------

    def feed(self, topic, msg):
        if topic == TRACK:
            self.handle_track(msg)
        elif topic == POSE:
            self.handle_pose(msg)
        elif topic == MODE:
            self.handle_mode(msg)
        elif topic == CTRL:
            self.on_ctrl(topic, msg)
        elif topic == SOS:
            self.handle_sos(msg)
        elif topic == THERMAL:
            self.handle_thermal(msg)

    def advance(self, t):
        if not isinstance(t, int) or isinstance(t, bool):
            return
        if t > self.now:
            self.now = t
        elif self.now - t > RESYNC_MS:  # else every click would stamp pins with a time from the old source
            log.warning("bus time went back %.0f s: following it", (self.now - t) / 1000)
            self.now = t

    def touch(self, p):
        """Stamp a pin with the bus time but keep when it was last heard (t - last_s), which the
        dashboard and lora_out work out: a click on a pin whose track went quiet isn't a sighting."""
        dt = self.now - p.get("t", self.now)
        if dt > 0:
            p["t"] = self.now
            p["last_s"] = p.get("last_s", 0) + round(dt / 1000)

    def handle_pose(self, msg):
        self.advance(msg.get("t"))
        try:
            self.pose = (float(msg["x"]), float(msg["y"]))
        except (KeyError, TypeError, ValueError):
            self.stats["bad_pose"] += 1

    def handle_mode(self, msg):
        mode = msg.get("mode")
        if mode not in MODES:
            self.stats["bad_mode"] += 1
            return
        if mode != self.mode:
            log.info("mode %s", mode)
            self.since.clear()  # another rule now: start the hold again
        self.mode = mode
        if mode != "investigate" and mode != self.rule_mode:
            self.rule_mode = mode
            self.save()

    def handle_track(self, tr):
        if validate(TRACK, tr) or not isinstance(tr["t"], int):
            self.stats["bad_track"] += 1
            return
        try:
            float(tr["x"]), float(tr["y"]), float(tr["r95"])
        except (TypeError, ValueError):
            self.stats["bad_track"] += 1
            return
        self.advance(tr["t"])
        name = tr["track"]
        ids = list(tr.get("ids") or [])
        pin = self.links.get(name)
        if pin is not None and ids and self.ids.get(name) and not set(ids) & set(self.ids[name]):
            # None of its keys: the tracker restarted (or wrapped past T255) and this is another beacon
            log.warning("%s now has other ids (tracker restarted?): %s no longer follows it", name, pin)
            del self.links[name]
            self.save()
            pin = None
        new = pin is None
        if new:
            rule = None if tr.get("with_drone") or tr.get("baseline") else self.rule(tr)
            if rule is None:
                self.since.pop(name, None)  # the hold starts again, e.g. once a with_drone mark clears
                return
            # The rule must hold for pin_hold_s first, because a pin stays: early estimates jump
            # about, and our own phones take a few seconds to be marked with_drone
            start = self.since.get(name)
            if start is None or start > tr["t"]:  # new, or bus time went back
                start = self.since[name] = tr["t"]
            if tr["t"] - start < self.hold_ms:
                return
            self.since.pop(name, None)
            self.n += 1
            pin = f"P{self.n}"
            self.pins[pin] = {"pin": pin, "rule": rule, "status": "new", "flags": dict(NO_FLAGS), "br": None}
            self.links[name] = pin
            log.info("%s: new pin for %s %s (%s)", pin, tr["cls"], name, rule)
            self.stats["pins"] += 1
        p = self.pins[pin]
        # The ranger cleared it; or the track is with_drone (picked up, or the pod right over it), so
        # its x, y is the pod's: the pin stays where the beacon was, rather than ride along greyed out
        frozen = p["status"] == "cleared" or bool(tr.get("with_drone"))
        if not frozen:
            x, y = float(tr["x"]), float(tr["y"])
            p.update(t=tr["t"], track=name, cls=tr["cls"], r95=tr["r95"], c=tr.get("c", 0.0),
                     state="confirmed" if p["status"] == "confirmed" else tr["state"], last_s=tr["last_s"])
            self.place(p, x, y)
        if new or (ids and ids != self.ids.get(name)):  # save on a new pin or a key rotation, not every update
            if ids:
                self.ids[name] = ids
            self.save()  # after filling it in, so a restart never loads half a pin
        if not frozen:
            self.publish(p)

    def rule(self, tr):
        """Why this track should be a pin under the current mode, or None."""
        mode = self.rule_mode if self.mode == "investigate" else self.mode
        cls, x, y = tr["cls"], float(tr["x"]), float(tr["y"])
        if mode == "search":
            return "search" if cls in LOST else None
        if cls not in ALWAYS_REPORTED or tr["state"] not in PLACED:
            return None
        if self.pm.dist_to_kind(x, y, "campground") <= tr["r95"]:
            return None  # its 95% circle reaches a campground, a permitted place: maybe in it, so wait
        if mode == "night":
            return "night_outside_camp"
        if self.pm.dist_to_trail(x, y) > self.offtrail_m and (tr.get("passes") or 0) >= self.min_passes:
            return "day_offtrail"
        return None

    def place(self, p, x, y):
        """Position, lat/lon and the map fields; then prio, which depends on everything else."""
        lat, lon = geo.to_latlon(x, y, *self.origin)
        off = self.pm.dist_to_trail(x, y)
        p.update(x=round(x, 1), y=round(y, 1), lat=round(lat, 7), lon=round(lon, 7),
                 off_trail_m=None if math.isinf(off) else round(off),
                 zone=self.pm.zone_label(x, y, off, self.offtrail_m))
        p["prio"] = prio(p)

    def on_ctrl(self, topic, msg):
        cmd, pin = msg.get("cmd"), msg.get("pin")
        if cmd == "new_search":  # every id changes and the tracker starts new tracks; old pins stay
            self.links.clear()
            self.ids.clear()
            self.since.clear()
            self.sos_pin = None  # an SOS answer from now on opens a new pin, not the old search's
            self.save()
            log.info("new search: tracks unlinked, %d pins kept for the ranger", len(self.pins))
            return
        if cmd not in STEPS:
            return
        p = self.pins.get(pin) if isinstance(pin, str) else None
        if p is None:
            log.warning("ignored %s: no pin %r", cmd, pin)
            self.stats["ignored_cmd"] += 1
            return
        allowed, to = STEPS[cmd]
        if p["status"] not in allowed:
            log.warning("ignored %s on %s: it is %s", cmd, pin, p["status"])
            self.stats["ignored_cmd"] += 1
            return
        p["status"] = to
        if to == "confirmed":
            p["state"] = "confirmed"
        if to == "investigating":
            self.inv_t[pin] = self.now
        else:
            self.inv_t.pop(pin, None)
        p["prio"] = prio(p)
        self.touch(p)
        log.info("%s %s", pin, to)
        self.save()
        self.publish(p)

    def investigated(self):
        """The pin set to investigating last, or None."""
        inv = [pin for pin, p in self.pins.items() if p["status"] == "investigating"]
        return self.pins[max(inv, key=lambda pin: self.inv_t.get(pin, 0))] if inv else None

    def handle_thermal(self, msg):
        """A hot spot with the pod over the pin under investigation: set flags.thermal, once.
        thermal.py already ignores hot spots above [thermal] max_alt_m."""
        self.advance(msg.get("t"))
        if msg.get("hot") is not True or self.pose is None:
            return
        p = self.investigated()
        if p is None or p["flags"]["thermal"] or "x" not in p:
            return
        d = math.dist(self.pose, (p["x"], p["y"]))
        if d > (p.get("r95") or 0) + self.thermal_near_m:
            return  # on the way there: that heat is somewhere else
        p["flags"]["thermal"] = True
        self.touch(p)  # seen by the camera, not heard: when the beacon was last heard stays put
        log.info("%s: thermal hot spot with the pod %.0f m away", p["pin"], d)
        self.save()
        self.publish(p)

    def handle_sos(self, msg):
        self.advance(msg.get("t"))
        sos = {"hurt": bool(msg.get("hurt")), "walk": bool(msg.get("walk")), "count": msg.get("count", 0)}
        p = self.investigated()
        if p is not None:
            self.touch(p)  # the answer came over Wi-Fi: the beacon itself wasn't heard
        else:
            open_sos = self.pins.get(self.sos_pin)
            if open_sos is not None and open_sos["status"] not in ("cleared", "confirmed"):
                p = open_sos
            else:
                self.n += 1
                p = self.pins[f"P{self.n}"] = {
                    "pin": f"P{self.n}", "track": "T0", "rule": "sos", "status": "new", "cls": "wifi",
                    "c": 0.0, "state": "heard", "r95": self.sos_r95, "flags": dict(NO_FLAGS), "br": None}
                self.sos_pin = p["pin"]
                self.stats["pins"] += 1
            if self.pose is None:
                log.warning("SOS with no pod position yet: %s goes at the origin", p["pin"])
            p.update(t=max(p.get("t", 0), self.now), last_s=0)  # heard just now
            self.place(p, *(self.pose or (0.0, 0.0)))  # the answer came from within Wi-Fi range of the pod
        p["sos"] = sos
        p["flags"]["sos"] = True
        p["prio"] = prio(p)
        log.info("%s: SOS answer, hurt %s, can walk %s, %s people", p["pin"], sos["hurt"], sos["walk"], sos["count"])
        self.save()
        self.publish(p)

    # ---------- output ----------

    def publish(self, p):
        problems = validate(TARGET, p)
        if problems:
            self.stats["invalid"] += 1
            log.warning("contract: %s", problems)
            return
        self.stats[TARGET] += 1
        self.bus.publish(TARGET, p)

    # ---------- saved pins ----------

    def save(self):
        if self.pins_file is None:
            return
        state = {"n": self.n, "pins": self.pins, "links": self.links, "ids": self.ids,
                 "inv_t": self.inv_t, "sos_pin": self.sos_pin, "rule_mode": self.rule_mode}
        tmp = self.pins_file.with_suffix(".tmp")
        try:
            self.pins_file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(state))
            os.replace(tmp, self.pins_file)  # a crash mid-write never leaves half a file
        except OSError as e:
            log.warning("couldn't save pins to %s: %s", self.pins_file, e)

    def restore(self):
        """Load the saved pins if they're recent, and republish them so the dashboard has them all."""
        f = self.pins_file
        if f is None or not f.exists():
            return
        age = time.time() - f.stat().st_mtime
        if age > self.max_age_s:
            log.info("saved pins are %.0f min old: starting fresh at P1", age / 60)
            return
        try:
            s = json.loads(f.read_text())
            self.n, self.pins, self.links, self.ids = s["n"], s["pins"], s["links"], s["ids"]
            self.inv_t, self.sos_pin, self.rule_mode = s["inv_t"], s["sos_pin"], s["rule_mode"]
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("couldn't read saved pins (%s): starting fresh at P1", e)
            return
        log.info("restored %d pins from %s; next is P%d", len(self.pins), f, self.n + 1)
        for p in self.pins.values():
            self.publish(p)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--broker", help="MQTT host (default: the config's [mqtt] host)")
    ap.add_argument("--fresh", action="store_true", help="ignore the saved pins and start at P1")
    ap.add_argument("--dry-run", action="store_true",
                    help="read `mosquitto_sub -v` lines from stdin, print instead of publishing (saves nothing)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = cfgmod.load(a.config)

    if a.dry_run:
        g = Geofence(cfg, PrintBus())
        for line in sys.stdin:
            topic, _, payload = line.strip().partition(" ")
            try:
                msg = json.loads(payload)
            except ValueError:
                continue
            if isinstance(msg, dict):
                g.feed(topic, msg)
        log.info("%d pins, stats: %s", len(g.pins), dict(g.stats))
        return

    pins_file = cfgmod.resolve(cfg, cfg["rules"]["pins_file"]) if cfg["rules"]["pins_file"] else None
    bus = Bus(a.broker or cfg["mqtt"]["host"], cfg["mqtt"]["port"], "geofence")
    g = Geofence(cfg, bus, pins_file=pins_file)
    if a.fresh:
        g.save()  # overwrite the old pins now, or a lora_out restarted after this reloads them
    else:
        g.restore()
    inbox = queue.Queue()  # handlers only queue, so paho's thread stays fast; this loop does the work
    for topic in (TRACK, POSE, MODE, CTRL, SOS, THERMAL):
        bus.subscribe(topic, lambda topic, msg: inbox.put((topic, msg)))
    last_log = time.monotonic()
    try:
        while True:
            try:
                g.feed(*inbox.get(timeout=1.0))
            except queue.Empty:
                pass
            except Exception:  # one bad message mustn't stop the pins
                log.exception("failed on a message")
            if time.monotonic() - last_log > 30:
                last_log = time.monotonic()
                log.info("%d pins, mode %s, stats: %s", len(g.pins), g.mode, dict(g.stats))
    except KeyboardInterrupt:
        bus.close()


if __name__ == "__main__":
    main()
