"""The dashboard's half of the TAK link, added from outside dashboard.py: ag/team kept in the state,
POST /api/rescue -> ag/rescue, and meta.tak so the one TV (code/afterglow/web) sends the rescue route."""
import json
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from afterglow import config
from afterglow.dashboard import make_meta
from afterglow.parkmap import ParkMap

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tak"))  # the TAK add-on lives in tak/, beside code/
from tak_contracts import RESCUE, TEAM, validate  # noqa: E402
from tak_dashboard import TakCommands, TakState, make_tak_handler  # noqa: E402

WEB = Path(__file__).resolve().parents[1] / "afterglow" / "web"


class StubBus:
    def __init__(self):
        self.sent = []

    def publish(self, topic, msg, retain=False):
        self.sent.append((topic, msg))


def test_rescue_route_is_published_and_checked():
    bus = StubBus()
    cmds = TakCommands(lambda: bus, {})
    code, out = cmds.rescue({"pin": "P3", "pts": [[0, 0], [10.5, 20]], "secs": 400.4, "label": "Gorham Mountain lot",
                             "name": "never forwarded"})
    assert code == 200 and bus.sent[0][0] == RESCUE
    msg = bus.sent[0][1]
    assert not validate(RESCUE, msg) and msg["secs"] == 400 and "name" not in msg  # geometry only, no case details
    assert cmds.rescue({"pin": "P3", "pts": []})[0] == 200  # takes the route away
    for bad in ({"pin": "3", "pts": []}, {"pin": "P3", "pts": [[0]]}, {"pin": "P3", "pts": "x"},
                {"pin": "P3", "pts": [[0, float("nan")]]}):
        assert cmds.rescue(bad)[0] == 400


def test_team_positions_by_phone_and_messages_newest_first():
    st = TakState(5.0)
    st.apply(TEAM, {"t": 1, "uid": "A", "callsign": "ALPHA", "kind": "pos", "x": 1, "y": 2})
    st.apply(TEAM, {"t": 2, "uid": "A", "callsign": "ALPHA", "kind": "pos", "x": 3, "y": 4})
    st.apply(TEAM, {"t": 3, "uid": "B", "callsign": "BRAVO", "kind": "pos", "x": 5, "y": 6})
    st.apply(TEAM, {"t": 4, "uid": "A", "callsign": "ALPHA", "kind": "chat", "text": "on my way"})
    st.apply(TEAM, {"t": 5, "uid": "A", "callsign": "ALPHA", "kind": "report", "pin": "P3", "verdict": "found", "text": "found P3"})
    snap = st.snapshot()
    assert set(snap["teams"]) == {"A", "B"} and snap["teams"]["A"]["x"] == 3
    assert [m["kind"] for m in snap["team_log"]] == ["report", "chat"]
    assert "pins" in snap and "poses" in snap  # the dashboard's own state is all still there
    st.clear()
    assert st.snapshot()["teams"] == {} and st.snapshot()["team_log"] == []


def test_http_serves_the_tak_tv_and_takes_rescue_routes():
    cfg = config.load()
    meta = make_meta(cfg, ParkMap.load(cfg))
    bus = StubBus()
    st = TakState(meta["trail_step_m"])
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_tak_handler(st, TakCommands(lambda: bus, {}), meta, {}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        tv = urllib.request.urlopen(base + "/tv.js").read()  # bytes: no encoding or line-ending guesses (Windows)
        assert tv == (WEB / "tv.js").read_bytes() and b"FieldBanner" in tv  # the one TV, with the TAK parts built in
        assert b"<html" in urllib.request.urlopen(base + "/tv.html").read().lower()
        assert json.load(urllib.request.urlopen(base + "/api/map"))["tak"] is True  # the TV may send rescue routes
        assert "teams" in json.load(urllib.request.urlopen(base + "/api/state"))
        req = urllib.request.Request(base + "/api/rescue", method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"pin": "P2", "pts": [[0, 0], [5, 5]]}).encode())
        assert json.load(urllib.request.urlopen(req))["ok"] and bus.sent[-1][0] == RESCUE
    finally:
        server.shutdown()
        server.server_close()
