"""tak_link.py both ways with the TAK phones: chat at each step of a pin, the rescue route, a phone that
connects late, and what the phones send back (positions, chat, ack / check / found / nobody)."""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from afterglow import config, geo
from afterglow.contracts import CTRL, MODE, TARGET

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tak"))  # the TAK add-on lives in tak/, beside code/
from tak_contracts import RESCUE, TEAM, validate  # noqa: E402
from tak_link import CHAT, CHAT_ROOM, DELETE, ROUTE, CotOut, cot_time  # noqa: E402

CFG = config.load()
ORIGIN = CFG["mission"]["origin_lat"], CFG["mission"]["origin_lon"]


def target(**kw):
    m = {"t": 1731000000123, "pin": "P3", "track": "T1", "x": 40.0, "y": 60.0, "lat": 0, "lon": 0, "r95": 12.0,
         "c": 0.82, "prio": 2, "cls": "findmy", "state": "localized", "last_s": 2, "off_trail_m": 46,
         "zone": "off_trail", "rule": "day_offtrail", "status": "new",
         "flags": {"sos": False, "thermal": False, "radar": False, "camera": False}, "br": None}
    m.update(kw)
    return m


def evs(out):
    return [ET.fromstring(b) for b in out]


def chats(out):
    return [e.find("detail/remarks").text for e in evs(out) if e.get("type") == CHAT]


def run(chat=True):
    out, bus = [], []
    co = CotOut(CFG, out.append, clock=lambda: 1_800_000_000.0, chat=chat, publish=lambda t, m: bus.append((t, m)))
    return co, out, bus


def test_one_chat_per_step_never_per_repeat():
    co, out, _ = run()
    for _ in range(3):  # geofence republishes, and cot_out repeats every repeat_s
        co.feed(TARGET, target())
    msgs = chats(out)
    assert len(msgs) == 1
    assert "Possible person P3" in msgs[0] and "ranger review" in msgs[0] and "46 m off trail" in msgs[0]
    assert "CONFIRMED" not in msgs[0]  # the drone proposes; it never confirms anyone
    chat = [e for e in evs(out) if e.get("type") == CHAT][0].find("detail/__chat")
    assert chat.get("chatroom") == CHAT_ROOM and chat.get("senderCallsign") == "AFTERGLOW"


def test_the_steps_investigate_seen_confirmed_cleared():
    co, out, _ = run()
    co.feed(TARGET, target())
    co.feed(TARGET, target(status="investigating"))
    co.feed(TARGET, target(status="investigating"))
    co.feed(TARGET, target(status="investigating", flags={"thermal": True, "camera": True}))
    co.feed(TARGET, target(status="confirmed", state="confirmed", prio=1, flags={"thermal": True, "camera": True}))
    co.feed(TARGET, target(status="confirmed", state="confirmed", prio=1))
    msgs = chats(out)
    assert [m.split(":")[0].split(" now")[0] for m in msgs] == [
        "Possible person P3", "Drone checking P3", "P3 check", "PERSON CONFIRMED by the ranger at P3"]
    assert "heat and a person on camera" in msgs[2]
    out.clear()
    co.feed(TARGET, target(pin="P4"))
    co.feed(TARGET, target(pin="P4", status="cleared"))
    assert chats(out)[-1].startswith("P4 cleared by the ranger")
    assert any(e.get("type") == DELETE for e in evs(out))


def test_off_by_default_so_markers_are_unchanged():
    co, out, _ = run(chat=False)
    co.feed(TARGET, target())
    assert out and not chats(out)


def test_mode_change_is_news_but_not_the_first_one():
    co, out, _ = run()
    co.feed(MODE, {"t": 1, "mode": "patrol"})
    co.feed(MODE, {"t": 2, "mode": "patrol"})
    assert not chats(out)
    co.feed(MODE, {"t": 3, "mode": "search"})
    assert chats(out) == ["AFTERGLOW now on: Trail pathing."]


