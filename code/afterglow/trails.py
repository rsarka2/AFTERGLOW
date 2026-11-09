"""Trail pathing: which trails a missing person most likely took, and the route to fly along one.

Used by the TV dashboard (dashboard.py's /api/case) and the fake pod (tools/fake_bus.py).
Everything is in local metres, from parkmap.py.

    ranked = rank(cfg, pm, start_xy)          # [{"name", "pct", "dist_m", "length_m", "pop"}, ...]
    pts = route(pm, "Ocean Path", start_xy)   # [[x, y], ...] along the trail, nearest end first
    pts = route(pm, "Ocean Path", start_xy, max_m=600)   # just the stretch to search first
    along, total, off = progress(pts, x, y)   # metres along the route, its length, metres off it

The score is a stand-in until we have the published trail-use numbers:
    score = popularity x exp(-distance from the start / near_m)
with popularity per trail name from the config's [search] table (SAMPLE values).
"""
import math

from .parkmap import ParkMap, seg_dist

MIN_PIECE_M = 25.0  # trail scraps shorter than this are left out of a route
MAX_GAP_M = 150.0   # pieces of one trail further apart than this aren't joined


def start_point(cfg: dict, car_park: str | None) -> tuple[list[float], str]:
    """(x, y) and a label for a case's start: the named car park, else the park entrance."""
    s = cfg["search"]
    for cp in s["car_parks"]:
        if car_park and cp["name"] == car_park:
            return [float(cp["x"]), float(cp["y"])], cp["name"]
    return [float(v) for v in s["entrance_xy"]], "Park entrance"


def pieces(pm: ParkMap) -> dict[str, list[list[tuple[float, float]]]]:
    """Named trails -> their polylines (roads and unnamed scraps left out)."""
    out: dict[str, list] = {}
    for ln in pm.lines:
        if ln.kind != "trail" or not ln.name or len(ln.pts) < 2:
            continue
        out.setdefault(ln.name, []).append([tuple(p) for p in ln.pts])
    return out


def length(pts) -> float:
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def dist_to(pts, x: float, y: float) -> float:
    return min(seg_dist(x, y, a, b) for a, b in zip(pts, pts[1:]))


def rank(cfg: dict, pm: ParkMap, start, top: int = 5) -> list[dict]:
    """The most likely trails from start, best first, with pct summing to 100 over all trails."""
    s = cfg["search"]
    pop, near = s["popularity"], float(s["near_m"])
    rows = []
    for name, ps in pieces(pm).items():
        d = min(dist_to(p, *start) for p in ps)
        p = float(pop.get(name, s["default_popularity"]))
        rows.append({"name": name, "dist_m": round(d), "length_m": round(sum(length(q) for q in ps)),
                     "pop": p, "score": p * math.exp(-d / near)})
    total = sum(r["score"] for r in rows) or 1.0
    for r in rows:
        r["pct"] = round(100 * r.pop("score") / total)
    rows.sort(key=lambda r: (-r["pct"], r["dist_m"]))
    return rows[:top]


def chain(ps: list) -> list:
    """Join a trail's pieces into the longest single line, greedily by nearest free end."""
    ps = [p for p in ps if length(p) >= MIN_PIECE_M] or ps
    best: list = []
    for i, first in enumerate(ps):
        for seed in (first, first[::-1]):
            line, used = list(seed), {i}
            while True:
                end, pick = line[-1], None
                for j, p in enumerate(ps):
                    if j in used:
                        continue
                    for cand in (p, p[::-1]):
                        g = math.dist(end, cand[0])
                        if g <= MAX_GAP_M and (pick is None or g < pick[0]):
                            pick = (g, j, cand)
                if pick is None:
                    break
                used.add(pick[1])
                line += pick[2] if math.dist(end, pick[2][0]) > 1e-6 else pick[2][1:]
            if length(line) > length(best):
                best = line
    return best


def route(pm: ParkMap, name: str, start, max_m: float | None = None) -> list[list[float]]:
    """The trail as one line; [] if no such trail.

    Without max_m: the whole trail, starting at the end nearer the start point.
    With max_m: the stretch to search first. It begins where the trail passes closest to the
    start (the car park) and runs max_m along the longer way from there."""
    ps = pieces(pm).get(name)
    if not ps:
        return []
    line = chain(ps)
    if max_m:
        line = stretch(line, start, max_m)
    elif math.dist(line[-1], start) < math.dist(line[0], start):
        line = line[::-1]
    return [[round(x, 1), round(y, 1)] for x, y in line]


def stretch(line, start, max_m: float) -> list:
    """max_m of line, from its point nearest start, the longer way along it."""
    i = min(range(len(line)), key=lambda k: math.dist(line[k], start))
    way = line[i:] if length(line[i:]) >= length(line[:i + 1]) else line[i::-1]
    out, run = [way[0]], 0.0
    for a, b in zip(way, way[1:]):
        seg = math.dist(a, b)
        if run + seg >= max_m:
            f = (max_m - run) / seg if seg else 0.0
            out.append((a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])))
            return out
        out.append(b)
        run += seg
    return out


def progress(pts, x: float, y: float) -> tuple[float, float, float]:
    """(metres along the route to the point nearest (x, y), route length, metres off the route)."""
    if len(pts) < 2:
        return 0.0, 0.0, math.inf
    best, along, run = math.inf, 0.0, 0.0
    for a, b in zip(pts, pts[1:]):
        seg = math.dist(a, b)
        d = seg_dist(x, y, a, b)
        if d < best:
            t = 0.0 if seg == 0 else max(0.0, min(1.0, ((x - a[0]) * (b[0] - a[0]) + (y - a[1]) * (b[1] - a[1])) / seg ** 2))
            best, along = d, run + t * seg
        run += seg
    return along, run, best
