#!/usr/bin/env python3
"""
usb_import_service.py  (was camera_service.py)

Imports files off a USB mass-storage device — a wildlife camera or an acoustic
recorder — routing them by type. Selected by the launcher when
`capture_manager.capture_mode == "camera-usb"`.

- Powers the device on/off via the VBUS switch (GPIO or uhubctl)
- Mounts the camera (or acoustic-recorder) USB mass storage
- Copies files off it using atomic .part -> rename, routing by extension:
    images (IMAGE_EXTS) -> DEST_DIR  as {seq}_{timestamp}.jpg
    audio  (AUDIO_EXTS) -> AUDIO_DIR as {seq}_{timestamp}.wav
  So swapping the camera for a WAV recorder Just Works — the audio detector
  picks the WAVs up from AUDIO_DIR exactly like mic-recorded clips.
- Deletes originals from the device after successful save
- NO compression/optimization here (XBee/uploader/audio detector handle that)

Modes:
- default: loop forever with camera_poll_interval_sec
- --once: run one capture cycle and exit
"""

import argparse
import time
import os
import json
import shutil
import subprocess
import glob
import atexit
import signal
import sys
from datetime import datetime
from PIL import Image, ExifTags

# Shared helpers moved to lib/ — put it on the path before importing them.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
from device_id_util import read_device_id
from vbus_control import make_vbus
from robust import have_min_free_mb

# =============================================================================
# Base paths / config
# =============================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)

CONFIG_FILE = os.path.join(PROJECT_ROOT, "config", "config.json")
try:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
except Exception as e:
    print(f"[config] Could not read {CONFIG_FILE}: {e}")
    cfg = {}

DEST_DIR = cfg.get("dest_dir", os.path.join(PROJECT_ROOT, "images"))
os.makedirs(DEST_DIR, exist_ok=True)

# Audio destination — used when an acoustic recorder is swapped in for the
# camera. Same USB-mass-storage import path; files just route here by extension.
AUDIO_DIR = cfg.get("audio_dir", os.path.join(PROJECT_ROOT, "audio"))
os.makedirs(AUDIO_DIR, exist_ok=True)

# Route copied files by extension. Images land in DEST_DIR (normalised to .jpg
# so the sender's *.jpg glob picks them up); audio lands in AUDIO_DIR keeping
# its real extension so the audio detector's *.wav glob picks it up. Both are
# config-overridable; defaults cover a stills camera or a WAV recorder.
IMAGE_EXTS = {e.lower() for e in cfg.get("capture_image_exts", [".jpg", ".jpeg"])}
AUDIO_EXTS = {e.lower() for e in cfg.get("capture_audio_exts", [".wav"])}

DEVICE_ID_FILE = cfg.get("device_id_file", os.path.join(PROJECT_ROOT, "device_id.txt"))
DEVICE_NAME = read_device_id(DEVICE_ID_FILE)

# =============================================================================
# Hardware / mount settings
# =============================================================================

FORCED_MOUNT = cfg.get("forced_mount", "/mnt/camera")
AUTO_MOUNT_BASE = cfg.get("auto_mount_base", "/media/sparrow")
POLL_INTERVAL_SEC = int(cfg.get("camera_poll_interval_sec", 600))

os.makedirs(FORCED_MOUNT, exist_ok=True)

# =============================================================================
# Global state
# =============================================================================

POWERED = False
MOUNTED = False

# =============================================================================
# VBUS controller — pluggable backend (uhubctl or GPIO)
# =============================================================================
# Note: this also fixes the latent cfg-nesting bug. Previously the module read
# `cfg.get("vbus_pin", 17)` from the *top* level of config, but config.json
# nests the key under `camera_usb`. The factory now reads from the per-service
# block, so any historical `vbus_pin` value finally takes effect.
vbus = make_vbus(cfg.get("camera_usb", {}))
print(f"[usb_import] vbus={vbus!r}")


def power_on():
    global POWERED
    if POWERED:
        return
    print(f"Powering camera ON via {vbus!r}")
    vbus.on()
    POWERED = True
    time.sleep(2.0)


def power_off():
    global POWERED
    if not POWERED:
        return
    print(f"Powering camera OFF via {vbus!r}")
    vbus.off()
    POWERED = False
    time.sleep(0.5)


# =============================================================================
# USB / mount helpers
# =============================================================================

def _system_disks():
    """Whole-disk nodes (e.g. /dev/sda) that back /, /boot etc. — never touch these."""
    sys_disks = set()
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                dev, mnt = parts[0], parts[1]
                if dev.startswith("/dev/sd") and mnt in ("/", "/boot", "/boot/firmware"):
                    sys_disks.add(dev.rstrip("0123456789"))
    except OSError:
        pass
    return sys_disks


