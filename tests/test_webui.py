import unittest
from unittest.mock import patch, MagicMock
import json
import os
import sys

# Add project root to path so we can import obsidian_codec
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from obsidian_codec.src.web_ui.webui import app


class TestWebUI(unittest.TestCase):
    def setUp(self):
        # Configure app for testing
        app.config["TESTING"] = True
        self.client = app.test_client()
        # Retrieve the static CSRF token
        self.csrf_token = app.config["CSRF_TOKEN"]

    def test_home_route(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Obsidian Codec", response.data)

    def test_csrf_route(self):
        response = self.client.get("/api/csrf")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("token", data)
        self.assertEqual(data["token"], self.csrf_token)

    def test_csrf_protection_missing_token(self):
        # POST requests without CSRF should fail with 403
        response = self.client.post("/api/analyze", json={"filepath": "test.mp4"})
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"Invalid CSRF token", response.data)

    @patch("obsidian_codec.src.web_ui.webui.is_safe_path")
    @patch("os.path.exists")
    def test_csrf_protection_with_valid_token(self, mock_exists, mock_is_safe):
        mock_is_safe.return_value = True
        mock_exists.return_value = True
        # Send valid CSRF token in header
        headers = {"X-CSRF-Token": self.csrf_token}

        # Mock probe_file to avoid real execution
        with patch("obsidian_codec.src.web_ui.webui.probe_file") as mock_probe:
            mock_probe.return_value = {"duration": 100, "format_name": "mov", "video_streams": [], "audio_streams": []}

            response = self.client.post("/api/analyze", json={"filepath": "test.mp4"}, headers=headers)
            self.assertEqual(response.status_code, 200)
            data = json.loads(response.data)
            self.assertEqual(data.get("duration"), 100)

    @patch("obsidian_codec.src.web_ui.webui.is_safe_path")
    def test_analyze_unsafe_path(self, mock_is_safe):
        mock_is_safe.return_value = False
        headers = {"X-CSRF-Token": self.csrf_token}
        response = self.client.post("/api/analyze", json={"filepath": "/etc/passwd"}, headers=headers)
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"Access denied", response.data)

    def test_cleanup_session_valid_csrf(self):
        # test form-encoded POST body validation
        headers = {"X-CSRF-Token": self.csrf_token}

        with patch("shutil.rmtree") as mock_rmtree, patch("os.path.exists") as mock_exists:
            mock_exists.return_value = True

            response = self.client.post("/api/cleanup-session", data={"session_id": "test-session-id"}, headers=headers)
            self.assertEqual(response.status_code, 200)
            data = json.loads(response.data)
            self.assertTrue(data["success"])
            mock_rmtree.assert_called_once()

    @patch("obsidian_codec.src.web_ui.webui.is_safe_path")
    @patch("obsidian_codec.src.web_ui.webui.is_safe_output_path")
    @patch("obsidian_codec.src.web_ui.webui.probe_file")
    @patch("obsidian_codec.src.web_ui.webui.start_conversion_thread")
    @patch("os.path.exists")
    def test_api_convert_route(self, mock_exists, mock_start_thread, mock_probe, mock_safe_out, mock_safe):
        mock_safe.return_value = True
        mock_safe_out.return_value = True
        mock_exists.return_value = True
        mock_probe.return_value = {
            "duration": 60.0,
            "format_name": "mp4",
            "video_streams": [{"codec_name": "h264", "index": 0}],
            "audio_streams": [{"codec_name": "aac", "index": 1}],
        }
        mock_start_thread.return_value = MagicMock()

        headers = {"X-CSRF-Token": self.csrf_token}
        payload = {
            "session_id": "test-session",
            "input_path": "test.mp4",
            "operation": "convert",
            "video_codec": "libx264",
            "audio_codec": "aac",
            "format": "mp4",
            "crf": "23",
            "preset": "medium",
            "resolution": "original",
            "hw_accel": "none",
        }

        response = self.client.post("/api/convert", json=payload, headers=headers)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("job_id", data)

    @patch("obsidian_codec.src.web_ui.webui.is_safe_path")
    @patch("os.path.exists")
    def test_bearer_token_auth_success(self, mock_exists, mock_is_safe):
        mock_is_safe.return_value = True
        mock_exists.return_value = True
        # Set Authorization Bearer header without CSRF token
        headers = {"Authorization": f"Bearer {app.config['BEARER_TOKEN']}"}

        with patch("obsidian_codec.src.web_ui.webui.probe_file") as mock_probe:
            mock_probe.return_value = {"duration": 100}
            response = self.client.post("/api/analyze", json={"filepath": "test.mp4"}, headers=headers)
            self.assertEqual(response.status_code, 200)

    def test_bearer_token_auth_failure(self):
        # Invalid bearer token should be rejected
        headers = {"Authorization": "Bearer invalid_token_value"}
        response = self.client.post("/api/analyze", json={"filepath": "test.mp4"}, headers=headers)
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
