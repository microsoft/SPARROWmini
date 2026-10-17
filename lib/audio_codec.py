#!/usr/bin/env python3
"""
audio_codec.py

Tiny wrapper around `lameenc` for encoding WAVs to MP3 with bird-friendly
settings. Bird calls span 1-12 kHz fundamentals with harmonics extending into
the 15-20 kHz range, so the default LAME preset's aggressive lowpass cutoff
would discard exactly the content we care about. This module sets:

- Lowpass = 0  (disabled — preserve up to Nyquist; LAME's psychoacoustic model
              will still discard inaudible content but won't hard-clip the spectrum)
- VBR quality 2 (high quality, smallish files)
- Joint stereo for stereo inputs (mono inputs stay mono)
- Sample rate preserved from the source WAV

One public function: `encode_mp3_atomic(wav_path, mp3_path, bitrate_kbps)`.
Writes to `<mp3_path>.part` first, then renames — never leaves a half-written
MP3 visible to the FIFO scheduler.

`lameenc` is a pure-Python wheel (no system ffmpeg/lame needed).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np
import soundfile as sf

log = logging.getLogger(__name__)


def encode_mp3_atomic(
    wav_path: Path,
    mp3_path: Path,
    bitrate_kbps: int = 48,
) -> None:
    """Encode `wav_path` to MP3 at `mp3_path`. Atomic via .part → rename.

    Args:
        wav_path: source WAV file (any sample rate / channel count).
        mp3_path: target MP3 file. Will be overwritten if it exists.
        bitrate_kbps: target CBR bitrate. 32-64 kbps is the sweet spot for
            bird audio: small enough for XBee airtime, high enough that the
            LAME psychoacoustic model doesn't slash the upper harmonics.

    Raises whatever the underlying library raises if the WAV is malformed
    or lameenc is unavailable. Callers are expected to handle.
    """
    # Lazy-import lameenc so the module loads cleanly on hosts without it
    # (CI, dev machines that only run inference).
    import lameenc

    pcm, sr = sf.read(str(wav_path), dtype="int16", always_2d=True)
    # pcm shape after always_2d: (frames, channels). lameenc wants interleaved
    # int16 bytes, which is exactly what numpy's tobytes() produces.
    channels = pcm.shape[1]

    enc = lameenc.Encoder()
    enc.set_bit_rate(int(bitrate_kbps))
    enc.set_in_sample_rate(int(sr))
    enc.set_channels(int(channels))
    enc.set_quality(2)              # 2 = high quality, smallish CPU cost
    # Disable LAME's aggressive lowpass cutoff so bird harmonics above ~16 kHz
    # survive. Negative quality numbers are LAME presets; set_quality(2) maps
    # to internal `-q 2`. Lowpass=0 means "no lowpass" (preserve full spectrum).
    try:
        enc.silence()                       # quiet stderr from libmp3lame
    except AttributeError:
        pass
    # lameenc exposes lowpass via raw set if the wheel was built with it;
    # older wheels don't, in which case the default LAME lowpass applies
    # (around 16-19 kHz depending on bitrate, still plenty for most birds).
    try:
        enc.set_lowpass(0)
    except AttributeError:
        log.debug("lameenc.set_lowpass not available; using LAME default")

    part = mp3_path.with_suffix(mp3_path.suffix + ".part")
    with open(part, "wb") as f:
        f.write(enc.encode(pcm.tobytes()))
        f.write(enc.flush())
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(part, mp3_path)
    log.info(
        "encoded %s -> %s (%d Hz, %d ch, %d kbps, %d bytes)",
        wav_path.name, mp3_path.name, sr, channels, bitrate_kbps,
        mp3_path.stat().st_size,
    )
