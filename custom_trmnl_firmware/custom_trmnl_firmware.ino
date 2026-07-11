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
#else
// Default: Screen pins matching the Seeed Studio TRMNL 7.5" OG DIY Kit
#define EPD_SCK_PIN  7
#define EPD_MOSI_PIN 9
#define EPD_CS_PIN   44
#define EPD_RST_PIN  38
#define EPD_DC_PIN   10
#define EPD_BUSY_PIN 4
#endif

// Wakeup boot button (GPIO 0 is the BOOT button on the XIAO ESP32-S3)
#define BOOT_BUTTON_PIN 0

// Screen dimensions
#define SCREEN_WIDTH 800
#define SCREEN_HEIGHT 480
#define SCREEN_BUFFER_SIZE (SCREEN_WIDTH * SCREEN_HEIGHT / 8) // Exactly 48,000 bytes

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
void drawScreen(int index);
void startCaptivePortal();
void handleRoot();
void handleSave();
void handleNotFound();
String getMacAddress();

void setup() {
  Serial.begin(115200);
  pinMode(BOOT_BUTTON_PIN, INPUT_PULLUP);
  
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

  // Calculate sync condition
  // Sync if we haven't synced yet, or if we have rotated through all downloaded screens
  bool needSync = (total_screens == 0 || wake_counter >= total_screens);

  if (needSync) {
    Serial.println("Sync Interval Reached. Booting Wi-Fi...");
    if (connectWiFi()) {
      if (fetchBatchFromProxy()) {
        Serial.println("Batch sync successful!");
        wake_counter = 0;
        current_screen_idx = 0;
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
    Serial.printf("Drawing screen index: %d / %d\n", current_screen_idx, total_screens);
    drawScreen(current_screen_idx);
    
    // Increment rotation steps
    current_screen_idx = (current_screen_idx + 1) % total_screens;
    wake_counter++;
  } else {
    Serial.println("No screens cached to display!");
  }

  // Go back to Deep Sleep for the cycle interval
  Serial.printf("Going to sleep for %d seconds...\n", cycle_interval);
  esp_sleep_enable_timer_wakeup(cycle_interval * 1000000ULL);
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
  while (WiFi.status() != WL_CONNECTED && retry < 25) {
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
  http.setTimeout(30000); // 30 seconds timeout for batch processing
  
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
      total_screens = min(16, batchCount);
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
        sprintf(path, "/s%d.raw", i);
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

void drawScreen(int index) {
  char path[16];
  sprintf(path, "/s%d.raw", index);
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

  // Full refresh to clear ghosts if maximum compatibility is enabled OR if it is the first screen
  if (maximum_compatibility == 1 || index == 0) {
    bbep.refresh(REFRESH_FULL);
  } else {
    bbep.refresh(REFRESH_PARTIAL);
  }

  free(buffer);
}

void startCaptivePortal() {
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
  String html = "<html><head><meta name='viewport' content='width=device-width, initial-scale=1'><style>";
  html += "body { font-family: sans-serif; padding: 20px; }";
  html += "input, button { display: block; width: 100%; margin: 10px 0; padding: 12px; font-size: 16px; box-sizing: border-box; }";
  html += "button { background: #000; color: #fff; border: none; font-weight: bold; }";
  html += "</style></head><body>";
  html += "<h2>TRMNL Batch Sync Setup</h2>";
  html += "<form action='/save' method='post'>";
  html += "<input type='text' name='ssid' placeholder='WiFi SSID' value='" + wifi_ssid + "' required>";
  html += "<input type='password' name='pass' placeholder='WiFi Password' value='" + wifi_pass + "'>";
  html += "<input type='url' name='server' placeholder='Proxy URL (e.g. http://192.168.1.100:5000)' value='" + server_url + "' required>";
  html += "<button type='submit'>Save Configuration</button>";
  html += "</form></body></html>";
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

  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid.c_str(), pass.c_str());
  
  int retry = 0;
  while (WiFi.status() != WL_CONNECTED && retry < 20) {
    delay(500);
    retry++;
  }

  if (WiFi.status() == WL_CONNECTED) {
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
  sprintf(macStr, "%02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  return String(macStr);
}
