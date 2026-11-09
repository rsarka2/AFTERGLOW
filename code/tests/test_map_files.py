"""The committed map files must load and match the shared config (origin and waypoints)."""
import json

import pytest

from afterglow import config, geo

CFG = config.load()
BASE = config.DEFAULT.parent
LAT0, LON0 = CFG["mission"]["origin_lat"], CFG["mission"]["origin_lon"]


def load(key):
    return json.loads((BASE / CFG["rules"][key]).read_text())


def to_xy(pt):
    lon, lat = pt  # GeoJSON order is [lon, lat]
    return geo.to_xy(lat, lon, LAT0, LON0)


def test_origin_is_set():
    assert (LAT0, LON0) != (0.0, 0.0)


def test_trail_follows_taped_waypoints():
    doc = load("trails_geojson")
    assert doc["type"] == "FeatureCollection"
    trail = doc["features"][0]
    assert trail["geometry"]["type"] == "LineString"
    pts = [to_xy(p) for p in trail["geometry"]["coordinates"]]
    wps = [CFG["waypoints"][n] for n in (0, 1, 2, 3)]
    for (x, y), (wx, wy) in zip(pts, wps, strict=True):
        # Fails if the origin or waypoints changed without rerunning tools/make_indoor_map.py
        assert x == pytest.approx(wx, abs=0.05) and y == pytest.approx(wy, abs=0.05)


def test_zones_have_a_closed_campground():
    doc = load("zones_geojson")
    kinds = {f["properties"]["kind"] for f in doc["features"]}
    assert "campground" in kinds
    for f in doc["features"]:
        ring = f["geometry"]["coordinates"][0]
        assert f["geometry"]["type"] == "Polygon" and ring[0] == ring[-1] and len(ring) >= 4


def test_building_holds_the_taped_park():
    # The TV's Live tab draws this outline (60 State Street, approximate) around the waypoints.
    doc = load("building_geojson")
    f = doc["features"][0]
    ring = [to_xy(p) for p in f["geometry"]["coordinates"][0]]
    assert f["geometry"]["type"] == "Polygon" and ring[0] == ring[-1] and len(ring) >= 4
    xs, ys = [x for x, _ in ring], [y for _, y in ring]
    for wx, wy in CFG["waypoints"].values():
        assert min(xs) < wx < max(xs) and min(ys) < wy < max(ys)
