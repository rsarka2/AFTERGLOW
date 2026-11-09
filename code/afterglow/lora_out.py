"""lora_out.py (tier 1, Utility; written by Software): ag/target -> 16-byte LoRa alerts from the pod's RFM95W.

    python -m afterglow.lora_out                  # on the Pi: the RFM95W on SPI1 (afterglow/radio.py), local broker
    python -m afterglow.lora_out --print          # read the bus, print the receiver lines instead of sending
    python tools/fake_bus.py --print --duration 120 | python -m afterglow.lora_out --dry-run \\
        | python -m afterglow.lora_rx --dry-run | python -m afterglow.cot_out --dry-run   # no radio, no broker

A plain encoder of geofence's pins (docs/CONTRACTS.md, "LoRa alert"):
- every live pin goes out every [lora] repeat_s (5 s), and at once when it's new or its state, prio, flags,
  SOS answer or alert type change. There are no acks: the repeats cover a lost packet. Priority 1 goes first.
  On a channel too busy for every pin (a dozen pins at SF10), whatever has been due longest goes next, so
  every pin and the heartbeat slow down instead of the low-priority ones going silent;
- a cleared pin stops (ATAK lets its marker go stale). A confirmed pin goes out as type 3, an SOS-only pin
  (rule "sos") as type 2, everything else as type 1;
- last_s counts up between repeats, and the battery comes from ag/sys batt_pct (null -> 255, unknown);
- a heartbeat (type 4, pin 0) every repeat_s carries the pod's latest ag/pose x, y for ATAK's pod marker;
- ag/ctrl lora_off stops every packet, heartbeat included, and lora_on starts them again. The off state is
  remembered in [lora] off_file, so a restart stays silent: the pilot's control link always wins. ag/ctrl
  isn't retained, so a lora_off sent while lora_out isn't running is lost: to start silent, create the
  off_file by hand (any content).

ag/target isn't retained, so at start-up it reloads geofence's saved pins ([rules] pins_file), or pins that
geofence no longer republishes (left from before a new_search, or a track gone quiet) would never go out again.
After `geofence --fresh`, restart lora_out too, or it keeps sending the old pins.

It owns the radio while it runs: stop it before `tools/check_hw.py --only lora`. Its clock is the wall clock
(the Pi stamps every t), except with --dry-run, where it follows the messages' t.
"""
import argparse
import json
import logging
import math
import queue
import sys
import time
from collections import Counter
from pathlib import Path

from . import alert
from . import config as cfgmod
from .bus import Bus
from .contracts import CTRL, POSE, SYS, TARGET, validate

log = logging.getLogger("lora_out")

URGENT_GAP_S = 1.0  # a changed pin goes out at once, but at most this often, so a flapping state can't flood the channel
RETRY_OPEN_S = 10.0  # the radio didn't answer: try to open it again this often
HEARTBEAT = None  # due()'s name for the heartbeat: no pin can be None


def pin_num(pin) -> int:
    digits = "".join(ch for ch in str(pin) if ch.isdigit())
    return int(digits) if digits else 0


def kind_of(p: dict) -> int:
    """Alert type: a confirmed person outranks everything, even an SOS-only pin."""
    if p.get("status") == "confirmed" or p.get("state") == "confirmed":
        return alert.TYPE_CONFIRMED
    if p.get("rule") == "sos":
        return alert.TYPE_SOS
    return alert.TYPE_TRACK


def news(p: dict) -> tuple:
    """What, when it changes, is worth a packet at once. Moves and status clicks wait for the repeat."""
    flags, sos = p.get("flags") or {}, p.get("sos") or {}
    return (kind_of(p), p.get("state"), p.get("prio"), tuple(bool(flags.get(k)) for k in alert.FLAG_BITS),
            sos.get("hurt"), sos.get("walk"), sos.get("count"))


