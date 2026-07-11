import unittest
from unittest.mock import patch, mock_open, MagicMock
import os
import io
import json
import hashlib
from PIL import Image

# Import the flask app and functions from trmnl_loop
import trmnl_loop

class TestTrmnlLoop(unittest.TestCase):

    def setUp(self):
        # Reset the config cache state before each test
        trmnl_loop._config_cache = {}
        trmnl_loop._config_mtime = 0
        self.app = trmnl_loop.app.test_client()

    @patch("os.path.exists")
    @patch("os.path.getmtime")
    @patch("builtins.open", new_callable=mock_open)
    def test_get_config(self, mock_file, mock_getmtime, mock_exists):
        # 1. Test when config file does not exist
        mock_exists.return_value = False
        config = trmnl_loop.get_config()
        self.assertEqual(config, {})

        # 2. Test loading valid config file
        mock_exists.return_value = True
        mock_getmtime.return_value = 1000
        config_data = {
            "TRMNL_BYOS_URL": "http://1.2.3.4",
            "CYCLE_INTERVAL": 120,
            "REFRESH_PADDING": 30
        }
        mock_file.return_value.read.return_value = json.dumps(config_data)

        config = trmnl_loop.get_config()
        self.assertEqual(config["TRMNL_BYOS_URL"], "http://1.2.3.4")
        self.assertEqual(config["CYCLE_INTERVAL"], 120)

        # 3. Test that it caches and doesn't read again if modification time doesn't change
        mock_file.reset_mock()
        config = trmnl_loop.get_config()
        mock_file.assert_not_called()

        # 4. Test that it reloads if mtime increases
        mock_getmtime.return_value = 1001
        new_config_data = {
            "TRMNL_BYOS_URL": "http://5.6.7.8",
            "CYCLE_INTERVAL": 150
        }
        mock_file.return_value.read.return_value = json.dumps(new_config_data)
        config = trmnl_loop.get_config()
        self.assertEqual(config["TRMNL_BYOS_URL"], "http://5.6.7.8")

    @patch("trmnl_loop.get_config")
    @patch("requests.get")
    @patch("requests.post")
    def test_setup_proxy(self, mock_post, mock_get, mock_get_config):
        # Test error case: TRMNL_BYOS_URL is missing
        mock_get_config.return_value = {}
        response = self.app.get('/api/setup')
        self.assertEqual(response.status_code, 500)
        self.assertIn("TRMNL_BYOS_URL is not configured", response.json["error"])

        # Test forwarding success case
        mock_get_config.return_value = {"TRMNL_BYOS_URL": "http://192.168.1.10"}
        mock_get_response = MagicMock()
        mock_get_response.content = b"success"
        mock_get_response.status_code = 200
        mock_get_response.headers = {"Some-Header": "Value"}
        mock_get.return_value = mock_get_response

        response = self.app.get('/api/setup')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"success")
        mock_get.assert_called_with("http://192.168.1.10/api/setup", headers=unittest.mock.ANY, params=unittest.mock.ANY, timeout=10)

    @patch("trmnl_loop.get_config")
    @patch("requests.get")
    @patch("trmnl_loop.download_and_convert_image")
    def test_display_batch_loop_protection(self, mock_convert, mock_get, mock_get_config):
        mock_get_config.return_value = {
            "TRMNL_BYOS_URL": "http://192.168.1.10",
            "CYCLE_INTERVAL": 300,
            "REFRESH_PADDING": 60
        }

        # Mock requests.get side effect to simulate different URLs per step,
        # but returning the SAME URL at step 2 to trigger loop protection.
        def get_side_effect(url, **kwargs):
            step = 0
            if "step=" in url:
                step = int(url.split("step=")[-1].split("&")[0])
            
            resp = MagicMock()
            resp.status_code = 200
            
            # Step 0, 1 are unique. Step 2 repeats step 0
            img_url = f"http://byos/img{step}.png" if step < 2 else "http://byos/img0.png"
            
            resp.json.return_value = {
                "refresh_rate": 1800,  # 6 screens expected without loop protection
                "image_url": img_url,
                "maximum_compatibility": False,
                "special_function": "none"
            }
            return resp

        mock_get.side_effect = get_side_effect
        mock_convert.side_effect = lambda url, fmt, w, h: f"raw_{url.split('/')[-1]}".encode()

        response = self.app.get('/api/display')
        self.assertEqual(response.status_code, 200)
        
        # Loop protection should stop at 2 screens (img0 and img1), discarding img2 because it matches img0.
        self.assertEqual(response.headers.get("X-Batch-Count"), "2")
        self.assertEqual(response.data, b"raw_img0.png" + b"raw_img1.png")

    @patch("requests.get")
    def test_download_and_convert_image(self, mock_get):
        # Mock requests.get to download a tiny valid PNG image
        img = Image.new("RGB", (10, 10), color="white")
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="PNG")
        img_bytes.seek(0)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = img_bytes.read()
        mock_get.return_value = mock_resp

        # Test converting to 1-bit monochrome, resized to 800x480
        # Target buffer size = 800 * 480 / 8 = 48,000 bytes
        raw_bytes = trmnl_loop.download_and_convert_image("http://byos/img.png", "1bit", 800, 480)
        self.assertEqual(len(raw_bytes), 48000)

        # Test converting to 3color (black, white, red)
        # Target buffer size = 2 * (800 * 480 / 8) = 96,000 bytes
        raw_bytes_3color = trmnl_loop.download_and_convert_image("http://byos/img.png", "3color", 800, 480)
        self.assertEqual(len(raw_bytes_3color), 96000)

    @patch("os.path.exists")
    @patch("trmnl_loop.send_file")
    def test_firmware_downloads(self, mock_send_file, mock_exists):
        mock_exists.return_value = True
        mock_send_file.return_value = "file_sent"

        # Check endpoints return file response
        response = self.app.get('/firmware.bin')
        self.assertEqual(response.data, b"file_sent")

        response = self.app.get('/app.bin')
        self.assertEqual(response.data, b"file_sent")

        # Test 404 when file does not exist
        mock_exists.return_value = False
        response = self.app.get('/firmware.bin')
        self.assertEqual(response.status_code, 404)

if __name__ == '__main__':
    unittest.main()
