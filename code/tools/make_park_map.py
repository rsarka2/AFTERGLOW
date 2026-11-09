#!/usr/bin/env python3
"""Write data/park_trails.geojson and data/park_zones.geojson for the park-scale profile
(park.toml) from OpenStreetMap: real trails, roads and the campground around Blackwoods
Campground in Acadia.

    python3 tools/make_park_map.py --config park.toml               # fetch from Overpass, write both
    python3 tools/make_park_map.py --config park.toml --raw osm.json  # use a saved Overpass answer
    python3 tools/make_park_map.py --config park.toml --save-raw osm.json

Needs internet unless --raw is given. The output is committed, so the dashboard and the
fake bus work offline. Map data (c) OpenStreetMap contributors, ODbL; keep the credit on
the dashboard map.

Feature properties (a superset of make_indoor_map.py's):
    park_trails.geojson  LineString  {"id": "osm_<way>_<n>", "name": "...", "kind": "trail" | "road"}
    park_zones.geojson   Polygon     {"id": "camp_1" | "park", "name": "...", "kind": "campground" | "park"}
The "park" rectangle (PARK_BOX below, metres from the origin) frames the map; lines are
clipped to it.
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from afterglow import config, geo  # noqa: E402

PARK_BOX = (-700.0, -600.0, 1500.0, 1500.0)  # x_min, y_min, x_max, y_max in metres from the origin
PARK_NAME = "Acadia NP, Blackwoods"
TRAIL = {"path", "footway", "track", "bridleway", "steps"}
ROAD = {"primary", "secondary", "tertiary", "unclassified", "residential", "service"}
OVERPASS = "https://overpass-api.de/api/interpreter"
CREDIT = "Map data (c) OpenStreetMap contributors, ODbL"


def lonlat(x, y, lat0, lon0):
    lat, lon = geo.to_latlon(x, y, lat0, lon0)
    return [round(lon, 7), round(lat, 7)]


def bbox_latlon(lat0, lon0):
    x0, y0, x1, y1 = PARK_BOX
    s, w = geo.to_latlon(x0, y0, lat0, lon0)
    n, e = geo.to_latlon(x1, y1, lat0, lon0)
    return s, w, n, e


def fetch(lat0, lon0):
    s, w, n, e = bbox_latlon(lat0, lon0)
    b = f"{s:.5f},{w:.5f},{n:.5f},{e:.5f}"
    q = (f'[out:json][timeout:80];('
         f'way["highway"~"^({"|".join(sorted(TRAIL | ROAD))})$"]({b});'
         f'way["tourism"="camp_site"]({b}););out geom tags;')
    req = urllib.request.Request(OVERPASS, data=urllib.parse.urlencode({"data": q}).encode(),
                                 headers={"User-Agent": "afterglow-hackathon-map/0.1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def clip(pts, box):
    """Clip a polyline (list of (x, y)) to a rectangle; returns a list of pieces (Liang-Barsky)."""
    x0, y0, x1, y1 = box
    pieces, cur = [], []
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        dx, dy = bx - ax, by - ay
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
            if p == 0:
                if q < 0:
                    t0, t1 = 1.0, 0.0
                    break
            else:
                r = q / p
                if p < 0:
                    t0 = max(t0, r)
                else:
                    t1 = min(t1, r)
        if t0 > t1:  # segment entirely outside
            if len(cur) >= 2:
                pieces.append(cur)
            cur = []
            continue
        a = (ax + t0 * dx, ay + t0 * dy)
        b = (ax + t1 * dx, ay + t1 * dy)
        if not cur:
            cur = [a]
        cur.append(b)
        if t1 < 1.0:  # leaves the box here
            pieces.append(cur)
            cur = []
    if len(cur) >= 2:
        pieces.append(cur)
    return pieces


def build(osm, lat0, lon0):
    lines, camps = [], []
    for e in osm["elements"]:
        tags, geom = e.get("tags", {}), e.get("geometry") or []
        if e.get("type") != "way" or len(geom) < 2:
            continue
        if tags.get("access") in ("private", "no"):
            continue
        xy = [geo.to_xy(p["lat"], p["lon"], lat0, lon0) for p in geom]
        if tags.get("tourism") == "camp_site":
            if xy[0] == xy[-1] and len(xy) >= 4:
                camps.append((tags.get("name", "Campground"), e["id"], xy))
            continue
        hw = tags.get("highway")
        kind = "trail" if hw in TRAIL else "road" if hw in ROAD else None
        if kind is None:
            continue
        for n, piece in enumerate(clip(xy, PARK_BOX)):
            lines.append({"type": "Feature",
                          "properties": {"id": f"osm_{e['id']}_{n}", "name": tags.get("name", ""), "kind": kind},
                          "geometry": {"type": "LineString",
                                       "coordinates": [lonlat(x, y, lat0, lon0) for x, y in piece]}})
    lines.sort(key=lambda f: (f["properties"]["kind"] != "trail", f["properties"]["id"]))

    x0, y0, x1, y1 = PARK_BOX
    zones = [{"type": "Feature",
              "properties": {"id": "park", "name": PARK_NAME, "kind": "park"},
              "geometry": {"type": "Polygon", "coordinates": [[lonlat(x, y, lat0, lon0) for x, y in
                                                               [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]]}}]
    for i, (name, _, ring) in enumerate(sorted(camps, key=lambda c: c[1]), 1):
        zones.append({"type": "Feature",
                      "properties": {"id": f"camp_{i}", "name": name, "kind": "campground"},
                      "geometry": {"type": "Polygon", "coordinates": [[lonlat(x, y, lat0, lon0) for x, y in ring]]}})
    fc = lambda feats: {"type": "FeatureCollection", "attribution": CREDIT, "features": feats}  # noqa: E731
    return fc(lines), fc(zones)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="the park profile, e.g. park.toml")
    ap.add_argument("--raw", help="read a saved Overpass JSON answer instead of fetching")
    ap.add_argument("--save-raw", help="also save the Overpass answer here")
    a = ap.parse_args()

    cfg = config.load(a.config)
    lat0, lon0 = cfg["mission"]["origin_lat"], cfg["mission"]["origin_lon"]
    osm = json.loads(Path(a.raw).read_text()) if a.raw else fetch(lat0, lon0)
    if a.save_raw:
        Path(a.save_raw).write_text(json.dumps(osm))
    trails, zones = build(osm, lat0, lon0)
    for key, doc in (("trails_geojson", trails), ("zones_geojson", zones)):
        path = config.resolve(cfg, cfg["rules"][key])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, separators=(",", ":")) + "\n")
        print(f"wrote {path}: {len(doc['features'])} features")


if __name__ == "__main__":
    main()