class LoraOut:
    def __init__(self, cfg: dict, radio, clock=time.time, off_file: Path | None = None):
        lc = cfg["lora"]
        self.radio, self.clock = radio, clock  # radio(pkt: bytes) -> bool, True once sent
        self.repeat_s, self.version = lc["repeat_s"], lc["alert_version"]
        if self.version not in alert.SCALE:
            raise SystemExit(f"[lora] alert_version must be one of {sorted(alert.SCALE)}, not {self.version!r}")
        self.off_file = off_file   # None: not remembered (tests, --dry-run)
        self.off = bool(off_file and off_file.exists())
        self.pins = {}             # "P3" -> the latest ag/target, cleared pins left out
        self.sent = {}             # "P3" -> clock time it last went out
        self.sent_news = {}        # "P3" -> news() as last sent
        self.pose = (0.0, 0.0)     # the heartbeat's x, y: 0, 0 before the first ag/pose
        self.battery = None        # ag/sys batt_pct
        self.hb_sent = None        # clock time of the last heartbeat
        self.waiting = {}          # "P3" or HEARTBEAT -> clock time it came due and is still waiting
        self.stats = Counter()
        if self.off:
            log.warning("LoRa is OFF: lora_off is remembered in %s. Send ag/ctrl lora_on (or delete the file) "
                        "to send again", off_file)

    # ---------- input ----------

    def feed(self, topic, msg):
        if topic == TARGET:
            self.handle_target(msg)
        elif topic == POSE:
            self.handle_pose(msg)
        elif topic == SYS:
            self.handle_sys(msg)
        elif topic == CTRL:
            self.on_ctrl(topic, msg)

    def handle_target(self, m):
        if validate(TARGET, m):
            self.stats["bad_target"] += 1
            return
        pin = m["pin"]
        if m["status"] == "cleared":
            if self.pins.pop(pin, None) is not None:
                log.info("%s cleared: no longer sent", pin)
            self.sent.pop(pin, None)
            self.sent_news.pop(pin, None)
            return
        if pin not in self.pins:
            log.info("%s: new pin, sending every %s s", pin, self.repeat_s)
        self.pins[pin] = m

    def handle_pose(self, m):
        try:
            x, y = float(m["x"]), float(m["y"])
        except (KeyError, TypeError, ValueError):
            x = y = math.nan
        if not (math.isfinite(x) and math.isfinite(y)):
            self.stats["bad_pose"] += 1
            return
        self.pose = (x, y)

    def handle_sys(self, m):
        b = m.get("batt_pct")
        self.battery = b if isinstance(b, (int, float)) and not isinstance(b, bool) else None

    def on_ctrl(self, topic, msg):
        cmd = msg.get("cmd")
        if cmd == "lora_off" and not self.off:
            self.set_off(True)
            log.warning("lora_off: LoRa silent until lora_on")
        elif cmd == "lora_on" and self.off:
            self.set_off(False)
            self.sent.clear()  # everything goes out again at once
            self.hb_sent = None
            self.waiting.clear()  # time spent off isn't time spent waiting for the channel
            log.info("lora_on: sending %d pins", len(self.pins))

    def set_off(self, off: bool):
        self.off = off
        if self.off_file is None:
            return
        try:
            if off:
                self.off_file.parent.mkdir(parents=True, exist_ok=True)
                self.off_file.write_text(json.dumps({"off_since": time.strftime("%Y-%m-%d %H:%M:%S")}) + "\n")
            else:
                self.off_file.unlink(missing_ok=True)
        except OSError as e:
            log.warning("couldn't remember lora_%s in %s: %s", "off" if off else "on", self.off_file, e)

    def restore(self, pins_file: Path, max_age_s: float):
        """Geofence's saved pins, if recent: ag/target isn't retained, and some pins are never republished."""
        if not pins_file.exists():
            return
        if time.time() - pins_file.stat().st_mtime > max_age_s:
            log.info("geofence's saved pins are too old to send: waiting for ag/target")
            return
        try:
            pins = json.loads(pins_file.read_text())["pins"].values()
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning("couldn't read geofence's saved pins (%s): waiting for ag/target", e)
            return
        for p in pins:
            if isinstance(p, dict):
                self.handle_target(p)
        log.info("loaded %d live pins from %s", len(self.pins), pins_file)

    # ---------- output ----------

    def due(self, now):
        """What to send now, most urgent first; HEARTBEAT stands for the heartbeat:
        1. priority 1 pins;
        2. anything that has been due for a whole repeat_s without going out, longest first. Only a channel
           too busy for every pin gets here, and then it shares the airtime, instead of the heartbeat and
           priority 4 never going out;
        3. the rest by priority, changed pins first, then by pin number; the heartbeat last."""
        found = []  # (pin or HEARTBEAT, prio, changed, pin number)
        for pin, p in self.pins.items():
            last = self.sent.get(pin)
            changed = news(p) != self.sent_news.get(pin)
            if last is None or now - last >= self.repeat_s or (changed and now - last >= URGENT_GAP_S):
                prio = p.get("prio") if isinstance(p.get("prio"), int) else 4
                found.append((pin, prio, changed, pin_num(pin)))
        if self.hb_sent is None or now - self.hb_sent >= self.repeat_s:
            found.append((HEARTBEAT, 5, False, 0))
        # since when each has been due: what went out (or was cleared) since the last call drops out
        self.waiting = {f[0]: self.waiting.get(f[0], now) for f in found}
        found.sort(key=lambda f: self.rank(now, self.waiting[f[0]], *f[1:]))
        return [f[0] for f in found]

    def rank(self, now, since, prio, changed, num):
        starved = now - since >= self.repeat_s
        tier = 0 if prio == 1 else 1 if starved else 2
        return (tier, since if tier == 1 else 0.0, prio, not changed, num)

    def tick(self) -> bool:
        """Send the most urgent packet that's due. One per call, so a lora_off or a new pin gets in between
        (a packet takes 50 ms at SF7, 370 ms at SF10). Returns True if it sent one."""
        if self.off:
            return False
        now = self.clock()
        for what in self.due(now):
            if what is HEARTBEAT:
                self.send_heartbeat(now)
                return True
            if self.send_pin(what, now):
                return True
        return False

    def send_pin(self, pin, now) -> bool:
        p = self.pins[pin]
        m = dict(p, battery=self.battery)
        t = p.get("t")
        age = max(0.0, now - t / 1000) if isinstance(t, (int, float)) else 0.0
        try:
            m["last_s"] = float(p.get("last_s") or 0) + age  # still counting between geofence's updates
            pkt = alert.encode(m, version=self.version, kind=kind_of(p))
        except (TypeError, ValueError, KeyError) as e:
            log.warning("%s can't be encoded (%s): dropped until geofence sends it again", pin, e)
            self.stats["bad_target"] += 1
            del self.pins[pin]
            return False
        self.transmit(pkt)
        self.sent[pin], self.sent_news[pin] = now, news(p)
        self.stats[f"kind{kind_of(p)}"] += 1
        return True

    def send_heartbeat(self, now):
        x, y = self.pose
        self.transmit(alert.encode({"x": x, "y": y, "battery": self.battery}, version=self.version,
                                   kind=alert.TYPE_HEARTBEAT))
        self.hb_sent = now
        self.stats["heartbeat"] += 1

    def transmit(self, pkt: bytes):
        """A failed send still counts as sent: the next repeat is the retry."""
        ok = self.radio(pkt)
        self.stats["sent" if ok else "send_failed"] += 1
        n = self.stats["send_failed"]
        if not ok and (n == 1 or n % 100 == 0):  # don't flood the log
            log.warning("LoRa send failed (%d so far): radio missing, unpowered or no antenna?", n)


