import os
import sys
import argparse
import uuid
import time
import threading
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, SpinnerColumn
from rich.prompt import Prompt, Confirm

# Add project root and src directory to Python path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from obsidian_codec.src.utils.ffmpeg_utils import (
    probe_file,
    run_ffmpeg_subprocess,
    ACTIVE_JOBS,
    JOBS_LOCK,
    generate_thumbnail_grid,
    get_supported_hw_encoders
)

# Enforce UTF-8 stdout/stderr on Windows to render unicode banners
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

console = Console()


def map_codec_and_build_args(vcodec, preset, crf, resolution):
    """Maps standard codec to hardware codec and returns appropriate arguments."""
    available_hw = get_supported_hw_encoders()
    hw_type = "none"
    
    # Auto preference: nvenc > qsv > amf > mf
    for t in ["nvenc", "qsv", "amf", "mf"]:
        if t in available_hw:
            hw_type = t
            break
            
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
            
        # Quality control
        if mapped_vcodec in ["h264_nvenc", "hevc_nvenc"]:
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
        elif mapped_vcodec in ["prores", "prores_ks"]:
            val = int(crf)
            if val <= 10:
                profile = 3 # hq
            elif val <= 22:
                profile = 2 # standard
            elif val <= 35:
                profile = 1 # lt
            else:
                profile = 0 # proxy
            args += ["-profile:v", str(profile)]
        else:
            args += ["-crf", str(crf)]
            
        # Preset mapping
        if preset:
            if "nvenc" in mapped_vcodec:
                nv_presets = {
                    "ultrafast": "p1", "superfast": "p2", "veryfast": "p3",
                    "faster": "p3", "fast": "p4", "medium": "p4",
                    "slow": "p5", "slower": "p6", "veryslow": "p7"
                }
                args += ["-preset", nv_presets.get(preset, "p4")]
            elif "qsv" in mapped_vcodec:
                qsv_presets = {
                    "ultrafast": "7", "superfast": "6", "veryfast": "5",
                    "faster": "5", "fast": "4", "medium": "4",
                    "slow": "3", "slower": "2", "veryslow": "1"
                }
                args += ["-preset", qsv_presets.get(preset, "4")]
            elif "amf" in mapped_vcodec:
                amf_presets = {
                    "ultrafast": "speed", "superfast": "speed", "veryfast": "speed",
                    "faster": "speed", "fast": "speed", "medium": "balanced",
                    "slow": "quality", "slower": "quality", "veryslow": "quality"
                }
                args += ["-preset", amf_presets.get(preset, "balanced")]
            else:
                args += ["-preset", preset]
                
    return mapped_vcodec, args


def print_banner():
    banner = """
 ██████╗ ██████╗ ███████╗██╗██████╗ ██╗ █████╗ ███╗   ██╗     ██████╗ ██████╗ ██████╗ ███████╗ ██████╗
██╔═══██╗██╔══██╗██╔════╝██║██╔══██╗██║██╔══██╗████╗  ██║    ██╔════╝██╔═══██╗██╔══██╗██╔════╝██╔════╝
██║   ██║██████╔╝███████╗██║██║  ██║██║███████║██╔██╗ ██║    ██║     ██║   ██║██║  ██║█████╗  ██║     
██║   ██║██╔══██╗╚════██║██║██║  ██║██║██╔══██║██║╚██╗██║    ██║     ██║   ██║██║  ██║██╔══╝  ██║     
╚██████╔╝██████╔╝███████║██║██████╔╝██║██║  ██║██║ ╚████║    ╚██████╗╚██████╔╝██████╔╝███████╗╚██████╗
 ╚═════╝ ╚═════╝ ╚══════╝╚═╝╚═════╝ ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝     ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝ ╚═════╝
                           [bold cyan]UNIVERSAL VIDEO & MEDIA SUITE[/bold cyan]
    """
    console.print(Panel.fit(banner, border_style="cyan", title="v1.0.0 PRO", subtitle="MIT License"))