def find_device_node(timeout=10.0, interval=0.5):
    """Wait for a NON-system /dev/sdX1 (or /dev/sdX) to appear and return it.

    Skips any disk currently backing / or /boot, so on a USB-boot Pi (or one with
    a USB SSD) we never mount and delete off the system disk."""
    elapsed = 0.0
    sys_disks = _system_disks()

    def _ok(d):
        if d.rstrip("0123456789") in sys_disks:
            print(f"Skipping system disk {d}")
            return False
        return True

    while elapsed < timeout:
        for d in sorted(glob.glob("/dev/sd[a-z][0-9]")):
            if _ok(d):
                print(f"Found partition node: {d}")
                return d
        for d in sorted(glob.glob("/dev/sd[a-z]")):
            if _ok(d):
                print(f"Found raw block device: {d}")
                return d
        time.sleep(interval)
        elapsed += interval
    return None


def unmount_auto():
    """Unmount any auto-mounted /dev/sd* under /media/sparrow to avoid conflicts."""
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                dev_node = parts[0]
                mnt_pt = parts[1]
                if dev_node.startswith("/dev/sd") and mnt_pt.startswith(AUTO_MOUNT_BASE):
                    print(f"Auto-unmounting (lazy) {dev_node} from {mnt_pt}")
                    subprocess.run(["sudo", "umount", "-l", mnt_pt], check=False)
    except Exception as e:
        print(f"[unmount_auto] Warning: {e}")


def mount_camera(dev_node):
    global MOUNTED
    if MOUNTED:
        return
    print(f"Mounting {dev_node} → {FORCED_MOUNT}")
    subprocess.run(
        ["sudo", "mount", "-o", "uid=1000,gid=1000", dev_node, FORCED_MOUNT],
        check=True, timeout=30,
    )
    MOUNTED = True


def unmount_camera():
    global MOUNTED
    if not MOUNTED:
        return
    print(f"Unmounting {FORCED_MOUNT}")
    try:
        rc = subprocess.run(["sudo", "umount", FORCED_MOUNT], check=False, timeout=30).returncode
    except subprocess.TimeoutExpired:
        rc = 124
    if rc != 0:
        # Busy or timed out — lazy-detach so the later power_off() can't cut VBUS
        # out from under a live filesystem (FAT corruption on the camera card).
        print(f"[unmount] umount rc={rc}; lazy-detaching (-l) before power-off")
        try:
            subprocess.run(["sudo", "umount", "-l", FORCED_MOUNT], check=False, timeout=30)
        except subprocess.TimeoutExpired:
            pass
    MOUNTED = False


# =============================================================================
# EXIF timestamp helper
# =============================================================================

def get_exif_datetime(filepath):
    try:
        img = Image.open(filepath)
        exif = img._getexif()
        if not exif:
            return None

        dt_tag = None
        for tag, name in ExifTags.TAGS.items():
            if name == "DateTimeOriginal":
                dt_tag = tag
                break

        if dt_tag and dt_tag in exif:
            dt_string = exif[dt_tag]
            return datetime.strptime(dt_string, "%Y:%m:%d %H:%M:%S")
    except Exception as e:
        print(f"[EXIF] Error reading EXIF from {filepath}: {e}")
    return None


# =============================================================================
# Atomic write helper
# =============================================================================

def safe_write_final(dst_final, writer_fn):
    part = dst_final + ".part"
    try:
        writer_fn(part)
        try:
            fd = os.open(part, os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)
        except Exception:
            pass
        os.rename(part, dst_final)
    except Exception:
        # Never leak a partial .part on failure — orphans fill the disk and block
        # the collision loop (which treats a leftover .part as "occupied").
        try:
            os.remove(part)
        except OSError:
            pass
        raise


# =============================================================================
# Core copy logic
# =============================================================================

def _same_size(path, size):
    """True if `path` exists and is exactly `size` bytes (cheap identity check)."""
    try:
        return os.path.getsize(path) == size
    except OSError:
        return False


