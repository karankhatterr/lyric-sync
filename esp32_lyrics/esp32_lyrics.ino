/*
 * LyricSync ESP32 client — polls the /now JSON endpoint and shows lyrics
 * on a 128x64 SSD1306 I2C OLED.
 *
 * Hardware: ESP32 (mini/D1 mini form factor is fine) + SSD1306 OLED
 * Wiring:   OLED VCC->3V3, GND->GND, SDA->GPIO21, SCL->GPIO22
 *
 * Arduino IDE setup:
 *   1. Boards Manager: install "esp32" by Espressif
 *   2. Library Manager: install "ArduinoJson", "Adafruit SSD1306",
 *      "Adafruit GFX Library"
 *   3. Fill in the three settings below, select your board, upload.
 *
 * On the Mac side run:  python3 main.py --serve 8765
 */
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// ------------------------- settings -------------------------
const char* WIFI_SSID  = "YOUR_WIFI_NAME";
const char* WIFI_PASS  = "YOUR_WIFI_PASSWORD";
const char* SERVER_URL = "http://192.168.1.50:8765/now";  // your Mac's IP
const unsigned long POLL_MS = 1000;
// -------------------------------------------------------------

#define SCREEN_W 128
#define SCREEN_H 64
Adafruit_SSD1306 display(SCREEN_W, SCREEN_H, &Wire, -1);

String lastLine = "";
String lastTitle = "";

void setup() {
  Serial.begin(115200);
  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println("SSD1306 not found (try address 0x3D)");
    for (;;) delay(1000);
  }
  display.setTextColor(SSD1306_WHITE);
  showMessage("LyricSync", "connecting to WiFi...");

  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) delay(250);
  showMessage("LyricSync", WiFi.localIP().toString());
  delay(1000);
}

void loop() {
  static unsigned long last = 0;
  if (millis() - last < POLL_MS) return;
  last = millis();

  if (WiFi.status() != WL_CONNECTED) {
    showMessage("WiFi lost", "reconnecting...");
    WiFi.reconnect();
    return;
  }

  HTTPClient http;
  http.setTimeout(3000);
  http.begin(SERVER_URL);
  int code = http.GET();
  if (code != 200) {
    http.end();
    showMessage("no server", String("HTTP ") + code);
    return;
  }

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, http.getStream());
  http.end();
  if (err) { showMessage("json error", err.c_str()); return; }

  String state = doc["state"] | "idle";
  if (state == "idle") { showMessage("LyricSync", "(nothing playing)"); return; }

  String title  = doc["title"]  | "";
  String artist = doc["artist"] | "";
  String line   = doc["line"]   | "";
  String next   = doc["next_line"] | "";
  bool hasLyrics = doc["has_lyrics"] | false;
  if (!hasLyrics) line = "(no synced lyrics)";
  if (line == "") line = "~";

  // redraw only when something changed (avoids flicker)
  if (line == lastLine && title == lastTitle) return;
  lastLine = line; lastTitle = title;

  display.clearDisplay();
  display.setTextSize(1);
  // header: title - artist, truncated to one row (21 chars)
  String header = title + " - " + artist;
  display.setCursor(0, 0);
  display.println(ascii(header).substring(0, 21));
  display.drawFastHLine(0, 10, SCREEN_W, SSD1306_WHITE);
  // current line, word-wrapped, up to 4 rows
  drawWrapped(ascii(line), 14, 4);
  // next line preview on the bottom row
  display.setCursor(0, 56);
  display.print("> " + ascii(next).substring(0, 19));
  display.display();
}

// --- helpers ---

// GFX default font is ASCII-only; replace anything else
String ascii(String s) {
  String out = "";
  for (unsigned int i = 0; i < s.length(); i++) {
    char c = s[i];
    if (c >= 32 && c < 127) out += c;
  }
  return out;
}

void drawWrapped(String text, int y, int maxRows) {
  const int cols = 21;
  int row = 0;
  while (text.length() > 0 && row < maxRows) {
    String chunk;
    if ((int)text.length() <= cols) {
      chunk = text; text = "";
    } else {
      int brk = text.lastIndexOf(' ', cols);
      if (brk <= 0) brk = cols;
      chunk = text.substring(0, brk);
      text = text.substring(brk);
      text.trim();
    }
    display.setCursor(0, y + row * 10);
    display.print(chunk);
    row++;
  }
}

void showMessage(String a, String b) {
  display.clearDisplay();
  display.setTextSize(1);
  display.setCursor(0, 20); display.println(a);
  display.setCursor(0, 34); display.println(b);
  display.display();
  lastLine = ""; lastTitle = "";
}