def display_file_probe(file_path):
    """Displays ffprobe metadata in a rich layout."""
    info = probe_file(file_path)
    if "error" in info:
        console.print(f"[bold red]Error probing file:[/bold red] {info['error']}")
        return None

    # General Format Table
    fmt_table = Table(title="Container Metadata", title_style="bold purple", border_style="dim")
    fmt_table.add_column("Property", style="cyan")
    fmt_table.add_column("Value", style="green")
    
    duration_secs = info['duration']
    mins, secs = divmod(int(duration_secs), 60)
    hrs, mins = divmod(mins, 60)
    duration_str = f"{hrs:02d}:{mins:02d}:{secs:02d}"
    
    fmt_table.add_row("Filename", info['filename'])
    fmt_table.add_row("File Path", info['filepath'])
    fmt_table.add_row("Duration", duration_str)
    fmt_table.add_row("Size", f"{info['size'] / (1024*1024):.2f} MB")
    fmt_table.add_row("Bitrate", f"{info['bitrate'] / 1000:.1f} kbps" if info['bitrate'] else "N/A")
    fmt_table.add_row("Format", info['format_long_name'])
    
    console.print(fmt_table)
    console.print()

    # Streams Table
    streams_table = Table(title="Available Media Tracks (Streams)", title_style="bold purple", border_style="dim")
    streams_table.add_column("Index", style="yellow")
    streams_table.add_column("Type", style="cyan")
    streams_table.add_column("Codec", style="green")
    streams_table.add_column("Details", style="white")

    for s in info['video_streams']:
        streams_table.add_row(
            str(s['index']), 
            "VIDEO", 
            s['codec_name'].upper(), 
            f"{s['width']}x{s['height']} | {s['r_frame_rate']} fps"
        )
    for s in info['audio_streams']:
        streams_table.add_row(
            str(s['index']), 
            "AUDIO", 
            s['codec_name'].upper(), 
            f"{s['channel_layout']} ({s['channels']} ch) | {s['sample_rate']}Hz"
        )
    for s in info['subtitle_streams']:
        lang = s['tags'].get('language', 'unknown')
        title = s['tags'].get('title', '')
        detail = f"Language: {lang}"
        if title: detail += f" | Title: {title}"
        streams_table.add_row(str(s['index']), "SUBTITLE", s['codec_name'].upper(), detail)

    console.print(streams_table)
    
    if info['chapters']:
        console.print()
        ch_table = Table(title="Chapters", title_style="bold purple", border_style="dim")
        ch_table.add_column("ID", style="yellow")
        ch_table.add_column("Title", style="cyan")
        ch_table.add_column("Range (s)", style="white")
        for ch in info['chapters']:
            ch_table.add_row(str(ch['id']), ch['title'], f"{ch['start']:.2f}s - {ch['end']:.2f}s")
        console.print(ch_table)
        
    return info

def run_ffmpeg_with_cli_progress(cmd, duration, output_path):
    """Runs FFmpeg and renders progress bar inside terminal."""
    job_id = str(uuid.uuid4())
    
    with JOBS_LOCK:
        ACTIVE_JOBS[job_id] = {
            'status': 'pending',
            'progress': 0.0,
            'speed': '0.0x',
            'eta': 'Pending',
            'size': '0 B',
            'log': [],
            'process': None,
            'output_path': output_path,
            'input_path': None,
            'error': None
        }

    # Start the standard execution thread
    t = threading.Thread(
        target=run_ffmpeg_subprocess,
        args=(job_id, cmd, duration, output_path, None),
        daemon=True
    )
    t.start()
    
    # Track progress inside CLI
    console.print()
    console.print("[cyan]Initializing FFmpeg Pipeline...[/cyan]")
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}[/bold cyan]"),
        BarColumn(bar_width=40, complete_style="green", finished_style="bold green"),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("[cyan]Speed: {task.fields[speed]}[/cyan]"),
        TextColumn("[yellow]Size: {task.fields[size]}[/yellow]"),
        TimeRemainingColumn(),
        console=console
    ) as progress:
        
        task = progress.add_task("Processing", total=100, speed="0.0x", size="0 B")
        
        last_log_idx = 0
        
        while True:
            time.sleep(0.3)
            with JOBS_LOCK:
                job = ACTIVE_JOBS.get(job_id)
                
            if not job:
                break
                
            # Print new log lines to preserve context
            logs = job.get('log', [])
            if len(logs) > last_log_idx:
                for i in range(last_log_idx, len(logs)):
                    # Clear line and print log if desired, or skip to avoid cluttering progress bars
                    pass
                last_log_idx = len(logs)
                
            progress.update(
                task, 
                completed=job['progress'], 
                speed=job['speed'], 
                size=job['size']
            )
            
            if job['status'] == 'completed':
                progress.update(task, completed=100)
                console.print()
                console.print(Panel(f"[bold green]✔ Successfully Completed![/bold green]\nOutput saved: [cyan]{output_path}[/cyan]\nFinal size: [yellow]{job['size']}[/yellow]", border_style="green"))
                break
            elif job['status'] == 'failed':
                console.print()
                console.print(Panel(f"[bold red]✘ Encoding Failed![/bold red]\n{job['error']}", border_style="red"))
                break
            elif job['status'] == 'cancelled':
                console.print()
                console.print(Panel("[bold yellow]⚠ Job was cancelled by user.[/bold yellow]", border_style="yellow"))
                break

