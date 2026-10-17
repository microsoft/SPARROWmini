#!/usr/bin/env python3
"""
robust.py — shared robustness helpers for the Robin field node (stdlib only).

Everything here is best-effort and defensive: an unattended Pi in the field must
never crash, hang, corrupt state, or fill its disk. These helpers are imported by
services in capture/, detect/, transport/ and web/ (all put lib/ on sys.path).

- atomic_write_text / atomic_write_json : tmp + fsync + os.replace (no torn files)
- read_json                             : tolerant read (returns default on any error)
- free_mb / have_min_free_mb            : disk-space guard for pause-at-low-disk
- file_lock / flock_bounded             : cross-process lock that can't hang forever
- update_config_atomic                  : re-read-under-lock + apply delta + atomic write
- quarantine_file / record_failure      : drain poison files so a backlog always empties
"""

import os
import json
import time
import fcntl
import shutil
import contextlib


# --------------------------------------------------------------------------- IO

def atomic_write_text(path, content, mode=0o644):
    """Write `content` to `path` atomically (tmp in same dir → fsync → replace)."""
    path = str(path)
    d = os.path.dirname(path) or "."
    tmp = os.path.join(d, f".{os.path.basename(path)}.tmp.{os.getpid()}")
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


def atomic_write_json(path, obj, mode=0o644):
    atomic_write_text(path, json.dumps(obj, indent=2) + "\n", mode)


def read_json(path, default=None):
    """Read JSON, returning `default` on any error (missing/torn/invalid)."""
    try:
        with open(str(path), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ------------------------------------------------------------------------- disk

def free_mb(path):
    """Free MB on the filesystem holding `path` (its nearest existing dir).
    Returns +inf if it can't be determined, so a probe failure never blocks."""
    p = str(path)
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    try:
        return shutil.disk_usage(p or "/").free / (1024.0 * 1024.0)
    except OSError:
        return float("inf")


def have_min_free_mb(path, min_mb):
    try:
        return free_mb(path) >= float(min_mb)
    except (TypeError, ValueError):
        return True


# ------------------------------------------------------------------------- lock

def flock_bounded(fh, timeout_sec, poll=0.2):
    """Acquire an exclusive flock within `timeout_sec` using non-blocking retries.
    Returns True on success, False on timeout — never blocks forever."""
    deadline = time.time() + float(timeout_sec)
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.time() >= deadline:
                return False
            time.sleep(poll)


@contextlib.contextmanager
def file_lock(lock_path, timeout_sec=10, poll=0.2):
    """Cross-process exclusive lock with a deadline. Raises TimeoutError if the
    lock can't be taken in time (caller decides how to degrade)."""
    fh = open(str(lock_path), "w")
    try:
        if not flock_bounded(fh, timeout_sec, poll):
            raise TimeoutError(f"could not acquire {lock_path} within {timeout_sec}s")
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def update_config_atomic(config_path, mutate, lock_timeout=10):
    """Serialize a config read-modify-write across processes: take a lock, RE-READ
    the file from disk, apply `mutate(cfg)` in place, then atomically write it back.
    Prevents both torn files (power loss) and lost updates (concurrent writers)."""
    config_path = str(config_path)
    with file_lock(config_path + ".lock", lock_timeout):
        cfg = read_json(config_path, default={}) or {}
        mutate(cfg)
        atomic_write_json(config_path, cfg)
        return cfg


# -------------------------------------------------------------------- poison files

FAIL_SUFFIX = ".fail"


def _quarantine_dir(path):
    d = os.path.join(os.path.dirname(os.path.abspath(str(path))), ".quarantine")
    os.makedirs(d, exist_ok=True)
    return d


def quarantine_file(path, reason="", extra_suffixes=()):
    """Move `path` (and any sibling `path+suffix`) into a `.quarantine/` subdir so
    it stops being reprocessed and the backlog can drain. Best-effort; never raises.
    Returns the new path, or None on failure."""
    path = str(path)
    try:
        qd = _quarantine_dir(path)
        dest = os.path.join(qd, os.path.basename(path))
        i = 1
        while os.path.exists(dest):
            dest = os.path.join(qd, f"{os.path.basename(path)}.{i}")
            i += 1
        os.replace(path, dest)
        for suf in extra_suffixes:
            s = path + suf
            if os.path.exists(s):
                try:
                    os.replace(s, dest + suf)
                except OSError:
                    pass
        if reason:
            try:
                atomic_write_text(dest + ".why.txt", reason.rstrip("\n") + "\n")
            except OSError:
                pass
        return dest
    except OSError:
        return None


def record_failure(path, max_attempts, reason="", extra_suffixes=()):
    """Increment a persisted per-file failure counter (`path`+`.fail`). Once it
    reaches `max_attempts`, quarantine the file (plus its counter and any extras)
    and return True; otherwise return False. Lets `--once` services drain a poison
    file across cycles instead of retrying it forever. Best-effort."""
    fp = path + FAIL_SUFFIX
    try:
        with open(fp, "r", encoding="utf-8") as f:
            n = int((f.read().strip() or "0"))
    except (OSError, ValueError):
        n = 0
    n += 1
    try:
        atomic_write_text(fp, str(n) + "\n")
    except OSError:
        pass
    if n >= int(max_attempts):
        quarantine_file(
            path,
            reason=f"{reason} (after {n} attempts)",
            extra_suffixes=tuple(extra_suffixes) + (FAIL_SUFFIX,),
        )
        return True
    return False


def clear_failure(path):
    """Drop the failure counter after a successful process. Best-effort."""
    try:
        os.remove(str(path) + FAIL_SUFFIX)
    except OSError:
        pass
