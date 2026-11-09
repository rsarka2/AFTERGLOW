"""tools/replay.py: timing, filters and the changes it makes on the way through."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from replay import Opts, parse, schedule  # noqa: E402

T = 1_731_000_000_000


def line(topic, **msg):
    return f"{topic} {json.dumps(msg)}"


LOG = [
    line("ag/pose", t=T, x=0, y=0, z=3, src="waypoint"),
    "garbage that isn't a message",
    line("ag/obs/ble", t=T + 500, src="omni", id="aa", cls="findmy", rssi=-70),
    line("ag/ctrl", t=T + 800, cmd="confirm", pin="P1"),
    line("ag/mode", t=T + 900, mode="night"),
    line("ag/coverage", t=T + 1000, x0=0, y0=0, cell_m=1, nx=1, ny=1, covered=[1], p=[0], pct=1.0),
    line("ag/pose", t=T + 61_000, x=1, y=0, z=3, src="waypoint"),  # a minute's pause
]


def run(o=Opts(), log=LOG):
    stats = {}
    return list(schedule(parse(log, stats), o, stats)), stats


def test_parse_skips_junk():
    stats = {}
    assert len(list(parse(LOG, stats))) == 6 and stats["skipped"] == 1


def test_operator_commands_are_not_replayed_by_default():
    # A recorded confirm was a person's decision then; replaying it would make it again unasked.
    out, stats = run()
    assert {t for _, t, _, _ in out}.isdisjoint({"ag/ctrl", "ag/mode"}) and stats["operator_skipped"] == 2
    out, _ = run(Opts(with_ctrl=True))
    assert {"ag/ctrl", "ag/mode"} <= {t for _, t, _, _ in out}


def test_timing_gaps_and_speed():
    out, _ = run()
    offsets = [off for off, *_ in out]
    assert offsets[:3] == pytest.approx([0.0, 0.5, 0.5])  # retained coverage plays in place, sets no pace
    assert offsets[3] == pytest.approx(10.5)  # the 60 s pause shrinks to max_gap (10 s)
    fast, _ = run(Opts(speed=2.0))
    assert [off for off, *_ in fast] == pytest.approx([o / 2 for o in offsets])


def test_pose_is_marked_replay_and_coverage_retained():
    out, _ = run()
    poses = [m for _, t, m, _ in out if t == "ag/pose"]
    assert all(m["src"] == "replay" for m in poses)
    assert all(retain == (t == "ag/coverage") for _, t, _, retain in out)
    out, _ = run(Opts(with_ctrl=True))
    assert all(retain == (t in {"ag/coverage", "ag/mode"}) for _, t, _, retain in out)
    kept, _ = run(Opts(keep_src=True))
    assert all(m["src"] == "waypoint" for _, t, m, _ in kept if t == "ag/pose")


def test_topic_filter_and_start():
    out, _ = run(Opts(topics=("ag/obs/#", "ag/pose")))
    assert {t for _, t, _, _ in out} == {"ag/obs/ble", "ag/pose"}
    out, _ = run(Opts(start_s=0.7))
    assert [(off, t) for off, t, _, _ in out] == [(0.0, "ag/pose")]  # the clock starts at the start point


def test_old_retained_message_first_causes_no_pause():
    log = [line("ag/coverage", t=T - 3_600_000, x0=0, y0=0, cell_m=1, nx=1, ny=1, covered=[0], p=[0], pct=0.0),
           line("ag/pose", t=T, x=0, y=0, z=3, src="gps"),
           line("ag/obs/ble", t=T - 200, src="omni", id="aa", cls="findmy", rssi=-70),  # out of order
           line("ag/pose", t=T + 200, x=0, y=0, z=3, src="gps")]
    out, _ = run(log=log)
    assert [off for off, *_ in out] == pytest.approx([0.0, 0.0, 0.0, 0.2])


def test_replayed_messages_still_validate():
    out, stats = run(Opts(with_ctrl=True))
    assert "invalid" not in stats
