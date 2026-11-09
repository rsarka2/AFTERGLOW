"""thermal.py: the warm spot, the hot rule in both kinds of frame, the PNG, --fake and the camera reader."""
import itertools
import json
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

import numpy as np
import pytest

from afterglow import config, thermal
from afterglow.contracts import THERMAL, validate
from afterglow.thermal import Thermal, fake_frame, fake_frames

CODE = Path(__file__).resolve().parent.parent
CFG = config.load()


class Collect:
    def __init__(self):
        self.msgs = []

    def publish(self, topic, obj, retain=False):
        self.msgs.append((topic, obj))


def make(tmp_path, **kw):
    cfg = {**CFG, "thermal": {**CFG["thermal"], "rotate": 0, "flip": False, **kw}}  # upright, whatever the pod's mounting
    bus = Collect()
    return Thermal(cfg, bus, tmp_path), bus


def cool(h=192, w=256, seed=0):
    return 17.0 + np.random.default_rng(seed).normal(0, 0.15, (h, w))


def add_blob(a, col, row, temp, sx=4, sy=8):
    yy, xx = np.mgrid[0:a.shape[0], 0:a.shape[1]]
    b = np.exp(-(((xx - col) / sx) ** 2 + ((yy - row) / sy) ** 2))
    return a + (temp - a) * b


