# Hardware integration: what's plugged in where, and how software reaches it

**Source:** Rohan's "AFTERGLOW Pod Hardware Reference for Code" (`docs/AFTERGLOW Hardware.pdf`, Nov 7), which lists the parts we actually have. It replaces the build doc's parts list.

**`docs/wiring/wiring_diagram.png` (Nov 8) and `pins.csv` are current** and agree pin for pin. The wiring page (`wiring_and_build.html`) and `pod_layout.jpg` are still out of date (Meshtastic, D6/D7 on the XIAOs).

## The pod in one picture

```
USB-C power bank 5 V 3 A ──USB-C──► Raspberry Pi 5 16 GB + Active Cooler + AI HAT+ (Hailo-8)
                                    │
   OMNI  XIAO ESP32S3 #1 ── UART0  /dev/ttyAMA0  921600 ──┤  BLE beacons (passive)
   WIFI  XIAO ESP32S3 #2 ── UART3  /dev/ttyAMA3  921600 ──┤  Wi-Fi probes, SOS page
   LoRa  RFM95W (SX1276) ── SPI1   /dev/spidev1.0 ────────┤──► 915 MHz ──► ADALM-PLUTO SDR ── USB ── laptop ──► ATAK phone
   IMU   BNO086          ── I²C-1  0x4B, INT + RST ───────┤
   Thermal HTI HT-203U   ── USB  /dev/v4l/by-id/… (replaces the MLX90640; 256×392 YUYV with raw heat values)
   Camera Module 3       ── CSI ribbon
   AI HAT+               ── PCIe ribbon  /dev/hailo0
   Radar MR60BHA2 kit    ── USB  /dev/radar  (tier 3)
   GPS   SparkFun NEO-F10N + ANN-MB5 antenna ── UART2  /dev/ttyAMA2  38400 (off for the indoor demo: gps = "")
```

