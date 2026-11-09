"""The open missing-person case, for trail pathing on the TV dashboard (tv.html).

Laptop only. The case holds personal details (name, age, description), so it lives in the
config's [search] case_file under runs/ (never committed) and never goes on the MQTT bus or
over LoRa. The dashboard serves it at /api/case; the fake pod reads it to know which trail
to fly in the simulation.

    cs = CaseStore(cfg, pm)
    cs.open({"name": "...", "age": "34", "details": "red jacket", "carrying": "iphone",
             "car_park": "Gorham Mountain lot"})       # -> (problems, view)
    cs.open({"ping": "P3"})                            # the ping the ranger linked to the case
    cs.view()      # {"case", "start", "ranking", "route", "trail"}; case None when none is open
    cs.close()

Every change comes from a ranger's click on the dashboard. Nothing here flies the drone:
the trail is only a suggestion until the ranger picks it.
"""
import json
import logging
import threading

from . import config as cfgmod
from . import trails
from .contracts import now_ms
from .parkmap import ParkMap

log = logging.getLogger("case")

CARRYING = {  # what they carry -> the beacon class to expect
    "iphone": "findmy", "airtag": "findmy", "android": "findhub", "samsung": "samsung_off", "unknown": None,
}
LIMITS = {"name": 80, "age": 12, "details": 400}


class CaseStore:
    def __init__(self, cfg: dict, pm: ParkMap, path=None):
        self.cfg, self.pm = cfg, pm
        self.path = path if path is not None else cfgmod.resolve(cfg, cfg["search"]["case_file"])
        self.lock = threading.Lock()
        self.listeners = []  # fn(view) after every change
        self.case = None
        self._view = None
        try:
            if self.path and self.path.is_file():
                loaded = json.loads(self.path.read_text())
                self.case = loaded if isinstance(loaded, dict) and loaded.get("name") else None
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable case file %s: %s", self.path, e)

    # ---------- reads ----------
    def view(self) -> dict:
        with self.lock:
            if self._view is None:
                self._view = self._make_view()
            return self._view

    def _make_view(self) -> dict:
        c = self.case
        start, label = trails.start_point(self.cfg, c.get("car_park") if c else None)
        ranking = trails.rank(self.cfg, self.pm, start) if c else []
        names = {r["name"] for r in ranking}
        trail = c.get("trail") if c and c.get("trail") in names else (ranking[0]["name"] if ranking else None)
        return {"case": c, "start": {"x": start[0], "y": start[1], "label": label},
                "ranking": ranking, "trail": trail,
                "route": trails.route(self.pm, trail, start, self.cfg["search"]["segment_m"]) if trail else []}

    # ---------- writes (each one is a click) ----------
    def open(self, body: dict) -> tuple[list[str], dict]:
        """Open a case, or update the open one with the fields given."""
        problems, clean = self.check(body)
        if problems:
            return problems, self.view()
        with self.lock:
            base = dict(self.case or {"opened_t": now_ms()})
            base.update(clean)
            if not base.get("name"):
                return ["a case needs a name"], self._view or self._make_view()
            self.case = base
            self._view = None
        self._changed()
        return [], self.view()

    def close(self) -> dict:
        with self.lock:
            self.case, self._view = None, None
        self._changed()
        return self.view()

    def check(self, body: dict) -> tuple[list[str], dict]:
        problems, clean = [], {}
        for k, n in LIMITS.items():
            if k in body:
                v = body[k]
                if not isinstance(v, (str, int)) or isinstance(v, bool):
                    problems.append(f"{k} must be text")
                    continue
                v = str(v).strip()
                if len(v) > n:
                    problems.append(f"{k} is longer than {n} characters")
                clean[k] = v[:n]
        if "carrying" in body:
            if body["carrying"] not in CARRYING:
                problems.append(f"carrying must be one of {sorted(CARRYING)}")
            else:
                clean["carrying"] = body["carrying"]
                clean["expect_cls"] = CARRYING[body["carrying"]]
        known = {cp["name"] for cp in self.cfg["search"]["car_parks"]}
        if "car_park" in body:
            if body["car_park"] not in known | {"", None}:
                problems.append(f"unknown car park; pick one of {sorted(known)} or none")
            else:
                clean["car_park"] = body["car_park"] or ""
        if "team" in body:  # the ranger says a ground team is on its way: stamp the time
            if body["team"] is not True:
                problems.append("team must be true")
            else:
                clean["team_t"] = now_ms()
        if "prior_mode" in body:  # the sweep before the case, to go back to once a team is on its way
            if body["prior_mode"] not in ("patrol", "night"):
                problems.append("prior_mode must be patrol or night")
            else:
                clean["prior_mode"] = body["prior_mode"]
        if "ping" in body:  # the ping the ranger says is this person's (P3), or "" to unlink
            v = body["ping"]
            if v not in ("", None) and not (isinstance(v, str) and len(v) <= 6 and v[:1] == "P" and v[1:].isdigit()):
                problems.append("ping must look like P3, or be empty")
            else:
                clean["ping"] = v or ""
        if "trail" in body:
            names = set(trails.pieces(self.pm))
            if body["trail"] not in names | {"", None}:
                problems.append("unknown trail")
            else:
                clean["trail"] = body["trail"] or ""
        return problems, clean

    def _changed(self):
        if self.path:
            try:
                if self.case is None:
                    self.path.unlink(missing_ok=True)
                else:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self.path.write_text(json.dumps(self.case, indent=1))
            except OSError as e:
                log.warning("couldn't save the case to %s: %s", self.path, e)
        view = self.view()
        log.info("case %s", "opened/updated" if view["case"] else "closed")  # no personal details in the log
        for fn in list(self.listeners):
            try:
                fn(view)
            except Exception:
                log.exception("case listener failed")