def run_interactive_wizard():
    print_banner()
    
    # Input path
    while True:
        filepath = Prompt.ask("[bold white]Enter source media file path[/bold white]").strip('"\'')
        if os.path.exists(filepath):
            break
        console.print(f"[bold red]File not found:[/bold red] {filepath}. Please try again.", style="red")
        
    info = display_file_probe(filepath)
    if not info:
        return

    # Choose operation
    console.print()
    console.print("[bold cyan]Choose Media Operation:[/bold cyan]")
    console.print("  [yellow]1.[/yellow] Convert / Compress Media")
    console.print("  [yellow]2.[/yellow] Extract Audio track")
    console.print("  [yellow]3.[/yellow] Extract Video track (Mute)")
    console.print("  [yellow]4.[/yellow] Extract Subtitle track")
    console.print("  [yellow]5.[/yellow] Extract Chapter metadata")
    console.print("  [yellow]6.[/yellow] Extract Still Frames / GIF")
    console.print("  [yellow]7.[/yellow] Generate Contact Sheet (Thumbnail Grid)")
    console.print("  [yellow]8.[/yellow] Embed Subtitles or Audio file")
    
    op_choice = Prompt.ask("Select operation number", choices=["1", "2", "3", "4", "5", "6", "7", "8"], default="1")
    
    # Default outputs
    input_dir = os.path.dirname(filepath)
    base_name, ext = os.path.splitext(os.path.basename(filepath))
    
    if op_choice == "1": # Convert
        container = Prompt.ask("Select output container", choices=["mp4", "mkv", "webm", "avi", "mov"], default="mp4")
        vcodec = Prompt.ask("Select video codec", choices=["libx264", "libx265", "libvpx-vp9", "libaom-av1", "copy"], default="libx264")
        resolution = Prompt.ask("Select target resolution", choices=["original", "3840x2160", "1920x1080", "1280x720", "854x480"], default="original")
        preset = Prompt.ask("Select encoding preset", choices=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"], default="medium")
        crf = Prompt.ask("Enter CRF value (0-51, lower is better quality)", default="23")
        
        acodec = Prompt.ask("Select audio codec", choices=["aac", "libmp3lame", "libopus", "flac", "copy", "none"], default="aac")
        
        # Build command
        cmd = ["ffmpeg", "-y", "-i", filepath]
        mapped_vcodec, v_args = map_codec_and_build_args(vcodec, preset, crf, resolution)
        cmd += v_args
        
        if mapped_vcodec != vcodec:
            console.print(f"[bold green]GPU Acceleration Active:[/bold green] Auto-switched [cyan]{vcodec}[/cyan] to [cyan]{mapped_vcodec}[/cyan]!")
            
        if acodec == "none":
            cmd += ["-an"]
        else:
            cmd += ["-c:a", acodec]
            
        cmd += ["-map_metadata", "0"]
        
        out_path = os.path.join(input_dir, f"{base_name}_obsidian.{container}")
        cmd.append(out_path)
        
        run_ffmpeg_with_cli_progress(cmd, info['duration'], out_path)
        
    elif op_choice == "2": # Extract Audio
        acodec = Prompt.ask("Select audio codec", choices=["libmp3lame", "aac", "flac", "pcm_s16le", "libopus"], default="libmp3lame")
        bitrate = Prompt.ask("Select audio bitrate", choices=["320k", "256k", "192k", "128k"], default="320k")
        
        out_ext = "." + acodec.replace("libmp3lame", "mp3").replace("pcm_s16le", "wav").replace("libopus", "opus")
        out_path = os.path.join(input_dir, f"{base_name}_extracted{out_ext}")
        
        cmd = ["ffmpeg", "-y", "-i", filepath, "-vn", "-c:a", acodec, "-b:a", bitrate, out_path]
        run_ffmpeg_with_cli_progress(cmd, info['duration'], out_path)
        
    elif op_choice == "3": # Extract Video Only
        vcodec = Prompt.ask("Select video codec", choices=["copy", "libx264", "libx265"], default="copy")
        out_path = os.path.join(input_dir, f"{base_name}_video{ext}")
        cmd = ["ffmpeg", "-y", "-i", filepath, "-an", "-c:v", vcodec, out_path]
        run_ffmpeg_with_cli_progress(cmd, info['duration'], out_path)
        
    elif op_choice == "4": # Extract Subtitles
        if not info['subtitle_streams']:
            console.print("[bold red]No subtitles found in this file![/bold red]")
            return
            
        track_opts = [str(i) for i, s in enumerate(info['subtitle_streams'])]
        sub_track = Prompt.ask("Select subtitle track index to extract", choices=track_opts, default="0")
        fmt = Prompt.ask("Select subtitle output format", choices=["srt", "vtt"], default="srt")
        
        out_path = os.path.join(input_dir, f"{base_name}_subs.{fmt}")
        cmd = ["ffmpeg", "-y", "-i", filepath, "-map", f"0:s:{sub_track}", out_path]
        run_ffmpeg_with_cli_progress(cmd, info['duration'], out_path)
        
    elif op_choice == "5": # Extract Chapters
        out_path = os.path.join(input_dir, f"{base_name}_chapters.json")
        console.print(f"[cyan]Writing chapters to: {out_path}...[/cyan]")
        import json
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(info['chapters'], f, indent=2)
        console.print("[bold green]✔ Chapters extracted successfully![/bold green]")
        
    elif op_choice == "6": # Extract Frames
        mode = Prompt.ask("Select extraction mode", choices=["single", "interval", "gif"], default="single")
        
        if mode == "single":
            timestamp = Prompt.ask("Enter timestamp", default="00:00:01")
            fmt = Prompt.ask("Enter image format", choices=["png", "jpg"], default="png")
            out_path = os.path.join(input_dir, f"{base_name}_frame.{fmt}")
            cmd = ["ffmpeg", "-y", "-ss", timestamp, "-i", filepath, "-vframes", "1", "-q:v", "2", out_path]
            run_ffmpeg_with_cli_progress(cmd, 1.0, out_path)
            
        elif mode == "interval":
            fps = Prompt.ask("Extract 1 frame every N seconds (e.g. 1, 5, 10)", default="1")
            fmt = Prompt.ask("Enter image format", choices=["png", "jpg"], default="png")
            out_path = os.path.join(input_dir, f"{base_name}_frame_%04d.{fmt}")
            cmd = ["ffmpeg", "-y", "-i", filepath, "-vf", f"fps=1/{fps}", "-q:v", "2", out_path]
            run_ffmpeg_with_cli_progress(cmd, info['duration'], out_path.replace("%04d", "0001"))
            
        elif mode == "gif":
            start = Prompt.ask("Enter start timestamp", default="00:00:00")
            dur = Prompt.ask("Enter duration (seconds)", default="5")
            fps = Prompt.ask("Enter GIF frame rate", default="12")
            out_path = os.path.join(input_dir, f"{base_name}_clip.gif")
            cmd = [
                "ffmpeg", "-y", "-ss", start, "-t", dur, "-i", filepath,
                "-filter_complex", f"[0:v] fps={fps},scale=480:-1:flags=lanczos,split [a][b];[a] palettegen [p];[b][p] paletteuse",
                out_path
            ]
            run_ffmpeg_with_cli_progress(cmd, float(dur), out_path)
            
    elif op_choice == "7": # Thumbnail Grid
        rows = int(Prompt.ask("Enter grid rows", default="4"))
        cols = int(Prompt.ask("Enter grid columns", default="4"))
        out_path = os.path.join(input_dir, f"{base_name}_grid.png")
        
        console.print("[cyan]Generating thumbnail sheet... This may take a moment.[/cyan]")
        try:
            generate_thumbnail_grid(filepath, out_path, rows, cols, info['duration'])
            console.print(Panel(f"[bold green]✔ Successfully Generated![/bold green]\nThumbnail sheet: [cyan]{out_path}[/cyan]", border_style="green"))
        except Exception as e:
            console.print(f"[bold red]Failed to generate thumbnail grid:[/bold red] {e}")
            
    elif op_choice == "8": # Embed Subtitles or Audio
        embed_choice = Prompt.ask("Select integration type", choices=["audio", "subtitle"], default="subtitle")
        
        if embed_choice == "audio":
            audio_path = Prompt.ask("Enter external audio file path").strip('"\'')
            if not os.path.exists(audio_path):
                console.print(f"[bold red]File not found:[/bold red] {audio_path}")
                return
            mode = Prompt.ask("Select Mux mode", choices=["replace", "add"], default="replace")
            out_path = os.path.join(input_dir, f"{base_name}_muxed{ext}")
            
            cmd = ["ffmpeg", "-y", "-i", filepath, "-i", audio_path]
            if mode == "replace":
                cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac"]
            else:
                cmd += ["-map", "0:v:0", "-map", "0:a?", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac"]
            cmd.append(out_path)
            run_ffmpeg_with_cli_progress(cmd, info['duration'], out_path)
            
        else: # Subtitle
            sub_path = Prompt.ask("Enter external subtitle file path (.srt, .vtt)").strip('"\'')
            if not os.path.exists(sub_path):
                console.print(f"[bold red]File not found:[/bold red] {sub_path}")
                return
            mode = Prompt.ask("Select Mux mode", choices=["soft", "hard"], default="soft")
            
            if mode == "soft":
                out_path = os.path.join(input_dir, f"{base_name}_subbed.mkv") # mkv handles soft subs easily
                cmd = ["ffmpeg", "-y", "-i", filepath, "-i", sub_path, "-map", "0:v:0", "-map", "0:a?", "-map", "1:s:0", "-c:v", "copy", "-c:a", "copy", "-c:s", "srt", out_path]
                run_ffmpeg_with_cli_progress(cmd, info['duration'], out_path)
            else:
                out_path = os.path.join(input_dir, f"{base_name}_burned{ext}")
                escaped_sub = sub_path.replace("\\", "/").replace(":", "\\:")
                cmd = ["ffmpeg", "-y", "-i", filepath, "-vf", f"subtitles='{escaped_sub}'", "-c:a", "copy", out_path]
                run_ffmpeg_with_cli_progress(cmd, info['duration'], out_path)

def main():
    parser = argparse.ArgumentParser(description="Obsidian_Codec: Universal Video & Media Converter CLI")
    parser.add_argument("-i", "--input", help="Source media file path")
    parser.add_argument("-o", "--output", help="Output destination file path")
    parser.add_argument("--probe", action="store_true", help="Probe source file and print metadata streams")
    parser.add_argument("-interactive", "--interactive", action="store_true", help="Launch terminal wizard interactive UI")
    
    # Parsing other simple flags to support CLI arguments
    parser.add_argument("-c:v", "--vcodec", help="Video codec")
    parser.add_argument("-c:a", "--acodec", help="Audio codec")
    parser.add_argument("--crf", help="CRF value (0-51)")
    parser.add_argument("--preset", help="Encoding preset")
    parser.add_argument("--extract-audio", action="store_true", help="Extract audio track")
    parser.add_argument("--extract-video", action="store_true", help="Extract video track (mute)")
    parser.add_argument("--extract-subs", action="store_true", help="Extract subtitle track")
    parser.add_argument("--sub-track", type=int, help="Index of subtitle track")
    parser.add_argument("--sub-mode", choices=["soft", "hard"], default="soft", help="Mux mode for subtitles (soft or hard)")
    parser.add_argument("--audio-track", type=int, help="Index of audio track")
    
    args = parser.parse_args()
    
    if len(sys.argv) == 1 or args.interactive:
        run_interactive_wizard()
        return
        
    if not args.input:
        console.print("[bold red]Error:[/bold red] Input file (-i/--input) is required unless in interactive mode.", style="red")
        sys.exit(1)
        
    if not os.path.exists(args.input):
        console.print(f"[bold red]Error:[/bold red] Input file not found: {args.input}", style="red")
        sys.exit(1)
        
    if args.probe:
        print_banner()
        display_file_probe(args.input)
        return
        
    # Programmatic CLI triggers
    meta = probe_file(args.input)
    duration = meta.get("duration", 0)
    
    input_dir = os.path.dirname(args.input)
    base_name, ext = os.path.splitext(os.path.basename(args.input))
    
    output_path = args.output
    
    if args.extract_audio:
        codec = args.acodec or "libmp3lame"
        out_ext = "." + codec.replace("libmp3lame", "mp3").replace("pcm_s16le", "wav").replace("libopus", "opus")
        if not output_path:
            output_path = os.path.join(input_dir, f"{base_name}_extracted{out_ext}")
        cmd = ["ffmpeg", "-y", "-i", args.input, "-vn", "-c:a", codec]
        if args.audio_track is not None:
            cmd += ["-map", f"0:a:{args.audio_track}"]
        cmd.append(output_path)
        run_ffmpeg_with_cli_progress(cmd, duration, output_path)
        
    elif args.extract_video:
        codec = args.vcodec or "copy"
        if not output_path:
            output_path = os.path.join(input_dir, f"{base_name}_video{ext}")
        cmd = ["ffmpeg", "-y", "-i", args.input, "-an", "-c:v", codec, output_path]
        run_ffmpeg_with_cli_progress(cmd, duration, output_path)
        
    elif args.extract_subs:
        track = args.sub_track if args.sub_track is not None else 0
        if not output_path:
            output_path = os.path.join(input_dir, f"{base_name}_subs.srt")
        cmd = ["ffmpeg", "-y", "-i", args.input, "-map", f"0:s:{track}", output_path]
        run_ffmpeg_with_cli_progress(cmd, duration, output_path)
        
    else:
        # Standard conversion
        vcodec = args.vcodec or "libx264"
        acodec = args.acodec or "aac"
        
        if not output_path:
            output_path = os.path.join(input_dir, f"{base_name}_obsidian{ext}")
            
        cmd = ["ffmpeg", "-y", "-i", args.input]
        crf_val = args.crf or "23"
        preset_val = args.preset or "medium"
        
        mapped_vcodec, v_args = map_codec_and_build_args(vcodec, preset_val, crf_val, "original")
        cmd += v_args
        
        if mapped_vcodec != vcodec:
            console.print(f"[bold green]GPU Acceleration Active:[/bold green] Auto-switched [cyan]{vcodec}[/cyan] to [cyan]{mapped_vcodec}[/cyan]!")
            
        if acodec == "none":
            cmd += ["-an"]
        else:
            cmd += ["-c:a", acodec]
            
        # Audio track mapping
        if args.audio_track is not None:
            cmd += ["-map", "0:v:0", "-map", f"0:a:{args.audio_track}"]
        else:
            cmd += ["-map", "0:v:0?"]
            if acodec != "none":
                cmd += ["-map", "0:a?"]
                
        # Subtitle track mapping
        if args.sub_track is not None and args.sub_track != -1:
            sub_idx = args.sub_track
            if args.sub_mode == "hard":
                escaped_path = args.input.replace("\\", "/").replace(":", "\\:")
                sub_vf = f"subtitles='{escaped_path}':si={sub_idx}"
                vf_arg = sub_vf
                for i, arg in enumerate(cmd):
                    if arg == "-vf":
                        cmd[i+1] = f"{cmd[i+1]},{sub_vf}"
                        vf_arg = None
                        break
                if vf_arg:
                    cmd += ["-vf", vf_arg]
            else:
                sub_codec = "mov_text" if output_path.lower().endswith((".mp4", ".m4v", ".mov")) else "copy"
                cmd += ["-map", f"0:s:{sub_idx}", "-c:s", sub_codec]
        elif args.sub_track == -1:
            cmd += ["-sn"]
        else:
            sub_codec = "mov_text" if output_path.lower().endswith((".mp4", ".m4v", ".mov")) else "copy"
            cmd += ["-map", "0:s?", "-c:s", sub_codec]
            
        cmd += ["-map_metadata", "0", output_path]
        run_ffmpeg_with_cli_progress(cmd, duration, output_path)

if __name__ == "__main__":
    main()
