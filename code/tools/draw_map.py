#!/usr/bin/env python3
"""Draw the trails and zones GeoJSON as a small PNG in local metres, to check the map by eye.

    python3 tools/draw_map.py                      # writes docs/maps/indoor_map.png
    python3 tools/draw_map.py --out /tmp/map.png
    python3 tools/draw_map.py --no-waypoints       # outdoor maps
    python3 tools/draw_map.py --config park.toml --phones --out ../docs/maps/park_map.png

Reads the files and origin named in the config (afterglow.toml by default). Laptop only:
needs matplotlib.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from afterglow import config, geo  # noqa: E402

DEFAULT_OUT = config.DEFAULT.parent.parent / "docs" / "maps" / "indoor_map.png"
ZONE_STYLE = {  # kind -> (fill, edge, legend label)
    "park": ("#eef4e8", "#7d9a6c", "Park outline"),
    "campground": ("#f7d8a3", "#c27c25", "Campground"),
}
TRAIL = "#8a5a2b"
ROAD = "#9a9a9a"


def features_xy(path, lat0, lon0):
    """Yield (properties, geometry type, list of rings or lines in metres)."""
    doc = json.loads(Path(path).read_text())
    for f in doc["features"]:
        g = f["geometry"]
        parts = g["coordinates"] if g["type"] in ("Polygon", "MultiLineString") else [g["coordinates"]]
        xy = [[geo.to_xy(lat, lon, lat0, lon0) for lon, lat in part] for part in parts]
        yield f["properties"], g["type"], xy


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--no-waypoints", action="store_true")
    ap.add_argument("--phones", action="store_true", help="also draw the fake bus's hidden phones and search box")
    a = ap.parse_args()

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon
    from matplotlib.ticker import MultipleLocator

    cfg = config.load(a.config)
    lat0, lon0 = cfg["mission"]["origin_lat"], cfg["mission"]["origin_lon"]

    fig, ax = plt.subplots(figsize=(6.4, 5.4), dpi=120)
    seen = set()
    zones = list(features_xy(config.resolve(cfg, cfg["rules"]["zones_geojson"]), lat0, lon0))
    big = any(abs(v) > 200 for _, _, rings in zones for ring in rings for p in ring for v in p)  # park scale
    zones.sort(key=lambda z: z[0].get("kind") != "park")  # park outline underneath
    for props, _, rings in zones:
        fill, edge, label = ZONE_STYLE.get(props.get("kind"), ("#dde3ee", "#5a6a8a", props.get("kind")))
        ax.add_patch(Polygon(rings[0], closed=True, facecolor=fill, edgecolor=edge, lw=1.5,
                             label=None if label in seen else label, zorder=1))
        seen.add(label)

    trails_path = config.resolve(cfg, cfg["rules"]["trails_geojson"])
    for props, _, lines in features_xy(trails_path, lat0, lon0):
        road = props.get("kind") == "road"
        label = "Road" if road else "Trail"
        for line in lines:
            xs, ys = zip(*line)
            ax.plot(xs, ys, color=ROAD if road else TRAIL, lw=1.2 if big else 5, solid_capstyle="round",
                    zorder=2, label=None if label in seen else label)
            seen.add(label)

    phones = [p for p in cfg.get("sim", {}).get("phones", []) if "x" in p]
    if a.phones and phones:
        ax.scatter([p["x"] for p in phones], [p["y"] for p in phones], marker="x", color="#c0392b",
                   zorder=6, label="Hidden phone (fake bus)")
        for p in phones:
            ax.annotate(p["name"], (p["x"], p["y"]), xytext=(4, 4), textcoords="offset points", fontsize=6.5)
    area = cfg.get("sim", {}).get("area_m")
    if a.phones and area:
        x0, y0, x1, y1 = area
        ax.add_patch(Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], closed=True, fill=False,
                             edgecolor="#2c6fbb", ls="--", lw=1, zorder=3, label="Fake bus search box"))

    if not a.no_waypoints and cfg["waypoints"]:
        wp = sorted(cfg["waypoints"].items())
        xs, ys = zip(*(p for _, p in wp))
        ax.plot(xs, ys, color="#555", lw=1, ls="--", zorder=3, label="Carry route (waypoint order)")
        ax.scatter(xs, ys, s=230, facecolor="white", edgecolor="#222", lw=1.2, zorder=4)
        for n, (x, y) in wp:
            ax.text(x, y, str(n), ha="center", va="center", fontsize=8, weight="bold", zorder=5)
            ax.text(x, y + 0.55, f"({x:g}, {y:g})", ha="center", va="bottom", fontsize=6.5,
                    color="#444", zorder=5, bbox=dict(fc="white", ec="none", pad=0.6, alpha=0.8))

    ax.set_aspect("equal")
    ax.autoscale_view()
    span = max(ax.get_xlim()[1] - ax.get_xlim()[0], ax.get_ylim()[1] - ax.get_ylim()[0])
    if span < 60:  # indoors: grid every metre, labels every 2 m
        ax.xaxis.set_major_locator(MultipleLocator(2))
        ax.yaxis.set_major_locator(MultipleLocator(2))
        ax.xaxis.set_minor_locator(MultipleLocator(1))
        ax.yaxis.set_minor_locator(MultipleLocator(1))
    ax.grid(which="both", color="#000", alpha=0.07, lw=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(labelsize=8)
    ax.set_xlabel("x: metres east of " + ("the origin" if big else "waypoint 0"), fontsize=8)
    ax.set_ylabel("y: metres north", fontsize=8)
    ax.set_title(f"AFTERGLOW map, origin {lat0:.4f}, {lon0:.4f}", fontsize=10)
    credit = json.loads(trails_path.read_text()).get("attribution")
    if credit:
        ax.text(0.99, 0.01, credit, transform=ax.transAxes, ha="right", va="bottom", fontsize=6, color="#666")
    ax.annotate("N", xy=(0.95, 0.97), xytext=(0.95, 0.86), xycoords="axes fraction",
                ha="center", va="center", fontsize=9, weight="bold",
                arrowprops=dict(arrowstyle="-|>", color="#222", lw=1.2))
    ax.legend(loc="upper left", fontsize=7, framealpha=0.9)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
