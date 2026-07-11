import os
import io
import requests
from flask import Flask, request, Response, jsonify, send_file
from PIL import Image

app = Flask(__name__)

# Server Configurations from environment variables
TRMNL_BYOS_URL = os.getenv("TRMNL_BYOS_URL")
if not TRMNL_BYOS_URL:
    raise RuntimeError("TRMNL_BYOS_URL environment variable is required but not set.")
CYCLE_INTERVAL = int(os.getenv("CYCLE_INTERVAL", 300))  # Offline cycle rate in seconds (default 5m)
REFRESH_PADDING = int(os.getenv("REFRESH_PADDING", 60))  # Buffer in seconds to prevent sync race conditions

@app.route('/api/setup', methods=['GET', 'POST'])
def setup_proxy():
    """
    Transparently forward setup/registration requests to the TRMNL BYOS server.
    This ensures native auto-join and setup flows still work perfectly.
    """
    headers = {key: value for key, value in request.headers if key.lower() not in ['host']}
    url = f"{TRMNL_BYOS_URL}/api/setup"
    
    try:
        if request.method == 'POST':
            resp = requests.post(url, headers=headers, json=request.json, timeout=10)
        else:
            resp = requests.get(url, headers=headers, params=request.args, timeout=10)
        return Response(resp.content, status=resp.status_code, headers=dict(resp.headers))
    except Exception as e:
        return jsonify({"error": f"Proxy setup request failed: {str(e)}"}), 500

