# Live map feed (`web/livemap.js`)

The live bus drawn on a 3D terrain map, with labels on the map and a built-in key. It only **draws**: no buttons, no commands. The dashboard owns every click, so the drone still never decides.

## Use it

It's the map inside the one dashboard (the operator TV, `tv.js`); there's no page of its own. As a component:

```html
<link rel="stylesheet" href="vendor/maplibre-gl-4.7.1/maplibre-gl.css">
<script src="vendor/maplibre-gl-4.7.1/maplibre-gl.js"></script>
```
```js
import { LiveMap } from './livemap.js';

const lm = new LiveMap(el, meta, { onPick: (pin) => setSel(pin) });   // meta = GET /api/map; el needs a height
lm.update(store.state, { sel, layers, follow });                        // whenever store.js changes
lm.flyTo(x, y);  lm.fit();  lm.setLayer('coverage', false);  lm.destroy();
```

- `layers` keys: `coverage`, `probability`, `trail`, `tracks`, `roads`, `heard` and `labels`.
- Constructor options: `onPick(pin)` (a pin was clicked), `key`, `tools`, `status`, `labels` (default `true`; `key` also takes `'open'` or `'closed'`).
- `mountLiveMap(el, opts)` does the fetch, the `store.js` connection and the updates in one call.

To try it without the pod or Mosquitto: `python tools/local_stack.py --config park.toml --speed 10`, then open `http://localhost:8081`.

## What each thing on the map means

| On the map | What it is | Comes from |
|---|---|---|
| Tan line | Trail | the profile's trails GeoJSON (`/api/map`) |
| Grey line | Road | same |
| Orange dashed area | Campground. Pins wait until a beacon's 95% circle is clear of it | zones GeoJSON |
| Green dashed outline | Park / map frame | zones GeoJSON |
| Blue block + "POD · 15 m" label | The pod, raised to its height over the ground | `ag/pose` |
| Blue line | Where the pod has flown | `ag/pose` (the dashboard's breadcrumb trail) |
| Blue circle around the pod | How far it can hear a beacon (`[locate] detect_m`) | config |
| Small dots, blue → yellow → red | A beacon advert heard **at that spot**, weak → strong. It marks where the pod was, not the beacon. Beacons riding on the pod (`with_drone`) are left out. Hover one for type and dBm | `ag/obs/ble` + the pose at that moment |
| Light-blue cells | Ground already covered | `ag/coverage` `covered` |
| Yellow → red cells | Where a beacon probably is | `ag/coverage` `p` |
| Dashed circle | A track: the tracker's 95% circle for one beacon. Grey = heard only, yellow = localized, orange = pinned | `ag/track` |
| "T3 · Find My · ±19 m" | Label of a located track that isn't a pin yet | `ag/track` |
| Coloured chip "P1" + "Find My · 58 m off trail · NEW" | A pin: a possible person for the ranger. Colour = priority (1 red, 2 orange, 3 yellow, 4 grey). Text = beacon type · off-trail distance · status. Pulses while investigating or confirmed; faded once cleared. Hover for confidence and radius; click calls `onPick` | `ag/target` |
| Purple dashed line | A leg the planner proposes, waiting for the ranger | `ag/plan` |

Status bar (top left): the pose source (**LIVE**, **SIMULATED** or **REPLAY**, from `ag/pose` `src`), the profile name, the mode, the share covered, the open pins and the bus clock.

## Heights

With a GPS fix carrying `alt_m`, the pod's height over the ground is `alt_m` minus the terrain under it (AWS Terrain Tiles, about 10 to 30 m resolution, no trees). Without it (waypoints indoors, `fake_bus.py`), the label shows `z` as reported, "15 m up".

## Online and offline

The basemap, satellite imagery and terrain load from the internet. With no internet, the map shows a plain background and still draws everything from the bus and `/api/map`. MapLibre itself is vendored (`web/vendor/maplibre-gl-4.7.1/`), with no CDN.