class PodRadio:
    """The RFM95W on SPI1. Opens lazily and again after a failure, so an unplugged radio doesn't stop the module."""

    def __init__(self, lcfg: dict):
        self.lcfg, self.rfm, self.next_try = lcfg, None, 0.0
        self.open()  # fail fast on a laptop (no Blinka), but not on a radio that's just slow to answer

    def open(self):
        from . import radio
        try:
            self.rfm = radio.open_pod_radio(self.lcfg)
            log.info("RFM95W open: %.1f MHz, SF%d, %d dBm", self.lcfg["freq_mhz"], self.lcfg["sf"],
                     self.lcfg["tx_power_dbm"])
        except ImportError as e:
            raise SystemExit(f"{e.name} not installed: this runs on the Pi (pip install -r requirements-pi.txt); "
                             "on a laptop use --print or --dry-run")
        except Exception as e:  # not wired, SPI1 off, no reboot after config.txt
            self.rfm, self.next_try = None, time.monotonic() + RETRY_OPEN_S
            log.error("no RFM95W (%s); retrying every %.0f s. Check SPI1 wiring and dtoverlay=spi1-1cs",
                      e, RETRY_OPEN_S)

    def __call__(self, pkt: bytes) -> bool:
        if self.rfm is None:
            if time.monotonic() < self.next_try:
                return False
            self.open()
            if self.rfm is None:
                return False
        try:
            return bool(self.rfm.send(pkt))  # the library adds the header: to laptop_node, from pod_node
        except Exception as e:
            log.error("RFM95W send raised %s: reopening", e)
            self.rfm, self.next_try = None, time.monotonic() + RETRY_OPEN_S
            return False


