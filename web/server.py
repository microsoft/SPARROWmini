#!/usr/bin/env python3
"""
web/server.py

Tiny status web server. Anyone joined to the Robin Wi-Fi AP can hit http://10.42.0.1:8080 to see XBee link
status, current backlog, and a curated read-only summary of config.json.

Reads rssi.json (produced by
xbee_signal_monitor.py) and config/config.json on every request.
Serves web/index.html (loaded once at startup) as the page shell.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(WEB_DIR)

# Shared robustness helpers — cross-process-locked, atomic config read-modify-write.
sys.path.insert(0, os.path.join(PROJECT_DIR, "lib"))
from robust import update_config_atomic  # noqa: E402

CONFIG_PATH = os.path.join(PROJECT_DIR, "config", "config.json")
RSSI_PATH = "/home/sparrow/robin/rssi.json"
DETECTOR_STATUS_PATH = "/home/sparrow/robin/detector_status.json"
AUDIO_DETECTOR_STATUS_PATH = "/home/sparrow/robin/audio_detector_status.json"
UPLOADER_STATUS_PATH = "/home/sparrow/robin/uploader_status.json"
ONBOARDED_PATH = "/home/sparrow/robin/onboarded.json"
DEVICE_ID_PATH = os.path.join(PROJECT_DIR, "device_id.txt")
INDEX_PATH = os.path.join(WEB_DIR, "index.html")

LOG_DIR = os.path.join(PROJECT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "web_status.log")
DAILY_LOG_FILE = os.path.join(
    LOG_DIR, f"web_status_{datetime.now().strftime('%Y%m%d')}.log"
)

# -----------------------------------------------------------------------------
# Logging (same dual-handler pattern as audio_service.py)
# -----------------------------------------------------------------------------

logger = logging.getLogger("web_status")
logger.setLevel(logging.INFO)
logger.handlers.clear()
logger.propagate = False

_fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
for _h in (
    logging.StreamHandler(),
    logging.FileHandler(LOG_FILE),
    logging.FileHandler(DAILY_LOG_FILE),
):
    _h.setFormatter(_fmt)
    _h.setLevel(logging.INFO)
    logger.addHandler(_h)

# -----------------------------------------------------------------------------
# Static HTML shell (read once at startup)
# -----------------------------------------------------------------------------

try:
    INDEX_BYTES = Path(INDEX_PATH).read_bytes()
except OSError as e:
    logger.error("could not read %s: %s", INDEX_PATH, e)
    INDEX_BYTES = b"<!doctype html><title>Robin</title><h1>index.html missing</h1>"

LOGO_PATH = os.path.join(WEB_DIR, "logo.png")
try:
    LOGO_BYTES = Path(LOGO_PATH).read_bytes()
except OSError as e:
    logger.warning("could not read %s: %s", LOGO_PATH, e)
    LOGO_BYTES = b""

HELP_PATH = os.path.join(WEB_DIR, "help.html")
try:
    HELP_BYTES = Path(HELP_PATH).read_bytes()
except OSError as e:
    logger.warning("could not read %s: %s", HELP_PATH, e)
    HELP_BYTES = b"<!doctype html><title>Robin help</title><h1>help.html missing</h1>"

STYLES_PATH = os.path.join(WEB_DIR, "styles.css")
try:
    STYLES_BYTES = Path(STYLES_PATH).read_bytes()
except OSError as e:
    logger.warning("could not read %s: %s", STYLES_PATH, e)
    STYLES_BYTES = b"/* styles.css missing */"

# -----------------------------------------------------------------------------
# Actions (POST endpoints that mutate hardware state)
# -----------------------------------------------------------------------------

XBEE_CONFIGURE_PATH = os.path.join(PROJECT_DIR, "transport", "xbee_configure.py")
XBEE_SENDER_PATH = os.path.join(PROJECT_DIR, "transport", "xbee_node_sender_polled.py")
XBEE_SERIAL_LOCK = "/home/sparrow/robin/xbee_serial.lock"
ACTION_TIMEOUT_SEC = 120
LOCK_WAIT_SEC = 60


def _run_action_cmd(cmd, timeout):
    """Run a flock-wrapped radio action, capturing merged output, in its own
    process group so a timeout kills the whole tree. Otherwise a wedged
    xbee_configure / sender grandchild is orphaned still holding /dev/ttyUSB0 and
    the serial lock, and the sender/monitor can never talk to the radio again.
    Returns (returncode, output); returncode -1 means timed out."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, (out or "")
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except Exception:
                pass
        try:
            out, _ = proc.communicate(timeout=5)
        except Exception:
            out = ""
        return -1, (out or "") + f"\n[timed out after {timeout}s; killed process group]"


