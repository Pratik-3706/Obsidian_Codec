import os
import sys
import json
import shutil
import subprocess
import threading
import time
import psutil
from typing import List, Dict, Any, Optional, Tuple, Union

# Active jobs tracking
# Format: { job_id: { 'status': ..., 'progress': ..., 'speed': ..., 'eta': ..., 'size': ..., 'log': [], 'process': ..., 'output_path': ..., 'input_path': ..., 'error': ... } }
ACTIVE_JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.RLock()
_SUPPORTED_HW_ENCODERS: Optional[List[str]] = None

TEMP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "temp"))
OUTPUT_ROOT = os.path.abspath(os.environ.get("OUTPUT_ROOT", os.path.expanduser("~/Obsidian_Codec_Output")))
OUTPUT_DIR = OUTPUT_ROOT

# Allow 2 parallel conversions
# NOTE: This Semaphore is in-memory (per-process). If deploying with multi-process
# WSGI servers (e.g. Gunicorn with multiple workers), the cap will apply per-worker.
CONVERSION_SEMAPHORE = threading.Semaphore(2)

JOBS_FILE = os.path.join(TEMP_DIR, "active_jobs.json")
CSRF_FILE = os.path.join(TEMP_DIR, "csrf_secret.txt")
BEARER_FILE = os.path.join(TEMP_DIR, "bearer_token.txt")

# Dedicated lock to serialize file writes to JOBS_FILE across threads
_JOBS_FILE_LOCK = threading.Lock()


def escape_ffmpeg_filter_path(path: str) -> str:
    """Escapes special characters in path for FFmpeg filter graph (subtitles filter)."""
    if not path:
        return ""
    # Replace backslashes with forward slashes for cross-platform stability
    p = path.replace("\\", "/")

    # Escape special characters that are parsed by the filter graph option parser
    # Characters that need escaping in filter graphs: colons, commas, semicolons, brackets
    escaped = []
    for char in p:
        if char in (":", ",", ";", "[", "]"):
            escaped.append("\\" + char)
        elif char == "'":
            # Escaping single quote in single quoted string literal in filtergraph:
            # We break out of single quotes, insert an escaped single quote, and restart single quotes.
            escaped.append("'\\\\''")
        else:
            escaped.append(char)
    return "".join(escaped)


def is_safe_path(path: Optional[str]) -> bool:
    if not path:
        return False
    try:
        path_abs = os.path.abspath(os.path.realpath(path))
        allowed_dirs = [TEMP_DIR, OUTPUT_ROOT]

        custom_bases = os.environ.get("OBSIDIAN_CODEC_ALLOWED_BASES")
        if custom_bases:
            for base in custom_bases.split(os.pathsep):
                if base.strip():
                    allowed_dirs.append(os.path.abspath(os.path.realpath(base.strip())))
        else:
            allowed_dirs.append(os.path.abspath(os.path.realpath(os.path.expanduser("~"))))

        for parent in allowed_dirs:
            parent_abs = os.path.abspath(os.path.realpath(parent))
            try:
                if os.path.commonpath([path_abs, parent_abs]) == parent_abs:
                    return True
            except ValueError:
                pass
    except Exception:
        pass
    return False


def save_jobs_to_disk() -> None:
    """Persist active jobs to disk. Uses a dedicated file lock to prevent
    concurrent writes from the stderr-reader and progress-parser threads,
    and retries os.replace() to handle transient Windows file locks."""
    if not _JOBS_FILE_LOCK.acquire(blocking=False):
        # Another thread is already writing – skip this save to avoid piling up.
        return
    try:
        ensure_temp_dir()
        serializable = {}
        with JOBS_LOCK:
            for jid, job in ACTIVE_JOBS.items():
                serializable[jid] = {k: v for k, v in job.items() if k != "process"}
        tmp_file = JOBS_FILE + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2)

        # Retry os.replace() up to 3 times to handle transient Windows file locks
        # (antivirus, search indexer, or another thread finishing a read).
        for attempt in range(3):
            try:
                os.replace(tmp_file, JOBS_FILE)
                break
            except OSError:
                if attempt < 2:
                    time.sleep(0.05)
                else:
                    # Last attempt failed – silently clean up the temp file
                    try:
                        os.unlink(tmp_file)
                    except OSError:
                        pass
    except Exception:
        # Non-critical persistence – swallow to avoid polluting CLI/WebUI output
        pass
    finally:
        _JOBS_FILE_LOCK.release()


