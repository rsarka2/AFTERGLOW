"""bridge.py (tier 1, Utility): ESP32 serial lines -> MQTT, with addresses hashed.

    python -m afterglow.bridge                 # on the Pi: ttyAMA0 (OMNI) + ttyAMA3 (WIFI)
    python -m afterglow.bridge --dry-run < lines.txt   # no hardware, no broker: prints topics

Raw addresses never leave this file. See docs/CONTRACTS.md, "Serial lines".
"""
import argparse
import hashlib
import json
import logging
import secrets
import sys
import threading
from collections import Counter

from . import config as cfgmod
from .bus import Bus, PrintBus
from .contracts import CTRL, OBS_BLE, OBS_WIFI, SOS, now_ms, validate

log = logging.getLogger("bridge")


class Hasher:
    def __init__(self, salt: str = ""):
        self.new_salt(salt)

    def new_salt(self, salt: str = ""):
        self.salt = salt or secrets.token_hex(8)

    def __call__(self, addr: str) -> str:
        return hashlib.sha256((self.salt + addr.lower()).encode()).hexdigest()[:8]


def convert(line: str, src: str, hasher: Hasher, t: int | None = None):
    """One serial line -> (topic, message), or None to drop it."""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    t = t or now_ms()
    kind = d.get("type")
    if kind == "ble" and "addr" in d:
        return OBS_BLE, {"t": t, "src": src, "id": hasher(d["addr"]), "cls": d.get("cls", "other"), "rssi": d.get("rssi")}
    if kind == "wifi" and "addr" in d:
        return OBS_WIFI, {"t": t, "id": hasher(d["addr"]), "rand": bool(d.get("rand")), "rssi": d.get("rssi"), "ch": d.get("ch")}
    if kind == "sos":
        try:
            count = int(d.get("count", 0))
        except (TypeError, ValueError):
            return None
        return SOS, {"t": t, "hurt": bool(d.get("hurt")), "walk": bool(d.get("walk")),
                     "count": count, "rssi": d.get("rssi")}
    return None


class Bridge:
    def __init__(self, bus, hasher: Hasher):
        self.bus, self.hasher = bus, hasher
        self.stats = Counter()
        self.writers = {}  # name -> serial port, for commands back to the boards

    def handle(self, line: str, src: str):
        out = convert(line, src, self.hasher)
        if out is None:
            self.stats["dropped"] += 1
            return
        topic, msg = out
        problems = validate(topic, msg)
        if problems:
            self.stats["invalid"] += 1
            log.warning("contract: %s", problems)
            return
        self.stats[topic] += 1
        self.bus.publish(topic, msg)

    def on_ctrl(self, topic, msg):
        cmd = msg.get("cmd")
        if cmd == "new_search":
            self.hasher.new_salt()
            log.info("new search: new salt")
        elif cmd in ("sos_on", "sos_off") and "wifi" in self.writers:
            self.writers["wifi"].write((json.dumps({"cmd": cmd}, separators=(",", ":")) + "\n").encode())
            log.info("sent %s to WIFI board", cmd)

    def read_port(self, name: str, port: str, baud: int):
        import serial  # pyserial; imported here so --dry-run works without it
        while True:
            try:
                with serial.Serial(port, baud, timeout=1) as s:
                    self.writers[name] = s
                    log.info("%s open on %s at %d", name, port, baud)
                    while True:
                        raw = s.readline()
                        if raw:
                            self.handle(raw.decode("utf-8", "replace").strip(), name)
            except Exception as e:  # unplugged, permission, etc.: retry
                self.writers.pop(name, None)
                log.error("%s on %s: %s; retrying in 2 s", name, port, e)
                threading.Event().wait(2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--dry-run", action="store_true", help="read lines from stdin, print instead of publishing")
    ap.add_argument("--src", default="omni", help="with --dry-run: label for stdin lines")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = cfgmod.load(a.config)
    hasher = Hasher(cfg["mission"].get("salt", ""))

    if a.dry_run:
        br = Bridge(PrintBus(), hasher)
        for line in sys.stdin:
            br.handle(line.strip(), a.src)
        log.info("stats: %s", dict(br.stats))
        return

    bus = Bus(cfg["mqtt"]["host"], cfg["mqtt"]["port"], "bridge")
    br = Bridge(bus, hasher)
    bus.subscribe(CTRL, br.on_ctrl)
    baud = cfg["serial"]["esp_baud"]
    for name in ("omni", "wifi"):
        threading.Thread(target=br.read_port, args=(name, cfg["serial"][name], baud), daemon=True).start()
    try:
        while True:
            threading.Event().wait(30)
            log.info("stats: %s", dict(br.stats))
    except KeyboardInterrupt:
        bus.close()


if __name__ == "__main__":
    main()
