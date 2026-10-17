#!/usr/bin/env python3
import json
import time
import struct
import argparse
import fcntl
from pathlib import Path
from typing import Optional

import serial

START_DELIM = 0x7E
LOCK_FILE = "/home/sparrow/robin/xbee_serial.lock"


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
            print("[SIGMON] short length field")
            continue
        length = struct.unpack(">H", ln)[0]
        data = ser.read(length)
        if len(data) != length:
            print(f"[SIGMON] short frame body got={len(data)} expected={length}")
            continue
        cks = ser.read(1)
        if len(cks) != 1:
            print("[SIGMON] missing checksum byte")
            continue
        if ((sum(data) + cks[0]) & 0xFF) != 0xFF:
            print("[SIGMON] checksum fail")
            continue
        return data
    return None


def xbee_local_at(ser: serial.Serial, at_cmd: str, parameter: bytes = b"", timeout_s: float = 2.0, frame_id: int = 0x61) -> Optional[bytes]:
    print(f"[SIGMON] AT {at_cmd} ->")
    ser.write(build_at_command_0x08(frame_id, at_cmd, parameter))
    ser.flush()

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        frame = read_api_frame(ser, timeout_s=0.25)
        if not frame:
            continue

        print(f"[SIGMON] AT RX frame type=0x{frame[0]:02X} len={len(frame)} data={frame.hex()}")

        if frame[0] != 0x88 or len(frame) < 5:
            continue
        if frame[1] != frame_id:
            continue
        if frame[2:4] != at_cmd.encode("ascii"):
            continue

        status = frame[4]
        payload = frame[5:]
        print(f"[SIGMON] AT {at_cmd} status=0x{status:02X} payload={payload.hex()}")
        if status != 0x00:
            return None
        return payload

    print(f"[SIGMON] AT {at_cmd} timeout")
    return None


def next_frame_id(cur: int) -> int:
    return (cur % 255) + 1


def load_config(config_path: Path) -> dict:
    return json.loads(config_path.read_text(encoding="utf-8"))


def save_rssi_json(path: Path, ok: bool, dbm: Optional[int], note: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "timestamp": time.time(),
        "ok": ok,
        "rssi_dbm": dbm,
        "note": note,
    }
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    print(f"[SIGMON] wrote {path} ok={ok} dbm={dbm} note={note}")


def poll_peer_once(ser: serial.Serial, peer64: int, repeat_sec: float, timeout_sec: float) -> tuple[bool, Optional[int], str]:
    fid = 1
    deadline = time.time() + timeout_sec
    next_poll = 0.0

    print(f"[SIGMON] poll_peer_once peer64={peer64:016X} repeat_sec={repeat_sec} timeout_sec={timeout_sec}")

    while time.time() < deadline:
        if time.time() >= next_poll:
            tx = build_tx_request_0x10(fid, peer64, b"SG?")
            print(f"[SIGMON] SG? -> peer64={peer64:016X} fid={fid} bytes={tx.hex()}")
            ser.write(tx)
            ser.flush()
            fid = next_frame_id(fid)
            next_poll = time.time() + repeat_sec

        frame = read_api_frame(ser, timeout_s=0.25)
        if not frame:
            continue

        print(f"[SIGMON] RX frame type=0x{frame[0]:02X} len={len(frame)} data={frame.hex()}")

        if frame[0] != 0x90 or len(frame) < 12:
            continue

        src64 = int.from_bytes(frame[1:9], "big", signed=False)
        rf = frame[12:]
        print(f"[SIGMON] RX 0x90 from={src64:016X} rf={rf!r} rf_hex={rf.hex()}")

        if rf.startswith(b"SG!"):
            print("[SIGMON] got SG! reply")
            db_payload = xbee_local_at(ser, "DB", timeout_s=1.0, frame_id=0x62)
            dbm = None
            if db_payload and len(db_payload) >= 1:
                dbm = -int(db_payload[0])
            print(f"[SIGMON] local DB -> {dbm}")
            return True, dbm, "SPARROW XBee Link"

    print("[SIGMON] poll timeout")
    return False, None, "SPARROW XBee Link timeout"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/config.json")
    ap.add_argument("--loop", action="store_true")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    xbee = cfg["xbee"]
    mon = cfg.get("signal_monitor", {})

    port = xbee.get("port", "/dev/ttyUSB0")
    baud = int(xbee.get("baud", 115200))

    def _parse_dest64(raw):
        raw = str(raw or "").strip()
        if not raw:
            return None
        try:
            return int(raw.replace("0x", "").replace(" ", ""), 16)
        except ValueError:
            print(f"[SIGMON] ignoring malformed xbee.dest64={raw!r}")
            return None

    peer64 = _parse_dest64(xbee.get("dest64"))
    if peer64 is None:
        print("[SIGMON] No usable xbee.dest64 yet; waiting for discovery.")

    interval_sec = float(mon.get("interval_sec", 15.0))
    poll_timeout_sec = float(mon.get("poll_timeout_sec", 2.0))
    poll_repeat_sec = float(mon.get("poll_repeat_sec", 0.75))
    output_file = Path(mon.get("output_file", "/home/sparrow/robin/rssi.json"))

    output_file.parent.mkdir(parents=True, exist_ok=True)

    while True:
        try:
            cfg = load_config(Path(args.config))
            xbee = cfg["xbee"]
            dest64_raw = str(xbee.get("dest64", "")).strip()
            if dest64_raw:
                parsed = _parse_dest64(dest64_raw)
                if parsed is not None:
                    peer64 = parsed
        except Exception as e:
            print(f"[SIGMON] config reload failed: {e}")

        print(f"[SIGMON] config port={port} baud={baud} peer64={'NONE' if peer64 is None else f'{peer64:016X}'}")

        if peer64 is None:
            save_rssi_json(output_file, False, None, "no peer yet")
            if not args.loop:
                return 0
            time.sleep(interval_sec)
            continue

        got_lock = False
        with open(LOCK_FILE, "w") as lock_fh:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got_lock = True
                print("[SIGMON] lock acquired")
            except BlockingIOError:
                print("[SIGMON] lock busy, uploader owns radio")
                save_rssi_json(output_file, False, None, "radio busy")
            except Exception as e:
                print(f"[SIGMON] lock error: {e}")
                save_rssi_json(output_file, False, None, f"lock error: {e}")

            if got_lock:
                ser = None
                try:
                    print(f"[SIGMON] opening serial {port}@{baud}")
                    ser = serial.Serial(port, baud, timeout=0.05)
                    print("[SIGMON] serial open ok")

                    ok, dbm, note = poll_peer_once(
                        ser=ser,
                        peer64=peer64,
                        repeat_sec=poll_repeat_sec,
                        timeout_sec=poll_timeout_sec,
                    )
                    save_rssi_json(output_file, ok, dbm, note)
                except Exception as e:
                    print(f"[SIGMON] serial/poll error: {e}")
                    save_rssi_json(output_file, False, None, f"serial error: {e}")
                finally:
                    try:
                        if ser is not None:
                            ser.close()
                            print("[SIGMON] serial closed")
                    except Exception as e:
                        print(f"[SIGMON] serial close error: {e}")

        if not args.loop:
            return 0

        time.sleep(interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())