def test_rescue_route_drawn_once_announced_and_removed():
    co, out, _ = run()
    msg = {"t": 1, "pin": "P3", "pts": [[0, 0], [30, 40], [40, 60]], "secs": 400, "label": "Gorham Mountain lot"}
    assert not validate(RESCUE, msg)
    co.feed(RESCUE, msg)
    co.feed(RESCUE, msg)
    routes = [e for e in evs(out) if e.get("type") == ROUTE]
    assert len(routes) == 2 and len(routes[0].findall("detail/link")) == 3
    assert routes[0].get("uid") == "afterglow.rescue.P3"
    msgs = chats(out)
    assert len(msgs) == 1 and "rescue route to P3" in msgs[0] and "about 7 min" in msgs[0]
    out.clear()
    co.feed(RESCUE, {"t": 2, "pin": "P3", "pts": []})
    assert any(e.get("type") == DELETE and e.find("detail/link").get("uid") == "afterglow.rescue.P3" for e in evs(out))
    assert "person safe" in chats(out)[0]


def test_a_phone_that_connects_gets_old_markers_cleaned_and_a_catch_up():
    co, out, _ = run()
    co.feed(TARGET, target(pin="P2"))
    out.clear()
    mine = []
    co.hello(mine.append)
    deleted = {e.find("detail/link").get("uid") for e in evs(mine) if e.get("type") == DELETE}
    assert "afterglow.P1" in deleted and "afterglow.P7.r95" in deleted
    assert "afterglow.P2" not in deleted and "afterglow.P2.r95" not in deleted  # live: kept, and resent below
    assert any(e.get("uid") == "afterglow.P2" for e in evs(out))
    catch_up = [e.find("detail/remarks").text for e in evs(mine) if e.get("type") == CHAT]
    assert catch_up and "Open pins: P2 (new)" in catch_up[0] and "found P3" in catch_up[0]


def phone_chat(text, who="ALPHA"):
    return (f'<event version="2.0" uid="GeoChat.X.All Chat Rooms.1" type="b-t-f" how="h-g-i-g-o" time="t" start="t" stale="t">'
            f'<point lat="0" lon="0" hae="0" ce="0" le="0"/><detail><__chat chatroom="All Chat Rooms" senderCallsign="{who}">'
            f'<chatgrp uid0="iTAK-123" uid1="All Chat Rooms"/></__chat><remarks>{text}</remarks></detail></event>').encode()


def test_phone_position_goes_on_the_bus_in_local_metres():
    co, _, bus = run()
    lat, lon = geo.to_latlon(120.0, -30.0, *ORIGIN)
    sa = (f'<event version="2.0" uid="iTAK-123" type="a-f-G-U-C" how="m-g" time="t" start="t" stale="t">'
          f'<point lat="{lat}" lon="{lon}" hae="0" ce="9" le="9"/><detail><contact callsign="ALPHA"/></detail></event>').encode()
    co.from_phone(sa)
    co.from_phone(sa)  # throttled: one per POS_EVERY_S
    assert len(bus) == 1
    topic, msg = bus[0]
    assert topic == TEAM and not validate(TEAM, msg)
    assert msg["kind"] == "pos" and msg["callsign"] == "ALPHA" and abs(msg["x"] - 120) < 0.5 and abs(msg["y"] + 30) < 0.5


