"""health.py (tier 1, Utility): pod health -> ag/sys, for the dashboard's Pod panel.

    python -m afterglow.health                 # on the Pi: ag/sys every [health] period_s
    python -m afterglow.health --dry-run       # no broker: print one ag/sys and stop

It never opens a radio, a serial port or a camera: those belong to lora_out, bridge, position,
thermal and camera, and two owners would break them. Instead it reads the Pi itself (vcgencmd, the
disk) and watches the bus: a part counts as working when its module published recently.

    cpu_c         the SoC temperature (vcgencmd, else /sys/class/thermal)
    throttled     vcgencmd get_throttled, "0x0" when power and heat are fine
    disk_free_gb  free space where the code (and runs/, live/) live
    lora          true: lora_out is running and not silenced; false: stopped, or lora_off
    gps_fix       true: an ag/pose from the GPS with a fix lately; null: no GPS in the config
    batt_pct      null: the power bank can't be read (CONTRACTS.md)
    omni, wifi, thermal, camera   optional: that part's data arrived within [health] stale_s
"""
import argparse
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import config as cfgmod
from .bus import Bus, PrintBus
from .contracts import OBS_BLE, OBS_WIFI, POSE, SYS, THERMAL, now_ms, validate

log = logging.getLogger("health")
CODE_DIR = Path(__file__).resolve().parent.parent


def run(cmd: list[str], timeout: float = 3.0) -> str | None:
    if shutil.which(cmd[0]) is None:
        return None
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return None


def cpu_temp() -> float | None:
    out = run(["vcgencmd", "measure_temp"])  # temp=61.2'C
    m = re.search(r"([\d.]+)", out or "")
    if m:
        return round(float(m.group(1)), 1)
    try:
        return round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except (OSError, ValueError):
        return None  # a laptop


def throttled() -> str | None:
    m = re.search(r"0x[0-9a-fA-F]+", run(["vcgencmd", "get_throttled"]) or "")
    return m.group(0) if m else None


def lora_out_running() -> bool | None:
    """Is a lora_out process alive? None where pgrep doesn't exist."""
    if shutil.which("pgrep") is None:
        return None
    return subprocess.run(["pgrep", "-f", "afterglow.lora_out"], capture_output=True).returncode == 0


class Health:
    def __init__(self, cfg: dict, bus, clock=time.monotonic):
        hc = cfg["health"]
        self.bus, self.clock = bus, clock
        self.stale_s = float(hc["stale_s"])
        self.gps_in_config = bool(cfg["serial"]["gps"])
        lcfg = cfg["lora"]
        self.off_file = cfgmod.resolve(cfg, lcfg["off_file"]) if lcfg["off_file"] else None
        self.camera_file = cfgmod.resolve(cfg, cfg["http"]["live_dir"]) / "camera.jpg"
        self.disk_path = CODE_DIR
        self.seen: dict[str, float] = {}  # part -> clock() of its latest message
        self.last_fix: float | None = None
        self.lock = threading.Lock()

    def handle_msg(self, topic: str, msg: dict):
        """Every ag/# message (paho's thread: keep it quick)."""
        part = {OBS_BLE: "omni", OBS_WIFI: "wifi", THERMAL: "thermal"}.get(topic)
        now = self.clock()
        with self.lock:
            if part:
                self.seen[part] = now
            if topic == POSE and msg.get("src") == "gps" and (msg.get("fix") or 0) > 0:
                self.last_fix = now

    def fresh(self, part: str, now: float) -> bool:
        t = self.seen.get(part)
        return t is not None and now - t <= self.stale_s

    def sample(self, t: int | None = None) -> dict:
        now = self.clock()
        running = lora_out_running()
        silenced = bool(self.off_file and self.off_file.exists())
        try:
            free_gb = round(shutil.disk_usage(self.disk_path).free / 1e9, 1)
        except OSError:
            free_gb = None
        try:
            camera = time.time() - self.camera_file.stat().st_mtime <= self.stale_s
        except OSError:
            camera = False
        with self.lock:
            gps_fix = None if not self.gps_in_config else (
                self.last_fix is not None and now - self.last_fix <= self.stale_s)
            msg = {"t": t or now_ms(), "cpu_c": cpu_temp(), "throttled": throttled(),
                   "gps_fix": gps_fix, "lora": None if running is None else (running and not silenced),
                   "disk_free_gb": free_gb, "batt_pct": None,
                   "omni": self.fresh("omni", now), "wifi": self.fresh("wifi", now),
                   "thermal": self.fresh("thermal", now), "camera": camera}
        return msg

    def publish(self) -> dict:
        msg = self.sample()
        problems = validate(SYS, msg)
        if problems:
            log.warning("contract: %s", problems)
        else:
            self.bus.publish(SYS, msg)
        return msg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--dry-run", action="store_true", help="no broker: print one ag/sys and stop")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = cfgmod.load(a.config)

    if a.dry_run:
        Health(cfg, PrintBus()).publish()
        return

    bus = Bus(cfg["mqtt"]["host"], cfg["mqtt"]["port"], "health")
    h = Health(cfg, bus)
    bus.subscribe("ag/#", h.handle_msg)
    period = float(cfg["health"]["period_s"])
    last_warn = None
    try:
        while True:
            m = h.publish()
            warn = [k for k in ("omni", "wifi", "thermal", "camera") if m.get(k) is False]
            if m.get("lora") is False:
                warn.append("lora")
            if m.get("throttled") not in (None, "0x0"):
                warn.append(f"throttled {m['throttled']}")
            if warn != last_warn:  # log changes only
                log.info("cpu %s C; %s", m["cpu_c"], "not heard: " + ", ".join(warn) if warn else "all parts heard")
                last_warn = warn
            time.sleep(period)
    except KeyboardInterrupt:
        pass
    finally:
        bus.close()


if __name__ == "__main__":
    main()
