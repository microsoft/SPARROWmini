#!/usr/bin/env python3
"""
xbee_node_sender_polled.py

Behavior:
- Processes a BACKLOG: sends the oldest stable .jpg first (FIFO)
- Optionally compresses/resizes each image atomically before sending
- Deletes the image after a confirmed OK + CRC match
- Avoids recompressing the same file on every retry
- Uses a STABLE session_id per file so retries keep the same sid
- Recovers from serial disconnects by reopening the port
- Auto-discovers the XBee peer by NI if dest64 is missing
- Keeps retrying discovery forever until the peer is found
- Re-discovers the peer after repeated GO timeouts if cached dest64 is stale
"""

import os
import sys
import glob
import json
import time
import struct
import binascii
import argparse
from pathlib import Path
from collections import deque
from typing import Optional

import serial
from PIL import Image
import fcntl

# Shared robustness helpers (atomic/locked config writes, poison-file quarantine).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
from robust import (  # noqa: E402
    update_config_atomic, record_failure, clear_failure, quarantine_file, flock_bounded,
)

LOCK_FILE = "/home/sparrow/robin/xbee_serial.lock"

START_DELIM = 0x7E


# -------------------- XBee API helpers --------------------

def xbee_checksum(frame_data: bytes) -> int:
    return (0xFF - (sum(frame_data) & 0xFF)) & 0xFF


def build_api_frame(frame_data: bytes) -> bytes:
    length = len(frame_data)
    return bytes([START_DELIM]) + struct.pack(">H", length) + frame_data + bytes([xbee_checksum(frame_data)])


def build_tx_request_0x10(frame_id: int, dest64: int, rf_data: bytes) -> bytes:
    frame_type = 0x10
    dest16 = 0xFFFE
    broadcast_radius = 0x00
    options = 0x00
    frame = struct.pack(">BBQHBB", frame_type, frame_id, dest64, dest16, broadcast_radius, options) + rf_data
    return build_api_frame(frame)


def build_at_command_0x08(frame_id: int, at_cmd: str, parameter: bytes = b"") -> bytes:
    frame_type = 0x08
    frame = struct.pack(">BB2s", frame_type, frame_id, at_cmd.encode("ascii")) + parameter
    return build_api_frame(frame)


def read_api_frame(ser: serial.Serial, timeout_s: float = 0.5) -> Optional[bytes]:
    end = time.time() + timeout_s
    while time.time() < end:
        b = ser.read(1)
        if not b:
            continue
        if b[0] != START_DELIM:
            continue
        ln = ser.read(2)
        if len(ln) != 2:
            continue
        length = struct.unpack(">H", ln)[0]
        data = ser.read(length)
        if len(data) != length:
            continue
        cks = ser.read(1)
        if len(cks) != 1:
            continue
        if ((sum(data) + cks[0]) & 0xFF) != 0xFF:
            continue
        return data
    return None