def test_phone_chat_and_reports():
    co, out, bus = run()
    co.from_phone(phone_chat("on my way to the lot"))
    co.from_phone(phone_chat("ack p3"))
    co.from_phone(phone_chat("check P3 please"))
    co.from_phone(phone_chat("found P3, he's conscious"))
    co.from_phone(phone_chat("nobody at P4"))
    kinds = [(t, m.get("kind") or m.get("cmd")) for t, m in bus]
    assert kinds == [(TEAM, "chat"), (TEAM, "chat"), (TEAM, "report"), (TEAM, "chat"), (TEAM, "report"),
                     (TEAM, "chat"), (TEAM, "report"), (TEAM, "chat"), (TEAM, "report")]
    assert all(not validate(t, m) for t, m in bus)
    reports = [m for t, m in bus if t == TEAM and m["kind"] == "report"]
    assert [(r["pin"], r["verdict"]) for r in reports] == [("P3", "ack"), ("P3", "check"), ("P3", "found"), ("P4", "nobody")]
    replies = chats(out)
    assert "Waiting for the ranger to send it" in replies[1]
    assert "Waiting for the ranger to confirm" in replies[2] and "Waiting for the ranger to clear" in replies[3]
    assert not any("CONFIRMED" in r for r in replies)  # a report never confirms anyone


def test_a_phone_never_sends_the_drone_or_changes_a_pin():
    """The one rule: only the ranger's click does. Nothing a phone sends becomes ag/ctrl."""
    co, _, bus = run()
    for text in ("check P3", "ack P3", "found P3", "nobody P3", "investigate P3", "confirm P3", "clear P3"):
        co.from_phone(phone_chat(text))
    assert bus and not any(t == CTRL for t, _ in bus)


def test_only_a_message_that_starts_with_the_command_is_a_report():
    co, out, bus = run()
    for text in ("P3 is not a person", "is P3 clear?", "don't check P3 yet",
                 "nothing at P2, will check P4 next", "P3 not here", "we found nothing near P5"):
        co.from_phone(phone_chat(text))
    assert [m["kind"] for _, m in bus] == ["chat"] * 6  # chat on the TV, never a report
    hints = [r for r in chats(out) if "start the message with the command" in r]
    assert len(hints) == 3  # check, check, found: a command word in the wrong place gets told how to report


def test_own_messages_are_ignored():
    co, _, bus = run()
    co.from_phone(phone_chat("hello", who="AFTERGLOW"))
    co.from_phone(b"not xml")
    assert bus == []


def test_markers_match_the_tv_colour_and_words():
    co, out, _ = run()
    co.feed(MODE, {"t": 1, "mode": "patrol"})
    co.feed(TARGET, target(prio=2))
    spot = [e for e in evs(out) if e.get("uid") == "afterglow.P3"][-1]
    assert spot.find("detail/contact").get("callsign") == "P3 · ranger decision"
    assert spot.find("detail/color").get("argb") == str(0xFFF76B15 - (1 << 32))  # the TV's orange
    co.feed(TARGET, target(status="investigating"))
    assert [e for e in evs(out) if e.get("uid") == "afterglow.P3"][-1].find("detail/contact").get("callsign") == "P3 · drone checking"
    co.handle_pose({"t": 10, "x": 0, "y": 0, "z": 30, "src": "sim"})
    drone = [e for e in evs(out) if e.get("uid") == "afterglow.pod"][-1]
    assert drone.find("detail/contact").get("callsign") == "AFTERGLOW · Day sweep"


def test_a_cleared_pin_is_deleted_and_also_expired():
    co, out, _ = run()
    co.feed(TARGET, target())
    out.clear()
    co.feed(TARGET, target(status="cleared"))
    e = evs(out)
    assert any(x.get("type") == DELETE for x in e)
    expired = [x for x in e if x.get("uid") == "afterglow.P3"]
    assert expired and expired[0].get("stale") < cot_time(1_800_000_000.0)  # already stale: ages out on any TAK app


def test_start_and_pause_are_told_to_the_phones():
    co, out, _ = run()
    co.sim_state(paused=False, started=False)
    co.sim_state(paused=True, started=True)
    co.sim_state(paused=False, started=True)
    assert [m.split(":")[0].split(".")[0] for m in chats(out)] == ["AFTERGLOW launched", "AFTERGLOW paused by the ranger", "AFTERGLOW resumed"]
