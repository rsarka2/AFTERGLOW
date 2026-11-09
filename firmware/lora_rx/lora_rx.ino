// AFTERGLOW laptop LoRa radio: Seeed XIAO ESP32S3 #3 + Adafruit RFM95W (SX1276).
//
// A dumb modem that only listens. Every LoRa packet it hears goes to USB as one JSON line,
// header and all; code/afterglow/lora_rx.py checks the sender and decodes the 16-byte alert.
// Line formats: docs/CONTRACTS.md, "Laptop LoRa radio".
//
// Arduino IDE: board "XIAO_ESP32S3" (esp32 core), USB CDC On Boot: Enabled (Serial = USB).
// Library: "LoRa" by Sandeep Mistry (Library Manager). It polls, so there's no interrupt
// handler, and it passes the 4-byte RadioHead header through as plain bytes, which is
// exactly what the pod's adafruit_rfm9x sends.
//
// Wiring (docs/HARDWARE.md): 5V -> VIN, GND -> GND, D8 -> SCK, D9 -> MISO, D10 -> MOSI,
// D0 -> CS, D1 -> RST, D3 -> G0 (wired but unused: we poll). D6 and D7 are free.
// Solder the 915 MHz spring antenna to ANT before powering it.

#include <SPI.h>
#include <LoRa.h>

// Must match [lora] in code/afterglow.toml. tests/test_lora_rx.py checks these lines.
#define FREQ_MHZ 915.0
#define SF 7
#define BW_HZ 125000
#define CR 5
#define PREAMBLE 8

const int PIN_CS = D0;
const int PIN_RST = D1;
const int PIN_G0 = D3;

static void printCfg() {
  Serial.printf("{\"type\":\"cfg\",\"freq\":%.3f,\"sf\":%d,\"bw\":%ld,\"cr\":%d,\"pre\":%d}\n",
                FREQ_MHZ, SF, (long)BW_HZ, CR, PREAMBLE);
}

void setup() {
  Serial.begin(115200);  // USB CDC: the baud rate is ignored
  SPI.begin(D8, D9, D10, PIN_CS);  // SCK, MISO, MOSI, SS
  LoRa.setSPI(SPI);
  LoRa.setPins(PIN_CS, PIN_RST, PIN_G0);
  while (!LoRa.begin((long)(FREQ_MHZ * 1e6))) {
    Serial.println("{\"type\":\"err\",\"msg\":\"no RFM95W: check SPI wiring, CS on D0, RST on D1, power\"}");
    delay(2000);
  }
  LoRa.setSpreadingFactor(SF);
  LoRa.setSignalBandwidth(BW_HZ);
  LoRa.setCodingRate4(CR);
  LoRa.setPreambleLength(PREAMBLE);
  LoRa.enableCrc();  // packets with a bad CRC are dropped by parsePacket()
  printCfg();
}

void loop() {
  // lora_rx.py sends "?" when it opens the port: answer with the radio settings.
  while (Serial.available()) {
    if (Serial.read() == '?') printCfg();
  }

  int n = LoRa.parsePacket();
  if (n <= 0) return;

  static char hex[2 * 255 + 1];
  int len = 0;
  while (LoRa.available() && len < 255) {
    snprintf(hex + 2 * len, 3, "%02x", (uint8_t)LoRa.read());
    len++;
  }
  hex[2 * len] = '\0';
  Serial.printf("{\"type\":\"rx\",\"rssi\":%d,\"snr\":%.1f,\"hex\":\"%s\"}\n",
                LoRa.packetRssi(), LoRa.packetSnr(), hex);
}
