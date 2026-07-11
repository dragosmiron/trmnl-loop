import pytest
from unittest.mock import patch, mock_open, MagicMock
import os
import io
import json
import hashlib
from PIL import Image

import trmnl_loop

@pytest.fixture
def client():
    # Reset config state before each test
    trmnl_loop._config_cache = {}
    trmnl_loop._config_mtime = 0
    return trmnl_loop.app.test_client()

class TestTrmnlLoop:

    class TestGetConfig:
        @patch("os.path.exists")
        @patch("os.path.getmtime")
        @patch("builtins.open", new_callable=mock_open)
        def test_when_config_file_does_not_exist_returns_empty_dict(self, mock_file, mock_getmtime, mock_exists):
            mock_exists.return_value = False
            config = trmnl_loop.get_config()
            assert config == {}

        @patch("os.path.exists")
        @patch("os.path.getmtime")
        @patch("builtins.open", new_callable=mock_open)
        def test_when_config_file_is_valid_loads_values(self, mock_file, mock_getmtime, mock_exists):
            mock_exists.return_value = True
            mock_getmtime.return_value = 1000
            config_data = {
                "TRMNL_BYOS_URL": "http://1.2.3.4",
                "CYCLE_INTERVAL": 120,
                "REFRESH_PADDING": 30
            }
            mock_file.return_value.read.return_value = json.dumps(config_data)

            config = trmnl_loop.get_config()
            assert config["TRMNL_BYOS_URL"] == "http://1.2.3.4"
            assert config["CYCLE_INTERVAL"] == 120
            assert config["REFRESH_PADDING"] == 30

        @patch("os.path.exists")
        @patch("os.path.getmtime")
        @patch("builtins.open", new_callable=mock_open)
        def test_caches_values_until_modification_time_changes(self, mock_file, mock_getmtime, mock_exists):
            trmnl_loop._config_cache = {"TRMNL_BYOS_URL": "http://1.2.3.4"}
            trmnl_loop._config_mtime = 1000
            
            mock_exists.return_value = True
            mock_getmtime.return_value = 1000
            
            config = trmnl_loop.get_config()
            mock_file.assert_not_called()
            assert config["TRMNL_BYOS_URL"] == "http://1.2.3.4"

        @patch("os.path.exists")
        @patch("os.path.getmtime")
        @patch("builtins.open", new_callable=mock_open)
        def test_reloads_values_when_modification_time_increases(self, mock_file, mock_getmtime, mock_exists):
            trmnl_loop._config_cache = {"TRMNL_BYOS_URL": "http://1.2.3.4"}
            trmnl_loop._config_mtime = 1000
            
            mock_exists.return_value = True
            mock_getmtime.return_value = 1001
            new_config_data = {
                "TRMNL_BYOS_URL": "http://5.6.7.8",
                "CYCLE_INTERVAL": 150
            }
            mock_file.return_value.read.return_value = json.dumps(new_config_data)
            
            config = trmnl_loop.get_config()
            assert config["TRMNL_BYOS_URL"] == "http://5.6.7.8"

    class TestSetupProxy:
        @patch("trmnl_loop.get_config")
        def test_when_byos_url_is_missing_returns_500(self, mock_get_config, client):
            mock_get_config.return_value = {}
            response = client.get('/api/setup')
            assert response.status_code == 500
            assert "TRMNL_BYOS_URL is not configured" in response.json["error"]

        @patch("trmnl_loop.get_config")
        @patch("requests.get")
        def test_when_byos_url_is_configured_forwards_request(self, mock_get, mock_get_config, client):
            mock_get_config.return_value = {"TRMNL_BYOS_URL": "http://192.168.1.10"}
            mock_get_response = MagicMock()
            mock_get_response.content = b"success"
            mock_get_response.status_code = 200
            mock_get_response.headers = {"Some-Header": "Value"}
            mock_get.return_value = mock_get_response

            response = client.get('/api/setup')
            assert response.status_code == 200
            assert response.data == b"success"
            mock_get.assert_called_once()

    class TestDisplayBatch:
        @patch("trmnl_loop.get_config")
        @patch("requests.get")
        @patch("trmnl_loop.download_and_convert_image")
        def test_loop_protection_stops_fetching_when_duplicate_screen_detected(self, mock_convert, mock_get, mock_get_config, client):
            mock_get_config.return_value = {
                "TRMNL_BYOS_URL": "http://192.168.1.10",
                "CYCLE_INTERVAL": 300,
                "REFRESH_PADDING": 60
            }

            def get_side_effect(url, **kwargs):
                step = 0
                if "step=" in url:
                    step = int(url.split("step=")[-1].split("&")[0])
                
                resp = MagicMock()
                resp.status_code = 200
                img_url = f"http://byos/img{step}.png" if step < 2 else "http://byos/img0.png"
                
                resp.json.return_value = {
                    "refresh_rate": 1800,
                    "image_url": img_url,
                    "maximum_compatibility": False,
                    "special_function": "none"
                }
                return resp

            mock_get.side_effect = get_side_effect
            mock_convert.side_effect = lambda url, fmt, w, h: f"raw_{url.split('/')[-1]}".encode()

            response = client.get('/api/display')
            assert response.status_code == 200
            assert response.headers.get("X-Batch-Count") == "2"
            assert response.data == b"raw_img0.png" + b"raw_img1.png"

    class TestImageConversion:
        @patch("requests.get")
        def test_download_and_convert_to_1bit_monochrome(self, mock_get):
            img = Image.new("RGB", (10, 10), color="white")
            img_bytes = io.BytesIO()
            img.save(img_bytes, format="PNG")
            img_bytes.seek(0)

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = img_bytes.read()
            mock_get.return_value = mock_resp

            raw_bytes = trmnl_loop.download_and_convert_image("http://byos/img.png", "1bit", 800, 480)
            assert len(raw_bytes) == 48000

        @patch("requests.get")
        def test_download_and_convert_to_3color_plane(self, mock_get):
            img = Image.new("RGB", (10, 10), color="white")
            img_bytes = io.BytesIO()
            img.save(img_bytes, format="PNG")
            img_bytes.seek(0)

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = img_bytes.read()
            mock_get.return_value = mock_resp

            raw_bytes_3color = trmnl_loop.download_and_convert_image("http://byos/img.png", "3color", 800, 480)
            assert len(raw_bytes_3color) == 96000

    class TestFirmwareDownloads:
        @patch("os.path.exists")
        @patch("trmnl_loop.send_file")
        def test_when_files_exist_returns_file_response(self, mock_send_file, mock_exists, client):
            mock_exists.return_value = True
            mock_send_file.return_value = "file_sent"

            response = client.get('/firmware.bin')
            assert response.data == b"file_sent"

            response = client.get('/app.bin')
            assert response.data == b"file_sent"

        @patch("os.path.exists")
        def test_when_files_do_not_exist_returns_404(self, mock_exists, client):
            mock_exists.return_value = False
            response = client.get('/firmware.bin')
            assert response.status_code == 404
