"""Bus must survive a bad message: a handler error must not kill paho's network thread."""
import json
from types import SimpleNamespace

from afterglow.bus import Bus


def make_bus():
    b = Bus.__new__(Bus)  # skip __init__: no broker needed
    b.handlers = {}
    return b


def deliver(b, topic, payload):
    b._on_message(None, None, SimpleNamespace(topic=topic, payload=payload))


def test_handler_error_is_contained():
    b = make_bus()
    got = []
    b.handlers["ag/ctrl"] = lambda t, m: got.append(int(m["n"]))
    deliver(b, "ag/ctrl", json.dumps({"cmd": "waypoint", "n": "oops"}).encode())  # raises inside the handler
    deliver(b, "ag/ctrl", json.dumps({"cmd": "waypoint", "n": 3}).encode())
    assert got == [3]


def test_cleared_retained_message_is_ignored_quietly(caplog):
    b = make_bus()
    got = []
    b.handlers["ag/#"] = lambda t, m: got.append(m)
    deliver(b, "ag/coverage", b"")  # what a subscriber sees when fake_bus wipes its grid
    assert got == [] and not caplog.records


def test_non_object_json_is_dropped():
    b = make_bus()
    got = []
    b.handlers["ag/#"] = lambda t, m: got.append(m)
    deliver(b, "ag/mode", b"42")
    deliver(b, "ag/mode", b"not json")
    assert got == []
