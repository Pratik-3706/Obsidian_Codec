import os
import sys
import uuid
import shutil
import subprocess
import json
import threading
import time
import secrets
import signal
import fnmatch
import zipfile
import re
import webbrowser
from typing import Any, Optional
from flask import Flask, request, jsonify, render_template, send_from_directory, send_file, abort, Response
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


# Add project root and src directory to Python path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from obsidian_codec.src.utils.ffmpeg_utils import (
    ACTIVE_JOBS,
    JOBS_LOCK,
    TEMP_DIR,
    cleanup_temp_dir,
    get_job_status,
    cancel_job,
    probe_file,
    start_conversion_thread,
    generate_thumbnail_grid,
    ensure_temp_dir,
    get_supported_hw_encoders,
    get_detected_gpus,
    validate_transcoding_combination,
    is_safe_path,
    is_safe_output_path,
    save_jobs_to_disk,
    escape_ffmpeg_filter_path,
    map_codec_and_build_args,
    get_input_decoder_args,
)

app = Flask(__name__, template_folder="templates", static_folder="static")

# Enable template auto-reload
app.config["TEMPLATES_AUTO_RELOAD"] = True


def get_or_create_csrf_token() -> str:
    ensure_temp_dir()
    csrf_file = os.path.join(TEMP_DIR, "csrf_secret.txt")
    if os.path.exists(csrf_file):
        try:
            with open(csrf_file, "r", encoding="utf-8") as f:
                token = f.read().strip()
                if token:
                    return token
        except Exception as e:
            print(f"Error reading CSRF secret file: {e}", file=sys.stderr)

    # Generate new one
    token = secrets.token_urlsafe(32)
    try:
        tmp_file = csrf_file + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            f.write(token)
        os.replace(tmp_file, csrf_file)
    except Exception as e:
        print(f"Error writing CSRF secret file: {e}", file=sys.stderr)
    return token


# CSRF protection token
app.config["CSRF_TOKEN"] = get_or_create_csrf_token()


