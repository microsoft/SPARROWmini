#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
import time
import json
import logging
from datetime import datetime

# Backstop timeouts (seconds) for each --once stage. A config value of 0/unset
# means "use these" — NOT "no timeout" — so a wedged child can never freeze the
# launcher forever. Generous, since normal cycles finish far faster.
DEFAULT_CAPTURE_TIMEOUT_SEC = 300.0
DEFAULT_DETECTOR_TIMEOUT_SEC = 600.0
DEFAULT_FORWARDER_TIMEOUT_SEC = 900.0

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Shared helper modules live in lib/ now; put it on the path for the import below.
LIB_DIR = os.path.join(BASE_DIR, "lib")
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

from device_id_util import ensure_device_id

# ---- logging ----
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "launcher.log")
DAILY_LOG_FILE = os.path.join(LOG_DIR, f"launcher_{datetime.now().strftime('%Y%m%d')}.log")

logger = logging.getLogger("launcher")
logger.setLevel(logging.INFO)
logger.handlers.clear()
logger.propagate = False

fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

console_handler = logging.StreamHandler()
console_handler.setFormatter(fmt)
console_handler.setLevel(logging.INFO)

# Rotating file handler so launcher.log can't grow without bound on a node that
# runs for months. (The old per-day handler computed the date once at startup and
# then wrote to that same dated file forever — replaced by size-based rotation.)
from logging.handlers import RotatingFileHandler  # noqa: E402
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3)
file_handler.setFormatter(fmt)
file_handler.setLevel(logging.INFO)

logger.addHandler(console_handler)
logger.addHandler(file_handler)
# -----------------

CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.json")

def normalize_mode(raw) -> str:
    # The only two modes are `relay` (XBee to the SPARROW hub) and `direct`
    # (HTTPS straight to SPARROW Studio). Anything unset defaults to relay.
    s = str(raw or "").lower().strip()
    return s or "relay"

CAMERA_CAPTURE_SCRIPT = "capture/usb_import_service.py"
SDWIRE_CAPTURE_SCRIPT = "capture/sd_service.py"
DETECTOR_SCRIPT = "detect/detector_service.py"
AUDIO_DETECTOR_SCRIPT = "detect/audio_detector_service.py"
AUDIO_SERVICE_SCRIPT = "capture/audio_service.py"
XBEE_SENDER_SCRIPT = "transport/xbee_node_sender_polled.py"
UPLOADER_SCRIPT = "transport/uploader_service.py"
SIGNAL_MONITOR_SCRIPT = "transport/xbee_signal_monitor.py"
WEB_STATUS_SCRIPT = "web/server.py"
FTP_SERVER_SCRIPT = "capture/ftp_server.py"
EMAIL_SERVER_SCRIPT = "capture/email_server.py"


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"No config found at {CONFIG_PATH}; using defaults.")
        return {}
    except Exception as e:
        logger.error(f"Could not read config: {e}")
        return {}


def get_capture_mode(cfg: dict) -> str:
    cm = cfg.get("capture_manager", {}) if isinstance(cfg, dict) else {}
    mode = cm.get("capture_mode", "sdwire")
    if mode not in ("sdwire", "camera-usb"):
        logger.warning(f"Unknown capture_manager.capture_mode={mode!r}; defaulting to 'sdwire'")
        mode = "sdwire"
    return mode


def get_python() -> str:
    py = os.path.join(BASE_DIR, ".venv", "bin", "python")
    if os.path.exists(py):
        return py
    return sys.executable


