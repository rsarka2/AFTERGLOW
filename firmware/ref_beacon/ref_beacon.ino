// AFTERGLOW reference beacon: any spare ESP32S3 advertising as our `ref` class (no board runs it now).
//
// A test target we control, for I6 (a pin within 5 m on the taped trail), the range test and
// the demo baseline. OMNI's scanner classifies manufacturer data FF FF 'A' 'G' as `ref`
// (firmware/README.md); tracker and geofence pin it in patrol and night, not in search.
//
// Arduino IDE: board "XIAO_ESP32S3", USB CDC On Boot: Enabled. No extra library.
// Power: USB-C from a power bank or the laptop. No wiring. Stick the U.FL antenna on.
//
// It sends only the 4-byte marker: no name, no payload, non-connectable.

#include <BLEDevice.h>
#include <BLEAdvertising.h>

// Quiet on purpose, roughly like an AirTag. The tracker marks a beacon whose median RSSI stays
// above [track] with_drone_rssi (-55) while the pod moves as riding on the pod, and never pins it.
// If OMNI never hears it, try ESP_PWR_LVL_N6; if it's marked with_drone (grey), go lower.
#define TX_POWER ESP_PWR_LVL_N12
#define ADV_MS 200  // advertising interval

void setup() {
  Serial.begin(115200);  // USB debug only
  BLEDevice::init("");
  BLEDevice::setPower(TX_POWER, ESP_BLE_PWR_TYPE_ADV);

  const uint8_t marker[] = {0xFF, 0xFF, 'A', 'G'};
  BLEAdvertisementData data;
  data.setFlags(0x06);  // LE general discoverable, no BR/EDR
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  String md;
  for (uint8_t b : marker) md += (char)b;
#else
  std::string md((const char *)marker, sizeof marker);
#endif
  data.setManufacturerData(md);

  BLEAdvertising *adv = BLEDevice::getAdvertising();
  adv->setAdvertisementData(data);
  adv->setScanResponse(false);
  // Non-connectable. Core 3.3+ runs NimBLE on the S3; older cores run Bluedroid.
#if defined(CONFIG_NIMBLE_ENABLED)
  adv->setAdvertisementType(BLE_GAP_CONN_MODE_NON);
#else
  adv->setAdvertisementType(ADV_TYPE_NONCONN_IND);
#endif
  adv->setMinInterval(ADV_MS / 0.625);  // units of 0.625 ms
  adv->setMaxInterval(ADV_MS / 0.625);
  adv->start();
}

void loop() {
  Serial.printf("[%lus] AFTERGLOW ref beacon advertising (FF FF 'A' 'G', every %d ms)\n",
                (unsigned long)(millis() / 1000), ADV_MS);
  delay(5000);
}
