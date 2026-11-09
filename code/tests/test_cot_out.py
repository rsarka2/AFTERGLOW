"""cot_out.py: ag/alert (or ag/target and ag/pose with --wifi) -> CoT XML for ATAK."""
import socket
import time
import xml.etree.ElementTree as ET

import pytest

from afterglow import alert, config, geo
from afterglow.contracts import ALERT, POSE, TARGET, validate
from afterglow.cot_out import (CIRCLE, COLOURS, DELETE, DRONE, SPOT, CotOut, TcpServer, UdpSender, argb,
                               parse_dest)

CFG = config.load()
ORIGIN = CFG["mission"]["origin_lat"], CFG["mission"]["origin_lon"]
STALE, REPEAT = CFG["atak"]["stale_s"], CFG["atak"]["repeat_s"]


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make():
    out, clock = [], Clock()
    return CotOut(CFG, out.append, clock=clock), out, clock


def parse(out):
    evs = [ET.fromstring(b) for b in out]
    out.clear()
    return evs


def callsign(ev):
    return ev.find("detail/contact").get("callsign")


def remarks(ev):
    return ev.find("detail/remarks").text


def alert_msg(kind=alert.TYPE_TRACK, **kw):
    """What lora_rx.py publishes: a real encode/decode round trip, plus t, rssi and snr."""
    m = {"pin": "P3", "track": "T1", "x": 18.0, "y": 9.0, "r95": 12.0, "c": 0.82, "prio": 2, "cls": "findmy",
         "state": "localized", "last_s": 4, "flags": {"thermal": True}, "off_trail_m": 46}
    m.update(kw)
    msg = {"t": 1731000000123, "rssi": -71, "snr": 9.5, **alert.decode(alert.encode(m, kind=kind))}
    assert validate(ALERT, msg) == []
    return msg


def target_msg(**kw):
    """What geofence.py publishes."""
    m = {"t": 1731000000123, "pin": "P3", "track": "T1", "x": 4.0, "y": 6.0, "lat": 0, "lon": 0, "r95": 1.5,
         "c": 0.82, "prio": 2, "cls": "findmy", "state": "pinned", "last_s": 2, "off_trail_m": 6,
         "zone": "off_trail", "rule": "day_offtrail", "status": "investigating",
         "flags": {"sos": False, "thermal": False, "radar": False, "camera": False}, "br": None}
    m.update(kw)
    assert validate(TARGET, m) == []
    return m


def test_alert_becomes_a_coloured_spot_marker_and_circle():
    co, out, _ = make()
    co.feed(ALERT, alert_msg())
    mark, circle = parse(out)
    assert mark.get("type") == SPOT and mark.get("uid") == "afterglow.P3"
    assert callsign(mark) == "P3"  # the pin number, as on the dashboard
    lat, lon = geo.to_latlon(18.0, 9.0, *ORIGIN)
    pt = mark.find("point")
    assert float(pt.get("lat")) == pytest.approx(lat, abs=1e-6) and float(pt.get("lon")) == pytest.approx(lon, abs=1e-6)
    assert mark.find("detail/color").get("argb") == str(argb(COLOURS[2]))
    r = remarks(mark)
    for part in ("priority 2", "conf 82%", "Apple Find My (separated)", "46 m off trail", "±12 m", "localized", "also thermal",
                 "LoRa -71 dBm"):
        assert part in r
    assert circle.get("type") == CIRCLE and circle.get("uid") == "afterglow.P3.r95"
    assert circle.find("detail/shape/ellipse").get("major") == "12.0"
    assert circle.find("point").get("lat") == pt.get("lat")


def test_stale_time_and_colours():
    co, out, clock = make()
    co.feed(ALERT, alert_msg(prio=1))
    mark = parse(out)[0]
    assert mark.get("time").endswith("Z") and mark.get("stale") > mark.get("time")
    assert mark.find("detail/color").get("argb") == "-65536"  # red, as ATAK writes it
    assert argb(COLOURS[3]) == -256  # yellow


