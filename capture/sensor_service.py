#!/usr/bin/env python3
import os
import time
import json
import signal
import logging
from datetime import datetime

import smbus2
from smbus2 import SMBus

# =============================================================================
# Paths & configuration
# =============================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "config.json")

LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "bme688.log")
DAILY_LOG_FILE = os.path.join(LOG_DIR, f"bme688_{datetime.now().strftime('%Y%m%d')}.log")

def setup_logging():
    logger = logging.getLogger("bme688")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    fh = logging.FileHandler(LOG_FILE)
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)

    dfh = logging.FileHandler(DAILY_LOG_FILE)
    dfh.setLevel(logging.INFO)
    dfh.setFormatter(fmt)

    logger.addHandler(ch)
    logger.addHandler(fh)
    logger.addHandler(dfh)
    return logger

logger = setup_logging()

def load_cfg():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Could not read {CONFIG_PATH}: {e}")
        return {}

_cfg = load_cfg()
bme_cfg = _cfg.get("bme688", {})

# Optional enable flag (default True)
ENABLED = bool(bme_cfg.get("enabled", True))

# Where to write readings
READINGS_DIR = bme_cfg.get("readings_dir", os.path.join(PROJECT_ROOT, "readings"))
os.makedirs(READINGS_DIR, exist_ok=True)

DATA_FILE   = os.path.join(READINGS_DIR, "data.jsonl")
LATEST_FILE = os.path.join(READINGS_DIR, "latest.json")

# BME688 settings (config → env → defaults)
I2C_BUS = int(bme_cfg.get("i2c_bus", os.getenv("I2C_BUS", 1)))
READ_INTERVAL_SEC = float(bme_cfg.get("read_interval_sec", os.getenv("READ_INTERVAL_SEC", 60)))

# NEW: how often to retry sensor detection if missing
DETECT_RETRY_SEC = float(bme_cfg.get("detect_retry_sec", 30))

# =============================================================================
# Device constants
# =============================================================================

BME688_ADDRS = (0x77, 0x76)

# Oversampling defaults (tweak if needed)
OSRS_H = 0x01  # humidity oversampling x1  (set before ctrl_meas)
OSRS_T = 0x02  # temperature oversampling x2
OSRS_P = 0x04  # pressure oversampling x8

# =============================================================================
# I2C globals
# =============================================================================

bus: SMBus | None = None
BME688_ADDRESS: int | None = None
_CAL = None  # calibration cache

# =============================================================================
# I2C helpers
# =============================================================================

def _u8(reg):
    return bus.read_byte_data(BME688_ADDRESS, reg)

def _u16(reg_lsb_first):
    return (_u8(reg_lsb_first + 1) << 8) | _u8(reg_lsb_first)

def _s16(reg_lsb_first):
    v = _u16(reg_lsb_first)
    return v - 0x10000 if v & 0x8000 else v

# =============================================================================
# Calibration (BME688 map)
# =============================================================================

def bme688_read_calibration_data():
    """
    Read BME688 calibration constants from the chip.
    Returns dict cal with keys: T1,T2,T3,P1..P10,H1..H7.

    Note: H1/H2 are 12-bit signed values split across E1/E2/E3.
    """
    global _CAL
    if _CAL is not None:
        return _CAL

    cal = {}

    # Temperature
    cal["T1"] = _u16(0xE9)          # unsigned
    cal["T2"] = _s16(0x8A)          # signed
    t3 = _u8(0x8C)                  # signed 8-bit
    if t3 & 0x80:
        t3 -= 256
    cal["T3"] = t3

    # Pressure
    cal["P1"] = _u16(0x8E)
    cal["P2"] = _s16(0x90)
    p3 = _u8(0x92);  cal["P3"] = p3 - 256 if (p3 & 0x80) else p3
    cal["P4"] = _s16(0x94)
    cal["P5"] = _s16(0x96)
    p6 = _u8(0x99);  cal["P6"] = p6 - 256 if (p6 & 0x80) else p6
    p7 = _u8(0x98);  cal["P7"] = p7 - 256 if (p7 & 0x80) else p7
    cal["P8"] = _s16(0x9C)
    cal["P9"] = _s16(0x9E)
    cal["P10"] = _u8(0xA0)

    # Humidity (12-bit signed)
    e1 = _u8(0xE1); e2 = _u8(0xE2); e3 = _u8(0xE3)
    h1 = (e3 << 4) | (e2 & 0x0F)
    h2 = (e1 << 4) | (e2 >> 4)
    if h1 & 0x800: h1 -= 0x1000
    if h2 & 0x800: h2 -= 0x1000
    cal["H1"] = h1
    cal["H2"] = h2

    def _s8(v): return v - 256 if (v & 0x80) else v
    cal["H3"] = _s8(_u8(0xE4))
    cal["H4"] = _s8(_u8(0xE5))
    cal["H5"] = _s8(_u8(0xE6))
    cal["H6"] = _u8(0xE7)  # unsigned
    cal["H7"] = _s8(_u8(0xE8))

    _CAL = cal
    return _CAL

