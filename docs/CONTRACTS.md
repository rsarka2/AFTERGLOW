# Contracts: how the programs talk to each other

Agree on this file at kickoff and don't change it casually. Several people build against it at once, so a silent change costs hours. If you must change something, change `code/afterglow/contracts.py` and this file in the same commit, and tell everyone.

The topics come from the plan doc's contract table. The drone build (FPV, GPS module, camera person check, park modes) adds a few. Those are marked **new** or **changed**.

## Ground rules

- **Message bus:** Mosquitto on the Pi, `localhost:1883`. Every message is one JSON object (UTF-8). Every topic starts with `ag/`.
- **Retained topics:** only `ag/coverage` and `ag/mode` (`contracts.RETAINED`). The broker keeps the latest of each, so a module or dashboard that starts late gets it at once. Nothing else is retained.
  - An empty retained message deletes one; `Bus` ignores it. `fake_bus.py` deletes its fake `ag/coverage` when it stops.
  - To delete one by hand, for example a stale grid from the other profile: `mosquitto_pub -r -n -t ag/coverage`.
- **Time:** `t` is Unix time in milliseconds. The Pi stamps it when a message arrives. The ESP32s keep no clock.
- **Position:** every module works in local metres.
  - `x` is metres east and `y` is metres north of the mission origin (`origin_lat`, `origin_lon` in `afterglow.toml`).
  - Only `position.py` (in) and `cot_out.py` (out) convert to and from latitude and longitude, both using `afterglow/geo.py`.
  - The pod and the laptop must use **the same origin**, so it lives in the shared config file.
- **Indoors** (taped park on the venue floor), the origin is taped waypoint 0. Waypoint coordinates come from the `[waypoints]` table in the config.
- **Profiles:** `afterglow.toml` is the demo profile (indoor, the real pod). `park.toml` is the park-scale profile: real Acadia trails, laptop only, simulated, with its own origin at Blackwoods Campground. Every module takes `--config`. The two files must have the same keys (`tests/test_parkmap.py`).
- **Device IDs:** `id` is a salted hash of the device address, 8 hex characters. `bridge.py` makes it. The salt changes on every `new_search`.
- **Track IDs:** `"T1"`, `"T2"` and so on, assigned by `tracker.py`. A dashboard pin is an `ag/target` record.

## Privacy rules (non-negotiable)

- Never write a raw Bluetooth or Wi-Fi address to disk, a log or the screen. Only `bridge.py` sees raw addresses, and it hashes them before publishing.
- Never try to decode or decrypt beacon contents. We only read the type marker and the signal strength.
- Scanners listen passively and never transmit. The SOS Wi-Fi page is the only thing that transmits, and it has an off switch (`ag/ctrl` `sos_off`).
- Call it a patrol or search-assist system, never surveillance. A human confirms every pin.

## Topics