def _kill_process_group(proc) -> None:
    """Kill the child AND anything it spawned (mount/arecord/sdwire/...). The child
    is started in its own session, so os.killpg reaps the whole tree — otherwise a
    timed-out stage can leave orphaned grandchildren holding the mount or radio."""
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def run_script_blocking(script_name: str, extra_args=None, timeout=None) -> int:
    script_path = os.path.join(BASE_DIR, script_name)
    if not os.path.exists(script_path):
        logger.warning(f"Skipping missing script: {script_path}")
        return 0

    py = get_python()
    cmd = [py, "-u", script_path]
    if extra_args:
        cmd.extend(extra_args)

    logger.info(f"Running: {' '.join(cmd)}")
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=BASE_DIR,
            stdout=sys.stdout,
            stderr=sys.stderr,
            start_new_session=True,   # own process group → killpg reaps grandchildren
        )
        rc = proc.wait(timeout=timeout)
        logger.info(f"{script_name} exited with code {rc}")
        return rc
    except subprocess.TimeoutExpired:
        logger.error(f"{script_name} timed out after {timeout}s; killing process group")
        _kill_process_group(proc)
        return 124
    except KeyboardInterrupt:
        _kill_process_group(proc)
        raise
    except Exception as e:
        logger.error(f"Failed to run {script_name}: {e}")
        _kill_process_group(proc)
        return 1


def start_background_script(script_name: str, extra_args=None) -> subprocess.Popen | None:
    script_path = os.path.join(BASE_DIR, script_name)
    if not os.path.exists(script_path):
        logger.warning(f"Missing background script: {script_path}")
        return None

    py = get_python()
    cmd = [py, "-u", script_path]
    if extra_args:
        cmd.extend(extra_args)

    logger.info(f"Starting background: {' '.join(cmd)}")
    try:
        return subprocess.Popen(
            cmd,
            cwd=BASE_DIR,
            stdout=sys.stdout,
            stderr=sys.stderr,
            start_new_session=True,   # own process group → clean group-kill on stop
        )
    except Exception as e:
        logger.error(f"Failed to start background script {script_name}: {e}")
        return None


def stop_background(proc: subprocess.Popen | None, name: str) -> None:
    if proc is None:
        return

    try:
        if proc.poll() is None:
            logger.info(f"Stopping background {name}")
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                logger.info(f"Killing background {name}")
                _kill_process_group(proc)
    except Exception as e:
        logger.warning(f"Error stopping {name}: {e}")


def reconcile_bg(proc: subprocess.Popen | None, want: bool, name: str) -> subprocess.Popen | None:
    """Make a config-gated background service match `want`, every cycle:
    start it if it should run and isn't, restart it if it crashed, stop it if
    it shouldn't run. Returns the (possibly new) proc handle, or None.
    Used for the FTP/SMTP push receivers, which run in both transport modes."""
    if want:
        if proc is None or proc.poll() is not None:
            if proc is not None:
                logger.warning(f"{name} exited (rc={proc.returncode}); restarting")
            return start_background_script(name)
        return proc
    if proc is not None:
        stop_background(proc, name)
    return None


