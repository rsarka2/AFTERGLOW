"""Latitude/longitude <-> local metres around the mission origin.

Same flat-earth formula the plan uses; good to well under a metre over a few km.
x = metres east, y = metres north.
"""
import math

M_PER_DEG = 111_320.0


def to_xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    x = (lon - lon0) * M_PER_DEG * math.cos(math.radians(lat0))
    y = (lat - lat0) * M_PER_DEG
    return x, y


def to_latlon(x: float, y: float, lat0: float, lon0: float) -> tuple[float, float]:
    lat = lat0 + y / M_PER_DEG
    lon = lon0 + x / (M_PER_DEG * math.cos(math.radians(lat0)))
    return lat, lon
