"""lora_out.py: ag/target -> 16-byte alerts: repeats, urgency, order, lora_off, restarts, and the chain to ATAK."""
import json
import os
import sys
import time
import types

import pytest

from afterglow import alert, config, radio
from afterglow.contracts import ALERT, CTRL, POSE, SYS, TARGET, validate
from afterglow.cot_out import CotOut
from afterglow.lora_out import URGENT_GAP_S, LoraOut, PodRadio, PrintRadio
from afterglow.lora_rx import LoraRx

CFG = config.load()
PARK = config.load(config.DEFAULT.parent / "park.toml")
REPEAT = CFG["lora"]["repeat_s"]
T0 = 1_800_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class Radio:
    def __init__(self, ok=True):
        self.pkts, self.ok = [], ok

    def __call__(self, pkt):
        assert len(pkt) == alert.SIZE == 16
        self.pkts.append(pkt)
        return self.ok

    def take(self):
        out = [alert.decode(p) for p in self.pkts]
        self.pkts.clear()
        return out


def make(cfg=CFG, off_file=None):
    radio, clock = Radio(), Clock()
    return LoraOut(cfg, radio, clock=clock, off_file=off_file), radio, clock


def drain(lo):
    while lo.tick():
        pass


def target(**kw):
    """What geofence.py publishes."""
    m = {"t": int(T0 * 1000), "pin": "P3", "track": "T1", "x": 4.0, "y": 6.0, "lat": 0, "lon": 0, "r95": 1.5,
         "c": 0.82, "prio": 2, "cls": "findmy", "state": "pinned", "last_s": 2, "off_trail_m": 6,
         "zone": "off_trail", "rule": "day_offtrail", "status": "new",
         "flags": {"sos": False, "thermal": False, "radar": False, "camera": False}, "br": None}
    m.update(kw)
    assert validate(TARGET, m) == []
    return m


def pins(alerts):
    return [a["pin"] for a in alerts if a["kind"] != alert.TYPE_HEARTBEAT]


def test_a_new_pin_goes_out_at_once_with_its_fields():
    lo, radio, _ = make()
    lo.feed(TARGET, target())
    drain(lo)
    a, hb = radio.take()
    assert a["kind"] == alert.TYPE_TRACK and a["pin"] == "P3" and a["version"] == CFG["lora"]["alert_version"] == 1
    assert (a["x"], a["y"], a["r95"]) == (4.0, 6.0, 1.5)  # version 1: decimetres keep indoor detail
    assert a["state"] == "pinned" and a["prio"] == 2 and a["cls"] == "findmy" and a["off_trail_m"] == 6
    assert a["c"] == pytest.approx(0.82, abs=0.01) and a["battery"] is None and a["last_s"] == 2
    assert hb["kind"] == alert.TYPE_HEARTBEAT and hb["pin"] == "P0"


def test_repeats_every_repeat_s_but_not_on_an_unchanged_republish():
    lo, radio, clock = make()
    lo.feed(TARGET, target())
    drain(lo)
    radio.take()
    for _ in range(4):  # geofence republishes about once a second; the pin hasn't changed
        clock.t += 1
        lo.feed(TARGET, target(t=int(clock.t * 1000), x=4.2))
        drain(lo)
    assert radio.take() == []
    clock.t += 1  # REPEAT s since it went out
    drain(lo)
    (a, hb) = radio.take()
    assert a["pin"] == "P3" and a["x"] == 4.2 and hb["kind"] == alert.TYPE_HEARTBEAT