def load_jobs_from_disk() -> None:
    global ACTIVE_JOBS
    if os.path.exists(JOBS_FILE):
        with _JOBS_FILE_LOCK:
            try:
                with open(JOBS_FILE, "r", encoding="utf-8") as f:
                    jobs = json.load(f)
                for jid, job in jobs.items():
                    if job.get("status") in ("running", "pending"):
                        job["status"] = "failed"
                        job["error"] = "Server restarted during encoding."
                        job["finished_time"] = time.time()
                    job["process"] = None
                    ACTIVE_JOBS[jid] = job
            except Exception as e:
                print(f"Error loading jobs: {e}", file=sys.stderr)


def is_safe_output_path(path: Optional[str]) -> bool:
    if not path:
        return False
    if not is_safe_path(path):
        return False
    if os.path.islink(path):
        return False
    ext = os.path.splitext(path)[1].lower()
    allowed_exts = {
        ".mp4",
        ".mkv",
        ".avi",
        ".mov",
        ".webm",
        ".ts",
        ".flv",
        ".ogg",
        ".m4v",
        ".mp3",
        ".opus",
        ".aac",
        ".wav",
        ".flac",
        ".alac",
        ".m4a",
        ".srt",
        ".vtt",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".json",
    }
    if ext not in allowed_exts:
        return False
    base_name = os.path.basename(path)
    if base_name.startswith("."):
        return False
    return True


TRANSCODING_COMPATIBILITY_MATRIX = {
    "webm": {
        "video": ["libvpx", "libvpx-vp9", "libaom-av1", "copy"],
        "audio": ["libvorbis", "libopus", "copy", "none"],
        "notes": "WebM container only supports VP8, VP9, and AV1 video, and Vorbis and Opus audio.",
    },
    "ogg": {
        "video": ["none", "copy"],
        "audio": ["libvorbis", "libopus", "flac", "copy", "none"],
        "notes": "Ogg container only supports Vorbis, Opus, and FLAC audio. Video stream encoding to Ogg is not supported.",
    },
    "flv": {
        "video": ["libx264", "mpeg4", "copy"],
        "audio": ["aac", "libmp3lame", "pcm_s16le", "copy", "none"],
        "notes": "FLV container does not support modern codecs like HEVC (libx265), VP9, AV1, or audio codecs like Opus, FLAC, AC3, ALAC, or Vorbis.",
    },
    "ts": {
        "video": ["libx264", "libx265", "mpeg4", "copy"],
        "audio": ["aac", "libmp3lame", "ac3", "copy", "none"],
        "notes": "MPEG-TS container does not support VP9, AV1, VP8, ProRes video, or Opus, FLAC, Vorbis, ALAC audio.",
    },
    "avi": {
        "video": ["libx264", "mpeg4", "libxvid", "copy"],
        "audio": ["libmp3lame", "ac3", "pcm_s16le", "copy", "none"],
        "notes": "AVI container does not support HEVC (libx265), VP9, AV1, ProRes, VP8 video, or AAC, Opus, FLAC, Vorbis, ALAC audio.",
    },
    "mov": {
        "video": ["libx264", "libx265", "prores", "mpeg4", "libxvid", "libvpx-vp9", "libaom-av1", "copy"],
        "audio": ["aac", "libmp3lame", "alac", "pcm_s16le", "ac3", "flac", "copy", "none"],
        "notes": "QuickTime MOV supports H.264, HEVC, ProRes, MPEG-4, VP9, AV1 video, and AAC, MP3, ALAC, PCM, AC3, FLAC audio. VP8 video and Opus/Vorbis audio are not supported.",
    },
    "mp4": {
        "video": ["libx264", "libx265", "libvpx-vp9", "libaom-av1", "mpeg4", "libxvid", "copy"],
        "audio": ["aac", "libmp3lame", "libopus", "flac", "ac3", "alac", "libvorbis", "pcm_s16le", "copy", "none"],
        "notes": "MP4 container does not support ProRes or VP8 video.",
    },
    "m4v": {
        "video": ["libx264", "libx265", "libvpx-vp9", "libaom-av1", "mpeg4", "libxvid", "copy"],
        "audio": ["aac", "libmp3lame", "libopus", "flac", "ac3", "alac", "libvorbis", "pcm_s16le", "copy", "none"],
        "notes": "M4V container does not support ProRes or VP8 video.",
    },
}

VIDEO_CODEC_MAP = {
    "h264": "libx264",
    "hevc": "libx265",
    "vp9": "libvpx-vp9",
    "av1": "libaom-av1",
    "prores": "prores",
    "mpeg4": "mpeg4",
    "vp8": "libvpx",
    "xvid": "libxvid",
}

AUDIO_CODEC_MAP = {
    "mp3": "libmp3lame",
    "vorbis": "libvorbis",
    "opus": "libopus",
}


