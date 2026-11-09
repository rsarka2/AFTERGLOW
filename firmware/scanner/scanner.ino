// AFTERGLOW OMNI board: passive Bluetooth LE scanner (Seeed XIAO ESP32S3 #1).
//
// Listens for advertisements and prints one JSON line per beacon heard to the Pi on Serial1:
//   {"type":"ble","addr":"aa:bb:cc:dd:ee:ff","cls":"findmy","rssi":-71}
// Formats: docs/CONTRACTS.md, "Serial lines from the ESP32 boards". Rules: firmware/README.md.
//
// Privacy (CLAUDE.md): passive scan only, never decode a payload (only the type marker is
// read), and the raw address goes ONLY to the Pi's UART. The USB debug output shows the
// class and RSSI, never an address.
//
// Arduino IDE: board "XIAO_ESP32S3" (esp32 core by Espressif), USB CDC On Boot: Enabled,
// so Serial = USB (debug) and Serial1 = the Pi's UART0 on D9 (RX) / D10 (TX).
// Unplug the 5 V wire from the Pi before plugging in USB-C to flash.

#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>

// false: only findmy, findhub, samsung_off and ref (a crowded venue can't flood the line).
// true: also apple_other and other (handy on the bench to check the near/separated split).
#define REPORT_ALL false

#define PI_BAUD 921600
#define SCAN_S 5        // each scan call lasts this long, then restarts at once
#define SUMMARY_MS 5000 // USB debug summary period

static BLEScan *scan;

// Counts per class for the USB summary. Index order matches CLS_NAMES.
static const char *CLS_NAMES[] = {"findmy", "findhub", "samsung_off", "ref", "apple_other", "other"};
enum { C_FINDMY, C_FINDHUB, C_SAMSUNG, C_REF, C_APPLE_OTHER, C_OTHER, C_COUNT };
static volatile uint32_t counts[C_COUNT];

static int classify(BLEAdvertisedDevice &dev) {
  if (dev.haveManufacturerData()) {
    auto md = dev.getManufacturerData();  // String (core 3.x) or std::string (2.x)
    size_t n = md.length();
    if (n >= 4 && (uint8_t)md[0] == 0xFF && (uint8_t)md[1] == 0xFF && md[2] == 'A' && md[3] == 'G')
      return C_REF;  // our reference beacon
    if (n >= 2 && (uint8_t)md[0] == 0x4C && (uint8_t)md[1] == 0x00) {
      // Apple: 4C 00 12 19 = offline finding, separated from its owner (the 25-byte form)
      if (n >= 4 && (uint8_t)md[2] == 0x12 && (uint8_t)md[3] == 0x19) return C_FINDMY;
      return C_APPLE_OTHER;  // includes 4C 00 12 02 (near its owner) and every other Apple frame
    }
  }
  if (dev.haveServiceData()) {
    BLEUUID feaa((uint16_t)0xFEAA);
    for (int i = 0; i < dev.getServiceDataCount(); i++)
      if (dev.getServiceDataUUID(i).equals(feaa)) return C_FINDHUB;
  }
  if (dev.haveServiceUUID() && dev.isAdvertisingService(BLEUUID((uint16_t)0xFD69)))
    return C_SAMSUNG;
  return C_OTHER;
}

class OnAdvert : public BLEAdvertisedDeviceCallbacks {
  void onResult(BLEAdvertisedDevice dev) override {
    int c = classify(dev);
    counts[c]++;
    if (!REPORT_ALL && c == C_OTHER) return;   // was: (c == C_APPLE_OTHER || c == C_OTHER)
    Serial1.printf("{\"type\":\"ble\",\"addr\":\"%s\",\"cls\":\"%s\",\"rssi\":%d}\n",
                   dev.getAddress().toString().c_str(), CLS_NAMES[c], dev.getRSSI());
  }
};

void setup() {
  Serial.begin(115200);       // USB debug; the baud rate is ignored
  Serial.setTxTimeoutMs(0);   // don't stall when no computer is listening on USB
  Serial1.setTxBufferSize(4096);
  Serial1.begin(PI_BAUD, SERIAL_8N1, D9, D10);  // baud, format, RX pin, TX pin

  BLEDevice::init("");
  scan = BLEDevice::getScan();
  scan->setAdvertisedDeviceCallbacks(new OnAdvert(), true);  // true: report every packet, not just new devices
  scan->setActiveScan(false);  // passive: we never transmit scan requests
  scan->setInterval(100);      // ms; window = interval, so it listens all the time
  scan->setWindow(100);
  Serial.println("AFTERGLOW scanner: passive BLE scan, lines to Serial1 (D9/D10) at 921600");
}

void loop() {
  scan->start(SCAN_S, false);  // blocks for SCAN_S seconds; results come through OnAdvert
  scan->clearResults();        // free the memory the scan kept

  static uint32_t last = 0;
  if (millis() - last >= SUMMARY_MS) {
    last = millis();
    Serial.printf("[%lus] heard:", (unsigned long)(millis() / 1000));
    for (int i = 0; i < C_COUNT; i++) Serial.printf(" %s=%lu", CLS_NAMES[i], (unsigned long)counts[i]);
    Serial.printf("  (REPORT_ALL=%s)\n", REPORT_ALL ? "true" : "false");
  }
}
