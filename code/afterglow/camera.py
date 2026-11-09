"""camera.py (tier 1, Software): Camera Module 3 -> live/camera.jpg, the dashboard's camera panel.

    python -m afterglow.camera --fake              # made-up pictures (laptop)
    python -m afterglow.camera --fake --frames 8   # write 8 and stop
    python -m afterglow.camera                     # on the Pi: the real camera

A live view of what the pod sees, next to thermal, so the ranger can look for themselves. It
detects nothing. The only thing drawn on the picture is the time it was taken (so a frozen feed is
obvious), plus FAKE on made-up pictures. Person detection on the AI HAT+ is a stretch goal
(TODO.md); it would go in this module, because only one process can open the camera.

No MQTT: it only writes the file (temp file, then rename, so the dashboard never reads half a
picture). Settings are in the config's [camera] table.
"""
import argparse
import io
import logging
import math
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import config as cfgmod
from .thermal import write_atomic

log = logging.getLogger("camera")

FULL_VIEW = (2304, 1296)  # IMX708's 2x2-binned mode: the whole field of view (its 1536x864 mode is a crop)
FONT_FILE = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"  # on Pi OS
PAD = 6


def load_font(px: int):
    try:
        return ImageFont.truetype(FONT_FILE, px)
    except OSError:
        pass
    try:
        return ImageFont.load_default(size=px)  # Pillow 10.1+
    except TypeError:
        return ImageFont.load_default()  # older Pillow: a small bitmap font


