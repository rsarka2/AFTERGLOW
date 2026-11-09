"""The park map in local metres: trails, roads and zones from the GeoJSON files the config names.

geofence.py (off-trail distance, which zone a pin is in), the dashboard (map drawing) and
the fake bus all use this, so they agree on the geometry.

    pm = ParkMap.load(cfg)
    pm.dist_to_trail(x, y)        # metres to the nearest trail or road
    pm.zone_at(x, y)              # the most specific zone there, e.g. the campground, or None
    pm.bounds()                   # (x0, y0, x1, y1) of the "park" zone

Trails without a "kind" property (the indoor map) count as "trail".
"""
import json
import math
from dataclasses import dataclass

from . import config, geo

Pt = tuple[float, float]


@dataclass
class Line:
    id: str
    name: str
    kind: str  # "trail" or "road"
    pts: list[Pt]


@dataclass
class Zone:
    id: str
    name: str
    kind: str  # "park", "campground", ...
    ring: list[Pt]  # closed: first point == last point


def seg_dist(px: float, py: float, a: Pt, b: Pt) -> float:
    """Distance from point p to segment ab."""
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def point_in_ring(px: float, py: float, ring: list[Pt]) -> bool:
    """Ray casting; points exactly on an edge may go either way."""
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > py) != (y2 > py) and px < x1 + (py - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


class ParkMap:
    def __init__(self, lines: list[Line], zones: list[Zone], credit: str = ""):
        self.lines = lines
        self.zones = zones
        self.credit = credit

    @classmethod
    def load(cls, cfg: dict) -> "ParkMap":
        lat0, lon0 = cfg["mission"]["origin_lat"], cfg["mission"]["origin_lon"]

        def xy(coords):
            return [geo.to_xy(lat, lon, lat0, lon0) for lon, lat in coords]  # GeoJSON is [lon, lat]

        lines, zones, credit = [], [], set()
        for key in ("trails_geojson", "zones_geojson"):
            doc = json.loads(config.resolve(cfg, cfg["rules"][key]).read_text())
            if doc.get("attribution"):
                credit.add(doc["attribution"])
            for f in doc["features"]:
                p, g = f.get("properties") or {}, f["geometry"]
                if g["type"] == "LineString":
                    parts = [g["coordinates"]]
                elif g["type"] == "MultiLineString":
                    parts = g["coordinates"]
                elif g["type"] == "Polygon":
                    zones.append(Zone(p.get("id", ""), p.get("name", ""), p.get("kind", "zone"),
                                      xy(g["coordinates"][0])))
                    continue
                else:
                    continue
                for part in parts:
                    lines.append(Line(p.get("id", ""), p.get("name", ""), p.get("kind", "trail"), xy(part)))
        return cls(lines, zones, "; ".join(sorted(credit)))

    def dist_to_trail(self, x: float, y: float, kinds: tuple[str, ...] = ("trail", "road")) -> float:
        """Metres to the nearest line of these kinds; inf if there are none."""
        best = math.inf
        for ln in self.lines:
            if ln.kind not in kinds:
                continue
            for a, b in zip(ln.pts, ln.pts[1:]):
                best = min(best, seg_dist(x, y, a, b))
        return best

    def zones_at(self, x: float, y: float) -> list[Zone]:
        return [z for z in self.zones if point_in_ring(x, y, z.ring)]

    def zone_at(self, x: float, y: float) -> Zone | None:
        """The most specific zone containing the point: a campground before the park outline."""
        hits = self.zones_at(x, y)
        hits.sort(key=lambda z: z.kind == "park")
        return hits[0] if hits else None

    def in_kind(self, x: float, y: float, kind: str) -> bool:
        return any(z.kind == kind for z in self.zones_at(x, y))

    def dist_to_kind(self, x: float, y: float, kind: str) -> float:
        """Metres to the nearest zone of this kind: 0 inside one, inf if there are none."""
        best = math.inf
        for z in self.zones:
            if z.kind != kind:
                continue
            if point_in_ring(x, y, z.ring):
                return 0.0
            for a, b in zip(z.ring, z.ring[1:]):
                best = min(best, seg_dist(x, y, a, b))
        return best

    def zone_label(self, x: float, y: float, off: float, offtrail_m: float) -> str:
        """ag/target "zone": a campground's (or other zone's) id, else "trail_corridor" within
        offtrail_m of a trail or "off_trail". `off` is dist_to_trail(x, y), already worked out."""
        z = self.zone_at(x, y)
        if z is not None and z.kind != "park":
            return z.id
        return "trail_corridor" if off <= offtrail_m else "off_trail"

    def bounds(self) -> tuple[float, float, float, float]:
        """(x0, y0, x1, y1) of the "park" zone, or of everything if there is none."""
        pts = [p for z in self.zones if z.kind == "park" for p in z.ring]
        if not pts:
            pts = [p for z in self.zones for p in z.ring] + [p for ln in self.lines for p in ln.pts]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def to_json(self) -> dict:
        """The map in metres, for the dashboard."""
        r = lambda pts: [[round(x, 2), round(y, 2)] for x, y in pts]  # noqa: E731
        return {"bounds": [round(v, 2) for v in self.bounds()], "credit": self.credit,
                "lines": [{"id": ln.id, "name": ln.name, "kind": ln.kind, "pts": r(ln.pts)} for ln in self.lines],
                "zones": [{"id": z.id, "name": z.name, "kind": z.kind, "ring": r(z.ring)} for z in self.zones]}
