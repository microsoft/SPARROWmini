#!/usr/bin/env python3
import os
import json
import time
import subprocess
import numpy as np
import soundfile as sf
from datetime import datetime
import logging

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
CONFIG_FILE = os.path.join(PROJECT_ROOT, "config", "config.json")

# Logs folder (sibling of this script)
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Log files
LOG_FILE = os.path.join(LOG_DIR, "audio.log")
DAILY_LOG_FILE = os.path.join(LOG_DIR, f"audio_{datetime.now().strftime('%Y%m%d')}.log")

# Logging (console + file)
logger = logging.getLogger("audio_monitor")
logger.setLevel(logging.INFO)
logger.handlers.clear()
logger.propagate = False

fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

console_handler = logging.StreamHandler()
console_handler.setFormatter(fmt)
console_handler.setLevel(logging.INFO)

file_handler = logging.FileHandler(LOG_FILE)
file_handler.setFormatter(fmt)
file_handler.setLevel(logging.INFO)

daily_file_handler = logging.FileHandler(DAILY_LOG_FILE)
daily_file_handler.setFormatter(fmt)
daily_file_handler.setLevel(logging.INFO)

logger.addHandler(console_handler)
logger.addHandler(file_handler)
logger.addHandler(daily_file_handler)

# Load config (strict)
def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

cfg = load_config()
acfg = cfg["audio"]
DEST_DIR = cfg["audio_dir"]

# Hard ceiling on any single recording. Clips ship over XBee as MP3, and a long
# clip is minutes of radio airtime — so the node clamps every duration to this
# regardless of what's in config or set via the web UI (defense-in-depth: the
# web UI validates to the same max, this guarantees it even for a hand-edited
# config or a legacy `duration_minutes` value).
MAX_RECORD_SEC = 10

def _clamp_rec(seconds) -> int:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        s = MAX_RECORD_SEC
    return max(1, min(s, MAX_RECORD_SEC))

SAMPLE_RATE      = int(acfg["SAMPLE_RATE"])
MONITOR_DURATION = int(acfg["MONITOR_DURATION"])
RECORD_DURATION  = _clamp_rec(acfg["RECORD_DURATION"])
THRESHOLD        = int(acfg["THRESHOLD"])
MODE             = str(acfg["MODE"]).lower()
SCHEDULE         = acfg["SCHEDULE"]
# Explicit device override from config; blank => auto-detect (re-probed at runtime).
DEVICE_OVERRIDE  = str(acfg.get("device", "")).strip() or None

INTERVAL_SEC = int(SCHEDULE["interval_minutes"]) * 60
# Schedule clip length is seconds now (capped at MAX_RECORD_SEC). Fall back to a
# legacy `duration_minutes` value if that's all the config has, then clamp.
if SCHEDULE.get("duration_seconds") is not None:
    DURATION_SEC = _clamp_rec(SCHEDULE["duration_seconds"])
else:
    DURATION_SEC = _clamp_rec(int(SCHEDULE.get("duration_minutes", 0)) * 60)

# Make dirs
os.makedirs(DEST_DIR, exist_ok=True)

# Detect microphone if not set
def detect_usb_microphone():
    try:
        result = subprocess.run(["arecord", "-l"], capture_output=True, text=True, check=True)
        for line in result.stdout.splitlines():
            if ("AudioMoth" in line) or ("USB Microphone" in line) or ("Microphone" in line):
                parts = line.split(":")
                card_number = parts[0].split()[1]
                dev_num = "0"
                for seg in parts[1:]:
                    if "device" in seg:
                        dev_num = seg.split("device")[-1].split()[0].strip(":,")
                        break
                ident = f"plughw:{card_number},{dev_num}"
                logger.info(f"Detected microphone: {ident} from line: {line.strip()}")
                return ident
        logger.error("No microphone found in `arecord -l` output.")
    except Exception as e:
        logger.error(f"Audio device detection failed: {e}")
    return None

# Runtime mic resolution. Instead of exiting when no mic is present (which meant
# a mic plugged in *after* the service started stayed unseen until a full service
# restart), we re-probe on demand. A hotplugged AudioMoth is picked up on the next
# loop iteration — no `systemctl restart` needed.
MIC_RETRY_SEC = 10
_resolved_device = None      # last mic we resolved to
_warned_no_mic = False       # throttle the "waiting for mic" log to once per outage

