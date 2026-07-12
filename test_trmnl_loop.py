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

        @patch("trmnl_loop.get_config")
        @patch("requests.post")
        def test_when_byos_url_is_configured_forwards_post_request(self, mock_post, mock_get_config, client):
            mock_get_config.return_value = {"TRMNL_BYOS_URL": "http://192.168.1.10"}
            mock_post_response = MagicMock()
            mock_post_response.content = b"post_success"
            mock_post_response.status_code = 200
            mock_post_response.headers = {"Some-Header": "Value"}
            mock_post.return_value = mock_post_response

            response = client.post('/api/setup', json={"mac": "00:11:22:33:44:55"})
            assert response.status_code == 200
            assert response.data == b"post_success"
            mock_post.assert_called_once()

        @patch("trmnl_loop.get_config")
        @patch("requests.get")
        def test_when_setup_request_raises_exception_returns_500(self, mock_get, mock_get_config, client):
            mock_get_config.return_value = {"TRMNL_BYOS_URL": "http://192.168.1.10"}
            mock_get.side_effect = Exception("Connection Timeout")

            response = client.get('/api/setup')
            assert response.status_code == 500
            assert "Proxy setup request failed" in response.json["error"]

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

        @patch("trmnl_loop.get_config")
        @patch("requests.get")
        def test_when_first_screen_request_returns_non_200_returns_that_status_code(self, mock_get, mock_get_config, client):
            mock_get_config.return_value = {"TRMNL_BYOS_URL": "http://192.168.1.10"}
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_resp.content = b"Not Found"
            mock_resp.headers = {"Content-Type": "text/plain"}
            mock_get.return_value = mock_resp

            response = client.get('/api/display')
            assert response.status_code == 404
            assert response.data == b"Not Found"

        @patch("trmnl_loop.get_config")
        @patch("requests.get")
        def test_when_first_screen_request_returns_500_device_not_found_forwards_response(self, mock_get, mock_get_config, client):
            mock_get_config.return_value = {"TRMNL_BYOS_URL": "http://192.168.1.10"}
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_resp.content = b"Device not found"
            mock_resp.headers = {"Content-Type": "text/plain"}
            mock_get.return_value = mock_resp

            response = client.get('/api/display')
            assert response.status_code == 500
            assert response.data == b"Device not found"

        @patch("trmnl_loop.get_config")
        @patch("requests.get")
        def test_when_first_screen_request_raises_exception_returns_500(self, mock_get, mock_get_config, client):
            mock_get_config.return_value = {"TRMNL_BYOS_URL": "http://192.168.1.10"}
            mock_get.side_effect = Exception("Connection refused")

            response = client.get('/api/display')
            assert response.status_code == 500
            assert "Failed to connect to BYOS backend" in response.json["error"]

        @patch("trmnl_loop.get_config")
        @patch("requests.get")
        @patch("trmnl_loop.download_and_convert_image")
        def test_when_intermediate_screen_request_fails_gracefully_continues(self, mock_convert, mock_get, mock_get_config, client):
            mock_get_config.return_value = {
                "TRMNL_BYOS_URL": "http://192.168.1.10",
                "CYCLE_INTERVAL": 300,
                "REFRESH_PADDING": 60
            }

            def get_side_effect(url, **kwargs):
                resp = MagicMock()
                if "step=0" in url:
                    resp.status_code = 200
                    resp.json.return_value = {
                        "refresh_rate": 600,
                        "image_url": "http://byos/img0.png",
                        "maximum_compatibility": False
                    }
                else:
                    resp.status_code = 500
                return resp

            mock_get.side_effect = get_side_effect
            mock_convert.return_value = b"raw_img0"

            response = client.get('/api/display')
            assert response.status_code == 200
            assert response.headers.get("X-Batch-Count") == "1"
            assert response.data == b"raw_img0"

        @patch("trmnl_loop.get_config")
        @patch("requests.get")
        def test_when_no_valid_screens_fetched_returns_500(self, mock_get, mock_get_config, client):
            mock_get_config.return_value = {"TRMNL_BYOS_URL": "http://192.168.1.10"}
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "refresh_rate": 600,
                "image_url": None,
                "maximum_compatibility": False
            }
            mock_get.return_value = resp

            response = client.get('/api/display')
            assert response.status_code == 500
            assert "Failed to fetch any screens" in response.json["error"]

        @patch("trmnl_loop.get_config")
        @patch("requests.get")
        @patch("trmnl_loop.download_and_convert_image")
        def test_returns_correct_custom_headers(self, mock_convert, mock_get, mock_get_config, client):
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
                resp.json.return_value = {
                    "refresh_rate": 600,
                    "image_url": f"http://byos/img{step}.png",
                    "maximum_compatibility": True,
                    "special_function": "fast_refresh"
                }
                return resp
            
            mock_get.side_effect = get_side_effect
            mock_convert.side_effect = lambda url, fmt, w, h: f"raw_{url.split('/')[-1]}".encode()

            response = client.get('/api/display')
            assert response.status_code == 200
            assert response.headers.get("X-Batch-Count") == "2"
            assert response.headers.get("X-Cycle-Interval") == "300"
            assert response.headers.get("X-Hard-Refresh") == "660" # 600 + 60
            assert response.headers.get("X-Max-Compatibility") == "1"
            assert response.headers.get("X-Frame-Size") == "12" # len(b"raw_img0.png")
            assert response.headers.get("X-Special-Function") == "fast_refresh"

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

        @patch("requests.get")
        def test_download_and_convert_to_4gray(self, mock_get):
            img = Image.new("RGB", (10, 10), color="white")
            img_bytes = io.BytesIO()
            img.save(img_bytes, format="PNG")
            img_bytes.seek(0)

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = img_bytes.read()
            mock_get.return_value = mock_resp

            raw_bytes_4gray = trmnl_loop.download_and_convert_image("http://byos/img.png", "4gray", 800, 480)
            assert len(raw_bytes_4gray) == 96000

        @patch("requests.get")
        @patch("trmnl_loop.logger")
        def test_download_and_convert_resizes_image(self, mock_logger, mock_get):
            img = Image.new("RGB", (10, 10), color="white")
            img_bytes = io.BytesIO()
            img.save(img_bytes, format="PNG")
            img_bytes.seek(0)

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = img_bytes.read()
            mock_get.return_value = mock_resp

            raw_bytes = trmnl_loop.download_and_convert_image("http://byos/img.png", "1bit", 16, 16)
            assert len(raw_bytes) == (16 * 16) // 8
            mock_logger.warning.assert_any_call("Resizing image from (10, 10) to (16, 16)")

        @patch("requests.get")
        def test_download_and_convert_removes_transparency(self, mock_get):
            img = Image.new("RGBA", (16, 16), color=(255, 0, 0, 128))
            img_bytes = io.BytesIO()
            img.save(img_bytes, format="PNG")
            img_bytes.seek(0)

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = img_bytes.read()
            mock_get.return_value = mock_resp

            raw_bytes = trmnl_loop.download_and_convert_image("http://byos/transparent.png", "1bit", 16, 16)
            assert len(raw_bytes) == (16 * 16) // 8

        @patch("requests.get")
        def test_download_and_convert_failed_download_raises_exception(self, mock_get):
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_get.return_value = mock_resp

            with pytest.raises(Exception) as exc_info:
                trmnl_loop.download_and_convert_image("http://byos/404.png")
            assert "HTTP 404 when downloading image" in str(exc_info.value)

    class TestQueryParameterValidation:
        @patch("trmnl_loop.get_config")
        @patch("requests.get")
        @patch("trmnl_loop.download_and_convert_image")
        def test_invalid_width_and_height_query_params_fallback(self, mock_convert, mock_get, mock_get_config, client):
            mock_get_config.return_value = {
                "TRMNL_BYOS_URL": "http://192.168.1.10",
                "CYCLE_INTERVAL": 300
            }
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "refresh_rate": 300,
                "image_url": "http://byos/img0.png"
            }
            mock_get.return_value = mock_resp
            mock_convert.return_value = b"raw"

            client.get('/api/display?width=abc&height=xyz')
            mock_convert.assert_called_with("http://byos/img0.png", "1bit", 800, 480)

        @patch("trmnl_loop.get_config")
        @patch("requests.get")
        @patch("trmnl_loop.download_and_convert_image")
        def test_invalid_format_query_param_fallback(self, mock_convert, mock_get, mock_get_config, client):
            mock_get_config.return_value = {
                "TRMNL_BYOS_URL": "http://192.168.1.10",
                "CYCLE_INTERVAL": 300
            }
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "refresh_rate": 300,
                "image_url": "http://byos/img0.png"
            }
            mock_get.return_value = mock_resp
            mock_convert.return_value = b"raw"

            client.get('/api/display?format=invalid_fmt')
            mock_convert.assert_called_with("http://byos/img0.png", "1bit", 800, 480)

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
