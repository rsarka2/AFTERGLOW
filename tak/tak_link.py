"""tak_link.py (TAK add-on, laptop): code/afterglow/cot_out.py extended both ways, so the operator TV and
the field teams' TAK phones (iTAK / ATAK) work as one system. Run it instead of cot_out.py; cot_out.py
itself is unchanged. See tak/README.md.

    python tak/tak_link.py --config code/afterglow.toml               # ag/alert from lora_rx.py, as cot_out.py
    python tak/tak_link.py --wifi <pi-ip> --tls-dir tak/runs          # ag/target + ag/pose from the Pi, TLS for iTAK
    python tak/tak_link.py --print                                    # print the CoT instead of sending it
    (for the simulation, tak/tak_stack.py runs this in-process with the TV)

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

Two ways, so the TV (tv.html) and the phones work as one:
- To the phones: markers, plus a chat message at each step of a pin (possible person, drone checking,
  what it sees, confirmed or cleared by the ranger), the mode, and the ground team's rescue route
  (ag/rescue) as a white line. A phone that connects gets the map cleaned of old markers and a
  catch-up message. --no-chat leaves the messages out.
- From the phones: each phone's position (ag/team kind pos) and chat (kind chat) go to the TV.
  A chat that starts with "ack P3", "check P3", "found P3" or "nobody P3" is also a field report
  (kind report). The TV shows it to the ranger with the matching button. A phone never publishes
  ag/ctrl: the drone never decides, neither does a chat, and only the ranger sends the drone or
  confirms a person.
Phones also see each other: positions and chat are passed on to the other phones, as a TAK server does.
"""
import argparse
import json
import logging
import math
import queue
import re
import socket
import ssl
import struct
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

