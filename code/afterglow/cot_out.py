"""cot_out.py (tier 1, Software, laptop): pins -> ATAK markers (Cursor-on-Target XML over UDP).

    python -m afterglow.cot_out                      # ag/alert from lora_rx.py on the laptop's broker
    python -m afterglow.cot_out --wifi <pi-ip>       # fallback: ag/target and ag/pose from the Pi's broker
    python -m afterglow.cot_out --to udp://<phone-ip>:4242   # straight to one phone, if multicast is blocked
    python -m afterglow.cot_out --print              # read the bus, print the CoT instead of sending it
    python -m afterglow.lora_rx --dry-run < rx_lines.txt | python -m afterglow.cot_out --dry-run

Each pin becomes a spot marker coloured by priority (1 red, 2 orange, 3 yellow, 4 grey), named by
its pin number as on the dashboard ("P3"), with a remarks line and its 95% circle. The pod is a friendly drone,
placed from the LoRa heartbeat (or ag/pose with --wifi). x and y go back to lat/lon around the
config's origin, so the laptop must use the pod's origin. See docs/CONTRACTS.md, "ATAK".

Over LoRa, lora_out.py repeats every live pin every 5 s, and a pin it stops sending (cleared) goes
stale in ATAK after [atak] stale_s. With --wifi there is no lora_out, so cot_out repeats the pins
itself while the Pi's broker is still sending, and removes a cleared pin from ATAK at once.

It sends the same thing pytak would to ATAK's multicast group, without the extra package. It is
also a tiny TAK server on [atak] tcp_port: iTAK takes multicast for SA only, so on an iPhone add
this laptop as a server (Settings > Network > Servers, its IP, port 8087, TCP) to get the pins.
"""
import argparse
import json
import logging
import math
import queue
import socket
import struct
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

from . import config as cfgmod
from . import geo
from .alert import TYPE_CONFIRMED, TYPE_HEARTBEAT, TYPE_SOS
from .bus import Bus
from .contracts import ALERT, POSE, TARGET, validate

log = logging.getLogger("cot_out")

SPOT, DRONE, CIRCLE, DELETE = "b-m-p-s-m", "a-f-A-M-H-Q", "u-d-c-c", "t-x-d-d"
UNKNOWN = "9999999.0"  # CoT for "no height" and "no error estimate"
COLOURS = {1: 0xFFFF0000, 2: 0xFFFF8000, 3: 0xFFFFFF00, 4: 0xFF9E9E9E}  # prio -> ARGB: red, orange, yellow, grey
CLS_TEXT = {"findmy": "Apple Find My (separated)", "findhub": "Google Find Hub", "samsung_off": "Samsung lost mode",
            "ref": "our reference beacon", "wifi": "SOS Wi-Fi page", "other": "other beacon"}
STATE_TEXT = {"heard": "rough position", "localized": "localized", "pinned": "pinned"}
POSE_SRC_TEXT = {"sim": "SIMULATED", "replay": "REPLAY", "waypoint": "taped waypoints", "gps": "GPS", "imu": "IMU walk"}
XML_DECL = b'<?xml version="1.0" encoding="UTF-8"?>\n'


def argb(v: int) -> int:
    """ATAK reads colours as signed 32-bit ARGB."""
    return v - (1 << 32) if v >= 1 << 31 else v


