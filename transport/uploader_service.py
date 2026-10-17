#!/usr/bin/env python3
"""
uploader_service.py

Direct-mode counterpart to xbee_node_sender_polled.py. When mode == "direct",
robin has direct internet (Wi-Fi / cellular) and uploads its own captures
straight to the SPARROW cloud endpoint.

Per cycle:
  1. Drain new images from dest_dir to {base_url}{image_endpoint_path}
     (default /v1/uploads/image), one POST per image with:
        Headers: X-API-Key, X-Unit-ID, Idempotency-Key
        Multipart body: file=jpeg, metadata=<detector sidecar JSON>
  2. If server.send_metrics, also POST one system-metrics reading to
     {base_url}{metrics_endpoint_path} (default /v1/metrics/system) with:
        Headers: X-API-Key, X-Unit-ID
        JSON body: psutil snapshot + BME688 latest reading + Pi CPU temp

Skips a JPG until detector_service has written its <name>.jpg.det.json sidecar.
Connectivity is gated by a raw-socket check (mirrors sparrow's is_server_online).
On 2xx: deletes the JPG + sidecar + .compressed marker. On failure: keeps
everything for the next launcher cycle (no retry caps).

Auth contract: matches src/api/sparrow_routes.py in the sparrow server.
  X-API-Key = contents of server.auth_key_file
  X-Unit-ID = server.unit_id (falls back to device_id.txt when blank)
The server's `discarded: true` privacy response (200 with save_person=false)
is treated as a successful upload — the file is deleted just like a 201.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
_LIB_DIR = os.path.join(PROJECT_ROOT, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from device_id_util import read_device_id  # noqa: E402
from robust import quarantine_file  # noqa: E402  (dead-letter permanent 4xx rejects)

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "config.json")

SERVER_DEFAULTS = {
    "base_url": "https://server.sparrow-earth.com",
    "image_endpoint_path": "/v1/uploads/image",
    "audio_endpoint_path": "/v1/uploads/audio",
    "metrics_endpoint_path": "/v1/metrics/system",
    "auth_key_file": "access_key.txt",
    "unit_id": "",
    "send_metrics": True,
    "connectivity_timeout_sec": 8,
    "upload_timeout_sec": 30,
    "retry_sleep_sec": 5,
    "status_file": "/home/sparrow/robin/uploader_status.json",
}


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

def _setup_logger() -> logging.Logger:
    log_dir = os.path.join(PROJECT_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "uploader.log")
    daily_file = os.path.join(
        log_dir, f"uploader_{datetime.now().strftime('%Y%m%d')}.log"
    )

    lg = logging.getLogger("uploader")
    lg.setLevel(logging.INFO)
    lg.handlers.clear()
    lg.propagate = False

    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    from logging.handlers import RotatingFileHandler
    for h in (
        logging.StreamHandler(),
        RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3),
    ):
        h.setFormatter(fmt)
        h.setLevel(logging.INFO)
        lg.addHandler(h)
    return lg


log = _setup_logger()


# -----------------------------------------------------------------------------
# Config helpers
# -----------------------------------------------------------------------------

def load_cfg() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error("could not read %s: %s", CONFIG_PATH, e)
        return {}


def server_cfg(cfg: dict) -> dict:
    raw = cfg.get("server", {}) if isinstance(cfg, dict) else {}
    merged = dict(SERVER_DEFAULTS)
    merged.update({k: v for k, v in raw.items() if v is not None})
    return merged


def resolve_auth_key(server: dict) -> Optional[str]:
    path = str(server.get("auth_key_file", "access_key.txt"))
    if not os.path.isabs(path):
        path = os.path.join(PROJECT_ROOT, path)
    try:
        key = Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        log.error("auth key file not found at %s — create it with the X-API-Key value", path)
        return None
    except Exception as e:
        log.error("could not read auth key from %s: %s", path, e)
        return None
    if not key:
        log.error("auth key file %s is empty", path)
        return None
    return key


def resolve_unit_id(server: dict, cfg: dict) -> Optional[str]:
    """X-Unit-ID is the device_name bound during /v1/onboarding.

    server.unit_id wins; falls back to the local device_id.txt. If neither
    resolves, the cycle is a no-op (header auth will fail server-side anyway).
    """
    explicit = str(server.get("unit_id", "")).strip()
    if explicit:
        return explicit

    device_id_file = cfg.get("device_id_file", "/home/sparrow/robin/device_id.txt")
    try:
        return read_device_id(device_id_file)
    except Exception as e:
        log.error("could not read device_id from %s: %s", device_id_file, e)
        return None


# -----------------------------------------------------------------------------
# Backlog walk + connectivity
# -----------------------------------------------------------------------------

def _oldest_stable_with_sidecar(
    pool_dir: Path,
    extensions: tuple[str, ...],
    stable_sec: float,
    sidecar_suffix: str,
) -> Iterable[Path]:
    """Yield files (oldest first) matching extensions, stable on disk, with sidecar.

    Future mtimes (camera-clock ahead of Pi) are accepted as stable — only
    positive age < stable_sec is treated as "still being written".
    """
    if not pool_dir.exists():
        return
    now = time.time()
    candidates = sorted(
        (p for p in pool_dir.iterdir()
         if p.is_file() and p.suffix.lower() in extensions),
        key=lambda p: p.stat().st_mtime,
    )
    for p in candidates:
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        if st.st_size <= 0:
            continue
        age = now - st.st_mtime
        if 0 <= age < stable_sec:
            continue
        if not p.with_name(p.name + sidecar_suffix).exists():
            continue
        yield p


def oldest_stable_jpgs(dest_dir: Path, stable_sec: float, sidecar_suffix: str) -> Iterable[Path]:
    """Stable JPGs with a `<name>.jpg.det.json` sidecar, oldest first."""
    return _oldest_stable_with_sidecar(dest_dir, (".jpg", ".jpeg"), stable_sec, sidecar_suffix)


def oldest_stable_mp3s(audio_dir: Path, stable_sec: float, sidecar_suffix: str) -> Iterable[Path]:
    """Stable MP3s with a `<name>.mp3.det.json` sidecar, oldest first.

    The audio detector deletes the WAV after encoding, so MP3 + sidecar is
    the canonical state for upload-eligible audio.
    """
    return _oldest_stable_with_sidecar(audio_dir, (".mp3",), stable_sec, sidecar_suffix)


def is_server_online(url: str, timeout_s: float) -> bool:
    """Raw socket reachability check. Mirrors rest_client.is_server_online."""
    parsed = urlparse(url)
    host = parsed.hostname
    scheme = (parsed.scheme or "http").lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except Exception as e:
        log.warning("connectivity check failed for %s: %s", url, e)
        return False


# -----------------------------------------------------------------------------
# Status file
# -----------------------------------------------------------------------------

def write_status_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


# -----------------------------------------------------------------------------
# Image upload
# -----------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def upload_one(
    image_path: Path,
    sidecar_path: Path,
    endpoint: str,
    auth_key: str,
    unit_id: str,
    upload_timeout_sec: float,
) -> tuple[bool, bool]:
    """POST one image + sidecar metadata. Returns (success, discarded).

    Auth: X-API-Key + X-Unit-ID headers. Idempotency-Key = sha256(file) so
    a retry of the same bytes is a server-side no-op.
    Discarded (200 + save_person=false response) counts as success for cleanup.
    """
    name = image_path.name
    try:
        meta = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("could not read sidecar %s: %s", sidecar_path, e)
        return False, False

    try:
        idem = _sha256_file(image_path)
    except Exception as e:
        log.warning("sha256 failed for %s: %s — proceeding without Idempotency-Key", image_path, e)
        idem = None

    headers = {
        "X-API-Key": auth_key,
        "X-Unit-ID": unit_id,
    }
    if idem:
        headers["Idempotency-Key"] = idem

    try:
        with image_path.open("rb") as fh:
            files = {"file": (name, fh, "image/jpeg")}
            data = {"metadata": json.dumps(meta)}
            n_det = len(meta.get("detections", []))
            log.info(
                "POST %s name=%s detections=%d captured_at=%s",
                endpoint, name, n_det, meta.get("captured_at", "")
            )
            resp = requests.post(
                endpoint, files=files, data=data,
                headers=headers, timeout=upload_timeout_sec,
            )
            resp.raise_for_status()
            try:
                body = resp.json()
            except ValueError:
                body = {}
    except requests.exceptions.HTTPError as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        log.error("upload failed for %s: HTTP %s", name, code)
        if isinstance(code, int) and 400 <= code < 500 and code != 429:
            # Permanent client error (bad metadata / too large): this file will never
            # be accepted, so quarantine it instead of re-POSTing it every cycle forever.
            quarantine_file(str(image_path), reason=f"HTTP {code} (permanent reject)",
                            extra_suffixes=(".compressed", ".det.json"))
        return False, False
    except requests.exceptions.RequestException as e:
        log.error("upload failed for %s: %s", name, e)
        return False, False
    except Exception as e:
        log.exception("unexpected error uploading %s: %s", name, e)
        return False, False

    discarded = bool(body.get("discarded"))
    if discarded:
        log.info("server discarded %s: %s", name, body.get("reason", ""))
    else:
        log.info("uploaded %s (%d detections, image_id=%s)",
                 name, n_det, body.get("image_id"))
    return True, discarded


def cleanup_after_upload(image_path: Path, sidecar_path: Path) -> None:
    for p in (image_path, sidecar_path, image_path.with_name(image_path.name + ".compressed")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("could not delete %s: %s", p, e)


# -----------------------------------------------------------------------------
# Audio upload
# -----------------------------------------------------------------------------

def _audio_metadata_from_sidecar(sidecar: dict, mp3_name: str) -> dict:
    """Transform the audio detector's sidecar into the wire payload the server
    expects at /v1/uploads/audio.

    Server contract (per sparrow_routes): {audio_name, detected_at, label, confidence}.
    The detector's sidecar carries the full per-window detections list, so we
    distil it down to `label`=first detection's label (always "bird" for the
    binary classifier today) and `confidence`=max across windows.
    """
    detections = sidecar.get("detections") or []
    if detections:
        label = detections[0].get("label", "bird")
        confidence = max(
            (float(d.get("confidence", 0.0)) for d in detections),
            default=0.0,
        )
    else:
        # Blank kept by keep_blanks=true. Server can store the row for stats.
        label = "blank"
        confidence = 0.0
    return {
        "audio_name": sidecar.get("audio_name", mp3_name),
        "detected_at": sidecar.get("captured_at"),
        "label": label,
        "confidence": round(confidence, 4),
    }


def upload_audio_one(
    audio_path: Path,
    sidecar_path: Path,
    endpoint: str,
    auth_key: str,
    unit_id: str,
    upload_timeout_sec: float,
) -> tuple[bool, bool]:
    """POST one MP3 + audio metadata. Returns (success, discarded).

    Mirrors upload_one's auth + idempotency machinery; only the endpoint, MIME
    type, and metadata transform differ.
    """
    name = audio_path.name
    try:
        raw_sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("could not read audio sidecar %s: %s", sidecar_path, e)
        return False, False

    meta = _audio_metadata_from_sidecar(raw_sidecar, name)

    try:
        idem = _sha256_file(audio_path)
    except Exception as e:
        log.warning("sha256 failed for %s: %s — proceeding without Idempotency-Key", audio_path, e)
        idem = None

    headers = {
        "X-API-Key": auth_key,
        "X-Unit-ID": unit_id,
    }
    if idem:
        headers["Idempotency-Key"] = idem

    try:
        with audio_path.open("rb") as fh:
            files = {"file": (name, fh, "audio/mpeg")}
            data = {"metadata": json.dumps(meta)}
            log.info(
                "POST %s name=%s label=%s confidence=%.3f detected_at=%s",
                endpoint, name, meta["label"], meta["confidence"],
                meta.get("detected_at") or "",
            )
            resp = requests.post(
                endpoint, files=files, data=data,
                headers=headers, timeout=upload_timeout_sec,
            )
            resp.raise_for_status()
            try:
                body = resp.json()
            except ValueError:
                body = {}
    except requests.exceptions.HTTPError as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        log.error("audio upload failed for %s: HTTP %s", name, code)
        if isinstance(code, int) and 400 <= code < 500 and code != 429:
            quarantine_file(str(audio_path), reason=f"HTTP {code} (permanent reject)",
                            extra_suffixes=(".det.json",))
        return False, False
    except requests.exceptions.RequestException as e:
        log.error("audio upload failed for %s: %s", name, e)
        return False, False
    except Exception as e:
        log.exception("unexpected error uploading audio %s: %s", name, e)
        return False, False

    discarded = bool(body.get("discarded"))
    if discarded:
        log.info("server discarded audio %s: %s", name, body.get("reason", ""))
    else:
        log.info("uploaded audio %s (audio_id=%s)", name, body.get("audio_id"))
    return True, discarded


def cleanup_after_audio_upload(audio_path: Path, sidecar_path: Path) -> None:
    # No `.compressed` marker for audio — MP3 is the only compression step
    # and it's done inside the detector, not the sender.
    for p in (audio_path, sidecar_path):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("could not delete %s: %s", p, e)


# -----------------------------------------------------------------------------
# System metrics
# -----------------------------------------------------------------------------

def _read_cpu_temp_c() -> Optional[float]:
    """Pi CPU temperature in °C from the standard sysfs thermal zone."""
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        return round(int(raw) / 1000.0, 2)
    except Exception:
        return None


def _read_bme688_latest(cfg: dict) -> dict:
    """Best-effort read of readings/latest.json written by sensor_service.

    Returns the dict as-is on success, {} on any failure. The dashboard
    extracts `bme688_temperature_celsius` etc. via field-name fallbacks so
    we forward the names as sensor_service writes them (t_c, rh_pct, etc.)
    while also re-keying them under the server's expected names.
    """
    bme_cfg = cfg.get("bme688", {})
    readings_dir = bme_cfg.get("readings_dir", os.path.join(PROJECT_ROOT, "readings"))
    p = Path(readings_dir) / "latest.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("could not read %s: %s", p, e)
        return {}


def _gather_metrics(cfg: dict, unit_id: str) -> dict:
    """Build the JSON payload for /v1/metrics/system.

    Field naming follows the server's legacy-firmware fallbacks where
    possible so the dashboard's Chart.js extraction (cpu_temp_c,
    bme688_temperature_celsius) lights up automatically.
    """
    try:
        import psutil
    except ImportError:
        psutil = None  # psutil isn't required for image uploads to work

    metrics: dict = {
        "unique_id": unit_id,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cpu_temp_c": _read_cpu_temp_c(),
    }

    if psutil is not None:
        try:
            metrics["cpu_usage"] = psutil.cpu_percent(interval=0.2)
            metrics["memory_usage"] = psutil.virtual_memory().percent
            metrics["disk_usage"] = psutil.disk_usage("/").percent
            metrics["uptime_seconds"] = int(time.time() - psutil.boot_time())
        except Exception as e:
            log.warning("psutil snapshot failed: %s", e)

    bme = _read_bme688_latest(cfg)
    if bme:
        # Pass through the raw blob...
        metrics["bme688"] = bme
        # ...and surface the server-known field names so Chart.js picks them up.
        # sensor_service.py writes temperature_c / humidity_pct / pressure_pa.
        t_c = bme.get("temperature_c")
        rh = bme.get("humidity_pct")
        p_pa = bme.get("pressure_pa")
        if isinstance(t_c, (int, float)):
            metrics["bme688_temperature_celsius"] = float(t_c)
        if isinstance(rh, (int, float)):
            metrics["bme688_humidity_percent"] = float(rh)
        if isinstance(p_pa, (int, float)):
            metrics["bme688_pressure_pa"] = float(p_pa)

    return metrics


def post_metrics(
    cfg: dict,
    endpoint: str,
    auth_key: str,
    unit_id: str,
    timeout_sec: float,
) -> Optional[float]:
    """POST one system-metrics reading. Returns the unix-ts on success, None on failure."""
    payload = _gather_metrics(cfg, unit_id)
    headers = {
        "X-API-Key": auth_key,
        "X-Unit-ID": unit_id,
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=timeout_sec)
        resp.raise_for_status()
        log.info("metrics posted to %s (status=%s)", endpoint, resp.status_code)
        return time.time()
    except requests.exceptions.RequestException as e:
        log.warning("metrics POST failed: %s", e)
        return None


# -----------------------------------------------------------------------------
# Main cycle
# -----------------------------------------------------------------------------

def _build_status(
    image_endpoint: str,
    metrics_endpoint: Optional[str],
    online: bool,
    last_success_at: Optional[float],
    last_metrics_at: Optional[float],
    uploaded: int,
    discarded: int,
    failed: int,
    skipped: int,
    duration_sec: float,
    error: Optional[str] = None,
    audio_endpoint: Optional[str] = None,
    audio_uploaded: int = 0,
    audio_discarded: int = 0,
    audio_failed: int = 0,
) -> dict:
    payload = {
        "timestamp": time.time(),
        "endpoint": image_endpoint,
        "audio_endpoint": audio_endpoint,
        "metrics_endpoint": metrics_endpoint,
        "online": online,
        "last_success_at": last_success_at,
        "last_metrics_at": last_metrics_at,
        "last_run": {
            "uploaded": uploaded,
            "discarded": discarded,
            "failed": failed,
            "skipped": skipped,
            "duration_sec": round(duration_sec, 2),
            "audio_uploaded": audio_uploaded,
            "audio_discarded": audio_discarded,
            "audio_failed": audio_failed,
        },
    }
    if error:
        payload["error"] = error
    return payload


def run_once(cfg: dict) -> dict:
    server = server_cfg(cfg)
    image_endpoint = server["base_url"].rstrip("/") + str(server["image_endpoint_path"])
    audio_endpoint = server["base_url"].rstrip("/") + str(server["audio_endpoint_path"])
    metrics_endpoint = (
        server["base_url"].rstrip("/") + str(server["metrics_endpoint_path"])
        if server.get("send_metrics") else None
    )
    upload_timeout_sec = float(server["upload_timeout_sec"])
    connectivity_timeout_sec = float(server["connectivity_timeout_sec"])
    retry_sleep_sec = float(server["retry_sleep_sec"])

    dest_dir = Path(cfg.get("dest_dir", "/home/sparrow/robin/images"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = Path(cfg.get("audio_dir", "/home/sparrow/robin/audio"))
    audio_dir.mkdir(parents=True, exist_ok=True)
    stable_sec = float(cfg.get("image", {}).get("stable_file_sec", 1.0))
    sidecar_suffix = str(cfg.get("detector", {}).get("sidecar_suffix", ".det.json"))
    audio_sidecar_suffix = str(
        cfg.get("audio_detector", {}).get("sidecar_suffix", sidecar_suffix)
    )

    t_start = time.time()
    uploaded = 0
    discarded_count = 0
    failed = 0
    skipped = 0
    audio_uploaded = 0
    audio_discarded = 0
    audio_failed = 0
    last_success_at: Optional[float] = None
    last_metrics_at: Optional[float] = None
    online = False

    auth_key = resolve_auth_key(server)
    if auth_key is None:
        return _build_status(
            image_endpoint, metrics_endpoint,
            online=False, last_success_at=None, last_metrics_at=None,
            uploaded=0, discarded=0, failed=0, skipped=0,
            duration_sec=time.time() - t_start, error="auth key missing",
            audio_endpoint=audio_endpoint,
        )

    unit_id = resolve_unit_id(server, cfg)
    if not unit_id:
        return _build_status(
            image_endpoint, metrics_endpoint,
            online=False, last_success_at=None, last_metrics_at=None,
            uploaded=0, discarded=0, failed=0, skipped=0,
            duration_sec=time.time() - t_start, error="unit_id missing",
            audio_endpoint=audio_endpoint,
        )

    online = is_server_online(server["base_url"], connectivity_timeout_sec)
    if not online:
        log.warning("server %s unreachable; will retry next cycle", server["base_url"])
        return _build_status(
            image_endpoint, metrics_endpoint,
            online=False, last_success_at=None, last_metrics_at=None,
            uploaded=0, discarded=0, failed=0, skipped=0,
            duration_sec=time.time() - t_start,
            audio_endpoint=audio_endpoint,
        )

    # 1) Images
    for jpg in oldest_stable_jpgs(dest_dir, stable_sec, sidecar_suffix):
        sidecar = jpg.with_name(jpg.name + sidecar_suffix)
        ok, was_discarded = upload_one(
            jpg, sidecar, image_endpoint, auth_key, unit_id, upload_timeout_sec,
        )
        if ok:
            cleanup_after_upload(jpg, sidecar)
            if was_discarded:
                discarded_count += 1
            else:
                uploaded += 1
            last_success_at = time.time()
        else:
            failed += 1
            if not is_server_online(server["base_url"], connectivity_timeout_sec):
                log.warning("server went offline mid-cycle; stopping for retry next tick")
                online = False
                break
            time.sleep(retry_sleep_sec)

    # 2) Audio (MP3 + audio sidecar). Separate endpoint; auth identical.
    if online:
        for mp3 in oldest_stable_mp3s(audio_dir, stable_sec, audio_sidecar_suffix):
            side = mp3.with_name(mp3.name + audio_sidecar_suffix)
            ok, was_discarded = upload_audio_one(
                mp3, side, audio_endpoint, auth_key, unit_id, upload_timeout_sec,
            )
            if ok:
                cleanup_after_audio_upload(mp3, side)
                if was_discarded:
                    audio_discarded += 1
                else:
                    audio_uploaded += 1
                last_success_at = time.time()
            else:
                audio_failed += 1
                if not is_server_online(server["base_url"], connectivity_timeout_sec):
                    log.warning("server went offline mid-cycle (audio); stopping for retry next tick")
                    online = False
                    break
                time.sleep(retry_sleep_sec)

    # 3) Metrics (best-effort; upload health is independent)
    if metrics_endpoint and online:
        ts = post_metrics(cfg, metrics_endpoint, auth_key, unit_id, upload_timeout_sec)
        if ts is not None:
            last_metrics_at = ts

    duration = time.time() - t_start
    summary = _build_status(
        image_endpoint, metrics_endpoint,
        online=online,
        last_success_at=last_success_at,
        last_metrics_at=last_metrics_at,
        uploaded=uploaded,
        discarded=discarded_count,
        failed=failed,
        skipped=skipped,
        duration_sec=duration,
        audio_endpoint=audio_endpoint,
        audio_uploaded=audio_uploaded,
        audio_discarded=audio_discarded,
        audio_failed=audio_failed,
    )
    log.info(
        "cycle done: img(up=%d disc=%d fail=%d) audio(up=%d disc=%d fail=%d) "
        "metrics=%s in %.2fs online=%s",
        uploaded, discarded_count, failed,
        audio_uploaded, audio_discarded, audio_failed,
        "ok" if last_metrics_at else "skip",
        duration, online,
    )
    return summary


def _merge_preserved(summary: dict, status_path: Path) -> dict:
    """Carry forward last_success_at / last_metrics_at across cycles that
    didn't upload anything, so the UI stops showing '—' the moment one
    succeeds."""
    try:
        prev = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return summary
    if summary.get("last_success_at") is None and isinstance(prev.get("last_success_at"), (int, float)):
        summary["last_success_at"] = prev["last_success_at"]
    if summary.get("last_metrics_at") is None and isinstance(prev.get("last_metrics_at"), (int, float)):
        summary["last_metrics_at"] = prev["last_metrics_at"]
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Robin direct-mode HTTPS uploader.")
    ap.add_argument("--once", action="store_true",
                    help="Drain the backlog then exit (the launcher uses this).")
    args = ap.parse_args()

    cfg = load_cfg()
    server = server_cfg(cfg)
    status_path = Path(server["status_file"])

    summary = run_once(cfg)
    summary = _merge_preserved(summary, status_path)
    try:
        write_status_atomic(status_path, summary)
    except Exception as e:
        log.warning("could not write status file %s: %s", status_path, e)

    if not args.once:
        scan_interval = float(cfg.get("server", {}).get("scan_interval_sec", 30.0))
        while True:
            time.sleep(scan_interval)
            summary = run_once(cfg)
            summary = _merge_preserved(summary, status_path)
            try:
                write_status_atomic(status_path, summary)
            except Exception as e:
                log.warning("could not write status file %s: %s", status_path, e)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
