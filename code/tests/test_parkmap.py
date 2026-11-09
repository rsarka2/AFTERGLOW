"""parkmap geometry (what geofence.py will use), both map profiles, and config profile parity."""
import math
import tomllib

import pytest

from afterglow import config
from afterglow.parkmap import Line, ParkMap, Zone, point_in_ring, seg_dist

INDOOR = config.load(config.DEFAULT)
PARK = config.load(config.DEFAULT.parent / "park.toml")


def square(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def toy():
    return ParkMap([Line("t1", "Trail", "trail", [(0, 0), (10, 0)]),
                    Line("r1", "Road", "road", [(0, 20), (10, 20)])],
                   [Zone("park", "Park", "park", square(-5, -5, 15, 25)),
                    Zone("camp_1", "Camp", "campground", square(2, 5, 6, 9))])


def test_seg_dist():
    assert seg_dist(5, 3, (0, 0), (10, 0)) == pytest.approx(3)  # beside the middle
    assert seg_dist(-3, 4, (0, 0), (10, 0)) == pytest.approx(5)  # past an end
    assert seg_dist(1, 1, (0, 0), (0, 0)) == pytest.approx(math.sqrt(2))  # zero-length segment


def test_point_in_ring():
    ring = square(0, 0, 4, 4)
    assert point_in_ring(2, 2, ring)
    assert not point_in_ring(5, 2, ring)
    assert not point_in_ring(2, -1, ring)


def test_dist_to_trail_kinds():
    pm = toy()
    assert pm.dist_to_trail(5, 4) == pytest.approx(4)
    assert pm.dist_to_trail(5, 18) == pytest.approx(2)  # the road counts by default...
    assert pm.dist_to_trail(5, 18, kinds=("trail",)) == pytest.approx(18)  # ...unless excluded
    assert pm.dist_to_trail(5, 18, kinds=("ski",)) == math.inf


def test_zone_at_prefers_the_campground():
    pm = toy()
    assert pm.zone_at(4, 7).id == "camp_1"
    assert pm.zone_at(12, 12).id == "park"
    assert pm.zone_at(100, 100) is None
    assert pm.in_kind(4, 7, "campground") and not pm.in_kind(12, 12, "campground")
    assert pm.bounds() == (-5, -5, 15, 25)


def test_indoor_map():
    pm = ParkMap.load(INDOOR)
    assert pm.dist_to_trail(2.0, 6.0) == pytest.approx(6.0, abs=0.05)  # the trail runs along y = 0
    assert pm.zone_at(7.0, -2.5).kind == "campground"
    assert pm.bounds() == pytest.approx((-2, -5, 11, 7), abs=0.05)


def test_park_map():
    pm = ParkMap.load(PARK)
    assert {ln.kind for ln in pm.lines} == {"trail", "road"}
    assert pm.zone_at(0, 0).name == "Blackwoods Campground"  # the origin is the campground
    assert "OpenStreetMap" in pm.credit  # ODbL: keep the credit on the map
    x0, y0, x1, y1 = pm.bounds()
    for ln in pm.lines:  # clipped to the park box
        for x, y in ln.pts:
            assert x0 - 0.5 <= x <= x1 + 0.5 and y0 - 0.5 <= y <= y1 + 0.5
    named = {ln.name for ln in pm.lines}
    assert {"Ocean Path", "Quarry Trail", "Otter Cove Trail"} <= named


def test_park_phones_are_where_park_toml_says():
    pm = ParkMap.load(PARK)
    off = {p["name"]: pm.dist_to_trail(p["x"], p["y"]) for p in PARK["sim"]["phones"] if "x" in p}
    assert off["dead_iphone"] > PARK["rules"]["offtrail_m"]
    assert off["airtag_lost"] > PARK["rules"]["offtrail_m"]


def keys(path):
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return {(table, k) for table, body in raw.items() if table != "waypoints" for k in body}


def test_profiles_have_the_same_keys():
    # A key added to one profile and not the other breaks modules run under the other one.
    a, b = keys(config.DEFAULT), keys(config.DEFAULT.parent / "park.toml")
    assert a == b, f"only in afterglow.toml: {sorted(a - b)}; only in park.toml: {sorted(b - a)}"


def test_resolve_is_relative_to_the_config_file(tmp_path):
    p = tmp_path / "x.toml"
    p.write_text("[rules]\n")
    assert config.resolve(config.load(p), "data/a.geojson") == tmp_path / "data" / "a.geojson"