def cot_time(s: float) -> str:
    return datetime.fromtimestamp(s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def metres(v: float) -> str:
    return f"{v:.1f}" if abs(v) < 10 else f"{v:.0f}"  # indoor numbers are small


def uid_of(pin: str) -> str:
    """One marker per pin, from either input, so switching to --wifi moves the same markers."""
    return f"afterglow.{pin}"


def event(uid, typ, lat, lon, now, stale_s, how="m-f", ce=None):
    ev = ET.Element("event", version="2.0", uid=uid, type=typ, how=how,
                    time=cot_time(now), start=cot_time(now), stale=cot_time(now + stale_s))
    ET.SubElement(ev, "point", lat=f"{lat:.7f}", lon=f"{lon:.7f}", hae=UNKNOWN,
                  ce=UNKNOWN if ce is None else f"{ce:.1f}", le=UNKNOWN)
    return ev, ET.SubElement(ev, "detail")


def to_bytes(ev) -> bytes:
    return XML_DECL + ET.tostring(ev, encoding="unicode").encode()


def placeable(m: dict) -> bool:
    try:
        return math.isfinite(float(m["x"])) and math.isfinite(float(m["y"]))
    except (KeyError, TypeError, ValueError):
        return False


def lora_text(m: dict) -> str:
    snr = m.get("snr")
    return f"LoRa {m['rssi']} dBm" + (f", SNR {snr}" if snr is not None else "")


def remarks(m: dict, prio: int, sos_pin: bool) -> str:
    """One line for the ATAK user, most urgent first. Never an id: the alert carries none anyway."""
    flags = m.get("flags") or {}
    parts = []
    if m.get("state") == "confirmed" or m.get("kind") == TYPE_CONFIRMED:
        parts.append("PERSON CONFIRMED by the ranger")
    if flags.get("sos"):  # alert.decode always fills sos; the flag says whether it means anything
        sos = m.get("sos") or {}
        n = sos.get("count") or 0
        parts.append("SOS: " + ", ".join(
            ["hurt" if sos.get("hurt") else "not hurt", "can walk" if sos.get("walk") else "can't walk"]
            + ([f"{n} {'person' if n == 1 else 'people'}"] if n else [])))
    parts.append(f"priority {prio}")
    if not sos_pin and m.get("c"):
        parts.append(f"conf {round(m['c'] * 100)}%")
    parts.append(CLS_TEXT.get(m.get("cls"), str(m.get("cls"))))
    if m.get("off_trail_m") is not None:
        parts.append(f"{metres(m['off_trail_m'])} m off trail")
    if m.get("r95"):
        parts.append(f"±{metres(m['r95'])} m")
    if not sos_pin and m.get("state") in STATE_TEXT:
        parts.append(STATE_TEXT[m["state"]])
    seen = [k for k in ("thermal", "camera", "radar") if flags.get(k)]
    if seen:
        parts.append("also " + ", ".join(seen))
    if m.get("br"):
        parts.append(f"breathing {m['br']}/min")
    try:  # an absolute time, so a repeated marker never shows a stale "4 s ago"
        heard = m["t"] / 1000 - float(m.get("last_s") or 0)
        parts.append("last heard " + datetime.fromtimestamp(heard).strftime("%H:%M:%S"))
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        pass
    if m.get("status"):  # --wifi only: the LoRa alert has no status
        parts.append(f"status {m['status']}")
    if m.get("rssi") is not None:
        parts.append(lora_text(m))
    return " · ".join(parts)


class CotOut:
    def __init__(self, cfg: dict, send, clock=time.time):
        a = cfg["atak"]
        self.send_raw, self.clock = send, clock
        self.origin = cfg["mission"]["origin_lat"], cfg["mission"]["origin_lon"]
        self.stale_s, self.repeat_s, self.circles = a["stale_s"], a["repeat_s"], a["circles"]
        self.quiet_s = 3 * self.repeat_s  # --wifi: no message this long (ag/pose is 5 Hz) = lost the Pi
        self.pod_every_ms = a["pod_every_s"] * 1000
        self.pins = {}         # uid -> latest ag/target (--wifi only), repeated every repeat_s like lora_out
        self.sent = {}         # uid -> wall time it last went out
        self.pod_t = {}        # drone -> bus t of its last marker
        self.last_input = None  # wall time of the last message: stop repeating once the bus goes quiet
        self.stats = Counter()

    # ---------- input ----------

    def feed(self, topic, msg):
        self.last_input = self.clock()
        if topic == ALERT:
            self.handle_alert(msg)
        elif topic == TARGET:
            self.handle_target(msg)
        elif topic == POSE:
            self.handle_pose(msg)

    def handle_alert(self, m):
        if validate(ALERT, m) or not placeable(m):
            self.stats["bad_alert"] += 1
            return
        if m["kind"] == TYPE_HEARTBEAT:  # lora_out puts the pod's position in it
            batt = m.get("battery")
            self.send_pod("pod", m["x"], m["y"],
                          ["battery unknown" if batt is None else f"battery {batt}%", lora_text(m)])
        else:
            self.send_pin(m)

    def handle_target(self, m):
        if validate(TARGET, m) or not placeable(m):
            self.stats["bad_target"] += 1
            return
        uid = uid_of(m["pin"])
        if m["status"] == "cleared":
            self.pins.pop(uid, None)
            self.sent.pop(uid, None)
            self.send_delete(uid)
            return
        self.pins[uid] = m
        self.send_pin(m)

    def handle_pose(self, m):
        if validate(POSE, m) or not placeable(m) or not isinstance(m["t"], int):
            self.stats["bad_pose"] += 1
            return
        drone = m.get("drone") or "pod"
        last = self.pod_t.get(drone)
        if last is not None and 0 <= m["t"] - last < self.pod_every_ms:  # ag/pose comes at 5 Hz
            return
        self.pod_t[drone] = m["t"]
        self.send_pod(drone, m["x"], m["y"], [POSE_SRC_TEXT.get(m["src"], str(m["src"]))],
                      m.get("course"), m.get("speed_mps"))

    def tick(self):
        """--wifi: resend every live pin each repeat_s, as lora_out does over LoRa."""
        now = self.clock()
        if self.last_input is None or now - self.last_input > self.quiet_s:
            return  # lost the Pi's broker: let the markers go stale rather than look live
        for uid, m in list(self.pins.items()):
            if now - self.sent.get(uid, 0) >= self.repeat_s:
                self.send_pin(m)

    # ---------- output ----------

    def send(self, ev):
        self.send_raw(to_bytes(ev))

    def send_pin(self, m):
        now = self.clock()
        x, y, r95 = float(m["x"]), float(m["y"]), float(m.get("r95") or 0)
        lat, lon = geo.to_latlon(x, y, *self.origin)
        prio = min(4, max(1, int(m.get("prio") or 4)))
        sos_pin = m.get("kind") == TYPE_SOS or m.get("rule") == "sos"
        uid = uid_of(m["pin"])
        colour = argb(COLOURS[prio])
        callsign = f"{m['pin']} SOS" if sos_pin else m["pin"]

        ev, d = event(uid, SPOT, lat, lon, now, self.stale_s, ce=r95 or None)
        ET.SubElement(d, "contact", callsign=callsign)
        ET.SubElement(d, "color", argb=str(colour))
        ET.SubElement(d, "usericon", iconsetpath=f"COT_MAPPING_SPOTMAP/{SPOT}/{colour}")
        ET.SubElement(d, "remarks").text = remarks(m, prio, sos_pin)
        self.send(ev)

        if self.circles and r95 > 0:
            ev, d = event(uid + ".r95", CIRCLE, lat, lon, now, self.stale_s, how="h-e")
            shape = ET.SubElement(d, "shape")
            ET.SubElement(shape, "ellipse", major=f"{r95:.1f}", minor=f"{r95:.1f}", angle="360")  # radius, metres
            ET.SubElement(d, "strokeColor", value=str(colour))
            ET.SubElement(d, "strokeWeight", value="3.0")
            ET.SubElement(d, "fillColor", value=str(argb(COLOURS[prio] & 0x00FFFFFF | 0x40000000)))
            ET.SubElement(d, "contact", callsign=f"{callsign} 95%")
            ET.SubElement(d, "labels_on", value="false")
            self.send(ev)
        self.sent[uid] = now
        self.stats["pin"] += 1

    def send_pod(self, drone, x, y, notes, course=None, speed=None):
        now = self.clock()
        lat, lon = geo.to_latlon(float(x), float(y), *self.origin)
        ev, d = event(f"afterglow.{drone}", DRONE, lat, lon, now, self.stale_s)
        ET.SubElement(d, "contact", callsign="AFTERGLOW" if drone == "pod" else f"AFTERGLOW {drone}")
        if isinstance(course, (int, float)) and isinstance(speed, (int, float)):
            ET.SubElement(d, "track", course=f"{course:.1f}", speed=f"{speed:.1f}")
        ET.SubElement(d, "remarks").text = " · ".join(["search-assist pod", *notes])
        self.send(ev)
        self.stats["pod"] += 1

    def send_delete(self, uid):
        now = self.clock()
        for u in (uid, uid + ".r95"):
            ev, d = event(u + ".delete", DELETE, 0.0, 0.0, now, 20, how="h-g-i-g-o")
            ET.SubElement(d, "link", uid=u, relation="none", type="none")
            ET.SubElement(d, "__forcedelete")
            self.send(ev)
        self.stats["deleted"] += 1


def parse_dest(url: str) -> tuple[str, int]:
    u = urlparse(url)
    if u.scheme != "udp" or not u.hostname or not u.port:
        raise SystemExit(f"bad ATAK address {url!r}: use udp://host:port")
    return u.hostname, u.port


class UdpSender:
    """Sends each event to every address: ATAK's multicast group, or a phone's own IP."""

    def __init__(self, urls, iface: str = ""):
        self.dests = [parse_dest(u) for u in urls]
        if not self.dests:
            raise SystemExit("no ATAK address: set [atak] send_to or pass --to")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("B", 1))  # this network only
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, struct.pack("B", 1))  # and this laptop
        if iface:  # else the system picks, which may not be the travel router
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
        self.errors = 0

    def __call__(self, data: bytes):
        for dest in self.dests:
            try:
                self.sock.sendto(data, dest)
            except OSError as e:  # Wi-Fi down, no route: keep trying, don't flood the log
                self.errors += 1
                if self.errors == 1 or self.errors % 100 == 0:
                    log.warning("can't send to %s:%d (%s); %d errors so far", dest[0], dest[1], e, self.errors)


