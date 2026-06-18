import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Add project root to path so we can import obsidian_codec
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from obsidian_codec.src.cmd_line.cli import build_ffmpeg_convert_cmd, main


class TestCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.mock_meta = {
            "duration": 120.0,
            "format_name": "mov",
            "video_streams": [{"codec_name": "h264", "index": 0}],
            "audio_streams": [{"codec_name": "aac", "index": 1}],
            "subtitle_streams": [],
            "chapters": [],
        }

    @patch("obsidian_codec.src.utils.ffmpeg_utils.get_supported_hw_encoders")
    def test_build_ffmpeg_convert_cmd_software(self, mock_hw: MagicMock) -> None:
        # Mock no hardware accelerators available
        mock_hw.return_value = []

        cmd = build_ffmpeg_convert_cmd(
            input_path="input.mp4",
            output_path="output.mp4",
            vcodec="libx264",
            acodec="aac",
            crf="23",
            preset="medium",
            resolution="original",
            audio_track=None,
            sub_track=None,
            sub_mode="soft",
            meta=self.mock_meta,
        )

        # Verify basic conversion command elements
        self.assertIn("-i", cmd)
        self.assertIn("input.mp4", cmd)
        self.assertIn("-c:v", cmd)
        self.assertIn("libx264", cmd)
        self.assertIn("-crf", cmd)
        self.assertIn("23", cmd)
        self.assertIn("-c:a", cmd)
        self.assertIn("aac", cmd)
        self.assertEqual(cmd[-1], "output.mp4")

    @patch("obsidian_codec.src.utils.ffmpeg_utils.get_supported_hw_encoders")
    def test_build_ffmpeg_convert_cmd_hardware_nvenc(self, mock_hw: MagicMock) -> None:
        # Mock nvenc available
        mock_hw.return_value = ["nvenc"]

        cmd = build_ffmpeg_convert_cmd(
            input_path="input.mp4",
            output_path="output.mp4",
            vcodec="libx264",
            acodec="copy",
            crf="20",
            preset="slow",
            resolution="1280x720",
            audio_track=0,
            sub_track=None,
            sub_mode="soft",
            meta=self.mock_meta,
        )

        # Check input hardware decoder was added
        self.assertIn("-c:v", cmd)
        self.assertIn("h264_cuvid", cmd)
        # Check output hardware encoder mapped
        self.assertIn("h264_nvenc", cmd)
        # Check resolution filter scale
        self.assertTrue(any("scale=1280:720" in arg for arg in cmd))
        # Check audio track map
        self.assertIn("-map", cmd)
        self.assertIn("0:a:0", cmd)

    @patch("obsidian_codec.src.cmd_line.cli.build_ffmpeg_convert_cmd")
    @patch("obsidian_codec.src.cmd_line.cli.run_ffmpeg_with_cli_progress")
    @patch("obsidian_codec.src.cmd_line.cli.probe_file")
    @patch("sys.argv")
    def test_cli_convert_subcommand(
        self, mock_argv: MagicMock, mock_probe: MagicMock, mock_run: MagicMock, mock_build: MagicMock
    ) -> None:
        mock_probe.return_value = self.mock_meta
        mock_build.return_value = ["ffmpeg", "-i", "dummy"]

        # Simulate 'convert' subcommand
        mock_argv.value = ["obsidian-cli", "convert", "tests/test.mp4", "--vcodec", "libx265", "--acodec", "libopus"]
        with patch("os.path.exists", return_value=True):
            with patch(
                "sys.argv", ["obsidian-cli", "convert", "tests/test.mp4", "--vcodec", "libx265", "--acodec", "libopus"]
            ):
                main()

        mock_build.assert_called_once()
        args, kwargs = mock_build.call_args
        self.assertEqual(args[2], "libx265")
        self.assertEqual(args[3], "libopus")

    @patch("obsidian_codec.src.cmd_line.cli.display_file_probe")
    @patch("os.path.exists")
    def test_cli_probe_subcommand(self, mock_exists: MagicMock, mock_display: MagicMock) -> None:
        mock_exists.return_value = True
        with patch("sys.argv", ["obsidian-cli", "probe", "tests/test.mp4"]):
            main()
        mock_display.assert_called_once_with("tests/test.mp4")


if __name__ == "__main__":
    unittest.main()
