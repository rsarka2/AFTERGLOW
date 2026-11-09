"""thermal.py (tier 1 view, Utility): HTI HT-203U thermal camera -> live/thermal.png + ag/thermal.

    python -m afterglow.thermal --fake                        # made-up frames, to the broker
    python -m afterglow.thermal --fake --dry-run --frames 8   # no broker: print ag/thermal (still writes the PNG)
    python -m afterglow.thermal                               # on the Pi: the real camera (needs v4l2-ctl)

Every frame goes through Thermal.handle_frame(): find the warm spot, decide `hot`, write the
picture for the dashboard (temp file, then rename) and publish ag/thermal (CONTRACTS.md).
Above [thermal] max_alt_m (the latest ag/pose `z`) a person is too few pixels to tell from a
warm rock, so `hot` stays false up there. With no pose yet, any height counts.

A frame is a 2-D array, in one of two kinds:
- degrees C, if the camera gives temperatures: `max_c` is the spot's temperature, and `hot`
  means body-warm (the [thermal] band) and well above the rest of the frame;
- 0-255 brightness, if it gives only a picture with automatic contrast: `max_c` is null, and
  `hot` means the spot stands far out from the rest of the frame.
The HT-203U gives temperatures: its 256x392 mode carries raw heat values above the picture
(parse_raw; format test Sat 7 Nov). --fake feeds made-up frames in degrees C.
"""
import argparse
import logging
import math
import os
import re
import shutil
import struct
import sys
import time
import zlib
from pathlib import Path

import numpy as np

from . import config as cfgmod
from .bus import Bus, PrintBus
from .contracts import POSE, THERMAL, now_ms

log = logging.getLogger("thermal")

MIN_SPAN_C = 8.0  # the picture's colour range is at least this many degrees, so a flat scene doesn't look on fire
HOT_EDGE_PX = 5  # an engine's or a hot rock's rim cools through the body band: ignore this many pixels round it
MARK = (0, 255, 255)  # cyan box round a hot spot
# Ironbow-style palette: black, purple, red, orange, yellow, white.
_ANCHORS = [(0.0, (0, 0, 16)), (0.2, (64, 0, 128)), (0.4, (170, 0, 140)),
            (0.6, (235, 70, 20)), (0.8, (255, 180, 0)), (1.0, (255, 255, 230))]
PALETTE = np.stack([np.interp(np.linspace(0, 1, 256), [a for a, _ in _ANCHORS], [c[i] for _, c in _ANCHORS])
                    for i in range(3)], axis=1).round().astype(np.uint8)


def box3(a: np.ndarray) -> np.ndarray:
    """3x3 mean, so a single hot pixel (noise, a dead pixel) can't be the spot on its own."""
    h, w = a.shape
    p = np.pad(a, 1, mode="edge")
    return sum(p[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3)) / 9.0


def png_bytes(rgb: np.ndarray) -> bytes:
    """An 8-bit RGB PNG from an (h, w, 3) uint8 array, with only the standard library."""
    h, w, _ = rgb.shape
    raw = np.concatenate([np.zeros((h, 1), np.uint8), rgb.reshape(h, w * 3)], axis=1).tobytes()

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 3)) + chunk(b"IEND", b""))


