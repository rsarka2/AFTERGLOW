# AFTERGLOW TODO

The team's plan and build log from the hackathon, ticked as each item landed (with the time). For a summary of what works, see `README.md`.

Hour 0 is Sat 7 Nov 10:00. Tier 1 is due by hour 12 (Sat 22:00), the freeze is at hour 22 (Sun 08:00) and submission is due by hour 26 (Sun 12:00).

- Owners follow `docs/MODULES.md`: **SW** is Software, **UT** is Utility, and **ME1** and **ME2** are the mechanical leads.
- Tick items as they land. If a milestone slips by more than an hour, cut from the bottom of the tiers. Never push the freeze.
- Checkpoints I1–I9 are defined in `docs/MODULES.md`.

---

## Phase 0: Housekeeping and kickoff (hour 0–1, 10:00–11:00)

- [x] Move the handoff folder up to the repo root; add `.gitignore`, `CLAUDE.md` and this TODO.
- [x] Fix `requirements-laptop.txt`, which was missing `pynmea2`, so the tests couldn't even import on a laptop.
- [x] Set up the laptop venv. All 13 tests pass.
- [x] Push to the team GitHub repo, https://github.com/CodeMetalForge-Hackathon/Team-30-AFTERGLOW (private). Teammates clone it, and the Pi clones it to `/home/pi/afterglow`.
- [x] Install Mosquitto on the laptop (`brew install mosquitto`). Run `fake_bus.py --speed 5` and watch it with `mosquitto_sub -v -t 'ag/#'`. It runs as a `brew services` service, localhost only.
- [x] Settle these contract gaps at kickoff. Edit `contracts.py` and `docs/CONTRACTS.md` together. **Tell the team.**
  - [x] **`ag/target` gets `state` and `last_s`**, which geofence copies from the track. Only the ranger's Confirm sets `confirmed`.
  - [x] **SOS goes through geofence**, onto the investigating pin, or an SOS-only `T0` pin. **Battery** is `ag/sys` `batt_pct`, `null` for now, and goes out as 255.
  - [x] **New retained `ag/coverage` topic** from locate. Its settings are in the new `[locate]` config table.
  - [x] **Images:** dashboard.py serves `live/` and `snaps/` on port 8080, and thermal.py and camera.py just write files.
- [x] Decide the dashboard stack. **Locked:**
  - **Server:** `dashboard.py` on Python's built-in `ThreadingHTTPServer`, port 8080. It subscribes to `ag/#`, keeps the latest message for each topic and each pin, and streams them to the browser with Server-Sent Events (`/api/events`). On every reconnect it resends the full state first. Buttons POST to `/api/ctrl` and `/api/mode`; the server stamps `t`, runs `validate()` and publishes. It also serves `live/`, `snaps/` and the map, converted to local metres once with `geo.py`.
  - **Front end:** `code/afterglow/web/` holds `index.html` and `app.js`. Panels use Preact + htm, and the map uses Leaflet in local metres (`CRS.Simple`). There is no build step and no CDN: the libraries are saved in `web/vendor/`.
  - **No new pip packages** on the Pi or the laptop.
  - **Room to grow:** MJPEG live streams from the same server, a `--broker` flag so the dashboard can run on the laptop, and an optional `drone` field once there's a second drone.
