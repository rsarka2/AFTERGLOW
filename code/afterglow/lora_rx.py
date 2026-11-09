"""lora_rx.py (tier 1, Software, laptop): receiver lines -> ag/alert on the laptop's broker.

    python tools/pluto_rx.py | python -m afterglow.lora_rx --stdin          # Pluto SDR (now), publishes to localhost
    python tools/pluto_rx.py | python -m afterglow.lora_rx --stdin --print  # Pluto SDR, print, no broker
    python -m afterglow.lora_rx [--port /dev/cu.usbmodem1101] [--print]      # old path: XIAO #3 + RFM95W on USB
    python -m afterglow.lora_rx --dry-run < lines.txt   # no radio, no broker

The receiver (tools/pluto_rx.py on the Pluto, or XIAO #3's firmware/lora_rx/) is a dumb modem:
it prints every packet it hears as one JSON line, header and all. This module keeps only 16-byte alerts sent from the pod to the laptop, decodes
them with alert.decode and publishes ag/alert. See docs/CONTRACTS.md, "Laptop LoRa radio".
"""
import argparse
import glob
import json
import logging
import sys
import time
from collections import Counter

from . import alert
from . import config as cfgmod
from .bus import Bus, PrintBus
from .contracts import ALERT, now_ms, validate

log = logging.getLogger("lora_rx")

# Keys of the board's {"type":"cfg"} line -> keys of the config's [lora] table
RADIO_KEYS = {"freq": "freq_mhz", "sf": "sf", "bw": "bw_hz", "cr": "cr", "pre": "preamble"}


def radio_mismatch(reply: dict, lcfg: dict) -> list[str]:
    """Settings where the board's cfg line differs from the config. Empty = they match."""
    out = []
    for k, ck in RADIO_KEYS.items():
        if k not in reply:
            out.append(f"{k} missing")
        elif abs(float(reply[k]) - float(lcfg[ck])) > 1e-3:
            out.append(f"{k}: board {reply[k]}, config {lcfg[ck]}")
    return out


def find_port(lcfg: dict) -> str:
    if lcfg.get("rx_port"):
        return lcfg["rx_port"]
    ports = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*"))
    if len(ports) != 1:
        raise SystemExit(f"found {ports or 'no USB serial ports'}: pass --port or set [lora] rx_port")
    return ports[0]


class LoraRx:
    def __init__(self, bus, lcfg: dict):
        self.bus, self.lcfg = bus, lcfg
        self.stats = Counter()
        self.radio_ok = None  # None until the board answers "?", then True or False

    def handle_line(self, line: str, t: int | None = None):
        try:
            d = json.loads(line)
        except ValueError:
            d = None
        if not isinstance(d, dict):
            self.stats["junk"] += 1  # boot chatter from the ESP32
            return
        kind = d.get("type")
        if kind == "cfg":
            bad = radio_mismatch(d, self.lcfg)
            self.radio_ok = not bad
            if bad:
                log.error("radio settings differ from [lora] in the config: %s", "; ".join(bad))
            else:
                log.info("radio settings match the config")
            return
        if kind == "err":
            log.error("board: %s", d.get("msg"))
            return
        if kind != "rx":
            self.stats["junk"] += 1
            return
        self.handle_packet(d.get("hex", ""), d.get("rssi"), d.get("snr"), t)

    def handle_packet(self, hexstr: str, rssi, snr, t: int | None = None):
        try:
            raw = bytes.fromhex(hexstr)
        except (TypeError, ValueError):
            self.stats["bad_hex"] += 1
            return
        if len(raw) < 4:
            self.stats["short"] += 1
            return
        to, frm, payload = raw[0], raw[1], raw[4:]  # RadioHead header: to, from, id, flags
        if to != self.lcfg["laptop_node"] or frm != self.lcfg["pod_node"]:
            self.stats["not_ours"] += 1  # another team's LoRa
            return
        try:
            a = alert.decode(payload)
        except ValueError:
            self.stats["bad_alert"] += 1
            return
        msg = {"t": t or now_ms(), "rssi": rssi, "snr": snr, **a}
        problems = validate(ALERT, msg)
        if problems:
            self.stats["invalid"] += 1
            log.warning("contract: %s", problems)
            return
        self.stats[ALERT] += 1
        self.bus.publish(ALERT, msg)

    def read_lines(self, lines):
        """Read receiver lines until they end (tools/pluto_rx.py piped in). Exits if its settings differ."""
        next_stats = time.monotonic() + 30
        for line in lines:
            self.handle_line(line.strip())
            if self.radio_ok is False:
                raise SystemExit("the receiver's radio settings differ from [lora] in the config")
            if time.monotonic() > next_stats:
                log.info("stats: %s", dict(self.stats))
                next_stats = time.monotonic() + 30
        log.info("receiver closed its output; stats: %s", dict(self.stats))

    def read_port(self, port: str, baud: int):
        """Read XIAO #3 forever, reopening after an unplug. Exits if its radio settings differ."""
        import serial  # pyserial; imported here so --dry-run works without it
        next_stats = time.monotonic() + 30
        while True:
            try:
                with serial.Serial(port, baud, timeout=1) as s:
                    log.info("XIAO #3 open on %s", port)
                    self.radio_ok, asked, opened = None, 0, time.monotonic()
                    while True:
                        now = time.monotonic()
                        # Ask for the settings until it answers (opening the port can reset the board).
                        if self.radio_ok is None and asked < 3 and now - opened > asked * 2:
                            s.write(b"?\n")
                            asked += 1
                        if self.radio_ok is None and asked >= 3 and now - opened > 8:
                            log.warning("board never sent its settings (old firmware?): not checked")
                            self.radio_ok = True
                        if now > next_stats:
                            log.info("stats: %s", dict(self.stats))
                            next_stats = now + 30
                        raw = s.readline()
                        if raw:
                            self.handle_line(raw.decode("utf-8", "replace").strip())
                        if self.radio_ok is False:
                            raise SystemExit("fix firmware/lora_rx/lora_rx.ino or [lora] so they match")
            except Exception as e:  # unplugged, permission, etc.: retry (SystemExit still gets out)
                log.error("%s: %s; retrying in 2 s", port, e)
                time.sleep(2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--port", help="XIAO #3's serial port (default: [lora] rx_port, or the only one found)")
    ap.add_argument("--print", action="store_true", help="read the radio, print instead of publishing")
    ap.add_argument("--stdin", action="store_true", help="read receiver lines from stdin (tools/pluto_rx.py) instead of a USB port")
    ap.add_argument("--dry-run", action="store_true", help="read lines from stdin, print instead of publishing")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = cfgmod.load(a.config)
    lcfg = cfg["lora"]

    if a.dry_run:
        rx = LoraRx(PrintBus(), lcfg)
        for line in sys.stdin:
            rx.handle_line(line.strip())
        log.info("stats: %s", dict(rx.stats))
        return

    port = None if a.stdin else (a.port or find_port(lcfg))
    bus = PrintBus() if a.print else Bus(cfg["mqtt"]["host"], cfg["mqtt"]["port"], "lora_rx")
    rx = LoraRx(bus, lcfg)
    try:
        if a.stdin:
            rx.read_lines(sys.stdin)
        else:
            rx.read_port(port, lcfg["rx_baud"])
    except KeyboardInterrupt:
        pass
    finally:
        bus.close()


if __name__ == "__main__":
    main()