class PrintRadio:
    """Stand-in for --print and --dry-run: prints the line the receiver would, so lora_rx --dry-run can read it.
    rssi and snr are 0, so nobody mistakes it for a real link."""

    def __init__(self, lcfg: dict):
        self.to, self.frm, self.n = lcfg["laptop_node"], lcfg["pod_node"], 0

    def __call__(self, pkt: bytes) -> bool:
        header = bytes([self.to, self.frm, self.n & 0xFF, 0])  # RadioHead: to, from, id, flags
        self.n += 1
        print(json.dumps({"type": "rx", "rssi": 0, "snr": 0, "hex": (header + pkt).hex()}), flush=True)
        return True


class BusClock:
    """--dry-run: time is the newest t seen, so fake and sped-up data repeat on their own schedule."""

    def __init__(self):
        self.t = 0.0

    def see(self, msg):
        t = msg.get("t")
        if isinstance(t, (int, float)) and not isinstance(t, bool) and t / 1000 > self.t:
            self.t = t / 1000

    def __call__(self):
        return self.t


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--broker", help="MQTT host (default: the config's [mqtt] host)")
    ap.add_argument("--print", action="store_true", help="read the bus, print the receiver lines instead of sending")
    ap.add_argument("--dry-run", action="store_true",
                    help="read `mosquitto_sub -v` lines from stdin, print the receiver lines (no radio, no broker)")
    a = ap.parse_args()
    # Logs go to stderr, so the printed lines can be piped into lora_rx --dry-run
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stderr)
    cfg = cfgmod.load(a.config)
    lcfg, rules = cfg["lora"], cfg["rules"]

    if a.dry_run:
        clock = BusClock()
        lo = LoraOut(cfg, PrintRadio(lcfg), clock=clock)
        for line in sys.stdin:
            topic, _, payload = line.strip().partition(" ")
            try:
                msg = json.loads(payload)
            except ValueError:
                continue
            if isinstance(msg, dict):
                clock.see(msg)
                lo.feed(topic, msg)
                while lo.tick():
                    pass
        log.info("%d live pins, stats: %s", len(lo.pins), dict(lo.stats))
        return

    radio = PrintRadio(lcfg) if a.print else PodRadio(lcfg)
    off_file = cfgmod.resolve(cfg, lcfg["off_file"]) if lcfg["off_file"] and not a.print else None
    lo = LoraOut(cfg, radio, off_file=off_file)
    if rules["pins_file"]:
        lo.restore(cfgmod.resolve(cfg, rules["pins_file"]), rules["pins_max_age_min"] * 60)
    host = a.broker or cfg["mqtt"]["host"]
    bus = Bus(host, cfg["mqtt"]["port"], "lora_out")
    inbox = queue.Queue()  # handlers only queue, so paho's thread stays fast; this loop does the sending
    for topic in (TARGET, POSE, SYS, CTRL):
        bus.subscribe(topic, lambda topic, msg: inbox.put((topic, msg)))
    log.info("ag/target from %s -> %s; alert v%d, every %s s", host,
             "stdout" if a.print else f"RFM95W {lcfg['freq_mhz']} MHz", lo.version, lo.repeat_s)
    last_log, busy = time.monotonic(), False
    try:
        while True:
            try:
                item = inbox.get(block=not busy, timeout=0.1)  # just sent: only what's already waiting
                while True:  # take in everything waiting (a lora_off above all) before the next packet
                    try:
                        lo.feed(*item)
                    except Exception:  # one bad message mustn't stop the alerts
                        log.exception("failed on a message")
                    item = inbox.get_nowait()
            except queue.Empty:
                pass
            try:
                busy = lo.tick()
            except Exception:
                log.exception("failed to send")
                busy = False
            if time.monotonic() - last_log > 30:
                last_log = time.monotonic()
                log.info("%d live pins%s, stats: %s", len(lo.pins), ", LoRa OFF" if lo.off else "", dict(lo.stats))
    except KeyboardInterrupt:
        pass
    finally:
        bus.close()


if __name__ == "__main__":
    main()