def test_a_change_goes_out_at_once_but_never_faster_than_the_gap():
    lo, radio, clock = make()
    lo.feed(TARGET, target(state="localized", prio=3))
    drain(lo)
    radio.take()
    lo.feed(TARGET, target(state="pinned", prio=2))
    drain(lo)
    assert radio.take() == []  # changed straight away: waits out the gap
    clock.t += URGENT_GAP_S
    drain(lo)
    (a,) = radio.take()
    assert a["state"] == "pinned" and a["prio"] == 2

    clock.t += URGENT_GAP_S
    lo.feed(TARGET, target(state="pinned", prio=2, flags={"thermal": True}))  # a flag counts too
    drain(lo)
    (a,) = radio.take()
    assert a["flags"]["thermal"]

    clock.t += URGENT_GAP_S
    lo.feed(TARGET, target(state="pinned", prio=2, flags={"thermal": True}, status="acknowledged"))
    drain(lo)
    assert radio.take() == []  # a status click changes nothing in the alert: it waits for the repeat


def test_priority_one_goes_first():
    lo, radio, _ = make()
    lo.feed(TARGET, target(pin="P2", prio=3, state="localized"))
    lo.feed(TARGET, target(pin="P9", prio=1, state="confirmed", status="confirmed"))
    lo.feed(TARGET, target(pin="P5", prio=3, state="localized"))
    lo.feed(TARGET, target(pin="P4", prio=2))
    assert lo.tick()  # one packet per tick, so a lora_off gets in between
    assert pins(radio.take()) == ["P9"]
    drain(lo)
    assert pins(radio.take()) == ["P4", "P2", "P5"]


def test_alert_types():
    lo, radio, _ = make()
    lo.feed(TARGET, target(pin="P1"))
    lo.feed(TARGET, target(pin="P2", track="T0", rule="sos", cls="wifi", prio=1, c=0, state="heard", r95=30.0,
                           flags={"sos": True}, sos={"hurt": True, "walk": False, "count": 2}))
    lo.feed(TARGET, target(pin="P3", track="T0", rule="sos", status="confirmed", state="confirmed", prio=1))
    drain(lo)
    by_pin = {a["pin"]: a for a in radio.take()}
    assert by_pin["P1"]["kind"] == alert.TYPE_TRACK
    assert by_pin["P2"]["kind"] == alert.TYPE_SOS and by_pin["P2"]["sos"] == {"hurt": True, "walk": False, "count": 2}
    assert by_pin["P2"]["r95"] == 25.5  # version 1 tops out at 25.5 m
    assert by_pin["P3"]["kind"] == alert.TYPE_CONFIRMED  # confirmed outranks SOS


def test_a_cleared_pin_stops():
    lo, radio, clock = make()
    lo.feed(TARGET, target(pin="P1"))
    lo.feed(TARGET, target(pin="P2"))
    drain(lo)
    radio.take()
    lo.feed(TARGET, target(pin="P1", status="cleared"))
    clock.t += REPEAT
    drain(lo)
    assert pins(radio.take()) == ["P2"]
    lo.feed(TARGET, target(pin="P7", status="cleared"))  # never seen: nothing to do
    assert "P7" not in lo.pins


def test_last_s_counts_up_and_battery_comes_from_sys():
    lo, radio, clock = make()
    lo.feed(SYS, {"t": 1, "batt_pct": 64})
    lo.feed(TARGET, target(last_s=3))
    drain(lo)
    assert [a["battery"] for a in radio.take()] == [64, 64]
    clock.t += REPEAT * 2 + 0.4  # geofence hasn't republished (its track went quiet)
    lo.feed(SYS, {"t": 2, "batt_pct": None})
    drain(lo)
    a, hb = radio.take()
    assert a["last_s"] == 3 + REPEAT * 2 and a["battery"] is None and hb["battery"] is None


def test_heartbeat_carries_the_pod_position():
    lo, radio, clock = make()
    drain(lo)
    (hb,) = radio.take()
    assert hb["kind"] == alert.TYPE_HEARTBEAT and (hb["x"], hb["y"]) == (0.0, 0.0)  # no pose yet
    lo.feed(POSE, {"t": 1, "x": 7.3, "y": -2.1, "z": 3.0, "src": "waypoint"})
    lo.feed(POSE, {"t": 2, "x": float("nan"), "y": 0, "z": 3.0, "src": "waypoint"})
    drain(lo)
    assert radio.take() == []
    clock.t += REPEAT
    drain(lo)
    (hb,) = radio.take()
    assert (hb["x"], hb["y"]) == (7.3, -2.1) and lo.stats["bad_pose"] == 1


