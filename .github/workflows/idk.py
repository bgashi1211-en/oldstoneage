import os
import random
import subprocess
import threading
import time
from pathlib import Path
import gdown

TMP = Path("/tmp/clips")
TMP.mkdir(parents=True, exist_ok=True)

CLIPS_FOLDER  = "1Noj7fu2_ejCXuaT9mgJftHK1gacSj9nO"
AUDIOS_FOLDER = "1pK3XF4J_AL9Hm2sr9jrusNLPOtA4Ru98"

MIN_SIZE_BYTES = 1_000_000_000
MAX_SIZE_BYTES = int(1.99 * 1024 ** 3)  # stay safely under GitHub's 2 GiB per-asset hard limit

AUDIO_BITRATE_K = 128
VIDEO_BITRATE_K = 4500  # 4.5 Mbps @ 1080p30

TARGET_SIZE_BYTES = random.randint(int(1.7 * 1024 ** 3), int(1.95 * 1024 ** 3))

TOTAL_BITRATE_K = VIDEO_BITRATE_K + AUDIO_BITRATE_K
DURATION        = int((TARGET_SIZE_BYTES * 8) / (TOTAL_BITRATE_K * 1000))

X264_PRESET = "medium"

VIDEO_EXT = {".mp4", ".mov", ".webm"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

TARGET_CLIP_NAME = os.environ.get("TARGET_CLIP_NAME", "").strip()
if not TARGET_CLIP_NAME:
    raise SystemExit("[FATAL] TARGET_CLIP_NAME env var not set.")

# Fixed crossfade length for the seamless loop point
LOOP_BLEND_SEC = 0.6


def run_with_timeout(fn, timeout_sec=1800, label="operation"):
    result = [None]; error = [None]
    def worker():
        try: result[0] = fn()
        except Exception as e: error[0] = e
    t = threading.Thread(target=worker, daemon=True)
    t.start(); t.join(timeout_sec)
    if t.is_alive():
        raise TimeoutError(f"[TIMEOUT] {label} exceeded {timeout_sec}s")
    if error[0]: raise error[0]
    return result[0]


def dl_folder(folder_id, dest_dir, label, retries=3, timeout=900):
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, retries + 1):
        try:
            print(f"[DL] {label} attempt {attempt}/{retries}")
            run_with_timeout(
                lambda: gdown.download_folder(id=folder_id, output=str(dest_dir), quiet=False),
                timeout_sec=timeout, label=label,
            )
            print(f"[OK] {label} downloaded.")
            return
        except Exception as e:
            print(f"[WARN] {label} attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(30 * attempt)
    raise SystemExit(f"[FATAL] Could not download {label}.")


def check_disk(path, min_gb, label="disk check"):
    stat = os.statvfs(str(path))
    free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
    print(f"[DISK] {label}: {free_gb:.1f} GB free")
    if free_gb < min_gb:
        raise SystemExit(f"[FATAL] Need {min_gb} GB, only {free_gb:.1f} GB free.")
    return free_gb


def probe_duration(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        raise SystemExit(f"[FATAL] Could not read duration for {path} via ffprobe: {result.stderr}")


def make_seamless_loop(src_path):
    """Crossfades the clip's tail into its head so -stream_loop -1 has no hard seam."""
    clip_dur = probe_duration(src_path)
    blend = min(LOOP_BLEND_SEC, clip_dur * 0.4)  # never eat more than 40% of a very short clip
    offset = round(clip_dur - blend, 3)
    out_path = src_path.parent / f"seamless_{src_path.stem}.mp4"

    filter_complex = (
        f"[0:v]split=2[v1][v2];"
        f"[v2]trim=0:{blend:.3f},setpts=PTS-STARTPTS[vtail];"
        f"[v1][vtail]xfade=transition=fade:duration={blend:.3f}:offset={offset:.3f}[outv]"
    )

    cmd = [
        "ffmpeg", "-y", "-hide_banner",
        "-i", str(src_path),
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        str(out_path),
    ]

    print(f"[LOOP] Building seamless loop: clip={clip_dur:.2f}s blend={blend:.2f}s offset={offset:.2f}s")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        print(proc.stdout)
        print(proc.stderr)
        raise SystemExit("[FATAL] Seamless loop generation failed.")

    print(f"[OK] Seamless loop created: {out_path.name}")
    return out_path


# ── Downloads ──────────────────────────────────────────────────────────────────
check_disk(TMP, 4.0, "before downloads")

clips_dir  = TMP / "clips"
audios_dir = TMP / "audios"

dl_folder(CLIPS_FOLDER,  clips_dir,  "clips")
dl_folder(AUDIOS_FOLDER, audios_dir, "audios")

# ── Locate target clip ──────────────────────────────────────────────────────────
matches = list(clips_dir.rglob(TARGET_CLIP_NAME))
if not matches:
    raise SystemExit(f"[FATAL] {TARGET_CLIP_NAME} not found in clips folder.")
clip_path = matches[0]

# Sanitize filename
safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in clip_path.name)
safe_path = clip_path.parent / safe_name
if safe_name != clip_path.name:
    clip_path.rename(safe_path)
    clip_path = safe_path
    print(f"[OK] Renamed clip to: {clip_path.name}")

clip_ext = clip_path.suffix.lower()
if clip_ext not in VIDEO_EXT:
    raise SystemExit(f"[FATAL] Unsupported file type: {clip_ext}")
print(f"[OK] Clip: {clip_path.name}")

# ── Build seamless loop ──────────────────────────────────────────────────────────
clip_path = make_seamless_loop(clip_path)

# ── Pick one audio track ─────────────────────────────────────────────────────────
audio_files = sorted(p for p in audios_dir.rglob("*") if p.suffix.lower() in AUDIO_EXT and p.is_file())
if not audio_files:
    raise SystemExit("[FATAL] No audio found in audios folder.")
audio_path = random.choice(audio_files)
print(f"[OK] Audio picked: {audio_path.name}")

check_disk(TMP, 2.0, "after downloads")

output_path = TMP / f"OUT_{clip_path.stem}.mp4"

print(f"""
=== RENDER JOB ===
  CLIP         : {clip_path.name}
  AUDIO        : {audio_path.name}
  DURATION     : {DURATION}s ({DURATION//3600}h {(DURATION%3600)//60}m)
  TARGET SIZE  : {TARGET_SIZE_BYTES / 1e9:.2f} GB
  VIDEO BITRATE: {VIDEO_BITRATE_K}k
  PRESET       : {X264_PRESET}
""")

# input 0 = clip (looped), input 1 = audio (looped)
filter_complex = (
    "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
    "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p[outv]"
)

cmd = [
    "ffmpeg", "-y", "-hide_banner", "-fflags", "+genpts",
    "-stream_loop", "-1", "-i", str(clip_path),
    "-stream_loop", "-1", "-i", str(audio_path),
    "-filter_complex", filter_complex,
    "-map", "[outv]",
    "-map", "1:a",
    "-t", str(DURATION),
    "-c:v", "libx264",
    "-preset", X264_PRESET,
    "-b:v", f"{VIDEO_BITRATE_K}k",
    "-bufsize", f"{VIDEO_BITRATE_K * 2}k",
    "-maxrate", f"{int(VIDEO_BITRATE_K * 1.2)}k",
    "-c:a", "aac",
    "-b:a", f"{AUDIO_BITRATE_K}k",
    "-ar", "44100",
    "-pix_fmt", "yuv420p",
    "-r", "30",
    "-g", "60",
    "-profile:v", "high",
    "-level", "4.1",
    "-movflags", "+faststart",
    str(output_path),
]

print("=== Starting FFmpeg ===")
proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
stopped_by_watcher = False


def size_watcher():
    global stopped_by_watcher
    while proc.poll() is None:
        time.sleep(10)
        if output_path.exists():
            size = output_path.stat().st_size
            print(f"[SIZE] {size/1024**2:.0f} MB  ({size/1024**3:.3f} GB)", flush=True)
            if size >= MAX_SIZE_BYTES:
                print("[SIZE] Reached cap — stopping FFmpeg.", flush=True)
                stopped_by_watcher = True
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break


watcher_thread = threading.Thread(target=size_watcher, daemon=True)
watcher_thread.start()

for line in proc.stdout:
    print(line, end="", flush=True)

proc.wait()
watcher_thread.join(timeout=30)

if not stopped_by_watcher and proc.returncode not in (0, -15, 255):
    raise SystemExit(f"[FATAL] FFmpeg exited with code {proc.returncode}")

if not output_path.exists() or output_path.stat().st_size == 0:
    raise SystemExit("[FATAL] Output file missing or empty.")

final_size    = output_path.stat().st_size
final_size_mb = final_size / (1024 ** 2)
final_size_gb = final_size / (1024 ** 3)
stop_reason   = "size cap" if stopped_by_watcher else "duration reached"

if final_size < MIN_SIZE_BYTES:
    raise SystemExit(f"[FATAL] Output only {final_size_gb:.3f} GB — below 1 GB minimum.")

print(f"""
=== RENDER COMPLETE ===
  Output    : {output_path}
  Stop      : {stop_reason}
  Size      : {final_size_mb:.1f} MB ({final_size_gb:.3f} GB)
  OK        : {'YES' if MIN_SIZE_BYTES <= final_size <= MAX_SIZE_BYTES else 'OUT OF RANGE'}
""")

github_output = os.environ.get("GITHUB_OUTPUT")
if github_output:
    with open(github_output, "a") as f:
        f.write(f"output_path={output_path}\n")
        f.write(f"clip_name={clip_path.name}\n")
        f.write(f"audio_used={audio_path.name}\n")
        f.write(f"duration_seconds={DURATION}\n")
        f.write(f"final_size_mb={final_size_mb:.1f}\n")
        f.write(f"stop_reason={stop_reason}\n")
