#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <LittleFS.h>
#include <Preferences.h>
#include <DNSServer.h>
#include <WebServer.h>
#include <ArduinoJson.h> // Make sure ArduinoJson library is installed in Arduino IDE
#include "bb_epaper.h"

#if defined(BOARD_TRMNL_OG)
// Screen pins for the official TRMNL OG Board
#define EPD_SCK_PIN  7
#define EPD_MOSI_PIN 8
#define EPD_CS_PIN   6
#define EPD_RST_PIN  10
#define EPD_DC_PIN   5
#define EPD_BUSY_PIN 4
#define BOOT_BUTTON_PIN 0 // Modify if official TRMNL uses a different pin

#elif defined(BOARD_SEEED_XIAO_ESP32S3)
// Screen pins matching the Seeed Studio TRMNL 7.5" OG DIY Kit
#define EPD_SCK_PIN  7
#define EPD_MOSI_PIN 9
#define EPD_CS_PIN   44
#define EPD_RST_PIN  38
#define EPD_DC_PIN   10
#define EPD_BUSY_PIN 4
#define BOOT_BUTTON_PIN 0     // Onboard BOOT button on the XIAO module
#define EXPANSION_KEY1_PIN 2  // KEY1 on Seeed E-Paper expansion board (D1) - Previous Screen
#define EXPANSION_KEY3_PIN 5  // KEY3 on Seeed E-Paper expansion board (D4) - Next Screen

#else
// Default: Custom Board (Replace with your own pins)
// These default to the standard TRMNL OG pins but can be overridden
#define EPD_SCK_PIN  7
#define EPD_MOSI_PIN 8
#define EPD_CS_PIN   6
#define EPD_RST_PIN  10
#define EPD_DC_PIN   5
#define EPD_BUSY_PIN 4
#define BOOT_BUTTON_PIN 0
#endif

// Screen dimensions
#define SCREEN_WIDTH 800
#define SCREEN_HEIGHT 480
#define SCREEN_BUFFER_SIZE (SCREEN_WIDTH * SCREEN_HEIGHT / 8) // Exactly 48,000 bytes

// System Constants
#define MAX_SCREENS 16
#define WIFI_RETRY_LIMIT 25
#define HTTP_TIMEOUT_MS 30000

// E-paper class instance
BBEPAPER bbep(EP75_800x480);

// RTC Variables (Survive deep sleep)
RTC_DATA_ATTR int wake_counter = 0;       // Current step in the rotation cycle
RTC_DATA_ATTR int current_screen_idx = 0; // Index of the screen to draw next
RTC_DATA_ATTR int total_screens = 0;      // Number of screens downloaded in last sync
RTC_DATA_ATTR int frame_size = 48000;     // Size of each screen frame in bytes (1bit=48k, 3color=96k)

// Setup configurations stored in NVS Preferences
Preferences prefs;
String wifi_ssid = "";
String wifi_pass = "";
String server_url = "";
String api_token = "";
int cycle_interval = 300; // Default: cycle every 5 minutes
int hard_refresh = 1800;  // Default: sync every 30 minutes
int maximum_compatibility = 0; // 1 = force full refresh every screen change

// Web server for the Captive Portal setup
WebServer server(80);
DNSServer dnsServer;

// Function declarations
void loadSettings();
void saveSettings(String ssid, String pass, String server, String token);
bool connectWiFi();
bool fetchBatchFromProxy();
void drawScreen(int index, bool is_reset, bool is_sync, bool is_manual);
void startCaptivePortal();
void handleRoot();
void handleSave();
void handleNotFound();
String getMacAddress();

