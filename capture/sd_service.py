#!/usr/bin/env python3
"""
sdwire_import.py

- Power-cycles SDWire USB via GPIO (simulates unplug/replug)
- Uses SDWire to switch SD card to HOST
- Mounts the SD partition
- Copies JPGs into DEST_DIR using atomic .part -> rename
- (OPTIONAL) compress/optimize JPEGs while copying
- Deletes originals from the SD card after successful save
- Unmounts
- Switches SDWire back to TARGET (camera)
- Powers SDWire USB off

Runs on an interval read from config/config.json:
  capture.interval_sec (fallback 600)

Filename convention:
  {seq:02d}_{YYYYMMDDHHMMSS}.jpg

Systemd fix:
  - Resolves the sdwire binary even when PATH is minimal under systemd.
  - Supports venv install at /home/sparrow/robin/.venv/bin/sdwire.
  - Optional config override: sdwire.bin
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from datetime import datetime
from typing import Optional

from PIL import Image, ExifTags

import sys
_LIB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
from vbus_control import make_vbus

# =========================
# Config loading
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "config.json")


def load_cfg() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[config] Could not read {CONFIG_PATH}: {e}")
        return {}


CFG = load_cfg()


def cfg_get(d: dict, path: str, default):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


# =========================
# Settings (from config with sane fallbacks)
# =========================

# main scheduling
CAPTURE_INTERVAL_SEC = int(cfg_get(CFG, "capture.interval_sec", 600))

# shared paths
DEST_DIR = str(cfg_get(CFG, "dest_dir", "/home/sparrow/robin/images"))

# SDWire config
SD_PARTITION = str(cfg_get(CFG, "sdwire.partition", "/dev/sda1"))
MOUNT_POINT = str(cfg_get(CFG, "sdwire.mount", "/mnt/sdwire"))
CAMERA_SUBDIR = str(cfg_get(CFG, "sdwire.camera_subdir", "DCIM"))
SWITCH_BACK_TO_TARGET = bool(cfg_get(CFG, "sdwire.switch_back_to_target", True))
SDWIRE_VIDPID = str(cfg_get(CFG, "sdwire.vidpid", "0bda:0316"))

SDWIRE_VBUS_PIN = int(cfg_get(CFG, "sdwire.vbus_pin", 17))
POWER_ON_SETTLE_SEC = float(cfg_get(CFG, "sdwire.power_on_sec", 2.0))
POWER_OFF_SEC = float(cfg_get(CFG, "sdwire.power_off_sec", 0.8))

# timing / retries
MAX_ATTEMPTS = int(cfg_get(CFG, "sdwire.max_attempts", 5))
WAIT_FOR_SDWIRE_CTRL_SEC = float(cfg_get(CFG, "sdwire.wait_ctrl_sec", 12.0))
WAIT_FOR_PARTITION_SEC = float(cfg_get(CFG, "sdwire.wait_partition_sec", 15.0))
POLL_INTERVAL_SEC = float(cfg_get(CFG, "sdwire.poll_interval_sec", 0.25))

# file handling
STABLE_FILE_SECONDS = float(cfg_get(CFG, "image.stable_file_sec", 1.0))
JPG_EXTS = {".jpg", ".jpeg"}

# compression
ENABLE_COMPRESSION = bool(cfg_get(CFG, "image.enable_compression", True))
JPEG_QUALITY = int(cfg_get(CFG, "image.jpeg_quality", 82))
MAX_DIM = int(cfg_get(CFG, "image.max_dim", 0))
STRIP_EXIF = bool(cfg_get(CFG, "image.strip_exif", False))

# optional override for systemd-safe sdwire path:
#   "sdwire": { "bin": "/home/sparrow/robin/.venv/bin/sdwire", ... }
SDWIRE_BIN_CFG = str(cfg_get(CFG, "sdwire.bin", "")).strip()


def resolve_sdwire_bin() -> str:
    """
    Resolve the sdwire executable reliably under systemd.

    Priority:
      1) config sdwire.bin (absolute path)
      2) venv from VIRTUAL_ENV/bin/sdwire
      3) project venv /home/sparrow/robin/.venv/bin/sdwire
      4) common system paths
      5) PATH lookup via shutil.which
      6) fallback "sdwire" (will error, but we print a warning)
    """
    # 1) Config override
    if SDWIRE_BIN_CFG:
        if os.path.exists(SDWIRE_BIN_CFG) and os.access(SDWIRE_BIN_CFG, os.X_OK):
            return SDWIRE_BIN_CFG
        print(f"[{now_ts()}] [WARN] sdwire.bin set but not executable: {SDWIRE_BIN_CFG}")

    # 2) Activated venv
    venv = os.environ.get("VIRTUAL_ENV", "")
    if venv:
        cand = os.path.join(venv, "bin", "sdwire")
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            return cand

    # 3) Known project venv (yours)
    cand = os.path.join(PROJECT_ROOT, ".venv", "bin", "sdwire")
    if os.path.exists(cand) and os.access(cand, os.X_OK):
        return cand

    # 4) Common system locations
    for cand in ("/usr/local/bin/sdwire", "/usr/bin/sdwire"):
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            return cand

    # 5) PATH lookup
    found = shutil.which("sdwire")
    if found:
        return found

    # 6) final fallback
    return "sdwire"


SDWIRE = resolve_sdwire_bin()
if SDWIRE == "sdwire":
    print(f"[{now_ts()}] [WARN] Could not resolve sdwire binary. "
          f"Set config sdwire.bin to /home/sparrow/robin/.venv/bin/sdwire")


# ---- helpers ----
def run(cmd: list[str], check: bool = True, timeout: float = 60) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd))
    return subprocess.run(cmd, check=check, timeout=timeout)


def run_capture(cmd: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)


# ===== VBUS / power handling =====
# Pluggable backend (uhubctl or GPIO). Reads cfg['sdwire']['vbus'] if present,
# else falls back to the legacy cfg['sdwire']['vbus_pin'] integer (default 17).
_vbus = make_vbus(CFG.get("sdwire", {}), legacy_pin_default=SDWIRE_VBUS_PIN)
print(f"[sd_service] vbus={_vbus!r}")

SDWIRE_POWERED = False
MOUNTED = False


def sdwire_power_on() -> None:
    global SDWIRE_POWERED
    print(f"Powering SDWire USB ON via {_vbus!r}")
    _vbus.on()
    SDWIRE_POWERED = True
    time.sleep(POWER_ON_SETTLE_SEC)


def sdwire_power_off() -> None:
    global SDWIRE_POWERED
    print(f"Powering SDWire USB OFF via {_vbus!r}")
    _vbus.off()
    SDWIRE_POWERED = False
    time.sleep(POWER_OFF_SEC)


def lsusb_has_sdwire() -> bool:
    cp = run_capture(["lsusb"])
    return SDWIRE_VIDPID.lower() in (cp.stdout or "").lower()


def wait_for_sdwire_control(timeout_s: float = WAIT_FOR_SDWIRE_CTRL_SEC) -> bool:
    end = time.time() + timeout_s
    while time.time() < end:
        ok_usb = lsusb_has_sdwire()
        cp = run_capture([SDWIRE, "list"])
        ok_ctrl = (cp.returncode == 0) and ("Block Dev" in (cp.stdout or ""))
        if ok_usb and ok_ctrl:
            return True
        time.sleep(POLL_INTERVAL_SEC)
    return False


def wait_for_partition(path: str = SD_PARTITION, timeout_s: float = WAIT_FOR_PARTITION_SEC) -> bool:
    end = time.time() + timeout_s
    p = Path(path)
    while time.time() < end:
        if p.exists():
            return True
        time.sleep(POLL_INTERVAL_SEC)
    return False


def mount_partition() -> None:
    global MOUNTED
    ensure_dir(MOUNT_POINT)
    print(f"Mounting {SD_PARTITION} → {MOUNT_POINT}")
    run([
        "sudo", "mount",
        "-o", "uid=1000,gid=1000,umask=0022",
        SD_PARTITION,
        MOUNT_POINT
    ], check=True)
    MOUNTED = True


def unmount_partition() -> None:
    global MOUNTED
    if not MOUNTED:
        return
    print(f"Unmounting {MOUNT_POINT}")
    run(["sudo", "umount", MOUNT_POINT], check=False)
    MOUNTED = False


# ===== EXIF timestamp helper =====
def get_exif_datetime(filepath: str) -> Optional[datetime]:
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


# ===== Atomic write helper =====
def safe_write_final(dst_final: str, writer_fn) -> None:
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
        # Never leak a partial .part on failure (disk fill + blocks the collision loop).
        try:
            os.remove(part)
        except OSError:
            pass
        raise


def is_stable_file(p: Path, wait_s: float = STABLE_FILE_SECONDS) -> bool:
    try:
        s1 = p.stat().st_size
        time.sleep(wait_s)
        s2 = p.stat().st_size
        return s1 == s2
    except Exception:
        return False


def compress_jpeg_to_path(
    src_path: str,
    out_path: str,
    quality: int,
    max_dim: int,
    strip_exif: bool,
) -> None:
    with Image.open(src_path) as im:
        if im.mode != "RGB":
            im = im.convert("RGB")

        if max_dim and max(im.size) > max_dim:
            try:
                resample = Image.Resampling.LANCZOS
            except AttributeError:
                resample = Image.LANCZOS
            im.thumbnail((max_dim, max_dim), resample)

        save_kwargs = {
            "format": "JPEG",
            "quality": int(quality),
            "optimize": True,
            "progressive": True,
        }

        if strip_exif:
            im.save(out_path, **save_kwargs)
        else:
            exif = im.info.get("exif")
            if exif:
                im.save(out_path, exif=exif, **save_kwargs)
            else:
                im.save(out_path, **save_kwargs)


def process_and_save_images(src_root: Path, dest_dir: str) -> int:
    ensure_dir(dest_dir)

    run_count = 0
    counter = 1

    for root, _, files in os.walk(src_root):
        for fn in files:
            if Path(fn).suffix.lower() not in JPG_EXTS:
                continue

            src = os.path.join(root, fn)

            if not is_stable_file(Path(src)):
                print(f"[skip] still changing: {src}")
                continue

            dt = get_exif_datetime(src)
            if dt is None:
                dt = datetime.fromtimestamp(os.path.getmtime(src))
            timestamp = dt.strftime("%Y%m%d%H%M%S")

            seq = f"{counter:02d}"
            new_name = f"{seq}_{timestamp}.jpg"
            dst_final = os.path.join(dest_dir, new_name)

            suffix = 1
            base, ext = os.path.splitext(new_name)
            while os.path.exists(dst_final) or os.path.exists(dst_final + ".part"):
                new_name = f"{base}_{suffix}{ext}"
                dst_final = os.path.join(dest_dir, new_name)
                suffix += 1

            print(f"[xfer] {src} → {dst_final}")

            try:
                def _write(to_path: str) -> None:
                    if ENABLE_COMPRESSION:
                        compress_jpeg_to_path(
                            src_path=src,
                            out_path=to_path,
                            quality=JPEG_QUALITY,
                            max_dim=MAX_DIM,
                            strip_exif=STRIP_EXIF,
                        )
                    else:
                        shutil.copy2(src, to_path)

                safe_write_final(dst_final, _write)

                try:
                    os.remove(src)
                    print(f"[delete] {src}")
                except Exception as e:
                    print(f"[DELETE] Error deleting {src}: {e}")

                run_count += 1
                counter += 1

            except Exception as e:
                print(f"[COPY] Error processing {src}: {e}")
                continue

    print(f"Finished saving {run_count} file(s) this run.")
    return run_count


def run_one_cycle() -> int:
    """
    One SDWire import cycle with up to MAX_ATTEMPTS retries.
    Returns exit-like code: 0 success, 2 failed after retries, 3 DCIM missing.
    """
    ensure_dir(DEST_DIR)
    ensure_dir(MOUNT_POINT)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"\n=== Attempt {attempt}/{MAX_ATTEMPTS} ({now_ts()}) ===")

        sdwire_power_off()
        sdwire_power_on()

        if not wait_for_sdwire_control():
            print("[error] SDWire control not ready.")
            run(["lsusb"], check=False)
            run([SDWIRE, "list"], check=False)
            continue

        run([SDWIRE, "switch", "host"], check=True)
        time.sleep(0.75)

        if not wait_for_partition(SD_PARTITION, WAIT_FOR_PARTITION_SEC):
            print("[warn] Partition node not present. lsblk:")
            run(["lsblk"], check=False)
            continue

        mount_partition()

        mount_root = Path(MOUNT_POINT)
        src_root = mount_root / CAMERA_SUBDIR
        if not src_root.exists():
            print(f"[error] {src_root} not found. Top-level on card:")
            run(["bash", "-lc", f"ls -la {MOUNT_POINT}"], check=False)
            return 3

        saved = process_and_save_images(src_root, DEST_DIR)
        print(f"[done] saved={saved}")

        run(["sync"], check=False)
        unmount_partition()

        if SWITCH_BACK_TO_TARGET and SDWIRE_POWERED and lsusb_has_sdwire():
            run([SDWIRE, "switch", "target"], check=False)

        sdwire_power_off()
        return 0

    print("\n[error] Failed after retries.")
    run(["bash", "-lc", "dmesg | tail -n 80"], check=False)
    return 2


def _sd_cleanup() -> None:
    """Unmount the card and force VBUS OFF. Safe to call repeatedly (flag-guarded)."""
    try:
        unmount_partition()
    except Exception:
        pass
    try:
        if SWITCH_BACK_TO_TARGET and SDWIRE_POWERED and lsusb_has_sdwire():
            run([SDWIRE, "switch", "target"], check=False)
    except Exception:
        pass
    try:
        sdwire_power_off()
    except Exception:
        pass


def main() -> int:
    print(
        f"[{now_ts()}] sdwire_import interval={CAPTURE_INTERVAL_SEC}s dest={DEST_DIR} "
        f"compress={ENABLE_COMPRESSION} sdwire_bin={SDWIRE}"
    )

    try:
        while True:
            start = time.time()
            try:
                _ = run_one_cycle()
            except Exception as e:
                # A transient mount/switch/timeout error must not crash the service;
                # log, clean up (finally), and retry next cycle.
                print(f"[sd] cycle error: {e}")
            finally:
                # Guarantee unmount + VBUS OFF after EVERY cycle — including the
                # early-return (missing DCIM) and error paths inside run_one_cycle,
                # which otherwise leave USB power pinned on until the next cycle
                # (battery drain) and the card mounted when VBUS is later cut (FAT
                # corruption).
                _sd_cleanup()

            elapsed = time.time() - start
            sleep_s = CAPTURE_INTERVAL_SEC - elapsed
            if sleep_s > 0:
                print(f"[{now_ts()}] Sleeping {sleep_s:.1f}s…")
                time.sleep(sleep_s)
            else:
                print(f"[{now_ts()}] Cycle took {elapsed:.1f}s (>= interval), starting next cycle immediately…")

    finally:
        # Best-effort cleanup
        try:
            unmount_partition()
        except Exception:
            pass
        try:
            if SWITCH_BACK_TO_TARGET and SDWIRE_POWERED and lsusb_has_sdwire():
                run([SDWIRE, "switch", "target"], check=False)
        except Exception:
            pass
        try:
            sdwire_power_off()
        except Exception:
            pass
        try:
            _vbus.cleanup()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())