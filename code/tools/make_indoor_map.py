#!/usr/bin/env python3
"""Write data/trails.geojson, data/zones.geojson and data/building.geojson for the taped indoor park.

    python3 tools/make_indoor_map.py            # write both files
    python3 tools/make_indoor_map.py --print    # show them, write nothing

The layout is in local metres: the trail follows taped waypoints 0-3 from the
config's [waypoints] table, and the campground and park outline are set below.
The files are stored in standard GeoJSON lon/lat around the config origin, the
same format as an OSM or CalTopo export, so the outdoor maps load the same way.
Rerun this after changing the origin or the waypoints. tests/test_map_files.py
fails if you forget.

Feature properties (what geofence.py, the dashboard and sim.py can rely on):
    trails.geojson  LineString  {"id": "trail_1", "name": "..."}
    zones.geojson   Polygon     {"id": "camp_1", "name": "...", "kind": "campground" | "park"}
To read them in metres, use geo.to_xy(lat, lon, origin_lat, origin_lon) on each
[lon, lat] point (GeoJSON puts lon first).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from afterglow import config, geo  # noqa: E402

TRAIL_WAYPOINTS = [0, 1, 2, 3]  # the taped trail runs along these waypoints

# Rectangles in metres from waypoint 0: (x_min, y_min, x_max, y_max).
# The campground sits south of the trail, close enough that the carry route hears
# a tag inside it (night-mode demo). The park outline frames the dashboard map.
ZONES = [
    ("camp_1", "Campground", "campground", (5.0, -4.0, 9.0, -1.0)),
    ("park", "Taped park", "park", (-2.0, -5.0, 11.0, 7.0)),
]

# The building the taped park is in, for the TV's Live tab: 60 State Street, Boston, in metres from
# waypoint 0 (the origin). APPROXIMATE: drawn by hand (no map data offline), about 62 x 54 m with
# State Street along the south side. Swap in the real outline (OSM way, or traced on geojson.io).
BUILDING = ("bldg_1", "60 State Street", [(-30.0, -28.0), (32.0, -28.0), (32.0, 18.0), (24.0, 26.0),
                                         (-30.0, 26.0), (-30.0, -28.0)])


def lonlat(x, y, lat0, lon0):
    lat, lon = geo.to_latlon(x, y, lat0, lon0)
    return [round(lon, 7), round(lat, 7)]  # 7 decimals is about 1 cm


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]  # closed ring, counter-clockwise


def build(cfg):
    m = cfg["mission"]
    lat0, lon0 = m["origin_lat"], m["origin_lon"]
    if lat0 == 0.0 and lon0 == 0.0:
        sys.exit("origin_lat and origin_lon are 0.0; set the origin in afterglow.toml first")
    wp = cfg["waypoints"]
    trails = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "properties": {"id": "trail_1", "name": "Taped trail"},
        "geometry": {"type": "LineString",
                     "coordinates": [lonlat(*wp[n], lat0, lon0) for n in TRAIL_WAYPOINTS]},
    }]}
    zones = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "properties": {"id": zid, "name": name, "kind": kind},
        "geometry": {"type": "Polygon",
                     "coordinates": [[lonlat(x, y, lat0, lon0) for x, y in rect(*box)]]},
    } for zid, name, kind, box in ZONES]}
    bid, bname, ring = BUILDING
    building = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "properties": {"id": bid, "name": bname, "kind": "building", "approximate": True},
        "geometry": {"type": "Polygon", "coordinates": [[lonlat(x, y, lat0, lon0) for x, y in ring]]},
    }]}
    return trails, zones, building


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--print", action="store_true", help="print instead of writing")
    a = ap.parse_args()

    cfg = config.load(a.config)
    trails, zones, building = build(cfg)
    out = [(cfg["rules"]["trails_geojson"], trails), (cfg["rules"]["zones_geojson"], zones)]
    if cfg["rules"].get("building_geojson"):
        out.append((cfg["rules"]["building_geojson"], building))
    for rel, doc in out:
        text = json.dumps(doc, indent=2) + "\n"
        if a.print:
            print(f"--- {rel}\n{text}")
            continue
        path = config.resolve(cfg, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