def _action_configure_xbee() -> dict:
    """Re-apply the documented client-node XBee configuration.

    Wraps the call in `flock -w 60` against the same xbee_serial.lock the
    sender and signal monitor use, so we never collide on /dev/ttyUSB0.
    Returns a dict suitable for JSON-serialising back to the page."""
    # Read the radio family from config so a 900-family node re-configures
    # correctly (xbee_configure skips the BR command for 900, which errors
    # on that family). Defaults to 868 if unset.
    xbee_cfg = (_read_json(CONFIG_PATH) or {}).get("xbee") or {}
    family = str(xbee_cfg.get("family") or "868").strip()
    if family not in ("868", "900"):
        family = "868"
    cmd = [
        "flock", "-E", "75", "-w", str(LOCK_WAIT_SEC), XBEE_SERIAL_LOCK,
        sys.executable, XBEE_CONFIGURE_PATH,
        "--port", "/dev/ttyUSB0",
        "--baud", "115200",
        "--api", "1",
        "--rf", "1",
        "--router", "0",
        "--netid", "1234",
        "--family", family,
    ]
    logger.info("action: configure_xbee starting (family=%s)", family)
    rc, output = _run_action_cmd(cmd, ACTION_TIMEOUT_SEC)
    if rc == -1:
        logger.warning("configure_xbee timed out after %ss", ACTION_TIMEOUT_SEC)
        return {"ok": False, "exit_code": -1, "output": output, "locked": False}
    if len(output) > 8000:
        output = "…[truncated]…\n" + output[-8000:]

    locked = (rc == 75)
    if locked:
        logger.info("configure_xbee: flock could not acquire lock in %ss", LOCK_WAIT_SEC)
    else:
        logger.info("configure_xbee finished rc=%s", rc)

    return {"ok": rc == 0, "exit_code": rc, "output": output, "locked": locked}


def _action_rediscover_master() -> dict:
    """Force a fresh discovery of the SPARROW hub and re-pin `xbee.dest64`.

    When the hub's XBee is swapped for a new chip it comes up with a new 64-bit
    address; the node stays pinned to the stale cached dest64 and can never hand
    off again. This runs the sender's one-shot `--rediscover` (which ignores the
    cached address, resolves the hub by NI, and writes the new address back),
    wrapped in the same `flock -w 60` the configure button uses so it can't
    collide with an in-progress send / signal poll on /dev/ttyUSB0."""
    cmd = [
        "flock", "-E", "75", "-w", str(LOCK_WAIT_SEC), XBEE_SERIAL_LOCK,
        sys.executable, XBEE_SENDER_PATH,
        "--config", CONFIG_PATH,
        "--rediscover",
    ]
    logger.info("action: rediscover_master starting")
    rc, output = _run_action_cmd(cmd, ACTION_TIMEOUT_SEC)
    if rc == -1:
        logger.warning("rediscover_master timed out after %ss", ACTION_TIMEOUT_SEC)
        return {"ok": False, "exit_code": -1, "output": output, "locked": False, "dest64": None}
    if len(output) > 8000:
        output = "…[truncated]…\n" + output[-8000:]

    locked = (rc == 75)
    # Read back the freshly-written address so the page can show what it locked onto.
    dest64 = None
    try:
        dest64 = ((_read_json(CONFIG_PATH) or {}).get("xbee") or {}).get("dest64") or None
    except Exception:
        pass

    if locked:
        logger.info("rediscover_master: flock could not acquire lock in %ss", LOCK_WAIT_SEC)
    else:
        logger.info("rediscover_master finished rc=%s dest64=%s", rc, dest64)

    return {"ok": rc == 0, "exit_code": rc, "output": output, "locked": locked, "dest64": dest64}

# -----------------------------------------------------------------------------
# Server settings + onboarding (writes config.json and access_key.txt)
# -----------------------------------------------------------------------------

# Single in-process lock so two simultaneous POSTs don't collide on the
# config.json read-modify-write or stomp the access_key.txt rename.
_SERVER_SETTINGS_LOCK = threading.Lock()

ACCESS_KEY_FILE_DEFAULT = "access_key.txt"

# The only two modes are `relay` (XBee to the SPARROW hub) and `direct`
# (HTTPS straight to SPARROW Studio).
_VALID_MODES = {"relay", "direct"}


def _normalize_mode(raw) -> str:
    s = str(raw or "").lower().strip()
    return s or "relay"


def _resolve_auth_key_path(server_cfg: dict) -> str:
    """Where access_key.txt lives — mirrors uploader_service.resolve_auth_key."""
    path = str(server_cfg.get("auth_key_file") or ACCESS_KEY_FILE_DEFAULT)
    if not os.path.isabs(path):
        path = os.path.join(PROJECT_DIR, path)
    return path


