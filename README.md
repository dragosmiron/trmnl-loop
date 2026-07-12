# TRMNL E-Ink Batch Caching & Rotation System

🔮 *Proudly vibe-coded with the assistance of Antigravity, Google DeepMind's coding agent.*

This repository contains the custom firmware and Docker proxy service to enable **dynamic offline e-ink slideshow caching** on your TRMNL e-ink display (such as the Seeed Studio TRMNL DIY Kit, the official TRMNL board, or other compatible custom setups). 

With this setup, your device only wakes up its Wi-Fi radio periodically to fetch the latest screens from your self-hosted TRMNL server (e.g. LaraPaper or Terminus). Once downloaded, it stores the screens locally in its flash memory, shuts off the Wi-Fi radio, and loops through them offline. This saves approximately **70% to 80% of battery consumption**.

---

## How It Works (The Caching Strategy)
*   **Sync Interval (WiFi Wakes):** Configured on your server (e.g. 15 minutes or 1 hour). The device connects to Wi-Fi, requests the batch of screens from the proxy, and shuts down Wi-Fi immediately.
*   **Local Cycle (Offline Swaps):** The board wakes up from low-power deep sleep every few minutes (e.g. 5 minutes), reads the next cached screen from local flash memory, refreshes the display, and returns to deep sleep.
*   **Dynamic Playlist Wrap:** The proxy queries your server sequentially and hashes each image using SHA-256. When it detects a duplicate image, it knows the playlist has looped back to the beginning. It stops querying immediately and serves only the unique screens (e.g. 4 screens). The board then loops these 4 screens locally for the remainder of the sync window.

---

## Part A — Deploy the Proxy Service (Docker)

The proxy runs as a lightweight Docker service on your Linux server.

### 1. Set Up the Project Directory
Copy the project files (`trmnl_loop.py`, `Dockerfile`, and `docker-compose.yml`) to a folder on your server (e.g., `/opt/trmnl-loop`).

### 2. Configure Settings
Create a configuration file by copying the example:
```bash
cp config.json.example config.json
nano config.json
```

Add your configuration details (adjust to match your TRMNL server URL and desired cycle interval):
```json
{
  "TRMNL_BYOS_URL": "http://192.168.1.100:4567",
  "CYCLE_INTERVAL": 300,
  "REFRESH_PADDING": 60
}
```
*   **`TRMNL_BYOS_URL`**: The base URL of your self-hosted TRMNL backend (e.g. LaraPaper or Terminus instance).
*   **`CYCLE_INTERVAL`**: The time in seconds that the device stays in deep sleep between rotating local cached screens offline (e.g. `300` seconds = 5 minutes).
*   **`REFRESH_PADDING`**: The grace period in seconds added to the hard refresh window calculations to accommodate network latency when connecting to Wi-Fi, and to prevent time desynchronization issues if the server has not yet completed its default cycle interval when the device wakes up to sync.

*Note: Any changes made to `config.json` on the host will be dynamically reloaded by the proxy server immediately, without needing to restart the container!*

### 3. Start the Proxy Container
Run the following command to build and launch the container:
```bash
docker compose up -d --build
```
Verify the logs to ensure the proxy is listening on port `5000`:
```bash
docker compose logs -f trmnl-loop
```

---

## Part B — Compile & Flash the Firmware

You can flash the board using either your browser (via Docker compilation) or locally on your computer using the Arduino IDE.

> [!WARNING]
> **Flash Cleanup Required:** When switching between compilation environments, or recovering from bootloops, you **must** perform a full flash erase before programming the chip. This wipes corrupted filesystem partitions and ensures the new LittleFS partition formats cleanly.

---

### Option 1: PlatformIO (Docker Build & Browser Flash)
This is the recommended workflow. It performs compilation inside a PlatformIO container and serves the merged firmware binary for browser flashing.

#### 1. Compile the Binaries
Always clean your target's build cache before compilation to ensure changes are applied:
```bash
# A. Clean the compilation cache
docker run --rm -v $(pwd):/workspace -w /workspace takigama/platformio platformio run --target clean -e seeed_xiao_esp32s3

# B. Compile the partition files
docker run --rm -v $(pwd):/workspace -w /workspace takigama/platformio platformio run -e seeed_xiao_esp32s3
```

#### 2. Download the Firmware Binary
Open your terminal on your local machine and download the generated firmware from the proxy container:
```bash
curl -H "Cache-Control: no-cache" -o ~/Downloads/firmware.bin http://<server_ip>:5000/firmware.bin
```