class TcpServer:
    """A tiny TAK server: each phone that adds this laptop as a server (plain TCP) gets every event.
    No SSL and no login, so only on our own travel router. It answers pings and ignores the rest."""

    def __init__(self, port: int, host: str = ""):
        # IPv4 and IPv6 at once: on some phone hotspots the laptop gets an IPv6 address only.
        dual = not host and socket.has_dualstack_ipv6()
        self.sock = socket.create_server((host, port), family=socket.AF_INET6 if dual else socket.AF_INET,
                                         dualstack_ipv6=dual)
        self.port = self.sock.getsockname()[1]
        self.clients = []
        self.lock = threading.Lock()  # one writer at a time, so two events never interleave on a stream
        threading.Thread(target=self._accept, daemon=True).start()

    def __call__(self, data: bytes):
        with self.lock:
            for conn in list(self.clients):
                self._send(conn, data)

    def close(self):
        self.sock.close()
        with self.lock:
            for conn in list(self.clients):
                self._drop(conn)

    def _accept(self):
        while True:
            try:
                conn, addr = self.sock.accept()
            except OSError:
                return  # closed
            conn.settimeout(1.0)  # a phone that stops reading (screen locked) is dropped, not waited on
            with self.lock:
                self.clients.append(conn)
                log.info("TAK client %s connected (%d now)", addr[0], len(self.clients))
            threading.Thread(target=self._read, args=(conn,), daemon=True).start()

    def _read(self, conn):
        """Drain what the phone sends (its own position, chat) and answer its pings."""
        while True:
            try:
                data = conn.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                break
            if not data:
                break
            if b'type="t-x-c-t"' in data:
                ev, _ = event("takPong", "t-x-c-t-r", 0.0, 0.0, time.time(), 20, how="h-g-i-g-o")
                with self.lock:
                    self._send(conn, to_bytes(ev))
        with self.lock:
            self._drop(conn)

    def _send(self, conn, data):  # holding self.lock
        try:
            conn.sendall(data)
        except OSError:  # gone, or stopped reading: a part-sent event would garble its stream anyway
            self._drop(conn)

    def _drop(self, conn):  # holding self.lock
        if conn in self.clients:
            self.clients.remove(conn)
            log.info("TAK client left (%d now)", len(self.clients))
        conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--broker", help="MQTT host for ag/alert (default: the config's [mqtt] host)")
    ap.add_argument("--wifi", metavar="PI_HOST",
                    help="fallback if the radio fails: read ag/target and ag/pose from the Pi's broker")
    ap.add_argument("--to", action="append", metavar="URL",
                    help="send here instead of [atak] send_to, e.g. udp://192.168.8.30:4242 (repeatable)")
    ap.add_argument("--print", action="store_true", help="read the bus, print the CoT instead of sending it")
    ap.add_argument("--dry-run", action="store_true",
                    help="read `mosquitto_sub -v` lines from stdin, print the CoT")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = cfgmod.load(a.config)
    acfg = cfg["atak"]

    urls, where = a.to or acfg["send_to"], "stdout"
    if a.dry_run or a.print:
        send = lambda data: print(data.decode(), flush=True)  # noqa: E731
    else:
        senders = [UdpSender(urls, acfg["iface"])] if urls else []
        if acfg["tcp_port"]:
            senders.append(TcpServer(acfg["tcp_port"]))
        if not senders:
            raise SystemExit("nowhere to send: set [atak] send_to or tcp_port, or pass --to")
        send = lambda data: [s(data) for s in senders]  # noqa: E731
        where = ", ".join(urls + ([f"TAK clients on TCP {acfg['tcp_port']}"] if acfg["tcp_port"] else []))
    co = CotOut(cfg, send)

    if a.dry_run:
        for line in sys.stdin:
            topic, _, payload = line.strip().partition(" ")
            try:
                msg = json.loads(payload)
            except ValueError:
                continue
            if isinstance(msg, dict):
                co.feed(topic, msg)
        log.info("stats: %s", dict(co.stats))
        return

    host = a.wifi or a.broker or cfg["mqtt"]["host"]
    topics = (TARGET, POSE) if a.wifi else (ALERT,)
    bus = Bus(host, cfg["mqtt"]["port"], "cot_out")
    inbox = queue.Queue()  # handlers only queue, so paho's thread stays fast; this loop does the work
    for topic in topics:
        bus.subscribe(topic, lambda topic, msg: inbox.put((topic, msg)))
    log.info("%s from %s -> %s; origin %.6f, %.6f (%s)", "+".join(topics), host,
             where, *co.origin, cfg["mission"]["name"])
    last_log = time.monotonic()
    try:
        while True:
            try:
                co.feed(*inbox.get(timeout=1.0))
            except queue.Empty:
                pass
            except Exception:  # one bad message mustn't stop the markers
                log.exception("failed on a message")
            co.tick()
            if time.monotonic() - last_log > 30:
                last_log = time.monotonic()
                log.info("stats: %s", dict(co.stats))
    except KeyboardInterrupt:
        pass
    finally:
        bus.close()


if __name__ == "__main__":
    main()
