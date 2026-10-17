#!/usr/bin/env python3
"""
audio_detector_service.py

Runs MD Audio Birds V1 (binary bird sigmoid, ONNX) over the .wav backlog in
`audio_dir`. Pi Zero W2 friendly memory-wise: this script is invoked as a
`--once` subprocess by the launcher, so the ONNX session + librosa state are
torn down between cycles. The vision detector runs in a separate subprocess
ahead of this one — they never coexist in RAM.

Per WAV:
  - Decode (soundfile), downmix to mono, resample to 48 kHz if needed
  - Compute the FULL mel spectrogram once (Slaney scale + Slaney filter norm,
    n_fft=2048, hop=512, n_mels=224 — matches training-time front-end)
  - Slide a 1.0 s window with 0.3 s stride; per window run ONNX inference,
    record bird-confidence
  - If max(confidence) >= conf_threshold: write `<name>.wav.det.json` sidecar
    listing every above-threshold window, AND encode `<name>.mp3` sibling for
    downstream transport, then DELETE the WAV
  - Else, if keep_blanks=true: write empty-detections sidecar, encode MP3, keep
  - Else (no detection, keep_blanks=false): delete the WAV (no MP3, no sidecar)

Sidecar schema mirrors the image detector's so the hub / cloud can route
audio metadata uniformly:

    {
      "audio_name": "01_20260609143022.wav",
      "captured_at": "2026-06-09T14:30:22Z",
      "duration_sec": 60.0,
      "sample_rate": 48000,
      "detections": [
        {"label": "bird", "confidence": 0.95,
         "start_sec": 12.3, "end_sec": 13.3, "class_id": 0},
        ...
      ]
    }

Relay-mode contract: the MP3 is what crosses the XBee (the .wav is discarded
locally once encoded). Direct-mode contract: the MP3 is also what gets POSTed
to /v1/uploads/audio (per user decision — bandwidth saver on cellular).
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
import soundfile as sf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
_LIB_DIR = os.path.join(PROJECT_ROOT, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from audio_codec import encode_mp3_atomic  # noqa: E402
from robust import record_failure  # noqa: E402  (poison-file quarantine)

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "config.json")

AUDIO_DETECTOR_DEFAULTS = {
    "enabled": True,
    "model_dir": "models/md-audiobirds-v1",
    "keep_blanks": False,
    "conf_threshold": 0.9,
    "window_sec": 1.0,
    "stride_sec": 0.3,
    "sample_rate": 48000,
    "n_fft": 2048,
    "hop_length": 512,
    "n_mels": 224,
    "fmin": 0,
    "fmax": 24000,
    "top_db": 80.0,
    "num_threads": 4,
    "status_file": "/home/sparrow/robin/audio_detector_status.json",
    "sidecar_suffix": ".det.json",
}

AUDIO_BITRATE_DEFAULT_KBPS = 48

WAV_EXTS = {".wav"}


# -----------------------------------------------------------------------------
# Logging (matches the project's dual-handler pattern)
# -----------------------------------------------------------------------------

def _setup_logger() -> logging.Logger:
    log_dir = os.path.join(PROJECT_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "audio_detector.log")
    daily_file = os.path.join(
        log_dir, f"audio_detector_{datetime.now().strftime('%Y%m%d')}.log"
    )

    lg = logging.getLogger("audio_detector")
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


def audio_detector_cfg(cfg: dict) -> dict:
    raw = cfg.get("audio_detector", {}) if isinstance(cfg, dict) else {}
    merged = dict(AUDIO_DETECTOR_DEFAULTS)
    merged.update({k: v for k, v in raw.items() if v is not None})
    return merged


def load_labels(model_dir: Path) -> list[str]:
    # Mirror detector_service.load_labels — "name,id" per line.
    p = model_dir / "labels.txt"
    if not p.exists():
        return ["bird"]
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
    return [name for _, name in labels] or ["bird"]


# -----------------------------------------------------------------------------
# Audio I/O + mel spectrogram (librosa)
# -----------------------------------------------------------------------------

def load_wav_mono_48k(path: Path, target_sr: int) -> tuple[np.ndarray, int]:
    """Read WAV, downmix to mono, resample to target_sr if needed.

    Returns (float32 samples in [-1, 1], sample_rate).
    """
    # soundfile reads any libsndfile-supported format. always_2d=True makes the
    # subsequent mean(axis=1) downmix work uniformly for mono and stereo files.
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if data.shape[1] > 1:
        data = data.mean(axis=1)
    else:
        data = data[:, 0]

    if sr != target_sr:
        # librosa.resample is the standard. Keep target dtype float32.
        import librosa
        data = librosa.resample(y=data, orig_sr=sr, target_sr=target_sr, res_type="soxr_hq")
        sr = target_sr
    return np.ascontiguousarray(data, dtype=np.float32), sr


def mel_spectrogram_db(
    audio: np.ndarray,
    sample_rate: int,
    n_fft: int,
    hop_length: int,
    n_mels: int,
    fmin: float,
    fmax: float,
    top_db: float,
) -> np.ndarray:
    """Return mel spectrogram in dB. Slaney scale + Slaney filter norm (training parity).

    Output shape: (n_mels, time_frames).
    """
    import librosa

    # Power mel spectrogram. htk=False -> Slaney scale (librosa default).
    # norm='slaney' -> Slaney filter normalisation. Both match the model card.
    mel = librosa.feature.melspectrogram(
        y=audio,
        sr=sample_rate,
        n_fft=n_fft,
        hop_length=hop_length,
        n_mels=n_mels,
        fmin=fmin,
        fmax=fmax,
        power=2.0,
        htk=False,
        norm="slaney",
        window="hann",
        center=True,
    )
    mel_db = librosa.power_to_db(mel, top_db=top_db)
    return mel_db.astype(np.float32, copy=False)


# -----------------------------------------------------------------------------
# Sliding-window inference
# -----------------------------------------------------------------------------

class AudioDetector:
    """Wraps the ONNX session + sliding-window inference."""

    def __init__(self, model_path: Path, num_threads: int) -> None:
        import onnxruntime as ort  # lazy — keeps module loadable on CI

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, int(num_threads))
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            str(model_path),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self.input_name = self.session.get_inputs()[0].name
        log.info(
            "loaded ONNX model: %s (input=%s)",
            model_path, self.session.get_inputs()[0].shape,
        )

    def infer_windows(
        self,
        mel_db: np.ndarray,
        window_frames: int,
        stride_frames: int,
        batch_size: int = 16,
    ) -> list[tuple[int, float]]:
        """Run sliding-window inference over the precomputed mel spectrogram.

        Args:
            mel_db: (n_mels, T) float32
            window_frames: number of STFT frames per window (e.g. 94 for 1s @ 48k/hop 512)
            stride_frames: number of frames to advance per step
            batch_size: how many windows per ORT call. 16 keeps RSS reasonable
                on the Pi Zero W2 while still giving ~10x throughput vs per-window.

        Returns list of (start_frame, confidence) for every window.
        Confidence is a true [0, 1] probability — the raw ONNX output is a
        logit (matches sparrow-pi/sparrow/audio.py:476) and we apply sigmoid
        here before returning.
        """
        n_mels, total_frames = mel_db.shape

        # Collect every window's start frame and tensor in one pass.
        if total_frames < window_frames:
            padded = np.zeros((n_mels, window_frames), dtype=np.float32)
            padded[:, :total_frames] = mel_db
            chunks = padded[np.newaxis, :, :]  # (1, n_mels, window_frames)
            start_frames = [0]
        else:
            start_frames = list(range(0, total_frames - window_frames + 1, stride_frames))
            chunks = np.stack(
                [mel_db[:, s:s + window_frames] for s in start_frames],
                axis=0,
            )  # (N, n_mels, window_frames)

        # Add the channel axis once: (N, 1, n_mels, window_frames).
        chunks = chunks[:, np.newaxis, :, :]

        # Run in batches so peak RSS stays bounded on the Pi.
        logits_all: list[np.ndarray] = []
        for i in range(0, chunks.shape[0], batch_size):
            batch = chunks[i:i + batch_size]
            out = self.session.run(None, {self.input_name: batch})[0]
            logits_all.append(np.asarray(out).reshape(-1).astype(np.float32, copy=False))

        logits = np.concatenate(logits_all) if logits_all else np.array([], dtype=np.float32)
        # The ONNX export emits raw logits; sigmoid gives bird-probability in [0, 1].
        probs = 1.0 / (1.0 + np.exp(-logits))

        return [(start_frames[i], float(probs[i])) for i in range(len(start_frames))]


# -----------------------------------------------------------------------------
# Backlog walk + sidecar I/O (same idioms as detector_service.py)
# -----------------------------------------------------------------------------

def stable_wavs(audio_dir: Path, stable_sec: float, sidecar_suffix: str) -> Iterable[Path]:
    """Yield WAVs older than stable_sec without an existing sidecar.

    Oldest-first (same FIFO logic as the image detector). Accepts future
    mtimes (camera-clock skew tolerance, mirrors detector_service.stable_jpgs).
    """
    if not audio_dir.exists():
        return
    now = time.time()
    candidates = sorted(
        (p for p in audio_dir.iterdir()
         if p.is_file() and p.suffix.lower() in WAV_EXTS),
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
        if p.with_name(p.name + sidecar_suffix).exists():
            continue
        yield p


def write_sidecar(wav_path: Path, suffix: str, payload: dict) -> None:
    side = wav_path.with_name(wav_path.name + suffix)
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


def write_status_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def captured_at_from_name(wav_name: str, fallback_mtime: float) -> str:
    """Parse YYYYMMDDHHMMSS or YYYYMMDD_HHMMSS from filename, ISO-format it.

    Audio service used `YYYYMMDD_HHMMSS.wav` historically; new format is
    `{seq:02d}_{YYYYMMDDHHMMSS}.wav`. Handle both.
    """
    base = wav_name.split(".")[0]
    # Try the new format first: "01_20260609143022"
    parts = base.split("_")
    candidate = parts[-1] if parts else base
    if len(candidate) >= 14 and candidate[:14].isdigit():
        try:
            dt = datetime.strptime(candidate[:14], "%Y%m%d%H%M%S")
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
    # Try the legacy format: "20260609_143022"
    if len(parts) >= 2 and len(parts[-2]) == 8 and parts[-1][:6].isdigit():
        try:
            dt = datetime.strptime(parts[-2] + parts[-1][:6], "%Y%m%d%H%M%S")
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
    # Fallback: mtime
    return datetime.utcfromtimestamp(fallback_mtime).strftime("%Y-%m-%dT%H:%M:%SZ")


# -----------------------------------------------------------------------------
# One-shot cycle
# -----------------------------------------------------------------------------

def run_once(cfg: dict) -> dict:
    det_cfg = audio_detector_cfg(cfg)
    model_dir = Path(det_cfg["model_dir"])
    if not model_dir.is_absolute():
        model_dir = Path(PROJECT_ROOT) / model_dir
    model_path = model_dir / "1" / "model.onnx"
    if not model_path.exists():
        log.error("ONNX model not found at %s; skipping cycle", model_path)
        return {"error": f"model not found: {model_path}"}

    labels = load_labels(model_dir)
    log.info("model=%s labels=%s threshold=%.2f", model_dir.name, labels,
             float(det_cfg["conf_threshold"]))

    audio_dir = Path(cfg.get("audio_dir", "/home/sparrow/robin/audio"))
    audio_dir.mkdir(parents=True, exist_ok=True)
    stable_sec = float(cfg.get("image", {}).get("stable_file_sec", 1.0))

    sample_rate = int(det_cfg["sample_rate"])
    n_fft = int(det_cfg["n_fft"])
    hop_length = int(det_cfg["hop_length"])
    n_mels = int(det_cfg["n_mels"])
    fmin = float(det_cfg["fmin"])
    fmax = float(det_cfg["fmax"])
    top_db = float(det_cfg["top_db"])
    window_sec = float(det_cfg["window_sec"])
    stride_sec = float(det_cfg["stride_sec"])
    conf_thres = float(det_cfg["conf_threshold"])
    keep_blanks = bool(det_cfg["keep_blanks"])
    sidecar_suffix = str(det_cfg["sidecar_suffix"])

    bitrate_kbps = int(cfg.get("audio", {}).get("encode_bitrate_kbps", AUDIO_BITRATE_DEFAULT_KBPS))

    # Convert window/stride from seconds to STFT-frame counts so we can slide
    # over the precomputed full-clip mel spectrogram (avoids re-STFT per window).
    window_frames = max(1, round(window_sec * sample_rate / hop_length))
    stride_frames = max(1, round(stride_sec * sample_rate / hop_length))

    det = AudioDetector(model_path=model_path, num_threads=det_cfg["num_threads"])

    t_start = time.time()
    processed = 0
    with_detections = 0
    blanks_dropped = 0
    blanks_kept = 0
    errors = 0
    bird_hits_total = 0

    for wav in stable_wavs(audio_dir, stable_sec, sidecar_suffix):
        try:
            t0 = time.time()
            audio, sr = load_wav_mono_48k(wav, sample_rate)
            duration_sec = len(audio) / float(sr)

            mel_db = mel_spectrogram_db(
                audio, sr, n_fft, hop_length, n_mels, fmin, fmax, top_db,
            )
            windows = det.infer_windows(mel_db, window_frames, stride_frames)
            dt = time.time() - t0
            processed += 1

            # Build the sidecar's detection list — every window above threshold.
            label_name = labels[0] if labels else "bird"
            detections = []
            for start_frame, conf in windows:
                if conf < conf_thres:
                    continue
                start_sec = round(start_frame * hop_length / sample_rate, 3)
                end_sec = round(start_sec + window_sec, 3)
                detections.append({
                    "label": label_name,
                    "confidence": round(conf, 4),
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "class_id": 0,
                })
            n_detections = len(detections)
            bird_hits_total += n_detections

            sidecar = {
                "audio_name": wav.name,
                "captured_at": captured_at_from_name(wav.name, wav.stat().st_mtime),
                "duration_sec": round(duration_sec, 3),
                "sample_rate": int(sr),
                "detections": detections,
            }

            if n_detections > 0:
                # Detection: write sidecar, encode MP3, drop the WAV.
                mp3_path = wav.with_suffix(".mp3")
                try:
                    encode_mp3_atomic(wav, mp3_path, bitrate_kbps=bitrate_kbps)
                except Exception as e:
                    log.error("MP3 encode failed for %s: %s — keeping WAV", wav.name, e)
                    errors += 1
                    # A WAV lameenc can never encode would be retried forever; quarantine after N.
                    if record_failure(str(wav), max_attempts=5, reason=f"MP3 encode failed: {e}"):
                        log.warning("quarantined WAV after repeated encode failures: %s", wav.name)
                    continue
                # Sidecar references MP3 (canonical going forward) and WAV
                # gets unlinked. Encode existed so we can clean up safely.
                sidecar["audio_name"] = mp3_path.name
                write_sidecar(mp3_path, sidecar_suffix, sidecar)
                try:
                    wav.unlink()
                except Exception as e:
                    log.warning("could not unlink wav %s: %s", wav, e)
                with_detections += 1
                log.info(
                    "%s: %d detection-window(s) max_conf=%.3f in %.2fs -> %s",
                    wav.name, n_detections,
                    max((d["confidence"] for d in detections), default=0.0),
                    dt, mp3_path.name,
                )
            else:
                if keep_blanks:
                    mp3_path = wav.with_suffix(".mp3")
                    try:
                        encode_mp3_atomic(wav, mp3_path, bitrate_kbps=bitrate_kbps)
                        sidecar["audio_name"] = mp3_path.name
                        write_sidecar(mp3_path, sidecar_suffix, sidecar)
                        try:
                            wav.unlink()
                        except Exception:
                            pass
                        blanks_kept += 1
                        log.info(
                            "%s: blank, kept (keep_blanks=true) in %.2fs -> %s",
                            wav.name, dt, mp3_path.name,
                        )
                    except Exception as e:
                        log.error("blank MP3 encode failed for %s: %s", wav.name, e)
                        errors += 1
                else:
                    try:
                        wav.unlink()
                        blanks_dropped += 1
                        log.info(
                            "%s: blank, deleted (keep_blanks=false) in %.2fs",
                            wav.name, dt,
                        )
                    except FileNotFoundError:
                        pass
                    except Exception as e:
                        log.error("could not delete blank %s: %s", wav, e)
                        errors += 1
        except Exception as e:
            errors += 1
            log.exception("inference failed for %s: %s", wav, e)
            # Poison WAV (undecodable / OOM) would re-fail every cycle; quarantine after N.
            if record_failure(str(wav), max_attempts=5, reason=f"inference failed: {e}"):
                log.warning("quarantined WAV after repeated inference failures: %s", wav.name)

    duration = time.time() - t_start
    summary = {
        "timestamp": time.time(),
        "model": model_dir.name,
        "labels": labels,
        "enabled": True,
        "keep_blanks": keep_blanks,
        "conf_threshold": conf_thres,
        "window_sec": window_sec,
        "stride_sec": stride_sec,
        "sample_rate": sample_rate,
        "last_run": {
            "processed": processed,
            "with_detections": with_detections,
            "blanks_dropped": blanks_dropped,
            "blanks_kept": blanks_kept,
            "errors": errors,
            "duration_sec": round(duration, 2),
            "bird_windows_total": bird_hits_total,
        },
    }
    log.info(
        "cycle done: processed=%d with_det=%d blanks_dropped=%d blanks_kept=%d "
        "errors=%d bird_windows=%d in %.2fs",
        processed, with_detections, blanks_dropped, blanks_kept, errors,
        bird_hits_total, duration,
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
        "last_run": None,
    }
    try:
        write_status_atomic(status_path, payload)
    except Exception as e:
        log.warning("could not write disabled status: %s", e)


def main() -> int:
    ap = argparse.ArgumentParser(description="Robin on-device audio bird detector (MD Audio Birds V1).")
    ap.add_argument("--once", action="store_true",
                    help="Process all eligible WAVs then exit (launcher uses this).")
    args = ap.parse_args()

    cfg = load_cfg()
    det_cfg = audio_detector_cfg(cfg)
    status_path = Path(det_cfg["status_file"])
    model_dir = Path(det_cfg["model_dir"])
    if not model_dir.is_absolute():
        model_dir = Path(PROJECT_ROOT) / model_dir

    if not det_cfg["enabled"]:
        log.info("audio detector disabled (audio_detector.enabled=false); exiting")
        write_disabled_status(status_path, det_cfg, model_dir)
        return 0

    try:
        summary = run_once(cfg)
    except Exception as e:
        log.exception("audio detector run_once failed: %s", e)
        summary = {"timestamp": time.time(), "enabled": True, "error": str(e), "last_run": None}
    try:
        write_status_atomic(status_path, summary)
    except Exception as e:
        log.warning("could not write status file %s: %s", status_path, e)

    if not args.once:
        scan_interval = float(cfg.get("audio_detector", {}).get("scan_interval_sec", 30.0))
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
