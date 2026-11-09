"""The dashboard's half of the TAK link, added from outside: code/afterglow/dashboard.py is unchanged.

- TakState: the dashboard State, plus the field teams (ag/team): each phone's latest position, and
  their chat and reports, newest first. The browsers already get every bus message over /api/events;
  this puts the teams in /api/state too, so a TV that opens late sees them.
- TakCommands: the dashboard Commands, plus rescue(): the ground team's route goes on the bus as
  ag/rescue, and tak_link.py draws it on the phones. Geometry only, never case details.
- make_tak_handler: the dashboard's HTTP handler, plus POST /api/rescue, and meta.tak = true so the TV
  (code/afterglow/web/tv.js, which has the field-team panels built in) sends the rescue route.
  Every page and file is served exactly as before.
"""
import json
import math
from urllib.parse import urlsplit

import tak_contracts  # noqa: F401  (puts code/ on sys.path)
from afterglow.contracts import now_ms
from afterglow.dashboard import Commands, State, make_handler
from tak_contracts import RESCUE, TEAM, validate

TEAM_LOG = 40  # recent chat and reports from the TAK phones kept
MAX_PTS = 500
MAX_BODY = 32768  # a route of MAX_PTS points; the dashboard's own POSTs keep their 4 KB limit


class TakState(State):
    def clear(self):
        super().clear()
        self.teams = {}  # TAK phone uid -> its latest ag/team position (the field teams on the map)
        self.team_log = []  # newest first: ag/team chat and reports from the phones

    def apply(self, topic, msg):
        if topic != TEAM:
            return super().apply(topic, msg)
        self.counts[topic] = self.counts.get(topic, 0) + 1
        if "uid" not in msg:
            return
        if msg.get("kind") == "pos":
            self.teams[str(msg["uid"])] = msg
        else:
            self.team_log.insert(0, msg)
            del self.team_log[TEAM_LOG:]

    def snapshot(self):
        return {**super().snapshot(), "teams": self.teams, "team_log": self.team_log}


class TakCommands(Commands):
    def rescue(self, body: dict) -> tuple[int, dict]:
        """The ranger sent a ground team: their route goes to TAK (tak_link.py). Geometry only."""
        pts = body.get("pts")
        msg = {"t": now_ms(), "pin": body.get("pin"), "pts": pts}
        problems = validate(RESCUE, msg)
        if not (isinstance(msg["pin"], str) and msg["pin"][:1] == "P" and msg["pin"][1:].isdigit()):
            problems.append("pin must look like P3")
        if isinstance(pts, list) and not all(isinstance(p, list) and len(p) == 2 and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in p) for p in pts):
            problems.append("pts must be [[x, y], ...] in metres")
        if isinstance(pts, list) and len(pts) > MAX_PTS:
            problems.append(f"at most {MAX_PTS} points")
        if isinstance(body.get("secs"), (int, float)) and not isinstance(body.get("secs"), bool):
            msg["secs"] = round(float(body["secs"]))
        if isinstance(body.get("label"), str):
            msg["label"] = body["label"][:60]
        return self._publish(RESCUE, msg, problems)


def make_tak_handler(state, cmds: TakCommands, meta, dirs, cases=None, sim=None):
    Base = make_handler(state, cmds, {**meta, "tak": True}, dirs, cases, sim)  # tak: the TV may POST /api/rescue

    class TakHandler(Base):
        def do_POST(self):
            if urlsplit(self.path).path != "/api/rescue":
                return super().do_POST()
            # the same checks as the dashboard's own POSTs: JSON only (no cross-site clicks), small bodies
            if self.headers.get_content_type() != "application/json":
                return self.send_json(415, {"ok": False, "problems": ["Content-Type must be application/json"]})
            try:
                n = int(self.headers.get("Content-Length", 0))
                if not 0 <= n <= MAX_BODY:
                    raise ValueError("bad body length")
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
            except ValueError as e:
                return self.send_json(400, {"ok": False, "problems": [str(e)]})
            self.send_json(*cmds.rescue(body))

    return TakHandler