def resolve_transcoding_codec(
    codec: str,
    codec_type: str,
    meta: Optional[Dict[str, Any]] = None,
    audio_track_idx: Optional[Union[int, str]] = None,
) -> str:
    if codec != "copy" or not meta:
        return codec

    if codec_type == "video" and meta.get("video_streams"):
        src_v = meta["video_streams"][0].get("codec_name")
        return str(VIDEO_CODEC_MAP.get(src_v, src_v))

    if codec_type == "audio" and meta.get("audio_streams"):
        track_idx = int(audio_track_idx) if audio_track_idx is not None and str(audio_track_idx).isdigit() else 0
        if track_idx < len(meta["audio_streams"]):
            src_a = meta["audio_streams"][track_idx].get("codec_name")
            return str(AUDIO_CODEC_MAP.get(src_a, src_a))

    return codec


def get_compatible_transcoding_codecs(
    container: str,
    codec_choices: List[str],
    codec_type: str,
    meta: Optional[Dict[str, Any]] = None,
    audio_track_idx: Optional[Union[int, str]] = None,
) -> List[str]:
    compatible = []
    allowed_codecs = TRANSCODING_COMPATIBILITY_MATRIX.get(container.lower().lstrip("."), {}).get(codec_type, [])
    # If container is not in matrix, fallback/allow
    if not allowed_codecs:
        return codec_choices
    for codec in codec_choices:
        check_codec = resolve_transcoding_codec(codec, codec_type, meta, audio_track_idx)
        if check_codec in allowed_codecs:
            compatible.append(codec)
    return compatible


def ensure_temp_dir() -> None:
    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR)


def cleanup_temp_dir() -> None:
    """Removes all files in the temp directory except whitelisted config files."""
    ensure_temp_dir()
    whitelist = {
        os.path.basename(JOBS_FILE),
        os.path.basename(CSRF_FILE),
        os.path.basename(BEARER_FILE),
    }
    for filename in os.listdir(TEMP_DIR):
        if filename in whitelist:
            continue
        file_path = os.path.join(TEMP_DIR, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            print(f"Failed to delete {file_path}. Reason: {e}", file=sys.stderr)


def get_supported_hw_encoders() -> List[str]:
    """Runs short tests to check which hardware encoders are supported by the system."""
    global _SUPPORTED_HW_ENCODERS
    if _SUPPORTED_HW_ENCODERS is not None:
        return _SUPPORTED_HW_ENCODERS
    supported = []
    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    # NVENC test
    try:
        res = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc", "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=startupinfo,
            timeout=3,
        )
        if res.returncode == 0:
            supported.append("nvenc")
    except Exception:
        pass

    # QSV test
    try:
        res = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc", "-frames:v", "1", "-c:v", "h264_qsv", "-f", "null", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=startupinfo,
            timeout=3,
        )
        if res.returncode == 0:
            supported.append("qsv")
    except Exception:
        pass

    # AMF test
    try:
        res = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc", "-frames:v", "1", "-c:v", "h264_amf", "-f", "null", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=startupinfo,
            timeout=3,
        )
        if res.returncode == 0:
            supported.append("amf")
    except Exception:
        pass

    # Media Foundation (Windows built-in)
    try:
        res = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc", "-frames:v", "1", "-c:v", "h264_mf", "-f", "null", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=startupinfo,
            timeout=3,
        )
        if res.returncode == 0:
            supported.append("mf")
    except Exception:
        pass

    _SUPPORTED_HW_ENCODERS = supported
    return supported


def get_job_status(job_id: str) -> Optional[Dict[str, Any]]:
    with JOBS_LOCK:
        job = ACTIVE_JOBS.get(job_id)
        if not job:
            return None
        # Return a copy without the process object (which is not JSON serializable)
        return {k: v for k, v in job.items() if k != "process"}


def cancel_job(job_id: str) -> Tuple[bool, str]:
    with JOBS_LOCK:
        job = ACTIVE_JOBS.get(job_id)
        if not job:
            return False, "Job not found"

        if job["status"] not in ["running", "pending"]:
            return False, f"Job is in state '{job['status']}' and cannot be cancelled"

        # Terminate subprocess
        proc = job.get("process")
        if proc:
            try:
                # Terminate the process group or the process tree
                parent = psutil.Process(proc.pid)
                for child in parent.children(recursive=True):
                    child.terminate()
                parent.terminate()
                # Wait briefly
                gone, alive = psutil.wait_procs([parent] + parent.children(), timeout=2)
                for p in alive:
                    p.kill()
            except Exception as e:
                print(f"Error terminating process tree: {e}", file=sys.stderr)
                try:
                    proc.kill()
                except Exception:
                    pass

        job["status"] = "cancelled"
        job["error"] = "Job was cancelled by the user."
        job["finished_time"] = time.time()

        # Clean up output file
        out_path = job.get("output_path")
        if out_path and os.path.exists(out_path) and is_safe_output_path(out_path):
            try:
                os.unlink(out_path)
            except Exception as e:
                print(f"Failed to delete incomplete output file: {e}", file=sys.stderr)

        # Clean up temp inputs
        in_path = job.get("input_path")
        if in_path and TEMP_DIR in os.path.abspath(in_path) and os.path.exists(in_path):
            try:
                os.unlink(in_path)
            except Exception:
                pass

        save_jobs_to_disk()
        return True, "Job cancelled"