def drain_frames(ser: serial.Serial, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        _ = read_api_frame(ser, timeout_s=0.05)


def src64_bytes_to_int(src64_b: bytes) -> int:
    return int.from_bytes(src64_b, "big", signed=False)


def fmt64(x: int) -> str:
    return f"{x:016X}"


def xbee_local_at_multi(
    ser: serial.Serial,
    at_cmd: str,
    parameter: bytes = b"",
    timeout_s: float = 8.0,
    frame_id: int = 0x44,
) -> list[tuple[int, bytes]]:
    ser.write(build_at_command_0x08(frame_id, at_cmd, parameter))
    ser.flush()

    deadline = time.time() + timeout_s
    responses: list[tuple[int, bytes]] = []

    while time.time() < deadline:
        frame = read_api_frame(ser, timeout_s=0.5)
        if not frame:
            continue

        if frame[0] != 0x88 or len(frame) < 5:
            continue
        if frame[1] != frame_id:
            continue
        if frame[2:4] != at_cmd.encode("ascii"):
            continue

        status = frame[4]
        payload = frame[5:]
        responses.append((status, payload))

        print(f"[NODE] AT {at_cmd} status=0x{status:02X} payload={payload.hex()}")

        if at_cmd != "ND":
            break

    if not responses:
        print(f"[NODE] AT {at_cmd} timed out")

    return responses


def xbee_local_at(
    ser: serial.Serial,
    at_cmd: str,
    parameter: bytes = b"",
    timeout_s: float = 8.0,
    frame_id: int = 0x44,
) -> Optional[bytes]:
    responses = xbee_local_at_multi(ser, at_cmd, parameter, timeout_s, frame_id)
    if not responses:
        return None

    status, payload = responses[0]
    if status != 0x00:
        return None
    return payload


# -------------------- Config helpers --------------------

def load_config(config_path: Path) -> dict:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if "xbee" not in data:
        raise ValueError("config.json missing top-level 'xbee' object")
    return data


def save_config(config_path: Path, cfg: dict) -> None:
    """Persist the discovered hub address. The sender only ever changes
    xbee.dest64, so we re-read the file under a cross-process lock and write back
    just that key atomically — this can't leave a torn config.json on power loss,
    and can't clobber a concurrent web-UI edit to another key (lost-update)."""
    new_dest = str(((cfg or {}).get("xbee") or {}).get("dest64", "")).strip()
    update_config_atomic(
        config_path,
        lambda c: c.setdefault("xbee", {}).__setitem__("dest64", new_dest),
    )


def read_device_id(device_id_file: str) -> int:
    p = Path(device_id_file)
    if not p.exists():
        return os.getpid() & 0xFFFF

    s = p.read_text(encoding="utf-8").strip()
    if not s:
        return os.getpid() & 0xFFFF

    try:
        if s.lower().startswith("0x"):
            v = int(s, 16)
        elif all(c in "0123456789" for c in s):
            v = int(s, 10)
        else:
            v = int(s, 16)
    except Exception:
        v = os.getpid() & 0xFFFF
    return v & 0xFFFFFFFF


# -------------------- File selection (FIFO + round-robin) --------------------

def oldest_stable_jpg(dest_dir: Path, stable_sec: float) -> Optional[Path]:
    """Original image-only picker. Kept for back-compat / callers outside this script."""
    return oldest_stable_in_pool(dest_dir, [".jpg"], stable_sec)


def oldest_stable_in_pool(
    pool_dir: Path,
    extensions: list[str],
    stable_sec: float,
) -> Optional[Path]:
    """Pick the oldest stable file in a pool with the given extensions.

    "Stable" = mtime older than stable_sec OR in the future (camera-clock skew).
    Files with a `.det.json` sidecar are still eligible — that's just the
    detector's "already analyzed" marker, not a forwarding gate.
    """
    if not pool_dir.exists():
        return None
    exts = {e.lower() for e in extensions}
    candidates = sorted(
        [p for p in pool_dir.rglob("*")
         if p.is_file() and p.suffix.lower() in exts
         and ".quarantine" not in p.parts],   # never re-pick quarantined poison files
        key=lambda p: p.stat().st_mtime,
        reverse=False,
    )
    if not candidates:
        return None

    now = time.time()
    for p in candidates:
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        if st.st_size <= 0:
            continue
        age = now - st.st_mtime
        if age < 0:
            return p
        if age >= stable_sec:
            return p
    return None


class RoundRobinPicker:
    """Stateful round-robin picker across multiple file pools.

    Used to alternate XBee sends between e.g. image (.jpg) and audio (.mp3)
    backlogs so a big pile of one type doesn't starve the other.

    Per `pick()` call, returns the oldest stable file from the current pool.
    If the current pool is empty, walks the remaining pools in order until one
    has a candidate, or returns None if everything is drained.

    After every successful send the caller invokes `mark_sent()`; once the
    consecutive send count reaches `quantum`, the picker advances to the next
    pool (wrapping). Used across a single --once invocation; state isn't
    persisted to disk between launcher cycles.
    """

    def __init__(self, pools: list[tuple[Path, list[str]]], quantum: int) -> None:
        # Drop pools whose dirs don't exist so we don't loop on dead entries.
        self.pools = [(d, exts) for d, exts in pools]
        self.quantum = max(1, int(quantum))
        self.current_idx = 0
        self.sent_in_current = 0

    def pick(self, stable_sec: float) -> Optional[Path]:
        n = len(self.pools)
        if n == 0:
            return None
        for _ in range(n):
            pool_dir, exts = self.pools[self.current_idx]
            file = oldest_stable_in_pool(pool_dir, exts, stable_sec)
            if file is not None:
                return file
            # Current pool empty — advance to the next and reset the per-pool
            # counter (the empty pool doesn't get credit toward its quantum).
            self.current_idx = (self.current_idx + 1) % n
            self.sent_in_current = 0
        return None

    def mark_sent(self) -> None:
        self.sent_in_current += 1
        if self.sent_in_current >= self.quantum:
            self.current_idx = (self.current_idx + 1) % max(1, len(self.pools))
            self.sent_in_current = 0


# -------------------- Audio-vs-image file-type discrimination --------------------

JPEG_EXTS = {".jpg", ".jpeg"}
AUDIO_EXTS = {".mp3"}


def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in JPEG_EXTS


def is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTS


# -------------------- JPEG compression helpers --------------------

def _jpeg_has_soi_eoi(path: Path) -> bool:
    try:
        if path.stat().st_size < 4:
            return False
        with path.open("rb") as f:
            head = f.read(2)
            f.seek(-2, os.SEEK_END)
            tail = f.read(2)
        return head == b"\xff\xd8" and tail == b"\xff\xd9"
    except Exception:
        return False


def compress_and_resize_atomic(path: Path, quality: int, max_dim: int, strip_exif: bool) -> bool:
    tmp = path.with_name(path.name + ".part")
    try:
        img = Image.open(path)
        img.load()
        img = img.convert("RGB")

        w, h = img.size
        if max_dim and max_dim > 0 and max(w, h) > max_dim:
            scale = max_dim / float(max(w, h))
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        save_kwargs = {
            "format": "JPEG",
            "quality": quality,
            "optimize": True,
        }
        if strip_exif:
            save_kwargs["exif"] = b""

        img.save(tmp, **save_kwargs)

        if not _jpeg_has_soi_eoi(tmp):
            raise RuntimeError("Compressed output failed SOI/EOI marker check")

        with Image.open(tmp) as im2:
            im2.verify()

        os.replace(tmp, path)
        return True

    except Exception as e:
        print(f"[NODE] Compression failed for {path.name}: {e} (sending original bytes)")
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False


# -------------------- Chunk builder --------------------

def build_chunks_fixed_session(img_path: Path, chunk_max: int, session_id: int):
    data = img_path.read_bytes()
    file_size = len(data)
    file_name = img_path.name.encode("utf-8")
    if len(file_name) > 120:
        file_name = file_name[:120]

    header_base = 2 + 4 + 2 + 2 + 1
    meta_len = 4 + 1 + len(file_name)

    first_payload_max = max(0, chunk_max - (header_base + meta_len))
    other_payload_max = max(0, chunk_max - header_base)

    if first_payload_max <= 0 or other_payload_max <= 0:
        raise ValueError("chunk size too small; increase xbee.chunk (try 180+)")

    payloads: list[tuple[int, bytes]] = []
    offset = 0

    first_payload = data[offset:offset + first_payload_max]
    offset += len(first_payload)
    payloads.append((1, first_payload))

    while offset < file_size:
        payload = data[offset:offset + other_payload_max]
        offset += len(payload)
        payloads.append((0, payload))

    total_chunks = len(payloads)

    def rf_for(idx: int) -> bytes:
        is_first, payload = payloads[idx]
        flags = 0x01 if is_first else 0x00
        rf = bytearray()
        rf += b"RB"
        rf += struct.pack(">I", session_id)
        rf += struct.pack(">H", total_chunks)
        rf += struct.pack(">H", idx)
        rf += struct.pack(">B", flags)
        if is_first:
            rf += struct.pack(">I", file_size)
            rf += struct.pack(">B", len(file_name))
            rf += file_name
        rf += payload
        return bytes(rf)

    return data, file_size, file_name, total_chunks, rf_for


def make_stable_session_id(device_id32: int, img_path: Path) -> int:
    st = img_path.stat()
    seed = f"{device_id32}:{img_path.name}:{st.st_size}:{st.st_mtime_ns}".encode("utf-8", errors="replace")
    return binascii.crc32(seed) & 0xFFFFFFFF


# -------------------- Discovery --------------------

def set_discovery_timeout_nt(ser: serial.Serial, nt_value: int = 0x64) -> bool:
    resp = xbee_local_at(ser, "NT", nt_value.to_bytes(1, "big"), timeout_s=3.0, frame_id=0x46)
    return resp is not None


def discover_peer_by_dn(ser: serial.Serial, peer_ni: str) -> Optional[int]:
    responses = xbee_local_at_multi(
        ser=ser,
        at_cmd="DN",
        parameter=peer_ni.encode("ascii"),
        timeout_s=12.0,
        frame_id=0x47,
    )
    if not responses:
        return None

    status, payload = responses[0]
    if status != 0x00:
        return None

    if len(payload) < 10:
        print(f"[NODE] DN response too short: len={len(payload)} data={payload.hex()}")
        return None

    ext64 = payload[2:10]
    peer64 = int.from_bytes(ext64, "big", signed=False)
    print(f"[NODE] DN resolved NI='{peer_ni}' addr64={fmt64(peer64)}")
    return peer64


def discover_peer_by_nd(ser: serial.Serial, peer_ni: str, timeout_s: float = 12.0) -> Optional[int]:
    responses = xbee_local_at_multi(
        ser=ser,
        at_cmd="ND",
        parameter=b"",
        timeout_s=timeout_s,
        frame_id=0x48,
    )
    if not responses:
        return None

    found = None

    for status, payload in responses:
        if status != 0x00:
            continue
        if len(payload) < 10:
            continue

        sh = payload[2:6]
        sl = payload[6:10]

        ni_start = 10
        try:
            ni_end = payload.index(0x00, ni_start)
            ni = payload[ni_start:ni_end].decode("utf-8", errors="replace")
        except ValueError:
            ni = ""

        addr64 = (int.from_bytes(sh, "big") << 32) | int.from_bytes(sl, "big")
        print(f"[NODE] ND candidate NI='{ni}' addr64={fmt64(addr64)}")

        if ni == peer_ni:
            found = addr64
            break

    return found


def resolve_peer64_forever(
    cfg: dict,
    config_path: Path,
    ser: serial.Serial,
    retry_sec: float = 5.0,
    max_attempts: int = 0,
) -> Optional[int]:
    """Discover the hub by NI, retrying until found. With max_attempts>0, give up
    after that many rounds and return None (caller exits cleanly) instead of
    looping forever — so a --once run can't be left for the launcher to kill."""
    xbee = cfg["xbee"]
    dest64_raw = str(xbee.get("dest64", "")).strip()

    if dest64_raw:
        peer64 = int(dest64_raw.replace("0x", "").replace(" ", ""), 16)
        print(f"[NODE] Peer from config: {fmt64(peer64)}")
        return peer64

    # Config key stays `xbee.master_ni` (peer-side wire identifier doesn't change),
    # but we use `peer_ni` as the local var name to match the new terminology.
    peer_ni = str(xbee.get("master_ni", "SPARROW_MASTER")).strip()
    port = xbee.get("port", "/dev/ttyUSB0")
    baud = int(xbee.get("baud", 115200))

    attempt = 0
    while True:
        attempt += 1
        if max_attempts and attempt > max_attempts:
            print(f"[NODE] Peer NI='{peer_ni}' not found after {max_attempts} attempts; giving up for now.")
            return None
        try:
            print(f"[NODE] No usable dest64 in config; discovering peer by NI='{peer_ni}'...")

            set_discovery_timeout_nt(ser, nt_value=0x64)

            peer64 = discover_peer_by_dn(ser, peer_ni)
            if peer64 is None:
                print("[NODE] DN did not resolve peer, trying ND fallback...")
                peer64 = discover_peer_by_nd(ser, peer_ni, timeout_s=12.0)

            if peer64 is not None:
                print(f"[NODE] Discovered peer64={fmt64(peer64)} by NI='{peer_ni}'")
                cfg["xbee"]["dest64"] = fmt64(peer64)
                try:
                    save_config(config_path, cfg)
                    print(f"[NODE] Saved discovered peer64 to {config_path}")
                except Exception as e:
                    print(f"[NODE] Warning: could not save discovered peer64: {e}")
                return peer64

            print(f"[NODE] Could not discover peer by NI='{peer_ni}'. Retrying in {retry_sec:.1f}s...")

        except (serial.SerialException, OSError) as e:
            print(f"[NODE] Serial fault during NI discovery: {e}")
            ser = reopen_serial(ser, port, baud)

        except Exception as e:
            print(f"[NODE] Unexpected error during NI discovery: {e}")

        time.sleep(retry_sec)


# -------------------- Main sender logic --------------------

def run_rediscover(cfg: dict, config_path: Path) -> int:
    """One-shot: discover the hub by NI (ignoring any cached dest64) and write the
    fresh 64-bit address back to config.json, then return an exit code.

    Used by the web UI's "Rediscover hub" button after the SPARROW hub's XBee is
    swapped for a new chip and comes up with a new address — the node would
    otherwise stay pinned to the stale cached dest64. The CALLER is expected to
    already hold the xbee serial lock (the web action wraps this in `flock`), so
    we do NOT re-acquire LOCK_FILE here (that would self-deadlock).

    Exit codes: 0 ok, 2 not found, 3 serial error, 4 found-but-save-failed.
    """
    xbee = cfg["xbee"]
    peer_ni = str(xbee.get("master_ni", "SPARROW_MASTER")).strip()
    port = resolve_serial_port(xbee.get("port", "/dev/ttyUSB0"))
    baud = int(xbee.get("baud", 115200))
    old = str(xbee.get("dest64", "")).strip()

    print(f"[NODE] REDISCOVER: looking for NI='{peer_ni}' (cached dest64={old or 'unset'})")
    try:
        ser = open_serial_forever(port, baud)
    except Exception as e:
        print(f"[NODE] REDISCOVER: could not open serial {port}@{baud}: {e}")
        return 3

    peer64 = None
    try:
        set_discovery_timeout_nt(ser, nt_value=0x64)
        peer64 = discover_peer_by_dn(ser, peer_ni)
        if peer64 is None:
            print("[NODE] REDISCOVER: DN did not resolve, trying ND fallback...")
            peer64 = discover_peer_by_nd(ser, peer_ni, timeout_s=12.0)
    except (serial.SerialException, OSError) as e:
        print(f"[NODE] REDISCOVER: serial fault: {e}")
        return 3
    finally:
        try:
            ser.close()
        except Exception:
            pass

    if peer64 is None:
        print(f"[NODE] REDISCOVER: FAILED — no node named '{peer_ni}' answered discovery. "
              f"Is the hub powered and in range?")
        return 2

    new = fmt64(peer64)
    cfg["xbee"]["dest64"] = new
    try:
        save_config(config_path, cfg)
    except Exception as e:
        print(f"[NODE] REDISCOVER: found {new} but could not save config: {e}")
        return 4

    if old and new.upper() == old.upper():
        print(f"[NODE] REDISCOVER: OK — hub unchanged, addr64={new}")
    else:
        print(f"[NODE] REDISCOVER: OK — hub addr64={new} (was {old or 'unset'})")
    return 0


def resolve_serial_port(port: str) -> str:
    """Resolve the XBee serial device, tolerating unstable /dev/ttyUSBn numbering.

    ttyUSB numbers depend on USB enumeration order, so a hardcoded /dev/ttyUSB0
    can point at nothing after a reboot (the FTDI may land on ttyUSB1). If the
    configured port is missing, fall back to the stable /dev/serial/by-id FTDI
    path, then to any ttyUSB. Prefer a by-id path in config to avoid this.
    """
    if os.path.exists(port):
        return port
    by_id = sorted(
        glob.glob("/dev/serial/by-id/*FTDI*")
        + glob.glob("/dev/serial/by-id/*FT231X*")
        + glob.glob("/dev/serial/by-id/*FT_X*")
    )
    if by_id:
        print(f"[NODE] configured port {port} missing; auto-detected {by_id[0]}")
        return by_id[0]
    ttys = sorted(glob.glob("/dev/ttyUSB*"))
    if ttys:
        print(f"[NODE] configured port {port} missing; falling back to {ttys[0]}")
        return ttys[0]
    return port  # nothing found — let open_serial_forever hit its retry cap


def open_serial_forever(port: str, baud: int, max_attempts: int = 30) -> serial.Serial:
    """Open the serial port, retrying briefly. Bounded so a genuinely-absent
    radio makes a --once run EXIT (non-zero) instead of looping forever and
    freezing the launcher, which waits on this subprocess. max_attempts=0 means
    unbounded (legacy behaviour)."""
    attempt = 0
    while True:
        attempt += 1
        try:
            ser = serial.Serial(port, baud, timeout=0.05)
            drain_frames(ser, 0.2)
            return ser
        except Exception as e:
            if max_attempts and attempt >= max_attempts:
                raise RuntimeError(
                    f"serial open failed after {attempt} attempts ({port}@{baud}): {e}"
                )
            print(f"[NODE] serial open failed ({port}@{baud}): {e} "
                  f"(retry {attempt}/{max_attempts} in 2s)")
            time.sleep(2.0)


def reopen_serial(ser: Optional[serial.Serial], port: str, baud: int) -> serial.Serial:
    try:
        if ser is not None:
            ser.close()
    except Exception:
        pass
    print(f"[NODE] Reopening serial {port}@{baud}")
    return open_serial_forever(port, baud)


def next_frame_id(cur: int) -> int:
    return (cur % 255) + 1


def send_rf(ser: serial.Serial, dest64: int, rf: bytes, fid: int) -> int:
    ser.write(build_tx_request_0x10(fid, dest64, rf))
    return fid


def wait_for_go(
    ser: serial.Serial,
    sid: int,
    rd_payload: bytes,
    peer64: int,
    rd_interval: float,
    go_timeout: float
) -> bool:
    fid = 1
    t_deadline = time.time() + go_timeout
    t_next_rd = 0.0

    while time.time() < t_deadline:
        if time.time() >= t_next_rd:
            send_rf(ser, peer64, rd_payload, fid)
            ser.flush()
            fid = next_frame_id(fid)
            t_next_rd = time.time() + rd_interval
            print(f"[NODE] RD -> peer sid={sid:08x}")

        frame = read_api_frame(ser, timeout_s=0.3)
        if not frame:
            continue
        if frame[0] != 0x90 or len(frame) < 12:
            continue
        rf = frame[12:]
        if rf.startswith(b"GO") and len(rf) >= 2 + 4:
            sid_rx = struct.unpack(">I", rf[2:6])[0]
            if sid_rx == sid:
                print(f"[NODE] GO <- peer sid={sid:08x}")
                return True

    print(f"[NODE] GO timeout sid={sid:08x}")
    return False


def send_windowed_with_txstatus(
    ser: serial.Serial,
    peer64: int,
    total_chunks: int,
    rf_for,
    window: int,
    retries: int,
    status_timeout: float,
    pace_ms: float = 0.0,
) -> None:
    in_flight: dict[int, int] = {}
    attempts = [0] * total_chunks
    pending = deque(range(total_chunks))
    pace_sec = pace_ms / 1000.0

    tx_fid = 1
    sent_ok = 0

    while pending:
        in_flight.clear()
        window_idxs = []

        while pending and len(window_idxs) < window:
            idx = pending.popleft()
            if attempts[idx] >= retries:
                raise RuntimeError(f"Chunk {idx} exceeded retries ({retries})")
            attempts[idx] += 1

            cur_fid = tx_fid
            tx_fid = next_frame_id(tx_fid)

            ser.write(build_tx_request_0x10(cur_fid, peer64, rf_for(idx)))
            in_flight[cur_fid] = idx
            window_idxs.append(idx)

            if pace_sec > 0:
                ser.flush()
                time.sleep(pace_sec)

        ser.flush()

        ok_chunks = set()
        end = time.time() + status_timeout
        while time.time() < end and in_flight:
            fr = read_api_frame(ser, timeout_s=0.2)
            if not fr:
                continue
            if fr[0] == 0x8B and len(fr) >= 7:
                fid_rx = fr[1]
                if fid_rx in in_flight:
                    delivery = fr[5]
                    idx = in_flight.pop(fid_rx)
                    if delivery == 0x00:
                        ok_chunks.add(idx)
                    else:
                        pending.append(idx)

        if in_flight:
            for _, idx in list(in_flight.items()):
                pending.append(idx)
            in_flight.clear()

        sent_ok += len(ok_chunks)
        if sent_ok % 50 < window:
            print(f"[NODE] ok_chunks={sent_ok}/{total_chunks} pending={len(pending)}")


def wait_for_ok(ser: serial.Serial, sid: int, ok_timeout: float) -> Optional[int]:
    deadline = time.time() + ok_timeout
    while time.time() < deadline:
        fr = read_api_frame(ser, timeout_s=0.3)
        if not fr:
            continue
        if fr[0] != 0x90 or len(fr) < 12:
            continue
        rf = fr[12:]
        if rf.startswith(b"OK") and len(rf) >= 2 + 4 + 4:
            sid_rx = struct.unpack(">I", rf[2:6])[0]
            if sid_rx == sid:
                crc = struct.unpack(">I", rf[6:10])[0]
                return crc
    return None


def safe_delete(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return True
    except Exception as e:
        print(f"[NODE] Failed to delete {path}: {e}")
        return False


def main():
    ap = argparse.ArgumentParser(description="DigiMesh node sender service (config/config.json).")
    ap.add_argument("--config", default="config/config.json", help="Path to config JSON")
    ap.add_argument("--scan-interval", type=float, default=2.0, help="Seconds between scans in continuous mode")
    ap.add_argument("--once", action="store_true", help="Keep sending until backlog is empty, then exit")
    ap.add_argument("--rediscover", action="store_true",
                    help="Discover the hub by NI (ignoring cached dest64), write it back to "
                         "config, and exit. Caller must already hold the xbee serial lock.")
    args = ap.parse_args()

    config_path = Path(args.config)
    cfg = load_config(config_path)

    # One-shot rediscovery (web UI 'Rediscover hub'): skip all the send-path setup,
    # just re-resolve the peer by NI and persist it. See run_rediscover() for why we
    # don't take LOCK_FILE here (the web action wraps us in `flock`).
    if args.rediscover:
        sys.exit(run_rediscover(cfg, config_path))

    mode = str(cfg.get("mode") or "").lower().strip()
    if mode != "relay" or cfg.get("transport") != "xbee":
        print(f"[NODE] Warning: config mode/transport are {cfg.get('mode')}/{cfg.get('transport')} (continuing)")

    device_id32 = read_device_id(cfg.get("device_id_file", "/tmp/device_id.txt"))

    dest_dir = Path(cfg.get("dest_dir", "/home/sparrow/robin/images"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = Path(cfg.get("audio_dir", "/home/sparrow/robin/audio"))
    # Don't auto-mkdir audio_dir — if audio_service isn't running, leave its absence
    # as a signal to the picker. mkdir() also doesn't hurt though.
    audio_dir.mkdir(parents=True, exist_ok=True)

    img_cfg = cfg.get("image", {})
    stable_sec = float(img_cfg.get("stable_file_sec", 1.0))
    enable_compression = bool(img_cfg.get("enable_compression", True))
    jpeg_quality = int(img_cfg.get("jpeg_quality", 82))
    max_dim = int(img_cfg.get("max_dim", 0))
    strip_exif = bool(img_cfg.get("strip_exif", False))

    xbee = cfg["xbee"]
    port = xbee.get("port", "/dev/ttyUSB0")
    baud = int(xbee.get("baud", 115200))
    pool_quantum = int(xbee.get("pool_quantum", 2))

    txstatus = str(xbee.get("txstatus", "on")).lower()
    window = int(xbee.get("window", 16))
    chunk = int(xbee.get("chunk", 220))
    retries = int(xbee.get("retries", 3))
    status_timeout = float(xbee.get("status_timeout", 1.5))
    pace_ms = float(xbee.get("pace_ms", 0.0))

    rd_interval = float(xbee.get("rd_interval", 2.0))
    go_timeout = float(xbee.get("go_timeout", 120.0))
    ok_timeout = float(xbee.get("ok_timeout", 10.0))
    retry_sleep_sec = float(xbee.get("retry_sleep_sec", 5.0))
    max_go_failures_before_rediscover = int(xbee.get("rediscover_after_go_failures", 3))
    # After this many failed send attempts, a file is quarantined so a single
    # poison file (corrupt / CRC-failing / oversized) can never wedge the backlog.
    max_send_attempts = int(xbee.get("max_send_attempts", 5))

    if txstatus != "on":
        print("[NODE] Note: txstatus is not 'on'. Reliable scaling expects txstatus=on.")

    print(f"[NODE] Service starting. dest_dir={dest_dir} audio_dir={audio_dir}")
    print(f"[NODE] Image config: enable_compression={enable_compression} jpeg_quality={jpeg_quality} max_dim={max_dim} strip_exif={strip_exif} stable_file_sec={stable_sec}")
    print(f"[NODE] Round-robin pool_quantum={pool_quantum} (images, audio)")
    # Round-robin picker: images first, then audio. Each pool gets `pool_quantum`
    # consecutive sends before rotation. If one pool is empty the other drains
    # without artificial gating.
    picker = RoundRobinPicker(
        pools=[
            (dest_dir, [".jpg", ".jpeg"]),
            (audio_dir, [".mp3"]),
        ],
        quantum=pool_quantum,
    )

    with open(LOCK_FILE, "w") as lock_fh:
        # Bounded lock: if another sender / a web configure-or-rediscover action holds
        # the radio, wait up to 120s then exit cleanly instead of blocking forever.
        if not flock_bounded(lock_fh, timeout_sec=120):
            print("[NODE] Could not acquire serial lock within 120s; another radio action is active. Exiting.")
            return

        port = resolve_serial_port(port)
        ser = open_serial_forever(port, baud)
        # In --once mode, bound discovery so a down/absent hub exits cleanly (backlog
        # preserved) rather than looping until the launcher's timeout kills us.
        peer64 = resolve_peer64_forever(
            cfg, config_path, ser, retry_sec=5.0,
            max_attempts=(12 if args.once else 0),
        )
        if peer64 is None:
            print("[NODE] Hub not discoverable; exiting (backlog preserved for next cycle).")
            return

        print(f"[NODE] XBee port={port} baud={baud} peer64={fmt64(peer64)} window={window} chunk={chunk}")

        go_timeout_failures = 0

        try:
            while True:
                try:
                    img = picker.pick(stable_sec=stable_sec)

                    if img is None:
                        if args.once:
                            print("[NODE] No more eligible files in any pool; backlog drained. Exiting --once.")
                            return
                        time.sleep(args.scan_interval)
                        continue

                    is_audio = is_audio_file(img)
                    compressed_marker = img.with_name(img.name + ".compressed")

                    # JPEGs get re-encoded once on the way out (lossy, lower
                    # bitrate). MP3s are already compressed by audio_detector_service
                    # at encode time — don't touch the bytes.
                    if is_audio:
                        # No-op for audio: MP3 is already compressed.
                        pass
                    elif enable_compression and not compressed_marker.exists():
                        compressed_ok = compress_and_resize_atomic(
                            img,
                            quality=jpeg_quality,
                            max_dim=max_dim,
                            strip_exif=strip_exif,
                        )
                        if compressed_ok:
                            try:
                                compressed_marker.write_text("ok", encoding="utf-8")
                            except Exception:
                                pass
                            print(f"[NODE] Compressed {img.name}")
                        else:
                            print(f"[NODE] Using original bytes for {img.name}")
                    elif enable_compression:
                        print(f"[NODE] Already compressed {img.name}")
                    else:
                        print(f"[NODE] Compression disabled for {img.name}")

                    sid = make_stable_session_id(device_id32, img)
                    try:
                        data, file_size, file_name_b, total_chunks, rf_for = build_chunks_fixed_session(img, chunk, sid)
                    except Exception as e:
                        # Unreadable, or too big for the 16-bit chunk count (~13 MB):
                        # this file can never be sent, so quarantine it immediately
                        # rather than let it wedge the head of the queue forever.
                        print(f"[NODE] Cannot prepare {img.name} ({e}); quarantining.")
                        quarantine_file(str(img), reason=f"chunking failed: {e}",
                                        extra_suffixes=(".compressed", ".det.json"))
                        continue
                    local_crc = binascii.crc32(data) & 0xFFFFFFFF

                    rd = (
                        b"RD"
                        + struct.pack(">I", sid)
                        + struct.pack(">I", file_size)
                        + struct.pack(">B", len(file_name_b))
                        + file_name_b
                    )

                    print(f"[NODE] Sending: {img.name} size={file_size} sid={sid:08x} crc32={local_crc:08x}")

                    if not wait_for_go(ser, sid, rd, peer64, rd_interval=rd_interval, go_timeout=go_timeout):
                        go_timeout_failures += 1
                        print(f"[NODE] GO timeout sid={sid:08x}. File kept for retry. failures={go_timeout_failures}")

                        if go_timeout_failures >= max_go_failures_before_rediscover:
                            print("[NODE] Too many GO timeouts; clearing cached dest64 and rediscovering peer...")
                            cfg["xbee"]["dest64"] = ""
                            try:
                                save_config(config_path, cfg)
                            except Exception as e:
                                print(f"[NODE] Warning: could not clear dest64 in config: {e}")

                            peer64 = resolve_peer64_forever(cfg, config_path, ser, retry_sec=5.0)
                            go_timeout_failures = 0

                        time.sleep(retry_sleep_sec)
                        continue

                    go_timeout_failures = 0

                    t0 = time.time()

                    if txstatus == "on":
                        try:
                            send_windowed_with_txstatus(
                                ser=ser,
                                peer64=peer64,
                                total_chunks=total_chunks,
                                rf_for=rf_for,
                                window=window,
                                retries=retries,
                                status_timeout=status_timeout,
                                pace_ms=pace_ms,
                            )
                        except Exception as e:
                            print(f"[NODE] Chunk send failure for {img.name}: {e}. File kept for retry.")
                            if record_failure(str(img), max_send_attempts, reason=f"chunk send failure: {e}",
                                              extra_suffixes=(".compressed", ".det.json")):
                                print(f"[NODE] QUARANTINED after {max_send_attempts} attempts: {img.name}")
                            time.sleep(retry_sleep_sec)
                            continue
                    else:
                        fid = 0
                        pace = pace_ms / 1000.0
                        for idx in range(total_chunks):
                            ser.write(build_tx_request_0x10(fid, peer64, rf_for(idx)))
                            if pace > 0:
                                ser.flush()
                                time.sleep(pace)
                            if idx % 50 == 0 or idx == total_chunks - 1:
                                print(f"[NODE] sent chunk {idx+1}/{total_chunks}")
                        ser.flush()

                    dt = time.time() - t0
                    kbps = (file_size * 8) / (dt * 1000) if dt > 0 else 0.0
                    print(f"[NODE] Sent chunks sid={sid:08x} time={dt:.1f}s rate~{kbps:.1f} kbps; waiting OK...")

                    peer_crc = wait_for_ok(ser, sid, ok_timeout=ok_timeout)
                    if peer_crc is None:
                        print(f"[NODE] OK timeout sid={sid:08x}. File kept for retry.")
                        time.sleep(retry_sleep_sec)
                        continue

                    if peer_crc != local_crc:
                        print(f"[NODE] CRC mismatch sid={sid:08x} peer={peer_crc:08x} local={local_crc:08x}. File kept for retry.")
                        if record_failure(str(img), max_send_attempts, reason="repeated CRC mismatch",
                                          extra_suffixes=(".compressed", ".det.json")):
                            print(f"[NODE] QUARANTINED after {max_send_attempts} attempts: {img.name}")
                        time.sleep(retry_sleep_sec)
                        continue

                    if safe_delete(img):
                        # Sibling cleanup: the .compressed marker only applies
                        # to JPEGs (audio has no equivalent), but unlink()
                        # silently handles FileNotFoundError so we can list
                        # both unconditionally.
                        for sibling in (
                            compressed_marker,
                            img.with_name(img.name + ".det.json"),
                        ):
                            try:
                                sibling.unlink()
                            except FileNotFoundError:
                                pass
                            except Exception:
                                pass
                        print(f"[NODE] OK received sid={sid:08x} crc32 match. DELETED: {img.name}")
                        clear_failure(str(img))  # drop any stale failure counter
                        # Advance the round-robin counter only on confirmed delivery.
                        # Failed/retried sends stay on the current pool so we don't
                        # accidentally skew the rotation toward whichever side has
                        # transient issues.
                        picker.mark_sent()
                    else:
                        print(f"[NODE] OK received sid={sid:08x} crc32 match. (but could not delete) {img.name}")

                    if not args.once:
                        time.sleep(0.2)

                except (serial.SerialException, OSError) as e:
                    print(f"[NODE] Serial fault: {e}")
                    ser = reopen_serial(ser, port, baud)
                    peer64 = resolve_peer64_forever(cfg, config_path, ser, retry_sec=5.0)
                    time.sleep(1.0)

        except KeyboardInterrupt:
            print("\n[NODE] Exiting.")
        finally:
            try:
                ser.close()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        # e.g. radio absent past the serial-open retry cap. Exit non-zero so the
        # launcher logs it and moves on to the next cycle instead of hanging.
        print(f"[NODE] fatal: {e}")
        raise SystemExit(1)