void setup() {
  Serial.begin(115200);
  pinMode(BOOT_BUTTON_PIN, INPUT_PULLUP);
#if defined(EXPANSION_KEY1_PIN)
  pinMode(EXPANSION_KEY1_PIN, INPUT_PULLUP);
#endif
#if defined(EXPANSION_KEY3_PIN)
  pinMode(EXPANSION_KEY3_PIN, INPUT_PULLUP);
#endif
  
  // Initialize file system
  if (!LittleFS.begin(true)) {
    Serial.println("LittleFS Mount Failed");
    return;
  }

  loadSettings();

  // Trigger setup portal if button is pressed or if no WiFi details are saved
  bool forcePortal = (digitalRead(BOOT_BUTTON_PIN) == LOW);
  if (forcePortal || wifi_ssid.length() == 0) {
    Serial.println("Entering Setup Portal mode...");
    startCaptivePortal();
    return;
  }

  // Check wakeup cause
  esp_sleep_wakeup_cause_t wakeup_reason = esp_sleep_get_wakeup_cause();
  bool is_reset = (wakeup_reason == ESP_SLEEP_WAKEUP_UNDEFINED);
  bool is_sync = false;
  bool is_manual = false;

  // Detect if manual cycle buttons woke the device
  if (wakeup_reason == ESP_SLEEP_WAKEUP_EXT1 && total_screens > 0) {
    uint64_t pin_mask = esp_sleep_get_ext1_wakeup_status();
    bool prev_pressed = false;
    bool next_pressed = false;

#if defined(EXPANSION_KEY1_PIN)
    prev_pressed = (pin_mask & (1ULL << EXPANSION_KEY1_PIN)) || (digitalRead(EXPANSION_KEY1_PIN) == LOW);
#endif
#if defined(EXPANSION_KEY3_PIN)
    next_pressed = (pin_mask & (1ULL << EXPANSION_KEY3_PIN)) || (digitalRead(EXPANSION_KEY3_PIN) == LOW);
#endif

    if (prev_pressed) {
      Serial.println("Wakeup caused by KEY1 (Previous Screen)");
      // Move pointer back: (current_screen_idx - 2) because it was already pointing to the next scheduled screen
      current_screen_idx = (current_screen_idx - 2 + total_screens) % total_screens;
      is_manual = true;
    } else if (next_pressed) {
      Serial.println("Wakeup caused by KEY3 (Next Screen)");
      // Already pointing to next scheduled screen, no change needed
      is_manual = true;
    }
  }

  // Calculate sync condition
  // Sync if we haven't synced yet, or if the time passed exceeds the hard refresh interval.
  // Bypass sync if we woke up manually to cycle screens.
  bool needSync = false;
  if (total_screens == 0) {
    needSync = true;
  } else if (!is_manual) {
    int time_passed = wake_counter * cycle_interval;
    if (time_passed >= hard_refresh) {
      needSync = true;
    }
  }

  if (needSync) {
    Serial.println("Sync Interval Reached. Booting Wi-Fi...");
    if (connectWiFi()) {
      if (fetchBatchFromProxy()) {
        Serial.println("Batch sync successful!");
        wake_counter = 0;
        current_screen_idx = 0;
        is_sync = true;
      } else {
        Serial.println("Failed to fetch screens. Falling back to local rotation.");
      }
      // Shut down Wi-Fi immediately to save power
      WiFi.disconnect(true);
      WiFi.mode(WIFI_OFF);
    } else {
      Serial.println("Could not connect to Wi-Fi. Falling back to local rotation.");
    }
  }

  // Draw the current screen from local LittleFS storage
  if (total_screens > 0) {
    Serial.printf("Drawing screen index: %d / %d (Manual: %d)\n", current_screen_idx, total_screens, is_manual);
    drawScreen(current_screen_idx, is_reset, is_sync, is_manual);
    
    // Increment rotation steps
    current_screen_idx = (current_screen_idx + 1) % total_screens;
    if (!is_manual) {
      wake_counter++;
    }
  } else {
    Serial.println("No screens cached to display!");
  }

  // Go back to Deep Sleep for the cycle interval
  Serial.printf("Going to sleep for %d seconds...\n", cycle_interval);
  esp_sleep_enable_timer_wakeup(cycle_interval * 1000000ULL);

  // Enable waking up from key presses during sleep
#if defined(EXPANSION_KEY1_PIN) && defined(EXPANSION_KEY3_PIN)
  uint64_t ext1_mask = (1ULL << EXPANSION_KEY1_PIN) | (1ULL << EXPANSION_KEY3_PIN);
  esp_sleep_enable_ext1_wakeup(ext1_mask, ESP_EXT1_WAKEUP_ANY_LOW);
#endif

  esp_deep_sleep_start();
}

void loop() {
  // Only called in Captive Portal mode
  dnsServer.processNextRequest();
  server.handleClient();
  delay(10);
}

void loadSettings() {
  prefs.begin("trmnl-batch", true);
  wifi_ssid = prefs.getString("ssid", "");
  wifi_pass = prefs.getString("pass", "");
  server_url = prefs.getString("server", "");
  api_token = prefs.getString("token", "");
  cycle_interval = prefs.getInt("cycle", 300);
  hard_refresh = prefs.getInt("refresh", 1800);
  maximum_compatibility = prefs.getInt("compat", 0);
  frame_size = prefs.getInt("frame_size", 48000);
  prefs.end();
}

