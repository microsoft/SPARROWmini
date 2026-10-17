# Model card: `MD_AudioBirds_V1`

> Scaffolded 2026-05-23. `[FILL IN]` sections need upstream-specific content (training data, paper, architecture) before they appear on the public Zenodo bundle page. The script-author did not fabricate.

## Model kind

Binary audio detector (sliding-window sigmoid over 1-second mel-spectrogram windows). Single class: bird vs no-bird.

## Intended use

Wildlife / camera-trap audio screening — answer the question "does this clip contain a bird vocalization?" at 1-second resolution. Used in sparrow-engine as the default audio detector for benchmarks and Phase 4.x manual tests. Pairs with downstream species-classification (e.g. Perch 2 / `perch-v2`) for projects that need taxonomic identification beyond bird-vs-no-bird.

## Input shape + preprocessing

(Auto-extracted from `manifest.toml`.)

- Sample rate: 48000 Hz
- Window: 1.0 s sliding, 0.3 s stride
- Spectrogram: mel, n_fft=2048, hop_length=512, n_mels=224, fmin=0, fmax=24000, top_db=80
- Window function: Hann (symmetric)
- Mel scale: Slaney
- Filter norm: Slaney
- Input layout to the ONNX graph: `(batch, 1, 224, <time_frames>)` (NCHW with dynamic time axis)

## Output shape

Per-window sigmoid logit → confidence in `[0, 1]` for the single `bird` class.

- 1 class (`bird`, see `labels.txt`)
- Default `confidence_threshold = 0.9` (the model returns near-saturated ~1.0 confidence on continuous bird recordings; 0.9 keeps merged-range output meaningful)

## Upstream source

`[FILL IN]` — upstream source URL (PW Bioacoustics / Microsoft CameraTraps release).

## License

MIT. See `LICENSE.md` in this directory.

## Architecture

`[FILL IN]` — backbone / parameter count / paper reference.

## Training data

`[FILL IN]` — dataset name, approximate size, geographic / taxonomic scope, citation.

## Performance numbers

- **GPU latency** (sparrow-engine-gpu, RTX 6000 Ada, FP16): 8.52 ms p50 on DUNAS_20230925 (1.71× faster than FP32; Phase 3.8 Step 2 post-STRETCH re-audit, 2026-05-05).
- **Numerical parity**: FP16 ↔ FP32 within §2.2 R2 gates with substantial headroom (`docs/research/phase3.8/step2/fp16_audit.md`).
- **Published benchmark numbers**: `[FILL IN]` — published precision / recall on a public benchmark.

## Bundle precision

This bundle ships the FP32 ONNX as the integrity reference. The FP16 conversion path is in `sparrow-engine/tools/convert_fp16.py`; running it in-place produces a parity-verified FP16 ONNX that the engine will load when the manifest is flipped to `precision = "fp16"` + `file_fp16 = "<fp16_filename>"`.

## ONNX signature

| Tensor | Role | Dtype | Shape |
|---|---|---|---|
| `input` | mel spectrogram batch | float32 | `(batch, 1, 224, time_frames)` |
| `output` | bird sigmoid logit | float32 | `(batch, 1)` |

`[FILL IN]` — re-introspect with `onnx.checker.check_model` if upstream documentation contradicts these.

## Usage with sparrow-engine

```bash
spe detect-audio --model md-audiobirds-v1 --audio /path/to/clip.wav
```

See the bundle-level `README.md` for installation + general usage.

## Citation

`[FILL IN]` — BibTeX entry if a paper accompanies the model.

## Notes

- Slaney mel scale + Slaney filter norm match the training-time front-end (verified verbatim against PW Bioacoustics training code in `docs/design/phase3.8/step2/round_02/cameratraps_audit.md`). htk-scale / area-norm produces measurably different spectrograms and worse downstream metrics.
- The model's near-saturated output means downstream consumers usually pick a high threshold (the production default is 0.9, not 0.5).
- Default audio detector for sparrow-engine's `audio_e2e_parity` and `bench_audio_e2e` integration tests.