def run_ffprobe(args: List[str]) -> Dict[str, Any]:
    """Runs ffprobe with arguments and returns the parsed JSON output."""
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json"] + args
    try:
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=True,
            startupinfo=startupinfo,
            timeout=10,
        )
        parsed = json.loads(result.stdout)
        if isinstance(parsed, dict):
            return parsed
        return {}
    except subprocess.CalledProcessError as e:
        print(f"ffprobe error: {e.stderr}", file=sys.stderr)
        return {}
    except Exception as e:
        print(f"ffprobe exception: {e}", file=sys.stderr)
        return {}


def probe_file(file_path: str) -> Dict[str, Any]:
    """Probes a file and returns details about streams, format, and chapters."""
    if not os.path.exists(file_path):
        return {"error": "File does not exist"}

    probe = run_ffprobe(["-show_format", "-show_streams", "-show_chapters", file_path])

    if not probe:
        return {"error": "Failed to probe file"}

    format_info = probe.get("format", {})
    streams_info = probe.get("streams", [])
    chapters_info = probe.get("chapters", [])

    result = {
        "filename": os.path.basename(file_path),
        "filepath": os.path.abspath(file_path),
        "duration": float(format_info.get("duration", 0)),
        "size": int(format_info.get("size", 0)),
        "bitrate": int(format_info.get("bit_rate", 0)) if format_info.get("bit_rate") else 0,
        "format_name": format_info.get("format_name", ""),
        "format_long_name": format_info.get("format_long_name", ""),
        "video_streams": [],
        "audio_streams": [],
        "subtitle_streams": [],
        "chapters": [],
    }

    for s in streams_info:
        stream_type = s.get("codec_type")
        codec_name = s.get("codec_name")
        codec_long = s.get("codec_long_name", "")
        idx = s.get("index")

        # Safely get tags dictionary
        tags = s.get("tags")
        if not isinstance(tags, dict):
            tags = {}

        stream_data = {
            "index": idx,
            "codec_name": codec_name or "unknown",
            "codec_long_name": codec_long or "Unknown Codec",
            "bitrate": int(s.get("bit_rate", 0)) if s.get("bit_rate") else 0,
            "tags": tags,
        }

        display_codec = (codec_name or "unknown").upper()
        title = tags.get("title")
        lang = tags.get("language")
        stream_data["display_name"] = f"#{idx}: {display_codec}"
        if title:
            stream_data["display_name"] += f" - {title}"
        if lang:
            stream_data["display_name"] += f" ({lang})"

        if stream_type == "video":
            stream_data.update(
                {
                    "width": int(s.get("width", 0)),
                    "height": int(s.get("height", 0)),
                    "r_frame_rate": s.get("r_frame_rate", "0/0"),
                    "avg_frame_rate": s.get("avg_frame_rate", "0/0"),
                }
            )
            result["video_streams"].append(stream_data)
        elif stream_type == "audio":
            stream_data.update(
                {
                    "channels": int(s.get("channels", 0)),
                    "channel_layout": s.get("channel_layout", "unknown"),
                    "sample_rate": int(s.get("sample_rate", 0)) if s.get("sample_rate") else 0,
                }
            )
            result["audio_streams"].append(stream_data)
        elif stream_type == "subtitle":
            result["subtitle_streams"].append(stream_data)

    for chap in chapters_info:
        result["chapters"].append(
            {
                "id": chap.get("id"),
                "title": chap.get("tags", {}).get("title", f"Chapter {chap.get('id')}"),
                "start": float(chap.get("start_time", 0)),
                "end": float(chap.get("end_time", 0)),
            }
        )

    return result


