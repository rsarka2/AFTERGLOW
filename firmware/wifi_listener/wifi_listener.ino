// AFTERGLOW WIFI board (Seeed XIAO ESP32S3 #2): passive Wi-Fi probe listener + the SOS page.
//
// Normally (SOS off, the default) it only listens: it hops channels 1, 6 and 11 every 250 ms and
// prints one JSON line per probe request to the Pi on Serial1:
//   {"type":"wifi","addr":"da:a1:19:00:00:01","rand":true,"rssi":-80,"ch":6}
// rand = the address is randomized (locally administered bit set).
//
// SOS page (tier 2): on {"cmd":"sos_on"} from the Pi (bridge.py forwards ag/ctrl sos_on) it opens an
// open access point "SOS-RESCUE" on channel SOS_CH. A phone that joins gets a captive page with three
// questions (hurt? can you walk? how many?), and each answer goes to the Pi as:
//   {"type":"sos","hurt":true,"walk":false,"count":2,"rssi":-55}
// rssi = the strongest phone connected to the AP. {"cmd":"sos_off"} closes the AP and resumes hopping.
// While the AP is up, the radio stays on SOS_CH, so the probe listener hears that channel only
// (fine: the page runs during Investigate, hovering over a pin).
//
// Formats: docs/CONTRACTS.md, "Serial lines from the ESP32 boards". Rules: firmware/README.md.
// Privacy (CLAUDE.md): the listener only receives, and reads only the frame type, the sender
// address and the RSSI. The SOS AP is the pod's only transmitter, off unless the Pi turns it on.
// Raw addresses go ONLY to the Pi's UART; USB debug never shows one, and an SOS line has none.
//
// Arduino IDE: board "XIAO_ESP32S3", USB CDC On Boot: Enabled (Serial = USB debug,
// Serial1 = the Pi's UART3 on D9 (RX) / D10 (TX)). No extra library.
// Unplug the 5 V wire from the Pi before plugging in USB-C to flash.

#include <WiFi.h>
#include <DNSServer.h>
#include <WebServer.h>
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

#define PI_BAUD 921600
#define HOP_MS 250
#define SUMMARY_MS 5000
#define SOS_SSID "SOS-RESCUE"
#define SOS_CH 6
#define ANSWER_GAP_MS 2000  // at most one answer line every 2 s (a double tap sends one)

static const uint8_t CHANNELS[] = {1, 6, 11};
static uint8_t chIdx = 0;

struct Probe {
  uint8_t addr[6];
  int8_t rssi;
  uint8_t ch;
};
static QueueHandle_t q;
static volatile uint32_t heard = 0, dropped = 0, randCount = 0;

static bool sosOn = false;
static uint32_t answers = 0;
static DNSServer dns;
static WebServer web(80);
static const IPAddress AP_IP(192, 168, 4, 1);

// ---------------------------------------------------------------- probe listener

// Runs in the Wi-Fi task: keep it tiny, just queue the probe.
static void onPacket(void *buf, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_MGMT) return;
  const wifi_promiscuous_pkt_t *p = (const wifi_promiscuous_pkt_t *)buf;
  if (p->rx_ctrl.sig_len < 24) return;   // shorter than a management header
  if (p->payload[0] != 0x40) return;     // frame control: management, subtype 4 = probe request
  Probe pr;
  memcpy(pr.addr, p->payload + 10, 6);   // address 2 = the sender
  pr.rssi = p->rx_ctrl.rssi;
  pr.ch = p->rx_ctrl.channel;
  if (xQueueSend(q, &pr, 0) != pdTRUE) dropped++;  // full: drop rather than block the Wi-Fi task
}

static void startListening() {
  wifi_promiscuous_filter_t filt;
  filt.filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT;
  esp_wifi_set_promiscuous_filter(&filt);
  esp_wifi_set_promiscuous_rx_cb(&onPacket);
  esp_wifi_set_promiscuous(true);
}

// ---------------------------------------------------------------- SOS page

