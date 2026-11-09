"""position.py (tier 1, Utility): the pod's position -> ag/pose. [pose] source picks how:

    "gps"        GPS NMEA on [serial] gps (ttyAMA2); with gps = "" only waypoint taps move it
    "imu"        the BNO086 carried in a straight line along [imu] line_from -> line_to:
                 distance = step count x stride_m (no integrating acceleration), heading for show
    "waypoints"  taped waypoint taps only

    python -m afterglow.position                          # on the Pi
    python -m afterglow.position --dry-run --source gps < nmea.txt   # gps: no hardware, no broker
    python -m afterglow.position --fake-walk --dry-run    # imu: a simulated walker, no hardware, no broker
    # the dashboard (or you) taps a waypoint (imu: the walk re-anchors to its spot on the line):
    mosquitto_pub -t ag/ctrl -m '{"cmd":"waypoint","n":3}'

The FPV's flight controller doesn't share its position, so the pod reads its own GPS (or IMU indoors).
"""
import argparse
import logging
import math
import random
import sys
import threading
import time

import pynmea2

from . import config as cfgmod
from . import geo
from .bus import Bus, PrintBus
from .contracts import CTRL, POSE, now_ms

log = logging.getLogger("position")
KNOT = 0.514444


class Position:
    def __init__(self, cfg: dict, bus):
        self.bus = bus
        m = cfg["mission"]
        self.lat0, self.lon0 = m.get("origin_lat", 0.0), m.get("origin_lon", 0.0)
        self.have_origin = not (self.lat0 == 0.0 and self.lon0 == 0.0)
        self.carry_h = cfg["pose"].get("carry_height_m", 3.0)
        self.min_dt = 1000 / cfg["pose"].get("publish_hz", 5)
        self.waypoints = cfg.get("waypoints", {})
        self.takeoff_alt = None
        self.course = self.speed = None
        self.last_pub = 0
        self.no_fix = 0

    def handle_nmea(self, line: str, t: int | None = None):
        """Feed one NMEA sentence; returns the pose it published, if any."""
        if not line.startswith("$"):
            return None
        try:
            msg = pynmea2.parse(line)
        except pynmea2.ParseError:
            return None
        if isinstance(msg, pynmea2.types.talker.RMC):
            if msg.spd_over_grnd is not None:
                self.speed = round(float(msg.spd_over_grnd) * KNOT, 2)
            if msg.true_course is not None:
                self.course = float(msg.true_course)
            return None
        if not isinstance(msg, pynmea2.types.talker.GGA):
            return None
        fix = int(msg.gps_qual or 0)
        if fix == 0 or msg.latitude == 0.0:
            self.no_fix += 1
            return None
        lat, lon = msg.latitude, msg.longitude
        alt = float(msg.altitude) if msg.altitude is not None else None
        if not self.have_origin:
            self.lat0, self.lon0, self.have_origin = lat, lon, True
            log.warning("origin set from first fix: origin_lat = %.7f, origin_lon = %.7f "
                        "(copy into afterglow.toml on the pod AND the laptop)", lat, lon)
        if self.takeoff_alt is None and alt is not None:
            self.takeoff_alt = alt
        t = t or now_ms()
        if t - self.last_pub < self.min_dt:
            return None
        x, y = geo.to_xy(lat, lon, self.lat0, self.lon0)
        z = max(0.0, alt - self.takeoff_alt) if alt is not None and self.takeoff_alt is not None else self.carry_h
        pose = {"t": t, "x": round(x, 2), "y": round(y, 2), "z": round(z, 1),
                "lat": round(lat, 7), "lon": round(lon, 7), "alt_m": alt,
                "course": self.course, "speed_mps": self.speed,
                "fix": fix, "sats": int(msg.num_sats or 0), "src": "gps"}
        self.last_pub = t
        self.bus.publish(POSE, pose)
        return pose

    def on_ctrl(self, topic, msg):
        if msg.get("cmd") != "waypoint":
            return
        n = int(msg.get("n", -1))
        if n not in self.waypoints:
            log.warning("unknown waypoint %s", n)
            return
        x, y = self.waypoints[n]
        lat, lon = geo.to_latlon(x, y, self.lat0, self.lon0)
        self.bus.publish(POSE, {"t": now_ms(), "x": x, "y": y, "z": self.carry_h,
                                "lat": round(lat, 7), "lon": round(lon, 7), "src": "waypoint", "n": n})


