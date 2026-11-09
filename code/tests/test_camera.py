"""camera.py: live/camera.jpg from fake pictures, orientation, the time stamp, and the run loop."""
import io
import subprocess
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from afterglow import config
from afterglow.camera import FULL_VIEW, Camera, camera_frames, fake_frames, run

CODE = Path(__file__).resolve().parent.parent
CFG = config.load()


def make(tmp_path, **kw):
    cfg = {**CFG, "camera": {**CFG["camera"], "rotate": 0, "flip": False, **kw}}  # upright, whatever the pod's mounting
    return Camera(cfg, tmp_path), cfg


def half_red(w=320, h=180):
    a = np.zeros((h, w, 3), np.uint8)
    a[:, : w // 2] = (255, 0, 0)
    return Image.fromarray(a)


def test_writes_a_jpeg_of_the_configured_size(tmp_path):
    cam, cfg = make(tmp_path)
    img, src = next(fake_frames(cfg))
    data = cam.handle_frame(img, src)
    out = Image.open(tmp_path / "camera.jpg")
    assert out.format == "JPEG" and out.size == (cfg["camera"]["width"], cfg["camera"]["height"])
    assert (tmp_path / "camera.jpg").read_bytes() == data
    assert not list(tmp_path.glob("*.tmp"))  # temp file renamed away


def test_stamp_sits_bottom_left_and_leaves_the_rest(tmp_path):
    cam, _ = make(tmp_path)
    grey = Image.new("RGB", (640, 360), (128, 128, 128))
    a = np.asarray(Image.open(io.BytesIO(cam.handle_frame(grey, "camera"))).convert("L"), float)
    assert a[-20:, :60].mean() < 100  # the dark box with the time
    assert abs(a[:200, :].mean() - 128) < 3  # the top of the picture untouched


def test_fake_is_marked_in_yellow(tmp_path):
    cam, _ = make(tmp_path)
    grey = Image.new("RGB", (640, 360), (128, 128, 128))
    a = np.asarray(Image.open(io.BytesIO(cam.handle_frame(grey, "fake"))), int)
    corner = a[-40:, :300]
    yellow = (corner[..., 0] > 200) & (corner[..., 1] > 150) & (corner[..., 2] < 100)
    assert yellow.sum() > 20


@pytest.mark.parametrize("rotate,flip,size,red_at", [
    (0, False, (320, 180), (10, 90)),     # red stays left
    (0, True, (320, 180), (310, 90)),     # mirrored: red on the right
    (180, False, (320, 180), (310, 90)),  # upside down: red on the right
    (90, False, (180, 320), (90, 10)),    # clockwise: the left half becomes the top
])
def test_orientation(tmp_path, rotate, flip, size, red_at):
    cam, _ = make(tmp_path, rotate=rotate, flip=flip)
    out = Image.open(io.BytesIO(cam.handle_frame(half_red(), "camera"))).convert("RGB")
    assert out.size == size
    r, g, b = out.getpixel(red_at)
    assert r > 200 and g < 60 and b < 60


def test_rotate_must_be_a_right_angle(tmp_path):
    with pytest.raises(ValueError):
        make(tmp_path, rotate=45)


def test_fake_person_moves(tmp_path):
    _, cfg = make(tmp_path, width=320, height=180)
    frames = fake_frames(cfg)
    a = np.asarray(next(frames)[0], int)
    for _ in range(10):
        b = np.asarray(next(frames)[0], int)
    orange = lambda x: np.argwhere((x[..., 0] > 200) & (x[..., 1] > 80) & (x[..., 1] < 140) & (x[..., 2] < 60))
    assert len(orange(a)) and len(orange(b))
    assert np.abs(orange(a).mean(0) - orange(b).mean(0)).max() > 3


class StubPicamera2:
    """Just enough of picamera2 to check what camera_frames asks for (the real one is Pi only)."""
    camera_controls = {}  # no autofocus

    def __init__(self):
        self.configs, self.closed = [], False

    def create_video_configuration(self, **kw):
        return kw

    def configure(self, c):
        self.configs.append(c)

    def start(self):
        pass

    def capture_image(self, name):
        return Image.new("RGB", (8, 8))

    def stop(self):
        pass

    def close(self):
        self.closed = True


@pytest.mark.parametrize("fps,sensor_fps,want", [(2, 10, 10.0), (15, 10, 15.0)])
def test_sensor_frame_rate_is_capped(tmp_path, monkeypatch, fps, sensor_fps, want):
    stub = StubPicamera2()
    monkeypatch.setitem(sys.modules, "picamera2", types.SimpleNamespace(Picamera2=lambda: stub))
    _, cfg = make(tmp_path, fps=fps, sensor_fps=sensor_fps)
    frames = camera_frames(cfg)
    img, src = next(frames)
    frames.close()
    (c,) = stub.configs
    assert c["controls"] == {"FrameRate": want} and c["sensor"] == {"output_size": FULL_VIEW}
    assert src == "camera" and stub.closed


def test_run_stops_after_limit(tmp_path):
    cam, cfg = make(tmp_path, width=320, height=180)
    run(cam, fake_frames(cfg), fps=2, limit=3, sleep=False)
    assert cam.frames == 3 and (tmp_path / "camera.jpg").exists()


def test_cli_fake_writes_frames(tmp_path):
    cfg_text = (CODE / "afterglow.toml").read_text().replace('live_dir = "live"', f'live_dir = "{tmp_path}"')
    cfg_file = tmp_path / "cfg.toml"
    cfg_file.write_text(cfg_text)
    r = subprocess.run([sys.executable, "-m", "afterglow.camera", "--config", str(cfg_file), "--fake", "--frames", "2"],
                       cwd=CODE, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    sideways = CFG["camera"]["rotate"] % 180 == 90  # the config's mounting turns the picture
    assert Image.open(tmp_path / "camera.jpg").size == ((720, 1280) if sideways else (1280, 720))