def run_ffmpeg_subprocess(
    job_id: str, cmd: List[str], total_duration: float, output_path: str, input_path: Optional[str]
) -> None:
    """Runs the ffmpeg command as a subprocess and parses progress logs."""
    ensure_temp_dir()

    try:
        with JOBS_LOCK:
            if job_id not in ACTIVE_JOBS:
                # If deleted or cancelled before starting
                return
            ACTIVE_JOBS[job_id]["status"] = "running"
            ACTIVE_JOBS[job_id]["output_path"] = output_path
            ACTIVE_JOBS[job_id]["input_path"] = input_path
        save_jobs_to_disk()

        try:
            # We append '-progress pipe:1' to get updates on stdout.
            # Ensure we place it properly.
            # But wait, if cmd already runs progress, don't duplicate it.
            if "-progress" not in cmd:
                cmd += ["-progress", "pipe:1"]

            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            # Run process
            # stdout: progress info, stderr: logs / warnings / errors
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                startupinfo=startupinfo,
            )

            with JOBS_LOCK:
                # Re-check state, maybe it was cancelled during launch
                if ACTIVE_JOBS[job_id]["status"] == "cancelled":
                    proc.kill()
                    return
                ACTIVE_JOBS[job_id]["process"] = proc
            save_jobs_to_disk()

            # Thread to read stderr logs so they don't block and we can show them to user
            stderr_lines = ["Command: " + " ".join(cmd)]

            def read_stderr() -> None:
                if proc.stderr:
                    for line in proc.stderr:
                        stripped = line.strip()
                        if stripped:
                            stderr_lines.append(stripped)
                            # Limit log buffer size
                            if len(stderr_lines) > 200:
                                stderr_lines.pop(0)
                            with JOBS_LOCK:
                                if job_id in ACTIVE_JOBS:
                                    ACTIVE_JOBS[job_id]["log"] = list(stderr_lines)
                            # NOTE: We intentionally do NOT call save_jobs_to_disk() here.
                            # Stderr lines arrive at very high frequency (dozens/sec during
                            # FFmpeg init) and concurrent file writes cause WinError 5/32
                            # on Windows. The progress-parser loop already persists state
                            # on every progress packet, which is sufficient.

            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stderr_thread.start()

            # Parse progress on stdout
            current_progress = 0.0
            current_speed = "0.0x"
            current_size = "0 B"
            current_eta = "Unknown"
            current_time = 0.0

            progress_data = {}

            if proc.stdout:
                for line in proc.stdout:
                    # We are parsing key=value pairs
                    if "=" in line:
                        key, val = line.strip().split("=", 1)
                        progress_data[key] = val

                    # We update on 'progress=continue' or when it completes
                    if key == "progress":
                        out_time_us = progress_data.get("out_time_us", "0")
                        try:
                            current_time = max(0.0, float(out_time_us) / 1000000.0)
                        except (ValueError, TypeError):
                            pass

                        if total_duration > 0:
                            current_progress = min(100.0, (current_time / total_duration) * 100.0)

                        speed_val = progress_data.get("speed", "0.0x").strip()
                        if speed_val != "N/A":
                            current_speed = speed_val

                        size_bytes = progress_data.get("total_size", "0")
                        if size_bytes.isdigit():
                            sb = int(size_bytes)
                            if sb < 1024:
                                current_size = f"{sb} B"
                            elif sb < 1024 * 1024:
                                current_size = f"{sb / 1024:.2f} KB"
                            elif sb < 1024 * 1024 * 1024:
                                current_size = f"{sb / (1024 * 1024):.2f} MB"
                            else:
                                current_size = f"{sb / (1024 * 1024 * 1024):.2f} GB"

                        # Calculate ETA
                        if current_progress > 0 and current_progress < 100:
                            speed_factor = 1.0
                            if current_speed.endswith("x"):
                                try:
                                    speed_factor = float(current_speed[:-1])
                                except ValueError:
                                    pass
                            if speed_factor > 0:
                                rem_time = (total_duration - current_time) / speed_factor
                                if rem_time >= 0:
                                    mins, secs = divmod(int(rem_time), 60)
                                    hrs, mins = divmod(mins, 60)
                                    if hrs > 0:
                                        current_eta = f"{hrs:02d}:{mins:02d}:{secs:02d}"
                                    else:
                                        current_eta = f"{mins:02d}:{secs:02d}"
                            else:
                                current_eta = "Unknown"
                        elif current_progress >= 100:
                            current_eta = "00:00"

                        # Update active job state
                        with JOBS_LOCK:
                            if job_id in ACTIVE_JOBS and ACTIVE_JOBS[job_id]["status"] == "running":
                                ACTIVE_JOBS[job_id].update(
                                    {
                                        "progress": round(current_progress, 2),
                                        "speed": current_speed,
                                        "size": current_size,
                                        "eta": current_eta,
                                        "out_time": current_time,
                                    }
                                )
                        save_jobs_to_disk()

                        progress_data = {}  # Reset for next packet

            proc.wait()
            stderr_thread.join(timeout=1.0)

            with JOBS_LOCK:
                if job_id in ACTIVE_JOBS:
                    if ACTIVE_JOBS[job_id]["status"] == "cancelled":
                        # Already handled by cancellation method
                        return
                    if proc.returncode == 0:
                        ACTIVE_JOBS[job_id]["status"] = "completed"
                        ACTIVE_JOBS[job_id]["finished_time"] = time.time()
                        ACTIVE_JOBS[job_id]["progress"] = 100.0
                        ACTIVE_JOBS[job_id]["eta"] = "00:00"

                        # Log final actual output size if available
                        if os.path.exists(output_path):
                            sb = os.path.getsize(output_path)
                            if sb < 1024 * 1024:
                                ACTIVE_JOBS[job_id]["size"] = f"{sb / 1024:.2f} KB"
                            elif sb < 1024 * 1024 * 1024:
                                ACTIVE_JOBS[job_id]["size"] = f"{sb / (1024 * 1024):.2f} MB"
                            else:
                                ACTIVE_JOBS[job_id]["size"] = f"{sb / (1024 * 1024 * 1024):.2f} GB"
                    else:
                        ACTIVE_JOBS[job_id]["status"] = "failed"
                        ACTIVE_JOBS[job_id]["finished_time"] = time.time()
                        err_msg = "\n".join(stderr_lines[-5:]) if stderr_lines else "Unknown FFmpeg error"
                        ACTIVE_JOBS[job_id]["error"] = (
                            f"FFmpeg failed with exit code {proc.returncode}. Error:\n{err_msg}"
                        )
                        # Remove incomplete output file
                        if os.path.exists(output_path) and is_safe_output_path(output_path):
                            try:
                                os.unlink(output_path)
                            except Exception:
                                pass
            save_jobs_to_disk()

        except Exception as e:
            print(f"Exception in running FFmpeg job: {e}", file=sys.stderr)
            with JOBS_LOCK:
                if job_id in ACTIVE_JOBS and ACTIVE_JOBS[job_id]["status"] != "cancelled":
                    ACTIVE_JOBS[job_id]["status"] = "failed"
                    ACTIVE_JOBS[job_id]["finished_time"] = time.time()
                    ACTIVE_JOBS[job_id]["error"] = str(e)
            save_jobs_to_disk()
            if os.path.exists(output_path) and is_safe_output_path(output_path):
                try:
                    os.unlink(output_path)
                except Exception:
                    pass
    finally:
        CONVERSION_SEMAPHORE.release()
        # Clean up temp inputs
        if input_path and TEMP_DIR in os.path.abspath(input_path) and os.path.exists(input_path):
            try:
                os.unlink(input_path)
            except Exception:
                pass