def _write_text_atomic(path: str, content: str, mode: int = 0o644) -> None:
    """Atomic write: .tmp -> rename, set mode."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _update_config_server(updates: dict) -> dict:
    """Read config.json, set keys under top-level `server`, write atomically.

    Only whitelisted keys are touched so a malformed POST body can't bleed into
    other config sections. Returns the updated server block.
    """
    allowed = {"base_url", "unit_id", "image_endpoint_path", "metrics_endpoint_path", "send_metrics"}
    clean = {k: v for k, v in (updates or {}).items() if k in allowed and v is not None}

    def _mutate(cfg):
        server = cfg.get("server") or {}
        server.update(clean)
        cfg["server"] = server

    try:
        # Cross-process lock + re-read + atomic write: never a torn file, never a
        # lost update against the sender writing xbee.dest64 concurrently.
        cfg = update_config_atomic(CONFIG_PATH, _mutate)
    except Exception as e:
        raise RuntimeError(f"could not update config.json: {e}")
    return cfg.get("server") or {}


def _resolve_unit_id(server: dict) -> str:
    explicit = str((server or {}).get("unit_id") or "").strip()
    if explicit:
        return explicit
    # Fall back to robin's local device_id.txt (12-char hex).
    return _read_device_id()


def _read_server_block() -> dict:
    cfg = _read_json(CONFIG_PATH) or {}
    return cfg.get("server") or {}


def _has_access_key(server: dict) -> bool:
    path = _resolve_auth_key_path(server)
    try:
        return bool(Path(path).read_text(encoding="utf-8").strip())
    except OSError:
        return False


def _action_save_server_settings(body: dict) -> dict:
    """POST /api/actions/save_server_settings — update endpoint + (optionally) key."""
    with _SERVER_SETTINGS_LOCK:
        updates = {}
        if "base_url" in body:
            base_url = str(body.get("base_url") or "").strip()
            if base_url and not base_url.lower().startswith(("http://", "https://")):
                return {"ok": False, "error": "base_url must start with http:// or https://"}
            if base_url:
                updates["base_url"] = base_url.rstrip("/")
        if "unit_id" in body:
            updates["unit_id"] = str(body.get("unit_id") or "").strip()

        try:
            server = _update_config_server(updates) if updates else _read_server_block()
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}

        wrote_key = False
        api_key = (body.get("api_key") or "").strip()
        if api_key:
            key_path = _resolve_auth_key_path(server)
            try:
                _write_text_atomic(key_path, api_key + "\n", mode=0o600)
                wrote_key = True
                logger.info("wrote access key to %s", key_path)
            except Exception as e:
                logger.exception("could not write access key: %s", e)
                return {"ok": False, "error": f"could not write access key: {e}"}

    return {
        "ok": True,
        "base_url": server.get("base_url"),
        "unit_id": server.get("unit_id") or "",
        "has_key": wrote_key or _has_access_key(server),
    }


def _action_set_mode(body: dict) -> dict:
    """POST /api/actions/set_mode — flip the top-level `mode` in config.json.

    The launcher re-reads config every cycle, so the flip takes effect at the
    next iteration boundary (within `launcher.loop_sleep_sec`).
    """
    raw = body.get("mode") if isinstance(body, dict) else None
    new_mode = _normalize_mode(raw)
    if new_mode not in _VALID_MODES:
        return {"ok": False, "error": f"mode must be one of {sorted(_VALID_MODES)}"}

    old_mode_box = {}

    def _mutate(cfg):
        old_mode_box["v"] = _normalize_mode(cfg.get("mode"))
        cfg["mode"] = new_mode

    with _SERVER_SETTINGS_LOCK:
        try:
            cfg = update_config_atomic(CONFIG_PATH, _mutate)
        except Exception as e:
            return {"ok": False, "error": f"could not update config.json: {e}"}
    old_mode = old_mode_box.get("v", "relay")

    loop_sleep_sec = None
    try:
        loop_sleep_sec = int((cfg.get("launcher") or {}).get("loop_sleep_sec", 1800))
    except Exception:
        pass

    logger.info("mode set via API: %s -> %s (was on disk)", old_mode, new_mode)
    return {
        "ok": True,
        "mode": new_mode,
        "previous": old_mode,
        "takes_effect": "next cycle",
        "loop_sleep_sec": loop_sleep_sec,
    }


def _action_set_keep_blanks(body: dict) -> dict:
    """POST /api/actions/set_keep_blanks — toggle blank-forwarding for a detector.

    Body: {"target": "image"|"audio", "keep_blanks": true|false}.
    Writes `detector.keep_blanks` (image) or `audio_detector.keep_blanks` (audio)
    in config.json. Each detector re-reads config on its next `--once` run, so the
    change is actioned at the next launcher cycle (within `launcher.loop_sleep_sec`).
    """
    target = str((body or {}).get("target") or "").lower().strip()
    section = {"image": "detector", "audio": "audio_detector"}.get(target)
    if section is None:
        return {"ok": False, "error": "target must be 'image' or 'audio'"}
    if not isinstance(body, dict) or "keep_blanks" not in body:
        return {"ok": False, "error": "keep_blanks (bool) is required"}
    keep = bool(body.get("keep_blanks"))

    old_box = {}

    def _mutate(cfg):
        sect = cfg.get(section)
        if not isinstance(sect, dict):
            sect = {}
        old_box["v"] = bool(sect.get("keep_blanks", False))
        sect["keep_blanks"] = keep
        cfg[section] = sect

    with _SERVER_SETTINGS_LOCK:
        try:
            cfg = update_config_atomic(CONFIG_PATH, _mutate)
        except Exception as e:
            return {"ok": False, "error": f"could not update config.json: {e}"}
    old = old_box.get("v", False)

    loop_sleep_sec = None
    try:
        loop_sleep_sec = int((cfg.get("launcher") or {}).get("loop_sleep_sec", 1800))
    except Exception:
        pass

    logger.info("keep_blanks[%s] set via API: %s -> %s", target, old, keep)
    return {
        "ok": True,
        "target": target,
        "keep_blanks": keep,
        "previous": old,
        "takes_effect": "next cycle",
        "loop_sleep_sec": loop_sleep_sec,
    }


# Audio recording controls exposed on the web portal. MODE picks the trigger;
# the numeric fields are validated against these inclusive ranges before any write.
# Clips ship over XBee, so recording length is hard-capped: a long clip is
# minutes of radio airtime. audio_service.py clamps to the same ceiling at
# runtime (defense-in-depth). Keep MAX_RECORD_SEC in sync with that module.
MAX_RECORD_SEC = 10
_AUDIO_MODES = {"schedule", "threshold"}
_AUDIO_INT_FIELDS = {
    "THRESHOLD": (1, 32767),                  # int16 peak amplitude (threshold/VOX mode)
    "RECORD_DURATION": (1, MAX_RECORD_SEC),   # seconds per triggered clip (threshold mode)
    "interval_minutes": (1, 1440),            # SCHEDULE: record once every N minutes
    "duration_seconds": (1, MAX_RECORD_SEC),  # SCHEDULE: clip length in seconds
}


def _coerce_audio_int(name: str, raw, lo: int, hi: int) -> int:
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer")
    if val < lo or val > hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return val


def _action_set_audio(body: dict) -> dict:
    """POST /api/actions/set_audio — set the audio recording trigger + params.

    Body may contain any of: MODE ("schedule"|"threshold"), THRESHOLD,
    RECORD_DURATION, interval_minutes, duration_seconds. The last two live under
    `audio.SCHEDULE`; the rest are top-level in `audio`. Recording lengths are
    capped at MAX_RECORD_SEC (XBee airtime). audio_service.py re-reads config on
    its loop, so the change is actioned at its next iteration.
    """
    if not isinstance(body, dict):
        return {"ok": False, "error": "body must be a JSON object"}

    # Validate everything up front so a bad field aborts before touching config.
    updates_top: dict = {}
    updates_sched: dict = {}
    if "MODE" in body:
        m = str(body.get("MODE") or "").lower().strip()
        if m not in _AUDIO_MODES:
            return {"ok": False, "error": f"MODE must be one of {sorted(_AUDIO_MODES)}"}
        updates_top["MODE"] = m
    try:
        for name in ("THRESHOLD", "RECORD_DURATION"):
            if name in body:
                lo, hi = _AUDIO_INT_FIELDS[name]
                updates_top[name] = _coerce_audio_int(name, body.get(name), lo, hi)
        for name in ("interval_minutes", "duration_seconds"):
            if name in body:
                lo, hi = _AUDIO_INT_FIELDS[name]
                updates_sched[name] = _coerce_audio_int(name, body.get(name), lo, hi)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    if not updates_top and not updates_sched:
        return {"ok": False, "error": "no recognised audio fields to update"}

    def _mutate(cfg):
        aud = cfg.get("audio")
        if not isinstance(aud, dict):
            aud = {}
        sched = aud.get("SCHEDULE")
        if not isinstance(sched, dict):
            sched = {}
        aud.update(updates_top)
        sched.update(updates_sched)
        # Migrate off the legacy minute-granularity key: once a seconds value is
        # set, drop duration_minutes so the two never disagree.
        if "duration_seconds" in sched:
            sched.pop("duration_minutes", None)
        iv = sched.get("interval_minutes")
        du = sched.get("duration_seconds")
        if isinstance(iv, int) and isinstance(du, int) and du > iv * 60:
            raise ValueError("duration_seconds must be <= interval (interval_minutes * 60)")
        aud["SCHEDULE"] = sched
        cfg["audio"] = aud

    with _SERVER_SETTINGS_LOCK:
        try:
            cfg = update_config_atomic(CONFIG_PATH, _mutate)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": f"could not update config.json: {e}"}

    aud_now = cfg.get("audio", {}) if isinstance(cfg, dict) else {}
    sched_now = aud_now.get("SCHEDULE", {}) if isinstance(aud_now, dict) else {}
    loop_sleep_sec = None
    try:
        loop_sleep_sec = int((cfg.get("launcher") or {}).get("loop_sleep_sec", 1800))
    except Exception:
        pass

    logger.info(
        "audio settings set via API: MODE=%s THRESHOLD=%s RECORD_DURATION=%s SCHEDULE=%s",
        aud_now.get("MODE"), aud_now.get("THRESHOLD"),
        aud_now.get("RECORD_DURATION"), sched_now,
    )
    return {
        "ok": True,
        "audio": {
            "MODE": aud_now.get("MODE"),
            "THRESHOLD": aud_now.get("THRESHOLD"),
            "RECORD_DURATION": aud_now.get("RECORD_DURATION"),
            "interval_minutes": sched_now.get("interval_minutes"),
            "duration_seconds": sched_now.get("duration_seconds"),
        },
        "takes_effect": "next cycle",
        "loop_sleep_sec": loop_sleep_sec,
    }


def _http_post_json(url: str, headers: dict, body: dict, timeout_sec: float) -> tuple[int | None, str]:
    """Stdlib JSON POST. Returns (status, raw_body). status None on transport failure."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body_text = (e.read() or b"").decode("utf-8", errors="replace")
        except Exception:
            body_text = ""
        return e.code, body_text
    except urllib.error.URLError as e:
        return None, f"network error: {e.reason}"
    except Exception as e:
        return None, f"unexpected error: {e}"


