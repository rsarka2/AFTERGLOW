# TrailSAR mission simulator

A browser simulation of the search drone in real parks and forests: it plans a sweep along the busiest trails at closing time, listens for the beacons lost or dead phones keep sending (Apple Find My, Google Find Hub, Samsung lost mode), pins a heard beacon down, then drops to 25 m and checks with thermal + mmWave before proposing a pin to the ranger, who confirms or clears it. It is separate from the pod software in `code/`: everything here is simulated and runs in one HTML page.

## Run it

```
cd simulator
python -m http.server 8765
```
Then open http://localhost:8765, pick a park and press **▶ Run mission**.

It needs internet for the map, satellite and terrain tiles. Trail data for the parks below is bundled in `data/`, so it doesn't depend on the public OpenStreetMap server.

## Parks

| In the list | What it is |
|---|---|
| AFTERGLOW · Acadia NP, Blackwoods | Same origin and the same three hidden test phones as `code/park.toml` (the live map) |
| CASE STUDY · Quandary Peak (CO) | The Jan 2026 night rescue, replayed: sunset sweep, snow, −10 °C, hiker ~300 m off-trail (a what-if; the reports give no exact position). Shows how much earlier the drone could flag him than the real ~9 pm sighting |
| CASE STUDY · Devil's Head, Pike NF (CO) | Douglas County's 2 lost hikers + dog |
| Rocky Mountain, Great Smoky Mountains, Yosemite, White Mountains, Olympic, Zion | A random scenario each time: a hiker who drifted off a busy trail, an injured hiker near a viewpoint, a decoy AirTag |

## What the drone does

1. **Plans** a route along the busiest and riskiest trails from the nearest trailhead (traffic is estimated from OpenStreetMap: trailheads, parking, viewpoints, peaks, trail type). It flies straight across gaps between trail networks, follows the terrain at 40 m and respects the 120 m (400 ft) limit.
2. **Listens** on the way. On a hit it leaves the route and pins the beacon down from the signal strengths it hears (and the silences). Homing is capped at 8 minutes.
3. **Checks** at 25 m with thermal + mmWave. Heat or motion → "likely person", a pin proposed to the ranger to confirm (the drone never confirms). No heat → "likely device, ranger review" (never auto-dismissed).
4. **Searches off-trail** where lost hikers usually end up (a probability map from thousands of simulated lost-hiker walks downhill from busy trails), then swaps batteries once and carries on.

## Results (fast-forwarded test runs, Nov 7)

57 of 68 hidden people proposed as likely people across 9 parks (4 runs each); no decoy was ever proposed as a person; a mission that finds everyone takes about 15–35 simulated minutes.

## Not real yet

The radio, thermal and mmWave numbers are assumptions (−110 dBm receiver, 6 dB body loss, thermal and mmWave hit rates). Replace them with ME 2's range test and the pod's measured values. Terrain is a ~10–30 m elevation model without trees. A beacon hit is not proof a person is present.

## Files

- `index.html`: the whole simulator (MapLibre GL JS 4.7.1 from unpkg; the pod's dashboard vendors its own copy).
- `data/osm-*.json`: bundled trail data per park, © OpenStreetMap contributors (ODbL).
- `fetch_area_data.py`: re-downloads that data, or adds a park. Add the park to `PRESETS` in both this script and `index.html`, then run `python fetch_area_data.py 3`.
