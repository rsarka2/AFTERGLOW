# Operator TV (`web/tv.html`)

The ops-room screen: big, calm, and only shows what needs a decision. It wraps the panels
around the live map (`web/livemap.js`, unchanged; see `docs/LIVEMAP.md`). Every button is a
ranger's click: nothing auto-confirms a ping or accepts a leg.

## Layout

A console look (graphite, one amber accent, colour only for status and priority) that always fits
the window exactly, at any size from 1280 px up:
- **Header:** logo, the location tabs, the four steps (a thin track), the mode buttons, Live, the clock.
- **Stage:** the map (Acadia) or the pictures and the hall walk (State St). Alerts sit in slim
  strips above it. A box on the map shows the sweep, the trail stretch, or the pilot's heading.
- **Rail** (right): the decision first (the ping waiting on the ranger, or the emergency), then
  the pings table, the case, the battery and the activity log.
- **Status bar:** SIMULATED / REPLAY / LIVE, the sim controls, the pod link, LoRa, the weather
  (simulated) and the battery, and the chime.

**Several new pings:** the decision card has a tab per new ping (and ‹ ›, or the `[` and `]` keys).
A tab only changes what the card and the map show; it never acknowledges anything.

**Health shows the battery alone** (`ag/sys` `batt_pct`): in the status bar, and as a card on the
live site. The link (pod Wi-Fi, broker) still shows in the status bar, because it decides the
LoRa-only banner and the locked buttons.

## Run it

```
cd code
.venv/bin/python tools/local_stack.py --config park.toml             # no broker needed; pause and speed on the TV
.venv/bin/python tools/local_stack.py --config park.toml --feeds     # plus fake camera and heat pictures
.venv/bin/python -m afterglow.dashboard --config park.toml            # or with Mosquitto + fake_bus.py
```