def _extract_detail(body_text: str) -> str:
    """FastAPI puts the human-readable error in {'detail': '...'} — surface it."""
    try:
        j = json.loads(body_text)
    except Exception:
        return body_text.strip() or "(no body)"
    if isinstance(j, dict) and "detail" in j:
        return str(j["detail"])
    return body_text.strip() or "(no body)"


def _action_onboard(body: dict) -> dict:
    """POST /api/actions/onboard — consume the onboarding key and bind this unit_id.

    Body: { "email": "..." }. The API key must already be on disk
    (via save_server_settings or out-of-band). server.base_url must be set.
    On 200/201, writes onboarded.json with the server's response for the UI.
    """
    email = str((body or {}).get("email") or "").strip()
    if not email:
        return {"ok": False, "error": "email is required"}

    with _SERVER_SETTINGS_LOCK:
        server = _read_server_block()
        base_url = str(server.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            return {"ok": False, "error": "server.base_url is not set — save settings first"}

        key_path = _resolve_auth_key_path(server)
        try:
            api_key = Path(key_path).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return {"ok": False, "error": f"access key file not found at {key_path}"}
        except Exception as e:
            return {"ok": False, "error": f"could not read access key: {e}"}
        if not api_key:
            return {"ok": False, "error": f"access key file {key_path} is empty"}

        unit_id = _resolve_unit_id(server)
        if not unit_id:
            return {"ok": False, "error": "unit_id is empty (no override, and device_id.txt is missing)"}

        url = base_url + "/v1/onboarding"
        headers = {"X-API-Key": api_key, "X-Email": email}
        logger.info("onboarding POST %s unit_id=%s email=%s", url, unit_id, email)
        status, raw = _http_post_json(url, headers, {"unit_id": unit_id}, timeout_sec=15.0)

        result: dict = {"status_code": status, "raw_body": raw}
        if status in (200, 201):
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {}
            result["ok"] = True
            result["onboarded"] = {
                "project_id": parsed.get("project_id"),
                "device_id": parsed.get("device_id"),
                "unit_id": parsed.get("unit_id") or unit_id,
                "idempotent": bool(parsed.get("idempotent")),
                "base_url": base_url,
                "onboarded_at": time.time(),
            }
            try:
                _write_text_atomic(ONBOARDED_PATH, json.dumps(result["onboarded"]))
            except Exception as e:
                logger.warning("could not write %s: %s", ONBOARDED_PATH, e)
            logger.info("onboarding ok: %s", result["onboarded"])
            return result

        result["ok"] = False
        result["error"] = _extract_detail(raw) if status is not None else raw
        logger.warning("onboarding failed: status=%s detail=%s", status, result["error"])
        return result


# -----------------------------------------------------------------------------
# File readers
# -----------------------------------------------------------------------------

def _read_json(path: str):
    try:
        text = Path(path).read_text(encoding="utf-8")
        return json.loads(text)
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def _read_device_id() -> str:
    try:
        return Path(DEVICE_ID_PATH).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return ""


# -----------------------------------------------------------------------------
# Backlog count (cached briefly; scandir on SD-pressured dir can stall)
# -----------------------------------------------------------------------------

_BACKLOG_CACHE: dict = {"ts": 0.0, "value": 0, "dir": None}
_BACKLOG_TTL = 5.0
_BACKLOG_LOCK = threading.Lock()


def _backlog_count(dest_dir: str) -> int:
    with _BACKLOG_LOCK:
        now = time.time()
        if (
            _BACKLOG_CACHE["dir"] == dest_dir
            and now - _BACKLOG_CACHE["ts"] < _BACKLOG_TTL
        ):
            return _BACKLOG_CACHE["value"]

        n = 0
        try:
            with os.scandir(dest_dir) as it:
                for entry in it:
                    if entry.is_file() and entry.name.lower().endswith(".jpg"):
                        n += 1
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            n = 0

        _BACKLOG_CACHE["ts"] = now
        _BACKLOG_CACHE["value"] = n
        _BACKLOG_CACHE["dir"] = dest_dir
        return n


# -----------------------------------------------------------------------------
# Link state derivation
# -----------------------------------------------------------------------------

def _link_state(rssi, interval_sec: float) -> dict:
    if not rssi or "timestamp" not in rssi:
        return {
            "ok": False,
            "rssi_dbm": None,
            "note": "no data",
            "age_sec": None,
            "state": "unknown",
        }

    try:
        ts = float(rssi.get("timestamp", 0.0))
    except (TypeError, ValueError):
        ts = 0.0

    age = max(0.0, time.time() - ts)
    raw_ok = bool(rssi.get("ok", False))
    dbm = rssi.get("rssi_dbm")
    note = str(rssi.get("note", ""))

    if raw_ok and age < 3.0 * interval_sec:
        state = "ok"
    elif age < 5.0 * interval_sec:
        state = "stale"
    else:
        state = "lost"

    return {
        "ok": raw_ok,
        "rssi_dbm": dbm if isinstance(dbm, (int, float)) else None,
        "note": note,
        "age_sec": round(age, 1),
        "state": state,
    }


# -----------------------------------------------------------------------------
# Status payload (curated allowlist -- never dump raw config)
# -----------------------------------------------------------------------------

def _pick(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _detector_view(cfg: dict) -> dict:
    """Curated detector status for the web UI.

    Pulls the live `enabled`/`keep_blanks` from config.json so the page reflects
    operator changes even before the next detector cycle, then layers the
    last_run summary the detector wrote on its previous pass.
    """
    det_cfg = cfg.get("detector", {}) if isinstance(cfg, dict) else {}
    status = _read_json(DETECTOR_STATUS_PATH) or {}

    enabled = bool(det_cfg.get("enabled", False))
    keep_blanks = bool(det_cfg.get("keep_blanks", False))

    model_dir = str(det_cfg.get("model_dir", "")).rstrip("/")
    model = status.get("model") or (model_dir.rsplit("/", 1)[-1] if model_dir else None)

    return {
        "enabled": enabled,
        "keep_blanks": keep_blanks,
        "model": model,
        "labels": status.get("labels") or [],
        "conf_threshold": det_cfg.get("conf_threshold"),
        "input_size": det_cfg.get("input_size"),
        "last_run": status.get("last_run"),
        "last_run_at": status.get("timestamp"),
    }


def _audio_detector_view(cfg: dict) -> dict:
    """Curated audio-detector status for the web UI.

    Mirrors _detector_view: live `enabled`/`keep_blanks` come from config.json so
    the page reflects operator changes immediately, then the last_run summary the
    audio detector wrote on its previous pass is layered on top.
    """
    aud_cfg = cfg.get("audio_detector", {}) if isinstance(cfg, dict) else {}
    status = _read_json(AUDIO_DETECTOR_STATUS_PATH) or {}

    enabled = bool(aud_cfg.get("enabled", False))
    keep_blanks = bool(aud_cfg.get("keep_blanks", False))

    model_dir = str(aud_cfg.get("model_dir", "")).rstrip("/")
    model = status.get("model") or (model_dir.rsplit("/", 1)[-1] if model_dir else None)

    return {
        "enabled": enabled,
        "keep_blanks": keep_blanks,
        "model": model,
        "labels": status.get("labels") or [],
        "conf_threshold": aud_cfg.get("conf_threshold"),
        "window_sec": aud_cfg.get("window_sec"),
        "sample_rate": aud_cfg.get("sample_rate"),
        "last_run": status.get("last_run"),
        "last_run_at": status.get("timestamp"),
    }


def _audio_view(cfg: dict) -> dict:
    """Curated audio-recording (capture) config for the web UI.

    This is the *trigger* side (audio_service.py) — distinct from the
    audio_detector view, which is the AI classification side. Values come
    straight from config.json so the page reflects operator edits immediately.
    """
    aud = cfg.get("audio", {}) if isinstance(cfg, dict) else {}
    sched = aud.get("SCHEDULE", {}) if isinstance(aud, dict) else {}
    mode = str(aud.get("MODE", "schedule")).lower().strip()
    if mode not in _AUDIO_MODES:
        mode = "schedule"
    # Effective schedule clip length in seconds — prefer the seconds key, fall
    # back to a legacy duration_minutes so a not-yet-migrated node still reports.
    dur_s = sched.get("duration_seconds")
    if dur_s is None and sched.get("duration_minutes") is not None:
        try:
            dur_s = int(sched["duration_minutes"]) * 60
        except (TypeError, ValueError):
            dur_s = None
    return {
        "enabled": bool(aud.get("enabled", False)),
        "mode": mode,
        "threshold": aud.get("THRESHOLD"),
        "record_duration_sec": aud.get("RECORD_DURATION"),
        "interval_minutes": sched.get("interval_minutes"),
        "duration_seconds": dur_s,
        "max_record_sec": MAX_RECORD_SEC,
        "sample_rate": aud.get("SAMPLE_RATE"),
        "encode_bitrate_kbps": aud.get("encode_bitrate_kbps"),
    }


def _onboarding_view(cfg: dict) -> dict:
    """Curated onboarding state for the drawer UI. Never leaks the api key."""
    server = cfg.get("server", {}) if isinstance(cfg, dict) else {}
    onboarded = _read_json(ONBOARDED_PATH) or {}

    return {
        "base_url": str(server.get("base_url") or "").strip(),
        "unit_id": _resolve_unit_id(server),
        "unit_id_source": "config" if str(server.get("unit_id") or "").strip() else "device_id.txt",
        "has_key": _has_access_key(server),
        "is_onboarded": bool(onboarded.get("project_id")),
        "onboarded": {
            "project_id": onboarded.get("project_id"),
            "device_id": onboarded.get("device_id"),
            "unit_id": onboarded.get("unit_id"),
            "onboarded_at": onboarded.get("onboarded_at"),
            "base_url": onboarded.get("base_url"),
        } if onboarded else None,
    }


def _receivers_view(cfg: dict) -> dict:
    """FTP + SMTP push-receiver status for the web UI. IP cameras push images
    into dest_dir over these, alongside USB/SDWire capture. Enabled state comes
    from config; the launcher keeps them running when enabled."""
    ftp = cfg.get("ftp", {}) if isinstance(cfg, dict) else {}
    email = cfg.get("email", {}) if isinstance(cfg, dict) else {}
    return {
        "ftp_enabled": bool(ftp.get("enabled", False)),
        "ftp_port": ftp.get("port", 21),
        "email_enabled": bool(email.get("enabled", False)),
        "email_port": 2525,
    }


def _upload_view(cfg: dict) -> dict:
    """Curated upload status for the direct-mode UI card.

    Never includes the auth_key, even though it lives next to base_url in
    config — the secret stays in access_key.txt and out of /api/status.
    """
    server = cfg.get("server", {}) if isinstance(cfg, dict) else {}
    status = _read_json(UPLOADER_STATUS_PATH) or {}

    base_url = str(server.get("base_url", "")).rstrip("/")
    endpoint = status.get("endpoint") or (
        base_url + str(server.get("image_endpoint_path", "/v1/uploads/image")) if base_url else None
    )
    metrics_endpoint = status.get("metrics_endpoint")
    if metrics_endpoint is None and server.get("send_metrics") and base_url:
        metrics_endpoint = base_url + str(server.get("metrics_endpoint_path", "/v1/metrics/system"))

    return {
        "endpoint": endpoint,
        "metrics_endpoint": metrics_endpoint,
        "online": status.get("online"),
        "last_success_at": status.get("last_success_at"),
        "last_metrics_at": status.get("last_metrics_at"),
        "last_run": status.get("last_run"),
        "status_at": status.get("timestamp"),
        "error": status.get("error"),
    }


def _build_status() -> dict:
    cfg = _read_json(CONFIG_PATH) or {}
    rssi = _read_json(RSSI_PATH)

    interval_sec = float(_pick(cfg, "signal_monitor", "interval_sec", default=15.0))
    dest_dir = str(_pick(cfg, "dest_dir", default="/home/sparrow/robin/images"))
    mode = _normalize_mode(cfg.get("mode", "relay"))

    curated = {
        "mode": mode,
        "capture_mode": _pick(cfg, "capture_manager", "capture_mode"),
        "image": {
            "enable_compression": _pick(cfg, "image", "enable_compression"),
            "jpeg_quality": _pick(cfg, "image", "jpeg_quality"),
            "max_dim": _pick(cfg, "image", "max_dim"),
        },
        "launcher": {
            "loop_sleep_sec": _pick(cfg, "launcher", "loop_sleep_sec"),
        },
    }

    if mode == "direct":
        curated["server"] = {
            "base_url": _pick(cfg, "server", "base_url"),
            "image_endpoint_path": _pick(cfg, "server", "image_endpoint_path"),
        }
    else:
        curated["xbee"] = {
            "family": _pick(cfg, "xbee", "family", default="868"),
            "master_ni": _pick(cfg, "xbee", "master_ni"),
            "dest64": _pick(cfg, "xbee", "dest64"),
            "window": _pick(cfg, "xbee", "window"),
            "chunk": _pick(cfg, "xbee", "chunk"),
        }
        curated["signal_monitor"] = {"interval_sec": interval_sec}

    payload = {
        "now": time.time(),
        "device_id": _read_device_id(),
        "mode": mode,
        "backlog": {"count": _backlog_count(dest_dir)},
        "detector": _detector_view(cfg),
        "audio": _audio_view(cfg),
        "audio_detector": _audio_detector_view(cfg),
        "receivers": _receivers_view(cfg),
        "onboarding": _onboarding_view(cfg),
        "config": curated,
    }
    if mode == "direct":
        payload["upload"] = _upload_view(cfg)
    else:
        payload["link"] = _link_state(rssi, interval_sec)

    return payload


# -----------------------------------------------------------------------------
# HTTP handler
# -----------------------------------------------------------------------------

class StatusHandler(BaseHTTPRequestHandler):
    server_version = "RobinStatus/1.0"
    # Per-connection socket timeout: a slow/half-open client on the open AP can't
    # tie up a handler thread forever (slowloris) on the resource-constrained Pi.
    timeout = 30

    def log_message(self, fmt, *args):
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, INDEX_BYTES, "text/html; charset=utf-8",
                           cache="max-age=300")
                return
            if path in ("/help", "/help.html"):
                self._send(200, HELP_BYTES, "text/html; charset=utf-8",
                           cache="max-age=300")
                return
            if path == "/styles.css":
                self._send(200, STYLES_BYTES, "text/css; charset=utf-8",
                           cache="max-age=300")
                return
            if path == "/api/status":
                payload = json.dumps(_build_status()).encode("utf-8")
                self._send(200, payload, "application/json; charset=utf-8")
                return
            if path == "/healthz":
                self._send(200, b"OK\n", "text/plain; charset=utf-8")
                return
            if path == "/logo.png":
                if LOGO_BYTES:
                    self._send(200, LOGO_BYTES, "image/png", cache="max-age=86400")
                else:
                    self._send(404, b"no logo\n", "text/plain; charset=utf-8")
                return
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
        except Exception:
            logger.exception("handler error for %s", self.path)
            try:
                self._send(503, b"transient error\n", "text/plain; charset=utf-8")
            except Exception:
                pass

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        # Cap body size — these endpoints accept tiny JSON only.
        if length > 16 * 1024:
            raise ValueError(f"body too large ({length} bytes)")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"invalid JSON: {e}")
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    def do_POST(self):
        try:
            path = self.path.split("?", 1)[0]

            if path == "/api/actions/configure_xbee":
                # Drain any body for this action (it takes none) before responding.
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > 0:
                    self.rfile.read(length)
                result = _action_configure_xbee()
                payload = json.dumps(result).encode("utf-8")
                self._send(200, payload, "application/json; charset=utf-8")
                return

            if path == "/api/actions/rediscover_master":
                # Takes no body; drain any before responding.
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > 0:
                    self.rfile.read(length)
                result = _action_rediscover_master()
                payload = json.dumps(result).encode("utf-8")
                self._send(200, payload, "application/json; charset=utf-8")
                return

            if path == "/api/actions/save_server_settings":
                try:
                    body = self._read_json_body()
                except ValueError as e:
                    self._send(400, json.dumps({"ok": False, "error": str(e)}).encode("utf-8"),
                               "application/json; charset=utf-8")
                    return
                result = _action_save_server_settings(body)
                self._send(200 if result.get("ok") else 400,
                           json.dumps(result).encode("utf-8"),
                           "application/json; charset=utf-8")
                return

            if path == "/api/actions/set_mode":
                try:
                    body = self._read_json_body()
                except ValueError as e:
                    self._send(400, json.dumps({"ok": False, "error": str(e)}).encode("utf-8"),
                               "application/json; charset=utf-8")
                    return
                result = _action_set_mode(body)
                self._send(200 if result.get("ok") else 400,
                           json.dumps(result).encode("utf-8"),
                           "application/json; charset=utf-8")
                return

            if path == "/api/actions/set_keep_blanks":
                try:
                    body = self._read_json_body()
                except ValueError as e:
                    self._send(400, json.dumps({"ok": False, "error": str(e)}).encode("utf-8"),
                               "application/json; charset=utf-8")
                    return
                result = _action_set_keep_blanks(body)
                self._send(200 if result.get("ok") else 400,
                           json.dumps(result).encode("utf-8"),
                           "application/json; charset=utf-8")
                return

            if path == "/api/actions/set_audio":
                try:
                    body = self._read_json_body()
                except ValueError as e:
                    self._send(400, json.dumps({"ok": False, "error": str(e)}).encode("utf-8"),
                               "application/json; charset=utf-8")
                    return
                result = _action_set_audio(body)
                self._send(200 if result.get("ok") else 400,
                           json.dumps(result).encode("utf-8"),
                           "application/json; charset=utf-8")
                return

            if path == "/api/actions/onboard":
                try:
                    body = self._read_json_body()
                except ValueError as e:
                    self._send(400, json.dumps({"ok": False, "error": str(e)}).encode("utf-8"),
                               "application/json; charset=utf-8")
                    return
                result = _action_onboard(body)
                self._send(200 if result.get("ok") else 400,
                           json.dumps(result).encode("utf-8"),
                           "application/json; charset=utf-8")
                return

            # Unknown path — drain body to avoid leaving a half-read socket.
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > 0:
                self.rfile.read(length)
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
        except Exception:
            logger.exception("POST handler error for %s", self.path)
            try:
                self._send(503, b"transient error\n", "text/plain; charset=utf-8")
            except Exception:
                pass


