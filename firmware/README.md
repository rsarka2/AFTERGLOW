# ESP32 firmware: what the Pi and the laptop expect

The sketches are `scanner/` (OMNI), `wifi_listener/` (WIFI) and `lora_rx/` (laptop radio). sos_page (tier 2) isn't written yet. All pod sketches print to the Pi on `Serial1` at D9/D10 (below), and their USB debug output never shows an address. This page covers only what the firmware must do for the Python side to work. Owner: Utility.

Pins and parts come from `docs/HARDWARE.md`. The line formats are in `docs/CONTRACTS.md`, "Serial lines from the ESP32 boards".

## Both pod XIAO ESP32S3 boards (OMNI and WIFI)

- **Print to `Serial1` on D9/D10, not `Serial`:**
  - The Pi's TX goes to D9 (GPIO8, RX) and its RX to D10 (GPIO9, TX). Use `Serial1.begin(921600, SERIAL_8N1, D9, D10)`; the arguments are baud, format, RX pin, TX pin.
  - With *USB CDC On Boot: Enabled*, `Serial` is the USB port. Keep it for debug output on the bench.
  - The wrong port is the most likely reason `check_hw.py` reports a board as "silent".
- **Line format:** 921600 baud, 8N1. One JSON object per line, `\n` terminated.
- **Power:** 5 V and GND come from the Pi's header (pins 2 and 9 for OMNI; 2 and 14 for WIFI). **Unplug that 5 V wire before you plug in USB-C to flash.**
- **Antenna:** each board needs the stick-on antenna from its own box on the U.FL socket: OMNI on the left wall, WIFI on the right, as far apart as possible. Without it, range drops to a few metres.

## OMNI: scanner

- **Passive scan only:** `setActiveScan(false)`, interval = window, so it listens all the time.
- **Always report** `findmy`, `findhub`, `samsung_off` and `ref`. Report everything else only when `REPORT_ALL` is true, so a crowded venue can't flood the serial line.
- **Send only** `type`, `addr`, `cls` and `rssi`. **Never put payload bytes on the line**: that's a privacy rule.
- **Classify:**

| `cls` | Match |
|---|---|
| `findmy` | Manufacturer data `4C 00 12 19`: Apple offline finding, **separated** from its owner (the full 25-byte payload) |
| `apple_other` | `4C 00 12 02` (offline finding **near** its owner: every passing iPhone), and any other `4C 00` manufacturer data |
| `findhub` | Service data UUID `0xFEAA` |
| `samsung_off` | Service UUID `0xFD69` |
| `ref` | Manufacturer data `FF FF 'A' 'G'` (our reference beacon) |
| `other` | Anything else |

- The separated/near split comes from the hardware reference. **Check it against our own AirTag and a powered-off iPhone on Saturday** before trusting it.
- The advertising address rotates about every 15 minutes near the owner and about once a day when separated.
- **Fallback:** if the built-in BLE library drops packets in a crowded room, switch to NimBLE-Arduino.

## WIFI: wifi_listener (tier 1), plus the SOS page (tier 2), in one sketch

**Built Sat 7 Nov:** the SOS page lives in `wifi_listener/wifi_listener.ino` (one board, one sketch). It's off at boot. `sos_on` opens the open AP `SOS-RESCUE` on channel 6 with a captive page (hurt? can walk? how many?), and each answer prints a `sos` line (`rssi` = the strongest connected phone, or `null`). `sos_off` closes it and resumes hopping. The TV dashboard has SOS On/Off buttons; or `mosquitto_pub -t ag/ctrl -m '{"t":0,"cmd":"sos_on"}'`. Compile-check without the IDE: `arduino-cli compile --fqbn esp32:esp32:XIAO_ESP32S3 firmware/wifi_listener`.

### Original notes

- **wifi_listener:** promiscuous mode, management frames, probe requests (`0x40`). Hop channels 1, 6 and 11 every 250 ms. Print `{"type":"wifi",…}` lines.
- **sos_page:** an open AP named `SOS-RESCUE` with a captive page (`WiFi.softAP()`, plus `DNSServer` answering every name with the board's IP, plus `WebServer`). Print `{"type":"sos",…}` when someone answers.
  - Accept `{"cmd":"sos_on"}` and `{"cmd":"sos_off"}` lines from the Pi.
  - The hardware reference's `{"cmd":"sos","on":true,"msg":…}` isn't used. A custom `msg` on `sos_on` and a "phone joined" event are tier-2 ideas, and they need a contract change first.
  - The pod switches the page off if it hurts the pilot's link.
- **Known clash:** while the SOS access point is up, the radio stays on the AP's channel, so the probe listener only hears that one channel. That's fine: the SOS page is for Investigate, when we're hovering over a pin.

## Laptop LoRa radio: XIAO #3 + RFM95W (`lora_rx/`): not used now

**We have only one RFM95W**, so the laptop receives with an ADALM-PLUTO SDR and gr-lora_sdr (`code/tools/pluto_rx.py`, see `docs/HARDWARE.md`). XIAO #3 is now the WIFI board, so there is no board for it either. Keep this sketch: it's the receiver again if a second RFM95W turns up.


- `lora_rx/lora_rx.ino` is a listen-only modem. It prints every packet as a JSON line on USB, and `code/afterglow/lora_rx.py` does the filtering and decoding.
- **Arduino IDE:** board `XIAO_ESP32S3`, USB CDC On Boot: Enabled. Library: **LoRa** by Sandeep Mistry, from the Library Manager.
- **Why not RadioHead** (the hardware reference's suggestion):
  - The LoRa library polls, so there's no interrupt handler on the ESP32.
  - It's in the Library Manager.
  - It passes the RadioHead header through as plain bytes, which is exactly what the pod's adafruit_rfm9x sends.
- **Radio settings** are `#define`s at the top of the sketch. They must match `[lora]` in `code/afterglow.toml`; `tests/test_lora_rx.py` checks.
- Solder the 915 MHz spring antenna to ANT before powering it. This is the last XIAO; there's no spare.

## Reference beacon (`ref_beacon/`): no board for it now

XIAO #3 became the WIFI board on Sun 8 Nov (the original WIFI board died), so nothing runs this sketch at the moment. Flash it on any spare ESP32 if one turns up.

- A test target we control: it advertises manufacturer data `FF FF 'A' 'G'`, which OMNI reports as `ref`. Tracker and geofence pin it in patrol and night, not in search.
- Used for I6 (a pin within 5 m), the range test and the demo baseline. No wiring: USB-C power and the U.FL antenna.
- **Quiet on purpose** (`ESP_PWR_LVL_N12`): a beacon whose median RSSI stays above `[track] with_drone_rssi` (-55) while the pod moves is taken to ride on the pod and never pinned. If OMNI doesn't hear it, try `N6`; if it goes grey (`with_drone`), go lower.

## Pod LoRa radio

No firmware. It's a bare RFM95W on the Pi's SPI1, driven from Python (`code/afterglow/radio.py`).

## Radar kit (tier 3)

- The MR60BHA2 kit ships with ESPHome. Reflash its XIAO ESP32C6 with an Arduino sketch (`Seeed_Arduino_mmWave`, class `SEEED_MR60BHA2`) that prints one JSON line per reading at 115200, for example `{"breath":14.2,"heart":78,"dist":0.9,"human":true}`. `radar.py` maps that onto `ag/radar`.
- Updating the radar module's own firmware wipes the sketch, so reflash it afterwards.
