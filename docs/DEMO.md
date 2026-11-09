# Demo runbook (Sun 8 Nov)

This replaces the old demo script in `drone_build_plan.md`, which assumed the drone would be carried along a taped trail and person detection would be built.

Judges see one laptop and the operator TV with two location tabs:

- **60 State St** (`:8080`): the real pod, live. Camera, heat, nearby alert beacons, a 10 m hall walk, and pins over LoRa to the laptop and an iPhone (iTAK).
- **Acadia NP** (`:8081`): the whole mission, simulated on real trails: a sweep, a false alarm, homing, a found person and the rescue route.

Run every laptop command from `code/`. `<pi-ip>` is the Pi's address on the hotspot, and `<laptop-ip>` is the laptop's.

## 1. Setup (start 45 min before)

### Network and power

- [ ] Turn on the phone hotspot. Join the Pi, the laptop and the iPhone to it, never to the venue Wi-Fi: the dashboard and TAK ports have no login.
- [ ] Power the Pi from the laptop's USB-C with a short, thick cable (no hub), not the power bank, which throttles it.
- [ ] Keep the Pi at least 1 m from the Pluto.
- [ ] Make sure the Pluto's antenna is on **RX** and its **USB** port (not PWR) goes to the laptop.

### Pi (`ssh pi@<pi-ip>`)

```
cd ~/afterglow && git pull                    # the 60 State St config and maps: must match the laptop's
sudo systemctl enable --now afterglow@dashboard afterglow@planner
sudo systemctl restart 'afterglow@*'
systemctl --no-pager list-units 'afterglow@*'  # all 11 "running"
vcgencmd get_throttled                         # must be throttled=0x0
```

- [ ] Check the config matches: `md5sum ~/afterglow/code/afterglow.toml` on the Pi should print the same hash as `md5 afterglow.toml` on the laptop.
- [ ] Don't run `tools/check_hw.py` while the services are up: they hold the serial ports, the radio and the cameras.

### Laptop: five terminals

Mosquitto must be running (`brew services start mosquitto`).

```
# 1. TV for 60 State St (the real pod)          -> http://localhost:8080
.venv/bin/python -m afterglow.dashboard --broker <pi-ip>

# 2. TV for Acadia (simulation, fake pictures)  -> http://localhost:8081
.venv/bin/python tools/local_stack.py --config park.toml --feeds

# 3. LoRa receiver: Pluto -> ag/alert on the laptop's broker
conda activate sdr
python tools/pluto_rx.py | .venv/bin/python -m afterglow.lora_rx --stdin

# 4. Show the LoRa alerts on the TV (it reads the Pi's broker, lora_rx writes the laptop's)
mosquitto_sub -t ag/alert | mosquitto_pub -h <pi-ip> -t ag/alert -l

# 5. Pins to the iPhone (iTAK over TLS): read from LoRa, not Wi-Fi
.venv/bin/python ../tak/tak_link.py --config afterglow.toml --tls-dir ../tak/runs
```

- **Terminal 4 matters.** Without it, the State St page shows "Last LoRa alert: none heard yet" even while LoRa works.
- **iTAK, first time on this laptop (or after its IP changes):** run `.venv/bin/python ../tak/tak_certs.py --host <laptop-ip>`, then import `../tak/runs/share/AFTERGLOW-iTAK.zip` on the iPhone (`tak/README.md`, "iPhone (iTAK)").
- **One TAK server at a time.** Don't also run `cot_out` or `tak_stack.py`, which use the same ports.

### Check it's all live

| Where | What you should see |
|---|---|
| State St tab, status bar | LIVE, the pod link up, and no "No signal" |
| Camera and heat pictures | Updating every second. A hand in front of the heat camera shows a hot box. |
| Nearby alerts pane | Rings for the alert beacons heard in the room |
| Walk the pod 2 m along the line | "x of 10 m walked" moves (IMU steps). Tap waypoint 0 to reset. |
| State St live readings | "Last LoRa alert" a few seconds ago, with an RSSI at the Pluto |
| iPhone (iTAK) | The AFTERGLOW pod marker |
| Acadia tab | "Send out" on the banner |

### Baseline and the target