# -----------------------------------------------------------------------------
# Server lifecycle (retry binding -- wlan0 may not be up yet at boot)
# -----------------------------------------------------------------------------

class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _bind_with_retry(preferred_host: str, fallback_host: str, port: int,
                     attempts: int = 6, delay_sec: float = 5.0) -> ThreadingHTTPServer:
    for i in range(1, attempts + 1):
        try:
            srv = _Server((preferred_host, port), StatusHandler)
            logger.info("bound to %s:%d", preferred_host, port)
            return srv
        except OSError as e:
            logger.warning("bind %s:%d failed (attempt %d/%d): %s",
                           preferred_host, port, i, attempts, e)
            time.sleep(delay_sec)

    logger.warning("falling back to %s:%d after %d failed binds on %s",
                   fallback_host, port, attempts, preferred_host)
    return _Server((fallback_host, port), StatusHandler)


def main():
    cfg = _read_json(CONFIG_PATH) or {}
    web_cfg = cfg.get("web_status", {}) if isinstance(cfg, dict) else {}
    host = str(web_cfg.get("host", "10.42.0.1"))
    port = int(web_cfg.get("port", 8080))

    server = _bind_with_retry(preferred_host=host, fallback_host="0.0.0.0", port=port)

    def _stop(_signum, _frame):
        logger.info("signal received, shutting down")
        threading.Thread(target=server.shutdown, daemon=True).start()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _stop)
        except Exception:
            pass

    logger.info("web_status serving on http://%s:%d/ (PID %d)",
                server.server_address[0], server.server_address[1], os.getpid())

    try:
        server.serve_forever()
    finally:
        try:
            server.server_close()
        except Exception:
            pass
        logger.info("web_status stopped")


if __name__ == "__main__":
    main()