| Topic | Posted by | Example | Meaning |
|---|---|---|---|
| `ag/obs/ble` | bridge.py | `{"t":1731000000123,"src":"omni","id":"7f3a9c21","cls":"findmy","rssi":-71}` | One Bluetooth advert heard. `cls` is in the table below |
| `ag/obs/wifi` | bridge.py | `{"t":…,"id":"c01d22aa","rand":true,"rssi":-80,"ch":6}` | One Wi-Fi probe request heard |
| `ag/sos` | bridge.py | `{"t":…,"hurt":true,"walk":false,"count":2,"rssi":-55}` | An answer from the SOS page (tier 2). geofence.py attaches it to a pin; see "SOS answers" below |
| `ag/pose` (**changed**) | position.py | `{"t":…,"x":412.3,"y":-88.0,"z":12.0,"lat":44.3386,"lon":-68.2733,"alt_m":61.2,"course":87.5,"speed_mps":4.1,"fix":1,"sats":14,"src":"gps"}` | Where the pod is. `z` is height above take-off in metres. `src` is `gps`, `waypoint` or `imu` (**new**) (the real pod), `replay` (`tools/replay.py`) or `sim` (`tools/fake_bus.py`, `sim.py`); the dashboard shows a REPLAY or SIMULATED badge for the last two. `imu` (`[pose] source = "imu"`): the pod is carried along the straight `[imu] line_from` → `line_to`, placed by the BNO086's step count × `stride_m`; it publishes at `publish_hz` even standing still, and a `waypoint` tap re-anchors it to that waypoint's spot on the line (that pose carries `n`). With `imu`, optional (**new**) `dist_m` (metres walked along the line), `steps` (since the walk's zero or last re-anchor) and `heading` (degrees from north, for show only: the line's bearing plus the turn since the first reading; missing if unknown). Optional `roll` and `pitch` (degrees) from the IMU. Optional `drone` (**new**): which drone sent it, once there is more than one; locate.py never joins two drones' poses into one line. Missing means the one pod |
| `ag/pass` (**changed**) | tracker.py | `{"t":…,"track":"T1","peak_x":4.2,"peak_y":7.9,"sharp":6.0,"n":14}` | One finished pass: where the pod heard a track loudest; `sharp` is dB above the pass median, optional `n` the sightings in it. Posted by tracker.py, not locate, because the tracker already splits sightings into passes. None for `with_drone` tracks |
| `ag/coverage` (**new**) | locate.py | `{"t":…,"x0":-50,"y0":-50,"cell_m":5,"nx":60,"ny":40,"covered":[0,1,1,…],"p":[0,12,255,…],"pct":0.62}` | Coverage and probability grids for the dashboard, about 1 Hz, **retained** so a reloaded dashboard gets it at once. See "Coverage grid" below. Stays on the pod, never goes over LoRa |
| `ag/track` (**changed**) | tracker.py | `{"t":…,"track":"T1","ids":["7f3a9c21","e9910b4c"],"cls":"findmy","x":18.5,"y":9.0,"r95":12.0,"state":"localized","passes":2,"last_s":4,"with_drone":false,"baseline":false,"c":0.62}` | Best estimate for one beacon, about once a second. `ids` lists the rotating keys followed through key changes (the last 4). `with_drone` marks beacons that move with the drone, such as our own phones. Optional `baseline` (**new**): heard during the baseline, so grey and never pinned. Optional `c` (**new**): confidence 0–1, which geofence copies onto the pin. See "Tracks" below |
| `ag/target` (**changed**) | geofence.py | `{"t":…,"pin":"P3","track":"T1","x":18.5,"y":9.0,"lat":44.3387,"lon":-68.2731,"r95":12.0,"c":0.82,"prio":2,"cls":"findmy","state":"localized","last_s":4,"off_trail_m":46,"zone":"trail_corridor","rule":"day_offtrail","status":"new","flags":{"sos":false,"thermal":true,"radar":false,"camera":true},"br":null}` | **The one record** the dashboard, LoRa alerts and ATAK use. One per pin. `state` and `last_s` come from the track (see "How geofence fills a pin"). Optional `sos` (`{"hurt":true,"walk":false,"count":2}`) once an SOS answer is attached |
| `ag/thermal` (**changed**) | thermal.py | `{"t":…,"max_c":33.8,"hot":true,"col":120,"row":80,"w":256,"h":192,"conf":0.8,"src":"ht203u"}` | The warm spot in the HT-203U's frame, about 4 times a second (see "Thermal spot"). Frames for the panel are image files over HTTP, not MQTT (see "Images over HTTP") |
| `ag/vision` (**new**, stretch) | camera.py (stretch: person detection) | `{"t":…,"kind":"person","conf":0.82,"box":[1820,940,96,140],"snap":"snaps/1731000000123.jpg","track":"T1"}` | Camera check. **Nothing publishes it yet:** person detection became a stretch goal on Sat 7 Nov, and camera.py only writes the live view. `kind` is `person` or `colour` (the Monviso-style colour check). `snap` is also its URL path on port 8080 |
| `ag/radar` | radar.py | `{"t":…,"present":true,"range_m":1.1,"br":14,"hr":72}` | Breathing and heart rate, landed only (tier 3) |
| `ag/plan` (**new**) | planner.py | `{"t":…,"id":"L7","kind":"homing","legs":[{"to_x":20,"to_y":5,"heading_deg":72,"dist_m":35,"alt_m":8}],"status":"proposed","pin":"P3"}` | Legs for the pilot. `kind` is `patrol`, `grid` or `homing`; `status` is `proposed`, `accepted`, `rejected`, `done` or `dropped` (**changed**). Optional `pin` (homing) or `trail` (patrol). The ranger accepts on the dashboard; the observer reads heading and distance to the pilot. See "Plans" below |
| `ag/mode` (**changed**) | dashboard.py | `{"t":…,"mode":"investigate"}` | `patrol`, `night`, `investigate` or `search` (the four modes in the build doc). **Retained**, so a restarted geofence or a second dashboard knows the mode at once. With no mode yet, geofence assumes `patrol` |
| `ag/ctrl` | dashboard.py | `{"t":…,"cmd":"ack","pin":"P3"}` | Operator commands, listed below |
| `ag/sys` (**new**) | health (Utility) | `{"t":…,"cpu_c":61.2,"throttled":"0x0","gps_fix":true,"lora":true,"disk_free_gb":41.0,"batt_pct":null}` | Pod health for the dashboard. Optional `batt_pct` (0–100, or `null`): `null` for now, because the pod runs on a USB power bank the Pi can't read. On a drone, a voltage divider from the LiPo into a XIAO analog pin would fill it (tier 3). `lora`: lora_out is running and not silenced (`false` after `lora_off`). Optional (**new**) `omni`, `wifi`, `thermal`, `camera`: that part's data arrived within `[health] stale_s` (`ag/obs/ble`, `ag/obs/wifi`, `ag/thermal`, `live/camera.jpg`). `gps_fix` is `null` while `[serial] gps = ""`. health.py never opens a radio, port or camera |
| `ag/alert` (**new**) | lora_rx.py (**laptop only**) | `{"t":…,"rssi":-71,"snr":9.5,"version":2,"kind":1,"pin":"P3","x":18.0,"y":9.0,"r95":12.0,"state":"localized","flags":{…},"c":0.82,"prio":2,"cls":"findmy","br":null,"sos":{…},"last_s":4,"battery":null,"off_trail_m":46}` | One LoRa alert heard by the laptop radio: `alert.decode`'s fields plus `t`, `rssi` and `snr`. `kind` is the alert type (1 track, 2 SOS, 3 confirmed person, 4 heartbeat). Lives on the laptop's own broker; cot_out.py and the LoRa fallback view read it |

### `cls` values (beacon type)