def test_heartbeat_places_the_pod():
    co, out, _ = make()
    co.feed(ALERT, alert_msg(kind=alert.TYPE_HEARTBEAT, pin=None, x=3.0, y=4.0, battery=80))
    (pod,) = parse(out)
    assert pod.get("type") == DRONE and pod.get("uid") == "afterglow.pod" and callsign(pod) == "AFTERGLOW"
    lat, _ = geo.to_latlon(3.0, 4.0, *ORIGIN)
    assert float(pod.find("point").get("lat")) == pytest.approx(lat, abs=1e-6)
    assert "battery 80%" in remarks(pod) and "search-assist" in remarks(pod)


def test_sos_only_pin_and_confirmed_person():
    co, out, _ = make()
    co.feed(ALERT, alert_msg(kind=alert.TYPE_SOS, pin="P4", track="T0", cls="wifi", prio=1, c=0, state="heard",
                             r95=30, flags={"sos": True}, sos={"hurt": True, "walk": False, "count": 2}))
    mark = parse(out)[0]
    assert callsign(mark) == "P4 SOS"
    assert remarks(mark).startswith("SOS: hurt, can't walk, 2 people · priority 1") and "conf" not in remarks(mark)

    co.feed(ALERT, alert_msg(state="confirmed", prio=1))
    mark = parse(out)[0]
    assert remarks(mark).startswith("PERSON CONFIRMED") and callsign(mark) == "P3"


def test_no_sos_text_without_the_flag():
    co, out, _ = make()
    co.feed(ALERT, alert_msg())  # alert.decode always fills sos, all zero
    assert "SOS" not in remarks(parse(out)[0])


def test_alerts_are_not_repeated():
    """Over LoRa, lora_out repeats the pins; cot_out repeating them would hide a dead radio."""
    co, out, clock = make()
    co.feed(ALERT, alert_msg())
    parse(out)
    clock.t += REPEAT * 3
    co.tick()
    assert out == []


def test_wifi_fallback_repeats_pins_and_deletes_cleared():
    co, out, clock = make()
    co.feed(TARGET, target_msg())
    mark, _ = parse(out)
    assert callsign(mark) == "P3" and "status investigating" in remarks(mark)
    assert "±1.5 m" in remarks(mark)  # indoor numbers keep a decimal

    clock.t += REPEAT / 2
    co.tick()
    assert out == []
    clock.t += REPEAT
    co.feed(POSE, {"t": 1731000001000, "x": 0, "y": 0, "z": 3, "src": "waypoint"})  # the bus is alive
    parse(out)
    co.tick()
    assert [e.get("uid") for e in parse(out)] == ["afterglow.P3", "afterglow.P3.r95"]

    co.feed(TARGET, target_msg(status="cleared"))
    dels = parse(out)
    assert [e.get("type") for e in dels] == [DELETE, DELETE]
    assert [e.find("detail/link").get("uid") for e in dels] == ["afterglow.P3", "afterglow.P3.r95"]
    clock.t += REPEAT * 2
    co.tick()
    assert out == []


def test_pins_sharing_a_track_are_separate_markers():
    """Every SOS-only pin is on T0, and a tracker restart can reuse a track number."""
    co, out, _ = make()
    co.feed(ALERT, alert_msg(kind=alert.TYPE_CONFIRMED, pin="P2", track="T0", state="confirmed", x=5.0))
    co.feed(ALERT, alert_msg(kind=alert.TYPE_SOS, pin="P7", track="T0", flags={"sos": True}, x=40.0))
    assert [e.get("uid") for e in parse(out)][::2] == ["afterglow.P2", "afterglow.P7"]

    co.feed(TARGET, target_msg(pin="P5", status="new"))
    co.feed(TARGET, target_msg(pin="P1", status="cleared"))  # same track T1
    dels = parse(out)[2:]
    assert [e.find("detail/link").get("uid") for e in dels] == ["afterglow.P1", "afterglow.P1.r95"]
    assert "afterglow.P5" in co.pins