class Camera:
    def __init__(self, cfg: dict, live_dir):
        c = cfg["camera"]
        self.out = Path(live_dir) / "camera.jpg"
        self.quality = int(c["quality"])
        self.rotate = int(c["rotate"]) % 360
        self.flip = bool(c["flip"])
        if self.rotate % 90:
            raise ValueError(f"[camera] rotate must be 0, 90, 180 or 270, not {c['rotate']}")
        self.font = load_font(max(12, int(c["height"]) // 28))
        self.frames = 0

    def orient(self, img: Image.Image) -> Image.Image:
        if self.flip:
            img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if self.rotate:
            img = img.rotate(-self.rotate, expand=True)  # clockwise
        return img

    def stamp(self, img: Image.Image, src: str, t_s: float):
        """The time the picture was taken, bottom left, on a dark box; FAKE on made-up pictures."""
        text = time.strftime("%H:%M:%S", time.localtime(t_s)) + ("  FAKE" if src == "fake" else "")
        d = ImageDraw.Draw(img)
        left, top, right, bottom = d.textbbox((0, 0), text, font=self.font)
        x, y = 2 * PAD, img.height - 2 * PAD - bottom
        d.rectangle([x + left - PAD, y + top - PAD, x + right + PAD, y + bottom + PAD], fill=(0, 0, 0))
        d.text((x, y), text, font=self.font, fill=(255, 200, 0) if src == "fake" else (255, 255, 255))

    def handle_frame(self, img: Image.Image, src: str, t_s: float | None = None) -> bytes:
        """One picture in; writes live/camera.jpg and returns the JPEG bytes."""
        img = self.orient(img.convert("RGB"))
        self.stamp(img, src, time.time() if t_s is None else t_s)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=self.quality)
        data = buf.getvalue()
        write_atomic(self.out, data)
        self.frames += 1
        return data


def fake_frames(cfg: dict, seed: int = 0, person=None):
    """Made-up pictures: grass, a sandy trail, and a person in an orange jacket walking a slow loop
    while the view drifts, as if the pod were moving. person: None to always show them, or a
    function saying whether a person is in view now (local_stack.py ties it to the fake pod)."""
    c = cfg["camera"]
    w, h = int(c["width"]), int(c["height"])
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    ground = np.empty((h, w, 3))
    ground[:] = (62, 92, 48)
    # Everything repeats across the width, so the drifting view has no seam.
    ground += 18 * np.sin(2 * math.pi * 6 * xx / w)[..., None] * np.cos(yy / 53.0)[..., None]
    ground += rng.normal(0, 12, (h, w, 1))
    trail = np.abs(yy - h * (0.55 + 0.18 * np.sin(2 * math.pi * xx / w))) < 0.05 * h
    ground[trail] = (168, 148, 108) + rng.normal(0, 8, (int(trail.sum()), 1))
    base = np.clip(ground, 0, 255).astype(np.uint8)
    s = h / 720  # sizes below are for a 720-pixel-high picture
    i = 0
    while True:
        img = Image.fromarray(np.roll(base, -2 * i, axis=1))  # the view drifts 2 px a picture
        if person is not None and not person():
            yield img, "fake"
            i += 1
            continue
        ph = 2 * math.pi * i / 120  # a minute a loop at 2 pictures a second
        k = 1.0 if person is None else 0.25  # tied to the fake pod: they stay near the middle while it circles
        px, py = w / 2 + k * 0.3 * w * math.sin(ph), h / 2 + k * 0.2 * h * math.sin(2 * ph)
        d = ImageDraw.Draw(img)
        d.ellipse([px - 14 * s, py + 22 * s, px + 18 * s, py + 30 * s], fill=(30, 45, 25))  # shadow
        d.rectangle([px - 8 * s, py - 10 * s, px + 8 * s, py + 12 * s], fill=(235, 110, 20))  # jacket
        d.rectangle([px - 7 * s, py + 12 * s, px + 7 * s, py + 26 * s], fill=(40, 50, 80))  # legs
        d.ellipse([px - 6 * s, py - 23 * s, px + 6 * s, py - 11 * s], fill=(200, 160, 130))  # head
        yield img, "fake"
        i += 1


def camera_frames(cfg: dict):
    """Pictures from the Camera Module 3 through picamera2 (from apt, so the Pi only)."""
    from picamera2 import Picamera2  # Pi only; imported here so the module loads on a laptop

    c = cfg["camera"]
    main = {"size": (int(c["width"]), int(c["height"])), "format": "RGB888"}
    # The sensor would otherwise run at 30+ frames a second while we keep 2 of them.
    controls = {"FrameRate": max(float(c["sensor_fps"]), float(c["fps"]))}
    cam = Picamera2()
    try:
        cam.configure(cam.create_video_configuration(main=main, sensor={"output_size": FULL_VIEW}, controls=controls))
    except Exception as e:  # another camera, or an older picamera2: let it pick the mode
        log.warning("couldn't ask for the %dx%d sensor mode (%s); using the default, which may crop", *FULL_VIEW, e)
        cam.configure(cam.create_video_configuration(main=main, controls=controls))
    cam.start()
    if "AfMode" in cam.camera_controls:
        from libcamera import controls
        cam.set_controls({"AfMode": controls.AfModeEnum.Continuous})
    else:
        log.warning("no autofocus on this camera: focus stays fixed")
    try:
        while True:
            yield cam.capture_image("main"), "camera"
    finally:
        cam.stop()
        cam.close()


def run(camera: Camera, frames, fps: float, limit: int = 0, sleep: bool = True):
    period = 1.0 / fps
    due = time.monotonic()
    for n, (img, src) in enumerate(frames, 1):
        camera.handle_frame(img, src)
        if limit and n >= limit:
            return
        if sleep:
            due += period
            time.sleep(max(0.0, due - time.monotonic()))
            due = max(due, time.monotonic() - period)  # after a stall, don't rush to catch up


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--fake", action="store_true", help="made-up pictures instead of the camera")
    ap.add_argument("--frames", type=int, default=0, help="stop after this many pictures (0 = run until stopped)")
    ap.add_argument("--seed", type=int, default=0, help="--fake: noise seed")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stderr)
    cfg = cfgmod.load(a.config)
    camera = Camera(cfg, cfgmod.resolve(cfg, cfg["http"]["live_dir"]))
    c = cfg["camera"]
    log.info("writing %s, %sx%s at %s fps, from %s", camera.out, c["width"], c["height"], c["fps"],
             "fake pictures" if a.fake else "the camera")
    try:
        frames = fake_frames(cfg, a.seed) if a.fake else camera_frames(cfg)
        run(camera, frames, float(c["fps"]), a.frames)
    except KeyboardInterrupt:
        pass
    except Exception as e:  # no camera, ribbon loose, camera busy: say so, and let systemd retry
        log.error("camera failed after %d pictures: %s (check_hw.py --only camera; is another process using it?)",
                  camera.frames, e)
        sys.exit(1)


if __name__ == "__main__":
    main()
