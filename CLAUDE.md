# AFTERGLOW

A search-assist sensing pod strapped under an FPV drone. It passively hears the Bluetooth beacons that lost, dead or switched-off phones and AirTags keep sending (Apple Find My, Google Find Hub, Samsung lost mode). It pins them on a park map, then sends each pin over LoRa (the pod's RFM95W, received by an ADALM-PLUTO SDR on the laptop) to the ranger's laptop and ATAK phone, with no Wi-Fi or cell. A live camera view, plus thermal and radar, helps a human confirm (person detection on the AI HAT+ is a stretch goal). Everything on the pod runs on a Raspberry Pi 5.

**Payload only (decided at kickoff):** the drones we have are too small to carry the pod, so we build and demo the payload on its own and pitch it as ready to fly on a larger drone. Don't plan work that needs the pod in the air.

The project is built at a 26-hour hackathon. Hour 0 is Sat 7 Nov 2026 at 10:00 ET, the freeze is at hour 22 (Sun 08:00) and submission is due by hour 26 (Sun 12:00). `TODO.md` holds the phased plan; tick items there as they land.

**How to answer:** keep answers and explanations simple and short. Go into detail only when asked.

**The one rule:** the drone never decides. The planner proposes, and the ranger approves every pin and every leg. No code path may auto-confirm a pin or auto-accept a leg.

## Read first

- `docs/CONTRACTS.md`: topics, JSON shapes, ESP32 serial lines, the 16-byte LoRa alert and the ATAK mapping. It is the source of truth for anything that crosses a module boundary.
- `docs/HARDWARE.md`: devices, ports, pins and known gotchas, taken from Rohan's hardware reference (`docs/AFTERGLOW Hardware.pdf`, Nov 7), which lists the parts we actually have.
- `docs/MODULES.md`: owners (Software, Utility), tiers and integration checkpoints I1–I9.
- `docs/drone_build_plan.md`: an offline copy of the build doc, with locked decisions, schedule, demo script and risks. Its parts list is out of date.
- If sources conflict:
  - the hardware reference and HARDWARE.md win for parts and pins;
  - CONTRACTS.md wins for formats;
  - the build doc wins for decisions.
- In `docs/wiring/`, `wiring_diagram.png` (Nov 8) and `pins.csv` are current; the wiring page and `pod_layout.jpg` are out of date (they still show Meshtastic), so don't use them for pins.

## Layout

```
code/
  afterglow/            Python package; every module runs as `python -m afterglow.<name>`
    contracts.py        topic names, enums, REQUIRED keys, validate()
    alert.py            16-byte LoRa alert encode/decode (v1 decimetres, v2 metres)
    geo.py              lat/lon <-> local metres around the origin
    bus.py              Bus (paho-mqtt 2.x) and PrintBus (for --dry-run)
    config.py           loads afterglow.toml (env AFTERGLOW_CONFIG overrides the path); resolve() for paths in it
    parkmap.py          trails, roads and zones in metres: dist_to_trail, zone_at, bounds (geofence, dashboard, fake bus)
    bridge.py           ESP32 serial -> ag/obs/ble, ag/obs/wifi, ag/sos (hashes addresses)
    position.py         GPS NMEA or taped waypoints -> ag/pose (NEO-F10N on UART2 works; the indoor demo uses waypoints, gps = "")
    tracker.py          ag/obs/* + ag/pose -> ag/track (grid fit of the [track] RSSI model) and ag/pass
    locate.py           ag/pose + ag/track -> retained ag/coverage (coverage and probability grids over [locate] area_m)
    geofence.py         ag/track + map + ag/mode -> ag/target (rules, prio, the pin status guard, SOS); saves pins to runs/
    planner.py          ag/target + ag/pose + ag/mode -> ag/plan: homing, grid and patrol legs; proposes only, accept_leg/reject_leg decide
    thermal.py          HT-203U frames via v4l2-ctl (or --fake) -> live/thermal.png + ag/thermal (warm spot, hot)
    camera.py           Camera Module 3 (or --fake) -> live/camera.jpg, a live view for the camera panel (no MQTT, no detection)
    health.py           pod health (temperature, throttling, disk, which parts are publishing) -> ag/sys for the Pod panel
    radio.py            opens the pod's RFM95W on SPI1 from the config's [lora] table (lora_out, check_hw)
    lora_out.py         pod: ag/target (+ ag/pose, ag/sys) -> 16-byte alerts on the RFM95W every [lora] repeat_s, plus a heartbeat; obeys lora_off
    lora_rx.py          laptop: receiver lines (Pluto SDR via tools/pluto_rx.py) -> ag/alert (drops other teams' packets)
    cot_out.py          laptop: ag/alert (or ag/target + ag/pose with --wifi) -> CoT XML: UDP multicast for ATAK, TCP server for iTAK ([atak] table)
    trails.py           trail pathing: rank trails from a start point ([search] popularity), the route along one
    case.py             the open missing-person case (laptop only, runs/; never on the bus or LoRa)
    dashboard.py        web server on [http] port: SSE state stream, /api/ctrl, /api/mode, /api/case, live/ and snaps/
    web/                the one dashboard, served at /: tv.html/tv.js/tv.css (operator TV, Preact+htm, docs/TV.md),
                        livemap.js (its 3D map, docs/LIVEMAP.md), store.js (SSE reducer);
                        nearby.html (close-range view of alert beacons only, north up: rings = heard, RSSI distance only; dots = located by tracker.py; ?demo=1 for fake data)
  afterglow.toml        DEMO profile (taped indoor park at 60 State Street, Boston); must be identical on the pod and the laptop
  park.toml             PARK profile (realistic view, real Acadia trails), laptop only; same keys as afterglow.toml
  data/                 trails/zones/building.geojson (indoor; building is approximate) and park_trails/park_zones.geojson (OSM); all lon/lat
  tools/fake_bus.py     fake pod per the profile's [sim] table, plus stand-in track/target/coverage
  tools/local_stack.py  the laptop stack in one process, no broker: fake pod or a replay -> real tracker/locate/geofence -> dashboard
  tools/replay.py       replays a mosquitto_sub -v log with the original timing (debugging, demo backup)
  tools/make_park_map.py  data/park_*.geojson from OpenStreetMap (needs internet)
  tools/check_hw.py     PASS/FAIL/SKIP for each pod part (run on the Pi); --only lora sends one heartbeat
  tools/pluto_rx.py     laptop: ADALM-PLUTO SDR -> gr-lora_sdr -> receiver lines for lora_rx --stdin (conda env "sdr")
  tools/make_indoor_map.py  writes data/*.geojson from the origin and waypoints
  tools/draw_map.py     draws the map files to docs/maps/indoor_map.png
  tests/                pytest
docs/                   CONTRACTS.md, HARDWARE.md, MODULES.md, TV.md, LIVEMAP.md, DEMO.md (the demo runbook), drone_build_plan.md, AFTERGLOW Hardware.pdf
  wiring/               wiring_diagram.png and pins.csv (current); pod_layout.jpg and wiring_and_build.html (out of date)
pi/                     setup_pi.sh, config.txt lines, udev rule (radar), Mosquitto conf, systemd unit
firmware/               what the ESP32 boards must send; lora_rx/ is the old XIAO #3 receiver sketch (unused: the Pluto replaced it)
tak/                    TAK add-on: field teams' phones (iTAK/ATAK) <-> the operator TV (tak_link.py, tak_stack.py); tests in code/tests/test_tak_*
simulator/              TrailSAR browser simulator of a whole mission in real parks; separate from code/, all simulated
```

On the Pi, clone the repo to `/home/pi/afterglow`, because `pi/systemd/afterglow@.service` assumes that path.

## Commands

Run all of these from `code/`:

```
python3 -m venv .venv && .venv/bin/pip install -r requirements-laptop.txt   # laptop setup
.venv/bin/python -m pytest -q                                            # tests
.venv/bin/python tools/fake_bus.py --print | head                        # fake data, no broker
.venv/bin/python tools/fake_bus.py --speed 5                             # fake data to local Mosquitto (demo profile)
.venv/bin/python tools/fake_bus.py --config park.toml --speed 10         # park profile (realistic view)
mosquitto_sub -v -t 'ag/#'                                               # watch the whole bus
.venv/bin/python -m afterglow.tracker & .venv/bin/python -m afterglow.locate & .venv/bin/python -m afterglow.geofence [--fresh] & .venv/bin/python -m afterglow.planner   # real modules, with fake_bus --only pose,ble
.venv/bin/python -m afterglow.thermal --fake [--dry-run --frames 8]      # fake thermal: live/thermal.png + ag/thermal
.venv/bin/python -m afterglow.camera --fake [--frames 8]                 # fake camera: live/camera.jpg (on the Pi, no --fake)
.venv/bin/python -m afterglow.dashboard [--config park.toml]             # the one dashboard (operator TV): http://localhost:8080, park on :8081
.venv/bin/python tools/local_stack.py --config park.toml --feeds         # whole laptop stack + fake camera/heat, no broker; open http://localhost:8081, then "Send out"
                                                                         # TV location tabs ([http] sites): Acadia :8081 (sim) <-> 60 State St :8080 (live demo, pod components panel)
.venv/bin/python -m afterglow.dashboard --broker <pi-ip>                 # laptop dashboard against the pod's broker; camera/heat pictures come from the Pi's afterglow@dashboard (--pod off: local live/)
mosquitto_sub -v -t 'ag/#' > runs/$(date +%H%M).log                      # record a run
.venv/bin/python tools/replay.py runs/1432.log [--loop] [--topics 'ag/obs/#,ag/pose']   # replay it
.venv/bin/python -m afterglow.bridge --dry-run < lines.txt               # no hardware, no broker
.venv/bin/python -m afterglow.position --dry-run --source gps < nmea.txt
.venv/bin/python -m afterglow.position --fake-walk --dry-run              # indoor IMU walk ([pose] source = "imu"), no hardware
.venv/bin/python tools/fake_bus.py --print --only pose,ble --duration 600 | .venv/bin/python -m afterglow.tracker --dry-run   # tracker on fake data, no broker
.venv/bin/python -m afterglow.lora_rx --dry-run < rx_lines.txt           # laptop radio lines, no hardware
conda activate sdr; python tools/pluto_rx.py --selftest                  # laptop: check GNU Radio + gr-lora_sdr, no Pluto
python tools/pluto_rx.py | .venv/bin/python -m afterglow.lora_rx --stdin [--print]   # laptop: the Pluto -> ag/alert (in the sdr env)
.venv/bin/python -m afterglow.cot_out [--wifi <pi-ip>] [--to udp://<phone-ip>:4242]   # laptop: ATAK markers (--print to see the CoT)
.venv/bin/python -m afterglow.lora_rx --dry-run < rx_lines.txt | .venv/bin/python -m afterglow.cot_out --dry-run   # CoT, no radio, no broker
.venv/bin/python tools/fake_bus.py --print --duration 120 | .venv/bin/python -m afterglow.lora_out --dry-run | .venv/bin/python -m afterglow.lora_rx --dry-run | .venv/bin/python -m afterglow.cot_out --dry-run   # the whole LoRa path, no radio, no broker
.venv/bin/python -m afterglow.lora_out [--print]                         # pod: alerts on the RFM95W (--print: show the receiver lines lora_rx would read)
mosquitto_pub -t ag/ctrl -m '{"t":0,"cmd":"lora_off"}'                   # silence LoRa (remembered across restarts); "lora_on" to resume
.venv/bin/python tools/check_hw.py [--only lora omni]                    # on the Pi only
```

Anything past `--print` or `--dry-run` needs Mosquitto on the laptop (`brew install mosquitto`).

## Architecture

- Modules are separate processes that talk only over MQTT: Mosquitto on the Pi at `localhost:1883`. Each message is one JSON object, and every topic starts with `ag/`. Only `ag/coverage` and `ag/mode` are retained (`contracts.RETAINED`). Only the 16-byte alert leaves the pod.
- `t` is Unix time in milliseconds, stamped on the Pi by `contracts.now_ms()`. The ESP32s keep no clock.
- Positions are in local metres: `x` is east and `y` is north of the mission origin set in `afterglow.toml`. Only `position.py` (inbound) and `cot_out.py` (outbound) convert lat/lon, both through `geo.py`.
- Data flow: bridge → `ag/obs/*`; position → `ag/pose`; tracker → `ag/track`, `ag/pass`; locate → `ag/coverage`; geofence (also reads `ag/sos`) → `ag/target`; planner (`ag/target`, `ag/pose`, `ag/mode`) → `ag/plan`, accepted only by the ranger; lora_out → pod RFM95W (SPI1) → 915 MHz → ADALM-PLUTO SDR → pluto_rx → lora_rx → `ag/alert` (laptop broker) → cot_out → ATAK. The dashboard writes `ag/ctrl` and `ag/mode`.
- `ag/target` is the one record per pin that the dashboard, the LoRa alert and ATAK all consume. `geofence.py` owns it.

## Conventions

- **Module shape.** Copy the pattern in `bridge.py` and `position.py`:
  - a class whose `handle_*` and `on_ctrl` methods take parsed input and call `bus.publish`;
  - a `main()` with argparse, `--config` and a `--dry-run` that uses `PrintBus`.
  Tests drive the class with a stub bus that collects `(topic, msg)` pairs.
- **Hardware imports.** Import hardware-only libraries (`serial`, `picamera2`, Hailo, Blinka's `board`/`busio`/`digitalio`, Adafruit drivers such as `adafruit_rfm9x`) inside the function that uses them, so the module still imports on a laptop.
- **Contracts.** Change `contracts.py` and `docs/CONTRACTS.md` together, in one commit, and tell the team. Several people build against them in parallel, so don't rename topics or fields casually.
- **Validation.** Every published message must pass `contracts.validate(topic, msg)`. Assert this in tests.
- **LoRa alert.** Use `alert.encode` and `alert.decode`. Never re-type the struct.
- **Profiles.** `afterglow.toml` is the demo (indoor, real pod) and `park.toml` the realistic park view (laptop only, simulated). Every module takes `--config`. A new config key goes in both files.
- **Config.** Load with `afterglow.config.load()`, and open files it names with `config.resolve(cfg, rel)`. Never hard-code ports, thresholds or the origin. The range-dependent numbers (localized and pinned radii, lane spacing, `offtrail_m`) are placeholders until ME 2's range test, so they belong in config.
- **Python version.** Target Python 3.11, which is what Pi OS Bookworm ships, so no 3.12-only syntax. The laptop runs 3.12. The Pi venv uses `--system-site-packages` because picamera2 and Hailo come from apt.
- **Bus behaviour.** `Bus` handlers run on paho's network thread, so keep them fast. `Bus.subscribe` keeps one handler per topic pattern: subscribing to the same pattern again replaces the first handler.
- **USB serial.** Never hard-code `ttyACM0`. On the Pi, use `/dev/radar` or `/dev/serial/by-id/…`. On the laptop, the LoRa receiver is the Pluto (libiio `usb:`), not a serial port.
- **LoRa radio settings** live in the config's `[lora]` table and are copied into `firmware/lora_rx/lora_rx.ino`. Change both together; `tests/test_lora_rx.py` checks they match.
- **Test against fake data first.** Every new module should run against `tools/fake_bus.py` before it touches hardware.
- **Dashboard.** It only relays clicks: nothing in it publishes `ag/ctrl` or `ag/mode` on its own. `web/store.js` `apply()` mirrors `State.apply()` in `dashboard.py`, so change both. Its clock is the latest pose's `t`, never the browser clock. No CDN and no build step: libraries live in `web/vendor/`.

## Privacy rules (non-negotiable)

- Never write a raw Bluetooth or Wi-Fi address to disk, a log or the screen. Only `bridge.py` sees raw addresses; it publishes an 8-hex salted hash as `id`, with a new salt on every `new_search`.
- Never decode or decrypt beacon payloads. Read only the type marker and the RSSI.
- The scanners are passive. The SOS Wi-Fi page is the only transmitter and must obey `sos_off`, and `lora_out.py` must obey `lora_off`. The pilot's control link always wins.
- Call it "patrol" or "search-assist", never "surveillance", in UI text, docs and the pitch.
- LoRa is plain and unencrypted, so an alert may carry a pin's position and class, but never an address, an `id` or payload bytes.
- Never commit secrets (`*.key`), camera snapshots (`snaps/`) or run logs (`runs/`).
- The missing-person case (name, age, description) stays on the laptop: `case.py` saves it under `runs/` and the dashboard streams it to browsers. Never publish it on the bus, send it over LoRa or write it to a log.

## Hackathon rules

- Tier 1 must run end to end by hour 12 (Sat 22:00). Tier 2 lands by the hour-22 freeze. Tier 3 happens only if the team is ahead.
- If a milestone slips by more than an hour, cut from the bottom of the tiers. Never push the freeze.
- After the freeze, only fixes that block the demo go in.
- Prefer working and demoable over complete.