def start_conversion_thread(
    job_id: str, cmd: List[str], total_duration: float, output_path: str, input_path: Optional[str] = None
) -> Optional[threading.Thread]:
    """Starts the FFmpeg process in a background thread."""
    ensure_temp_dir()

    # Try to acquire the semaphore first non-blockingly
    acquired = CONVERSION_SEMAPHORE.acquire(blocking=False)
    if not acquired:
        return None

    # Prune old finished jobs from memory (older than 5 minutes / 300 seconds)
    now = time.time()
    with JOBS_LOCK:
        expired_ids = [
            jid for jid, j in ACTIVE_JOBS.items() if j.get("finished_time") and (now - j["finished_time"] > 300)
        ]
        for jid in expired_ids:
            del ACTIVE_JOBS[jid]

        ACTIVE_JOBS[job_id] = {
            "status": "pending",
            "progress": 0.0,
            "speed": "0.0x",
            "eta": "Pending",
            "size": "0 B",
            "log": [],
            "process": None,
            "output_path": output_path,
            "input_path": input_path,
            "error": None,
            "finished_time": None,
        }
    save_jobs_to_disk()

    t = threading.Thread(
        target=run_ffmpeg_subprocess, args=(job_id, cmd, total_duration, output_path, input_path), daemon=True
    )
    t.start()
    return t