In search mode, the pod pins every lost-device beacon that stays heard for 15 s. A baseline greys out the ones already in the room (strangers' AirTags) so they're never pinned.

1. **Prove the target first.** Use the device you'll hide: an AirTag away from its owner's phone, or a team iPhone switched off. Bring it near the pod and watch for `"cls":"findmy"` with `mosquitto_sub -h <pi-ip> -t ag/obs/ble`. If it never shows, use another device: we never tested a switched-off iPhone.
2. **Move it out of range** for the baseline, into another room or wrapped in foil, or it gets greyed out too.
3. **Run the baseline** at the demo spot, with any team trackers on the pod:
   ```
   mosquitto_pub -h <pi-ip> -t ag/ctrl -m '{"t":0,"cmd":"baseline_start"}'
   # ...about 10 minutes...
   mosquitto_pub -h <pi-ip> -t ag/ctrl -m '{"t":0,"cmd":"baseline_stop"}'
   ```
   Don't send `new_search` after this: it forgets the baseline.
4. **Hide the target** a few metres off the hall line.
5. **Record a backup** during a rehearsal: `mosquitto_sub -h <pi-ip> -v -t 'ag/#' > runs/$(date +%H%M).log`.

## 2. The run (about 5 minutes)

1. **Hook (30 s).** On Mt. Massive in 2021, two hikers called 911, then their phone died, and they were found 34 hours later. Searchers didn't lack a helicopter; they lacked a location. (Or tell Greg's story.) A dead iPhone keeps sending a Bluetooth beacon for hours.
2. **The pod (State St tab).** Point at the live camera and heat. The Nearby pane shows the lost-device beacons heard in this room, as anonymous hashes: the pod only listens, and never decodes or stores an identity.
3. **Search.** Click **Trail pathing** (search mode) and carry the pod along the hall line. A ring tightens as the pod nears the target, and after about 15 s a new ping chimes and asks the ranger to decide.
4. **The one rule.** The ping offers only **Investigate** or **Clear**: "Confirm person" stays locked. The ranger clicks Investigate, and the planner proposes homing legs. Nothing is accepted until the ranger clicks **Accept leg**, and nothing ever flies itself.
5. **Check.** Hold the pod within 2 m of the ping. The card reads the heat ("warm"), and with no person detector it says "Not sure: check the camera yourself". The ranger looks at the live camera and clicks **Confirm person**.
6. **LoRa.** The pin reaches the laptop through the Pluto ("Last LoRa alert", with its RSSI), and the iPhone shows the marker. There's no Wi-Fi or cell on that path: it's a 16-byte packet at 915 MHz.
7. **Zoom out (Acadia tab).** Click **Send out** at 4×. The drone sweeps a box beside a real trail, a dropped AirTag pings first ("No one here", Clear), then the missing phone. Investigate, Confirm, and the rescue route for the ground team.
8. **Close.** Show what's real and what's simulated (README table), then the next steps: a larger drone, an autopilot that flies the accepted legs, and a measured range test.

## 3. If something breaks

| Symptom | Fallback |
|---|---|
| The pod or hotspot is down | Run the Acadia tab only. Add `?force=lora` to the State St URL to show the "LoRa only" banner. |
| The pins don't come from the live pod | Replay the backup: stop terminal 1, then run `.venv/bin/python tools/local_stack.py --replay runs/<HHMM>.log --loop` (it serves `:8080`). |
| The Pluto decodes nothing | Restart terminal 3. On the Pi, `sudo systemctl restart afterglow@lora_out`. As a last resort, run `tak_link.py --wifi <pi-ip>`, which sends pins to the iPhone over the hotspot instead of LoRa (say so). |
| The camera or heat says "No signal" | On the Pi, `sudo systemctl restart afterglow@camera afterglow@thermal afterglow@dashboard`. |
| The walk doesn't move | Tap waypoints instead: `mosquitto_pub -h <pi-ip> -t ag/ctrl -m '{"t":0,"cmd":"waypoint","n":3}'`. |
| A stranger's beacon becomes a ping | That's the system working. Click **Clear**: the ranger decides. |

## 4. Questions judges may ask

- **Did it fly?** No. The drones we had were too small for a 0.4 kg pod, so we built and tested the payload on its own. It's made to strap under a larger drone, and the planner already gives the pilot legs to fly by hand.
- **How accurate is it?** On simulated beacons, the worst error was 1.5 m indoors. We haven't run a range test with real beacons yet; it's the next step, and every radius is a config value waiting for it.
- **Isn't this surveillance?** No. It listens passively, and only to lost-device beacons. Addresses are hashed on the pod with a new salt for every search, payloads are never decoded, and only position and class go over the radio. The ranger approves every pin.
- **Why not just call the phone?** By the time a search starts, it's usually dead, switched off or out of coverage. Even switched off, an iPhone 11 or newer keeps beaconing for up to 24 hours.
- **What about a phone that's still on?** An Apple device with no internet, which is any phone out of coverage, sends the same Find My beacon about every 2 seconds. The SOS Wi-Fi page lets the person answer "hurt? can walk? how many?", and the answer lands on the pin.
- **How is this different from Lifeseeker or BlueFly?** Lifeseeker needs a powered-on phone and FCC approval, and one unit cost Teton County SAR more than $100k. BlueFly listens like we do, but we add dead-phone flags, the ranger's rules per mode, path planning and an off-grid LoRa link to ATAK.