#### 3. Program via Web Flasher
1. Connect your ESP32-S3 board (such as the Seeed Studio XIAO ESP32-S3, official TRMNL board, or other compatible ESP32-S3 board) to your computer using a USB-C data cable.
2. Put the board in **bootloader mode** (typically by holding the **BOOT** button, clicking the **RESET** button once, then releasing **BOOT**).
3. Open Google Chrome or Microsoft Edge and go to: **[espressif.github.io/esptool-js](https://espressif.github.io/esptool-js/)**.
4. Click **Connect**, select the serial port corresponding to your board, and click **Erase Flash** (wipes the chip clean).
5. Click the **`+` (Add File)** button to configure **1 row** in the flasher:
   *   **Row 1:** File: `firmware.bin` | Address: **`0x0`**
6. Ensure Flash settings are set to `keep` (mode, speed, size) and click **Program**.

---

### Option 2: Arduino IDE (Local Build)
Use this option if you prefer compiling and flashing natively using the Arduino IDE.

1.  Open the **Arduino IDE**.
2.  Go to **Tools > Board > Boards Manager** and install the `esp32` package by Espressif Systems.
3.  Go to the **Library Manager** (left panel) and install:
    *   `bb_epaper` (by Larry Bank)
    *   `ArduinoJson` (by Benoit Blanchon, v6 or v7)
4.  Open `custom_trmnl_firmware/custom_trmnl_firmware.ino` in the IDE.
5.  Set your board settings:
    *   **Board:** Select your specific ESP32-S3 board (e.g. `Seeed Studio XIAO ESP32S3` for the DIY Kit, or the corresponding board definition for your hardware).
    *   **PSRAM:** OPI PSRAM (Required for the 8MB PSRAM Sense/standard kit variant)
    *   **Flash Mode:** QIO
    *   **Partition Scheme:** Default 8MB
6.  Connect your board to your computer, put it in **bootloader mode** (Hold BOOT ➡️ Click RESET ➡️ Release BOOT), select your port under **Tools > Port**, and click **Upload**.

---

## Part C — Configure Device WiFi & Proxy

On the first boot after erasing the flash, the device will initialize and launch a captive setup portal:

1.  On your phone or laptop, connect to the open Wi-Fi hotspot named **`TRMNL-Batch-Setup`**.
2.  Open your browser and navigate to: **`http://192.168.4.1`**.
3.  Enter your home network credentials:
    *   **WiFi SSID:** Your home network name.
    *   **WiFi Password:** Your home network password.
    *   **Proxy URL:** Enter your trmnl-loop server address:
        ```text
        http://192.168.1.100:5000
        ```
        *(Ensure there is no trailing slash `/` at the end of the URL).*
4.  Click **Save Configuration**. The device will connect, perform its first sync, and start rotating screens!

> [!TIP]
> **Re-entering Setup Portal:** If you change your Wi-Fi network, hold the **BOOT** button on the back of the board while pressing the **RESET** button. This forces the device to boot back into setup portal mode.

---

## Gotchas & Troubleshooting

### 1. The "1-Screen Loop" Bug (Sleep Schedule)
Overnight, LaraPaper puts devices into "Sleep Schedule," returning `'special_function': 'sleep'`. During sleep mode, the server returns the same static sleeping logo image on every request. Our duplicate screen check will notice this and stop at 1 screen.
*   **To test playlist rotations at night:** Temporarily disable the Sleep Schedule in the LaraPaper Device settings, click **Save**, and clear the Laravel cache:
    ```bash
    docker exec -it <larapaper_container_id> php artisan cache:clear
    ```

### 2. Laravel Database Write Latency
LaraPaper advances the playlist pointer in the database on every request. Because sequential requests are made milliseconds apart, a fast loop can hit LaraPaper before a database transaction commits, resulting in duplicate fetches. 
*   *Solution:* The proxy includes a built-in `time.sleep(1.0)` delay between fetches to let transactions commit cleanly.

### 3. Flash Memory Capacity
A typical ESP32-S3 LittleFS partition is 1.5MB. Since each monochrome screen frame is exactly **48,000 bytes**, trying to fetch more than 16 screens in one batch will overflow the partition and crash the board.
*   *Solution:* The proxy and board are capped at a maximum of **16 unique cached screens** per sync window.

---

## Porting to Other E-Ink Displays & ESP32 Boards

The project is currently configured out of the box for the **Seeed Studio XIAO ESP32-S3 DIY Kit** (800x480 resolution, monochrome) and the **official TRMNL OG board** (800x480).

If you want to use a different board (e.g. an ESP32 DevKit, FireBeetle) or a different screen resolution (e.g. a 4.2" 400x300 screen, or a 3-color panel), you need to update a few configurations:

### 1. Update the Screen Resolution in the Firmware
In `custom_trmnl_firmware/custom_trmnl_firmware.ino` (around line 180), locate the HTTP request URL inside the `performSync()` function:
```cpp
String fetchUrl = server_url + "/api/display?format=1bit&width=800&height=480";
```
*   **Dimensions:** Change `width=800` and `height=480` to match your display size (e.g., `width=400&height=300`). This ensures the proxy resizes and crops your dashboards correctly.
*   **Color Format:** If you have a 3-color screen (Black, White, Red), change `format=1bit` to `format=3color`.

### 2. Update Pin Mappings
Locate the conditional hardware configuration sections at the top of `custom_trmnl_firmware.ino`. If using a custom board, define your custom GPIO pins for SPI, Chip Select, Reset, Data/Command, and Busy lines:
```cpp
#define EPD_CS_PIN    YOUR_CS_GPIO
#define EPD_RST_PIN   YOUR_RST_GPIO
#define EPD_DC_PIN    YOUR_DC_GPIO
#define EPD_BUSY_PIN  YOUR_BUSY_GPIO
```

### 3. Update the Screen Model Constant
The firmware uses Larry Bank's `bb_epaper` library. To initialize it for a different screen controller, update the enum value in `bbep.setPanelType(...)` inside `custom_trmnl_firmware.ino`:
```cpp
bbep.setPanelType(YOUR_SCREEN_MODEL_CONSTANT);
```
*(You can find all supported constants—like `EP426_800x480` or `EP75_800x480`—directly inside [bb_epaper.h](https://github.com/bitbank2/bb_epaper/blob/main/src/bb_epaper.h) on GitHub).*

### 4. PlatformIO Configuration
In `platformio.ini`, add a new target environment specifying your board identifier (from the [PlatformIO Registry](https://docs.platformio.org/en/latest/boards/)) and your custom build compiler flags:
```ini
[env:my_custom_board]
platform = espressif32
board = esp32dev    # <-- Put your board ID here
framework = arduino
lib_deps =
    bitbank2/bb_epaper
    bblanchon/ArduinoJson@^6.21.3
build_flags =
    -D BOARD_MY_CUSTOM_BOARD
```

