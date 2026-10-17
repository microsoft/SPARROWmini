#!/usr/bin/env python3
"""
vbus_control.py

Backend-agnostic VBUS power control for USB peripherals attached to a Pi.

Two backends:
- GpioVbus     — toggles a BCM GPIO pin (legacy DuPont/transistor rig)
- UhubctlVbus  — calls `uhubctl -l <location> -p <port> -a on/off` against a
                 per-port-power-switching USB hub (e.g. VL81x, VL812)

Factory `make_vbus(cfg_block)` selects the backend from per-service config:

  "vbus": {"backend": "uhubctl", "location": "1-1.4", "port": 2}
  "vbus": {"backend": "gpio",    "pin": 17}

If no `vbus` block is present, falls back to the legacy `vbus_pin: <int>`
key for back-compat with deployments wired before this abstraction landed.
The fallback default pin is 17 (matches the historical robin wiring).

`RPi.GPIO` is imported lazily inside GpioVbus so this module loads on hosts
without the library (CI, dev machines, uhubctl-only deployments).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from typing import Optional

log = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Abstract base
# -----------------------------------------------------------------------------

class VbusController(ABC):
    @abstractmethod
    def on(self) -> None: ...

    @abstractmethod
    def off(self) -> None: ...

    def cleanup(self) -> None:
        """Release any process-scoped state (GPIO releases its pin; uhubctl is stateless)."""
        return None

    @abstractmethod
    def __repr__(self) -> str: ...


# -----------------------------------------------------------------------------
# GPIO backend (legacy)
# -----------------------------------------------------------------------------

class GpioVbus(VbusController):
    """Toggle VBUS via a BCM GPIO pin driving an external transistor / level shifter."""

    def __init__(self, pin: int) -> None:
        # Lazy import — only require RPi.GPIO if this backend is actually selected.
        import RPi.GPIO as GPIO  # type: ignore

        self._GPIO = GPIO
        self._pin = int(pin)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self._pin, GPIO.OUT)

    def on(self) -> None:
        self._GPIO.output(self._pin, self._GPIO.HIGH)

    def off(self) -> None:
        self._GPIO.output(self._pin, self._GPIO.LOW)

    def cleanup(self) -> None:
        try:
            # Release just our pin, not the whole GPIO state — other services
            # may be using their own pins in the same process tree.
            self._GPIO.cleanup(self._pin)
        except Exception as e:  # pragma: no cover (cleanup is best-effort)
            log.warning("GpioVbus cleanup on pin %d failed: %s", self._pin, e)

    def __repr__(self) -> str:
        return f"GpioVbus(pin={self._pin})"


# -----------------------------------------------------------------------------
# uhubctl backend (modern)
# -----------------------------------------------------------------------------

class UhubctlVbus(VbusController):
    """Toggle VBUS via the `uhubctl` userspace utility against a PPPS-capable hub.

    Discover your hub's location + port with `sudo uhubctl` on the Pi.
    Hubs marked `ganged` instead of `ppps` cannot drive a single port — they
    will report success but power-cycle the whole hub (not what you want).
    """

    def __init__(self, location: str, port: int, binary: str = "uhubctl") -> None:
        location = str(location).strip()
        port = int(port)
        if not location:
            raise ValueError("UhubctlVbus requires a non-empty location (e.g. '1-1.4')")
        if port <= 0:
            raise ValueError(f"UhubctlVbus requires port > 0, got {port}")
        self._location = location
        self._port = port
        self._binary = self._resolve_binary(binary)

    @staticmethod
    def _resolve_binary(binary: str) -> str:
        """Resolve the uhubctl binary path. Mirrors sd_service.py's sdwire resolver."""
        if os.path.isabs(binary):
            if os.access(binary, os.X_OK):
                return binary
            log.warning("uhubctl override path %s is not executable; falling back", binary)
        found = shutil.which(binary)
        if found:
            return found
        # Final fallback — let subprocess raise FileNotFoundError later with a clear error.
        return binary

    def _invoke(self, action: str) -> None:
        cmd = [self._binary, "-l", self._location, "-p", str(self._port), "-a", action]
        try:
            r = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            log.debug("uhubctl %s ok: %s", action, r.stdout.strip())
        except FileNotFoundError as e:
            log.error("uhubctl binary not found (%s): %s", self._binary, e)
            raise
        except subprocess.CalledProcessError as e:
            log.error(
                "uhubctl %s failed (rc=%s) cmd=%s stderr=%s",
                action, e.returncode, " ".join(cmd), (e.stderr or "").strip(),
            )
            raise
        except subprocess.TimeoutExpired:
            log.error("uhubctl %s timed out: %s", action, " ".join(cmd))
            raise

    def on(self) -> None:
        self._invoke("on")

    def off(self) -> None:
        self._invoke("off")

    def __repr__(self) -> str:
        return f"UhubctlVbus(location={self._location!r}, port={self._port})"


# -----------------------------------------------------------------------------
# Factory
# -----------------------------------------------------------------------------

DEFAULT_LEGACY_PIN = 17


def make_vbus(
    cfg_block: Optional[dict],
    *,
    legacy_pin_default: int = DEFAULT_LEGACY_PIN,
    legacy_pin_key: str = "vbus_pin",
) -> VbusController:
    """Build a VbusController from a per-service config block.

    Args:
        cfg_block: e.g. `cfg["camera_usb"]` or `cfg["sdwire"]` (may be {} or None).
        legacy_pin_default: BCM pin used when neither `vbus` nor `legacy_pin_key` is set.
        legacy_pin_key: the key on `cfg_block` for the legacy integer pin spec.

    Recognised shapes inside `cfg_block["vbus"]`:
        {"backend": "uhubctl", "location": "1-1.4", "port": 2, "binary": "uhubctl"}
        {"backend": "gpio",    "pin": 17}

    Falls back to `cfg_block[legacy_pin_key]` (integer) → GpioVbus when no `vbus`
    block is present. This preserves existing `sdwire.vbus_pin: 17` configs
    without requiring a rewrite.
    """
    block = cfg_block or {}
    vbus_cfg = block.get("vbus") or {}
    backend = str(vbus_cfg.get("backend", "")).lower().strip()

    if backend == "uhubctl":
        return UhubctlVbus(
            location=vbus_cfg.get("location", ""),
            port=int(vbus_cfg.get("port", 0)),
            binary=str(vbus_cfg.get("binary") or "uhubctl"),
        )

    if backend == "gpio":
        return GpioVbus(pin=int(vbus_cfg.get("pin", legacy_pin_default)))

    # Legacy path: cfg_block has no `vbus`, fall back to top-level integer pin.
    pin = int(block.get(legacy_pin_key, legacy_pin_default))
    return GpioVbus(pin=pin)