def test_lora_off_silences_everything_and_is_remembered(tmp_path):
    off = tmp_path / "runs" / "lora_off.json"
    lo, radio, clock = make(off_file=off)
    lo.feed(TARGET, target())
    lo.feed(CTRL, {"t": 1, "cmd": "lora_off"})
    clock.t += REPEAT * 3
    drain(lo)
    assert radio.take() == [] and off.exists()

    lo2, radio2, _ = make(off_file=off)  # restarted by systemd: still silent
    lo2.feed(TARGET, target())
    drain(lo2)
    assert lo2.off and radio2.take() == []

    lo2.feed(CTRL, {"t": 2, "cmd": "lora_on"})
    drain(lo2)
    assert pins(radio2.take()) == ["P3"] and not off.exists()


def test_lora_on_resends_everything_at_once():
    lo, radio, clock = make()
    lo.feed(TARGET, target(pin="P1"))
    lo.feed(TARGET, target(pin="P2"))
    drain(lo)
    radio.take()
    clock.t += 1
    lo.feed(CTRL, {"t": 1, "cmd": "lora_off"})
    lo.feed(CTRL, {"t": 2, "cmd": "lora_on"})
    drain(lo)
    assert len(radio.take()) == 3  # two pins and the heartbeat, without waiting for the repeat


def test_restore_reloads_geofence_pins_but_not_cleared_or_old(tmp_path):
    f = tmp_path / "pins.json"
    f.write_text(json.dumps({"n": 3, "pins": {"P1": target(pin="P1", status="cleared"), "P2": target(pin="P2"),
                                              "P3": target(pin="P3", status="investigating")}, "links": {}}))
    lo, radio, _ = make()
    lo.restore(f, max_age_s=3600)
    drain(lo)
    assert pins(radio.take()) == ["P2", "P3"]

    old = time.time() - 7200
    os.utime(f, (old, old))
    lo, radio, _ = make()
    lo.restore(f, max_age_s=3600)
    assert lo.pins == {}

    f.write_text("not json")
    os.utime(f, None)
    make()[0].restore(f, max_age_s=3600)  # logs and carries on


def test_bad_targets_are_ignored():
    lo, radio, _ = make()
    lo.feed(TARGET, {**target(), "status": "lost"})
    lo.feed(TARGET, {k: v for k, v in target().items() if k != "x"})
    lo.feed(TARGET, target(pin="P4", x=None))  # passes validate, can't be encoded
    drain(lo)
    assert pins(radio.take()) == [] and lo.pins == {} and lo.stats["bad_target"] == 3


def test_a_failed_send_waits_for_the_repeat():
    lo, radio, clock = make()
    radio.ok = False
    lo.feed(TARGET, target())
    drain(lo)  # no busy loop on a missing radio
    assert len(radio.take()) == 2 and lo.stats["send_failed"] == 2
    clock.t += REPEAT
    drain(lo)
    assert len(radio.take()) == 2


def test_a_busy_channel_slows_every_pin_but_silences_none():
    """SF10: about 0.37 s a packet, so 20 pins and the heartbeat don't fit in repeat_s."""
    lo, radio, clock = make()
    for n in range(1, 21):
        lo.feed(TARGET, target(pin=f"P{n}", prio=1 if n <= 2 else 3 if n <= 12 else 4, state="localized"))
    heard = {}  # "P7" -> clock times it went out; "P0" is the heartbeat
    end = clock.t + 120
    while clock.t < end:
        if lo.tick():
            heard.setdefault(alert.decode(radio.pkts.pop())["pin"], []).append(clock.t)
            clock.t += 0.37  # the send blocks for its airtime
        else:
            clock.t += 0.1
    assert sorted(heard) == sorted(f"P{n}" for n in range(21))
    gaps = {pin: max(b - a for a, b in zip(ts, ts[1:])) for pin, ts in heard.items()}
    assert gaps["P1"] < REPEAT + 1 and gaps["P2"] < REPEAT + 1  # priority 1 keeps its pace
    assert max(gaps.values()) < 3 * REPEAT  # the rest slow down; nothing goes quiet


