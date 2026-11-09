#!/usr/bin/env python3
"""Replay a recorded bus log into the same topics with the original timing: the 3 a.m.
debugging tool and the demo backup.

Record (every run, see docs/MODULES.md "Habits"):
    mosquitto_sub -v -t 'ag/#' > runs/$(date +%H%M).log

Replay:
    python3 tools/replay.py runs/1432.log                       # everything except operator commands
    python3 tools/replay.py runs/1432.log --speed 4 --loop      # faster, forever (demo backup)
    python3 tools/replay.py runs/1432.log --topics 'ag/obs/#,ag/pose'   # raw input only: test a new tracker
    python3 tools/replay.py runs/1432.log --start 120 --print | head    # from 2 min in, no broker

What it changes, and why:
    - ag/pose src becomes "replay", so the dashboard badges it REPLAY (--keep-src to leave it).
    - t is moved to now, keeping the gaps, so ages and lora_out's last_s look live (--keep-t to leave it).
    - Gaps longer than --max-gap seconds (default 10) shrink to that, so a paused run doesn't stall.
    - ag/ctrl and ag/mode are skipped unless --with-ctrl: a recorded ack or confirm was a person's
      decision at the time, and replaying it into a live geofence would make it again unasked.
    - ag/coverage (and ag/mode with --with-ctrl) is republished retained, as locate and the
      dashboard do (contracts.RETAINED).

Log format: one `topic payload` per line, as `mosquitto_sub -v` writes it. Lines that aren't
a topic and a JSON object are skipped and counted.
"""
import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from afterglow import config  # noqa: E402
from afterglow.contracts import CTRL, MODE, POSE, RETAINED, now_ms, validate  # noqa: E402

OPERATOR = {CTRL, MODE}


@dataclass
class Opts:
    speed: float = 1.0
    start_s: float = 0.0
    max_gap_s: float = 10.0
    topics: tuple = ()  # MQTT patterns to keep; empty = everything
    with_ctrl: bool = False
    keep_src: bool = False


def parse(lines, stats=None):
    """Yield (topic, msg) from `mosquitto_sub -v` lines."""
    stats = {} if stats is None else stats
    for line in lines:
        line = line.strip()
        if not line:
            continue
        topic, _, payload = line.partition(" ")
        try:
            msg = json.loads(payload)
        except ValueError:
            msg = None
        if not topic.startswith("ag/") or not isinstance(msg, dict):
            stats["skipped"] = stats.get("skipped", 0) + 1
            continue
        yield topic, msg


def schedule(records, o: Opts, stats=None):
    """Yield (offset_s, topic, msg, retain): when to publish each message, in seconds of
    wall time after the replay starts (already divided by the speed). msg["t"] is still the
    recorded time here; main() moves it to now unless --keep-t."""
    stats = {} if stats is None else stats
    first = latest = None  # recorded times: the first, and the newest so far
    clock = 0.0  # seconds into the replay, before the speed
    started = False
    for topic, msg in records:
        if topic in OPERATOR and not o.with_ctrl:
            stats["operator_skipped"] = stats.get("operator_skipped", 0) + 1
            continue
        if o.topics and not any(mqtt.topic_matches_sub(p, topic) for p in o.topics):
            continue
        t = msg.get("t")
        # Retained messages don't set the pace: a log usually opens with an old retained one.
        # Anything older than the newest so far plays at once.
        if isinstance(t, (int, float)) and topic not in RETAINED:
            if first is None:
                first = latest = t
            elif t > latest:
                gap = (t - latest) / 1000
                latest = t
                if started:
                    clock += min(gap, o.max_gap_s)
        before_start = (first is None and o.start_s > 0) or (first is not None and (latest - first) / 1000 < o.start_s)
        if before_start:
            continue
        started = True
        msg = dict(msg)
        if topic == POSE and not o.keep_src:
            msg["src"] = "replay"
        problems = validate(topic, msg)
        if problems:
            stats["invalid"] = stats.get("invalid", 0) + 1
        yield clock / o.speed, topic, msg, topic in RETAINED


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", help="a mosquitto_sub -v log")
    ap.add_argument("--config", help="for the broker address ([mqtt])")
    ap.add_argument("--broker", help="MQTT host[:port], overrides the config")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--start", type=float, default=0.0, help="skip this many recorded seconds")
    ap.add_argument("--max-gap", type=float, default=10.0, help="shrink longer gaps to this (seconds)")
    ap.add_argument("--topics", default="", help="comma list of MQTT patterns to replay (default: all)")
    ap.add_argument("--with-ctrl", action="store_true", help="also replay ag/ctrl and ag/mode (operator commands)")
    ap.add_argument("--keep-src", action="store_true", help="leave ag/pose src as recorded")
    ap.add_argument("--keep-t", action="store_true", help="leave t as recorded instead of moving it to now")
    ap.add_argument("--loop", action="store_true", help="start again at the end, forever")
    ap.add_argument("--print", action="store_true", help="print instead of publishing (no waiting)")
    a = ap.parse_args()

    o = Opts(speed=a.speed, start_s=a.start, max_gap_s=a.max_gap, with_ctrl=a.with_ctrl, keep_src=a.keep_src,
             topics=tuple(p.strip() for p in a.topics.split(",") if p.strip()))
    lines = Path(a.log).read_text(errors="replace").splitlines()
    stats = {}
    if a.print:
        pub = lambda topic, m, retain: print(topic, json.dumps(m, separators=(",", ":")), flush=True)  # noqa: E731
        bus = None
    else:
        from afterglow.bus import Bus
        cfg = config.load(a.config)
        host, port = cfg["mqtt"]["host"], cfg["mqtt"]["port"]
        if a.broker:
            host, _, p = a.broker.partition(":")
            port = int(p) if p else port
        bus = Bus(host, port, f"replay-{int(time.time())}")
        pub = bus.publish

    n = 0
    try:
        while True:
            start = time.monotonic()
            sent = 0
            for offset, topic, msg, retain in schedule(parse(lines, stats), o, stats):
                if not a.print:
                    wait = start + offset - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                if not a.keep_t and "t" in msg:
                    msg["t"] = now_ms()
                pub(topic, msg, retain)
                sent += 1
            n += sent
            if not a.loop or sent == 0:
                break
            print(f"replay: loop done ({sent} messages), starting again", file=sys.stderr)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        if bus:
            bus.close()
        print(f"replay: {n} messages published; {stats}", file=sys.stderr)


if __name__ == "__main__":
    main()