def process_and_save_images():
    run_count = 0
    counter = 1

    for root, _, files in os.walk(FORCED_MOUNT):
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext in IMAGE_EXTS:
                dest_dir, out_ext, kind = DEST_DIR, ".jpg", "image"
            elif ext in AUDIO_EXTS:
                dest_dir, out_ext, kind = AUDIO_DIR, ext, "audio"
            else:
                continue

            src = os.path.join(root, fn)

            # EXIF DateTimeOriginal only exists on images; audio falls back to mtime.
            dt = get_exif_datetime(src) if kind == "image" else None
            if dt is None:
                dt = datetime.fromtimestamp(os.path.getmtime(src))
            timestamp = dt.strftime("%Y%m%d%H%M%S")

            # Runaway-duplicate guard. If the source delete below keeps failing
            # (e.g. a read-only card), the device re-presents the same file every
            # cycle and, without this, we'd re-copy it as _1.._N forever (that's
            # how a dest_dir once ballooned to 2.7 GB of one photo). Skip the copy
            # when we already hold an identically-sized capture for this timestamp;
            # still try to delete the source so the card eventually clears.
            try:
                src_size = os.path.getsize(src)
            except OSError:
                src_size = -1
            already = any(
                _same_size(existing, src_size)
                for existing in glob.glob(os.path.join(dest_dir, f"*_{timestamp}*{out_ext}"))
            )
            if already:
                print(f"Skipping already-imported {kind} {src} ({timestamp}, {src_size} bytes)")
                try:
                    os.remove(src)
                    print(f"Deleted original camera file: {src}")
                except Exception as e:
                    print(f"[DELETE] Error deleting {src}: {e}")
                continue

            seq = f"{counter:02d}"
            new_name = f"{seq}_{timestamp}{out_ext}"
            dst_final = os.path.join(dest_dir, new_name)

            suffix = 1
            base, name_ext = os.path.splitext(new_name)
            while os.path.exists(dst_final) or os.path.exists(dst_final + ".part"):
                if suffix > 99:
                    print(f"[WARN] too many name collisions for {timestamp}; skipping {src}")
                    dst_final = None
                    break
                new_name = f"{base}_{suffix}{name_ext}"
                dst_final = os.path.join(dest_dir, new_name)
                suffix += 1
            if dst_final is None:
                continue

            print(f"Copying {kind} {src} → {dst_final}")

            try:
                def _write(to_path):
                    shutil.copy2(src, to_path)

                safe_write_final(dst_final, _write)

                try:
                    os.remove(src)
                    print(f"Deleted original camera file: {src}")
                except Exception as e:
                    print(f"[DELETE] Error deleting {src}: {e}")

                run_count += 1
                counter += 1

            except Exception as e:
                print(f"[COPY] Error processing {src}: {e}")
                continue

    print(f"Finished saving {run_count} file(s) this run.")
    return run_count


# =============================================================================
# Safe teardown
# =============================================================================

def teardown(reason=""):
    try:
        if MOUNTED:
            unmount_camera()
    except Exception as e:
        print(f"[teardown] Unmount error: {e}")

    try:
        power_off()
    except Exception as e:
        print(f"[teardown] Power-off error: {e}")

    try:
        vbus.cleanup()
    except Exception as e:
        print(f"[teardown] VBUS cleanup error: {e}")

    if reason:
        print(f"[teardown] Completed ({reason}).")
    else:
        print("[teardown] Completed.")


def _signal_handler(signum, frame):
    names = {signal.SIGINT: "SIGINT", signal.SIGTERM: "SIGTERM", signal.SIGHUP: "SIGHUP"}
    reason = f"signal {names.get(signum, signum)}"
    print(f"\n[signal] Caught {reason}, tearing down…")
    teardown(reason)
    sys.exit(0)


for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(sig, _signal_handler)

atexit.register(lambda: teardown("atexit"))


# =============================================================================
# One capture cycle
# =============================================================================

def run_cycle():
    saved = 0
    # Low-disk pause: if the dest filesystem is nearly full (uplink down for days),
    # stop pulling NEW files but keep the forwarder draining the backlog. Nothing is
    # deleted; capture resumes automatically once space frees.
    min_free_mb = int(cfg.get("launcher", {}).get("min_free_mb", 200))
    if not have_min_free_mb(DEST_DIR, min_free_mb):
        print(f"[cycle] SKIP capture: <{min_free_mb}MB free on {DEST_DIR}; draining backlog only")
        return 0
    try:
        power_off()
        power_on()

        dev = find_device_node(timeout=10.0)
        if not dev:
            raise RuntimeError("Camera did not enumerate as /dev/sdX or /dev/sdX1 within timeout.")

        unmount_auto()
        mount_camera(dev)

        saved = process_and_save_images()

    except Exception as e:
        print(f"[cycle] ERROR: {e}")

    finally:
        try:
            unmount_camera()
        except Exception as ue:
            print(f"[finalize] Unmount error: {ue}")

        try:
            power_off()
        except Exception as pe:
            print(f"[finalize] Power-off error: {pe}")

        print("Capture & save cycle complete (finalized).")

    return saved


# =============================================================================
# Main
# =============================================================================

def main():
    ap = argparse.ArgumentParser(description="Camera import service")
    ap.add_argument("--once", action="store_true", help="Run one capture cycle and exit")
    args = ap.parse_args()

    print(f"[usb_import] device={DEVICE_NAME} dest_dir={DEST_DIR} audio_dir={AUDIO_DIR} interval={POLL_INTERVAL_SEC}s once={args.once}")

    if args.once:
        saved_count = run_cycle()
        print(f"Saved {saved_count} file(s) this iteration.")
        return 0

    while True:
        start = time.time()
        saved_count = run_cycle()
        print(f"Saved {saved_count} file(s) this iteration.")

        elapsed = time.time() - start
        to_sleep = POLL_INTERVAL_SEC - elapsed
        if to_sleep > 0:
            print(f"Sleeping for {to_sleep:.1f} seconds…")
            time.sleep(to_sleep)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nReceived Ctrl-C, shutting down…")
    finally:
        teardown("main finally")