- [x] Download Preact + htm (htm's `preact/standalone` bundle) and Leaflet 1.9.4 into `code/afterglow/web/vendor/` **while there's internet**. Pin exact versions. Saved as `vendor/htm-3.1.1/` and `vendor/leaflet-1.9.4/`; usage and hashes are in `vendor/README.md`.
- [x] Create `code/data/trails.geojson` and `code/data/zones.geojson` for the taped indoor park. Units: lon/lat (the GeoJSON standard, converted with `geo.to_xy` on load). `tools/make_indoor_map.py` generates them from the origin and waypoints; rerun it after changing either.
- [x] Set `origin_lat` and `origin_lon` in `afterglow.toml`; indoors, that's the point taped waypoint 0 maps to. Keep the file identical on the pod and the laptop. It's set to Acadia for now; switch it to the venue if the ATAK markers should appear there.
- [x] ~~Set the private Meshtastic channel on every board.~~ **Dropped:** the LoRa radios are two bare RFM95Ws, not Meshtastic boards, so there's no channel key. `lora_rx.py` filters by node address instead. `tools/setup_lora.py` is gone, and `code/lora_channel.key` can be deleted.
- [x] Reconcile the repo with Rohan's hardware reference (`docs/AFTERGLOW Hardware.pdf`, Nov 7): HARDWARE.md, CONTRACTS.md, firmware/README.md, `pins.csv`, the config, `config.txt.append`, `check_hw.py`. `docs/wiring/` (the page and diagrams) is still out of date.
  - **Contract changes, tell the team:**
    - the LoRa link is RFM95W ↔ XIAO #3, with no Meshtastic and no acks;
    - the new laptop-only topic `ag/alert`;
    - the XIAO UART moves to `Serial1` on D9/D10;
    - `findmy` means separated only (`4C 00 12 19`), and `4C 00 12 02` is `apple_other`;
    - new config tables `[lora]` and `[i2c]`, and `[serial] gps = ""`.
- [x] ~~Record the FPV details.~~ **Decided:** the available drones are too small to carry the pod. We build and demo the payload on its own and pitch it as ready to fly once we have a larger drone. Steps that mention the drone (I7, the demo script, the on-drone GPS test) need rewording for a payload-only demo.

## Phase 1: Bring-up and first data (hours 0–3, until 13:00, checkpoints I1–I5)

**Hardware (UT)**
- [x] Image the Pi, run `bash pi/setup_pi.sh` and reboot (no `PSU_MAX_CURRENT`: it's a 3 A power bank). `check_hw.py` passes for every connected part. **(I1)**
  - Done Sat 7 Nov: Bookworm 64-bit, user `pi`, repo at `/home/pi/afterglow`. `check_hw.py --only config power serial_ports mqtt` all PASS on the bare Pi (nothing wired yet); rerun the full check as each part is connected.
  - No travel router: the Pi and the laptop use Rohan's phone hotspot (the building Wi-Fi blocked HTTPS). SSH from the Mac works (fixed Sat 7 Nov ~17:55). The Pi's Mosquitto is open to the hotspot (`pi/mosquitto-afterglow.conf`), so the Mac can `mosquitto_sub -h <pi-ip>`.
- [x] ESP32 sketches into `firmware/` (scanner and wifi_listener; sos_page has its own tier-2 item, "sos_page firmware (UT)"). Each must print on `Serial1.begin(921600, SERIAL_8N1, D9, D10)`, not `Serial` (see `firmware/README.md`). The scanner sends `findmy` only for `4C 00 12 19`.
  - [x] No plan-doc sketches existed, so they were written from `firmware/README.md` and CONTRACTS.md: `firmware/scanner/scanner.ino` and `firmware/wifi_listener/wifi_listener.ino`. USB debug shows per-class counts, never an address.
  - [x] Compiled and flashed both in the Arduino IDE (Sat 7 Nov). Bench check over USB: both counts rise as expected. Both wired to the Pi (~18:05): `check_hw.py --only omni wifi` passes, and `ag/obs/ble` and `ag/obs/wifi` reach the Mac with hashed ids on channels 1, 6 and 11.
  - [x] **SW:** `apple_other` is now in `contracts.CLS_CODES` (alert class code 6), so the bridge keeps those lines with `REPORT_ALL` on instead of dropping them. **Contract change, tell the team.**
- [x] OMNI streams `ble` lines, and a real `findmy` beacon reaches `ag/obs/ble` through `bridge.py`. **(I2)** Done Sat 7 Nov ~18:15: OMNI on UART0, strangers' `findmy` tags reach `ag/obs/ble` (hashed ids, about one line per tag every 2 s), watched from the Mac via the Pi's broker. Bench counts in a crowded building: `findmy` about 1.5/s vs `apple_other` about 240/s, so the split looks right.
  - [x] ~~Still to check before I6: a powered-off iPhone shows up as `findmy`.~~ **Dropped Sat 7 Nov:** the team decided not to test it; the crowded-room counts are the evidence for the split.
  - Mac setup note: the venv needs Homebrew's Python 3.12 (macOS's own `python3` is 3.9 and lacks `tomllib`); brew's Mosquitto needed a `mosquitto.conf` created. `check_hw.py` now masks addresses in its sample line (it used to print them raw).
- [ ] Put the radar's serial number in `pi/99-afterglow.rules` so `/dev/radar` exists.
- [x] **LoRa link (I4):** done Sat 7 Nov 19:23, pod RFM95W → Pluto → `ag/alert` on the Mac.
  - **Changed Sat 7 Nov:** we have only **one RFM95W**. The laptop receives with an **ADALM-PLUTO SDR** (USB to the Mac, antenna on RX) and gr-lora_sdr; XIAO #3 is now the spare. Docs updated (HARDWARE, CONTRACTS, MODULES, firmware/README). The `rx` line format is unchanged.
  - [x] Wire the pod RFM95W to SPI1 with its spring antenna.
  - [x] Pi: `check_hw.py --only lora` passes ("sent a heartbeat"): proves the pod radio without any receiver. Done Sat 7 Nov (Blinka's first run installs its Pi 5 deps and fails once; now in `requirements-pi.txt`).
  - [x] Mac: GNU Radio + gr-lora_sdr in conda env `sdr` (`brew install miniforge`, then `conda create -n sdr -c tapparelj -c conda-forge gnuradio-lora_sdr gnuradio-iio libiio`); `iio_info -s` lists the Pluto at `usb:0.1.5`.
  - [x] **SW:** `tools/pluto_rx.py` (Pluto → gr-lora_sdr → `rx` lines, settings from `[lora]`) and `lora_rx --stdin`. `pluto_rx.py --selftest` (fake heartbeat, noise, 10 kHz offset, no Pluto) decodes into a valid `ag/alert`.
  - [x] The pod's heartbeat (`"kind":4`) shows up in `ag/alert` on the Mac. Done Sat 7 Nov 19:19: `check_hw --only lora` on the Pi → Pluto (AGC, about 1 m) → `pluto_rx | lora_rx --stdin`. 6 other teams' packets heard and dropped as `not_ours`.
  - Fallback if the SDR decode isn't working by I7: `cot_out --wifi <pi-ip>` gets the pins to ATAK over the hotspot; show the LoRa bursts on a Pluto waterfall.
- [x] **GPS NMEA by a window (I5).** Done Sat 7 Nov ~19:40: NEO-F10N on UART2 sends NMEA at 38400 (the config's `gps_baud`); indoors it sees 4 GPS satellites at 6–21 dB-Hz, no fix yet. Still to do: a fix at a window or outside. **Unblocked Sat 7 Nov:** we have a SparkFun NEO-F10N (L1/L5) breakout for the ANN-MB5 antenna. Wire its TX → Pi pin 29, RX → pin 7, GND → pin 30, power per its labels (5V/VIN → pin 4, else 3V3 shared from pin 1 or 17). Test NMEA on `/dev/ttyAMA2` (38400, else 9600). **Keep `gps = ""` for the indoor demo:** a fix near a window would place the pod relative to the Acadia origin. Set it only for an outdoor test or after moving the origin to the venue.
- [x] (Not needed: the user is `pi` and the clone is at `/home/pi/afterglow`.) If the Pi user isn't `pi` or the clone isn't at `/home/pi/afterglow`, fix `User=` and the paths in `pi/systemd/afterglow@.service`.

**Software (SW), on the fake bus: done** (reviewed and pushed Sat 7 Nov)
- [x] Dashboard skeleton (`dashboard.py` + `afterglow/web/`): the event stream with its full-state resend, a Leaflet map with trails and zones, live pose and raw observations, all running on the fake bus. **(I3: pins and coverage move, checked in both profiles.)**
  - The badge (LIVE, SIMULATED or REPLAY) comes from the pose's `src`. The park map carries the OSM credit.
  - Ages use the latest pose's `t`, not the browser clock. Using the newest `t` overall broke: a retained message from a 40× fake run sat hours in the future.
  - Run it with `python -m afterglow.dashboard [--config park.toml] [--broker host]` and open `http://localhost:8080`.
- [x] `tools/replay.py`: replay a `mosquitto_sub -v` log into the same topics with the original timing. It's the 3 a.m. debugging tool and the demo backup.
  - It marks the pose `replay` and moves `t` to now.
  - It shrinks pauses longer than 10 s, and `--topics` replays only the raw input.
  - It skips `ag/ctrl` and `ag/mode` unless you pass `--with-ctrl`, because a recorded Confirm shouldn't be made again unasked.
- [x] **Two config profiles.** Pick one with `--config` or `AFTERGLOW_CONFIG`; `tests/test_parkmap.py` fails if their keys drift apart.
  - **Demo**, `afterglow.toml`: the taped indoor park and real pod data. Now has indoor-scale placeholders: `offtrail_m` 3, `detect_m` 4, and a new `[track]` table with the localized and pinned radii.
  - **Park**, `park.toml`: realistic view at Blackwoods Campground, Acadia, using real OSM trails and roads (`data/park_*.geojson`, made by `tools/make_park_map.py`). Laptop only.
- [x] Sim stub: `fake_bus.py` follows the profile's new `[sim]` table (path, hidden phones, RSSI model). It walks the waypoint loop indoors and flies lanes in the park. It also fakes `ag/track`, `ag/target` and a retained `ag/coverage`, so pins and coverage move for I3. Use `--only pose,ble` once the real modules land. Its tracker cheats (it knows the true positions), so never take accuracy numbers from it.
- [x] **Contract change, tell the team:** `ag/pose` `src` can now be `sim`, and `validate()` checks `src`. The dashboard badges `sim` and `replay` data.
- [x] Phase 1 review fixes, each reproduced in a browser first:
  - an armed Confirm no longer carries over when you pick another pin (it took one click);
  - `/api/ctrl` and `/api/mode` take JSON only, so another web page can't click for the ranger;
  - the Heard list keeps adding devices past 300 (it used to freeze);
  - turning Coverage off keeps the Probability heatmap.
- [x] **Contract change, tell the team:**
  - `ag/mode` is now **retained** (`contracts.RETAINED`), so a restarted geofence or a second dashboard knows the mode at once. With no mode yet, geofence assumes patrol.
  - geofence accepts only the allowed pin status changes; above all, **Confirm only while a pin is investigating** (CONTRACTS.md, "Pin states and priority").
  - `fake_bus.py` deletes its retained `ag/coverage` when it stops, so a fake grid doesn't leak into the next run or profile.
- [x] `geofence.py` geometry: distance to the nearest trail and point-in-zone, with tests. It's in `afterglow/parkmap.py` (`ParkMap.load(cfg)`, `dist_to_trail`, `zone_at`, `bounds`, `to_json`), because the dashboard and the fake bus use it too.

## Phase 2: Tier 1 core pipeline (hours 3–12, until 22:00, checkpoints I6 and I7)

The modules are listed roughly in data-flow order, and each one feeds the next. Write tests that check every message with `validate()`.

- [x] **`tracker.py` (SW)** reads `ag/obs/*` and `ag/pose` and writes `ag/track` and `ag/pass` (CONTRACTS.md, "Tracks"):
  - a grid fit of the `[track]` RSSI model gives `x`, `y` and an honest `r95`; state follows `r95`;
  - follows key rotation by time and signal strength, and refuses an unclear match (a duplicate track beats merging two beacons);
  - `with_drone` for beacons that stay loud while the pod moves; `baseline` for beacons heard during the baseline; `new_search` clears;
  - confidence `c`; each finished pass on `ag/pass`;
  - on the fake bus (20 seeds, 33 min indoors): exactly one track per phone through every rotation, worst error 1.5 m. Live through Mosquitto too.
  - **Contract change, tell the team:** `ag/track` gains optional `baseline` and `c`; `ag/pass` now comes from the tracker, with optional `n`; geofence must never pin `with_drone` or `baseline` tracks. New `[track]` keys (pass gap, rotation, `with_drone`, RSSI model) in both profiles.
  - Fixed in `bus.py`: a module that subscribed while connecting could lose paho's thread and go deaf.
  - Known limit, park sim only: a key that rotates while the pod is away starts a new track, so a phone can split. Fix later by merging tracks whose estimates agree (needs a contract change for the merged-away track).
  - Set the `[track]` RSSI model (`rssi_p0`, `rssi_n`, `rssi_sigma`) from ME 2's range test.
  - [ ] Set `with_drone_rssi` (both profiles) from the range test: measure our phone strapped to the pod and a phone at carry height (3 m), and pick a value between them. At -55, a phone much louder than the model, right under the path, gets marked `with_drone` (grey, never pinned) while the pod is near it. Then let `fake_bus.py`'s team phone read as loud as 0.5 m (it stops at 1 m now), or its with_drone test breaks. Until then, the baseline covers it (Phase 4).
- [x] **`locate.py` (SW)**, now just the grids, since the tracker posts `ag/pass` and `r95` (CONTRACTS.md, "Coverage grid"):
  - keeps the probability grid (from `ag/track`: `x`, `y`, `r95`, `c`; skip `with_drone` and `baseline`). Each track is a blob that peaks at 255 × `c`; where blobs overlap, the higher one wins. A track not updated for `stale_s` (30) drops out;
  - the coverage overlay from `ag/pose`, joining each drone's poses in a straight line, so tapped waypoints leave no gaps;
  - publishes `ag/coverage` about once a second, **retained**. `new_search` clears both grids;
  - `fake_bus.py`'s stand-in coverage is now the real `Locate` fed the fake tracks, so the two grids can't drift apart;
  - tested on the fake pod through the real tracker, in both profiles and live through Mosquitto.
  - **Contract change, tell the team:** locate acts on `new_search`. There are new `[locate]` keys `area_m` (the box the grids cover; empty means the park zone) and `stale_s`. `ag/pose` has a new optional `drone` (which drone sent it; missing means the one pod). In `park.toml`, `area_m` is the search box (80 × 80 cells); the whole park would be 221 × 211, too big to send every second. `[sim] area_m` is now empty there, meaning the same box.
  - Later, maybe: lower `p` in cells already covered with nothing heard (a proper search map); keep coverage through a locate restart.
- [x] **`geofence.py` (SW)** writes `ag/target` (CONTRACTS.md, "How geofence fills a pin"):
  - applies the `day_offtrail`, `night_outside_camp` and `search` rules according to `ag/mode` (retained, so it's there at start-up; patrol until the first click). Investigate keeps the last other mode's rule;
  - sets priority 1–4 and assigns pin IDs (`P1`, `P2`, …);
  - updates the pin status on `ack`, `investigate`, `clear` and `confirm`, using only the allowed changes in CONTRACTS.md. **Confirm only while the pin is investigating:** the dashboard greys the button, but its server doesn't check, so geofence is the guard. Tested for every command from every status;
  - never pins a `with_drone` or `baseline` track;
  - copies `state`, `last_s` and `c` from the track, and sets `state` to `confirmed` only on Confirm;
  - attaches `ag/sos` answers to the investigating pin, or opens an SOS-only `T0` pin at the pod;
  - pins stay once made (only the ranger clears), so a rule must hold for `pin_hold_s` (15 s), and day and night wait until the track is localized with its 95% circle clear of campgrounds;
  - saves pins to `runs/pins_*.json`, so a restart doesn't reuse `P1`; unlinks a track that comes back with none of its ids (tracker restart);
  - on the fake pod through the real tracker (20 seeds, 20 min indoors): patrol pins only the dead iPhone in 19 of 20, night ignores the campground tag in 20 of 20, search pins all three in 19 of 20. The misses are early tracker estimates: one about 9 m out while claiming ±3.6 m, and one team phone marked `with_drone` only after 46 s.
  - **Contract change, tell the team:** `day_offtrail` now skips campgrounds (otherwise the campground tag gets pinned in patrol and demo step 8 can't show night ignoring it). New `[rules]` keys `pin_hold_s`, `pins_file`, `pins_max_age_min` in both profiles. `fake_bus.py` now sends `off_trail_m` `null`, not 9999, when there's no trail.
  - Park sim only: the tracker's key-rotation splits give some phones 2–3 pins, and `airtag_pack` (39 m off trail) is estimated at 20–29 m, under the 30 m threshold, so patrol often misses it. Both are tracker limits (see above).
  - Tier 2: set `flags.thermal` from `ag/thermal` on the investigating pin (done, see Phase 3). (`flags.camera` from `ag/vision` waits for the stretch person detector.)
- [x] **`dashboard.py` (SW)**. Most of this landed with the Phase 1 skeleton (status at the freeze, Sun 8 Nov):
  - [x] pins with Acknowledge, Investigate, Clear and Confirm buttons. Confirm needs two clicks and only works while the pin is being investigated. Each button shows "waiting for geofence" until the status changes, and warns if it never does;
  - [x] a mode switch for patrol, night, investigate and search;
  - [ ] ~~Baseline and New search buttons.~~ **Cut at the freeze:** send `baseline_start`/`baseline_stop` with `mosquitto_pub` (docs/DEMO.md). New search should also drop old tracks and pins from the dashboard's state (`dashboard.py` and `store.js`), since the tracker forgets them. Baseline tracks already show grey;
  - [x] waypoint taps for the indoor park, on the map and as a 0–7 strip in the Pod panel;
  - [x] the coverage and probability overlays, and pod health from `ag/sys`;
  - [x] the thermal and camera panel, on the operator TV (it reloads the `live/` images about once a second). **Scale each picture to fit** (`object-fit: contain`): a 90° `rotate` in config makes it portrait (CONTRACTS.md, "Images over HTTP");
    - [x] **Live tab on the operator TV (Sat 7 Nov night):** camera, heat with the live `ag/thermal` overlay (crosshair and °C on the warmest spot), a 2-minute temperature line, pod health, Bluetooth and the 60 State Street building map (docs/TV.md, "The Live tab"). Also: the rescue route view no longer changes the mode (Back or Esc leaves it), and the TV asks for a check-in on a found person every `[search] checkin_s` (5 min).
    - [ ] **Demo profile origin moved to 60 State Street** (`afterglow.toml`, map files regenerated): **copy `afterglow.toml` and `data/` to the Pi** so the pod and laptop match. Replace the approximate `data/building.geojson` with the real outline if anyone can;
    - [x] **On the operator TV (Sun 8 Nov):** Emergency's "Camera check" card shows the camera and heat pictures; outside Emergency, a Pictures switch on the map (Auto / On / Off) puts them in the map's corner (Auto: both while a ping is checked, heat alone in a night sweep). Each reloads once a second, swaps in only decoded frames, and says "Feed frozen" or "Feed lost" when the picture is over 5 s old (the server now sends `Last-Modified`). `local_stack.py --feeds` writes fake pictures. The ranger dashboard (`/`) still has no picture panel;
  - [x] serves `live/` and `snaps/` on port 8080;
  - [x] takes a `--broker` flag (it overrides `[mqtt] host`), so it also runs on the laptop. Each instance has its own MQTT client id, so the Pi's and the laptop's can share a broker;
  - [x] draw `ag/plan` legs, with Accept and Reject (`accept_leg`, `reject_leg`): the TV's leg card (Trail pathing) and the live map's proposed legs.
- [x] **`lora_out.py` (UT; written by SW)** sends `ag/target` through `alert.encode` from the RFM95W (CONTRACTS.md, "LoRa alert"):
  - every `[lora] repeat_s` (5 s) per live pin, and at once (at most once a second) when a pin is new or its type, state, prio, flags or SOS change. Priority 1 first; on a channel too busy for every pin, the longest-waiting go next, so none goes silent (tested: 40 pins at SF10); no acks;
  - a heartbeat every `repeat_s` with the pod's latest `x`, `y`; `last_s` ages between repeats; the battery from `ag/sys`;
  - type 3 for a confirmed pin, type 2 for an SOS-only pin, else type 1;
  - `lora_off` stops everything and is remembered in `[lora] off_file`, so a restart stays silent; `lora_on` resends at once. The dashboard has no LoRa button yet: use `mosquitto_pub` (CLAUDE.md). A `lora_off` sent while lora_out is down is lost (`ag/ctrl` isn't retained): to start silent, create the off_file by hand;
  - reloads geofence's pins file at start-up, since `ag/target` isn't retained; after `geofence --fresh`, restart lora_out too (`--fresh` now empties the pins file at once, so the restart can't reload the old pins);
  - sends one packet per loop, so a `lora_off` gets in between even at SF10; a missing radio is retried every 10 s, and each open first frees the SPI1 bus and pins the last one claimed;
  - tested with a stub radio, and the whole path with no hardware: `fake_bus --print | lora_out --dry-run | lora_rx --dry-run | cot_out --dry-run` (120 s: 104 packets, all decoded, 3 pins and the pod on ATAK).
  - **Contract change, tell the team (Utility):** new `[lora]` keys `repeat_s`, `alert_version` (1 indoors, 2 in `park.toml`) and `off_file`. lora_out owns the radio while it runs: stop it before `check_hw.py --only lora`, and health must not open the radio for `ag/sys` `lora`.
  - [x] **UT, on the Pi:** `python -m afterglow.lora_out` with the Pluto receiver on the laptop: pins and the heartbeat reach `ag/alert`; `lora_off` silences it. Done 19:23 with `fake_bus --speed 5`: heartbeat every 5 s with the pod moving, P1–P3 heard → localized → pinned, `lora_on` resends every pin at once. No drops seen at about 1 m.
  - [x] `sudo systemctl enable --now afterglow@lora_out` (stop it before `check_hw.py --only lora`). Done 19:25; the unit file had to be copied to `/etc/systemd/system/` first (`setup_pi.sh` now does that).
- [x] **`lora_rx.py` (SW, laptop):** receiver lines (was XIAO #3, now the Pluto via `pluto_rx.py`) → `ag/alert` on the laptop's broker. It drops other teams' packets and checks the radio settings at start-up. Tested on fake lines; I4 proves it on the radios.
- [x] **`cot_out.py` (SW, laptop)** reads `ag/alert` and sends CoT XML over UDP to `[atak] send_to` (default `239.2.3.1:6969`). It needs no pytak, which is now dropped from `requirements-laptop.txt`:
  - spot markers named by pin number as on the dashboard (`P3`), coloured by priority, with a remarks line and the 95% circle;
  - the pod is a friendly drone marker, placed from the heartbeat;
  - **fallback:** `--wifi <pi-ip>` reads `ag/target` and `ag/pose` from the Pi's broker instead, repeats the pins as lora_out would and removes cleared pins. `--to udp://<phone-ip>:4242` sends to one phone if the router blocks multicast;
  - **iTAK:** it takes multicast for SA only, so cot_out is also a tiny TAK server on TCP 8087 (`[atak] tcp_port`); add the laptop as a server in iTAK;
  - tested on decoded alerts (`lora_rx --dry-run | cot_out --dry-run`), and live with `--wifi localhost` against the fake bus: a listener on the multicast group got every pin, circle and pod marker.
  - **Contract changes, tell the team (Utility):** lora_out's heartbeat must carry the pod's latest `x`, `y` (CONTRACTS.md, "LoRa alert"). The alert's byte 1 is now the **pin number**, not the track (`alert.encode(target)` handles it; `ag/alert` has `pin` instead of `track`), so two pins on one track (every SOS-only pin is `T0`) stay two ATAK markers. There's a new `[atak]` config table in both profiles.
  - [ ] On a real phone (iTAK over TCP; ATAK too if we can borrow one) on the travel router: check the marker type, the colours, whether the circle is the right size (it's sent as a radius), status in the remarks, and that Clear removes the marker.
- [x] **`thermal.py` view (UT)**: **the thermal camera is now the HTI HT-203U**, a USB-C phone thermal camera (256×192), not the MLX90640 (HARDWARE.md). Frames at about 4 Hz go to `live/thermal.png` (temp file, then rename), and the warmest spot goes on `ag/thermal`.
  - [x] **First, 10 minutes on the Pi:** done Sat 7 Nov ~20:35. It's USB `2bdf:0102` "HikCamera"; **256×392 YUYV gives real heat values**: rows 0–191 are 16-bit raw (linear, 16 counts/°C), 192–195 a header (word 0 = sensor °C × 100), 196–387 the picture. The raw offset drifts many degrees as the camera warms, so frames are anchored to `[thermal] ambient_c` (`absolute = false`). The formats would have decided the design:
    - **a double-height frame (256×384) or a 16-bit format:** real temperatures. Similar cameras (Topdon TC001, InfiRay P2) put raw temperatures under the picture. `max_c` is in °C, and `hot` means the warmest spot is at least about 30 °C, at least about 5 °C above the frame's median, and under about 45 °C (so not an engine or sun-baked rock);
    - **only 256×192 YUYV or MJPEG:** a picture with automatic contrast and no °C. `hot` then comes from how far the warm spot stands out, and `max_c` must become `null` (contract change).
  - [x] **Everything but the camera reader (SW, Sat 7 Nov):** `afterglow/thermal.py` takes a frame in °C or 0–255 brightness, finds the warmest 3×3 spot, decides `hot` (CONTRACTS.md, "Thermal spot"), writes `live/thermal.png` (temp file, then rename; heat palette, cyan box round a hot spot) and publishes `ag/thermal`. It skips bad frames.
    - `--fake`: a person-sized 34 °C blob drifting over a cool background, out of frame for part of each 40 s loop (hot about 60% of frames), plus a 48 °C rock that must never count. **Dashboard teammate: build the thermal panel against `python -m afterglow.thermal --fake`.**
    - New `[thermal]` table in both profiles: `device`, `fps` (4), `rotate`, `flip`, the hot thresholds (all placeholders).
    - Tests in `tests/test_thermal.py`; live through Mosquitto at 4 Hz. About 5 ms a frame on the laptop.
    - **Contract change, tell the team:** `ag/thermal` gains `w`, `h` and `src` (`ht203u` or `fake`); `col`/`row` are in the 256×192 frame; `max_c` may be `null` (picture only). The dashboard shows `max_c` with `toFixed`, so it needs a `null` case and should mark `src: "fake"`.
    - Clean-up done: `[i2c] thermal_addr`, the 0x33 check, the MLX90640 driver and its config.txt comment are gone. `pi/setup_pi.sh` now installs `python3-opencv` and `v4l-utils`; **on the already-set-up Pi, run `sudo apt install -y python3-opencv v4l-utils` by hand.**
  - [x] **After the Pi test** (done Sat 7 Nov ~20:45, branch `thermal-ht203u`): `camera_frames()` streams through `v4l2-ctl`, `/dev/thermal` udev rule installed on the Pi, `check_hw.py --only thermal` passes, a hand 50 cm away reads hot (cyan box on the palm), the wall doesn't. `afterglow@thermal` enabled on the Pi. Still to do: tune `rotate`/`flip` once mounted. Was: write `camera_frames()` in `thermal.py` (OpenCV; it says "use --fake" until then), set `[thermal] device`, add a `/dev/thermal` rule to `pi/99-afterglow.rules` (never `/dev/videoN`: the Camera Module 3 makes `/dev/video*` nodes too) and `check_hw.py --only thermal` (open the device, grab one frame). Expect a frozen frame or two when the camera clicks its shutter. Then tune the thresholds and `rotate`/`flip` on real frames.
  - **ME:** it doesn't fit the 12 mm thermal hole, and its USB-C plug sticks out of its 46×70×14 mm body. It needs a new spot on the sensor face, pointing the same way as the camera.
- [ ] **`camera.py` (SW)**: a live view from the Camera Module 3 for the dashboard's camera panel, so the ranger sees more than thermal. **Changed Sat 7 Nov:** person detection (Hailo YOLO, tiling, the colour check) is now a stretch goal, and the camera module is `camera.py`, not `person.py`.
  - [x] Written and tested on the laptop (Sat 7 Nov): writes `live/camera.jpg` at `[camera] fps` (2), 1280×720 (temp file, then rename), the whole field of view (the 2304×1296 sensor mode, scaled), continuous autofocus. The time it was taken is drawn bottom left, so a frozen feed is obvious. No MQTT. New `[camera]` table in both profiles.
    - `--fake`: grass, a trail and a person in an orange jacket walking a loop, marked FAKE. **Dashboard teammate: build the camera panel against `python -m afterglow.camera --fake`** (it reloads `/live/camera.jpg`).
  - [x] **On the Pi:** `check_hw.py --only camera` passes (imx708_wide on CAM/DISP 0), `python -m afterglow.camera` writes a sharp `live/camera.jpg` with the time on it, and `afterglow@camera` is enabled. Done Sat 7 Nov ~20:58.
  - [ ] Set `[camera] rotate`/`flip` once the camera is mounted (it isn't yet).
- [x] **health (UT)**: `ag/sys` from `vcgencmd`, ports, disk, GPS fix and LoRa, plus `batt_pct: null`. Done Sat 7 Nov ~21:20 (`afterglow/health.py`, PR #10): it never opens hardware; omni, wifi, thermal and camera count as working while their data is fresh.
  - **All 9 pod modules start at boot** (`systemctl enable`: bridge, position, tracker, locate, geofence, lora_out, thermal, camera, health). `systemctl status 'afterglow@*'` lists them; `journalctl -u afterglow@<name> -f` shows a log.
  - [x] **Acadia cleanup and the State St walk (SW, Sat 7 Nov ~22:40):** Acadia's drone waits at base until "Send out", flies the lawn-mower lanes (drawn on the map), "Return to base"; decoy ~1:15 after launch, person ~1 min later (seed 6, same every time); pictures follow the sim; no health there. State St: indoor floor plan with the IMU walk line (position.py `[pose] source = "imu"`, BNO086 step counter, `[imu]`), no fake weather or pictures, "No signal" when the pod isn't reachable. docs/TV.md "Two locations".
    - [ ] **On the Pi:** `git pull`, restart `afterglow@position`, walk 5 m and check the steps count (`python -m afterglow.position --dry-run` shows poses; tune `[imu] stride_m`). If the BNO086 counts no steps when carried, tap waypoints instead.
  - [x] **Location tabs and pod components (SW, Sat 7 Nov ~22:10):** the TV switches between Acadia (sim, now port 8081) and 60 State St (live demo, 8080) from `[http] sites`; a "Pod components" panel shows each part's health from `ag/sys` (State St main view and the Live tab). fake_bus sends a stand-in `ag/sys`. docs/TV.md, "Two locations".
  - [ ] **Live cameras on the laptop (SW, Sat 7 Nov):** code done: `dashboard.py --broker <pi-ip>` fetches `/live/` and `/snaps/` from the Pi's dashboard (`--pod`, CONTRACTS.md "Images over HTTP"), with the picture age kept across the two clocks. **On the Pi:** `git pull && sudo systemctl enable --now afterglow@dashboard`, then check that `http://<pi-ip>:8080/live/camera.jpg` loads from the laptop.
  - [ ] **Power:** `throttled` read `0x50000` (under-voltage and throttling since boot) on the power bank with the cameras running. **Decided Sat 7 Nov: for the demo the Pi is powered from the laptop's USB-C.** Use a short, thick cable straight into the laptop (no hub), and keep the Pi at least 1 m from the Pluto. After a reboot with the cameras running for 5 minutes, `throttled` must stay `0x0`.
- [ ] Put ME 2's range numbers in by hour 7: the localized and pinned radii, the lane spacing and `fake_bus.py`'s RSSI model.
- [ ] **(I6, hour 7, 17:00):** carried along the taped trail, a pin lands within 5 m and shows its off-trail distance. Save a screenshot and the measured error.
- [ ] **(I7, hour 12, 22:00):** with the closed pod on the drone and props off, run two passes → pin → Investigate → thermal and camera panel → ATAK marker over LoRa. **Record the backup video.**

## Phase 3: Tier 2 (hours 12–22, until the 08:00 freeze, checkpoint I8 at 02:00)

- [x] **`planner.py` (SW, Sat 7 Nov)** writes `ag/plan` (CONTRACTS.md, "Plans"; tests in `test_planner.py`; runs in `local_stack.py`):
  - on `investigate`, homing legs: one crossing pass, then a shrinking orbit;
  - a search grid spaced by the measured range ([plan] `lane_m`, a placeholder);
  - patrol routes along the trails;
  - acts on `accept_leg` and `reject_leg`.
  The TV's leg card and Read to pilot card already show each leg's heading and distance.
  - [ ] **On the Pi:** `sudo systemctl enable --now afterglow@planner`.
  - [ ] Put ME 2's range numbers into the `[plan]` table (`lane_m`, `cross_m`, orbit sizes) with the others.
  - [ ] Next: the fake pod flies accepted legs; the TV draws accepted legs; the live map draws accepted legs too (only proposed now).
- [ ] **Dashboard live feeds (SW):** MJPEG streams of the camera and thermal views from `dashboard.py`, in place of the once-a-second image reload; then raise `[camera] fps`.
- [x] **`thermal.py` flag (SW, Sat 7 Nov ~22:20):** `hot` is never true above `[thermal] max_alt_m` (the latest `ag/pose` `z`; any height before the first pose). geofence sets `flags.thermal` once on the pin under investigation when `ag/thermal` is hot with the pod within `[rules] thermal_near_m` of the pin's 95% circle, and the flag stays set. New keys in both profiles; tests in `test_thermal.py` and `test_geofence.py`.
  - [ ] **UT: measure `max_alt_m`** (30 m now, a placeholder): the height at which a person still covers about 3×3 pixels, since the spot is a 3×3 mean. The old 8 m limit was for the MLX90640's 32×24 pixels. Also set `thermal_near_m` (2 m indoors, 10 m park) to about the thermal view's reach at hover height.
- [ ] **`sim.py` (SW):**
  - a park-scale simulator over Acadia, using a trails GeoJSON (done: the `park.toml` profile and `data/park_*.geojson`);
  - simulated phones using the measured range;
  - runs day patrol, night sweep and active search, and draws the coverage map.
- **Board swap Sun 8 Nov ~01:40:** the WIFI XIAO (#2) went silent and stopped showing up on USB, even in BOOT mode. XIAO #3 (the ref beacon) is now the WIFI board, flashed with `wifi_listener.ino`: `check_hw.py --only omni wifi` both pass. No spare XIAO and no ref beacon now (I6 would need a powered-off phone instead).
- [x] **sos_page firmware (UT):** an open `SOS-RESCUE` AP with a captive page that sends `sos` lines and obeys `sos_on` and `sos_off`. **Written Sat 7 Nov ~22:00** in `firmware/wifi_listener/wifi_listener.ino` (compiles with `arduino-cli`); flashed and tested with a phone Sat 7 Nov (join SOS-RESCUE → page pops up → Send). It's a toggle: off at boot, `sos_on`/`sos_off` (the TV dashboard's SOS buttons).
  - Optional, from the hardware reference (contract change first): a custom `msg` on `sos_on`, and a "phone joined the SOS network" event, which is a strong sign someone is there.
- [ ] **`position.py`:** add IMU roll and pitch from the BNO086 at `[i2c] imu_addr` (`0x4B`), with RST on GPIO23 passed as `reset=`.
- [ ] **(I8, hour 16, 02:00) evidence:**
  - the range numbers;
  - 10 blind passes, with the median and worst error;
  - the night-mode false-alarm count;
  - planner homing legs on the dashboard.
  These go on the evidence slide.

## Phase 4: Demo hardening and freeze (hours 16–26)

- [ ] Load the best run's replay and test it, as the backup if the hardware fails.
- [x] **Fix the simulator's wording (`simulator/`, PR #4).** Done Sat 7 Nov ~23:30: "LIKELY PERSON · RANGER CONFIRM" in place of "PERSON CONFIRMED" and "MISSING PERSON FOUND"; "pin proposed to the ranger" in place of "GPS fix sent to incident command"; the drone's `CONFIRM` mode is now `CHECK`; a supply drop is proposed, never done.
  - [ ] Still to decide: does it stay "TrailSAR" or become AFTERGLOW?
  - Not fixed: the simulated drone still flies its own legs without a ranger's OK. Fine for a what-if, but say so if a judge asks.
- [x] **Judging docs (Sun 8 Nov ~09:30):** the README is now for judges (what's real vs simulated, how to run it); `docs/DEMO.md` is the runbook (setup, the run, fallbacks, judges' questions). Code review at the freeze: no bugs found in the core paths; the one rule and the privacy rules hold (tests and a live API run).
- [ ] Demo setup, step by step in `docs/DEMO.md`:
  - a 10-minute baseline at the demo spot, with any team trackers on the pod, so they show grey instead of relying on `with_drone`. **Keep the target out of range (another room, or foil) during the baseline,** or it's greyed out too;
  - the target (an AirTag away from its owner, or a team iPhone switched off) proved as `findmy` on `ag/obs/ble` first, then hidden;
  - the phone hotspot on, with the pod, the laptop and the iPhone on it, never the venue Wi-Fi: the dashboard has no login, so anyone on the same network could click;
  - **`ag/alert` forwarded to the Pi's broker** (`mosquitto_sub -t ag/alert | mosquitto_pub -h <pi-ip> -t ag/alert -l`): the State St TV reads the Pi's broker, and lora_rx publishes on the laptop's, so without it "Last LoRa alert" stays empty;
  - everything charged.
- [ ] Two clean end-to-end rehearsals of the run in `docs/DEMO.md`.
- [ ] **Hour 22 (Sun 08:00): freeze.** After it, only fixes that block the demo.
- [ ] Submit before Sun 12:00.

---

## Stretch goals (only if ahead, most valuable first)

- [ ] **`radar.py` (tier 3, UT):** MR60BHA2 breathing and heart rate on `ag/radar`, landed within 1.5 m. Feeds the alert's `br` byte and the `radar` flag.
- [ ] **CalTopo export (tier 3, SW):** pins and coverage as GeoJSON, plus a live marker through CalTopo's position API.
- [ ] **LoRa fallback view (tier 3, SW):** once the laptop loses Wi-Fi to the Pi, the dashboard on the laptop shows pins from `lora_rx.py`, without feeds.
- [ ] **Live outdoor position test (tier 3, UT):** a GPS fix with the pod on the drone, logged against a phone's GPS.
- [ ] **Person detection (stretch, SW; was tier 2 `person.py` until Sat 7 Nov):** added to `camera.py`, because only one process can open the camera. It writes `ag/vision` with a snapshot (`snaps/<t>.jpg`), and geofence sets the investigating pin's `camera` flag:
  - a precompiled Hailo YOLO model on the AI HAT+, keeping only the person class;
  - each 4608×2592 frame tiled into overlapping 640×640 pieces (about 28 tiles, about 1 fps), boxes merged;
  - the CPU colour-anomaly check (no AI needed; could come first);
  - tuned for recall; runs only during Investigate if heat or power fails. It only points: the ranger decides.
- [ ] **Fine-tuned person detector** (after the one above):
  - train YOLO on HERIDAL on a GPU or Colab and export it to ONNX;
  - compile it with Hailo's Dataflow Compiler, using at least 1,024 calibration images.
  It took one developer a full weekend, so try it only with a GPU and spare time.
- [ ] **Radar pole mount.**
- [ ] **Pod battery reading (UT):** a voltage divider from the LiPo into a XIAO analog pin, so `ag/sys` `batt_pct` stops being `null`.
- [ ] **TAK server login (SW):** today anyone on our hotspot can join `tak_link.py` (plain TCP 8087 has no login; TLS 8089 doesn't require a client certificate), and post chat, field reports or positions to the TV. Phones can't move the drone or change a pin, so this matters only off our own hotspot. Fix: `ssl.CERT_REQUIRED` in `tak_link.tls_context()` (the iTAK package from `tak_certs.py` already holds a client certificate), and drop or firewall plain TCP. Test on a real iPhone first.

### Beyond the hackathon

These are the next steps from the build plan's "Not building" list.

- An autopilot (INAV or ArduPilot) that flies the planner's legs.
- Several drones on one dashboard: an optional `drone` field in the contracts, and the dashboard on the laptop listening to every pod.
- Trailhead Bluetooth tags for families, for the no-phone child case.
- Docking and charging stations for routine patrols.