def main():
    dev_id_path = os.path.join(BASE_DIR, "device_id.txt")
    dev_id = ensure_device_id(dev_id_path)
    logger.info(f"Device ID: {dev_id}")

    signal_monitor_proc = None
    web_status_proc = None
    audio_service_proc = None
    ftp_proc = None
    email_proc = None
    current_bg_mode: str | None = None  # mode the backgrounds were last started in
    audio_enabled_current: bool | None = None  # last-seen audio.enabled flag

    try:
        # web/server.py runs in both modes — start once at the top and let it survive flips.
        web_status_proc = start_background_script(WEB_STATUS_SCRIPT)

        while True:
            logger.info("==== New launcher cycle ====")
            cycle_sleep_sec = 60.0  # safe fallback if a cycle aborts early
            try:

                # Re-read config every cycle so a mode flip / threshold tweak in the
                # web UI takes effect at the next iteration boundary.
                cfg = load_config()
                mode = normalize_mode(cfg.get("mode", "relay"))
                transport = cfg.get("transport", "wifi")
                capture_mode = get_capture_mode(cfg)

                launcher_cfg = cfg.get("launcher", {})
                loop_sleep_sec = float(launcher_cfg.get("loop_sleep_sec", 1800.0))
                post_capture_delay_sec = float(launcher_cfg.get("post_capture_delay_sec", 3.0))
                capture_timeout_sec = float(launcher_cfg.get("capture_timeout_sec", 0))
                detector_timeout_sec = float(launcher_cfg.get("detector_timeout_sec", 0))
                xbee_sender_timeout_sec = float(launcher_cfg.get("xbee_sender_timeout_sec", 0))
                uploader_timeout_sec = float(launcher_cfg.get("uploader_timeout_sec", 0))

                detector_enabled = bool(cfg.get("detector", {}).get("enabled", False))
                audio_detector_enabled = bool(cfg.get("audio_detector", {}).get("enabled", False))
                audio_enabled = bool(cfg.get("audio", {}).get("enabled", False))
                is_direct = mode == "direct"
                forwarder_script = UPLOADER_SCRIPT if is_direct else XBEE_SENDER_SCRIPT
                forwarder_timeout_sec = uploader_timeout_sec if is_direct else xbee_sender_timeout_sec
                capture_script = SDWIRE_CAPTURE_SCRIPT if capture_mode == "sdwire" else CAMERA_CAPTURE_SCRIPT

                # First cycle: announce the resolved config. Subsequent cycles: only log
                # if the mode actually changed (avoid log noise on every cycle).
                if current_bg_mode is None:
                    logger.info(f"Config → mode={mode} transport={transport} capture_mode={capture_mode}")
                    logger.info(f"Capture selection: {capture_mode} → {capture_script}")
                    logger.info(f"Detector enabled: {detector_enabled}")
                    logger.info(f"Forwarder selection: mode={mode} → {forwarder_script}")
                elif mode != current_bg_mode:
                    logger.info(f"Mode flipped: {current_bg_mode} → {mode}")
                    logger.info(f"Forwarder selection: mode={mode} → {forwarder_script}")

                # Reconcile mode-gated background services. Only xbee_signal_monitor is
                # mode-gated — runs in relay mode (XBee is in use), idle in direct mode.
                if mode != current_bg_mode:
                    if mode == "relay":
                        if signal_monitor_proc is None or signal_monitor_proc.poll() is not None:
                            signal_monitor_proc = start_background_script(
                                SIGNAL_MONITOR_SCRIPT, extra_args=["--loop"]
                            )
                    else:
                        if signal_monitor_proc is not None:
                            stop_background(signal_monitor_proc, SIGNAL_MONITOR_SCRIPT)
                            signal_monitor_proc = None
                        else:
                            logger.info("Direct mode: XBee signal monitor not running (radio not in use).")
                    current_bg_mode = mode

                # Reconcile the audio recorder background. Independent of mode — runs
                # in both relay and direct, gated only on audio.enabled. Threshold or
                # scheduled recording drives capture cadence inside audio_service.
                if audio_enabled != audio_enabled_current:
                    if audio_enabled:
                        if audio_service_proc is None or audio_service_proc.poll() is not None:
                            audio_service_proc = start_background_script(AUDIO_SERVICE_SCRIPT)
                    else:
                        if audio_service_proc is not None:
                            stop_background(audio_service_proc, AUDIO_SERVICE_SCRIPT)
                            audio_service_proc = None
                        else:
                            logger.info("Audio disabled (audio.enabled=false); recorder not running.")
                    audio_enabled_current = audio_enabled
                elif audio_enabled and audio_service_proc is not None and audio_service_proc.poll() is not None:
                    # Crashed since last cycle — re-spawn so a transient arecord error
                    # doesn't leave the recorder permanently down.
                    logger.warning(f"{AUDIO_SERVICE_SCRIPT} exited (rc={audio_service_proc.returncode}); restarting")
                    audio_service_proc = start_background_script(AUDIO_SERVICE_SCRIPT)

                # Reconcile the push-receiver backgrounds. IP cameras (e.g. Reolink)
                # push images over FTP / SMTP into dest_dir, in addition to whatever
                # USB/SDWire capture runs below — so a node can do both at once. Both
                # run in relay and direct, gated on ftp.enabled / email.enabled.
                ftp_proc   = reconcile_bg(ftp_proc,   bool(cfg.get("ftp", {}).get("enabled", False)),   FTP_SERVER_SCRIPT)
                email_proc = reconcile_bg(email_proc, bool(cfg.get("email", {}).get("enabled", False)), EMAIL_SERVER_SCRIPT)

                # Keep the status/control web server alive — restart it if it died
                # (and reap it) so the UI can't be permanently lost to one crash.
                if web_status_proc is not None and web_status_proc.poll() is not None:
                    logger.warning(f"{WEB_STATUS_SCRIPT} exited (rc={web_status_proc.returncode}); restarting")
                    web_status_proc = start_background_script(WEB_STATUS_SCRIPT)
                elif web_status_proc is None:
                    web_status_proc = start_background_script(WEB_STATUS_SCRIPT)

                # 1) Capture/import all images first
                capture_timeout = capture_timeout_sec if capture_timeout_sec > 0 else DEFAULT_CAPTURE_TIMEOUT_SEC
                capture_rc = run_script_blocking(capture_script, extra_args=["--once"], timeout=capture_timeout)
                if capture_rc != 0:
                    logger.warning(f"Capture step returned non-zero exit code: {capture_rc}")

                # 2) Small settle delay after capture/USB changes
                if post_capture_delay_sec > 0:
                    logger.info(f"Waiting {post_capture_delay_sec}s after capture...")
                    time.sleep(post_capture_delay_sec)

                # 3) Run on-device detector (drops blanks if detector.keep_blanks=false,
                #    writes <name>.jpg.det.json sidecar otherwise)
                if detector_enabled:
                    det_timeout = detector_timeout_sec if detector_timeout_sec > 0 else DEFAULT_DETECTOR_TIMEOUT_SEC
                    det_rc = run_script_blocking(DETECTOR_SCRIPT, extra_args=["--once"], timeout=det_timeout)
                    if det_rc != 0:
                        logger.warning(f"Detector returned non-zero exit code: {det_rc}")

                # 3b) Audio detector — runs in a SEPARATE subprocess so its ORT session
                #     never coexists with the vision detector's in RAM (Pi Zero W2 only
                #     has 512 MB and the audio model alone is ~85 MB on disk).
                if audio_detector_enabled:
                    ad_timeout = detector_timeout_sec if detector_timeout_sec > 0 else DEFAULT_DETECTOR_TIMEOUT_SEC
                    ad_rc = run_script_blocking(AUDIO_DETECTOR_SCRIPT, extra_args=["--once"], timeout=ad_timeout)
                    if ad_rc != 0:
                        logger.warning(f"Audio detector returned non-zero exit code: {ad_rc}")

                # 4) Drain the backlog: XBee in relay mode, HTTPS uploader in direct mode
                fwd_timeout = forwarder_timeout_sec if forwarder_timeout_sec > 0 else DEFAULT_FORWARDER_TIMEOUT_SEC
                fwd_rc = run_script_blocking(forwarder_script, extra_args=["--once"], timeout=fwd_timeout)
                if fwd_rc != 0:
                    logger.warning(f"{forwarder_script} returned non-zero exit code: {fwd_rc}")

                # Cycle finished cleanly; sleep the configured interval (below).
                cycle_sleep_sec = loop_sleep_sec
            except KeyboardInterrupt:
                raise
            except Exception:
                logger.exception("Launcher cycle failed; recovering after a short delay")
                cycle_sleep_sec = 60.0
            # Sleep is OUTSIDE the guard and always validated, so a bad value
            # or a mid-cycle error can never crash or wedge the launcher.
            try:
                _s = float(cycle_sleep_sec)
            except (TypeError, ValueError):
                _s = 60.0
            if _s < 1.0:
                _s = 1.0
            logger.info(f"Cycle complete. Sleeping {_s:.0f}s...")
            time.sleep(_s)

    except KeyboardInterrupt:
        logger.info("Ctrl-C pressed, exiting launcher.")
    finally:
        stop_background(signal_monitor_proc, SIGNAL_MONITOR_SCRIPT)
        stop_background(audio_service_proc, AUDIO_SERVICE_SCRIPT)
        stop_background(ftp_proc, FTP_SERVER_SCRIPT)
        stop_background(email_proc, EMAIL_SERVER_SCRIPT)
        stop_background(web_status_proc, WEB_STATUS_SCRIPT)
        logger.info("Launcher stopped.")


if __name__ == "__main__":
    main()