static const char PAGE[] PROGMEM = R"HTML(<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SOS - search team</title>
<style>
body{font-family:-apple-system,system-ui,sans-serif;margin:0;padding:20px;background:#fff;color:#111;max-width:480px}
h1{font-size:26px;margin:0 0 6px;color:#b00}p{font-size:17px;line-height:1.4;margin:0 0 18px}
fieldset{border:0;padding:0;margin:0 0 20px}legend{font-size:20px;font-weight:700;margin-bottom:10px}
label{display:block;font-size:20px;padding:14px;border:2px solid #ccc;border-radius:10px;margin-bottom:8px}
input[type=radio]{transform:scale(1.6);margin-right:14px}
select{font-size:22px;padding:10px;width:100%}
button{width:100%;font-size:24px;font-weight:700;padding:18px;border:0;border-radius:12px;background:#b00;color:#fff}
</style></head><body>
<h1>Search team nearby</h1>
<p>A search drone is overhead. Answer three questions and tap Send. Your answer goes to the rescue team.</p>
<form action="/answer" method="get">
<fieldset><legend>Are you hurt?</legend>
<label><input type="radio" name="hurt" value="1" required>Yes, I'm hurt</label>
<label><input type="radio" name="hurt" value="0">No</label></fieldset>
<fieldset><legend>Can you walk?</legend>
<label><input type="radio" name="walk" value="1" required>Yes</label>
<label><input type="radio" name="walk" value="0">No</label></fieldset>
<fieldset><legend>How many people are with you, counting you?</legend>
<select name="count"><option>1</option><option>2</option><option>3</option><option>4</option>
<option>5</option><option value="6">6 or more</option></select></fieldset>
<button type="submit">Send</button>
</form></body></html>)HTML";

static const char THANKS[] PROGMEM = R"HTML(<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sent</title><style>body{font-family:-apple-system,system-ui,sans-serif;padding:20px;max-width:480px}
h1{color:#070}p{font-size:19px;line-height:1.4}a{font-size:19px}</style></head><body>
<h1>Sent to the search team</h1>
<p>Stay where you are if you can, and keep your phone on.</p>
<p>If something changes, you can send again.</p>
<p><a href="/">Send another answer</a></p></body></html>)HTML";

static int strongestStationRssi() {
  wifi_sta_list_t list;
  if (esp_wifi_ap_get_sta_list(&list) != ESP_OK || list.num == 0) return 0;
  int best = -127;
  for (int i = 0; i < list.num; i++) best = max(best, (int)list.sta[i].rssi);
  return best;
}

static void handlePage() {
  web.sendHeader("Cache-Control", "no-store");
  web.send_P(200, "text/html", PAGE);
}

static void handleAnswer() {
  if (!web.hasArg("hurt") || !web.hasArg("walk")) {  // an incomplete form: show it again
    handlePage();
    return;
  }
  static uint32_t lastAnswer = 0;
  if (answers == 0 || millis() - lastAnswer >= ANSWER_GAP_MS) {
    lastAnswer = millis();
    answers++;
    bool hurt = web.arg("hurt") == "1", walk = web.arg("walk") == "1";
    int count = constrain(web.arg("count").toInt(), 1, 63);  // the alert byte holds 0-63
    int rssi = strongestStationRssi();
    if (rssi < 0) {
      Serial1.printf("{\"type\":\"sos\",\"hurt\":%s,\"walk\":%s,\"count\":%d,\"rssi\":%d}\n",
                     hurt ? "true" : "false", walk ? "true" : "false", count, rssi);
    } else {
      Serial1.printf("{\"type\":\"sos\",\"hurt\":%s,\"walk\":%s,\"count\":%d,\"rssi\":null}\n",
                     hurt ? "true" : "false", walk ? "true" : "false", count);
    }
    Serial.printf("SOS answer #%lu: hurt=%d walk=%d count=%d rssi=%d\n", (unsigned long)answers, hurt, walk, count, rssi);
  }
  web.sendHeader("Cache-Control", "no-store");
  web.send_P(200, "text/html", THANKS);
}

// Phones probe a known URL to spot a captive portal; anything but the expected answer pops the page.
static void handleOther() {
  web.sendHeader("Location", String("http://") + AP_IP.toString() + "/", true);
  web.send(302, "text/plain", "");
}

static void sosStart() {
  if (sosOn) return;
  esp_wifi_set_promiscuous(false);
  WiFi.mode(WIFI_AP);
  WiFi.softAPConfig(AP_IP, AP_IP, IPAddress(255, 255, 255, 0));
  if (!WiFi.softAP(SOS_SSID, NULL, SOS_CH)) {  // NULL password: an open network anyone can join
    Serial.println("SOS: softAP failed; staying in listen mode");
    WiFi.mode(WIFI_STA);
    startListening();
    return;
  }
  dns.start(53, "*", AP_IP);  // every name -> the board, so any address the phone tries shows the page
  web.on("/", handlePage);
  web.on("/answer", handleAnswer);
  web.on("/hotspot-detect.html", handlePage);  // Apple
  web.on("/generate_204", handleOther);        // Android
  web.onNotFound(handleOther);
  web.begin();
  startListening();  // keep hearing probes, on SOS_CH only
  sosOn = true;
  Serial.printf("SOS on: open AP \"%s\" on channel %d, page at http://%s/\n", SOS_SSID, SOS_CH, AP_IP.toString().c_str());
}

static void sosStop() {
  if (!sosOn) return;
  web.stop();
  dns.stop();
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);  // no network is joined and nothing is sent
  WiFi.disconnect();
  startListening();
  esp_wifi_set_channel(CHANNELS[chIdx], WIFI_SECOND_CHAN_NONE);
  sosOn = false;
  Serial.println("SOS off: AP closed, listening on ch 1/6/11");
}

// ---------------------------------------------------------------- the Pi's commands

static void readPi() {
  static char line[96];
  static size_t n = 0;
  while (Serial1.available()) {
    char c = Serial1.read();
    if (c == '\n') {
      line[n] = 0;
      if (strstr(line, "\"sos_on\"")) sosStart();
      else if (strstr(line, "\"sos_off\"")) sosStop();
      n = 0;
    } else if (n < sizeof(line) - 1) {
      line[n++] = c;
    }
  }
}

void setup() {
  Serial.begin(115200);       // USB debug; the baud rate is ignored
  Serial.setTxTimeoutMs(0);   // don't stall when no computer is listening on USB
  Serial1.setTxBufferSize(4096);
  Serial1.begin(PI_BAUD, SERIAL_8N1, D9, D10);  // baud, format, RX pin, TX pin

  q = xQueueCreate(64, sizeof(Probe));

  WiFi.mode(WIFI_STA);   // no network is joined and nothing is sent
  WiFi.disconnect();
  startListening();
  esp_wifi_set_channel(CHANNELS[chIdx], WIFI_SECOND_CHAN_NONE);
  Serial.println("AFTERGLOW wifi_listener: probe requests on ch 1/6/11, lines to Serial1 (D9/D10) at 921600; SOS page off");
}

void loop() {
  Probe pr;
  while (xQueueReceive(q, &pr, 0) == pdTRUE) {
    bool rnd = pr.addr[0] & 0x02;  // locally administered = randomized
    heard++;
    if (rnd) randCount++;
    Serial1.printf("{\"type\":\"wifi\",\"addr\":\"%02x:%02x:%02x:%02x:%02x:%02x\",\"rand\":%s,\"rssi\":%d,\"ch\":%u}\n",
                   pr.addr[0], pr.addr[1], pr.addr[2], pr.addr[3], pr.addr[4], pr.addr[5],
                   rnd ? "true" : "false", pr.rssi, pr.ch);
  }

  if (sosOn) {
    dns.processNextRequest();
    web.handleClient();
  } else {
    static uint32_t lastHop = 0;
    if (millis() - lastHop >= HOP_MS) {
      lastHop = millis();
      chIdx = (chIdx + 1) % sizeof(CHANNELS);
      esp_wifi_set_channel(CHANNELS[chIdx], WIFI_SECOND_CHAN_NONE);
    }
  }

  readPi();

  static uint32_t lastSum = 0;
  if (millis() - lastSum >= SUMMARY_MS) {
    lastSum = millis();
    Serial.printf("[%lus] probes=%lu randomized=%lu dropped=%lu sos=%s answers=%lu phones=%d\n",
                  (unsigned long)(millis() / 1000), (unsigned long)heard, (unsigned long)randCount,
                  (unsigned long)dropped, sosOn ? "ON" : "off", (unsigned long)answers,
                  sosOn ? WiFi.softAPgetStationNum() : 0);
  }
  delay(1);
}
