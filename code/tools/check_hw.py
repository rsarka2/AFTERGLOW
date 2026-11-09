#!/usr/bin/env python3
"""Check every piece of pod hardware from the Pi, one line each: PASS, FAIL or SKIP.

    python3 tools/check_hw.py              # everything (about 30 s)
    python3 tools/check_hw.py --only lora omni
    python3 tools/check_hw.py --seconds 10 # listen longer on the serial ports

Run it after every hardware change. The bench bring-up stage passes when every
part you've connected shows PASS. Ports and addresses come from afterglow.toml.
Needs pyserial; the lora check also needs Blinka and adafruit-circuitpython-rfm9x
(requirements-pi.txt), and sends one heartbeat alert to the laptop radio.
"""
import argparse
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from afterglow import config as cfgmod  # noqa: E402

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def run(cmd, timeout=15):
    if shutil.which(cmd[0]) is None:
        return None, f"{cmd[0]} not installed"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return None, "timed out"


MAC = re.compile(r"\b[0-9a-fA-F]{2}([:-][0-9a-fA-F]{2}){5}\b")


def redact(line):
    """Privacy rule: never show a raw Bluetooth or Wi-Fi address on screen."""
    return MAC.sub("xx:xx:xx:xx:xx:xx", line)


def listen(port, baud, seconds, want):
    """Read lines from a serial port; count lines for which want(line) is True."""
    try:
        import serial
    except ImportError:
        return "SKIP", "pip install pyserial"
    if not os.path.exists(port):
        return "FAIL", f"{port} missing (config.txt overlay? reboot?)"
    good = total = 0
    sample = ""
    try:
        with serial.Serial(port, baud, timeout=0.5) as s:
            end = time.time() + seconds
            while time.time() < end:
                raw = s.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace").strip()
                total += 1
                if want(line):
                    good += 1
                    sample = sample or redact(line)[:90]
    except Exception as e:
        return "FAIL", str(e)
    if good:
        return "PASS", f"{good} good lines of {total} in {seconds} s, e.g. {sample}"
    if total:
        return "FAIL", f"{total} lines but none valid: wrong baud, or TX/RX swapped?"
    return "FAIL", "silent: check TX→RX crossing, ground, power and the firmware (Serial1 on D9/D10?)"


def is_json_type(kind):
    def f(line):
        try:
            return json.loads(line).get("type") == kind
        except Exception:
            return False
    return f


@check("config")
def c_config(a):
    path = "/boot/firmware/config.txt"
    if not os.path.exists(path):
        return "SKIP", "not on a Pi"
    text = open(path).read()
    need = ["dtparam=i2c_arm=on", "dtparam=uart0=on", "dtoverlay=uart3-pi5", "dtoverlay=uart2-pi5",
            "dtoverlay=spi1-1cs,cs0_pin=16", "camera_auto_detect=1", "dtparam=pciex1_gen=3"]
    missing = [n for n in need if n not in text]
    if missing:
        return "FAIL", "missing: " + ", ".join(missing)
    if "dtparam=spi=on" in (l.strip() for l in text.splitlines()):
        return "FAIL", "remove dtparam=spi=on: SPI0 takes GPIO8/9 from WIFI's UART3"
    return "PASS", "all lines present"


@check("power")
def c_power(a):
    rc, out = run(["vcgencmd", "get_throttled"])
    if rc is None:
        return "SKIP", out
    m = re.search(r"0x[0-9a-fA-F]+", out)
    val = m.group(0) if m else out
    _, temp = run(["vcgencmd", "measure_temp"])
    if val == "0x0":
        return "PASS", f"throttled=0x0, {temp}"
    return "FAIL", f"throttled={val} (under-voltage or heat; see docs/HARDWARE.md, Power), {temp}"


@check("serial_ports")
def c_ports(a):
    missing = [p for p in ("/dev/ttyAMA0", "/dev/ttyAMA2", "/dev/ttyAMA3") if not os.path.exists(p)]
    return ("FAIL", "missing " + ", ".join(missing)) if missing else ("PASS", "ttyAMA0, 2, 3 present")


@check("omni")
def c_omni(a):
    s = a.cfg["serial"]
    return listen(s["omni"], s["esp_baud"], a.seconds, is_json_type("ble"))


@check("wifi")
def c_wifi(a):
    s = a.cfg["serial"]
    return listen(s["wifi"], s["esp_baud"], a.seconds, is_json_type("wifi"))


@check("lora")
def c_lora(a):
    try:
        from afterglow import alert, radio
        rfm = radio.open_pod_radio(a.cfg["lora"])
    except ImportError as e:
        return "SKIP", f"{e.name} not installed (pip install -r requirements-pi.txt)"
    except Exception as e:
        return "FAIL", f"{e} (SPI1 wiring? dtoverlay=spi1-1cs,cs0_pin=16 and a reboot?)"
    if rfm.send(alert.encode({"x": 0, "y": 0}, kind=alert.TYPE_HEARTBEAT)):
        return "PASS", "RFM95W found; sent a heartbeat (lora_rx.py on the laptop should show it)"
    return "FAIL", "RFM95W found but the send timed out (power? antenna on ANT?)"