def read_png(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h, depth, colour = struct.unpack(">IIBB", data[16:26])
    idat, i = b"", 8
    while i < len(data):
        n, tag = struct.unpack(">I4s", data[i:i + 8])
        if tag == b"IDAT":
            idat += data[i + 8:i + 8 + n]
        i += 12 + n
    raw = zlib.decompress(idat)
    assert (depth, colour) == (8, 2) and len(raw) == h * (1 + 3 * w)
    return w, h


def test_person_is_hot_and_found(tmp_path):
    th, bus = make(tmp_path)
    msg = th.handle_frame(add_blob(cool(), 100, 60, 34.0), True, "fake", t=5)
    assert bus.msgs == [(THERMAL, msg)] and validate(THERMAL, msg) == []
    assert msg["hot"] and 30 <= msg["max_c"] <= 35 and msg["conf"] == 1.0
    assert abs(msg["col"] - 100) <= 1 and abs(msg["row"] - 60) <= 1
    assert (msg["w"], msg["h"], msg["src"], msg["t"]) == (256, 192, "fake", 5)


def test_never_hot_above_max_alt_m(tmp_path):
    th, bus = make(tmp_path, max_alt_m=20.0)
    person = add_blob(cool(), 100, 60, 34.0)
    assert th.handle_frame(person, True, "fake")["hot"]  # no pose yet: any height counts
    th.handle_pose({"t": 1, "x": 0, "y": 0, "z": 25.0})
    msg = th.handle_frame(person, True, "fake")
    assert not msg["hot"] and msg["conf"] == 0.0 and validate(THERMAL, msg) == []
    th.handle_pose({"t": 2, "x": 0, "y": 0, "z": "high"})  # a bad z keeps the last good one
    assert not th.handle_frame(person, True, "fake")["hot"]
    th.handle_pose({"t": 3, "x": 0, "y": 0, "z": 20.0})
    assert th.handle_frame(person, True, "fake")["hot"]


def test_cool_scene_and_lone_hot_pixel_are_not_hot(tmp_path):
    th, _ = make(tmp_path)
    msg = th.handle_frame(cool(), True, "fake")
    assert not msg["hot"] and msg["conf"] == 0.0 and validate(THERMAL, msg) == []
    a = cool()
    a[50, 50] = 36.0  # one noisy pixel: the 3x3 mean keeps it under the band
    assert not th.handle_frame(a, True, "fake")["hot"]


def test_rock_too_hot_for_a_person_is_ignored(tmp_path):
    th, _ = make(tmp_path)
    rock = add_blob(cool(), 220, 30, 60.0, 5, 5)
    assert not th.handle_frame(rock, True, "fake")["hot"]
    both = th.handle_frame(add_blob(rock, 80, 120, 34.0), True, "fake")
    assert both["hot"] and abs(both["col"] - 80) <= 1  # the person, not the warmer rock


def test_lukewarm_spot_needs_the_rise(tmp_path):
    th, _ = make(tmp_path)
    warm_room = 28.0 + np.random.default_rng(1).normal(0, 0.15, (192, 256))  # 31 C is only 3 C above it
    assert not th.handle_frame(add_blob(warm_room, 100, 60, 31.0), True, "fake")["hot"]


def test_picture_only_frames(tmp_path):
    th, _ = make(tmp_path)
    rng = np.random.default_rng(2)
    noise = np.clip(rng.normal(128, 40, (192, 256)), 0, 255)  # automatic contrast stretches plain noise
    msg = th.handle_frame(noise, False, "ht203u")
    assert not msg["hot"] and msg["max_c"] is None and validate(THERMAL, msg) == []
    scene = np.clip(rng.normal(90, 6, (192, 256)), 0, 255)
    msg = th.handle_frame(add_blob(scene, 30, 150, 250.0), False, "ht203u")
    assert msg["hot"] and msg["max_c"] is None and abs(msg["col"] - 30) <= 1


def test_png_is_whole_and_no_temp_file_left(tmp_path):
    th, _ = make(tmp_path)
    th.handle_frame(add_blob(cool(), 100, 60, 34.0), True, "fake")
    assert read_png(tmp_path / "thermal.png") == (256, 192)
    assert [p.name for p in tmp_path.iterdir()] == ["thermal.png"]


def test_rotate_and_flip(tmp_path):
    th, _ = make(tmp_path, rotate=90)
    msg = th.handle_frame(add_blob(cool(), 200, 20, 34.0), True, "fake")
    assert (msg["w"], msg["h"]) == (192, 256) and read_png(tmp_path / "thermal.png") == (192, 256)
    assert abs(msg["col"] - (191 - 20)) <= 1 and abs(msg["row"] - 200) <= 1  # clockwise
    th, _ = make(tmp_path, flip=True)
    assert abs(th.handle_frame(add_blob(cool(), 200, 20, 34.0), True, "fake")["col"] - 55) <= 1
    with pytest.raises(ValueError):
        make(tmp_path, rotate=45)


def test_bad_frames_are_skipped(tmp_path):
    th, bus = make(tmp_path)
    nan = cool()
    nan[3, 3] = np.nan
    for frame in (nan, np.zeros(10), np.zeros((0, 0)), [[1, 2], [3, 4]]):
        assert th.handle_frame(frame, True, "fake") is None
    assert th.bad == 4 and bus.msgs == []


def test_fake_scene_turns_hot_on_and_off(tmp_path):
    th, bus = make(tmp_path)
    frames = fake_frames(seed=3)
    for _ in range(160):  # one loop
        th.handle_frame(*next(frames))
    hot = [m["hot"] for _, m in bus.msgs]
    assert all(validate(t, m) == [] for t, m in bus.msgs)
    assert 0.4 < sum(hot) / len(hot) < 0.95  # the person leaves the frame for part of each loop
    assert all(m["max_c"] < 45 for _, m in bus.msgs if m["hot"])  # never the 48 C rock
    assert fake_frame(0, np.random.default_rng(0)).shape == (192, 256)


def test_command_line_fake_dry_run(tmp_path):
    shutil.copy(CODE / "afterglow.toml", tmp_path / "afterglow.toml")  # live/ lands next to the config
    out = subprocess.run([sys.executable, "-m", "afterglow.thermal", "--config", str(tmp_path / "afterglow.toml"),
                          "--fake", "--dry-run", "--frames", "3"],
                         cwd=CODE, capture_output=True, text=True, timeout=60, check=True).stdout
    lines = out.strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        topic, body = line.split(" ", 1)
        assert topic == THERMAL and validate(topic, json.loads(body)) == []
    sideways = CFG["thermal"]["rotate"] % 180 == 90  # the config's mounting turns the 256x192 frame
    assert read_png(tmp_path / "live" / "thermal.png") == ((192, 256) if sideways else (256, 192))


def test_no_camera_stops_with_a_hint(tmp_path):
    shutil.copy(CODE / "afterglow.toml", tmp_path / "afterglow.toml")
    r = subprocess.run([sys.executable, "-m", "afterglow.thermal", "--config", str(tmp_path / "afterglow.toml")],
                       cwd=CODE, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "no thermal camera" in r.stderr and "--fake" in r.stderr


def test_contract_checks_thermal_fields():
    ok = {"t": 1, "max_c": None, "hot": False}
    assert validate(THERMAL, ok) == []
    assert validate(THERMAL, {**ok, "hot": "yes"}) != []
    assert validate(THERMAL, {**ok, "max_c": "33"}) != []
    assert validate(THERMAL, {**ok, "src": "mlx"}) != []


def ht203u_buffer(raw_top: np.ndarray, sensor_centi_c: int = 2250) -> bytes:
    """A 256x392 YUYV buffer laid out as the HT-203U sends it: raw heat, header, then a picture."""
    from afterglow import thermal
    words = np.full((thermal.RAW_H, thermal.RAW_W), 0x8080, "<u2")
    words[:thermal.TEMP_ROWS] = raw_top
    words[thermal.TEMP_ROWS:thermal.TEMP_ROWS + thermal.HEADER_ROWS] = 0
    words[thermal.TEMP_ROWS, 0] = sensor_centi_c
    return words.tobytes()


def test_parse_raw_matches_the_wall_and_palm_check():
    from afterglow import config, thermal
    th = config.load()["thermal"]
    raw = np.full((192, 256), 5101, np.uint16)  # the wall
    raw[60:140, 80:180] = 5303                   # the palm
    c, sensor = thermal.parse_raw(ht203u_buffer(raw), *thermal.calibration(th))
    assert c.shape == (192, 256) and sensor == 22.5  # the reference sensor temperature: no drift term
    assert abs(float(np.median(c[:40])) - 22.0) < 0.2       # wall at room temperature
    assert 33.5 < float(c[100, 120]) < 35.5                  # palm: skin temperature
    with pytest.raises(ValueError):
        thermal.parse_raw(b"\0" * 1000, 16.0, 296.8)


def test_a_palm_in_view_is_hot():
    from afterglow import config, thermal
    cfg = config.load()
    raw = np.full((192, 256), 5101, np.uint16)
    raw[80:110, 100:130] = 5303
    c, _ = thermal.parse_raw(ht203u_buffer(raw), *thermal.calibration(cfg["thermal"]))
    bus = []

    class B:
        def publish(self, topic, msg, retain=False):
            bus.append((topic, msg))

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        thermal.Thermal(cfg, B(), d).handle_frame(c, True, "ht203u")
    topic, msg = bus[0]
    assert validate(topic, msg) == [] and msg["hot"] is True and msg["src"] == "ht203u"


def test_warm_camera_drift_is_corrected():
    """The same wall: raw 5101 at sensor 22.5 C, 4998 at 24.66 C. Both must read about 22 C."""
    from afterglow import config, thermal
    cal = thermal.calibration(config.load()["thermal"])
    cold, _ = thermal.parse_raw(ht203u_buffer(np.full((192, 256), 5101, np.uint16), 2250), *cal)
    warm, _ = thermal.parse_raw(ht203u_buffer(np.full((192, 256), 4998, np.uint16), 2466), *cal)
    assert abs(float(cold.mean()) - 22.0) < 0.3 and abs(float(warm.mean()) - 22.0) < 0.3


def test_no_v4l2_ctl_stops_with_the_install_hint(monkeypatch):
    from afterglow import config, thermal
    monkeypatch.setattr(thermal, "find_device", lambda th: "/dev/thermal")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(SystemExit, match="v4l-utils"):
        thermal.check_camera(config.load()["thermal"])


GOOD_FMT = ("Format Video Capture:\n\tWidth/Height      : 256/392\n\tPixel Format      : 'YUYV' (YUYV 4:2:2)\n"
            "\tField             : None\n\tBytes per Line    : 512\n\tSize Image        : 200704\n")
WALL = ht203u_buffer(np.full((192, 256), 5101, np.uint16))  # a good frame: a wall at about 22 C


class Stream:
    """A fake v4l2-ctl stream: hands out `frames` (any iterable of buffers), then stays open until killed,
    as a live stream does. The last one in a test parks the daemon reader thread for good."""

    def __init__(self, frames=(), pace_s=0.0):
        self.stdout, self.frames, self.pace_s, self.killed = self, iter(frames), pace_s, threading.Event()

    def read(self, n):
        if not self.killed.is_set():
            buf = next(self.frames, None)
            if buf is not None:
                time.sleep(self.pace_s)
                return buf
            self.killed.wait()
        return b""

    def kill(self):
        self.killed.set()

    def wait(self):
        pass


def fake_v4l2(monkeypatch, streams, fmts=()):
    """Stand in for v4l2-ctl: each stream start takes the next of `streams` (an exception there is raised
    instead); --get-fmt-video answers the next of `fmts`, then GOOD_FMT. Returns the stream commands."""
    streams, fmts, started = list(streams), list(fmts), []

    def popen(cmd, **kw):
        started.append(cmd)
        s = streams.pop(0)
        if isinstance(s, Exception):
            raise s
        return s

    def run(cmd, **kw):
        out = (fmts.pop(0) if fmts else GOOD_FMT) if "--get-fmt-video" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(thermal, "find_device", lambda th: "/dev/thermal")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/v4l2-ctl")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    return started


def first_frame(**kw):
    """The first frame camera_frames yields (in a thread, so a hang fails the test instead of stalling it)."""
    got = []
    t = threading.Thread(target=lambda: got.append(next(thermal.camera_frames(config.load(), retry_s=0.01, **kw))),
                         daemon=True)
    t.start()
    t.join(timeout=5)
    assert got, "no frame from the camera reader"
    celsius, is_c, src = got[0]
    assert is_c and src == "ht203u" and abs(float(np.median(celsius)) - 22.0) < 0.2
    return celsius


def test_camera_reader_survives_a_failure_and_retries(monkeypatch):
    """v4l2-ctl failing (here: missing) once must not kill the reader thread: it retries and frames arrive."""
    started = fake_v4l2(monkeypatch, [FileNotFoundError("v4l2-ctl"), Stream([WALL])])
    first_frame()
    assert len(started) == 2


def test_camera_in_the_wrong_mode_is_not_streamed(monkeypatch, caplog):
    wrong = GOOD_FMT.replace("256/392", "256/192")
    started = fake_v4l2(monkeypatch, [Stream([WALL])], fmts=[wrong])
    first_frame()
    assert len(started) == 1  # not streamed while in 256x192: the retry found the right mode
    assert "256x192 YUYV mode" in caplog.text


def test_format_problem():
    assert thermal.format_problem(GOOD_FMT) is None
    assert "256x192" in thermal.format_problem(GOOD_FMT.replace("256/392", "256/192"))
    assert "MJPG" in thermal.format_problem(GOOD_FMT.replace("'YUYV' (YUYV 4:2:2)", "'MJPG' (Motion-JPEG)"))
    assert thermal.format_problem("") is None  # can't read it: parse_raw's checks still catch a wrong mode


def test_parse_raw_rejects_frames_out_of_step():
    cal = thermal.calibration(config.load()["thermal"])
    row = thermal.RAW_W * 2
    for buf in (WALL[1:] + b"\x80",                     # one byte out: every word is garbage
                WALL[-100 * row:] + WALL[:-100 * row],   # 100 rows out: the picture where raw heat should be
                ht203u_buffer(np.full((192, 256), 5101, np.uint16), 0xFFFF)):  # sensor at 655 C
        with pytest.raises(ValueError, match="out of step"):
            thermal.parse_raw(buf, *cal)
    hot_pixels = np.full((192, 256), 5101, np.uint16)
    hot_pixels[0, :5] = 0xFFFF  # a few dead pixels are not a bad frame
    thermal.parse_raw(ht203u_buffer(hot_pixels), *cal)


def test_stream_out_of_step_is_restarted(monkeypatch):
    shifted = Stream(itertools.repeat(WALL[1:] + b"\x80"), pace_s=0.01)  # out of step for good
    started = fake_v4l2(monkeypatch, [shifted, Stream([WALL])])
    first_frame()
    assert shifted.killed.is_set() and len(started) == 2


def test_stalled_stream_is_restarted(monkeypatch):
    stuck = Stream()  # v4l2-ctl still running, but no frames
    started = fake_v4l2(monkeypatch, [stuck, Stream([WALL])])
    first_frame(stall_s=0.2)
    assert stuck.killed.is_set() and len(started) == 2
