import unittest
import os
import sys

# Add project root to path so we can import obsidian_codec
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from obsidian_codec.src.utils.ffmpeg_utils import (
    is_safe_path,
    is_safe_output_path,
    escape_ffmpeg_filter_path,
    validate_transcoding_combination,
    TEMP_DIR,
    OUTPUT_ROOT
)

class TestFfmpegUtils(unittest.TestCase):
    def test_is_safe_path(self):
        # Temp dir and Output root are safe
        self.assertTrue(is_safe_path(os.path.join(TEMP_DIR, "test.mp4")))
        self.assertTrue(is_safe_path(os.path.join(OUTPUT_ROOT, "subfolder", "movie.mkv")))
        
        # Test with restricted bases using env var to isolate tests from host filesystem layout
        os.environ["OBSIDIAN_CODEC_ALLOWED_BASES"] = TEMP_DIR
        try:
            self.assertFalse(is_safe_path(os.path.join(TEMP_DIR, "..", "..", "outside.txt")))
        finally:
            del os.environ["OBSIDIAN_CODEC_ALLOWED_BASES"]
            
        self.assertFalse(is_safe_path(None))

    def test_is_safe_output_path(self):
        # Valid output files in safe directories
        self.assertTrue(is_safe_output_path(os.path.join(TEMP_DIR, "test.mp4")))
        self.assertTrue(is_safe_output_path(os.path.join(OUTPUT_ROOT, "movie.mkv")))
        
        # Invalid extension is unsafe
        self.assertFalse(is_safe_output_path(os.path.join(TEMP_DIR, "test.exe")))
        self.assertFalse(is_safe_output_path(os.path.join(TEMP_DIR, "test.sh")))
        
        # Dotfile is unsafe
        self.assertFalse(is_safe_output_path(os.path.join(TEMP_DIR, ".hidden.mp4")))

    def test_escape_ffmpeg_filter_path(self):
        # Empty path
        self.assertEqual(escape_ffmpeg_filter_path(""), "")
        
        # Backslash conversion and colon escaping
        self.assertEqual(escape_ffmpeg_filter_path("C:\\path\\to\\subtitles.srt"), "C\\:/path/to/subtitles.srt")
        
        # Special characters colons, commas, semicolons, brackets
        self.assertEqual(escape_ffmpeg_filter_path("file:name,with;special[chars].srt"), "file\\:name\\,with\\;special\\[chars\\].srt")
        
        # Single quotes
        self.assertEqual(escape_ffmpeg_filter_path("sub'title.srt"), r"sub'\\''title.srt")

    def test_validate_transcoding_combination(self):
        # WebM compatibility check
        is_valid, msg = validate_transcoding_combination("webm", "libvpx-vp9", "libopus")
        self.assertTrue(is_valid)
        self.assertEqual(msg, "")
        
        is_valid, msg = validate_transcoding_combination("webm", "libx264", "aac")
        self.assertFalse(is_valid)
        self.assertIn("incompatible", msg.lower())
        
        # MP4 compatibility check
        is_valid, msg = validate_transcoding_combination("mp4", "libx264", "aac")
        self.assertTrue(is_valid)
        self.assertEqual(msg, "")
        
        # FLV compatibility check (does not support HEVC/libx265)
        is_valid, msg = validate_transcoding_combination("flv", "libx265", "aac")
        self.assertFalse(is_valid)
        self.assertIn("incompatible", msg.lower())

if __name__ == '__main__':
    unittest.main()