# =============================================================================
# Trigger & read a single measurement
# =============================================================================

def _trigger_forced_measurement():
    """
    Configure oversampling and start one forced measurement, then wait briefly
    for completion by polling meas_status_0 (0x1D).
    """
    bus.write_byte_data(BME688_ADDRESS, 0x72, OSRS_H & 0x07)  # ctrl_hum
    bus.write_byte_data(BME688_ADDRESS, 0x75, 0x00)           # config (IIR off)

    ctrl_meas = ((OSRS_T & 0x07) << 5) | ((OSRS_P & 0x07) << 2) | 0x01
    bus.write_byte_data(BME688_ADDRESS, 0x74, ctrl_meas)

    t0 = time.time()
    while True:
        s = bus.read_byte_data(BME688_ADDRESS, 0x1D)
        measuring = (s & 0x20) != 0
        new_data = (s & 0x80) != 0
        if new_data and not measuring:
            return
        if (time.time() - t0) > 0.5:
            return
        time.sleep(0.003)

def _read_raw_frame():
    """
    Read raw T/P/H from Field 0 registers.
    """
    p = bus.read_i2c_block_data(BME688_ADDRESS, 0x1F, 3)
    t = bus.read_i2c_block_data(BME688_ADDRESS, 0x22, 3)
    h = bus.read_i2c_block_data(BME688_ADDRESS, 0x25, 2)

    raw_p = (p[0] << 12) | (p[1] << 4) | (p[2] >> 4)
    raw_t = (t[0] << 12) | (t[1] << 4) | (t[2] >> 4)
    raw_h = (h[0] << 8) | h[1]
    return raw_t, raw_p, raw_h

# =============================================================================
# Compensation
# =============================================================================

def compensate_temperature(raw_t, cal):
    var1 = (raw_t / 16384.0 - cal["T1"] / 1024.0) * cal["T2"]
    var2 = ((raw_t / 131072.0 - cal["T1"] / 8192.0) ** 2) * cal["T3"]
    t_fine = var1 + var2
    temp_c = t_fine / 5120.0
    return temp_c, t_fine

def compensate_pressure(raw_p, t_fine, cal):
    var1 = (t_fine / 2.0) - 64000.0
    var2 = var1 * var1 * cal["P6"] / 131072.0
    var2 += var1 * cal["P5"] * 2.0
    var2 = (var2 / 4.0) + (cal["P4"] * 65536.0)
    var1 = (cal["P3"] * var1 * var1 / 16384.0 + cal["P2"] * var1) / 524288.0
    var1 = (1.0 + var1 / 32768.0) * cal["P1"]
    if var1 == 0:
        return 0.0
    p = 1048576.0 - raw_p
    p = ((p - (var2 / 4096.0)) * 6250.0) / var1
    var1 = cal["P9"] * p * p / 2147483648.0
    var2 = p * cal["P8"] / 32768.0
    p = p + (var1 + var2 + cal["P7"]) / 16.0
    return p

def compensate_humidity(raw_h, temp_c, cal):
    var1 = raw_h - (cal["H1"] * 16.0 + (cal["H3"] / 2.0) * temp_c)
    var2 = (cal["H2"] / 262144.0) * (
        1.0 +
        (cal["H4"] / 16384.0) * temp_c +
        (cal["H5"] / 1048576.0) * temp_c * temp_c
    )
    hum = var1 * var2
    hum += ((cal["H6"] / 16384.0) + (cal["H7"] / 2097152.0) * temp_c) * hum * hum
    if hum < 0.0:
        hum = 0.0
    if hum > 100.0:
        hum = 100.0
    return hum

