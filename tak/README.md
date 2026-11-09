# TAK integration: the operator TV and the field teams' phones as one system

The ranger runs the search from the operator TV (`tv.html`). The ground teams carry phones with
**iTAK** (iPhone) or **ATAK** (Android). This add-on joins them, so a control on one side shows up on the
other:

- **The TV → the phones:** every ping as a marker, in the TV's colour and with the TV's status ("P2 · ranger decision",
  "P2 · drone checking", "P2 · PERSON · rescue"). The drone, labelled with the mode. A chat message at each step.
  The ground team's **rescue route** as a white line once the ranger confirms a person. Start, pause and reset.
- **The phones → the TV:** each phone's position (a blue diamond with its callsign). Their chat. Their
  **field reports**: `check P3`, `found P3` and `nobody P3` show on the TV as a banner with the right button for
  the ranger; `ack P3` (the team has seen it) goes in the field log.

The rules stay the same. The drone never decides, and neither does a chat message: a phone never sends the
drone or changes a pin, and only the ranger's click confirms a person. No case details (name, age, what they
carry) go to the phones: the route is geometry only.

**Little else in the code changes.** `cot_out.py`, `dashboard.py`, `local_stack.py` and `contracts.py` are as
they are on `main`; this folder builds on them (see [How it fits](#how-it-fits)). The TAK parts of the TV are
built into the one TV (`code/afterglow/web/tv.js`, `store.js`, `tv.css`) and stay idle without TAK. Its tests
live with the others, in `code/tests/test_tak_*.py`.

## Run the demo (simulation, one laptop + phones)

```bash
pip install -r code/requirements-laptop.txt -r tak/requirements.txt
python tak/tak_stack.py --config code/park.toml --port 8090 --feeds
```

Open **http://localhost:8090**. The drone waits at base (Gorham Mountain lot) with no pings. Connect the
phones (below), then press **Send out · lawn-mower sweep**: the phones get "AFTERGLOW launched". The false
signal pings about 1:15 later, the person about a minute after that. **Return to base** brings the drone
home. **Reset** on the TV takes the pins and routes off the phones too, and puts the drone back at base.
`--feeds` adds the fake camera and heat pictures, as `local_stack.py --feeds` does. `--start-paused` also
stops the clock at time zero until **▶ Start the search** (then Send out).

To show the TV on another screen on the same network, add `--host 0.0.0.0` (then `http://<laptop-ip>:8090`).

## Connect the phones

The phones and the laptop must be on the same network. A travel router works, and so does a phone's
**personal hotspot**: join the laptop to it and set that Wi-Fi to a **private** network in Windows, or the
firewall blocks the phones. Find the laptop's IP with `ipconfig` (Windows) or `ipconfig getifaddr en0`
(Mac). On an iPhone hotspot it looks like `172.20.10.x`.

### iPhone (iTAK): one-time setup

iTAK only saves a server it can reach over TLS with a certificate. Typing a plain-TCP server makes it
fail to save or say *error authenticating the server*. So the laptop makes its own small certificate
authority and a data package for the phone:

```bash
python tak/tak_certs.py --host 172.20.10.4
```

Use the laptop's IP from above in place of `172.20.10.4`. It writes `tak/runs/` (git ignores it), including
`tak/runs/share/AFTERGLOW-iTAK.zip`.

1. Get the zip onto the iPhone: AirDrop, or upload it to Google Drive and **Save to Files** on the phone.
2. In iTAK: **Settings → Network → Servers → +** (or the import button) **→ Import data package**, and pick the zip
   from Files.
3. The *AFTERGLOW* server appears and connects. The laptop's log says `TAK client 172.20.10.1 connected over TLS`.

The zip holds the phone's private key and its password (TAK packages always do). Treat it like a key:
send it only to your own team's phones, and never commit `tak/runs/`. If the laptop's IP changes, run
`tak_certs.py` again and re-import the package.

### Android (ATAK)

Either is enough:
- Multicast (same Wi-Fi, no setup): markers arrive on ATAK's default SA group, `239.2.3.1:6969`.
- Server: **Settings → Network → TAK Servers → Add**, the laptop's IP, port **8087**, protocol **TCP**. You need this for
  chat and commands both ways.

## Using it

| On the TV (the ranger) | On the phones (field teams) |
|---|---|
| **Send out · lawn-mower sweep** | Chat: "AFTERGLOW launched: Day sweep…" |
| A new ping: yellow banner "New ping needs a ranger decision" | Marker `P2 · ranger decision` in the TV's colour, plus a chat "Possible person P2…" |
| Blue banner "ALPHA (field team) asks the drone to check P2" + **Check P2 with the drone** | A team types `check P2` |
| **Investigate** | Marker `P2 · drone checking`, plus a chat "Drone checking P2 now" |
| Blue banner "ALPHA (field team) reports a person at P2" + **Confirm person · start rescue** | A team types `found P2` |
| Blue banner "…reports no one at P2" + **Not a person · continue search** | A team types `nobody P2` |
| **Confirm** | Chat "PERSON CONFIRMED by the ranger", marker `P2 · PERSON · rescue`, and the **white rescue route** |
| **Not a person** / Clear | The marker is removed, plus a chat "P2 cleared" |
| **Close case** | The route is removed, plus a chat "Rescue for P2 closed" |
| Mode buttons | Chat "AFTERGLOW now on: Trail pathing.", and the drone's label shows the mode |
| Field teams card + blue diamonds on the map | Each phone's GPS position and chat |
| **Reset** | Every pin and route removed |

Field reports go to **All Chat Rooms**, aren't case-sensitive, and must **start** the message: `found P3, he's
conscious` is a report, while `P3 is not a person` or `don't check P3` is only chat (the add-on replies with how to
report). The four are `ack P3`, `check P3`, `found P3` and `nobody P3` (`found at P3` works too). Anything else is
just a message on the TV. A phone that connects late is cleaned of old AFTERGLOW markers and gets a catch-up
message listing the open pins.

## The real system (pod in the air)

Run `tak_link.py` on the laptop **in place of** `code/afterglow/cot_out.py`. It does everything cot_out does,
both ways, and serves TLS for iTAK:

```bash
python tak/tak_link.py --config code/afterglow.toml --tls-dir tak/runs            # ag/alert from lora_rx.py
python tak/tak_link.py --config code/afterglow.toml --wifi <pi-ip> --tls-dir tak/runs   # Wi-Fi fallback
```

The TV needs the TAK version of the dashboard for the field teams in `/api/state` and `/api/rescue` (the
TV's TAK panels stay empty and it sends no route without it). That's what `tak_stack.py` serves. For the MQTT dashboard, use `tak_dashboard.make_tak_handler` in place of
`dashboard.make_handler`, and `TakState` / `TakCommands` in place of `State` / `Commands`.

## How it fits

```
fake pod / Pi ──ag/*──► tracker, geofence, locate ──► dashboard state ──SSE──► tv.html (the one TV)
                                    ▲                         ▲                     │ clicks: /api/ctrl, /api/rescue
                                    │ ag/team                 │ ag/team              ▼
                              tak_link.py ◄──── TLS 8089 / TCP 8087 / UDP ────► iTAK / ATAK phones
                                    └── ag/target, ag/pose, ag/mode, ag/rescue ───────►
```

| File | What it is |
|---|---|
| `tak_link.py` | `cot_out.py` extended both ways: markers, chat at each step, the rescue route, the TLS server, positions, chat and commands from the phones. Runs on its own (MQTT) or inside `tak_stack.py`. |
| `tak_dashboard.py` | Subclasses of the dashboard's `State` and `Commands` (field teams, `rescue()`), and its HTTP handler with `POST /api/rescue` added and `meta.tak` set, so the TV sends the rescue route. |
| `tak_stack.py` | `code/tools/local_stack.py` plus TAK, the TAK dashboard, `--feeds` and `--start-paused`, with the same Send out / Return to base. It imports local_stack and doesn't change it, so a change to local_stack's `main()` (like the base and Send out) has to be copied here too. |
| `tak_certs.py` | The certificate authority, the server certificate and the iTAK data package (`tak/runs/`). |
| `tak_contracts.py` | The two new topics, `ag/team` and `ag/rescue`, with their checks. |
| TV (`code/afterglow/web/`) | The TAK parts are built in: field-report banner, field-teams card, team markers, rescue route to TAK, *Ready to launch*. Without TAK they stay idle. `store.js`'s `ag/team` part mirrors `TakState.apply()`. |
| tests | `code/tests/test_tak_*.py`: they run with everything else (`.venv/bin/python -m pytest -q` from `code/`). |

**To make it official later** (merge into the core instead of this add-on):
1. Move `TEAM`, `RESCUE` and their checks into `contracts.py`, and the two rows in `tak_contracts.py` into
   `docs/CONTRACTS.md`.
2. Fold `tak_link.py` into `cot_out.py`, `tak_dashboard.py` into `dashboard.py` (then drop `meta.tak`), and
   `HoldClock` into `local_stack.py`. The TV is already merged.

## Troubleshooting

- **iTAK won't save the server / "error authenticating the server":** don't type the server in by hand. Import the
  data package from `tak_certs.py`.
- **Phone never connects:** is the Windows network *Private*? Is Python allowed through the firewall on 8087/8089? Does
  the IP in the package match the laptop's current IP?
- **Old markers on the phone:** they're removed when the phone connects and on Reset. ATAK honours the TAK delete
  message; the add-on also resends a removed pin already expired, for a TAK app that ignores deletes. Anything left
  from before can be deleted by hand once in iTAK. iTAK's chat history isn't touched.
- **A team's diamond is off the map:** the phones send their real GPS position. In the simulation the park is
  somewhere else, so a phone in the room shows up outside it.
