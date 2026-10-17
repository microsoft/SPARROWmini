#!/usr/bin/env python3
"""
detector_service.py

Runs MegaDetector (YOLOv10 ONNX) over the .jpg backlog in dest_dir.
Pi Zero W2 friendly: onnxruntime + numpy only (no torch).

Per image:
  - Letterbox to detector.input_size (square)
  - Run ONNX session, then greedy NMS at detector.conf_threshold
  - If any detection survives: write empty `<name>.jpg.det` sibling, keep JPG
  - Else, if detector.keep_blanks is true: write `.det`, keep JPG
  - Else (blank + keep_blanks=false): delete JPG, no marker

Files with an existing `.det` sibling are skipped (already processed).
Writes detector_status.json (atomic) summarising the last run for the web UI.

Relay-mode contract: detector writes the sidecar to disk but it stays local.
The XBee sender never transmits detection metadata; the hub re-classifies whatever
makes it across the radio.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
from PIL import Image, ImageFile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "lib"))
from robust import record_failure  # noqa: E402  (poison-file quarantine)

ImageFile.LOAD_TRUNCATED_IMAGES = True

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "config.json")

DETECTOR_DEFAULTS = {
    "enabled": True,
    "model_dir": "models/MDV6-yolov10-c",
    "send_mode": "detections_only",  # legacy; kept for old configs
    "keep_blanks": False,
    "conf_threshold": 0.25,
    "iou_threshold": 0.5,
    "input_size": 640,
    "num_threads": 4,
    "status_file": "/home/sparrow/robin/detector_status.json",
    "sidecar_suffix": ".det.json",
}


# -----------------------------------------------------------------------------
# Logging (same dual-handler pattern as the rest of the services)
# -----------------------------------------------------------------------------

def _setup_logger() -> logging.Logger:
    log_dir = os.path.join(PROJECT_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "detector.log")
    daily_file = os.path.join(
        log_dir, f"detector_{datetime.now().strftime('%Y%m%d')}.log"
    )

    lg = logging.getLogger("detector")
    lg.setLevel(logging.INFO)
    lg.handlers.clear()
    lg.propagate = False

    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    for h in (
        logging.StreamHandler(),
        logging.FileHandler(log_file),
        logging.FileHandler(daily_file),
    ):
        h.setFormatter(fmt)
        h.setLevel(logging.INFO)
        lg.addHandler(h)
    return lg


log = _setup_logger()


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

def load_cfg() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error("could not read %s: %s", CONFIG_PATH, e)
        return {}


def detector_cfg(cfg: dict) -> dict:
    raw = cfg.get("detector", {}) if isinstance(cfg, dict) else {}
    merged = dict(DETECTOR_DEFAULTS)
    merged.update({k: v for k, v in raw.items() if v is not None})
    # Back-compat: an older config used send_mode="detections_only"|"all".
    if "keep_blanks" not in raw and "send_mode" in raw:
        merged["keep_blanks"] = raw["send_mode"] == "all"
    return merged


def load_labels(model_dir: Path) -> list[str]:
    # labels.txt format: "<name>,<id>" per line. We honour the id order.
    p = model_dir / "labels.txt"
    if not p.exists():
        return []
    labels: list[tuple[int, str]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) >= 2:
            try:
                labels.append((int(parts[1]), parts[0].strip()))
            except ValueError:
                continue
        else:
            labels.append((len(labels), parts[0].strip()))
    labels.sort(key=lambda x: x[0])
    return [name for _, name in labels]


# -----------------------------------------------------------------------------
# Preprocess / postprocess (numpy only)
# -----------------------------------------------------------------------------

def letterbox_np(img: Image.Image, new_size: int) -> tuple[np.ndarray, float, int, int]:
    """Resize with aspect-preserving pad to (new_size, new_size).

    Returns:
        (chw_float32_in_0_1, gain, pad_x, pad_y)
    """
    w, h = img.size
    gain = min(new_size / w, new_size / h)
    new_w, new_h = int(round(w * gain)), int(round(h * gain))
    pad_x = (new_size - new_w) // 2
    pad_y = (new_size - new_h) // 2

    resized = img.resize((new_w, new_h), Image.BILINEAR)
    canvas = Image.new("RGB", (new_size, new_size), (114, 114, 114))
    canvas.paste(resized, (pad_x, pad_y))

    arr = np.asarray(canvas, dtype=np.float32) / 255.0  # HWC RGB
    arr = np.transpose(arr, (2, 0, 1))  # CHW
    return arr, gain, pad_x, pad_y


def numpy_nms(boxes: np.ndarray, scores: np.ndarray, iou_thres: float) -> list[int]:
    """Greedy NMS. boxes: [N,4] xyxy, scores: [N]. Returns kept indices."""
    if boxes.size == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1).clip(min=0) * (y2 - y1).clip(min=0)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        union = areas[i] + areas[rest] - inter
        iou = np.where(union > 0, inter / union, 0.0)
        order = rest[iou <= iou_thres]
    return keep


def decode_yolo_output(
    output: np.ndarray, conf_thres: float, iou_thres: float
) -> np.ndarray:
    """Return [M, 6] (x1, y1, x2, y2, conf, cls) in letterbox coords.

    Handles two common YOLO export shapes:
      (1, N, 6)  -> YOLOv10-style: already (xyxy, conf, cls)
      (1, 4+nc, N) or (1, N, 4+nc) -> classic: (cx,cy,w,h) + per-class scores
    """
    if output.ndim != 3 or output.shape[0] != 1:
        log.error("unexpected detector output shape: %s", output.shape)
        return np.zeros((0, 6), dtype=np.float32)

    arr = output[0]

    # Case A: (N, 6) — YOLOv10 NMS-free output
    if arr.ndim == 2 and arr.shape[1] == 6:
        keep_mask = arr[:, 4] >= conf_thres
        return arr[keep_mask].astype(np.float32)

    # Case B: (4 + nc, N) -> transpose to (N, 4 + nc)
    if arr.ndim == 2 and arr.shape[0] <= 32 and arr.shape[1] > 32:
        arr = arr.T

    if arr.ndim != 2 or arr.shape[1] < 5:
        log.error("can't interpret detector output (after transpose): %s", arr.shape)
        return np.zeros((0, 6), dtype=np.float32)

    cxcywh = arr[:, :4]
    scores_per_class = arr[:, 4:]
    cls_id = np.argmax(scores_per_class, axis=1)
    cls_conf = scores_per_class[np.arange(scores_per_class.shape[0]), cls_id]

    keep_mask = cls_conf >= conf_thres
    if not keep_mask.any():
        return np.zeros((0, 6), dtype=np.float32)

    cxcywh = cxcywh[keep_mask]
    cls_conf = cls_conf[keep_mask]
    cls_id = cls_id[keep_mask]

    x1 = cxcywh[:, 0] - cxcywh[:, 2] / 2
    y1 = cxcywh[:, 1] - cxcywh[:, 3] / 2
    x2 = cxcywh[:, 0] + cxcywh[:, 2] / 2
    y2 = cxcywh[:, 1] + cxcywh[:, 3] / 2
    boxes = np.stack([x1, y1, x2, y2], axis=1)

    kept = numpy_nms(boxes, cls_conf, iou_thres)
    if not kept:
        return np.zeros((0, 6), dtype=np.float32)
    kept_arr = np.asarray(kept, dtype=np.int64)
    return np.concatenate(
        [
            boxes[kept_arr],
            cls_conf[kept_arr, None],
            cls_id[kept_arr, None].astype(np.float32),
        ],
        axis=1,
    )


# -----------------------------------------------------------------------------
# Detector session
# -----------------------------------------------------------------------------

class Detector:
    def __init__(self, model_path: Path, input_size: int, num_threads: int) -> None:
        import onnxruntime as ort  # imported lazily so the launcher cleanup works on hosts without ORT

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, int(num_threads))
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            str(model_path),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self.input_name = self.session.get_inputs()[0].name
        self.input_size = int(input_size)
        log.info(
            "loaded ONNX model: %s (input=%s)",
            model_path, self.session.get_inputs()[0].shape,
        )

    def infer(self, jpg: Path) -> tuple[np.ndarray, float, int, int, tuple[int, int]]:
        with Image.open(jpg) as im:
            im.load()
            if im.mode != "RGB":
                im = im.convert("RGB")
            orig_size = im.size  # (w, h)
            chw, gain, pad_x, pad_y = letterbox_np(im, self.input_size)
        batch = np.expand_dims(chw, axis=0)
        out = self.session.run(None, {self.input_name: batch})[0]
        return out, gain, pad_x, pad_y, orig_size


# -----------------------------------------------------------------------------
# Backlog walk
# -----------------------------------------------------------------------------

def stable_jpgs(dest_dir: Path, stable_sec: float, sidecar_suffix: str) -> Iterable[Path]:
    """Yield JPGs older than stable_sec that don't already have a sidecar.

    Oldest-first so any retry-stuck file gets processed before fresh arrivals.
    The sidecar JSON is both the result and the "already analyzed" marker.
    """
    now = time.time()
    candidates = sorted(
        (p for p in dest_dir.iterdir() if p.is_file() and p.suffix.lower() == ".jpg"),
        key=lambda p: p.stat().st_mtime,
    )
    for p in candidates:
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        if st.st_size <= 0:
            continue
        # mtime may be in the future when usb_import_service stamps EXIF
        # DateTimeOriginal and the camera's clock is ahead of the Pi's. A future
        # mtime means the file isn't "still being written" — treat it as stable.
        age = now - st.st_mtime
        if 0 <= age < stable_sec:
            continue
        if p.with_name(p.name + sidecar_suffix).exists():
            continue
        yield p


def write_status_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def captured_at_from_name(jpg_name: str, fallback_mtime: float) -> str:
    """Parse the YYYYMMDDHHMMSS timestamp from {seq:02d}_{ts}.jpg, ISO-format it.

    Mirrors the idiom in sparrow-pi/sparrow/inference.py so the server sees
    identical `captured_at` strings whether the upload comes from a relay's
    hub or directly from a robin in direct mode.
    """
    try:
        ts_part = jpg_name.split("_")[-1].split(".")[0][:14]
        dt = datetime.strptime(ts_part, "%Y%m%d%H%M%S")
    except Exception:
        dt = datetime.utcfromtimestamp(fallback_mtime)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def unletterbox_boxes(boxes_lb: np.ndarray, gain: float, pad_x: int, pad_y: int,
                      orig_w: int, orig_h: int) -> np.ndarray:
    """Map [N,4] xyxy boxes from letterbox space back to original-image pixels."""
    if boxes_lb.size == 0:
        return boxes_lb
    boxes = boxes_lb.copy().astype(np.float32)
    boxes[:, [0, 2]] -= pad_x
    boxes[:, [1, 3]] -= pad_y
    boxes /= max(gain, 1e-9)
    boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, orig_w - 1)
    boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, orig_h - 1)
    return boxes


def write_sidecar(jpg: Path, suffix: str, payload: dict) -> None:
    """Atomic .tmp -> rename sidecar write next to the JPG."""
    side = jpg.with_name(jpg.name + suffix)
    tmp = side.with_suffix(side.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, side)
    except Exception as e:
        log.warning("could not write sidecar %s: %s", side, e)
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def run_once(cfg: dict) -> dict:
    det_cfg = detector_cfg(cfg)
    model_dir = Path(det_cfg["model_dir"])
    if not model_dir.is_absolute():
        model_dir = Path(PROJECT_ROOT) / model_dir
    model_path = model_dir / "1" / "model.onnx"
    if not model_path.exists():
        log.error("ONNX model not found at %s; skipping detector cycle", model_path)
        return {"error": f"model not found: {model_path}"}

    labels = load_labels(model_dir)
    log.info("model=%s labels=%s", model_dir.name, labels)

    dest_dir = Path(cfg.get("dest_dir", "/home/sparrow/robin/images"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    stable_sec = float(cfg.get("image", {}).get("stable_file_sec", 1.0))

    det = Detector(
        model_path=model_path,
        input_size=det_cfg["input_size"],
        num_threads=det_cfg["num_threads"],
    )

    conf_thres = float(det_cfg["conf_threshold"])
    iou_thres = float(det_cfg["iou_threshold"])
    keep_blanks = bool(det_cfg["keep_blanks"])
    sidecar_suffix = str(det_cfg["sidecar_suffix"])

    t_start = time.time()
    processed = 0
    with_detections = 0
    blanks_dropped = 0
    blanks_kept = 0
    errors = 0
    per_class: dict[str, int] = {}

    for jpg in stable_jpgs(dest_dir, stable_sec, sidecar_suffix):
        try:
            t0 = time.time()
            output, gain, pad_x, pad_y, orig_size = det.infer(jpg)
            dets = decode_yolo_output(output, conf_thres, iou_thres)
            dt = time.time() - t0
            n = int(dets.shape[0])
            processed += 1

            orig_w, orig_h = orig_size
            boxes_px = unletterbox_boxes(dets[:, :4], gain, pad_x, pad_y, orig_w, orig_h)

            detection_records: list[dict] = []
            for i in range(n):
                cls = int(dets[i, 5])
                label_name = labels[cls] if 0 <= cls < len(labels) else f"cls{cls}"
                per_class[label_name] = per_class.get(label_name, 0) + 1
                x1, y1, x2, y2 = boxes_px[i].tolist()
                detection_records.append({
                    "label": label_name,
                    "confidence": float(dets[i, 4]),
                    "bbox": [int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))],
                    "class_id": cls,
                })

            sidecar = {
                "image_name": jpg.name,
                "captured_at": captured_at_from_name(jpg.name, jpg.stat().st_mtime),
                "image_width": orig_w,
                "image_height": orig_h,
                "detections": detection_records,
            }

            if n > 0:
                write_sidecar(jpg, sidecar_suffix, sidecar)
                with_detections += 1
                log.info("%s: %d detection(s) in %.2fs", jpg.name, n, dt)
            else:
                if keep_blanks:
                    write_sidecar(jpg, sidecar_suffix, sidecar)
                    blanks_kept += 1
                    log.info("%s: blank, kept (keep_blanks=true) in %.2fs", jpg.name, dt)
                else:
                    try:
                        jpg.unlink()
                        blanks_dropped += 1
                        log.info(
                            "%s: blank, deleted (keep_blanks=false) in %.2fs",
                            jpg.name, dt,
                        )
                        comp = jpg.with_name(jpg.name + ".compressed")
                        if comp.exists():
                            try:
                                comp.unlink()
                            except Exception:
                                pass
                    except FileNotFoundError:
                        pass
                    except Exception as e:
                        log.error("could not delete blank %s: %s", jpg, e)
                        errors += 1
        except Exception as e:
            errors += 1
            log.exception("inference failed for %s: %s", jpg, e)
            # Poison-file guard: a JPG that always throws (truncated/corrupt) would
            # be re-decoded every cycle forever and never leave the backlog.
            if record_failure(str(jpg), max_attempts=5, reason=f"inference failed: {e}"):
                log.warning("quarantined image after repeated inference failures: %s", jpg.name)

    duration = time.time() - t_start
    summary = {
        "timestamp": time.time(),
        "model": model_dir.name,
        "labels": labels,
        "enabled": True,
        "keep_blanks": keep_blanks,
        "conf_threshold": conf_thres,
        "iou_threshold": iou_thres,
        "input_size": det_cfg["input_size"],
        "last_run": {
            "processed": processed,
            "with_detections": with_detections,
            "blanks_dropped": blanks_dropped,
            "blanks_kept": blanks_kept,
            "errors": errors,
            "duration_sec": round(duration, 2),
            "per_class": per_class,
        },
    }
    log.info(
        "cycle done: processed=%d with_det=%d blanks_dropped=%d blanks_kept=%d "
        "errors=%d in %.2fs per_class=%s",
        processed, with_detections, blanks_dropped, blanks_kept, errors,
        duration, per_class,
    )
    return summary


def write_disabled_status(status_path: Path, det_cfg: dict, model_dir: Path) -> None:
    payload = {
        "timestamp": time.time(),
        "model": model_dir.name,
        "labels": load_labels(model_dir),
        "enabled": False,
        "keep_blanks": bool(det_cfg["keep_blanks"]),
        "conf_threshold": float(det_cfg["conf_threshold"]),
        "iou_threshold": float(det_cfg["iou_threshold"]),
        "input_size": det_cfg["input_size"],
        "last_run": None,
    }
    try:
        write_status_atomic(status_path, payload)
    except Exception as e:
        log.warning("could not write disabled status: %s", e)


def main() -> int:
    ap = argparse.ArgumentParser(description="Robin on-device detector (MegaDetector ONNX).")
    ap.add_argument("--once", action="store_true",
                    help="Process all eligible images then exit (the launcher uses this).")
    args = ap.parse_args()

    cfg = load_cfg()
    det_cfg = detector_cfg(cfg)
    status_path = Path(det_cfg["status_file"])
    model_dir = Path(det_cfg["model_dir"])
    if not model_dir.is_absolute():
        model_dir = Path(PROJECT_ROOT) / model_dir

    if not det_cfg["enabled"]:
        log.info("detector disabled (detector.enabled=false); exiting")
        write_disabled_status(status_path, det_cfg, model_dir)
        return 0

    # A crash in run_once (bad config coercion, ONNX/model failure) must not take
    # the process down silently — write an error status so the web UI shows it.
    try:
        summary = run_once(cfg)
    except Exception as e:
        log.exception("detector run_once failed: %s", e)
        summary = {"timestamp": time.time(), "enabled": True, "error": str(e), "last_run": None}
    try:
        write_status_atomic(status_path, summary)
    except Exception as e:
        log.warning("could not write status file %s: %s", status_path, e)

    if not args.once:
        # Loop mode mirrors the other services, but the launcher always uses --once.
        scan_interval = float(cfg.get("detector", {}).get("scan_interval_sec", 30.0))
        while True:
            time.sleep(scan_interval)
            summary = run_once(cfg)
            try:
                write_status_atomic(status_path, summary)
            except Exception as e:
                log.warning("could not write status file %s: %s", status_path, e)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