class ImuWalk:
    """Steps along a straight line -> ag/pose with src "imu".

    The first reading is the zero. Distance along the line = offset + (steps - base) x stride,
    clamped to the line. A waypoint tap moves offset to that waypoint's spot on the line.
    """

    def __init__(self, cfg: dict, bus):
        self.bus = bus
        m = cfg["mission"]
        self.lat0, self.lon0 = m.get("origin_lat", 0.0), m.get("origin_lon", 0.0)
        self.carry_h = cfg["pose"].get("carry_height_m", 3.0)
        self.waypoints = cfg.get("waypoints", {})
        im = cfg["imu"]
        self.x0, self.y0 = (float(v) for v in im["line_from"])
        x1, y1 = (float(v) for v in im["line_to"])
        self.len = math.hypot(x1 - self.x0, y1 - self.y0)
        # unit vector along the line (a zero-length line just stays at line_from)
        self.ux, self.uy = ((x1 - self.x0) / self.len, (y1 - self.y0) / self.len) if self.len else (1.0, 0.0)
        self.bearing = math.degrees(math.atan2(self.ux, self.uy)) % 360  # compass: 0 north, 90 east
        self.stride = float(im.get("stride_m", 0.7))
        self.base = None     # raw step count at the last zero or re-anchor
        self.offset = 0.0    # metres along the line at base
        self.steps = None    # latest raw step count
        self.yaw_ref = None  # the yaw that means "facing along the line"
        self.heading = None
        self.lock = threading.Lock()  # handle_imu runs on the main thread, on_ctrl on paho's

    def dist(self) -> float:
        walked = (self.steps - self.base) * self.stride if self.base is not None else 0.0
        return min(self.len, max(0.0, self.offset + walked))

    def handle_imu(self, steps: int, yaw_deg: float | None = None, t: int | None = None):
        """Feed one reading (yaw clockwise in degrees, any zero); publishes and returns the pose."""
        with self.lock:
            steps = int(steps)
            if self.base is None:
                self.base = steps
            elif steps < self.steps:  # counter went backwards: the sensor reset. Keep the distance so far.
                log.warning("step counter went back (%d -> %d): sensor reset, re-zeroing", self.steps, steps)
                self.offset, self.base = self.dist(), steps
                if self.heading is not None and yaw_deg is not None:  # its frame restarted too
                    self.yaw_ref = yaw_deg - (self.heading - self.bearing)
            self.steps = steps
            if yaw_deg is not None:
                if self.yaw_ref is None:
                    self.yaw_ref = yaw_deg
                self.heading = (self.bearing + yaw_deg - self.yaw_ref) % 360
            return self._publish(t)

    def _publish(self, t=None, n=None):
        d = self.dist()
        x, y = self.x0 + d * self.ux, self.y0 + d * self.uy
        lat, lon = geo.to_latlon(x, y, self.lat0, self.lon0)
        pose = {"t": t or now_ms(), "x": round(x, 2), "y": round(y, 2), "z": self.carry_h,
                "lat": round(lat, 7), "lon": round(lon, 7), "src": "imu", "dist_m": round(d, 2),
                "steps": (self.steps - self.base) if self.base is not None else 0}
        if self.heading is not None:
            pose["heading"] = round(self.heading, 1)
        if n is not None:
            pose["n"] = n
        self.bus.publish(POSE, pose)
        return pose

    def on_ctrl(self, topic, msg):
        """A waypoint tap: the pod is at waypoint n's spot on the line (0 = back to the start)."""
        if msg.get("cmd") != "waypoint":
            return
        n = int(msg.get("n", -1))
        if n not in self.waypoints:
            log.warning("unknown waypoint %s", n)
            return
        wx, wy = self.waypoints[n]
        s = (wx - self.x0) * self.ux + (wy - self.y0) * self.uy
        with self.lock:
            self.offset = min(self.len, max(0.0, s))
            self.base = self.steps  # None before the first reading: that reading becomes the base
            self._publish(n=n)