def test_the_radio_opens_again_after_a_failure(monkeypatch):
    """Blinka keeps SPI1 and the CS and RST pins claimed: a failed open must free them, or the retry finds
    them busy."""
    lcfg, claimed, answers = CFG["lora"], set(), [False, True, True]

    class Part:  # an SPI bus or a GPIO pin
        def __init__(self, name, **_):
            if name in claimed:
                raise RuntimeError(f"GPIO busy: {name}")
            claimed.add(name)
            self.name = name

        def deinit(self):
            claimed.discard(self.name)

    class RFM9x:
        def __init__(self, *a, **k):
            if not answers.pop(0):
                raise RuntimeError("Failed to find rfm9x with expected version -- check wiring")

        def send(self, pkt):
            if pkt == b"spi error":
                raise OSError("SPI")
            return True

    board = {"SCK_1": "SPI1", "MOSI_1": "MOSI", "MISO_1": "MISO",
             f"D{lcfg['cs_gpio']}": "CS", f"D{lcfg['reset_gpio']}": "RST"}
    monkeypatch.setitem(sys.modules, "board", types.SimpleNamespace(**board))
    monkeypatch.setitem(sys.modules, "busio", types.SimpleNamespace(SPI=Part))
    monkeypatch.setitem(sys.modules, "digitalio", types.SimpleNamespace(DigitalInOut=Part))
    monkeypatch.setitem(sys.modules, "adafruit_rfm9x", types.SimpleNamespace(RFM9x=RFM9x))

    pr = PodRadio(lcfg)  # not answering at start-up
    assert pr.rfm is None and claimed == set() and pr(b"x" * 16) is False
    pr.next_try = 0  # RETRY_OPEN_S later
    assert pr(b"x" * 16) is True and claimed == {"SPI1", "CS", "RST"}
    assert pr(b"spi error") is False and pr.rfm is None
    pr.next_try = 0
    assert pr(b"x" * 16) is True  # opened again over its own pins
    radio.close_pod_radio()
    assert claimed == set()


def test_park_profile_uses_metres():
    lo, radio, _ = make(PARK)
    lo.feed(TARGET, target(x=412.3, y=-88.0, r95=40.0))
    drain(lo)
    a = radio.take()[0]
    assert a["version"] == 2 and (a["x"], a["y"], a["r95"]) == (412.0, -88.0, 40.0)


def test_the_whole_chain_to_atak(capsys):
    """lora_out -> XIAO #3's line -> lora_rx -> ag/alert -> cot_out, as the --dry-run pipe runs it."""
    lo = LoraOut(CFG, PrintRadio(CFG["lora"]), clock=Clock())
    lo.feed(POSE, {"t": 1, "x": 3.0, "y": 1.0, "z": 3.0, "src": "waypoint"})
    lo.feed(TARGET, target(pin="P3", prio=1, state="confirmed", status="confirmed"))
    drain(lo)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2

    alerts = []

    class Collect:
        def publish(self, topic, msg, retain=False):
            assert topic == ALERT and validate(ALERT, msg) == []
            alerts.append(msg)

    rx = LoraRx(Collect(), CFG["lora"])
    for line in lines:
        rx.handle_line(line, t=int(T0 * 1000))
    assert [a["kind"] for a in alerts] == [alert.TYPE_CONFIRMED, alert.TYPE_HEARTBEAT]

    cot = []
    co = CotOut(CFG, cot.append, clock=Clock())
    for a in alerts:
        co.feed(ALERT, a)
    uids = [b.split(b'uid="')[1].split(b'"')[0] for b in cot]
    assert uids == [b"afterglow.P3", b"afterglow.P3.r95", b"afterglow.pod"]
    assert b"PERSON CONFIRMED" in cot[0]