| `cls` | What it is | Reported |
|---|---|---|
| `findmy` | Apple offline-finding beacon **separated from its owner**: manufacturer data starting `4C 00 12 19` (the full 25-byte payload). A dead, off or cut-off iPhone, or a lost AirTag | Always |
| `findhub` | Google Find Hub accessory frame (service data `0xFEAA`) | Always |
| `samsung_off` | Samsung lost-mode advert (service `0xFD69`) | Always |
| `ref` | Our reference beacon: manufacturer data `FF FF 'A' 'G'` | Always |
| `apple_other` | Other Apple frames from live devices, including `4C 00 12 02` (offline finding while **near its owner**: every passing hiker's iPhone) | Only with `REPORT_ALL` |
| `other` | Anything else | Only with `REPORT_ALL` |

### `ag/ctrl` commands

| `cmd` | Extra fields | Who acts on it |
|---|---|---|
| `new_search` | none | bridge.py (new salt), tracker.py (clears every track; track numbers keep counting), locate.py (clears both grids), planner.py (drops every open plan, then proposes again for the mode) |
| `baseline_start`, `baseline_stop` | none | tracker.py: beacons heard between them get `baseline: true` on `ag/track` and show grey. Send `new_search` first: it changes every id |
| `ack`, `investigate`, `clear`, `confirm` | `pin` | geofence.py updates the pin `status`, but only by the allowed changes under "Pin states and priority"; planner.py proposes homing once the pin's `ag/target` status turns `investigating` (so an ignored click proposes nothing) |
| `accept_leg`, `reject_leg` | `id` | planner.py: `accept_leg` on a proposed plan; `reject_leg` on a proposed or accepted one (the ranger stops it) |
| `waypoint` | `n` (taped waypoint number) | position.py, indoors |
| `lora_off`, `lora_on` | none | lora_out.py. Turn LoRa off if the pilot's control link drops |
| `sos_on`, `sos_off` | none | bridge.py forwards to the WIFI board |

### Pin states and priority

- **Track state:**
  - `heard`: somewhere in range.
  - `localized`: 95% within 25 m outdoors (5 m indoors).
  - `pinned`: within 10 m outdoors (2 m indoors).
  - `confirmed`: the ranger confirmed a person. It appears only on `ag/target`; `ag/track` never carries it.
  - The radii live in the config's `[track]` table (`localized_r95_m`, `pinned_r95_m`): the indoor numbers in `afterglow.toml`, the outdoor ones in `park.toml`. Set them from ME 2's range test. These are placeholders.
- **Pin `status`:** `new` → `acknowledged` → `investigating` → `confirmed` or `cleared`. geofence.py accepts only these changes, and ignores and logs any other command:
  - `ack`: from `new`.
  - `investigate`: from `new` or `acknowledged`.
  - `clear`: from anything except `confirmed` or `cleared`.
  - `confirm`: **only from `investigating`**.
  - The dashboard greys out the same buttons, but its server doesn't check a pin's status, so geofence is the real guard.
- **`rule`** (why it's a pin):
  - `day_offtrail`: farther off-trail than `offtrail_m`, seen on 2 or more passes, and not in a campground.
  - `night_outside_camp`: any beacon outside campgrounds after closing.
  - `search`: any `findmy`, `findhub` or `samsung_off` beacon during an active search.
  - `sos`: an SOS answer with no pin under investigation (see "SOS answers").
- **`prio`**, which sets the ATAK colour:
  - 1, red: confirmed, or an SOS that says hurt.
  - 2, orange: pinned and c ≥ 0.8.
  - 3, yellow: localized, pinned short of orange, or 0.5 ≤ c < 0.8.
  - 4, grey: heard only.

### Tracks

How tracker.py fills `ag/track`. Its numbers live in the config's `[track]` table and are placeholders until ME 2's range test.

- **Which beacons:** `findmy`, `findhub`, `samsung_off`, `ref`, and Wi-Fi with a fixed address (`cls` `wifi`). Live devices (`apple_other`, `other`) and randomized Wi-Fi addresses are never tracked. An id needs 3 sightings to become a track; one heard fewer times is forgotten after 30 minutes without a sighting.
- **Position:** each sighting is tagged with the latest `ag/pose`. `x`, `y` and `r95` come from a grid fit of the RSSI model (`rssi_p0`, `rssi_n`, `rssi_sigma`); `r95` is the radius around `x`, `y` that holds 95% of the probability.
- **`state`:** `pinned` if `r95` ≤ `pinned_r95_m`, `localized` if `r95` ≤ `localized_r95_m`, else `heard`. Never `confirmed`.
- **Passes:** a pass ends when the signal has peaked, fallen and risen again, or after `pass_gap_s` without a sighting. Each finished pass goes out on `ag/pass`.
- **Key rotation:** a new id joins an old track of the same `cls` whose id went quiet just before it (within `rotate_window_s`, heard from within `rotate_match_m`) at a matching strength, but only if the match is clear. If the key rotates while the pod is away, the new id gets a new track.
- **`with_drone`:** set once a beacon, heard without a break, is at `with_drone_rssi` or louder at both ends of a stretch where the pod moved `with_drone_move_m`. Cleared once its median RSSI falls below `with_drone_rssi`, so a fixed beacon wrongly marked while the pod passed over it comes back. Its `x`, `y` is the pod, `state` `heard` and `c` 0. Set `with_drone_rssi` from the range test, well above what a beacon at carry height reads.
- **`c`:** 0.2 to 0.95, mostly how tight `r95` is, then how many passes back it up.
- **Track numbers** wrap after `T255`. If all 255 are in use, the track quiet longest is dropped to make room. `T0` is the SOS-only pin.

### How geofence fills a pin

- **When a track becomes a pin:** the rule for the current mode must hold for `pin_hold_s` (the config's `[rules]` table). Early estimates jump about, and our own phones take a few seconds to be marked `with_drone`.
  - In `investigate` mode, the rule of the last other mode applies. With no mode yet, patrol.
  - Day and night also need the track `localized` or `pinned`, with its 95% circle clear of every campground. A beacon that may be in a campground waits until the estimate tightens. `search` needs no position.
  - Classes: `findmy`, `findhub`, `samsung_off` and `ref` (`search`: not `ref`). Never Wi-Fi.
  - Never a track with `with_drone` or `baseline` set.
- **A pin stays.** It keeps following its track when the mode changes or the estimate moves back onto the trail. Only the ranger clears it. A cleared pin is published once, then frozen, and its track never gets another pin.
  - While its track is `with_drone` (picked up, or the pod right over it), the pin keeps its last place instead of riding along with the pod. It follows again once the mark clears.
- `state`, `last_s` and `c` are copied from the latest `ag/track` for that track.
- A click, an SOS answer or the thermal flag restamps `t` and adds the same time to `last_s`, so when it was last heard (`t − last_s`) doesn't move.
- The one exception: `state` becomes `confirmed` only when the ranger presses Confirm (pin `status` `confirmed`). The tracker never sets it. That keeps the rule that the drone never decides.
- **`new_search`:** geofence unlinks every track, because the tracker starts new ones. It keeps the old pins for the ranger, and pin numbers keep counting. The next SOS answer opens a new SOS-only pin.
- **Restarts:**
  - geofence saves its pins to `[rules] pins_file` (under `runs/`, never committed). At start-up it reloads them, if they're newer than `pins_max_age_min`, and republishes them, so it never hands out `P1` twice. `--fresh` starts over and empties the file at once, so a lora_out restarted after it doesn't reload the old pins.
  - If a linked track comes back with none of its earlier `ids` (the tracker restarted, or wrapped past `T255`), the pin stops following it.

### SOS answers

geofence.py listens to `ag/sos`, not lora_out.py, because priority 1 and `flags.sos` live on `ag/target`, and the dashboard and ATAK need them too.

- The SOS page only runs during Investigate, so the answer goes on the pin whose `status` is `investigating` (the one set to investigating last, if there are several).
- geofence adds `"sos":{"hurt":…,"walk":…,"count":…}`, sets `flags.sos`, and sets `prio` 1 if the person says they're hurt.
- If no pin is being investigated, geofence opens an SOS-only pin at the pod's position (from `ag/pose`), with `track` `"T0"`, `rule` `"sos"` and `status` `new`. Its other fields:
  - `cls` `"wifi"`, because the answer came over the SOS Wi-Fi page;
  - `c` 0, `state` `heard` and `last_s` 0;
  - `r95` is `sos_r95_m` from the config's `[rules]` table: the SOS page's Wi-Fi range, a placeholder until it's measured;
  - `prio` 1 if the person says they're hurt, otherwise 2.
- Later answers with no pin under investigation update that same SOS-only pin (the latest answer, at the pod's latest position) until it's cleared or confirmed, or a `new_search` starts.

### Coverage grid

- `ag/coverage` carries two grids of `nx × ny` cells, each a flat list read row by row. Row 0 is the south edge, and each row runs west to east.
- Cell `i` is row `i // nx`, column `i % nx`. Its south-west corner is at `x0 + col·cell_m`, `y0 + row·cell_m`.
- `covered` is 0 or 1: whether a drone has passed within `detect_m` of that cell's centre. Two poses of one drone in a row count the straight line between them, unless the jump is too fast to fly (a GPS glitch).
- `p` is 0–255: the beacon probability heatmap. Each `ag/track` adds a Gaussian blob at its `x`, `y` with 95% inside `r95` (but at least a cell wide), peaking at 255 × `c`. Where blobs overlap, the higher one wins. Tracks with `with_drone` or `baseline` are left out, and so is a track not updated for `stale_s`.
- `pct` is the share of cells covered.
- `new_search` clears both grids.
- The `[locate]` table holds `cell_m`, `detect_m`, `stale_s` and `area_m`, the box the grids cover (empty means the map's `park` zone). The grid is capped at 20,000 cells: past that, `cell_m` grows. `cell_m` and `detect_m` are placeholders until ME 2's range test.
- The 95% circle needs no topic of its own, because `x`, `y` and `r95` are already on `ag/track` and `ag/target`.

### Thermal spot

- thermal.py sends one `ag/thermal` per frame, about 4 times a second (`[thermal] fps`), and writes the same frame to `live/thermal.png` with a cyan box round a hot spot.
- `col` and `row` are the spot's pixel in a `w` × `h` frame: 256 × 192, or 192 × 256 after a 90° `rotate`. The spot is the warmest 3 × 3 patch, so one noisy pixel can't be it.
- **With temperatures:** `max_c` is the spot in °C. The spot is the warmest patch inside the body band (`hot_min_c`–`hot_max_c`, 30–45 °C), or the warmest overall if nothing is in the band. `hot` means it's in the band and at least `hot_rise_c` (5 °C) above the frame's median, so an engine or a sun-baked rock never counts. Nor does its rim, which cools through the band: patches within 5 pixels of anything above `hot_max_c` are skipped.
- **Picture only** (no temperatures): `max_c` is `null`, and `hot` means the spot stands at least `hot_z` spreads (median absolute deviations) above the frame's median.
- **Height** (**new**): `hot` is always false, and `conf` 0, while the latest `ag/pose` `z` is above `[thermal] max_alt_m`, because a person is then too few pixels to tell apart. Before the first pose, any height counts.
- **The pin's flag** (**new**): geofence sets `flags.thermal` on the pin under investigation (the one set to investigating last) when `ag/thermal` is hot and the pod is within `[rules] thermal_near_m` of that pin's 95% circle. It's set once and stays set: a sign for the ranger, never a confirm. Like a click, it restamps `t` and keeps `t − last_s`.
- `conf` is 0–1: 0.5 right at the `hot` threshold, 1.0 at twice it.
- `src` is `ht203u` (the camera) or `fake` (`thermal.py --fake`). The panel should mark fake heat.
- The HT-203U does give temperatures on Linux (checked Sat 7 Nov: raw 16-bit heat in its 256×392 YUYV frames), but readers must still handle a `null` `max_c`. All the thresholds are placeholders in the `[thermal]` table.

### Plans

planner.py only proposes. Only the ranger's `accept_leg` makes a plan `accepted`, and nothing flies itself: the observer reads each leg to the pilot.

- **Legs:** each has `to_x`, `to_y` (local metres), `heading_deg` (compass: 0 north, 90 east), `dist_m` and `alt_m`. Each leg is measured from the end of the one before; `legs[0]` from the pod's latest pose.
- **What it proposes:**
  - `homing`, when a pin's `ag/target` status turns `investigating`: a crossing pass from the pod straight over the pin to `cross_m` past it, then 6-leg orbits round the pin, first at its `r95` (kept between `min_orbit_m` and `cross_m`), then at half that. Carries `pin`.
  - `grid`, when `ag/mode` turns `search`: east-west lanes at most `lane_m` apart over the grids' area (`[locate] area_m`, else the park zone), from the corner nearest the pod.
  - `patrol`, when `ag/mode` turns `patrol` or `night`: the top-ranked trail from the pod (`trails.py`), `patrol_m` of it (0 = all), as legs within `arrive_m` of the trail. Carries `trail`.
  - Nothing before the first `ag/mode` **and** the first `ag/pose`. A mode or pin that comes before the first pose (the retained `ag/mode` always does) is planned when the pose arrives, the newest one only.
  - The same mode again proposes again only if it's a new click (a new `t`, not the retained copy after a reconnect) and no plan of its kind is open, for example after the ranger rejected it.
- **Status:** `proposed` → `accepted` or `rejected` (clicks only); `accepted` → `rejected` (the ranger stops it) or `done` (the pod reached the last leg). A plan is `dropped` when a newer proposal replaces it (only one is `proposed` at a time), the ranger accepts another (only one is `accepted`), its pin stops being investigated (homing), or a `new_search` starts (which then proposes again for the current mode).
- **Clock:** a plan's `t` is the pod's: planner.py follows the `t` of `ag/pose` only, because the dashboard stamps `ag/ctrl` and `ag/mode` with the laptop's clock, and the Pi's can be minutes off it.
- **While open:** `legs[0]` is re-measured from each `ag/pose` and sent at most once a second. On an accepted plan, each leg the pod gets within `arrive_m` of is taken off the front. Open plans are sent again every 5 s, because `ag/plan` isn't retained.
- `id` is `L1`, `L2`… and starts again at `L1` when planner.py restarts. The `[plan]` table holds `alt_m`, `homing_alt_m`, `cross_m`, `min_orbit_m`, `lane_m`, `patrol_m` and `arrive_m` (placeholders until ME 2's range test; keep `arrive_m` under `min_orbit_m`).

## Map files

The config's `[rules]` `trails_geojson` and `zones_geojson` name them, relative to the config file (open them with `config.resolve`). They're stored in GeoJSON lon/lat; read them in metres through `afterglow/parkmap.py` (`ParkMap.load(cfg)`), which geofence, the dashboard and the fake bus all share.

| File | Geometry | Properties |
|---|---|---|
| trails | `LineString` | `id`, `name`, and optional `kind`: `trail` (the default) or `road` |
| zones | `Polygon` | `id`, `name`, `kind`: `park` (the map frame) or `campground` |

- `dist_to_trail` counts both trails and roads unless you pass `kinds`.
- `zone_at` returns a campground before the park outline.
- The park files come from OpenStreetMap through `tools/make_park_map.py` and carry an `attribution`. Show "© OpenStreetMap contributors" on any map drawn from them.
- The indoor files come from `tools/make_indoor_map.py`.

## Images over HTTP

dashboard.py serves everything on one port (`[http] port`, 8080), so there are no cross-origin problems. thermal.py and camera.py only write files and need no web code.

| URL on `http://<pi>:8080` | File | Written by |
|---|---|---|
| `/live/thermal.png` | `live/thermal.png` | thermal.py, about 4 times a second |
| `/live/camera.jpg` | `live/camera.jpg` | camera.py, `[camera] fps` times a second (2), 1280×720 JPEG. The time it was taken is drawn bottom left (with FAKE from `--fake`), so the panel needs no MQTT message to show a frozen feed |
| `/snaps/<t>.jpg` | `snaps/<t>.jpg` (the path in `ag/vision` `snap`) | stretch: camera.py's person detector, one per detection |

- Write each live frame to a temporary file and rename it over the old one, so a reader never gets half a frame.
- **Dashboard: scale each picture to fit its panel** (CSS `object-fit: contain`), never a fixed size. `[camera] rotate` or `[thermal] rotate` of 90 or 270 turns the picture on its side (the camera's becomes 720×1280).
- The folders are `[http] live_dir` and `snaps_dir`, relative to `code/`. Neither is committed.
- Each file comes with `Last-Modified` (the file's time) and `Date`, both from the server's clock, so a page can tell a frozen feed without trusting its own clock. The operator TV calls a picture over 5 s old frozen.
- `python -m afterglow.dashboard` serves these URLs. Without it, `python -m http.server 8080` run from `code/` serves the same paths.
- **Laptop dashboard (new):** the files are on the Pi, so the Pi runs `afterglow@dashboard` just to serve them. A laptop dashboard with `--pod <pi-ip>` (the default when `--broker` names another machine; `--pod off` to use its own folders) fetches `/live/` and `/snaps/` from the Pi and serves them on the same URLs. It moves `Last-Modified` onto the laptop's clock (laptop now minus the picture's age from the Pi's `Date` and `Last-Modified`), so a Pi clock that's off doesn't read as frozen. Pi unreachable: `502`; no such picture: `404`.

## Dashboard HTTP

`dashboard.py` serves the page and this API on the same port. The browser is its only client today; the LoRa fallback view (tier 3) would reuse it.

| Method and path | What |
|---|---|
| `GET /api/map` | Once per page load: the profile (`profile`, `name`, `origin`, `waypoints`, `radii`, `offtrail_m`, `detect_m`, `cell_m`) and the map in metres (`map`: `bounds`, `lines`, `zones`, `credit`), from `parkmap.py` |
| `GET /api/events` | Server-Sent Events. `state` (the full state) comes first on every connect and reconnect. Then a `msg` (`{"topic":…,"msg":…}`) for each bus message, and a `status` (`broker`, `bus_t`, `clients`, `counts`) about every 2 s |
| `GET /api/state` | The same full state as one JSON object, for debugging |
| `POST /api/ctrl` | A click: `{"cmd":"ack","pin":"P1"}`, `{"cmd":"waypoint","n":3}` and so on. The server keeps only `cmd`, `pin`, `id` and `n`, stamps `t`, runs `validate()` and checks that pin commands carry `pin`, leg commands carry `id` and `waypoint` carries a configured `n`. Then it publishes to `ag/ctrl`. Answers `{"ok":true,"msg":…}`, or `400` / `503` with `{"ok":false,"problems":[…]}`. The body must be sent as `Content-Type: application/json` (else `415`), so another web page can't post a click |
| `POST /api/mode` | `{"mode":"night"}`, published to `ag/mode` the same way, **retained** |
| `GET /api/case` | The open missing-person case for trail pathing (`tv.html`): `{"case", "start", "ranking", "trail", "route"}`. `case` is `null` when none is open; otherwise `name`, `age`, `details`, `carrying`, `expect_cls`, `car_park`, `trail`, `opened_t`. `start` is `{x, y, label}` (the car park, else the park entrance); `ranking` the most likely trails (`name`, `pct`, `dist_m`, `length_m`, `pop`); `route` the chosen trail as `[[x, y], …]` |
| `POST /api/case` | Open the case, or update the open one with the fields given: `name` (needed), `age`, `details`, `carrying` (`iphone`, `airtag`, `android`, `samsung`, `unknown`), `car_park` (a `[search] car_parks` name, or `""`), `trail`. Answers the same shape as `GET` plus `ok`, or `400` with `problems` |
| `POST /api/case/close` | Close the case |

- The full state holds the latest pose per drone (`drone` field, or `"pod"`), each drone's path, and the latest `ag/target` per pin, `ag/track` per track and `ag/plan` per id. It also holds a summary per heard id with the last 60 raw observations, the latest message on every other topic, and per-topic counts.
- **The dashboard's clock** (`bus_t`) is the latest pose's `t`, or the newest `t` before any pose arrives. Ages on the page are measured against it, because fake and replayed time isn't wall time.
- The dashboard publishes only when someone clicks. It never sends `ag/ctrl` or `ag/mode` by itself.
- **The case is laptop-only personal data** (`afterglow/case.py`). It's saved to `[search] case_file` under `runs/` (never committed), sent to browsers as an extra SSE `case` event (on connect and on every change), and **never** published on the bus or sent over LoRa. The sim reads the file to know which trail to fly.
- `/api/map` also carries `search`: `car_parks`, `entrance_xy` and `corridor_m` from the `[search]` table (SAMPLE values until the published trail-use data is in).
- **No login:** anyone who can reach port 8080 can click. Keep the pod and the laptop on the team's own hotspot, never the venue Wi-Fi.

## Serial lines from the ESP32 boards

One JSON object per line, `\n` terminated, 921600 baud, 8N1, on the XIAO's `Serial1` at D9 (RX, GPIO8) and D10 (TX, GPIO9): `Serial1.begin(921600, SERIAL_8N1, D9, D10)`. `Serial` stays the USB port for debug output. Raw addresses appear **only** on these wires.

```
OMNI  → Pi  {"type":"ble","addr":"aa:bb:cc:dd:ee:ff","cls":"findmy","rssi":-71}
WIFI  → Pi  {"type":"wifi","addr":"da:a1:19:00:00:01","rand":true,"rssi":-80,"ch":6}
WIFI  → Pi  {"type":"sos","hurt":true,"walk":false,"count":2,"rssi":-55}
Pi → WIFI  {"cmd":"sos_on"}   {"cmd":"sos_off"}
```

- A `ble` line carries only the class and the RSSI, never payload bytes. The hardware reference's proposed `key` field (the payload hex) would break the privacy rules, so it isn't used. Its `mac` is our `addr`, and its `t` isn't needed, because the Pi stamps time.
- The bridge drops lines that aren't valid JSON and counts them in its log.

### Laptop LoRa receiver (ADALM-PLUTO → laptop)

**Changed (Sat 7 Nov):** we have only one RFM95W, so the laptop receives with an ADALM-PLUTO SDR, not RFM95W + XIAO #3. The line format below is unchanged and stays the boundary:
- `tools/pluto_rx.py` runs gr-lora_sdr on the Pluto's samples, using the config's `[lora]` table, and prints one `rx` line per decoded packet with a good CRC, on stdout. It may print `rssi` and `snr` as `null` (an SDR has no calibrated RSSI), and `ag/alert` allows that.
- `lora_rx.py --stdin` reads those lines and publishes `ag/alert`, exactly as it did from XIAO #3's USB port. It doesn't filter: `lora_rx.py` drops other teams' packets.
- The old path (XIAO #3 + a second RFM95W, `firmware/lora_rx/`, read at 115200 from `/dev/cu.usbmodem*`) still works if a second RFM95W turns up.

```
receiver → laptop  {"type":"rx","rssi":-71,"snr":9.5,"hex":"a2a10700<32 hex chars: the alert>"}
receiver → laptop  {"type":"cfg","freq":915.000,"sf":7,"bw":125000,"cr":5,"pre":8}
receiver → laptop  {"type":"err","msg":"no radio: …"}
laptop → XIAO #3  ?        (asks for the cfg line; XIAO #3 only)
```

- `hex` is the whole packet: the 4-byte RadioHead header (to, from, id, flags), then the payload.
- `lora_rx.py` sends `?` when it opens the port, and stops if the `cfg` reply differs from the config's `[lora]` table.

## LoRa alert: 16 bytes

Sent by `lora_out.py` from the pod's RFM95W as plain LoRa (open the radio with `afterglow/radio.py`). Each packet is a 4-byte RadioHead header (to `laptop_node`, from `pod_node`, id, flags), then the 16 bytes:
- every `[lora] repeat_s` (5) seconds per live pin (every status except `cleared`);
- at once when a pin is new, or its alert type, `state`, `prio`, flags or SOS answer change, but at most once a second per pin. A move or a status click waits for the repeat;
- priority 1 first, then changed pins, then by pin number. On a channel too busy for every pin (a dozen pins at SF10), whatever has been due longest goes before the rest, so every pin and the heartbeat slow down and none goes silent. There are no acks: the repeats cover a lost packet.

lora_out.py stays a plain encoder of `ag/target`. These details:
- **`last_s`:** it sends `last_s + (now − t)/1000`, so the repeats keep counting even when geofence hasn't republished the pin.
- **Battery (byte 14):** it reads `batt_pct` from the latest `ag/sys` and passes it to `alert.encode` as `battery`. `null` goes out as 255, "unknown".
- **Type:** a confirmed pin goes out as type 3, even an SOS-only one. Otherwise a pin with `rule` `"sos"` goes out as type 2 (SOS), and anything else as type 1. An SOS attached to a normal pin rides in byte 11 and the SOS flag.
- **Version:** `[lora] alert_version`: 1 (decimetres) in the indoor profile, 2 (metres) in `park.toml`. lora_rx.py reads it from byte 0, so the laptop needs no setting.
- **Heartbeat** (type 4, **new**): every `repeat_s`, pin 0, `x` and `y` set to the pod's latest `ag/pose` (0, 0 before the first one), `battery` as above. cot_out.py places the pod's ATAK marker from it.
- **`lora_off`** stops every packet, heartbeat included, until `lora_on`. lora_out remembers it in `[lora] off_file` (under `runs/`), so a restarted lora_out stays silent. `lora_on` resends every live pin at once. `ag/ctrl` isn't retained, so a `lora_off` sent while lora_out isn't running is lost: to start silent, create the off_file by hand.
- **Restarts:** `ag/target` isn't retained, so at start-up lora_out reloads geofence's `[rules] pins_file` (if newer than `pins_max_age_min`). Pins that geofence no longer republishes, such as those from before a `new_search`, would otherwise never go out again.
- **One owner:** while lora_out runs, it alone drives the radio. Stop it before `check_hw.py --only lora`, and the health module must not open the radio to fill `ag/sys` `lora`.

`lora_rx.py` on the laptop decodes it onto `ag/alert`, and `cot_out.py` turns that into an ATAK marker. `code/afterglow/alert.py` implements and tests this. Use it, don't re-type it.

`struct.pack('<BBhhBBBBBBHBB', …)`

| Byte | Field | Encoding |
|---|---|---|
| 0 | version and type | High nibble version; low nibble type: 1 track, 2 SOS, 3 confirmed person, 4 heartbeat |
| 1 | pin (**changed**, was the track) | The dashboard's pin number, 1–255 (`P7` → 7; past 255 it wraps, `P256` → 1). 0 = heartbeat. Pin numbers are unique, unlike tracks: every SOS-only pin is `T0` |
| 2–3 | x | int16. **v1: decimetres** (indoor demo, ±3.2 km). **v2: metres** (park scale, ±32 km) |
| 4–5 | y | int16, same scale as x |
| 6 | r95 | uint8. v1 decimetres (to 25.5 m); v2 metres (to 255 m) |
| 7 | state and flags | Low 4 bits state (0 heard, 1 localized, 2 pinned, 3 confirmed). Bit 4 SOS, bit 5 thermal, bit 6 radar, bit 7 camera |
| 8 | confidence | 0–255 = c × 255 |
| 9 | priority and class | High nibble prio 1–4; low nibble class: 0 other, 1 findmy, 2 findhub, 3 samsung_off, 4 ref, 5 wifi, 6 apple_other (never sent in practice: live devices are never tracked or pinned) |
| 10 | breaths per minute | 0 = unknown |
| 11 | SOS answer | Bit 0 hurt, bit 1 can walk, bits 2–7 headcount (0–63) |
| 12–13 | seconds since last heard | uint16 |
| 14 | pod battery | Percent; 255 = unknown |
| 15 | off-trail distance | Metres, 0–254; 255 = unknown |

Changes from the plan doc's layout: version 2 adds a metres scale, because a park-scale pin can be farther than 25.5 m from where it was heard. Bit 7 and byte 15 were the gas-hazard fields; the drone build has no gas sensor, so they now carry the camera flag and the off-trail distance.

**Radio settings:** the config's `[lora]` table: 915.0 MHz, SF7, 125 kHz, coding rate 4/5, preamble 8, explicit header, CRC on, sync word left at the default (0x12, the private-network word; gr-lora_sdr's default matches). Both ends must match exactly.
- `tools/pluto_rx.py` reads the numbers from the config itself. `firmware/lora_rx/lora_rx.ino` (the old XIAO #3 path) copies them, and `tests/test_lora_rx.py` fails if they drift apart.
- SF 9–10 trades speed for range. Change it in both places after ME 2's range test.

**Filtering:** there's no private channel, so `lora_rx.py` keeps a packet only if all of these hold:
- it's addressed to `laptop_node` and comes from `pod_node`;
- it's 16 bytes;
- it carries a known alert version.

Everything else is counted as another team's traffic and dropped.

**Not encrypted:** anyone on 915 MHz can read an alert. It carries a pin's position and class, never a device address or ID.

## ATAK (Cursor-on-Target)

- **Input:** `cot_out.py` reads `ag/alert` on the laptop. **Fallback** (`--wifi <pi-ip>`): if the radio link fails (there's no spare XIAO), it reads `ag/target` and `ag/pose` from the Pi's broker over Wi-Fi instead. They have the same fields, already in metres.
- `cot_out.py` converts x and y back to latitude and longitude around the shared origin (`geo.to_latlon`):
  - `lat = lat0 + y / 111320`
  - `lon = lon0 + x / (111320 · cos lat0)`
- It sends plain CoT XML over UDP to each address in the config's `[atak] send_to`. The default is ATAK's multicast group, `239.2.3.1:6969`; this is what pytak would send, without the extra package. If the router drops multicast, add the phone's own address (`udp://<phone-ip>:4242`, ATAK's usual unicast input) or pass `--to`.
- **iTAK (iPhone)** takes multicast for SA only, so cot_out is also a tiny TAK server: plain TCP on `[atak] tcp_port` (8087; 0 = off), no SSL, no login. In iTAK, add a server with the laptop's IP, port 8087, protocol TCP. Every connected phone gets every event, and cot_out answers its pings (`t-x-c-t` → `t-x-c-t-r`). ATAK can connect the same way.
- **Pins:** one spot marker (`b-m-p-s-m`) per pin, uid `afterglow.P3`, coloured by priority: 1 red, 2 orange, 3 yellow, 4 grey. Both inputs use the same uid, so switching to `--wifi` moves the same markers.
  - Callsign: the pin number, as on the dashboard (`P3`). An SOS-only pin is `P3 SOS`.
  - Remarks, for example: `priority 2 · conf 82% · Apple Find My (separated) · 46 m off trail · ±12 m · localized · last heard 14:32:05 · LoRa -71 dBm, SNR 9.5`. A confirmed pin starts with `PERSON CONFIRMED by the ranger`, and a pin with an SOS answer with `SOS: hurt, can't walk, 2 people`. With `--wifi`, they add the status (`status investigating`).
  - The 95% circle is a drawn circle (`u-d-c-c`, uid `afterglow.P3.r95`) with radius `r95`, unless `circles = false`.
- **The pod:** a friendly drone marker (`a-f-A-M-H-Q`, uid `afterglow.pod`), placed from the LoRa heartbeat, or from `ag/pose` with `--wifi` (at most every `pod_every_s`).
- **Stale and cleared:** every marker goes stale `[atak] stale_s` after its last update.
  - Over LoRa, `lora_out.py` repeats the live pins. A cleared pin just stops coming, because the alert has no status, so it goes stale.
  - With `--wifi`, cot_out repeats every live pin every `repeat_s`, as `lora_out.py` would. It stops once nothing has come from the Pi's broker for 3 × `repeat_s` (ag/pose comes at 5 Hz), so the markers go stale. It removes a cleared pin from ATAK at once (`t-x-d-d`).
- Still to check in ATAK on a real phone: the marker type, the colours, and whether the circle's `major`/`minor` is a radius (as sent) or a diameter.
