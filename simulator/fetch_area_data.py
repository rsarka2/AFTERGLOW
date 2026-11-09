"""Pre-download OpenStreetMap trail data for the simulator presets into ./data/
so the pitch demo does not depend on the public Overpass servers being up.

Usage (from the trailsar folder):  python fetch_area_data.py
Re-run any time; existing files are skipped.
"""
import json, math, os, sys, time, urllib.parse, urllib.request

R = 6371000.0
PRESETS = {  # name: (lon, lat) — must match PRESETS in index.html
    "Acadia Blackwoods": (-68.1990, 44.3125),
    "Quandary Peak": (-106.0850, 39.3900),
    "Devil's Head": (-105.1010, 39.2650),
    "Bear Lake RMNP": (-105.6455, 40.3122),
    "Clingmans Dome": (-83.4960, 35.5568),
    "Happy Isles": (-119.5578, 37.7322),
    "Pinkham Notch": (-71.2530, 44.2571),
    "Hurricane Ridge": (-123.4985, 47.9695),
    "The Grotto": (-112.9508, 37.2593),
}
RADII_KM = [2, 3, 4]
SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]


def offset(lon, lat, dx, dy):
    return (lon + dx / (R * math.cos(math.radians(lat))) / (math.pi / 180), lat + dy / R / (math.pi / 180))


def query(b):
    bb = f"{b[1]},{b[0]},{b[3]},{b[2]}"
    return (f'[out:json][timeout:60];(way["highway"~"^(path|footway|track|bridleway|steps)$"]({bb});'
            f'node["highway"="trailhead"]({bb});node["amenity"="parking"]({bb});way["amenity"="parking"]({bb});'
            f'node["tourism"~"^(viewpoint|camp_site|picnic_site|alpine_hut|wilderness_hut)$"]({bb});'
            f'node["natural"~"^(peak|waterfall|spring|saddle)$"]({bb});node["waterway"="waterfall"]({bb}););out geom;')


def main():
    radii = [int(a) for a in sys.argv[1:]] or RADII_KM
    os.makedirs(os.path.join(os.path.dirname(__file__) or ".", "data"), exist_ok=True)
    for name, (lon, lat) in PRESETS.items():
        for rk in radii:
            r = rk * 1000
            sw, ne = offset(lon, lat, -r, -r), offset(lon, lat, r, r)
            b = (sw[0], sw[1], ne[0], ne[1])
            key = ",".join(f"{v:.4f}" for v in b)
            path = os.path.join(os.path.dirname(__file__) or ".", "data", f"osm-{key}.json")
            if os.path.exists(path):
                print(f"skip  {name} {rk} km (exists)")
                continue
            body = urllib.parse.urlencode({"data": query(b)}).encode()
            for url in SERVERS:
                try:
                    req = urllib.request.Request(url, data=body, headers={"User-Agent": "TrailSAR-sim/0.3"})
                    with urllib.request.urlopen(req, timeout=90) as resp:
                        txt = resp.read().decode()
                    json.loads(txt)
                    open(path, "w", encoding="utf-8").write(txt)
                    print(f"saved {name} {rk} km  ({len(txt)//1024} KB) via {urllib.parse.urlparse(url).netloc}")
                    break
                except Exception as e:  # try next mirror
                    print(f"  {urllib.parse.urlparse(url).netloc} failed for {name} {rk} km: {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()