@check("gps")
def c_gps(a):
    port = a.cfg["serial"]["gps"]
    if not port:
        return "SKIP", 'no GPS receiver yet ([serial] gps = ""): position uses the taped waypoints'
    fix_seen = []

    def nmea(line):
        if line.startswith("$G") and "*" in line:
            if line[3:6] == "GGA":
                parts = line.split(",")
                if len(parts) > 6 and parts[6] not in ("", "0"):
                    fix_seen.append(line)
            return True
        return False
    last = ("FAIL", "no NMEA at 38400, 9600 or 115200")
    for baud in (38400, 9600, 115200):
        st, msg = listen(port, baud, max(3, a.seconds // 2), nmea)
        if st == "PASS":
            fix = "has a fix" if fix_seen else "no fix yet (take it outside or to a window)"
            return "PASS", f"NMEA at {baud} baud, {fix}. Set gps_baud = {baud} in afterglow.toml"
        last = (st, msg)
        if "missing" in msg:
            break
    return last


@check("i2c")
def c_i2c(a):
    rc, out = run(["i2cdetect", "-y", "1"])
    if rc is None:
        return "SKIP", out + " (sudo apt install i2c-tools)"
    found = set(re.findall(r"\b([0-9a-f]{2})\b", "\n".join(l.split(":", 1)[1] for l in out.splitlines() if ":" in l)))
    imu = f"{a.cfg['i2c']['imu_addr']:02x}"
    if imu not in found:
        return "FAIL", f"IMU (BNO086) not at 0x{imu} (found {sorted(found) or 'nothing'}): 3.3 V, SDA/SCL, ADR jumper?"
    return "PASS", f"IMU at 0x{imu}"  # the thermal camera is USB now (HT-203U), not I2C


@check("thermal")
def c_thermal(a):
    """Grab one raw frame from the HT-203U and report the scene's temperature range."""
    from afterglow import thermal
    th = a.cfg["thermal"]
    try:
        dev = thermal.find_device(th)
    except SystemExit as e:
        return "FAIL", str(e)
    if shutil.which("v4l2-ctl") is None:
        return "FAIL", "v4l2-ctl not installed, so thermal.py can't read the camera (sudo apt install -y v4l-utils)"
    run(["v4l2-ctl", "-d", dev, f"--set-fmt-video=width={thermal.RAW_W},height={thermal.RAW_H},pixelformat=YUYV"])
    try:
        p = subprocess.run(["v4l2-ctl", "-d", dev, "--stream-mmap", "--stream-skip=3", "--stream-count=1", "--stream-to=-"],
                           capture_output=True, timeout=15)
        c, sensor_c = thermal.parse_raw(p.stdout, *thermal.calibration(th))
    except (subprocess.TimeoutExpired, ValueError) as e:
        return "FAIL", f"{dev}: {e} (another program using it? thermal.py running?)"
    return "PASS", f"{dev}: scene {c.min():.1f} to {c.max():.1f} C, median {float(np.median(c)):.1f} C; sensor {sensor_c:.1f} C"


@check("camera")
def c_camera(a):
    rc, out = run(["rpicam-hello", "--list-cameras"], timeout=20)
    if rc is None:
        return "SKIP", out
    if "imx708" in out.lower():
        return "PASS", "Camera Module 3 (imx708) found"
    return "FAIL", "no camera: ribbon seated, contacts the right way, camera_auto_detect=1? " + out[:80]


@check("hailo")
def c_hailo(a):
    rc, out = run(["hailortcli", "fw-control", "identify"], timeout=20)
    if rc is None:
        return "SKIP", out + " (sudo apt install hailo-all)"
    if rc == 0 and "hailo" in out.lower():
        line = next((l for l in out.splitlines() if "Board Name" in l or "Device Architecture" in l), "found")
        return "PASS", line.strip()
    return "FAIL", "AI HAT+ not found: PCIe ribbon seated? dtparam=pciex1_gen=3? " + out[:80]


@check("usb_serial")
def c_usb(a):
    byid = sorted(glob.glob("/dev/serial/by-id/*"))
    radar = a.cfg["serial"]["radar"]
    if not byid:
        return "FAIL", "no USB serial devices: radar plugged in (the only USB device)?"
    names = ", ".join(os.path.basename(p) for p in byid)
    note = f"; {radar} present" if os.path.exists(radar) else f"; no {radar}: add the udev rule (pi/99-afterglow.rules)"
    return "PASS", f"{len(byid)} found: {names}{note}"


@check("mqtt")
def c_mqtt(a):
    try:
        with socket.create_connection(("localhost", 1883), timeout=2):
            return "PASS", "Mosquitto listening on 1883"
    except OSError as e:
        return "FAIL", f"no broker on localhost:1883 ({e}); sudo systemctl enable --now mosquitto"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="names: " + " ".join(n for n, _ in CHECKS))
    ap.add_argument("--seconds", type=int, default=5, help="how long to listen on each serial port")
    ap.add_argument("--config")
    a = ap.parse_args()
    a.cfg = cfgmod.load(a.config)
    width = max(len(n) for n, _ in CHECKS)
    results = []
    for name, fn in CHECKS:
        if a.only and name not in a.only:
            continue
        try:
            st, msg = fn(a)
        except Exception as e:  # a check must never crash the run
            st, msg = "FAIL", f"error: {e}"
        results.append(st)
        print(f"{st:4}  {name:<{width}}  {msg}", flush=True)
    print(f"\n{results.count('PASS')} pass, {results.count('FAIL')} fail, {results.count('SKIP')} skipped")
    raise SystemExit(1 if "FAIL" in results else 0)


if __name__ == "__main__":
    main()