@app.route('/api/display', methods=['GET'])
def display_batch():
    """
    Main endpoint for batch downloading screens.
    Performs multiple sequential queries to the BYOS server to fetch next screens in playlist,
    converts them to 1-bit raw bytes, and serves them to the device.
    """
    # Forward original headers (Access-Token, MAC Address, RSSI, Battery etc.)
    headers = {key: value for key, value in request.headers if key.lower() not in ['host']}
    url = f"{TRMNL_BYOS_URL}/api/display"
    
    # 1. Query BYOS server for the first screen to read metadata
    try:
        first_resp = requests.get(f"{url}?step=0", headers=headers, timeout=10)
        if first_resp.status_code != 200:
            return Response(first_resp.content, status=first_resp.status_code, headers=dict(first_resp.headers))
        
        first_json = first_resp.json()
        hard_refresh = int(first_json.get("refresh_rate", 1800))  # Default to 30 mins
        maximum_compatibility = first_json.get("maximum_compatibility", False)
    except Exception as e:
        return jsonify({"error": f"Failed to connect to BYOS backend: {str(e)}"}), 500

    # Calculate required number of screens and cap it to prevent filesystem overflow
    MAX_BATCH_LIMIT = 16
    num_screens = min(MAX_BATCH_LIMIT, max(1, hard_refresh // CYCLE_INTERVAL))
    print(f"[Proxy] Hard Refresh: {hard_refresh}s | Local Cycle: {CYCLE_INTERVAL}s | Fetching up to {num_screens} screens.")

    raw_screens = []
    seen_hashes = set()
    import hashlib
    
    # Read requested format and dimensions from query parameters
    img_format = request.args.get("format", "1bit").lower()
    width = int(request.args.get("width", 800))
    height = int(request.args.get("height", 480))
    
    # 2. Download and convert the first screen image
    try:
        first_image_url = first_json.get("image_url")
        print(f"[Proxy] Screen 0 JSON: {first_json}")
        if first_image_url:
            first_raw = download_and_convert_image(first_image_url, img_format, width, height)
            first_hash = hashlib.sha256(first_raw).hexdigest()
            print(f"[Proxy] Screen 0 hash: {first_hash} | URL: {first_image_url}")
            seen_hashes.add(first_hash)
            raw_screens.append(first_raw)
    except Exception as e:
        print(f"[Error] Failed to process first screen: {e}")

    # 3. Sequentially query remaining screens in the playlist
    for i in range(1, num_screens):
        import time
        time.sleep(1.0)  # Rate-limit requests slightly to let LaraPaper database transactions commit
        try:
            resp = requests.get(f"{url}?step={i}", headers=headers, timeout=10)
            if resp.status_code == 200:
                js = resp.json()
                print(f"[Proxy] Screen {i} JSON: {js}")
                img_url = js.get("image_url")
                if img_url:
                    raw_data = download_and_convert_image(img_url, img_format, width, height)
                    img_hash = hashlib.sha256(raw_data).hexdigest()
                    print(f"[Proxy] Screen {i} hash: {img_hash} | URL: {img_url}")
                    if img_hash in seen_hashes:
                        print(f"[Proxy] Duplicate screen detected (playlist looped). Stopping fetch at {len(raw_screens)} unique screens.")
                        break
                    seen_hashes.add(img_hash)
                    raw_screens.append(raw_data)
            else:
                print(f"[Warning] BYOS server returned status {resp.status_code} for screen {i}")
        except Exception as e:
            print(f"[Error] Failed to process screen {i}: {e}")

    # Ensure we got at least one valid screen
    if not raw_screens:
        return jsonify({"error": "Failed to fetch any screens from BYOS server"}), 500

    # Concatenate all raw image frames
    combined_binary = b"".join(raw_screens)

    # Calculate frame size based on format
    frame_size = len(raw_screens[0])

    # 4. Return binary stream along with control headers for the ESP32
    response = Response(combined_binary, mimetype="application/octet-stream")
    response.headers['X-Batch-Count'] = str(len(raw_screens))
    response.headers['X-Cycle-Interval'] = str(CYCLE_INTERVAL)
    response.headers['X-Hard-Refresh'] = str(hard_refresh + REFRESH_PADDING)
    response.headers['X-Max-Compatibility'] = "1" if maximum_compatibility else "0"
    response.headers['X-Frame-Size'] = str(frame_size) # Tell device exactly how many bytes per screen
    response.headers['X-Special-Function'] = first_json.get("special_function", "none")
    
    print(f"[Proxy] Dispatched {len(raw_screens)} screens ({len(combined_binary)} bytes) in '{img_format}' ({width}x{height}) format (Frame Size: {frame_size} bytes).")
    return response

def download_and_convert_image(url, img_format="1bit", width=800, height=480):
    """
    Downloads PNG/BMP from URL and converts it to the requested raw pixel array format and size.
    * '1bit': Standard B/W monochrome (width * height / 8 bytes)
    * '3color': Red/Black/White panel (two planes = width * height / 4 bytes)
    * '4gray': 4-level grayscale (2 bits per pixel = width * height / 4 bytes)
    """
    resp = requests.get(url, timeout=10)
    if resp.status_code != 200:
        raise Exception(f"HTTP {resp.status_code} when downloading image")
        
    img = Image.open(io.BytesIO(resp.content))
    if img.size != (width, height):
        print(f"[Warning] Resizing image from {img.size} to ({width}, {height})")
        img = img.resize((width, height))

    # Calculate buffer size for one bit-plane (1 bit per pixel)
    plane_size = (width * height) // 8

    if img_format == "3color":
        img_rgb = img.convert("RGB")
        bw_plane = bytearray(plane_size)
        red_plane = bytearray(plane_size)
        
        pixels = img_rgb.load()
        for y in range(height):
            for x in range(width):
                r, g, b = pixels[x, y]
                pixel_idx = y * width + x
                idx = pixel_idx // 8
                bit = 7 - (pixel_idx % 8)
                
                # Check if color is predominantly red
                if r > 150 and g < 100 and b < 100:
                    red_plane[idx] |= (1 << bit)   # Mark as red pixel
                else:
                    # Otherwise map to standard B/W
                    luminance = int(0.299*r + 0.587*g + 0.114*b)
                    if luminance > 127:
                        bw_plane[idx] |= (1 << bit) # White
                        
        return bytes(bw_plane + red_plane)

    elif img_format == "4gray":
        # Grayscale panels expect 2 bits per pixel (00=black, 01=dark gray, 10=light gray, 11=white)
        img_gray = img.convert("L")
        gray_buffer = bytearray(plane_size * 2)
        pixels = img_gray.load()
        
        for y in range(height):
            for x in range(width):
                val = pixels[x, y]
                val_2bit = val // 64
                
                pixel_idx = y * width + x
                byte_idx = pixel_idx // 4
                shift = (3 - (pixel_idx % 4)) * 2
                gray_buffer[byte_idx] |= (val_2bit << shift)
                
        return bytes(gray_buffer)

    else:
        # Default '1bit' Monochrome
        img_1bit = img.convert('1')
        return img_1bit.tobytes()

@app.route('/firmware.bin', methods=['GET'])
def download_firmware():
    """
    Endpoint to download the compiled ESP32 firmware binary directly.
    Allows easy flashing using browser Web Serial flashers (like esp.github.io/esptool-js).
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    root_path = os.path.join(base_dir, "firmware.bin")

    if os.path.exists(root_path):
        return send_file(root_path, as_attachment=True)
    return jsonify({
        "error": "Firmware not compiled yet. Run compilation command on the host first."
    }), 404

@app.route('/app.bin', methods=['GET'])
def download_app_bin():
    """
    Endpoint to download the raw (unmerged) compiled application binary.
    Flash this directly to address 0x10000.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base_dir, ".pio/build/seeed_xiao_esp32s3/firmware.bin")
    if os.path.exists(path):
        return send_file(path, as_attachment=True)
    return jsonify({"error": "Raw app.bin not found. Run compilation command on the host first."}), 404

@app.route('/bootloader.bin', methods=['GET'])
def download_bootloader():
    """
    Endpoint to download the PlatformIO-compiled bootloader binary.
    Flash this directly to address 0x0.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base_dir, ".pio/build/seeed_xiao_esp32s3/bootloader.bin")
    if os.path.exists(path):
        return send_file(path, as_attachment=True)
    return jsonify({"error": "bootloader.bin not found. Compile first."}), 404

@app.route('/partitions.bin', methods=['GET'])
def download_partitions():
    """
    Endpoint to download the PlatformIO-compiled partition table binary.
    Flash this directly to address 0x8000.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base_dir, ".pio/build/seeed_xiao_esp32s3/partitions.bin")
    if os.path.exists(path):
        return send_file(path, as_attachment=True)
    return jsonify({"error": "partitions.bin not found. Compile first."}), 404

@app.route('/boot_app0.bin', methods=['GET'])
def download_boot_app0():
    """
    Endpoint to download the boot_app0 binary.
    Flash this directly to address 0xe000.
    """
    # Look for it inside the container's platformio package path
    path = "/root/.platformio/packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin"
    if os.path.exists(path):
        return send_file(path, as_attachment=True)
    return jsonify({"error": "boot_app0.bin not found in framework directory."}), 404

if __name__ == '__main__':
    # Run development server on port 5000 if executed directly
    app.run(host='0.0.0.0', port=5000)