def test_repeats_stop_soon_after_the_bus_goes_quiet():
    """ag/pose comes at 5 Hz over Wi-Fi, so a few silent seconds mean the Pi is gone."""
    co, out, clock = make()
    co.feed(TARGET, target_msg())
    parse(out)
    clock.t += 3 * REPEAT
    co.tick()
    assert len(parse(out)) == 2  # still repeating
    clock.t += 1
    co.tick()
    assert out == [] and 3 * REPEAT + 1 < STALE


def test_pose_is_throttled_per_drone():
    co, out, _ = make()
    every = CFG["atak"]["pod_every_s"] * 1000
    for t in (0, every // 4, every // 2, every, every + 1):
        co.feed(POSE, {"t": 1731000000000 + t, "x": 1.0, "y": 2.0, "z": 3.0, "src": "sim"})
    co.feed(POSE, {"t": 1731000000000, "x": 1.0, "y": 2.0, "z": 3.0, "src": "gps", "drone": "d2",
                   "course": 87.5, "speed_mps": 4.1})
    evs = parse(out)
    assert [e.get("uid") for e in evs] == ["afterglow.pod", "afterglow.pod", "afterglow.d2"]
    assert "SIMULATED" in remarks(evs[0])
    assert callsign(evs[2]) == "AFTERGLOW d2" and evs[2].find("detail/track").get("course") == "87.5"


def test_bad_messages_are_ignored():
    co, out, _ = make()
    bad_alert = alert_msg()
    del bad_alert["x"]
    co.feed(ALERT, bad_alert)
    co.feed(TARGET, {**target_msg(), "status": "lost"})
    co.feed(TARGET, {**target_msg(), "x": None})
    co.feed(POSE, {"t": 1, "x": float("nan"), "y": 0, "z": 0, "src": "sim"})
    assert out == []
    assert co.stats["bad_alert"] == 1 and co.stats["bad_target"] == 2 and co.stats["bad_pose"] == 1


def test_udp_sender_reaches_a_listener():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2)
    tx = UdpSender([f"udp://127.0.0.1:{rx.getsockname()[1]}"])
    try:
        CotOut(CFG, tx).feed(ALERT, alert_msg())
        data, _ = rx.recvfrom(65536)
        assert ET.fromstring(data).get("uid") == "afterglow.P3"
    finally:
        rx.close()
        tx.sock.close()


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        time.sleep(0.01)
    assert cond()


def read_event(sock):
    buf = b""
    while b"</event>" not in buf:
        chunk = sock.recv(65536)
        assert chunk, "server closed the stream"
        buf += chunk
    return ET.fromstring(buf[: buf.index(b"</event>") + len(b"</event>")])


def test_tcp_server_streams_events_to_a_phone_and_answers_pings():
    """iTAK adds the laptop as a TAK server over TCP: it must get the markers, and a pong for its ping."""
    srv = TcpServer(0, "127.0.0.1")
    phone = socket.create_connection(("127.0.0.1", srv.port), timeout=2)
    try:
        wait_for(lambda: len(srv.clients) == 1)
        CotOut(CFG, srv).feed(ALERT, alert_msg())
        assert read_event(phone).get("uid") == "afterglow.P3"
        assert read_event(phone).get("uid") == "afterglow.P3.r95"

        phone.sendall(b'<event version="2.0" uid="iTAK-ping" type="t-x-c-t" how="m-g"/>')
        assert read_event(phone).get("type") == "t-x-c-t-r"

        phone.close()  # the phone leaves: the server drops it and carries on
        wait_for(lambda: srv.clients == [])
        srv(b"<event/>")
    finally:
        phone.close()
        srv.close()


def test_destinations():
    assert parse_dest("udp://239.2.3.1:6969") == ("239.2.3.1", 6969)
    for bad in ("tcp://1.2.3.4:8087", "239.2.3.1:6969", "udp://239.2.3.1"):
        with pytest.raises(SystemExit):
            parse_dest(bad)