void saveSettings(String ssid, String pass, String server, String token) {
  prefs.begin("trmnl-batch", false);
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  prefs.putString("server", server);
  prefs.putString("token", token);
  prefs.end();
}

bool connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(wifi_ssid.c_str(), wifi_pass.c_str());
  
  int retry = 0;
  while (WiFi.status() != WL_CONNECTED && retry < WIFI_RETRY_LIMIT) {
    delay(500);
    Serial.print(".");
    retry++;
  }
  Serial.println("");
  return (WiFi.status() == WL_CONNECTED);
}

bool fetchBatchFromProxy() {
  HTTPClient http;
  String fetchUrl = server_url + "/api/display";
  http.begin(fetchUrl);
  http.setTimeout(HTTP_TIMEOUT_MS); // Timeout for batch processing
  
  // Copy native headers to authenticate with the proxy
  http.addHeader("Access-Token", api_token);
  http.addHeader("ID", getMacAddress());
  http.addHeader("User-Agent", "ESP32HTTPClient");

  // Collect response headers
  const char* headerKeys[] = {"X-Batch-Count", "X-Cycle-Interval", "X-Hard-Refresh", "X-Max-Compatibility", "X-Frame-Size", "X-Special-Function"};
  http.collectHeaders(headerKeys, 6);

  int httpCode = http.GET();
  if (httpCode == HTTP_CODE_OK) {
    int batchCount = http.header("X-Batch-Count").toInt();
    int newCycle = http.header("X-Cycle-Interval").toInt();
    int newRefresh = http.header("X-Hard-Refresh").toInt();
    int newCompat = http.header("X-Max-Compatibility").toInt();
    int newFrameSize = http.header("X-Frame-Size").toInt();
    String specialFunc = http.header("X-Special-Function");

    if (batchCount > 0) {
      total_screens = min(MAX_SCREENS, batchCount);
      cycle_interval = newCycle;
      hard_refresh = newRefresh;
      maximum_compatibility = newCompat;
      if (newFrameSize > 0) {
        frame_size = newFrameSize;
      }

      // If the server commands sleep mode, disable cycling and sleep for the full refresh interval
      if (specialFunc == "sleep") {
        cycle_interval = hard_refresh;
      }

      // Update stored intervals
      prefs.begin("trmnl-batch", false);
      prefs.putInt("cycle", cycle_interval);
      prefs.putInt("refresh", hard_refresh);
      prefs.putInt("compat", maximum_compatibility);
      prefs.putInt("frame_size", frame_size);
      prefs.end();

      WiFiClient* stream = http.getStreamPtr();
      uint8_t buffer[1024];

      // Stream the screens and write them to separate files in LittleFS
      for (int i = 0; i < total_screens; i++) {
        char path[16];
        snprintf(path, sizeof(path), "/s%d.raw", i);
        File file = LittleFS.open(path, "w");
        
        if (!file) {
          Serial.printf("Failed to write to file: %s\n", path);
          http.end();
          return false;
        }

        int written = 0;
        while (written < frame_size) {
          int toRead = min(sizeof(buffer), (size_t)(frame_size - written));
          int bytesRead = stream->readBytes(buffer, toRead);
          if (bytesRead <= 0) break;
          file.write(buffer, bytesRead);
          written += bytesRead;
        }
        file.close();
        Serial.printf("Saved file: %s (%d bytes)\n", path, written);
      }
      http.end();
      return true;
    }
  }
  
  Serial.printf("HTTP request failed, code: %d\n", httpCode);
  http.end();
  return false;
}

void drawScreen(int index, bool is_reset, bool is_sync, bool is_manual) {
  char path[16];
  snprintf(path, sizeof(path), "/s%d.raw", index);
  File file = LittleFS.open(path, "r");
  if (!file) {
    Serial.printf("No screen file at: %s\n", path);
    return;
  }

  uint8_t* buffer = (uint8_t*)malloc(frame_size);
  if (!buffer) {
    Serial.println("RAM allocation failed");
    file.close();
    return;
  }

  file.read(buffer, frame_size);
  file.close();

  // Initialize and write to display (setPanelType must run BEFORE initIO)
  bbep.setPanelType(EP75_800x480);
  bbep.initIO(EPD_DC_PIN, EPD_RST_PIN, EPD_BUSY_PIN, EPD_CS_PIN, EPD_MOSI_PIN, EPD_SCK_PIN, 8000000);
  bbep.setBuffer(buffer);
  
  // Set to true to invert colors if screen prints negative
  bbep.writePlane(PLANE_BOTH, false); 

  // Choose refresh mode based on sync, boot, cycle, and manual states
  if (maximum_compatibility == 1 || is_reset) {
    bbep.refresh(REFRESH_FULL);
  } else {
    bbep.refresh(REFRESH_FAST);
  }

  free(buffer);
}