This is the one dashboard: `/` opens it (`/tv.html` works too). Open `http://localhost:8081` for Acadia (`park.toml`'s port) or `:8080` for 60 State St
(`afterglow.toml`). Click once anywhere so the browser allows the ping chime. `?force=lora` shows
the LoRa-only banner for the demo.

## Two locations

The tabs next to the logo switch between the sites in `[http] sites`: **Acadia NP** (the park
simulation, port 8081) and **60 State St** (the live demo, port 8080). Each is its own dashboard,
so run both for the tabs to work; a site that isn't running shows "not running". On the laptop:

```
.venv/bin/python tools/local_stack.py --config park.toml --feeds        # Acadia, :8081 (simulation)
.venv/bin/python -m afterglow.dashboard --broker <pi-ip>                 # State St, :8080 (the real pod)
```

### Acadia (the simulation)

The drone waits at **base** (Gorham Mountain lot, `[sim] base_xy`): on the ground, scanners off.
**Send out · lawn-mower sweep** on the banner launches it onto the lanes, drawn faint on the map
("Planned sweep"). Then, at 1×: the **false signal** (the dropped AirTag) pings about 1:15 after
launch and the **person** about a minute later (`--seed 6`; the scenario plays the same however
long it waited). Trail pathing, Investigate, the rescue route for the ground team and the
check-ins work as before. **Return to base** (status bar) flies it straight home and lands it. The
camera and heat pictures follow the simulation: the warm blob and the person show only while the
drone is over the person.

The status bar also has **pause**, **1× / 2× / 4×** and **Reset** (`POST /api/sim`). A paused sim
never shows as a lost link. Reset (click twice) goes back to time zero: drone at base, no pings,
no case, day sweep, and every open TV reloads.

### 60 State St (the live demo)

Indoors, so no map: the stage is the pod's **camera and heat pictures**, big, with **nearby
alerts** beside them (`nearby.html?compact=1`: the alert beacons around the pod (Find My, Find Hub,
Samsung lost mode, ref; no live phones or Wi-Fi), north up; a ring is
heard, distance only, a dot is located by tracker.py), then the **hall
walk**, a strip with one straight walk line (`[imu] line_from` to `line_to`, a mark every metre,
Base at the left, pings beside the line), then the live readings (warmest spot, Bluetooth scans,
lost devices heard, strongest, last LoRa alert). Carry the pod forward along the line;
position.py (`[pose] source = "imu"`) turns the BNO086's step count into metres along it, the
walked part fills in and the side shows "x of 10 m walked". Tapping waypoint 0
(`{"cmd":"waypoint","n":0}`) puts it back at the start.

Nothing here is simulated: no fake weather, no fake pictures. When the pod isn't reachable,
it says so: **No signal** on the camera and heat pictures (no picture, or one over a minute old),
"No signal from the pod" in the status bar and beside the hall walk.

**Pod battery** (health.py's `ag/sys`, every `[health] period_s`): the one health reading shown,
in the rail and at the bottom of the Live tab. Amber under 40 %, red under 20 %; "not measured"
when the pod runs on USB power.

## The map

`livemap.js`, seen straight from above: flat, no tilt or rotation, satellite imagery by default
(Satellite / Map toggle). To keep it readable, its beacon dots, track circles, heatmap, labels,
key and tools are switched off through its options, and heard-only or cleared pings are left
off the map (they stay in the ping list). On top we draw a pulsing drone marker, the trail
stretch being searched (amber, turning green as it's flown), the case's car park and a short
key that changes with the mode. The map frames the job: the search box in the sweeps, the
stretch in trail pathing, the drone and the ping in an emergency.

## The park scenario (`park.toml`)

Small on purpose, so it reads at a glance and runs in minutes:
- the sweep box is 300 × 600 m beside Gorham Mountain lot and Ocean Path (about 10 minutes at 1×);
- two things to find: a **false signal first**, a dropped AirTag 40 m off Ocean Path about
  130 m from the lot with nobody near it (the check says "No one here"), then the missing iPhone
  40 m off Ocean Path about 140 m further along, with the person beside it;
- the sweep lanes start at the north edge, by the car park (`[sim] lanes_from`), and trail
  pathing always starts where the trail passes the car;
- trail pathing searches the first 600 m of the chosen trail from where it passes the car
  (`[search] segment_m`), about 75 seconds at 1×. The big % in the box on the map counts it.

## Modes

The four buttons are the existing `ag/mode` values with new labels, so no contract changed:

| Button | `ag/mode` | Screen |
|---|---|---|
| Day sweep | `patrol` | Lawn-mower progress and what makes a ping (box on the map), pings, activity, "Open a case" |
| Night sweep | `night` | The same, dimmer; pings only outside the campground; thermal status |
| Trail pathing | `search` | The case (who, start point, carrying, description), how much of the stretch is searched (big %), the top 3 trails from the car (click one to search it), the next leg (Accept / Reject) |
| Emergency | `investigate` | "Read to pilot" heading and distance, the SOS answer, camera check, hand-off coordinates, Confirm / Clear |

Always in view: the mode buttons and the clock (bus time) in the header; the SIMULATED / REPLAY /
LIVE badge, the pod link, LoRa, the battery and the chime toggle in the status bar.

Alert strips (above the map) appear only when they matter:
- **LoRa only**: no pose for 10 s (the pod's Wi-Fi is down). Pings from `ag/alert` are listed,
  and every button is locked, because commands go over Wi-Fi.
- **SOS answered** (red): a ping carries an SOS answer. "Open emergency view" switches the mode.
- **New ping** (priority colour): the highest-priority ping still `new`, longest waiting first,
  with a waiting timer. It shows in the rail's decision card; the strip appears only when the
  rail is busy with something else (an emergency, or another ping you picked). Each new ping plays a chime; SOS and priority 1 play an urgent one.

## The four steps

A thin track in the header shows where the search is: **1 Search · 2 Signal · 3 Check · 4 Rescue**.

1. **Search**: a day or night sweep, or trail pathing for a case.
2. **Signal**: a ping appears (banner and chime).
3. **Check**: press **Investigate**. The ping is linked to the open case (or link one yourself
   with "Link to case" on any ping card). The drone flies to it and circles, and the card fills
   in live: drone over the ping, heat (warm or not), camera (person seen or not).
4. The card then suggests one of two buttons. **The ranger still presses it: the drone never decides.**
   - **4a, person found** (warm and seen): "Confirm person · start rescue" confirms the ping and
     opens Emergency, with the **rescue route** for the ground team drawn on the map (white dots):
     from the car park, along the trail that was searched, then off the trail by compass
     direction, with a walking time. Then **"Team on the way · back to scanning"** records that a
     ground team is sent and goes back to the sweep that was running before the case.
   - **Check-ins:** every `[search] checkin_s` (5 min) the drone should fly back over the person.
     The "Rescue under way" card counts down to the next one; when it's late, an amber
     **Check-in due** banner gives the pilot a heading and distance (the drone never decides: the
     pilot flies it). A pass within the check distance resets the timer. The fake pod flies the
     check-ins by itself and circles for `[sim] checkin_dwell_s`. "Person safe · close case" ends it.
   - **Show the rescue route** (on that card, or "Show route" on the banner) opens the route
     **without changing the mode**: the drone keeps sweeping. **← Back to the sweep** (or Esc)
     leaves it. In Emergency, once the team is sent, the same button switches back to the sweep.
   - **4b, no one here** (nothing warm and nobody seen after about 12 s over it): "Not a person ·
     continue search" clears the ping, and the drone goes back to its path.

In the simulation, phones with `person = true` in `[sim] phones` read warm and show a person
when the drone is close (stand-ins for thermal.py and a person detector, which isn't built). The park's missing iPhone has
one; the decoy doesn't. The weather and wind in the status bar are simulated too.

## Camera and heat pictures

The pod's live pictures, `live/camera.jpg` (camera.py) and `live/thermal.png` (thermal.py), served
by the dashboard. Each reloads once a second and only swaps in a frame that has finished
loading, so it never flickers. Pictures are scaled to fit, so a 90° `rotate` in config still fits.

- **Emergency:** the "Camera check" card shows both, the camera above the heat picture, with the
  warmest spot's temperature on the heat picture (`ag/thermal`; "picture only" when `max_c` is null).
  A person detector's snapshot (`ag/vision` `snap`, stretch) replaces the camera for a minute.
- **The other modes:** the **Pictures** switch on the map (Auto / On / Off, remembered per browser)
  puts them in the map's bottom-right corner. Auto shows both while the drone checks a ping
  (Investigate), the heat picture alone in a night sweep, and nothing otherwise.
- **Old pictures:** the server sends `Last-Modified` with each picture. Over 5 s old, the picture
  is dimmed with "Feed frozen" (the camera program stopped); if the server can't be reached it
  says "Feed lost". Neither changes anything else on the TV.

**Real cameras:** the pictures are written on the Pi. Run the dashboard on the Pi too
(`sudo systemctl enable --now afterglow@dashboard`), then on the laptop
`python -m afterglow.dashboard --broker <pi-ip>`: it fetches the pictures from the Pi's dashboard
(`--pod` to name another host, `--pod off` to use the laptop's own `live/`). Opening
`http://<pi-ip>:8080` straight from the Pi works too.

With no pod, `tools/local_stack.py --feeds` writes `camera.py --fake` and `thermal.py --fake`
pictures. They aren't tied to the simulation: the heat picture's blob comes and goes on its own
40 s loop, while the temperature shown comes from the fake pod's `ag/thermal`.

## The case

"Open a case" asks for name, age, what they look like, what they carry (sets the beacon to
expect, and flags matching pings) and the car park (or "not found": start from the park
entrance). Saving switches to trail pathing. The case is personal data: it stays on the
laptop in `runs/` (`[search] case_file`) and never goes on the bus or LoRa.

## Trail ranking (`afterglow/trails.py`)

`likelihood = popularity × exp(−distance from the start / near_m)`, normalised to 100 %. The
car parks, popularity per trail and `near_m` live in the profile's `[search]` table. **They are
SAMPLE numbers**: swap in the published trail-use data when we have it. Time of day isn't in
the score yet.

## In the simulation

While the mode is `search` and a case is open, `fake_bus.py` / `local_stack.py` fly the case's
trail (to the nearest point, then back and forth along it) instead of the lanes. `local_stack.py`
also runs planner.py, so the leg card shows real proposals, but the fake pod doesn't fly accepted
legs yet. The real pod flies what the pilot flies.

## The Live tab

The **Live** button in the header (Esc leaves it) swaps the panels for everything the pod sends,
live. It's a view only; the mode and the pings carry on, and the banners stay on top.

- **Camera** (`live/camera.jpg`, reloaded twice a second) and **Heat** (`live/thermal.png`, four
  times a second, the HT-203U's rate).
- **The thermal overlay:** `ag/thermal` drawn over the heat picture in the frame's own pixels:
  a crosshair on the warmest spot and its temperature, amber when it's person-warm (`hot`).
  It hides when the picture is frozen, so it never marks an old frame. (The fake pod's stand-in
  `ag/thermal` has no spot position, so the simulation shows the reading but no crosshair.)
- **The map:** the building, north up, drawn from `[rules] building_geojson` (no map tiles, so
  it works offline): outline, the taped park, waypoints, the pod's path and the pings.
  **Building / Demo area** zooms between the whole building and the taped area.
- **Live readings:** thermal (warmest °C, the last 2 minutes as a line with the person-warm band,
  spot, confidence, frames a second, source), the pod (position and where it comes from, height,
  CPU, LoRa, GPS, disk, battery from `ag/sys`) and Bluetooth (scans a second, lost devices heard,
  strongest, open pings, SOS answers, last LoRa alert, link).

**60 State Street:** the demo profile (`afterglow.toml`) now puts its origin (waypoint 0) at
60 State Street, Boston (42.35889, −71.05667), where GPS gets no fix, so the taped park, the TV
map and the ATAK markers land on the building. `data/building.geojson` is an **approximate**
outline drawn by hand (no map data was reachable when it was made); swap in the real one
(trace it on geojson.io, or an OSM export) and keep `"approximate": true` out of it.

## Known gaps

- With no internet the live map never finishes loading (MapLibre's `load` event waits on the
  online tiles), so it draws no trails, pod or pings. This is in `livemap.js`, not the TV page.
- "From the car" on a ping is a straight line, not a walking route.
- The coverage colour on the map is still the live map's blue (it's drawn inside `livemap.js`).
- When a Find My key rotates and the tracker can't match it, the same phone can come back as a
  second ping (the real tracker and geofence do this, not the TV).