def write_atomic(path: Path, data: bytes):
    """Write to a temp file, then rename over the old one, so the dashboard never reads half a frame."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    # On Windows (laptop, --fake) the rename fails while the dashboard is reading the old picture:
    # try again briefly, then skip this frame. On the Pi (Linux) the first try always works.
    for wait in (0.01, 0.03, 0.1, None):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if wait is None:
                log.debug("%s busy, frame skipped", path.name)
                return
            time.sleep(wait)


class Thermal:
    def __init__(self, cfg: dict, bus, live_dir: str | os.PathLike):
        th = cfg["thermal"]
        self.bus = bus
        self.out = Path(live_dir) / "thermal.png"
        self.hot_min_c, self.hot_max_c = float(th["hot_min_c"]), float(th["hot_max_c"])
        self.hot_rise_c = float(th["hot_rise_c"])
        self.hot_z = float(th["hot_z"])
        self.max_alt_m = float(th["max_alt_m"])
        self.z = None  # the pod's height from the latest ag/pose
        self.rotate = int(th["rotate"]) % 360
        self.flip = bool(th["flip"])
        if self.rotate % 90:
            raise ValueError(f"[thermal] rotate must be 0, 90, 180 or 270, not {th['rotate']}")
        self.frames = 0
        self.bad = 0
        self.was_hot = None

    def _orient(self, a):
        if self.flip:
            a = a[:, ::-1]
        return np.rot90(a, k=-(self.rotate // 90))  # clockwise

    def handle_pose(self, msg):
        """ag/pose in: keep its height (runs on paho's thread; one assignment, so no lock)."""
        z = msg.get("z")
        if isinstance(z, (int, float)) and not isinstance(z, bool) and math.isfinite(z):
            self.z = float(z)

    def too_high(self) -> bool:
        return self.z is not None and self.z > self.max_alt_m

    def handle_frame(self, frame, celsius: bool, src: str, t: int | None = None):
        """One frame in; writes the PNG, publishes ag/thermal and returns that message. None for a bad frame."""
        a = np.asarray(frame, dtype=float)
        if a.ndim != 2 or min(a.shape) < 3 or not np.isfinite(a).all():
            self.bad += 1
            if self.bad in (1, 10) or self.bad % 100 == 0:
                log.warning("skipped %d bad frame(s); last shape %s", self.bad, a.shape)
            return None
        a = self._orient(a)
        h, w = a.shape
        sm = box3(a)
        med = float(np.median(a))
        if celsius:
            # The warmest body-warm spot; a hot engine or sun-baked rock is outside the band and can't hide a person.
            band = (sm >= self.hot_min_c) & (sm <= self.hot_max_c)
            too_hot = sm > self.hot_max_c
            if too_hot.any():
                near = too_hot.astype(float)
                for _ in range(HOT_EDGE_PX):
                    near = box3(near)  # grows the too-hot area by a pixel each time
                band &= near == 0
            pick = np.where(band, sm, -np.inf) if band.any() else sm
            row, col = np.unravel_index(int(np.argmax(pick)), pick.shape)
            peak = float(sm[row, col])
            score = (peak - med) / self.hot_rise_c if band[row, col] else 0.0
            max_c = round(peak, 1)
        else:
            # Picture only: the camera's automatic contrast stretches any scene, so measure the spot
            # in spreads of the frame (median absolute deviation), not in brightness levels.
            row, col = np.unravel_index(int(np.argmax(sm)), sm.shape)
            peak = float(sm[row, col])
            spread = max(1.4826 * float(np.median(np.abs(a - med))), 1.0)
            score = (peak - med) / spread / self.hot_z
            max_c = None
        high = self.too_high()
        hot = score >= 1.0 and not high
        # 0.5 at the threshold, 1.0 at twice it; 0 when too high, so no panel shows confidence without a box
        conf = 0.0 if high else round(min(1.0, max(0.0, score / 2)), 2)

        write_atomic(self.out, png_bytes(self.render(a, celsius, (row, col) if hot else None)))
        msg = {"t": t or now_ms(), "max_c": max_c, "hot": hot, "col": int(col), "row": int(row),
               "w": w, "h": h, "conf": conf, "src": src}
        self.bus.publish(THERMAL, msg)
        self.frames += 1
        if hot != self.was_hot:
            if high:
                log.info("pod at %.0f m, above max_alt_m %.0f m: hot spots ignored", self.z, self.max_alt_m)
            else:
                log.info("hot spot at col %d row %d (%s)" if hot else "no hot spot (warmest col %d row %d, %s)",
                         col, row, f"{max_c} C" if max_c is not None else "no temperatures")
            self.was_hot = hot
        return msg

    def render(self, a, celsius, spot=None) -> np.ndarray:
        """False-colour picture of the frame, with a box round the hot spot."""
        if celsius:
            # Tops out at hot_max_c, so an engine or a hot rock (white) can't wash out a person.
            lo = float(np.percentile(a, 1))
            hi = max(min(float(a.max()), self.hot_max_c), lo + MIN_SPAN_C)
            idx = (a - lo) / (hi - lo) * 255
        else:
            idx = a
        rgb = PALETTE[np.clip(idx, 0, 255).astype(np.uint8)]
        if spot is not None:
            h, w = a.shape
            r, c = spot
            r0, r1, c0, c1 = max(r - 6, 0), min(r + 6, h - 1), max(c - 6, 0), min(c + 6, w - 1)
            rgb[r0, c0:c1 + 1] = rgb[r1, c0:c1 + 1] = MARK
            rgb[r0:r1 + 1, c0] = rgb[r0:r1 + 1, c1] = MARK
        return rgb


def fake_frame(i: int, rng: np.random.Generator, w: int = 256, h: int = 192, person: bool | None = None) -> np.ndarray:
    """Frame i of the made-up scene, in degrees C.

    A cool background (16-18 C), a sun-warmed rock at 48 C in one corner (too hot for a person,
    so it's ignored) and a person-sized blob at about 34 C. person None: the blob drifts on a slow
    loop, out of the frame for part of it, so `hot` turns on and off. person True: the blob sits
    near the middle, as when the drone circles over someone; False: no blob.
    """
    yy, xx = np.mgrid[0:h, 0:w]
    a = 16.0 + 2.0 * yy / h + rng.normal(0, 0.15, (h, w))
    a += 32.0 * np.exp(-(((xx - 0.88 * w) / 5) ** 2 + ((yy - 0.15 * h) / 4) ** 2))  # the rock
    if person is False:
        return a
    ph = 2 * math.pi * i / 160  # 40 s a loop at 4 frames a second
    k = 0.62 if person is None else 0.08  # how far it wanders across the frame
    cx, cy = w / 2 + k * w * math.sin(ph), h / 2 + 0.4 * k * h * math.sin(2 * ph)
    blob = np.exp(-(((xx - cx) / 4) ** 2 + ((yy - cy) / 8) ** 2))  # about 8 x 16 px: a person some way off
    return a + (34.0 - a) * blob


def fake_frames(seed: int = 0, person=None):
    """person: None for the drifting loop, or a function saying whether a person is in view now
    (local_stack.py ties it to the fake pod)."""
    rng = np.random.default_rng(seed)
    i = 0
    while True:
        yield fake_frame(i, rng, person=None if person is None else bool(person())), True, "fake"
        i += 1


RAW_W, RAW_H = 256, 392  # the HT-203U's double-height YUYV mode (format test, Sat 7 Nov)
TEMP_ROWS, HEADER_ROWS = 192, 4  # rows 0-191: 16-bit raw heat; 192-195: header; 196-387: the 8-bit picture
FRAME_BYTES = RAW_W * RAW_H * 2
# A frame out of step (a short frame shifted the pipe) or in the wrong mode reads garbage, so parse_raw
# rejects one whose sensor (the camera's own temperature) or more than BAD_PIXELS_MAX of its pixels
# are outside these ranges. Pixel data where raw heat should be reads well over 1000 C.
SENSOR_OK_C = (-20.0, 90.0)
SCENE_OK_C = (-100.0, 1000.0)
BAD_PIXELS_MAX = 0.01
BAD_RUN = 3  # this many bad frames in a row restarts the stream (one or two: the shutter click, skipped)
STALL_S = 10.0  # no frame for this long restarts the stream


class CameraError(Exception):
    """The camera answers but can't give raw heat (wrong mode): log it and retry, no traceback."""


def parse_raw(buf: bytes, per_c: float, offset_c: float, sensor_k: float = 0.0, sensor_ref_c: float = 0.0):
    """One 256x392 YUYV buffer from the HT-203U -> (degrees C, 192 x 256; the sensor's own temperature in C).

    The top 192 rows aren't a picture: each 2-byte pixel is a little-endian raw heat value, linear in
    temperature. Header row 0, word 0 is the sensor's own temperature in hundredths of a degree.
    The raw values sag as the camera warms up (a wall: 5101 at sensor 22.5 C, 4998 at 24.7 C), so:
        degrees C = (raw + sensor_k * (sensor - sensor_ref_c)) / per_c - offset_c
    ([thermal] raw_per_c, raw_offset_c, raw_sensor_k, raw_sensor_ref_c; from a wall at about 22 C and
    a palm, Sat 7 Nov, so a degree or two out). ValueError for a buffer that can't be a raw heat frame."""
    if len(buf) != FRAME_BYTES:
        raise ValueError(f"frame is {len(buf)} bytes, not {FRAME_BYTES} (256x392 YUYV)")
    words = np.frombuffer(buf, "<u2").reshape(RAW_H, RAW_W)
    sensor = words[TEMP_ROWS, 0] / 100.0
    if not SENSOR_OK_C[0] <= sensor <= SENSOR_OK_C[1]:
        raise ValueError(f"header says the sensor is at {sensor:.1f} C: frame out of step or not raw heat")
    raw = words[:TEMP_ROWS].astype(np.float32) + sensor_k * (sensor - sensor_ref_c)
    c = raw / per_c - offset_c
    bad = float(np.mean((c < SCENE_OK_C[0]) | (c > SCENE_OK_C[1])))
    if bad > BAD_PIXELS_MAX:
        raise ValueError(f"{bad:.0%} of pixels read outside {SCENE_OK_C[0]:.0f} to {SCENE_OK_C[1]:.0f} C: "
                         "frame out of step or not raw heat")
    return c, sensor


def format_problem(text: str) -> str | None:
    """`v4l2-ctl --get-fmt-video` output -> what's wrong with the camera's mode, or None if it's 256x392
    YUYV. Also None if the output can't be read: parse_raw's checks still catch a wrong mode."""
    m = re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", text)
    if m is None:
        return None
    pf = re.search(r"Pixel Format\s*:\s*'([^']*)'", text)
    fourcc = pf[1].strip() if pf else "YUYV"
    if (int(m[1]), int(m[2])) != (RAW_W, RAW_H) or fourcc != "YUYV":
        return (f"thermal camera is in {m[1]}x{m[2]} {fourcc} mode, not {RAW_W}x{RAW_H} YUYV, so no raw heat "
                "(check v4l2-ctl --list-formats-ext)")
    return None


def calibration(th: dict) -> tuple:
    """[thermal] -> parse_raw's calibration arguments."""
    return (float(th["raw_per_c"]), float(th["raw_offset_c"]), float(th["raw_sensor_k"]), float(th["raw_sensor_ref_c"]))


def find_device(th: dict) -> str:
    dev = th["device"]
    if os.path.exists(dev):
        return dev
    import glob
    found = sorted(glob.glob("/dev/v4l/by-id/*HikCamera*index0"))
    if found:
        log.warning("%s missing (udev rule not installed?): using %s", dev, found[0])
        return found[0]
    raise SystemExit(f"no thermal camera: {dev} missing and no /dev/v4l/by-id/*HikCamera* (plugged in?)")


def check_camera(th: dict) -> str:
    """The camera's device, or SystemExit with what to do: no camera, or no v4l2-ctl to read it with."""
    dev = find_device(th)
    if shutil.which("v4l2-ctl") is None:
        raise SystemExit("v4l2-ctl not installed, so the thermal camera can't be read (sudo apt install -y v4l-utils)")
    return dev


def camera_frames(cfg: dict, retry_s: float = 2.0, stall_s: float = STALL_S):
    """The real HT-203U: v4l2-ctl streams raw 256x392 YUYV frames (25 fps) into a pipe; a thread keeps
    only the newest, so run() always gets a fresh frame at [thermal] fps. The reader thread logs and
    retries on any error rather than dying quietly, and won't stream a camera in the wrong mode. This
    loop restarts the stream (kills v4l2-ctl; the reader starts a new one, in step from its first byte)
    after stall_s with no frame or BAD_RUN bad frames in a row."""
    import subprocess
    import threading

    th = cfg["thermal"]
    cal = calibration(th)
    anchor = None if th["absolute"] else float(th["ambient_c"])
    # buf: the newest frame; src: the v4l2-ctl that sent it; proc: the one streaming now (None between streams)
    latest = {"buf": None, "src": None, "n": 0, "proc": None}
    cond = threading.Condition()

    check_camera(th)  # no camera or no v4l2-ctl: stop now with a clear message, not in the thread

    def stream(dev):
        """One v4l2-ctl stream, until it ends (or is killed)."""
        # Setting the format can fail with "Input/output error" when it's already set: harmless.
        # So read the mode back instead: in any other mode the frames hold no raw heat.
        subprocess.run(["v4l2-ctl", "-d", dev, f"--set-fmt-video=width={RAW_W},height={RAW_H},pixelformat=YUYV"],
                       capture_output=True, timeout=10)
        fmt = subprocess.run(["v4l2-ctl", "-d", dev, "--get-fmt-video"], capture_output=True, text=True, timeout=10)
        problem = format_problem(fmt.stdout or "")
        if problem:
            raise CameraError(problem)
        p = subprocess.Popen(["v4l2-ctl", "-d", dev, "--stream-mmap", "--stream-count=0", "--stream-to=-"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        with cond:
            latest["proc"] = p
        log.info("thermal camera streaming from %s", dev)
        try:
            while True:
                buf = p.stdout.read(FRAME_BYTES)
                if not buf or len(buf) < FRAME_BYTES:
                    return
                with cond:
                    latest["buf"], latest["src"], latest["n"] = buf, p, latest["n"] + 1
                    cond.notify_all()
        finally:
            with cond:
                latest["proc"] = None
            p.kill()
            p.wait()  # reap it, so restarts don't leave zombie processes

    def reader():
        while True:
            try:
                dev = find_device(th)
                stream(dev)
                log.error("thermal stream from %s stopped; restarting in %g s", dev, retry_s)
            except (SystemExit, CameraError) as e:  # unplugged or in the wrong mode: keep trying
                log.error("%s; retrying in %g s", e, retry_s)
            except Exception:  # v4l2-ctl gone, a pipe error: say so and keep trying
                log.exception("thermal camera reader failed; retrying in %g s", retry_s)
            time.sleep(retry_s)

    def restart(p, why):
        """Kill one v4l2-ctl stream (if it's still there) so the reader starts afresh."""
        if p is None:
            log.warning("%s", why)  # between streams: the reader is already retrying
        else:
            log.error("%s; restarting the thermal stream", why)
            p.kill()

    threading.Thread(target=reader, daemon=True, name="thermal-reader").start()
    seen = bad_run = 0
    while True:
        with cond:
            fresh = cond.wait_for(lambda: latest["n"] > seen, timeout=stall_s)
            buf, src, seen, now = latest["buf"], latest["src"], latest["n"], latest["proc"]
        if not fresh:
            restart(now, f"no thermal frame for {stall_s:g} s")
            continue
        try:
            celsius, sensor_c = parse_raw(buf, *cal)
        except ValueError as e:
            bad_run += 1
            if bad_run == 1:
                log.warning("skipped a thermal frame: %s", e)
            if bad_run >= BAD_RUN:
                restart(src, f"{bad_run} bad thermal frames in a row ({e})")
                bad_run = 0
            continue
        bad_run = 0
        if anchor is not None:
            # The raw offset drifts by many degrees as the camera warms; shift the frame so its
            # background reads ambient_c. A person still stands out by the same number of degrees.
            celsius = celsius + (anchor - float(np.median(celsius)))
        yield celsius, True, "ht203u"


def run(th: Thermal, frames, fps: float, limit: int = 0, sleep: bool = True):
    period = 1.0 / fps
    due = time.monotonic()
    for n, (frame, celsius, src) in enumerate(frames, 1):
        th.handle_frame(frame, celsius, src)
        if limit and n >= limit:
            return
        if sleep:
            due += period
            time.sleep(max(0.0, due - time.monotonic()))
            due = max(due, time.monotonic() - period)  # after a stall, don't rush to catch up


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--fake", action="store_true", help="made-up frames instead of the camera")
    ap.add_argument("--dry-run", action="store_true", help="print ag/thermal instead of publishing, as fast as it can")
    ap.add_argument("--frames", type=int, default=0, help="stop after this many frames (0 = run until stopped)")
    ap.add_argument("--seed", type=int, default=0, help="--fake: noise seed")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stderr)
    cfg = cfgmod.load(a.config)
    live = cfgmod.resolve(cfg, cfg["http"]["live_dir"])

    if a.fake:
        frames = fake_frames(a.seed)
    else:
        try:
            check_camera(cfg["thermal"])
        except SystemExit as e:
            log.error("%s; use --fake for made-up frames", e)
            sys.exit(2)
        frames = camera_frames(cfg)

    bus = PrintBus() if a.dry_run else Bus(cfg["mqtt"]["host"], cfg["mqtt"]["port"], "thermal")
    th = Thermal(cfg, bus, live)
    bus.subscribe(POSE, lambda topic, msg: th.handle_pose(msg))  # the height, for max_alt_m
    log.info("writing %s at %s fps from %s", th.out, cfg["thermal"]["fps"], "fake frames" if a.fake else "the camera")
    try:
        run(th, frames, float(cfg["thermal"]["fps"]), a.frames, sleep=not a.dry_run)
    except KeyboardInterrupt:
        pass
    finally:
        bus.close()


if __name__ == "__main__":
    main()