def resolve_device():
    """Return the mic device string to use, or None if none is available yet.

    Honors an explicit `audio.device` override; otherwise auto-detects and
    re-probes every call so plug/unplug is handled live."""
    global _resolved_device, _warned_no_mic
    if DEVICE_OVERRIDE:
        return DEVICE_OVERRIDE
    dev = detect_usb_microphone()
    if dev:
        if dev != _resolved_device:
            logger.info(f"Microphone available: {dev}")
        _warned_no_mic = False
    else:
        if not _warned_no_mic:
            logger.warning(f"No microphone present — waiting, re-probing every {MIC_RETRY_SEC}s")
            _warned_no_mic = True
    _resolved_device = dev
    return dev

logger.info(f"Audio config loaded: {acfg}")
logger.info(f"Recording cap: {MAX_RECORD_SEC}s (record={RECORD_DURATION}s schedule={DURATION_SEC}s)")

# Monitor audio
def monitor_audio(device):
    temp_file = "/tmp/monitor.wav"
    cmd = [
        "arecord", "-D", device,
        "-f", "S16_LE", "-r", str(SAMPLE_RATE),
        "-c", "1",
        "-d", str(MONITOR_DURATION), temp_file
    ]
    try:
        # -d self-terminates, but a wedged USB mic can hang arecord in D-state;
        # the timeout is a hard backstop so the loop can't block forever.
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=float(MONITOR_DURATION) + 15)
        try:
            size = os.path.getsize(temp_file)
            logger.info(f"monitor.wav size: {size} bytes")
            if size == 0:
                logger.warning("monitor.wav is empty (0 bytes)")
                return None
        except Exception:
            pass
        data, _ = sf.read(temp_file, dtype="int16", always_2d=False)
        arr = np.asarray(data).flatten()
        if arr.size == 0:
            return None
        return arr
    except subprocess.CalledProcessError as e:
        logger.error(f"Monitoring failed (arecord): {e.stderr.decode(errors='ignore')}")
    except Exception as e:
        logger.error(f"Monitoring failed: {e}")
    return None

# Record audio atomically (.part -> final)
def record_audio(duration_s, device):
    # Filename matches the image convention: {seq:02d}_{YYYYMMDDHHMMSS}.wav
    # so the round-robin scheduler can FIFO both pools by mtime uniformly.
    # seq is always "01" — the timestamp prevents collisions and audio_service
    # records one clip per invocation.
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    final = os.path.join(DEST_DIR, f"01_{ts}.wav")
    part  = final + ".part"
    cmd = [
        "arecord", "-D", device,
        "-f", "S16_LE", "-r", str(SAMPLE_RATE),
        "-c", "1",
        "-d", str(int(duration_s)), part
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=float(int(duration_s)) + 30)
        os.replace(part, final)
        logger.info(f"Recorded {duration_s}s → {final}")
    except subprocess.TimeoutExpired:
        logger.error("Recording timed out (arecord wedged); cleaning up .part")
        try:
            os.remove(part)
        except OSError:
            pass
    except subprocess.CalledProcessError as e:
        logger.error(f"Recording failed (arecord): {e.stderr.decode(errors='ignore')}")
        try:
            os.remove(part)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Recording failed: {e}")
        try:
            os.remove(part)
        except Exception:
            pass

# Main loop
def main():
    logger.info("Audio monitoring started.")
    while True:
        device = resolve_device()
        if not device:
            # No mic right now — wait and re-probe instead of dying. A mic
            # plugged in later is picked up on a subsequent iteration.
            time.sleep(MIC_RETRY_SEC)
            continue

        if MODE == "threshold":
            data = monitor_audio(device)
            if data is not None:
                max_amp = int(np.max(np.abs(data)))
                logger.info(f"Max amplitude: {max_amp}")
                if max_amp > THRESHOLD:
                    logger.info("Threshold exceeded → recording")
                    record_audio(RECORD_DURATION, device)
            time.sleep(MONITOR_DURATION)
        else:  # schedule
            record_audio(DURATION_SEC, device)
            to_sleep = max(0, INTERVAL_SEC - DURATION_SEC)
            logger.info(f"Next scheduled record in {to_sleep}s")
            time.sleep(to_sleep)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Stopped by user")