def read_bme688_once():
    _trigger_forced_measurement()
    cal = bme688_read_calibration_data()
    raw_t, raw_p, raw_h = _read_raw_frame()
    temp_c, t_fine = compensate_temperature(raw_t, cal)
    pressure_pa = compensate_pressure(raw_p, t_fine, cal)
    rh_pct = compensate_humidity(raw_h, temp_c, cal)
    return round(temp_c, 2), round(rh_pct, 2), round(pressure_pa, 2)

# =============================================================================
# JSON write helpers
# =============================================================================

DATA_MAX_BYTES = 5_000_000  # rotate the readings log at ~5MB so it can't fill the disk

def append_jsonl(path, obj):
    # Bound growth: once the file passes the cap, rotate to <path>.1 (keep exactly
    # one old generation) and start fresh — an unattended node runs for months.
    try:
        if os.path.exists(path) and os.path.getsize(path) > DATA_MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass
    line = json.dumps(obj, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as out:
        out.write(line)
        out.flush()
        try:
            os.fsync(out.fileno())
        except OSError:
            pass

def write_latest(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)

# =============================================================================
# Lifecycle (signals)
# =============================================================================

RUNNING = True

def _signal_handler(signum, frame):
    global RUNNING
    RUNNING = False
    logger.info(f"Caught signal {signum}, exiting...")

for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(sig, _signal_handler)

# =============================================================================
# Detection / init
# =============================================================================

def _close_bus():
    global bus
    try:
        if bus is not None:
            bus.close()
    except Exception:
        pass
    bus = None

def init_i2c_and_detect() -> bool:
    """
    Try to open the bus and detect the sensor.
    Returns True if detected, False otherwise.
    """
    global bus, BME688_ADDRESS, _CAL

    _close_bus()
    BME688_ADDRESS = None
    _CAL = None

    try:
        bus = smbus2.SMBus(I2C_BUS)
    except Exception as e:
        logger.warning(f"I2C bus open failed (bus={I2C_BUS}): {e}")
        _close_bus()
        return False

    for addr in BME688_ADDRS:
        try:
            if bus.read_byte_data(addr, 0xD0) == 0x61:
                BME688_ADDRESS = addr
                logger.info(f"Detected BME688 at 0x{addr:02X} on I2C bus {I2C_BUS}")
                return True
        except Exception:
            continue

    logger.warning("BME688 not found at 0x77 or 0x76 (will retry).")
    _close_bus()
    return False

# =============================================================================
# Main loop
# =============================================================================

def main():
    if not ENABLED:
        logger.info("BME688 service disabled (bme688.enabled=false). Exiting.")
        return 0

    logger.info(f"JSONL path:   {DATA_FILE}")
    logger.info(f"Latest path:  {LATEST_FILE}")
    logger.info(f"Interval:     {READ_INTERVAL_SEC}s (i2c_bus={I2C_BUS})")
    logger.info(f"Detect retry: {DETECT_RETRY_SEC}s")

    detected = init_i2c_and_detect()

    while RUNNING:
        # If not detected, retry periodically and do nothing else.
        if not detected:
            time.sleep(DETECT_RETRY_SEC)
            detected = init_i2c_and_detect()
            continue

        t0 = time.time()
        try:
            temp_c, rh_pct, pressure_pa = read_bme688_once()
        except Exception as e:
            logger.error(f"Read error: {e} (will re-detect)")
            # Something went wrong with comms; force re-detect next loop
            detected = False
            _close_bus()
            continue

        record = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "temperature_c": temp_c,
            "humidity_pct": rh_pct,
            "pressure_pa": pressure_pa
        }

        try:
            append_jsonl(DATA_FILE, record)
            write_latest(LATEST_FILE, record)
            logger.info(f"OK {record}")
        except Exception as e:
            logger.error(f"Write error: {e}")

        elapsed = time.time() - t0
        time.sleep(max(0.0, READ_INTERVAL_SEC - elapsed))

    _close_bus()
    logger.info("Stopped cleanly.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