TAK = Path(__file__).resolve().parent
for p in (TAK, TAK.parent / "code"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
from afterglow import config as cfgmod  # noqa: E402
from afterglow import geo  # noqa: E402
from afterglow.alert import TYPE_CONFIRMED, TYPE_HEARTBEAT, TYPE_SOS  # noqa: E402
from afterglow.bus import Bus  # noqa: E402
from afterglow.contracts import ALERT, MODE, POSE, TARGET, now_ms  # noqa: E402
from tak_contracts import RESCUE, TEAM, validate  # noqa: E402  (validate: the TAK topics, then contracts.py's)

log = logging.getLogger("tak_link")

SPOT, DRONE, CIRCLE, DELETE, CHAT = "b-m-p-s-m", "a-f-A-M-H-Q", "u-d-c-c", "t-x-d-d", "b-t-f"
CHAT_ROOM = "All Chat Rooms"  # TAK's room for everyone on the network
CHAT_SENDER = "afterglow.pod"  # the pod's marker uid, so the message links to the drone
ROUTE = "u-d-f"  # a drawn line: the ground team's rescue route
MODE_TEXT = {"patrol": "Day sweep", "night": "Night sweep", "search": "Trail pathing", "investigate": "Emergency"}
# A field report must start the message: "found P3, he's conscious" is one; "P3 is not a person" and
# "don't check P3 yet" are only chat. Anywhere else in a message the words are too easy to misread.
COMMAND_RE = re.compile(r"^\s*(ack|check|found|nobody)\s+(?:at\s+)?P\s?(\d{1,3})\b", re.I)
LOOSE_RE = re.compile(r"\b(ack|check|found|nobody)\b.*\bP\s?\d{1,3}\b|\bP\s?\d{1,3}\b.*\b(ack|check|found|nobody)\b", re.I)
POS_EVERY_S = 2.0  # a phone's position goes on the bus at most this often
UNKNOWN = "9999999.0"  # CoT for "no height" and "no error estimate"
COLOURS = {1: 0xFFE5484D, 2: 0xFFF76B15, 3: 0xFFF5C13D, 4: 0xFF8B95A3}  # prio -> ARGB: the TV's ping colours (livemap.js PRIO_COLOR)
# what the TV says about a ping, as the marker's label on the phones
STATUS_LABEL = {"new": "ranger decision", "acknowledged": "acknowledged", "investigating": "drone checking",
                "confirmed": "PERSON · rescue"}
CLS_TEXT = {"findmy": "Apple Find My (separated)", "findhub": "Google Find Hub", "samsung_off": "Samsung lost mode",
            "ref": "our reference beacon", "wifi": "SOS Wi-Fi page", "other": "other beacon"}
STATE_TEXT = {"heard": "rough position", "localized": "localized", "pinned": "pinned"}
POSE_SRC_TEXT = {"sim": "SIMULATED", "replay": "REPLAY", "waypoint": "taped waypoints", "gps": "GPS"}
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
    def __init__(self, cfg: dict, send, clock=time.time, chat=False, publish=None):
        a = cfg["atak"]
        self.send_raw, self.clock, self.chat = send, clock, chat
        self.publish = publish  # fn(topic, msg): phones' positions, chat and commands go on the bus
        self.announced = {}    # uid -> the steps already announced in chat ("new", "check", "seen", "confirmed")
        self.routes = {}       # route uid -> latest ag/rescue, resent to a phone that connects
        self.mode = None       # the last ag/mode, to announce a change
        self.team_t = {}       # phone uid -> wall time its position last went on the bus
        self.origin = cfg["mission"]["origin_lat"], cfg["mission"]["origin_lon"]
        self.stale_s, self.repeat_s, self.circles = a["stale_s"], a["repeat_s"], a["circles"]
        self.quiet_s = 3 * self.repeat_s  # --wifi: no message this long (ag/pose is 5 Hz) = lost the Pi
        self.pod_every_ms = a["pod_every_s"] * 1000
        self.pins = {}         # uid -> latest ag/target (--wifi only), repeated every repeat_s like lora_out
        self.drawn = {}        # uid -> the last pin message drawn on the phones, to expire it on a delete
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
        elif topic == MODE:
            self.handle_mode(msg)
        elif topic == RESCUE:
            self.handle_rescue(msg)

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
            said = self.announced.pop(uid, None)
            last = self.pins.pop(uid, None) or self.drawn.get(uid) or m
            self.sent.pop(uid, None)
            self.drawn.pop(uid, None)
            self.send_delete(uid, last)
            if self.chat and said:
                self.send_chat(f"{m['pin']} cleared by the ranger: no one there. The drone is back on its search.",
                               *geo.to_latlon(float(m["x"]), float(m["y"]), *self.origin))
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
        confirmed = m.get("state") == "confirmed" or m.get("status") == "confirmed"
        label = STATUS_LABEL.get("confirmed" if confirmed else m.get("status"), "")

        ev, d = event(uid, SPOT, lat, lon, now, self.stale_s, ce=r95 or None)
        ET.SubElement(d, "contact", callsign=f"{callsign} · {label}" if label else callsign)
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
        self.drawn[uid] = m
        self.stats["pin"] += 1
        if self.chat:
            self.announce(m, uid, lat, lon, prio, sos_pin, callsign, r95)

    def announce(self, m, uid, lat, lon, prio, sos_pin, callsign, r95):
        """A chat message at each step of a pin, once each, never on a repeat: it appears, the drone
        checks it, the drone sees something, the ranger confirms. Cleared is in handle_target. It only
        proposes: "confirmed" comes from the ranger's click, never from the drone."""
        flags, status = m.get("flags") or {}, m.get("status")
        confirmed = m.get("state") == "confirmed" or m.get("kind") == TYPE_CONFIRMED or status == "confirmed"
        done = self.announced.setdefault(uid, set())
        where = f"{lat:.5f}, {lon:.5f}" + (f" ±{metres(r95)} m" if r95 else "")
        off = f", {metres(m['off_trail_m'])} m off trail" if m.get("off_trail_m") is not None else ""
        what = CLS_TEXT.get(m.get("cls"), str(m.get("cls")))
        texts = []
        if "new" not in done:
            done.add("new")
            if sos_pin:
                texts.append(f"SOS answer near {callsign}: {where}. {remarks(m, prio, sos_pin)}. Awaiting ranger review.")
            elif not confirmed:
                texts.append(f"Possible person {callsign}: {what} beacon at {where}{off}, priority {prio}. "
                             f"Awaiting ranger review: see marker {callsign}.")
        if status == "investigating" and "check" not in done:
            done.add("check")
            texts.append(f"Drone checking {callsign} now: flying over it with thermal and camera. Stand by.")
        seen = [w for k, w in (("thermal", "heat"), ("camera", "a person on camera"), ("radar", "breathing")) if flags.get(k)]
        if seen and not confirmed and "seen" not in done:
            done.add("seen")
            texts.append(f"{callsign} check: the drone sees {' and '.join(seen)}. Waiting for the ranger to confirm.")
        if confirmed and "confirmed" not in done:
            done.add("confirmed")
            texts.append(f"PERSON CONFIRMED by the ranger at {callsign}: {where}{off}. Ground teams: head to marker {callsign}.")
        for text in texts:
            self.send_chat(text, lat, lon)

    def handle_mode(self, m):
        mode = m.get("mode")
        if mode not in MODE_TEXT or mode == self.mode:
            return
        first, self.mode = self.mode is None, mode
        if self.chat and not first:  # the retained mode at start-up isn't news
            self.send_chat(f"AFTERGLOW now on: {MODE_TEXT[mode]}.", *self.origin)

    def sim_state(self, paused: bool, started: bool):
        """The TV's Start / Pause (sim only): tell the phones, so both screens agree on what's happening."""
        if not self.chat:
            return
        if not paused and not started:
            mode = MODE_TEXT.get(self.mode, "Day sweep")
            self.send_chat(f"AFTERGLOW launched: {mode}. Pings will appear here as the drone hears them.", *self.origin)
        elif not paused:
            self.send_chat("AFTERGLOW resumed.", *self.origin)
        else:
            self.send_chat("AFTERGLOW paused by the ranger.", *self.origin)

    def handle_rescue(self, m):
        """The ranger sent a ground team: draw their route on the phones, or take it away (pts [])."""
        if validate(RESCUE, m):
            self.stats["bad_rescue"] += 1
            return
        pin = str(m["pin"])
        uid = f"afterglow.rescue.{pin}"
        pts = [p for p in m["pts"] if isinstance(p, (list, tuple)) and len(p) == 2]
        if len(pts) < 2:
            if self.routes.pop(uid, None) is not None:
                self.send_delete_one(uid)
                if self.chat:
                    self.send_chat(f"Rescue for {pin} closed: person safe. Thank you, teams.", *self.origin)
            return
        new = uid not in self.routes
        self.routes[uid] = {**m, "pts": pts}
        self.send_route(uid, pin, pts, m)
        if self.chat and new:
            lat, lon = geo.to_latlon(float(pts[-1][0]), float(pts[-1][1]), *self.origin)
            mins = f", about {max(1, round(m['secs'] / 60))} min on foot" if isinstance(m.get("secs"), (int, float)) else ""
            start = f" from {m['label']}" if m.get("label") else ""
            self.send_chat(f"Ground team: rescue route to {pin} is on your map (white line){start}{mins}. "
                           f"Person at {lat:.5f}, {lon:.5f}.", lat, lon)

    def send_route(self, uid, pin, pts, m):
        now = self.clock()
        lls = [geo.to_latlon(float(x), float(y), *self.origin) for x, y in pts]
        ev, d = event(uid, ROUTE, *lls[-1], now, max(self.stale_s, 3600), how="h-e")
        for la, lo in lls:
            ET.SubElement(d, "link", point=f"{la:.7f},{lo:.7f}")
        ET.SubElement(d, "strokeColor", value=str(argb(0xFFFFFFFF)))
        ET.SubElement(d, "strokeWeight", value="4.0")
        ET.SubElement(d, "contact", callsign=f"Rescue route {pin}")
        ET.SubElement(d, "labels_on", value="true")
        ET.SubElement(d, "remarks").text = f"Ground team route to {pin}" + (f" from {m['label']}" if m.get("label") else "")
        self.send(ev)
        self.stats["route"] += 1

    def reset(self):
        """The sim went back to time zero: take every pin and route off the phones."""
        for uid in set(self.sent) | set(self.pins) | set(self.announced):
            self.send_delete(uid, self.drawn.get(uid))
        for uid in list(self.routes):
            self.send_delete_one(uid)
        self.pins.clear(); self.sent.clear(); self.announced.clear(); self.routes.clear(); self.drawn.clear()
        self.mode = None
        if self.chat:
            self.send_chat("AFTERGLOW simulation reset: map cleared, starting a new search.", *self.origin)

    def hello(self, send_one):
        """A phone connected: clean the markers it kept from an earlier run (pins and routes that aren't
        live now), resend what is live, and tell it what's open, since it missed the earlier messages."""
        live = set(self.sent) | set(self.pins)
        now = self.clock()
        for n in range(1, 100):
            for u in (f"afterglow.P{n}", f"afterglow.P{n}.r95", f"afterglow.rescue.P{n}"):
                if u not in live and u.removesuffix(".r95") not in live and u not in self.routes:
                    ev, d = event(u + ".delete", DELETE, 0.0, 0.0, now, 20, how="h-g-i-g-o")
                    ET.SubElement(d, "link", uid=u, relation="none", type="none")
                    ET.SubElement(d, "__forcedelete")
                    send_one(to_bytes(ev))
        for m in list(self.pins.values()):
            self.send_pin(m)
        for uid, r in list(self.routes.items()):
            self.send_route(uid, r["pin"], r["pts"], r)
        if self.chat:
            open_ = [m for m in self.pins.values() if m.get("status") not in ("cleared",)]
            lines = [f"{m['pin']} ({'CONFIRMED' if m.get('status') == 'confirmed' else m.get('status', 'open')})" for m in open_]
            mode = f" on {MODE_TEXT[self.mode]}" if self.mode in MODE_TEXT else ""
            self.send_chat(f"Connected to AFTERGLOW{mode}. " + (f"Open pins: {', '.join(lines)}." if lines else "No pins yet.")
                           + " Reply 'ack P3', 'check P3', 'found P3' or 'nobody P3'.", *self.origin, to=send_one)

    # ---------- from the phones ----------

    def from_phone(self, data: bytes):
        """One CoT event a phone sent: its position, or a chat message (maybe a command)."""
        if self.publish is None:
            return
        try:
            ev = ET.fromstring(data)
        except ET.ParseError:
            self.stats["bad_phone"] += 1
            return
        typ, uid = ev.get("type", ""), ev.get("uid", "")
        d, pt = ev.find("detail"), ev.find("point")
        if typ == CHAT and d is not None:
            chat = d.find("__chat")
            who = chat.get("senderCallsign") if chat is not None else "TAK user"
            grp = chat.find("chatgrp") if chat is not None else None
            sender = grp.get("uid0") if grp is not None else uid
            text = (d.findtext("remarks") or "").strip()
            if not text or who == "AFTERGLOW":
                return
            self.publish(TEAM, {"t": now_ms(), "uid": sender, "callsign": who, "kind": "chat", "text": text[:300]})
            self.stats["phone_chat"] += 1
            self.command(who, sender, text)
        elif typ.startswith("a-") and pt is not None and not uid.startswith("afterglow."):
            now = self.clock()
            if now - self.team_t.get(uid, 0) < POS_EVERY_S:
                return
            try:
                lat, lon = float(pt.get("lat")), float(pt.get("lon"))
            except (TypeError, ValueError):
                return
            if not (math.isfinite(lat) and math.isfinite(lon)) or (lat == 0 and lon == 0):
                return
            self.team_t[uid] = now
            contact = d.find("contact") if d is not None else None
            who = contact.get("callsign") if contact is not None and contact.get("callsign") else uid[:12]
            x, y = geo.to_xy(lat, lon, *self.origin)
            self.publish(TEAM, {"t": now_ms(), "uid": uid, "callsign": who, "kind": "pos",
                                "x": round(x, 1), "y": round(y, 1), "lat": round(lat, 7), "lon": round(lon, 7)})
            self.stats["phone_pos"] += 1

    def command(self, who, uid, text):
        """A field report from the chat: "ack P3" (the team has seen it), "check P3" (asks for the drone),
        "found P3" or "nobody P3". Each goes to the TV as a report; the ranger's click is what acts on it.
        Never ag/ctrl: a phone can't send the drone or change a pin (CLAUDE.md, the one rule)."""
        cm = COMMAND_RE.match(text)
        if not cm:
            if self.chat and LOOSE_RE.search(text):  # looks like a command, but not one we act on: say how
                self.send_chat(f"{who}: to report, start the message with the command, e.g. "
                               "'found P3', 'nobody P3', 'check P3' or 'ack P3'.", *self.origin)
            return
        verdict, pin = cm.group(1).lower(), f"P{int(cm.group(2))}"
        self.publish(TEAM, {"t": now_ms(), "uid": uid, "callsign": who, "kind": "report", "pin": pin,
                            "verdict": verdict, "text": text[:300]})
        reply = {"ack": f"Got it, {who}: told the ranger you have seen {pin}.",
                 "check": f"{who} asks for the drone to check {pin}. Waiting for the ranger to send it.",
                 "found": f"{who} reports a person at {pin}. Waiting for the ranger to confirm.",
                 "nobody": f"{who} reports no one at {pin}. Waiting for the ranger to clear it."}[verdict]
        self.stats["phone_cmd"] += 1
        if self.chat:
            self.send_chat(reply, *self.origin)

    def send_chat(self, text, lat, lon, to=None):
        """A TAK GeoChat message to everyone (or one phone, with to), from the AFTERGLOW pod."""
        now = self.clock()
        self.stats["chat"] += 1
        mid = f"{int(now * 1000)}-{self.stats['chat']}"
        ev, d = event(f"GeoChat.{CHAT_SENDER}.{CHAT_ROOM}.{mid}", CHAT, lat, lon, now, self.stale_s, how="h-g-i-g-o")
        chat = ET.SubElement(d, "__chat", parent="RootContactGroup", groupOwner="false", messageId=mid,
                             chatroom=CHAT_ROOM, id=CHAT_ROOM, senderCallsign="AFTERGLOW")
        ET.SubElement(chat, "chatgrp", uid0=CHAT_SENDER, uid1=CHAT_ROOM, id=CHAT_ROOM)
        ET.SubElement(d, "link", uid=CHAT_SENDER, type=DRONE, relation="p-p")
        ET.SubElement(d, "remarks", source=f"BAO.F.ATAK.{CHAT_SENDER}", to=CHAT_ROOM, time=cot_time(now)).text = text
        (to or self.send_raw)(to_bytes(ev))

    def send_pod(self, drone, x, y, notes, course=None, speed=None):
        now = self.clock()
        lat, lon = geo.to_latlon(float(x), float(y), *self.origin)
        ev, d = event(f"afterglow.{drone}", DRONE, lat, lon, now, self.stale_s)
        name = "AFTERGLOW" if drone == "pod" else f"AFTERGLOW {drone}"
        ET.SubElement(d, "contact", callsign=f"{name} · {MODE_TEXT[self.mode]}" if self.mode in MODE_TEXT else name)
        if isinstance(course, (int, float)) and isinstance(speed, (int, float)):
            ET.SubElement(d, "track", course=f"{course:.1f}", speed=f"{speed:.1f}")
        ET.SubElement(d, "remarks").text = " · ".join(["search-assist pod", *notes])
        self.send(ev)
        self.stats["pod"] += 1

    def send_delete(self, uid, m=None):
        """Take a pin (and its circle) off the phones. ATAK honours t-x-d-d; for a TAK app that doesn't,
        the pin we drew is also resent already stale (time and stale in the past), so it ages out."""
        now = self.clock()
        for u in (uid, uid + ".r95"):
            ev, d = event(u + ".delete", DELETE, 0.0, 0.0, now, 20, how="h-g-i-g-o")
            ET.SubElement(d, "link", uid=u, relation="none", type="none")
            ET.SubElement(d, "__forcedelete")
            self.send(ev)
        if m and placeable(m):
            self.send_expired(uid, SPOT, m)
            if self.circles:
                self.send_expired(uid + ".r95", CIRCLE, m)
        self.stats["deleted"] += 1

    def send_expired(self, uid, typ, m):
        lat, lon = geo.to_latlon(float(m["x"]), float(m["y"]), *self.origin)
        then = self.clock() - 600
        ev, d = event(uid, typ, lat, lon, then, 1)
        ET.SubElement(d, "contact", callsign=f"{m['pin']} · cleared")
        self.send(ev)

    def send_delete_one(self, uid):
        ev, d = event(uid + ".delete", DELETE, 0.0, 0.0, self.clock(), 20, how="h-g-i-g-o")
        ET.SubElement(d, "link", uid=uid, relation="none", type="none")
        ET.SubElement(d, "__forcedelete")
        self.send(ev)


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
    """A tiny TAK server: each phone that adds this laptop as a server gets every event. It answers
    pings and ignores the rest. Plain TCP by default (no login: only on our own router or hotspot).
    With ssl_ctx it speaks TLS instead, which iTAK needs: tak/tak_certs.py makes the certificates
    and the data package that adds this server to iTAK."""

    def __init__(self, port: int, host: str = "", ssl_ctx=None, on_connect=None, on_event=None, relay=True):
        """on_connect(send_one): a phone joined (send_one sends to it alone). on_event(bytes): one complete
        event a phone sent. relay: pass each phone's position and chat on to the other phones."""
        self.ssl_ctx, self.on_connect, self.on_event, self.relay = ssl_ctx, on_connect, on_event, relay
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
            threading.Thread(target=self._open, args=(conn, addr), daemon=True).start()

    def _open(self, conn, addr):
        """TLS handshake if any (in this thread, so one slow phone never blocks the next), then serve."""
        if self.ssl_ctx is not None:
            try:
                conn.settimeout(10.0)
                conn = self.ssl_ctx.wrap_socket(conn, server_side=True)
            except (OSError, ssl.SSLError) as e:
                log.warning("TAK client %s: TLS handshake failed (%s)", addr[0], e)
                conn.close()
                return
        conn.settimeout(1.0)  # a phone that stops reading (screen locked) is dropped, not waited on
        with self.lock:
            self.clients.append(conn)
            log.info("TAK client %s connected%s (%d now)", addr[0], " over TLS" if self.ssl_ctx else "",
                     len(self.clients))
        if self.on_connect:
            try:
                self.on_connect(lambda data, c=conn: self._send_one(c, data))
            except Exception:
                log.exception("on_connect failed")
        self._read(conn)

    def _send_one(self, conn, data):
        with self.lock:
            if conn in self.clients:
                self._send(conn, data)

    def _read(self, conn):
        """Read what the phone sends: answer its pings; hand each complete event (its position, chat) to
        on_event, and pass positions and chat on to the other phones."""
        buf = b""
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
            buf = (buf + data)[-1_000_000:]
            while b"</event>" in buf:
                end = buf.index(b"</event>") + len(b"</event>")
                start = buf.rfind(b"<event", 0, end)
                one, buf = (buf[start:end] if start >= 0 else b""), buf[end:]
                if not one or b'type="t-x-c-t' in one:
                    continue
                if self.relay and (b'type="a-' in one or b'type="b-t-f"' in one):
                    with self.lock:
                        for other in list(self.clients):
                            if other is not conn:
                                self._send(other, XML_DECL + one)
                if self.on_event:
                    try:
                        self.on_event(one)
                    except Exception:
                        log.exception("on_event failed")
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


def tls_context(cert_dir) -> ssl.SSLContext:
    """Server side of TLS from tak/tak_certs.py's files. Phones show the client certificate from the
    data package; it's checked against our CA when given, but not required."""
    d = Path(cert_dir)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(d / "server.pem", d / "server.key")
    ctx.load_verify_locations(d / "ca.pem")
    ctx.verify_mode = ssl.CERT_OPTIONAL
    return ctx


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--broker", help="MQTT host for ag/alert (default: the config's [mqtt] host)")
    ap.add_argument("--tls-dir", help="also serve TLS on --tls-port with tak/tak_certs.py's certificates (iTAK)")
    ap.add_argument("--tls-port", type=int, default=8089)
    ap.add_argument("--wifi", metavar="PI_HOST",
                    help="fallback if the radio fails: read ag/target and ag/pose from the Pi's broker")
    ap.add_argument("--to", action="append", metavar="URL",
                    help="send here instead of [atak] send_to, e.g. udp://192.168.8.30:4242 (repeatable)")
    ap.add_argument("--print", action="store_true", help="read the bus, print the CoT instead of sending it")
    ap.add_argument("--no-chat", action="store_true", help="markers only: no TAK chat alert for new or confirmed pins")
    ap.add_argument("--dry-run", action="store_true",
                    help="read `mosquitto_sub -v` lines from stdin, print the CoT")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = cfgmod.load(a.config)
    acfg = cfg["atak"]

    urls, where = a.to or acfg["send_to"], "stdout"
    inbox = queue.Queue()  # bus messages and the phones' events: handlers only queue, this loop does the work
    phones = {"on_connect": lambda send_one: inbox.put(("__hello__", send_one)),
              "on_event": lambda data: inbox.put(("__phone__", data))}
    if a.dry_run or a.print:
        send = lambda data: print(data.decode(), flush=True)  # noqa: E731
    else:
        senders = [UdpSender(urls, acfg["iface"])] if urls else []
        if acfg["tcp_port"]:
            senders.append(TcpServer(acfg["tcp_port"], **phones))
        if a.tls_dir:
            senders.append(TcpServer(a.tls_port, ssl_ctx=tls_context(a.tls_dir), **phones))
        if not senders:
            raise SystemExit("nowhere to send: set [atak] send_to or tcp_port, or pass --to")
        send = lambda data: [s(data) for s in senders]  # noqa: E731
        where = ", ".join(urls + ([f"TAK clients on TCP {acfg['tcp_port']}"] if acfg["tcp_port"] else []))
    co = CotOut(cfg, send, chat=not a.no_chat)

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
    topics = (TARGET, POSE, MODE, RESCUE) if a.wifi else (ALERT, MODE, RESCUE)
    bus = Bus(host, cfg["mqtt"]["port"], "tak_link")
    co.publish = bus.publish  # the phones' positions, chat and commands go back on the bus
    for topic in topics:
        bus.subscribe(topic, lambda topic, msg: inbox.put((topic, msg)))
    log.info("%s from %s -> %s; origin %.6f, %.6f (%s)", "+".join(topics), host,
             where, *co.origin, cfg["mission"]["name"])
    last_log = time.monotonic()
    try:
        while True:
            try:
                topic, msg = inbox.get(timeout=1.0)
                if topic == "__hello__":
                    co.hello(msg)
                elif topic == "__phone__":
                    co.from_phone(msg)
                else:
                    co.feed(topic, msg)
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
