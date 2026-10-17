#!/usr/bin/env python3
import os, uuid

# This module lives in lib/; the project root (where device_id.txt and config/
# live) is one level up. device_id.txt defaults to the project dir, matching
# config.json's `device_id_file`. (Was hardcoded to /home/sparrow/config, which
# nothing else used — install.sh then couldn't find the id and aborted.)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
DEFAULT_PATH = os.path.join(PROJECT_ROOT, "device_id.txt")

def _atomic_write(path: str, data: str):
    tmp = path + ".part"
    with open(tmp, "w") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o644)
    except Exception:
        pass

def generate_short_uuid12() -> str:
    return uuid.uuid4().hex[:12]

def ensure_device_id(path: str = DEFAULT_PATH) -> str:
    """
    If the file exists → return its trimmed content.
    If not → create it atomically with a new 12-char UUID and return it.
    """
    try:
        if os.path.isfile(path):
            with open(path, "r") as f:
                return f.read().strip()
        new_id = generate_short_uuid12()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _atomic_write(path, new_id + "\n")
        return new_id
    except Exception as e:
        print(f"[device_id] Error ensuring ID at {path}: {e}")
        # Fallback (not persisted if write failed)
        return generate_short_uuid12()

def read_device_id(path: str = DEFAULT_PATH) -> str:
    with open(path, "r") as f:
        return f.read().strip()

if __name__ == "__main__":
    # Prefer the path from config.json (device_id_file) so we write where the
    # services and install.sh expect it; fall back to the script-relative default.
    import json
    path = DEFAULT_PATH
    try:
        with open(os.path.join(PROJECT_ROOT, "config", "config.json")) as f:
            path = json.load(f).get("device_id_file") or DEFAULT_PATH
    except Exception:
        pass
    print(ensure_device_id(path))