def get_bearer_token() -> str:
    env_token = os.environ.get("OBSIDIAN_BEARER_TOKEN")
    if env_token:
        return env_token
    ensure_temp_dir()
    token_file = os.path.join(TEMP_DIR, "bearer_token.txt")
    if os.path.exists(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                token = f.read().strip()
                if token:
                    return token
        except Exception as e:
            print(f"Error reading Bearer secret file: {e}", file=sys.stderr)

    # Generate new one
    token = secrets.token_urlsafe(32)
    try:
        tmp_file = token_file + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            f.write(token)
        os.replace(tmp_file, token_file)
    except Exception as e:
        print(f"Error writing Bearer secret file: {e}", file=sys.stderr)
    return token


app.config["BEARER_TOKEN"] = get_bearer_token()

# Rate limiter setup
limiter = Limiter(key_func=get_remote_address, app=app, default_limits=["120 per minute"])


@app.before_request
def security_checks() -> None:
    # 1. DNS Rebinding Protection
    host = request.headers.get("Host", "")
    host_name = host.split(":")[0]
    if host_name not in ("127.0.0.1", "localhost"):
        abort(400, description="Invalid Host header (DNS rebinding protection)")

    # Allow index, static/assets, and csrf endpoint without bearer validation
    if request.path in ("/", "/api/csrf") or request.path.startswith("/assets/") or request.path.startswith("/static/"):
        return

    # Check for Authorization header first
    auth_header = request.headers.get("Authorization")
    bearer_valid = False
    if auth_header and auth_header.startswith("Bearer "):
        client_token = auth_header.split(" ", 1)[1].strip()
        bearer_valid = secrets.compare_digest(client_token, app.config["BEARER_TOKEN"])

    if bearer_valid:
        return

    # If Bearer token is invalid/missing, we require CSRF check for state-changing routes (browser client)
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        token = request.headers.get("X-CSRF-Token")
        if not token:
            try:
                json_data = request.get_json(silent=True)
                if json_data:
                    token = json_data.get("csrf_token")
            except Exception:
                pass
            if not token and request.form:
                token = request.form.get("csrf_token")

        if not token or not secrets.compare_digest(token, app.config["CSRF_TOKEN"]):
            abort(403, description="Invalid CSRF token or missing/invalid Bearer token")

    # For GET requests to /api/ (e.g. status, preview), if Bearer is missing, we check if they are from the same browser session.
    elif request.path.startswith("/api/"):
        referer = request.headers.get("Referer", "")
        if referer:
            from urllib.parse import urlparse

            ref_host = urlparse(referer).hostname
            if ref_host in ("127.0.0.1", "localhost"):
                return
        abort(401, description="Unauthorized: Missing or invalid Bearer token")


@app.route("/api/csrf", methods=["GET"])
def get_csrf() -> Any:
    return jsonify({"token": app.config["CSRF_TOKEN"]})


def get_session_dir(session_id: Optional[str]) -> str:
    if not session_id:
        return TEMP_DIR
    safe_id = "".join([c for c in session_id if c.isalnum() or c in "-_"]).strip()
    session_dir = os.path.abspath(os.path.join(TEMP_DIR, f"session_{safe_id}"))
    if not os.path.exists(session_dir):
        try:
            os.makedirs(session_dir)
        except Exception:
            pass
    return session_dir


@app.route("/")
def index() -> Any:
    return render_template("index.html")


@app.route("/assets/<path:filename>")
def serve_assets(filename: str) -> Any:
    return send_from_directory("assets", filename)


@app.route("/api/analyze", methods=["POST"])
def api_analyze() -> Any:
    data = request.json or {}
    filepath_input = data.get("filepath")
    if not filepath_input:
        return jsonify({"error": "No file path provided"}), 400

    # Split by semicolon to support multiple files
    paths = []
    if ";" in filepath_input:
        paths = [p.strip() for p in filepath_input.split(";") if p.strip()]
    else:
        paths = [filepath_input.strip()]

    # Path sandbox checks
    for p in paths:
        if not is_safe_path(p):
            return jsonify({"error": f"Access denied: path is outside sandbox: {p}"}), 403

    # Check if a single directory was provided
    if len(paths) == 1 and os.path.isdir(paths[0]):
        dir_path = paths[0]
        video_extensions = [".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts", ".flv", ".ogg", ".m4v"]
        files = []
        try:
            for filename in os.listdir(dir_path):
                ext = os.path.splitext(filename)[1].lower()
                if ext in video_extensions:
                    abs_file = os.path.abspath(os.path.join(dir_path, filename))
                    if is_safe_path(abs_file):
                        files.append(abs_file)
        except Exception as e:
            return jsonify({"error": f"Failed to read directory: {e}"}), 500

        if not files:
            return jsonify({"error": "No supported video files found in the directory."}), 404

        return jsonify({"is_batch": True, "files": files})

    elif len(paths) > 1:
        invalid_paths = [p for p in paths if not os.path.exists(p) or os.path.isdir(p)]
        if invalid_paths:
            return jsonify({"error": f"Invalid or non-existent files: {', '.join(invalid_paths)}"}), 404
        return jsonify({"is_batch": True, "files": [os.path.abspath(p) for p in paths]})

    else:
        filepath = os.path.abspath(paths[0])
        if not os.path.exists(filepath):
            return jsonify({"error": "File does not exist on disk"}), 404

        info = probe_file(filepath)
        if "error" in info:
            return jsonify(info), 400

        return jsonify(info)


@app.route("/api/hw-encoders", methods=["GET"])
def api_hw_encoders() -> Any:
    return jsonify({"supported": get_supported_hw_encoders(), "gpus": get_detected_gpus()})


@app.route("/api/upload", methods=["POST"])
def api_upload() -> Any:
    ensure_temp_dir()

    session_id = request.form.get("session_id")
    session_dir = get_session_dir(session_id)

    chunk_number = int(request.form.get("resumableChunkNumber", 1))
    total_chunks = int(request.form.get("resumableTotalChunks", 1))
    identifier = request.form.get("resumableIdentifier", "default")
    filename = request.form.get("resumableFilename", "file")

    safe_filename = "".join([c for c in filename if c.isalnum() or c in ".-_ "]).strip()

    chunk_dir = os.path.join(session_dir, f"upload_{identifier}")
    if not os.path.exists(chunk_dir):
        os.makedirs(chunk_dir)

    chunk_file = request.files.get("file")
    if not chunk_file:
        return jsonify({"error": "No chunk file found"}), 400

    chunk_path = os.path.join(chunk_dir, f"{chunk_number}")
    chunk_file.save(chunk_path)

    if len(os.listdir(chunk_dir)) == total_chunks:
        final_path = os.path.join(session_dir, safe_filename)
        with open(final_path, "wb") as f_out:
            for i in range(1, total_chunks + 1):
                chunk_i_path = os.path.join(chunk_dir, str(i))
                if os.path.exists(chunk_i_path):
                    with open(chunk_i_path, "rb") as f_in:
                        f_out.write(f_in.read())

        try:
            shutil.rmtree(chunk_dir)
        except Exception as e:
            print(f"Error cleaning up chunk dir: {e}", file=sys.stderr)

        return jsonify({"status": "completed", "filepath": final_path, "filename": safe_filename})

    return jsonify({"status": "uploading", "chunk": chunk_number})


@app.route("/api/convert", methods=["POST"])
def api_convert() -> Any:
    ensure_temp_dir()
    data = request.json or {}

    session_id = data.get("session_id")
    session_dir = get_session_dir(session_id)

    input_path = data.get("input_path")
    operation = data.get("operation")

    if not input_path or not os.path.exists(input_path) or not is_safe_path(input_path):
        return jsonify({"error": "Input file path is invalid, missing, or outside sandbox"}), 400

    audio_file = data.get("audio_file")
    if audio_file and not is_safe_path(audio_file):
        return jsonify({"error": "Access denied: audio file path is outside sandbox"}), 400

    sub_file = data.get("sub_file")
    if sub_file and not is_safe_path(sub_file):
        return jsonify({"error": "Access denied: subtitle file path is outside sandbox"}), 400

    meta = probe_file(input_path)
    duration = meta.get("duration", 0)

    input_dir = os.path.dirname(input_path)
    base_name, ext = os.path.splitext(os.path.basename(input_path))

    if operation == "convert":
        video_codec = data.get("video_codec", "libx264")
        audio_codec = data.get("audio_codec", "aac")
        out_format = data.get("format", ext.lstrip(".") if ext else "mp4")
        audio_track = data.get("audio_track")
        is_valid, err_msg = validate_transcoding_combination(out_format, video_codec, audio_codec, meta, audio_track)
        if not is_valid:
            return jsonify({"error": err_msg}), 400

    job_id = str(uuid.uuid4())

    custom_output = data.get("output_path")

    if custom_output:
        if not is_safe_output_path(custom_output):
            return jsonify({"error": "Access denied: custom output path is invalid or outside sandbox"}), 400
        out_dir = os.path.dirname(custom_output)
        if out_dir and not os.path.exists(out_dir):
            try:
                os.makedirs(out_dir)
            except Exception:
                pass
        output_path = custom_output
    else:
        # Save in session_dir if input is located in the temp folder tree
        out_dir = session_dir if os.path.abspath(TEMP_DIR) in os.path.abspath(input_path) else input_dir

        # Determine appropriate extension
        if operation == "extract_audio":
            out_ext = "." + data.get("audio_codec", "mp3").replace("libmp3lame", "mp3").replace(
                "libopus", "opus"
            ).replace("libvorbis", "ogg")
            output_path = os.path.join(out_dir, f"{base_name}_extracted{out_ext}")
        elif operation == "extract_subs":
            out_ext = ".srt" if "vtt" not in data.get("sub_format", "srt") else ".vtt"
            output_path = os.path.join(out_dir, f"{base_name}_subs{out_ext}")
        elif operation == "extract_chapters":
            output_path = os.path.join(out_dir, f"{base_name}_chapters.json")
        elif operation == "extract_frames":
            img_format = data.get("image_format", "png")
            img_mode = data.get("image_mode", "single")
            if img_mode == "gif":
                output_path = os.path.join(out_dir, f"{base_name}_animated.gif")
            elif img_mode == "interval":
                # For pattern output
                output_path = os.path.join(out_dir, f"{base_name}_frame_%04d.{img_format}")
            else:
                output_path = os.path.join(out_dir, f"{base_name}_frame.{img_format}")
        elif operation == "thumbnail_grid":
            output_path = os.path.join(out_dir, f"{base_name}_grid.png")
        else:
            out_format = data.get("format", ext.lstrip(".") if ext else "mp4")
            output_path = os.path.join(out_dir, f"{base_name}_obsidian.{out_format}")

    # Check output path safety
    if not is_safe_output_path(output_path):
        return jsonify({"error": "Access denied: output path is invalid or outside sandbox"}), 400

    friendly_encoder = "Standard Pipeline"
    # Build cmd based on operation
    cmd = ["ffmpeg", "-y", "-i", input_path]

    try:
        if operation == "convert":
            video_codec = data.get("video_codec", "libx264")
            audio_codec = data.get("audio_codec", "aac")
            hw_accel = data.get("hw_accel", "auto")
            resolution = data.get("resolution", "original")
            bitrate = data.get("video_bitrate")
            crf = data.get("crf", 23)
            preset = data.get("preset")

            # Map video codec and build args using shared helper
            v_args, mapped_vcodec, hw_type = map_codec_and_build_args(
                vcodec=video_codec, preset=preset, crf=crf, resolution=resolution, hw_accel=hw_accel, bitrate=bitrate
            )

            # Get input decoder arguments using shared helper
            input_dec_args = get_input_decoder_args(hw_type, meta)

            cmd = ["ffmpeg", "-y"] + input_dec_args + ["-i", input_path]
            cmd += v_args

            if audio_codec == "none":
                cmd += ["-an"]
            else:
                cmd += ["-c:a", audio_codec]
                if audio_codec != "copy":
                    audio_bitrate = data.get("audio_bitrate")
                    if audio_bitrate:
                        cmd += ["-b:a", audio_bitrate]

                    channels = data.get("audio_channels")
                    if channels:
                        cmd += ["-ac", str(channels)]

                audio_track = data.get("audio_track")
                if audio_track is not None and audio_track != "":
                    cmd += ["-map", "0:v:0", "-map", f"0:a:{audio_track}"]
                else:
                    cmd += ["-map", "0:v:0?"]
                    if audio_codec != "none":
                        cmd += ["-map", "0:a?"]

            # Subtitles copy settings
            sub_track = data.get("sub_track")
            sub_mode = data.get("sub_mode", "soft")
            if sub_track is not None and sub_track != "" and int(sub_track) != -1:
                sub_idx = int(sub_track)
                if sub_mode == "hard":
                    escaped_path = escape_ffmpeg_filter_path(input_path)
                    sub_vf = f"subtitles='{escaped_path}':si={sub_idx}"
                    # Find if we already have -vf scale
                    # Subtitles filter must run after scaling
                    vf_arg: Optional[str] = sub_vf
                    for i, arg in enumerate(cmd):
                        if arg == "-vf":
                            cmd[i + 1] = f"{cmd[i + 1]},{sub_vf}"
                            vf_arg = None
                            break
                    if vf_arg:
                        cmd += ["-vf", vf_arg]
                else:
                    sub_codec = "mov_text" if output_path.lower().endswith((".mp4", ".m4v", ".mov")) else "copy"
                    cmd += ["-map", f"0:s:{sub_idx}", "-c:s", sub_codec]
            elif sub_track == -1 or sub_track == "-1":
                cmd += ["-sn"]
            else:
                # Copy subtitles by default if present
                sub_codec = "mov_text" if output_path.lower().endswith((".mp4", ".m4v", ".mov")) else "copy"
                cmd += ["-map", "0:s?", "-c:s", sub_codec]

            # Set friendly encoder details
            friendly_encoder = mapped_vcodec
            if "nvenc" in mapped_vcodec:
                friendly_encoder = "NVIDIA NVENC (GPU)"
            elif "qsv" in mapped_vcodec:
                friendly_encoder = "Intel QSV (GPU)"
            elif "amf" in mapped_vcodec:
                friendly_encoder = "AMD AMF (GPU)"
            elif "mf" in mapped_vcodec:
                friendly_encoder = "Windows Media Foundation (GPU)"
            elif mapped_vcodec == "copy":
                friendly_encoder = "Direct Copy (No Re-encoding)"
            else:
                friendly_encoder = f"CPU Software ({mapped_vcodec})"

            if video_codec != "copy" and any(x in mapped_vcodec for x in ["hevc", "h265", "x265"]):
                if output_path.lower().endswith((".mp4", ".m4v", ".mov")):
                    cmd += ["-tag:v", "hvc1"]
            cmd += ["-map_metadata", "0"]
            if output_path.lower().endswith(".m4v"):
                cmd += ["-f", "mp4"]
            cmd.append(output_path)

        elif operation == "extract_audio":
            cmd = ["ffmpeg", "-y", "-i", input_path, "-vn"]
            audio_codec = data.get("audio_codec", "libmp3lame")
            cmd += ["-c:a", audio_codec]

            audio_bitrate = data.get("audio_bitrate")
            if audio_bitrate:
                cmd += ["-b:a", audio_bitrate]

            audio_track = data.get("audio_track")
            if audio_track is not None and audio_track != "":
                cmd += ["-map", f"0:a:{audio_track}"]
            cmd.append(output_path)
            friendly_encoder = (
                f"Audio Extractor ({audio_codec.replace('libmp3lame', 'mp3').replace('libopus', 'opus')})"
            )

        elif operation == "extract_video":
            cmd = ["ffmpeg", "-y", "-i", input_path, "-an"]
            video_codec = data.get("video_codec", "copy")
            cmd += ["-c:v", video_codec]
            if output_path.lower().endswith(".m4v"):
                cmd += ["-f", "mp4"]
            cmd.append(output_path)
            friendly_encoder = f"Video Extractor ({video_codec})"

        elif operation == "extract_subs":
            cmd = ["ffmpeg", "-y", "-i", input_path]
            sub_track = data.get("sub_track", 0)
            cmd += ["-map", f"0:s:{sub_track}"]
            # Ffmpeg infers srt/vtt based on path extension
            cmd.append(output_path)
            friendly_encoder = "Subtitle Extractor"

        elif operation == "extract_chapters":
            # Chapter extraction is done in Python directly!
            # We fetch chapter data and write it.
            chapters = meta.get("chapters", [])
            with JOBS_LOCK:
                ACTIVE_JOBS[job_id] = {
                    "status": "completed",
                    "progress": 100.0,
                    "speed": "N/A",
                    "eta": "00:00",
                    "size": "0 B",
                    "log": ["Chapters written directly in Python."],
                    "process": None,
                    "output_path": output_path,
                    "input_path": None,
                    "error": None,
                    "encoder": "Chapter Extractor",
                    "finished_time": time.time(),
                }
            friendly_encoder = "Chapter Extractor"
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(chapters, f, indent=2)

            # Log output size
            if os.path.exists(output_path):
                sb = os.path.getsize(output_path)
                with JOBS_LOCK:
                    ACTIVE_JOBS[job_id]["size"] = f"{sb} B" if sb < 1024 else f"{sb / 1024:.2f} KB"

            save_jobs_to_disk()
            return jsonify({"job_id": job_id})

        elif operation == "extract_frames":
            img_format = data.get("image_format", "png")
            img_mode = data.get("image_mode", "single")

            if img_mode == "single":
                timestamp = data.get("timestamp", "00:00:01")
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    str(timestamp),
                    "-i",
                    input_path,
                    "-vframes",
                    "1",
                    "-q:v",
                    "2",
                    output_path,
                ]
            elif img_mode == "interval":
                fps = data.get("interval_fps", "1")
                cmd = ["ffmpeg", "-y", "-i", input_path, "-vf", f"fps={fps}", "-q:v", "2", output_path]
            elif img_mode == "gif":
                start = data.get("timestamp", "00:00:00")
                gif_duration = data.get("duration", "5")
                fps = data.get("interval_fps", "12")
                # Lanczos scaling palette logic for high-quality GIFs
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    str(start),
                    "-t",
                    str(gif_duration),
                    "-i",
                    input_path,
                    "-filter_complex",
                    f"[0:v] fps={fps},scale=480:-1:flags=lanczos,split [a][b];[a] palettegen [p];[b][p] paletteuse",
                    output_path,
                ]
            friendly_encoder = f"Frames Extractor ({img_mode.upper()})"

        elif operation == "thumbnail_grid":
            rows = int(data.get("grid_rows", 4))
            cols = int(data.get("grid_cols", 4))

            # Start background thread to run thumbnail generation
            with JOBS_LOCK:
                ACTIVE_JOBS[job_id] = {
                    "status": "running",
                    "progress": 20.0,
                    "speed": "N/A",
                    "eta": "Generating Grid",
                    "size": "0 B",
                    "log": ["Generating thumbnail sheet..."],
                    "process": None,
                    "output_path": output_path,
                    "input_path": None,
                    "error": None,
                }
            save_jobs_to_disk()

            def run_grid_generation():
                try:
                    generate_thumbnail_grid(input_path, output_path, rows, cols, duration)
                    with JOBS_LOCK:
                        if ACTIVE_JOBS[job_id]["status"] != "cancelled":
                            ACTIVE_JOBS[job_id].update(
                                {
                                    "status": "completed",
                                    "progress": 100.0,
                                    "eta": "00:00",
                                    "log": ["Thumbnail grid generated successfully."],
                                    "finished_time": time.time(),
                                }
                            )
                            if os.path.exists(output_path):
                                sb = os.path.getsize(output_path)
                                ACTIVE_JOBS[job_id]["size"] = f"{sb / 1024 / 1024:.2f} MB"
                    save_jobs_to_disk()
                except Exception as e:
                    with JOBS_LOCK:
                        if ACTIVE_JOBS[job_id]["status"] != "cancelled":
                            ACTIVE_JOBS[job_id].update(
                                {"status": "failed", "error": str(e), "finished_time": time.time()}
                            )
                    save_jobs_to_disk()

            t_grid = threading.Thread(target=run_grid_generation, daemon=True)
            t_grid.start()
            return jsonify({"job_id": job_id})

        elif operation == "embed_audio":
            audio_file = data.get("audio_file")
            if not audio_file or not os.path.exists(audio_file):
                return jsonify({"error": "Audio file does not exist"}), 400

            cmd = ["ffmpeg", "-y", "-i", input_path, "-i", audio_file]
            embed_mode = data.get("embed_mode", "replace")

            if embed_mode == "replace":
                cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac"]
            else:  # add
                cmd += ["-map", "0:v:0", "-map", "0:a?", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac"]

            if output_path.lower().endswith(".m4v"):
                cmd += ["-f", "mp4"]
            cmd.append(output_path)
            friendly_encoder = f"Audio Muxer ({embed_mode.upper()})"

        elif operation == "embed_subs":
            sub_file = data.get("sub_file")
            if not sub_file or not os.path.exists(sub_file):
                return jsonify({"error": "Subtitle file does not exist"}), 400

            sub_mode = data.get("sub_mode", "soft")
            if sub_mode == "hard":
                cmd = ["ffmpeg", "-y", "-i", input_path]
                escaped_sub = escape_ffmpeg_filter_path(sub_file)
                cmd += ["-vf", f"subtitles='{escaped_sub}'", "-c:a", "copy"]
                if output_path.lower().endswith(".m4v"):
                    cmd += ["-f", "mp4"]
                cmd.append(output_path)
            else:  # soft
                cmd = ["ffmpeg", "-y", "-i", input_path, "-i", sub_file]
                sub_codec = "mov_text" if output_path.lower().endswith((".mp4", ".m4v", ".mov")) else "srt"
                cmd += [
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a?",
                    "-map",
                    "1:s:0",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "copy",
                    "-c:s",
                    sub_codec,
                ]
                if output_path.lower().endswith(".m4v"):
                    cmd += ["-f", "mp4"]
                cmd.append(output_path)
            friendly_encoder = f"Subtitle Muxer ({sub_mode.upper()})"

        else:
            return jsonify({"error": f"Unknown operation: {operation}"}), 400

    except Exception as e:
        return jsonify({"error": f"Failed to build command: {e}"}), 400

    # Start the actual conversion thread
    # If the input file is in TEMP_DIR, it will be deleted automatically on complete/fail
    t = start_conversion_thread(job_id, cmd, duration, output_path, input_path)
    if t is None:
        return jsonify({"error": "Too many concurrent conversions. Max limit is 2. Please try again later."}), 429

    # Store processor details in job dict
    with JOBS_LOCK:
        if job_id in ACTIVE_JOBS:
            ACTIVE_JOBS[job_id]["encoder"] = friendly_encoder
            ACTIVE_JOBS[job_id]["sub_track"] = data.get("sub_track")
            ACTIVE_JOBS[job_id]["sub_file"] = data.get("sub_file")

    save_jobs_to_disk()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>", methods=["GET"])
def api_status(job_id: str) -> Any:
    status = get_job_status(job_id)
    if not status:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(status)


@app.route("/api/job-frame/<job_id>", methods=["GET"])
def api_job_frame(job_id: str) -> Any:
    with JOBS_LOCK:
        job = ACTIVE_JOBS.get(job_id)
        if not job:
            return "Job not found", 404

        input_path = job.get("input_path")
        out_time = job.get("out_time", 0.0)
        sub_track = job.get("sub_track")
        sub_file = job.get("sub_file")

    if not input_path or not os.path.exists(input_path):
        return "Input file not found", 404

    # Build filter graph to overlay subtitles if added
    vf_filters = []
    if sub_track is not None and int(sub_track) != -1:
        escaped_input = input_path.replace("\\", "/").replace(":", "\\:")
        vf_filters.append(f"subtitles='{escaped_input}':si={int(sub_track)}")
    elif sub_file and os.path.exists(sub_file):
        escaped_sub = sub_file.replace("\\", "/").replace(":", "\\:")
        vf_filters.append(f"subtitles='{escaped_sub}'")

    cmd = ["ffmpeg", "-y", "-ss", f"{out_time:.3f}", "-copyts", "-i", input_path]
    if vf_filters:
        cmd += ["-vf", ",".join(vf_filters)]

    cmd += ["-vframes", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]

    try:
        startupinfo: Any = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo)
        if res.returncode == 0:
            return Response(res.stdout, mimetype="image/jpeg")
        else:
            return "Failed to extract frame", 500
    except Exception as e:
        return str(e), 500