def imu_readings(cfg: dict):
    """Yield (steps, yaw_deg) from the BNO086 at [pose] publish_hz. Yaw is clockwise, from the
    game rotation vector (no magnetometer, so its zero is wherever the sensor started)."""
    import board
    from digitalio import DigitalInOut
    from adafruit_bno08x import BNO_REPORT_GAME_ROTATION_VECTOR, BNO_REPORT_STEP_COUNTER
    from adafruit_bno08x.i2c import BNO08X_I2C

    addr = cfg["i2c"]["imu_addr"]
    dt = 1.0 / cfg["pose"].get("publish_hz", 5)
    while True:
        try:
            reset = DigitalInOut(board.D23)
            try:
                bno = BNO08X_I2C(board.I2C(), address=addr, reset=reset)
                bno.enable_feature(BNO_REPORT_STEP_COUNTER)
                bno.enable_feature(BNO_REPORT_GAME_ROTATION_VECTOR)
                log.info("IMU (BNO086) at 0x%02x: steps + game rotation vector", addr)
                steps = 0  # the counter starts at 0 after the reset
                while True:
                    try:
                        steps = bno.steps
                    except RuntimeError:  # no step report yet
                        pass
                    try:
                        i, j, k, r = bno.game_quaternion
                        # yaw about the vertical axis is counter-clockwise; flip it to compass sense
                        yaw = -math.degrees(math.atan2(2 * (r * k + i * j), 1 - 2 * (j * j + k * k)))
                    except RuntimeError:  # no rotation report yet
                        yaw = None
                    yield steps, yaw
                    time.sleep(dt)
            finally:
                reset.deinit()
        except Exception as e:
            log.error("IMU at 0x%02x: %s; retrying in 2 s", addr, e)
            threading.Event().wait(2)


def fake_walk(hz: float = 5, rate: float = 1.7, seed: int | None = None, sleep=time.sleep):
    """Yield (steps, yaw_deg) like imu_readings: a walker at about `rate` steps/s who stops
    now and then, with a little heading wobble. No hardware."""
    rng = random.Random(seed)
    steps, yaw, walking, left = 0.0, 0.0, True, rng.uniform(4, 10)
    while True:
        yield int(steps), round(yaw, 1)
        sleep(1.0 / hz)
        left -= 1.0 / hz
        if left <= 0:  # switch between walking and a short stop
            walking = not walking
            left = rng.uniform(4, 10) if walking else rng.uniform(1.5, 4)
        if walking:
            steps += rate * rng.uniform(0.85, 1.15) / hz
            yaw = max(-8.0, min(8.0, yaw + rng.uniform(-1.5, 1.5)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--dry-run", action="store_true",
                    help="print instead of publishing (gps: read NMEA from stdin)")
    ap.add_argument("--fake-walk", action="store_true",
                    help="imu mode with a simulated walker instead of the BNO086 (no hardware)")
    ap.add_argument("--source", choices=["gps", "imu", "waypoints"], help="override [pose] source")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = cfgmod.load(a.config)
    source = "imu" if a.fake_walk else a.source or cfg["pose"].get("source", "gps")
    if source not in ("gps", "imu", "waypoints"):
        ap.error(f'[pose] source must be "gps", "imu" or "waypoints", not {source!r}')

    if source == "imu":
        bus = PrintBus() if a.dry_run else Bus(cfg["mqtt"]["host"], cfg["mqtt"]["port"], "position")
        w = ImuWalk(cfg, bus)
        bus.subscribe(CTRL, w.on_ctrl)
        hz = cfg["pose"].get("publish_hz", 5)
        readings = fake_walk(hz) if a.fake_walk else imu_readings(cfg)
        log.info("IMU walk along %s -> %s (%.1f m), %.2f m a step%s", cfg["imu"]["line_from"],
                 cfg["imu"]["line_to"], w.len, w.stride, " (fake walker)" if a.fake_walk else "")
        try:
            for steps, yaw in readings:  # every reading publishes, so a standing pod stays alive
                w.handle_imu(steps, yaw)
        except KeyboardInterrupt:
            pass
        finally:
            bus.close()
        return

    if a.dry_run:
        p = Position(cfg, PrintBus())
        p.min_dt = 0
        for line in sys.stdin:
            p.handle_nmea(line.strip())
        return

    import serial
    bus = Bus(cfg["mqtt"]["host"], cfg["mqtt"]["port"], "position")
    p = Position(cfg, bus)
    bus.subscribe(CTRL, p.on_ctrl)
    port, baud = cfg["serial"]["gps"], cfg["serial"]["gps_baud"]
    if source == "waypoints" or not port:  # no GPS receiver: only the taped waypoints move ag/pose
        log.info("waypoint taps only ([pose] source = %r, [serial] gps = %r)", source, port)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            bus.close()
        return
    while True:
        try:
            with serial.Serial(port, baud, timeout=2) as s:
                log.info("GPS on %s at %d", port, baud)
                while True:
                    raw = s.readline()
                    if raw:
                        p.handle_nmea(raw.decode("ascii", "replace").strip())
        except Exception as e:
            log.error("GPS %s: %s; retrying in 2 s", port, e)
            threading.Event().wait(2)


if __name__ == "__main__":
    main()
