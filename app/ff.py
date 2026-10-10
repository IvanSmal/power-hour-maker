"""Thin wrappers around the ffmpeg / ffprobe binaries."""
import json
import logging
import os
import subprocess
import threading
import time

from .paths import FFMPEG, FFPROBE

log = logging.getLogger(__name__)

# Keep console windows from flashing up for every ffmpeg call when running windowless.
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class FFmpegError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


class Stalled(FFmpegError):
    pass


STALL_SECONDS = 45


def run(args, duration=None, on_progress=None, cancel=None):
    """Run ffmpeg (args exclude the binary). Reports 0..1 progress against `duration` seconds.

    Raises Cancelled when `cancel` (a threading.Event) gets set, FFmpegError on failure.
    """
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats", *map(str, args)]
    log.debug("ffmpeg %s", subprocess.list2cmdline(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace")
    err = []
    reader = threading.Thread(target=lambda: err.extend(proc.stderr), daemon=True)
    reader.start()
    done = threading.Event()
    state = {"out": None, "changed": time.monotonic(), "stalled": False}

    def watchdog():
        # Kill on cancel, or when the output clock hasn't moved for a long while (ffmpeg deadlock).
        while not done.wait(0.25):
            if cancel is not None and cancel.is_set():
                proc.kill()
                return
            if time.monotonic() - state["changed"] > STALL_SECONDS:
                state["stalled"] = True
                proc.kill()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        for line in proc.stdout:
            if line.startswith("out_time_us="):
                v = line.split("=", 1)[1].strip()
                if v != state["out"]:
                    state["out"], state["changed"] = v, time.monotonic()
                if on_progress and duration and v.lstrip("-").isdigit():
                    on_progress(max(0.0, min(1.0, int(v) / 1e6 / duration)))
        proc.wait()
    finally:
        done.set()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    reader.join(timeout=5)
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    if state["stalled"]:
        raise Stalled(f"ffmpeg stopped making progress for {STALL_SECONDS}s")
    if proc.returncode != 0:
        tail = "".join(err[-25:]).strip()
        raise FFmpegError(tail or f"ffmpeg exited with code {proc.returncode}")
    if on_progress:
        on_progress(1.0)


def run_capture(args, timeout=600):
    """Run ffmpeg and return raw stdout bytes (for decoding audio/frames to a pipe)."""
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", *map(str, args)]
    p = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW, timeout=timeout)
    if p.returncode != 0:
        raise FFmpegError(p.stderr.decode("utf-8", "replace").strip()[-2000:] or "ffmpeg failed")
    return p.stdout


def probe(path):
    """Duration / dimensions / audio presence of a media file."""
    p = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                       capture_output=True, creationflags=NO_WINDOW, timeout=60)
    if p.returncode != 0:
        raise FFmpegError(p.stderr.decode("utf-8", "replace").strip() or "ffprobe failed")
    data = json.loads(p.stdout.decode("utf-8", "replace"))
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    dur = float(data.get("format", {}).get("duration") or 0)
    if not dur:
        dur = float((v or a or {}).get("duration") or 0)
    w = h = 0
    fps = 0.0
    if v:
        w, h = int(v.get("width") or 0), int(v.get("height") or 0)
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(float(sd["rotation"]))
        if abs(rot) % 180 == 90:      # ffmpeg auto-rotates on decode, so report display size
            w, h = h, w
        num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
        try:
            fps = float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            fps = 0.0
    return {"duration": dur, "has_video": bool(v), "has_audio": bool(a), "width": w, "height": h, "fps": fps}


# --- encoders ----------------------------------------------------------------------------

ENCODER_LABELS = {
    "libx264": "CPU (x264)",
    "h264_nvenc": "NVIDIA NVENC",
    "h264_qsv": "Intel Quick Sync",
    "h264_amf": "AMD AMF",
}

_encoders = None
_enc_lock = threading.Lock()


def available_encoders():
    """Encoders that actually work on this machine (test-encodes a few frames, cached)."""
    global _encoders
    with _enc_lock:
        if _encoders is None:
            found = ["libx264"]
            for name in ("h264_nvenc", "h264_qsv", "h264_amf"):
                try:
                    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                                        "-i", "color=s=640x360:d=0.3", "-c:v", name, "-f", "null", "-"],
                                       capture_output=True, creationflags=NO_WINDOW, timeout=30)
                    if r.returncode == 0:
                        found.append(name)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            _encoders = found
            log.info("working encoders: %s", found)
        return list(_encoders)


def pick_encoder(choice):
    encs = available_encoders()
    if choice and choice != "auto" and choice in encs:
        return choice
    for pref in ("h264_nvenc", "h264_qsv", "h264_amf"):
        if pref in encs:
            return pref
    return "libx264"


QUALITY = {
    #          x264 preset, crf, nvenc preset, cq
    "draft": ("ultrafast", 28, "p1", 31),
    "good": ("veryfast", 23, "p4", 27),     # ~SSIM 0.985 on grainy 1080p; p4 is ~35% faster than p5
    "best": ("slow", 19, "p6", 22),
}


def video_args(encoder, quality, fps, height):
    """Encoder args for segments that end up in the final file. Every segment of one render
    uses identical args so the final concat can stream-copy them."""
    x_preset, crf, nv_preset, cq = QUALITY.get(quality, QUALITY["good"])
    level = "4.2" if height <= 1088 else ("5.1" if height <= 1440 else "5.2")
    if encoder == "h264_nvenc":
        a = ["-c:v", "h264_nvenc", "-preset", nv_preset, "-tune", "hq", "-rc", "vbr", "-cq", cq,
             "-b:v", "0", "-maxrate", "40M", "-bufsize", "80M", "-profile:v", "high", "-bf", "0"]
    elif encoder == "h264_qsv":
        a = ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", cq, "-profile:v", "high", "-bf", "0"]
    elif encoder == "h264_amf":
        a = ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", cq, "-qp_p", cq,
             "-profile:v", "high", "-bf", "0"]
    else:
        a = ["-c:v", "libx264", "-preset", x_preset, "-crf", crf, "-profile:v", "high", "-bf", "0"]
    return [*map(str, a), "-level:v", level, "-pix_fmt", "yuv420p", "-g", str(int(fps * 2)), "-r", str(fps)]


def scratch_video_args(fps):
    """Near-lossless, very fast encode for the short head/tail pieces that feed transitions."""
    return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "8", "-pix_fmt", "yuv420p", "-r", str(fps)]


AUDIO_ARGS = ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"]