void startCaptivePortal() {
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP("TRMNL-Batch-Setup");
  dnsServer.start(53, "*", WiFi.softAPIP());
  
  server.on("/", HTTP_GET, handleRoot);
  server.on("/save", HTTP_POST, handleSave);
  server.onNotFound(handleNotFound);
  server.begin();
  
  Serial.print("Connect to WiFi hotspot 'TRMNL-Batch-Setup' and open http://");
  Serial.println(WiFi.softAPIP());
}

void handleRoot() {
  int n = WiFi.scanNetworks();
  
  String html = R"rawliteral(
<!DOCTYPE html>
<html>
<head>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <style>
    body { font-family: sans-serif; padding: 20px; background: #f9f9f9; color: #333; }
    .card { max-width: 400px; margin: auto; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
    h2 { margin-top: 0; text-align: center; }
    input, button { display: block; width: 100%; margin: 15px 0; padding: 12px; font-size: 16px; box-sizing: border-box; border: 1px solid #ccc; border-radius: 4px; }
    button { background: #000; color: #fff; border: none; font-weight: bold; cursor: pointer; }
    button:hover { background: #333; }
  </style>
</head>
<body>
  <div class='card'>
    <h2>TRMNL Device Setup</h2>
    <form action='/save' method='post'>
      <label><b>WiFi Network</b></label>
      <input type='text' name='ssid' list='networks' placeholder='Select or type SSID' value=')rawliteral";
  
  html += wifi_ssid;
  html += R"rawliteral(' required>
      <datalist id='networks'>
)rawliteral";

  for (int i = 0; i < n; ++i) {
    html += "<option value='" + WiFi.SSID(i) + "'>";
  }

  html += R"rawliteral(
      </datalist>
      <label><b>WiFi Password</b></label>
      <input type='password' name='pass' placeholder='Password (leave blank if open)' value=')rawliteral";
  html += wifi_pass;
  html += R"rawliteral('>
      <label><b>Proxy Server URL</b></label>
      <input type='url' name='server' placeholder='e.g. http://192.168.1.100:5000' value=')rawliteral";
  html += server_url;
  html += R"rawliteral(' required>
      <button type='submit'>Save & Connect</button>
    </form>
  </div>
</body>
</html>
)rawliteral";

  server.send(200, "text/html", html);
}

void handleSave() {
  String ssid = server.arg("ssid");
  String pass = server.arg("pass");
  String serverUrl = server.arg("server");
  
  if (serverUrl.endsWith("/")) {
    serverUrl.remove(serverUrl.length() - 1);
  }

  // Attempt to connect to WiFi and register with the server
  wifi_ssid = ssid;
  wifi_pass = pass;
  
  server.send(200, "text/html", "<html><body><h3>Saving config & connecting...</h3><p>Checking registration. Keep an eye on Serial output.</p></body></html>");
  delay(1000);

  if (connectWiFi()) {
    // WiFi connected successfully, now fetch Access-Token from the server
    HTTPClient http;
    String setupUrl = serverUrl + "/api/setup";
    http.begin(setupUrl);
    http.addHeader("ID", getMacAddress());
    http.addHeader("User-Agent", "ESP32HTTPClient");

    int code = http.GET();
    if (code == HTTP_CODE_OK) {
      String payload = http.getString();
      DynamicJsonDocument doc(1024);
      deserializeJson(doc, payload);
      String token = doc["api_key"].as<String>();

      if (token.length() > 0) {
        saveSettings(ssid, pass, serverUrl, token);
        Serial.println("Setup registered successfully! Resetting device...");
        delay(1000);
        ESP.restart();
      }
    }
    http.end();
  }

  // If connection or registration fails, restart back into portal mode
  Serial.println("Connection or Registration failed. Restarting...");
  delay(2000);
  ESP.restart();
}

void handleNotFound() {
  server.sendHeader("Location", "/", true);
  server.send(302, "text/plain", "");
}

String getMacAddress() {
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char macStr[18];
  snprintf(macStr, sizeof(macStr), "%02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  return String(macStr);
}