def generate_thumbnail_grid(
    input_path: str, output_path: str, rows: int = 4, cols: int = 4, duration: float = 0
) -> str:
    """Generates a contact sheet thumbnail grid using FFmpeg select and tile filters."""
    if duration <= 0:
        meta = probe_file(input_path)
        duration = meta.get("duration", 0)
        if duration <= 0:
            raise ValueError("Could not determine video duration for thumbnail generation.")

    num_thumbs = rows * cols
    # Calculate interval: we divide the duration into num_thumbs + 1 segments and pick the boundaries
    # select filter is select='not(mod(n, interval_frames))' or time based:
    # select='not(mod(t, interval_secs))'
    interval = duration / (num_thumbs + 1)
    if interval < 0.5:
        interval = 0.5  # Avoid division by zero/very small numbers

    # Build filter: select frames, scale them down, and tile them
    # select='expr': we select frames near the target times
    # A robust way is select=not(mod(t\,interval))
    # Let's add timestamps text overlay to each tiled frame:
    # Drawtext parameters: drawtext=text='%%{pts\:hms}':fontsize=16:fontcolor=white:box=1:boxcolor=black@0.6:x=10:y=10
    # Combining filters: select, scale, drawtext, tile
    # We must escape commas and colons inside drawtext filter arguments:
    # We escape them with backslashes
    drawtext_filter = (
        "drawtext=text='%{pts\\:hms}':fontsize=14:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=4:x=10:y=10"
    )
    filter_graph = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{interval:.3f})',scale=320:-1,{drawtext_filter},tile={cols}x{rows}"

    cmd = ["ffmpeg", "-y", "-i", input_path, "-vf", filter_graph, "-frames:v", "1", "-vsync", "vfr", output_path]

    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    res = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        startupinfo=startupinfo,
        timeout=60,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Thumbnail grid generation failed: {res.stderr}")
    return output_path


def get_detected_gpus() -> List[str]:
    """Returns a list of physical GPU names detected on the system."""
    gpus: List[str] = []
    if sys.platform == "win32":
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            res = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "name"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                startupinfo=startupinfo,
                timeout=3,
            )
            if res.returncode == 0:
                lines = [line.strip() for line in res.stdout.split("\n") if line.strip()]
                # Skip header 'Name' if present
                if len(lines) > 1 and lines[0].lower() == "name":
                    gpus = [g for g in lines[1:] if g]
                else:
                    gpus = [g for g in lines if g]
        except Exception:
            pass

    if not gpus:
        # Fallback to nvidia-smi (cross-platform / Linux or Windows environment fallback)
        try:
            sinfo: Any = None
            if sys.platform == "win32":
                sinfo = subprocess.STARTUPINFO()
                sinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                startupinfo=sinfo,
                timeout=3,
            )
            if res.returncode == 0:
                gpus = [line.strip() for line in res.stdout.split("\n") if line.strip()]
        except Exception:
            pass

    return gpus


def validate_transcoding_combination(
    container: str,
    vcodec: str,
    acodec: str,
    meta: Optional[Dict[str, Any]] = None,
    audio_track_idx: Optional[Union[int, str]] = None,
) -> Tuple[bool, str]:
    """
    Checks if the selected video and audio codecs are compatible with the target container format.
    Returns (is_valid, error_message).
    """
    container_lower = container.lower().lstrip(".")
    rules = TRANSCODING_COMPATIBILITY_MATRIX.get(container_lower)
    if not rules:
        return True, ""

    check_v = resolve_transcoding_codec(vcodec, "video", meta, audio_track_idx)
    check_a = resolve_transcoding_codec(acodec, "audio", meta, audio_track_idx)

    is_v_compat = not rules.get("video") or check_v in rules["video"]
    is_a_compat = not rules.get("audio") or check_a in rules["audio"]

    if not is_v_compat or not is_a_compat:
        err_msg = ""
        if not is_v_compat and not is_a_compat:
            err_msg = f"Incompatible combination: Both Video Codec ({check_v}) and Audio Codec ({check_a}) are incompatible with the {container_lower.upper()} container. "
        elif not is_v_compat:
            err_msg = f"Incompatible combination: Video Codec ({check_v}) is incompatible with the {container_lower.upper()} container. "
        else:
            err_msg = f"Incompatible combination: Audio Codec ({check_a}) is incompatible with the {container_lower.upper()} container. "
        err_msg += str(rules["notes"])
        return False, err_msg

    return True, ""


def get_input_decoder_args(hw_type: str, meta: Optional[Dict[str, Any]]) -> List[str]:
    """Returns hardware accelerated input decoder arguments if supported."""
    if not meta or hw_type not in ["nvenc", "qsv"]:
        return []
    if not meta.get("video_streams"):
        return []
    in_codec = meta["video_streams"][0].get("codec_name", "")
    if hw_type == "nvenc":
        if in_codec in ["h264", "hevc", "vp9", "av1"]:
            return ["-c:v", f"{in_codec}_cuvid"]
    elif hw_type == "qsv":
        if in_codec in ["h264", "hevc", "vp9", "av1"]:
            return ["-c:v", f"{in_codec}_qsv"]
    return []