@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id: str) -> Any:
    success, msg = cancel_job(job_id)
    if not success:
        return jsonify({"error": msg}), 400
    return jsonify({"status": "cancelled", "message": msg})


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder() -> Any:
    data = request.json or {}
    filepath = data.get("filepath")
    if not filepath:
        return jsonify({"error": "Invalid file path"}), 400

    # Handle filename patterns (e.g. %04d)
    if "%" in os.path.basename(filepath):
        dirpath = os.path.dirname(filepath)
        if not is_safe_path(dirpath):
            return jsonify({"error": "Access denied: path is outside sandbox"}), 403
        if os.path.exists(dirpath):
            try:
                if sys.platform == "win32":
                    subprocess.run(["explorer.exe", os.path.normpath(dirpath)])
                    return jsonify({"success": True})
                return jsonify({"error": "Not supported on this platform"}), 400
            except Exception as e:
                return jsonify({"error": str(e)}), 500
        return jsonify({"error": "Directory does not exist"}), 400

    if not is_safe_path(filepath):
        return jsonify({"error": "Access denied: path is outside sandbox"}), 403

    if not os.path.exists(filepath):
        return jsonify({"error": "Invalid file path"}), 400

    try:
        # Runs Explorer and highlights the file
        filepath_norm = os.path.normpath(filepath)
        if sys.platform == "win32":
            subprocess.run(["explorer.exe", "/select,", filepath_norm])
            return jsonify({"success": True})
        return jsonify({"error": "Not supported on this platform"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/download/<session_id>/<path:filename>", methods=["GET"])
def api_download(session_id: str, filename: str) -> Any:
    session_dir = get_session_dir(session_id)

    # Check if this is a pattern representation (e.g., %04d)
    if "%" in filename:
        glob_pattern = re.sub(r"%[0-9]*d", "*", filename)
        if "*" not in glob_pattern:
            glob_pattern = filename.replace("%", "*")

        matching_files = []
        for f in os.listdir(session_dir):
            if fnmatch.fnmatch(f, glob_pattern):
                matching_files.append(os.path.join(session_dir, f))

        if not matching_files:
            return "No matching files found", 404

        zip_name = filename.replace("%04d", "frames").replace("%d", "frames") + ".zip"
        zip_path = os.path.join(session_dir, zip_name)

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for file in matching_files:
                zipf.write(file, os.path.basename(file))

        response = send_file(zip_path, as_attachment=True, download_name=zip_name)

        @response.call_on_close
        def cleanup_zip_and_files() -> None:
            try:
                if os.path.exists(zip_path):
                    os.unlink(zip_path)
                for file in matching_files:
                    if os.path.exists(file):
                        os.unlink(file)
            except Exception as e:
                print(f"Failed to delete download zip/files on close: {e}", file=sys.stderr)

        return response

    file_path = os.path.abspath(os.path.join(session_dir, filename))
    if not file_path.startswith(session_dir):
        return "Access denied", 403
    if not os.path.exists(file_path):
        return "File not found", 404

    response = send_file(file_path, as_attachment=True)

    @response.call_on_close
    def cleanup_file() -> None:
        try:
            if os.path.exists(file_path):
                os.unlink(file_path)
        except Exception as e:
            print(f"Failed to delete download file on close: {e}", file=sys.stderr)

    return response


@app.route("/api/preview", methods=["GET"])
def api_preview() -> Any:
    filepath = request.args.get("filepath")
    if not filepath:
        abort(400, description="filepath is required")
    if not is_safe_path(filepath):
        abort(403, description="Access denied: file outside sandbox")
    if not os.path.exists(filepath):
        return "File not found", 404
    try:
        # Enforce absolute path and enable range requests (conditional=True)
        return send_file(os.path.abspath(filepath), conditional=True)
    except Exception as e:
        return str(e), 500


@app.route("/api/cleanup-session", methods=["POST"])
def api_cleanup_session() -> Any:
    session_id = request.form.get("session_id")
    if not session_id:
        try:
            json_data = request.get_json(silent=True)
            if json_data:
                session_id = json_data.get("session_id")
        except Exception:
            pass
    if session_id:
        safe_id = "".join([c for c in session_id if c.isalnum() or c in "-_"]).strip()
        session_dir = os.path.abspath(os.path.join(TEMP_DIR, f"session_{safe_id}"))
        if os.path.exists(session_dir):
            try:
                shutil.rmtree(session_dir)
            except Exception as e:
                print(f"Failed to delete session dir: {e}", file=sys.stderr)
    return jsonify({"success": True, "message": "Session cleanup successful."})


@app.route("/api/quit", methods=["POST"])
def api_quit() -> Any:
    cleanup_temp_dir()

    def shutdown() -> None:
        time.sleep(0.5)
        print("Shutting down Obsidian Codec Engine backend process gracefully...")
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=shutdown, daemon=True).start()
    return jsonify({"success": True, "message": "Server shutting down."})


def main() -> None:
    ensure_temp_dir()
    cleanup_temp_dir()  # Initial cleanup on launch

    use_https = os.environ.get("OBSIDIAN_USE_HTTPS", "0") == "1"
    ssl_context = None
    if use_https:
        try:
            import cryptography  # noqa: F401

            ssl_context = "adhoc"
        except ImportError:
            print("WARNING: 'cryptography' library is not installed. Falling back to HTTP.", file=sys.stderr)
            use_https = False

    protocol = "https" if use_https else "http"

    token = get_bearer_token()
    print("===================================================")
    print(f"  Obsidian_Codec WebUI starting on {protocol}://127.0.0.1:5000")
    print(f"  Bearer Token (survives restart): {token}")
    print("  (Pass 'Authorization: Bearer <Token>' for script APIs)")
    print("===================================================")

    def open_browser() -> None:
        time.sleep(1.0)
        webbrowser.open(f"{protocol}://127.0.0.1:5000")

    threading.Thread(target=open_browser, daemon=True).start()

    # Run the server
    DEBUG = os.environ.get("OBSIDIAN_CODEC_DEBUG", "0") == "1"
    app.run(host="127.0.0.1", port=5000, debug=DEBUG, use_reloader=False, ssl_context=ssl_context)


if __name__ == "__main__":
    main()