We have three XIAO ESP32S3 boards. There's only **one RFM95W** (the pod's), so the laptop receives LoRa with an **ADALM-PLUTO** SDR instead. **Sun 8 Nov, ~01:40:** the original WIFI board (#2) stopped showing up on USB at all (not even in BOOT mode), so **XIAO #3 is now the WIFI board** (same sketch, same wiring). There's no spare and no ref beacon board now.

## Device table

| Part | Connects to | Linux sees it as | Settings | Python library | Quick test |
|---|---|---|---|---|---|
| OMNI board (XIAO #1, BLE scanner) | Pins 8 TX, 10 RX, 2 5V, 9 GND | `/dev/ttyAMA0` | 921600 8N1 | pyserial | `tools/check_hw.py --only omni` |
| WIFI board (XIAO #2, probes + SOS page) | Pins 24 TX, 21 RX, 2 5V, 14 GND | `/dev/ttyAMA3` | 921600 8N1 | pyserial | `--only wifi` |
| Pod LoRa radio (Adafruit RFM95W) | SPI1: pins 12, 35, 38, 40; RST 22; G0 18; 3.3 V 17; GND 39 | `/dev/spidev1.0` | `[lora]` in the config | adafruit-circuitpython-rfm9x via Blinka; `afterglow/radio.py` | `--only lora` (sends one heartbeat) |
| Laptop LoRa receiver (ADALM-PLUTO SDR + RX antenna) | Its **USB** micro-USB port (not PWR) to the laptop; antenna on the **RX** SMA | libiio `usb:` (or `ip:192.168.2.1`) | `[lora]` in the config; 915 MHz is inside the stock 325–3800 MHz range | GNU Radio + gr-lora_sdr (conda); `tools/pluto_rx.py` → `lora_rx.py` | `iio_info -s` lists the Pluto; then the pod's heartbeat shows in `ag/alert` |
| IMU: SparkFun BNO086 (SEN-22857) | Pins 1, 3, 5, 6; INT pin 15 (GPIO22); RST pin 16 (GPIO23) | I²C bus 1, `0x4B` | 400 kHz | adafruit-circuitpython-bno08x | `--only i2c` |
| Thermal: HTI HT-203U (replaces the MLX90640) | USB-C plug, through a USB-C-to-A adapter to a USB port | a V4L2 video device; use `/dev/v4l/by-id/…`, never `/dev/videoN` | 256×392 YUYV: rows 0–191 are raw heat (16 counts/°C), rows 196–387 the picture (TODO.md, thermal) | OpenCV (`python3-opencv` from apt) | `v4l2-ctl --list-formats-ext`; `--only thermal` to write |
| Camera Module 3 (imx708) | CSI ribbon, 22-to-15-pin | libcamera camera 0 | 4608×2592 full frame | picamera2 (apt) | `rpicam-hello --list-cameras` |
| AI HAT+ 26 TOPS (Hailo-8) | PCIe ribbon + header | `/dev/hailo0` | PCIe Gen 3 | hailo-all (apt) | `hailortcli fw-control identify` |
| Radar MR60BHA2 kit (tier 3) | USB-C to a USB 2 port | `/dev/ttyACM*` → `/dev/radar` | 115200 JSON lines, from our sketch | pyserial | `--only usb_serial` |
| GPS: SparkFun NEO-F10N + ANN-MB5 antenna | Pins 29 (its TX), 7 (its RX), 30 GND; power per its labels (5 V pin 4, else 3V3) | `/dev/ttyAMA2` | 38400 | pynmea2 | `--only gps` (SKIP while `gps = ""`) |
| Power | USB-C power bank, 5 V 3 A | — | no `PSU_MAX_CURRENT`, no `usb_max_current_enable` | — | `vcgencmd get_throttled` must be `0x0` |

## Pin map

Hold the Pi with the USB and Ethernet ports toward you and the header on the right. Pin 1 is the top-left socket, farthest from the ports. Odd pins run down the inner column and even pins down the board edge, two per row: row *n* is pins 2*n*−1 and 2*n*. `wiring/pi_header_labelled.jpg` shows our actual board, labelled. `wiring/pins.csv` is the same table as data.

| Pin | Signal | Goes to | | Pin | Signal | Goes to |
|---|---|---|---|---|---|---|
| 1 | 3.3 V | IMU 3V3 | | 2 | 5 V | OMNI + WIFI 5 V (Y-lead) |
| 3 | GPIO2 SDA | IMU SDA | | 4 | 5 V | GPS VCC (if the breakout takes 5 V) |
| 5 | GPIO3 SCL | IMU SCL | | 6 | GND | IMU GND |
| 7 | GPIO4 UART2 TX | GPS RX | | 8 | GPIO14 UART0 TX | OMNI D9 (RX) |
| 9 | GND | OMNI GND | | 10 | GPIO15 UART0 RX | OMNI D10 (TX) |
| | | | | 12 | GPIO18 SPI1 CE0 | LoRa CS (driven by code) |
| | | | | 14 | GND | WIFI GND |
| 15 | GPIO22 | IMU INT (active low) | | 16 | GPIO23 | IMU RST (active low) |
| 17 | 3.3 V | LoRa VIN | | 18 | GPIO24 | LoRa G0 (DIO0) |
| 21 | GPIO9 UART3 RX | WIFI D10 (TX) | | 22 | GPIO25 | LoRa RST (active low) |
| | | | | 24 | GPIO8 UART3 TX | WIFI D9 (RX) |
| 27 | ID_SD | **AI HAT+ only, don't wire** | | 28 | ID_SC | **AI HAT+ only, don't wire** |
| 29 | GPIO5 UART2 RX | GPS TX | | 30 | GND | GPS GND |
| 35 | GPIO19 SPI1 MISO | LoRa MISO | | 36 | GPIO16 | **Leave free:** the kernel's SPI1 CS after `cs0_pin=16` |
| | | | | 38 | GPIO20 SPI1 MOSI | LoRa MOSI |
| 39 | GND | LoRa GND | | 40 | GPIO21 SPI1 SCLK | LoRa SCK |

- TX always crosses to RX. The sockets on top of the AI HAT+ are **female**, so every lead needs a male pin at the Pi end.
- **Free for later:** GPIO6, 7, 10, 11, 12, 13, 17, 26 and 27 (pins 31, 26, 19, 23, 32, 33, 11, 37 and 13).

### XIAO pins

| Board | XIAO pad | Goes to |
|---|---|---|
| OMNI and WIFI | D9 (GPIO8), `Serial1` RX | Pi UART TX (pin 8 OMNI, 24 WIFI) |
| | D10 (GPIO9), `Serial1` TX | Pi UART RX (pin 10 OMNI, 21 WIFI) |
| | 5V, GND | Pi pin 2; GND pin 9 (OMNI) or 14 (WIFI) |
| | D6, D7 | unused |
| XIAO #3 | — | **Now the WIFI board** (replaced #2, which died Sun 8 Nov). Wired like WIFI above |

## Pi setup (once)

1. Flash Raspberry Pi OS Bookworm 64-bit. Enable SSH, Wi-Fi to the team's phone hotspot and a hostname such as `afterglow`.
2. Clone the repo to `/home/pi/afterglow` and run `bash pi/setup_pi.sh`. It:
   - installs Mosquitto, i2c-tools, picamera2, lgpio and `hailo-all`;
   - appends `pi/config.txt.append` to `/boot/firmware/config.txt`;
   - turns the serial console off the GPIO UART;
   - adds you to the `dialout`, `i2c`, `spi`, `gpio` and `video` groups;
   - makes a venv with `--system-site-packages`.
3. Reboot.
4. Optional:
   - `pi/99-afterglow.rules` gives the radar a stable `/dev/radar`;
   - `pi/mosquitto-afterglow.conf` lets teammates watch the bus;
   - `pi/systemd/afterglow@.service` starts modules at boot.
5. Run `cd code && .venv/bin/python tools/check_hw.py`. Bring-up is done when every connected part passes. GPS shows SKIP while `gps = ""`.

## Gotchas we already know about

- **Board silent on its UART:**
  - The XIAO must print on `Serial1` at D9/D10: `Serial1.begin(921600, SERIAL_8N1, D9, D10)`. `Serial` is the USB port. See `firmware/README.md`.
  - TX must cross to RX.
  - The two boards share pin 2 for 5 V but have separate grounds (pins 9 and 14). Both must be connected.
  - Unplug a board's 5 V lead from the Pi before plugging USB-C into it to flash it.
- **Pi 5 serial names:** on the Pi 5, `/dev/ttyAMA10` is the little 3-pin debug connector, not the GPIO header. Our ports are `ttyAMA0`, `ttyAMA2` and `ttyAMA3`, and they only exist after the `config.txt` lines and a reboot.
- **SPI:**
  - Keep SPI0 off. `dtparam=spi=on` must not be set, because SPI0 would take GPIO8/9 from WIFI's UART3. `check_hw.py --only config` catches it.
  - The pod radio is on SPI1. adafruit_rfm9x drives CS on GPIO18 itself, so `config.txt` moves the kernel's CS to the free GPIO16 (`dtoverlay=spi1-1cs,cs0_pin=16`). Without that you get "GPIO busy".
- **GPIO from Python:** use gpiozero, lgpio or Blinka. `RPi.GPIO` doesn't work on a Pi 5.
- **LoRa:**
  - The pod's RFM95W needs the 915 MHz spring antenna soldered to ANT **before it powers up**. Never transmit without it.
  - The laptop side is an ADALM-PLUTO SDR on USB, receive only (antenna on RX; never transmit from it). It doesn't decode LoRa itself: GNU Radio's gr-lora_sdr does that on the laptop (`tools/pluto_rx.py`).
  - Pluto on macOS: plug into the port marked **USB**, not PWR. Use the libiio URI `usb:`; the `ip:192.168.2.1` network link may not come up on macOS (older Pluto firmware uses RNDIS).
  - Keep the Pluto at least 1 m from the pod: a 13 dBm transmitter next to it can overload the receiver.
  - It's plain LoRa with no encryption, so anyone on 915 MHz can read an alert. Alerts carry a pin, never a device address or ID. `lora_rx.py` drops other teams' packets by node address.
  - If nothing arrives, check that `pluto_rx.py` runs with the same `[lora]` table as the pod (same config file), and that the Pluto sees the signal at all (a waterfall at 915 MHz, e.g. in gqrx). Then check that lora_out isn't silenced: a remembered `lora_off` leaves `runs/lora_off.json`, and lora_out logs "LoRa is OFF" at start-up.
  - Only one process may drive the radio. Stop `afterglow@lora_out` before `check_hw.py --only lora`.
- **USB serial:** the radar is the only USB serial device. Still use `/dev/radar` or `/dev/serial/by-id/…`, never `ttyACM0`.
- **Thermal camera (HT-203U):**
  - It's a phone accessory (8–14 µm, −20 to 550 °C, ±2 °C, 28 g) powered from the USB port.
  - One user got a picture from it on Linux as a plain video device. Nobody we found has read temperatures from it there, so check its formats first (TODO, `thermal.py`).
  - The Camera Module 3 also makes `/dev/video*` nodes, so the numbers shift between boots. Use `/dev/v4l/by-id/…` or a `/dev/thermal` udev rule.
  - Its field of view is unknown, so measure how high it can still see a person.
- **Power:**
  - A 5 V 3 A power bank can't announce 5 A, so the Pi may warn and will limit its USB ports to 600 mA in total. The radar and the thermal camera should fit; check `get_throttled` with both plugged in.
  - Don't set `PSU_MAX_CURRENT=5000` or `usb_max_current_enable=1`. Both tell the Pi it can draw more than the bank gives.
  - The real risk is voltage sag. If `get_throttled` isn't `0x0` with everything running (detector included), use a shorter, thicker USB-C cable, or run the detector only during Investigate.
- **Heat:** the AI HAT+ adds heat. With the lid closed for 20 minutes, `vcgencmd measure_temp` must stay under 80 °C. If it doesn't, run the detector only during Investigate.
- **I²C:**
  - The BNO086 sits at `0x4B` (ADR jumper open). SparkFun says it only runs reliably with RST and INT connected, which is why they're wired.
  - Start from `BNO08X_I2C(board.I2C(), address=0x4B, reset=DigitalInOut(board.D23))`.
  - If reads fail now and then, drop the bus to 100 kHz (`dtparam=i2c_arm_baudrate=100000`). The IMU is the only I²C part now that the thermal camera is USB, so nothing needs 400 kHz.
- **Camera and Hailo from a venv:** both come from apt, so the venv must be created with `--system-site-packages`, or the imports fail.
- **Hailo detection (stretch goal since Sat 7 Nov; `camera.py` is a plain live view):** the heat and power notes above about "the detector" apply only if it's built.
  - Start from a model that's already compiled. `rpicam-apps` ships Hailo post-processing stages (see `/usr/share/rpi-camera-assets/hailo_*.json`), and Hailo's `hailo-rpi5-examples` repository has Python detection pipelines.
  - Keep only the `person` class, and tile full frames into 640×640 pieces (build doc, "Person detection plan").
- **Radar:**
  - The kit ships with ESPHome. Reflash its XIAO ESP32C6 with an Arduino sketch (`Seeed_Arduino_mmWave`, class `SEEED_MR60BHA2`) that prints JSON lines at 115200. `radar.py` maps them onto `ag/radar`.
  - Updating the radar module's own firmware wipes the sketch, so reflash it afterwards.
- **GPS:**
  - A SparkFun NEO-F10N (L1/L5) with the ANN-MB5 antenna is wired to UART2. It sends NMEA at 38400 (checked Sat 7 Nov; indoors it sees 4 satellites but gets no fix).
  - **Keep `[serial] gps = ""` for the indoor demo**, so position.py runs on the taped waypoints (`ag/ctrl` `waypoint`). A fix near a window would place the pod relative to the Acadia origin. Set `gps = "/dev/ttyAMA2"` only for an outdoor test or after moving the origin to the venue.
- **Radio coexistence (on a larger drone):**
  - Our LoRa is 915 MHz, which shares a band with an ELRS 900 MHz control link.
  - The SOS page is 2.4 GHz Wi-Fi, which shares a band with ELRS 2.4 GHz.
  - `lora_out.py` and the WIFI board must obey `ag/ctrl` `lora_off` and `sos_off`.

## Drone facts that matter to software

These matter for the pitch: we demo the payload on its own, and it's ready to fly on a larger drone.

- **Flight time:** a 5" FPV carrying the 0.4 kg pod flies only about 2–3 minutes; a 7" does better. Patrol legs must be short.
- **No autopilot:** Betaflight has no waypoint missions. `planner.py` proposes legs; the ranger accepts them on the dashboard; the visual observer reads heading and distance to the pilot.
- **Radar** reads breathing only when landed within about 1.5 m. Drone vibration swamps it in flight.
- **Thermal** is 256×192 pixels (the HT-203U), eight times the old MLX90640's 32×24, which lost a person at about 8 m. Measure the new limit before quoting it in the pitch.