def map_codec_and_build_args(
    vcodec: str,
    preset: Optional[str],
    crf: Union[int, str],
    resolution: str,
    hw_accel: str = "auto",
    bitrate: Optional[str] = None,
) -> Tuple[List[str], str, str]:
    """Maps standard codec to hardware codec and returns appropriate arguments, mapped codec, and hardware type."""
    available_hw = get_supported_hw_encoders()
    hw_type = "none"

    if vcodec != "copy" and hw_accel != "none":
        if hw_accel == "auto":
            # Auto preference: nvenc > qsv > amf > mf
            for t in ["nvenc", "qsv", "amf", "mf"]:
                if t in available_hw:
                    hw_type = t
                    break
        else:
            if hw_accel in available_hw:
                hw_type = hw_accel

    mapped_vcodec = vcodec
    if vcodec != "copy" and hw_type != "none":
        if hw_type == "nvenc":
            mapped_vcodec = "h264_nvenc" if vcodec == "libx264" else "hevc_nvenc" if vcodec == "libx265" else vcodec
        elif hw_type == "qsv":
            mapped_vcodec = "h264_qsv" if vcodec == "libx264" else "hevc_qsv" if vcodec == "libx265" else vcodec
        elif hw_type == "amf":
            mapped_vcodec = "h264_amf" if vcodec == "libx264" else "hevc_amf" if vcodec == "libx265" else vcodec
        elif hw_type == "mf":
            mapped_vcodec = "h264_mf" if vcodec == "libx264" else "hevc_mf" if vcodec == "libx265" else vcodec

    args = ["-c:v", mapped_vcodec]
    if mapped_vcodec != "copy":
        if any(x in mapped_vcodec for x in ["h264", "hevc", "vp9", "av1", "x264", "x265"]):
            args += ["-pix_fmt", "yuv420p"]

        if resolution != "original":
            w, h = resolution.split("x")
            args += ["-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"]

        # Quality/Bitrate control
        if bitrate:
            args += ["-b:v", bitrate]
        elif mapped_vcodec in ["h264_nvenc", "hevc_nvenc"]:
            args += ["-rc:v", "vbr", "-cq:v", str(crf), "-b:v", "0"]
        elif mapped_vcodec in ["h264_qsv", "hevc_qsv"]:
            args += ["-global_quality", str(crf)]
        elif mapped_vcodec in ["h264_amf", "hevc_amf"]:
            args += ["-rc:v", "cqp", "-qp_i", str(crf), "-qp_p", str(crf)]
        elif mapped_vcodec in ["h264_mf", "hevc_mf"]:
            if resolution == "3840x2160":
                args += ["-b:v", "15M"]
            elif resolution == "1920x1080":
                args += ["-b:v", "5M"]
            elif resolution == "1280x720":
                args += ["-b:v", "2.5M"]
            else:
                args += ["-b:v", "1.2M"]
        elif "prores" in mapped_vcodec:
            val = int(crf)
            if val <= 10:
                profile = 3  # hq
            elif val <= 22:
                profile = 2  # standard
            elif val <= 35:
                profile = 1  # lt
            else:
                profile = 0  # proxy
            args += ["-profile:v", str(profile)]
        elif mapped_vcodec in ["mpeg4", "libxvid", "libvpx"]:
            val = int(crf)
            qv = max(1, min(31, int(1 + (val / 51.0) * 30.0)))
            args += ["-q:v", str(qv)]
        else:
            args += ["-crf", str(crf)]

        # Preset mapping
        if preset:
            if "nvenc" in mapped_vcodec:
                nv_presets = {
                    "ultrafast": "p1",
                    "superfast": "p2",
                    "veryfast": "p3",
                    "faster": "p3",
                    "fast": "p4",
                    "medium": "p4",
                    "slow": "p5",
                    "slower": "p6",
                    "veryslow": "p7",
                }
                args += ["-preset", nv_presets.get(preset, "p4")]
            elif "qsv" in mapped_vcodec:
                qsv_presets = {
                    "ultrafast": "veryfast",
                    "superfast": "veryfast",
                    "veryfast": "veryfast",
                    "faster": "faster",
                    "fast": "fast",
                    "medium": "balanced",
                    "slow": "quality",
                    "slower": "veryslow",
                    "veryslow": "veryslow",
                }
                args += ["-preset", qsv_presets.get(preset, "balanced")]
            elif "amf" in mapped_vcodec:
                amf_presets = {
                    "ultrafast": "speed",
                    "superfast": "speed",
                    "veryfast": "speed",
                    "faster": "speed",
                    "fast": "speed",
                    "medium": "balanced",
                    "slow": "quality",
                    "slower": "quality",
                    "veryslow": "quality",
                }
                args += ["-preset", amf_presets.get(preset, "balanced")]
            else:
                args += ["-preset", preset]

    return args, mapped_vcodec, hw_type


# Load persisted jobs on startup
load_jobs_from_disk()
