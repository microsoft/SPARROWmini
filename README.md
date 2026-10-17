![image](https://zenodo.org/badge/DOI/10.5281/zenodo.23408340.svg)
 
<div align="center">
<font size="6"> Accelerating Research and Conservation with Edge AI.</font>
<br>
<hr>
<!-- Badges Section -->
<p align="center">
  <!-- License -->
  https://img.shields.io/badge/license-MIT-blue
  <!-- Contributions Welcome -->
  https://img.shields.io/badge/contributions-welcome-brsvg
<br><br>
</p>
</div>

# 👋 Welcome to SPARROWmini

**SPARROWmini** (Solar-Powered Acoustic and Remote Recording Observation Watch), developed by Microsoft's [AI for Good Lab](https://www.microsoft.com/en-us/research/group/ai-for-good-research-lab/), is a compact, AI-powered edge computing platform designed to operate autonomously in some of the world's most remote environments. Solar-powered and built around an ultra-low-power hardware architecture, SPARROWmini integrates with camera traps, acoustic sensors, and other environmental monitoring devices to collect biodiversity data. Using advanced PyTorch-based wildlife AI models, detections are performed directly on the edge, significantly reducing the amount of data that must be transmitted.

To overcome the communication challenges of remote deployments, SPARROWmini incorporates an integrated Digi XBee 868 MHz radio (EU) or Digi XBee-PRO 900 (US), enabling devices to form a self-healing DigiMesh network. Individual SPARROWmini units can relay detections, images, sensor measurements, and status information between neighbouring nodes over distances of up to approximately 10 km in open terrain, allowing monitoring networks to be extended well beyond the coverage of cellular or satellite communications. Through multi-hop communication, data from multiple SPARROWmini devices is forwarded across the mesh to a central **SPARROW gateway**, which provides the backhaul connection via 4G cellular or low-Earth-orbit satellite communications. This architecture enables large-scale biodiversity monitoring across remote landscapes while maintaining low power consumption and reducing communication costs.

docs/deployment-topologies.png

To access the SPARROWmini repository, please visit [aka.ms/sparrowmini](https://aka.ms/sparrowmini).

# ✨ Key Features

1. **Autonomous operation** Solar-powered field deployment with capture power switched off between cycles.
2. **Sensing** Camera traps, acoustic recorders, USB microphones / AudioMoth, and WiFi/IP cameras that push images over FTP or email feed the same capture pipeline. An optional BME688 provides environmental measurements.
3. **On-device AI** MegaDetector V6 YOLOv10-c and MD Audio Birds V1 run locally through ONNXRuntime. The image and audio models run separately to fit the Raspberry Pi Zero 2 W's 512 MB RAM.
4. **Connectivity** Relay detections over a compatible XBee DigiMesh network to a SPARROW gateway, or upload directly to the SPARROW server over Wi-Fi / 4G.
5. **Field status** A local web page provides link status, detector counts, backlog information, configuration, and onboarding controls.

---

This repository is the **field-node software** that runs on each SPARROWmini, a Raspberry Pi Zero 2 W in a weatherproof box. It pulls images off a wildlife camera (or audio off an acoustic recorder / USB mic), runs AI detection **on the device** to remove the empty frames, and transmits only positive detections upstream, over the XBee mesh to a SPARROW gateway (`relay`), or straight to the SPARROW cloud over Wi-Fi / 4G (`direct`).

**Part of the [SPARROW](https://github.com/microsoft/SPARROW) wildlife-monitoring system.**

## 🦎 What it's for

Camera traps generate enormous amounts of mostly-empty footage, and the sites that need monitoring are usually the ones with no power and no internet. SPARROWmini is the edge-node software that makes such a deployment practical:

- **On-device triage.** MegaDetector (vision) and MD Audio Birds V1 (acoustics) run on the Pi itself. Blank frames and silent clips are dropped *before* anything is transmitted, so you spend radio airtime and cloud storage only on real detections.
- **Two deployment topologies, one codebase**, flip per node with no redeploy:
  - **`relay`**, for remote, off-grid sites. The node has no internet; it forwards captures over a DigiMesh XBee radio link to a SPARROW **hub** that has backhaul. Several nodes can feed one hub.
  - **`direct`**, for connected sites. The node skips the radio and POSTs straight to the SPARROW cloud over Wi-Fi / 4G.
- **Source-agnostic capture.** A USB wildlife camera, an SDWire SD-card swapper, an acoustic recorder on USB mass storage, a live USB microphone / AudioMoth, or a **WiFi/IP camera that pushes over FTP or email**, all feed the same pipeline.
- **Low power, low spec.** Tuned for the Pi Zero 2 W (quad A53, 512 MB): the two AI models never sit in RAM at once, capture power is switched off between cycles, and audio is MP3-encoded before it ever crosses the radio.

Each SPARROWmini also hosts a small **status web page** (over its own Wi-Fi hotspot) so a field tech can check link quality, backlog, and what's running from a phone, no SSH needed. Connect your phone to the SPARROWmini SSID and open `http://10.42.0.1:8080` in a browser.

---

# 🚀 Getting Started

## 🛠 1. Hardware Assembly

### 📋 Prerequisites

Before you begin, ensure you have all the necessary hardware listed in the SPARROWmini Bill of Materials (BOM) below:

To download a PDF version of this BOM list, please visit [aka.ms/sparrowminibom](https://aka.ms/sparrowminibom).

SPARROWmini Bill of Materials (BOM) is licensed under MIT.
To view a copy of this license, visit: [MIT License](https://mit-license.org/)

### 🔧 Hardware you'll need

**Core (every node):**

- **Raspberry Pi** running Pi OS Lite (Bookworm or Trixie), with the primary user named **`sparrow`** (set this in Raspberry Pi Imager). Developed and tuned for the **Pi Zero 2 W**; bigger Pis are fine.
- A **microSD card** for the Pi OS, and field power (USB / battery / solar, a power-scheduling HAT like Witty-Pi is optional and out of scope here). `install.sh` disables Bluetooth and the HDMI/display rail to trim idle draw (WiFi stays on for the status hotspot).
- At least one **capture source**:
  - a wildlife/trail **camera** that exposes its storage as USB mass storage, **or**
  - an **SDWire** SD-card switcher, **or**
  - an **acoustic recorder** on USB mass storage, **or**
  - a **USB microphone / AudioMoth** for live recording, **or**
  - a **WiFi/IP camera** that pushes stills over **FTP or email** (joins the node's Wi-Fi AP; no USB needed).

**For `relay` mode (off-grid):**

- A **Digi XBee** radio (DigiMesh) on the node's `/dev/ttyUSB0`, either an **XBee SX 868** or an **XBee-PRO 900 / 900HP** (tell the installer which with `--xbee-family`), plus a matching XBee on a **SPARROW hub** (NI `SPARROW_MASTER`) within radio range. The hub runs the `sparrow_service` container ([SPARROW](https://github.com/microsoft/SPARROW)) and provides the backhaul. Both ends of a link must be the same family/band.

**For `direct` mode (connected):**

- Internet (Wi-Fi or a 4G dongle) and a reachable **SPARROW server** + an onboarding **API key**.

**Optional add-ons:**

- A **per-port-power-switching USB hub** (PPPS, check with `sudo uhubctl`) so the node can cut VBUS to the camera between cycles; otherwise a GPIO-driven power switch works.
- A **BME688** environmental sensor on I²C for temperature / humidity / pressure (reported in the direct-mode metrics).

### Hardware Assembly Instructions

To download the SPARROWmini Hardware Assembly Guide, please visit [aka.ms/sparrowminiassembly](https://aka.ms/sparrowminiassembly).

SPARROWmini Assembly and Set-Up Guide is licensed under MIT.
To view a copy of this license, visit: [MIT License](https://mit-license.org/)

---

## ⚡ 2. One-click Raspberry Pi Setup (Recommended)

On a fresh Pi (Pi OS Lite Bookworm/Trixie, primary user `sparrow`, internet for setup):

```bash
git clone https://github.com/microsoft/SPARROWmini.git ~/robin
cd ~/robin
sudo ./install.sh --pass <wifi-psk>
```

> `install.sh` can also fetch the repo itself: drop just the script onto the Pi and run it, and if `~/robin` isn't already a clone it does a plain `git clone` of the public repo before continuing.

That's it. One script installs OS deps, sets up the Python venv, brings up the Pi's Wi-Fi hotspot, configures the XBee radio (if plugged in), installs the systemd unit, and starts `robin.service`. The node boots into **relay** mode (XBee). Flip to **direct** mode later from the web UI.

**XBee radio family.** SPARROWmini supports two radio families and the config differs slightly between them, so `install.sh` needs to know which you have:

- **XBee SX 868** (default), the RF data rate is set over the air (`AT BR`).
- **XBee-PRO 900 / 900HP**, the RF rate is fixed by the firmware image, so `AT BR` errors and must be skipped.

If you don't pass `--xbee-family`, the installer **prompts** you interactively (868 is the default). For a headless / scripted install, pass it explicitly:

```bash
sudo ./install.sh --pass <wifi-psk> --xbee-family 900   # or 868
```

The chosen family is saved to `config.json` (`xbee.family`), so the web UI's **Configure XBee** button and any later re-config use the right sequence. It's also shown on the status page's Config card. (`XBEE_FAMILY=900` as an env var works too.)

**IP-camera FTP credentials.** If you use a WiFi/IP camera that pushes over FTP, pass its login with `--ftp-user` / `--ftp-pass` and the installer writes them into `config.json`, no manual edit needed. Both are optional; omit them to keep the shipped default (`camera` / `password`). A typical field install is one line:

```bash
sudo ./install.sh --pass password01 --ssid SparrowMini --xbee-family 900 \
    --ftp-user camera --ftp-pass password
```

To onboard a direct-mode node in the same step:

```bash
sudo ./install.sh \
    --pass <wifi-psk> \
    --mode direct \
    --server-url https://server.sparrow-earth.com \
    --api-key @/tmp/api-key.txt \
    --email you@example.com
```

`install.sh --help` lists every flag. The script is idempotent, re-running it on an already-set-up Pi just re-applies any drifted state. It's the single setup script, OS packages, the Python venv, the Wi-Fi AP, the XBee radio, and the systemd unit are all handled in one file.

## 📱 3. Web UI

Each node hosts a stdlib HTTP status page at `http://10.42.0.1:8080` (SPARROWmini Wi-Fi AP) or `http://<lan-ip>:8080`. Polls `/api/status` every 5 seconds; no client-side framework, just vanilla JS.

**Main view**, live status cards:

- **XBee link** (relay mode), RSSI, signal bars, age of last hub reply
- **Cloud upload** (direct mode), endpoint, online state, last success, last metrics push
- **Detector**, image model, classes, last-cycle counts
- **Audio detector**, audio model, threshold, last-cycle bird-window counts
- **Push receivers**, FTP (`:21`) and SMTP (`:2525`) enabled state, for IP cameras that push
- **Backlog**, JPGs and MP3s waiting to be sent
- **Config**, curated read-only snapshot (incl. the XBee family); secrets never appear

**Burger drawer**, actions:

- **Configure XBee**, re-apply the documented radio config. Reads `xbee.family` from config, so it uses the correct sequence for an 868 vs 900 module.
- **Rediscover hub**, re-resolve `SPARROW_MASTER` over the air and re-pin `xbee.dest64`. Use this if the hub's XBee chip is replaced with a new one (the node would otherwise stay pinned to the old, dead address).
- **Blank frames**, toggle whether captures with no detections are sent, per detector (**Images** / **Audio**). Off drops blanks on the node to save airtime; picked up at the next cycle.
- **Audio recording**, choose the recording trigger: **Schedule** (record `duration_seconds` every `interval_minutes`) or **Threshold** (record `RECORD_DURATION` s when a sample peaks above `THRESHOLD`). Clip length is capped at **10 s** in both the UI and the recorder (XBee airtime). Writes `audio.*` via a cross-process-locked atomic update; `audio_service` picks it up on its next loop. Independent of the PIR (which only triggers images).
- **Transport mode**, flip between XBee (relay) and 4G (direct). Confirms via dialog. Takes effect at the next launcher cycle (within `launcher.loop_sleep_sec`)
- **Server & Onboarding**, set the cloud endpoint URL, paste the API key (written to `access_key.txt` mode 600), enter your email and run device onboarding

A **Settings guide** (linked from the page footer, served at `/help`) explains every setting and each drawer action in plain language, useful for a field tech on a phone.

## 🌐 4. Direct-mode setup

Three things needed to send images straight to the cloud:

1. **API key**, issued by your SPARROW server admin (consumes one onboarding key)
2. **Endpoint URL**, typically `https://server.sparrow-earth.com`
3. **Onboarding call**, happens once per device. In the drawer's "Server & Onboarding" section, paste the key + your email, click **Onboard device**. The server binds the key to this Pi's 12-char `device_id` as the `unit_id`.

After onboarding, the same key authenticates every subsequent POST. Header-based: `X-API-Key`, `X-Unit-ID`, and `Idempotency-Key=sha256(file_bytes)` so retries dedupe server-side. The launcher also POSTs one system-metrics reading per cycle (`cpu_temp_c`, psutil snapshot, BME688 if attached) when `server.send_metrics` is true.

---

# 🔄 What happens in one cycle

Every `launcher.loop_sleep_sec` (default 30 min):

```
camera / SD ─────────┐
IP cam (FTP / SMTP) ─┴─► dest_dir/*.jpg ──► image detector ──┐
                                                             ├──► forwarder ──► sleep
mic / recorder ────────► audio_dir/*.wav ──► audio detector ─┘   (mode-selected,
                                                                 round-robins
                                                                 image + audio)
```

1. **Capture**, `capture/usb_import_service.py` pulls files off a USB mass-storage device (a camera **or** an acoustic recorder), routing by type: images → `dest_dir`, audio → `audio_dir`. Alternatives: `capture/sd_service.py` for an SDWire-swapped SD card, and `capture/audio_service.py` to record WAVs live from a USB mic on a schedule or amplitude trigger. All writes are atomic (`.part → rename`). **Push-based IP cameras** (e.g. Reolink) can also drop JPGs straight into `dest_dir` via the built-in **FTP** (`capture/ftp_server.py`, port 21) and **SMTP** (`capture/email_server.py`, port 2525) receivers, the launcher runs these alongside capture (gated on `ftp.enabled` / `email.enabled`), so a node can poll a USB/SDWire camera **and** accept pushed images at the same time. Point the cam at the node's Wi-Fi AP and FTP/email to `10.42.0.1`.
2. **Detect (image)**, MegaDetector V6 YOLOv10-c (`models/MDV6-yolov10-c`, ~9 MB ONNX). Runs in ~1.5 s/image on a Pi Zero W2. Writes a `<jpg>.det.json` sidecar; drops blanks if `detector.keep_blanks=false`.
3. **Detect (audio)**, MD Audio Birds V1 (`models/md-audiobirds-v1`, ~81 MB ONNX), sliding 1 s window over the librosa mel spectrogram. Bird-hit windows become a `<mp3>.det.json` sidecar + an MP3 sibling (lameenc, ~50 KB/min); the WAV is deleted. Drops blanks if `audio_detector.keep_blanks=false`. **Runs as a separate `--once` subprocess after the image detector**, the two ONNX sessions never coexist in RAM on the Pi Zero W2 (512 MB).
4. **Forward**, mode-switchable, hot-reloadable from the web UI:

| Mode | Transport | Destination | What's sent |
|---|---|---|---|
| **`relay`** (default) | XBee DigiMesh radio | SPARROW hub (separate device, NI `SPARROW_MASTER`) | JPG + MP3 bytes, round-robined (`xbee.pool_quantum` per pool), hub re-classifies |
| **`direct`** | HTTPS POST | SPARROW server `/v1/uploads/image` + `/v1/uploads/audio` | JPG **plus** detection sidecar; MP3 **plus** `{label, confidence}` distilled from the audio sidecar |

Both modes share capture and detection. Only the last step differs.

---

# 📡 Recommended DigiMesh deployment spacing

Use the following as simple field-planning guidance. Actual performance depends on antenna height and gain, vegetation, terrain, obstructions, and local radio conditions.

| Environment | Recommended node spacing | Guidance |
|---|---|---|
| Open terrain | **1,500–2,000 m** | Keep antennas high, vertical, and as clear of obstructions whenever possible. |
| Woodland / dense vegetation / terrain variation | **1,000–1,200 m** | Use shorter hops and position nodes to maintain a strong, reliable link. |

Each SPARROWmini added to the network can extend the mesh **and** can also operate its own wildlife camera or sensor.

**IMPORTANT:** All nodes in the same mesh must use compatible radios in the same frequency family (all 868 MHz **or** all 900 MHz, never mixed). Both ends of every link must match.

---

# 🌐 Deployment modes: relay & direct

The capture and AI process is **identical** in both modes, only how detections leave the node changes. Flip per node from the status page (**menu → Transport mode**); it takes effect at the next cycle.

### Relay (DigiMesh) mode

Use **relay** when the SPARROWmini is deployed away from cellular or satellite connectivity:

1. The camera or recorder creates a capture.
2. The SPARROWmini runs AI locally.
3. Empty captures can be discarded to save power, radio airtime, and storage.
4. Useful detections are sent over the XBee radio.
5. If the gateway is not directly reachable, another SPARROWmini relays the data.
6. The data continues through the mesh until it reaches the SPARROW gateway.
7. The gateway forwards the data using cellular or low-Earth-orbit satellite backhaul.

**Key point:** a SPARROWmini can monitor its own camera *while also* acting as a relay for neighbouring SPARROWmini devices.

### Direct mode

Use **direct** when the SPARROWmini itself has Wi-Fi or 4G internet:

1. Open the SPARROWmini status page.
2. Open the menu and change **Transport mode** to **Direct**.
3. Open **Server & Onboarding**.
4. Enter the SPARROW server address, API key, and onboarding email.
5. Select **Onboard device**.
6. Confirm that **Cloud upload** shows a successful connection.

See [Direct-mode setup](#direct-mode-setup) below for the onboarding detail.

---

# 🎧 Audio pipeline

SPARROWmini can run as a bioacoustic node alongside (or instead of) the camera. The audio path mirrors the image path, record → detect on-device → forward, but with its own recorder, model, and codec. All three stages are gated on `audio.enabled` / `audio_detector.enabled`, so a camera-only node skips them entirely.

**1. Record, `capture/audio_service.py`.** Captures mono S16_LE WAV at `audio.SAMPLE_RATE` (48 kHz) via `arecord`. The mic (AudioMoth or any USB microphone) is auto-detected from `arecord -l` when `audio.device` is blank, and **re-probed live on every loop**, a mic hot-plugged after the service starts is picked up within seconds, no restart needed (and its absence never kills the service). Two recording modes (`audio.MODE`):

- **schedule** (default), record `SCHEDULE.duration_seconds` every `SCHEDULE.interval_minutes`.
- **threshold**, sample `MONITOR_DURATION` s, and only record a full `RECORD_DURATION` s clip when peak amplitude exceeds `THRESHOLD`.

Clip length is hard-capped at **10 s** (`MAX_RECORD_SEC`), clamped by the recorder regardless of config, since audio ships over XBee and a long clip is minutes of airtime.

Clips are written atomically (`.part → rename`) as `01_{YYYYMMDDHHMMSS}.wav`, the same `{seq}_{timestamp}` shape as JPGs, so the forwarder's round-robin scheduler treats both pools uniformly. The recorder is a long-running background subprocess (loop only, no `--once`); the launcher starts/stops it as `audio.enabled` flips.

**2. Detect, `detect/audio_detector_service.py`.** MD Audio Birds V1 (`models/md-audiobirds-v1`, ~81 MB ONNX), a binary "bird present" sigmoid, runs over the WAV backlog once per cycle, after the image detector. Uses `onnxruntime` + `librosa` + `soundfile`. It computes the full-clip mel spectrogram **once** (`n_fft=2048`, `hop=512`, `n_mels=224`, `fmin=0`, `fmax=24000`, `top_db=80`, Slaney mel scale + Slaney filter norm, the model card requires these; htk/area-norm measurably hurt accuracy), then slides a `window_sec` (1.0 s) window at `stride_sec` (0.3 s) stride. Every window above `audio_detector.conf_threshold` (default 0.90) becomes an entry in a `<mp3>.det.json` sidecar (`{audio_name, captured_at, duration_sec, sample_rate, detections:[{label, confidence, start_sec, end_sec, class_id}]}`). It runs as a separate `--once` subprocess so its ONNX session never coexists in RAM with the image model on the 512 MB Pi Zero W2.

**3. Encode, `lib/audio_codec.py`.** Once classified, the WAV is encoded to MP3 via `lameenc` (a pure-Python wheel, no system ffmpeg/lame) at CBR `audio.encode_bitrate_kbps` (default 48). LAME's lowpass is **disabled** so bird harmonics above ~16 kHz survive. The MP3 becomes canonical and the WAV is deleted, downstream only ever sees MP3s. In practice this is a ~16× shrink (a 20 s 48 kHz mono WAV ≈ 1.9 MB → ≈ 120 KB MP3), which is what makes shipping audio over the radio viable.

**4. Forward.** Same mode switch as images:

- **relay**, the MP3 ships over XBee exactly like a JPG. The sender round-robins the image and audio pools (`xbee.pool_quantum` sends per pool); the hub re-classifies. The sidecar is **not** transmitted.
- **direct**, the MP3 is POSTed to `server.audio_endpoint_path` (`/v1/uploads/audio`, separate from images) with distilled metadata `{audio_name, detected_at, label, confidence}` and the same `X-API-Key` / `X-Unit-ID` / `Idempotency-Key` headers.

If `audio_detector.keep_blanks=false` (default), WAVs with no above-threshold window are deleted on the node, no MP3, no sidecar, no airtime. Set it true to keep a `detections: []` MP3 + sidecar.

The detector writes `audio_detector_status.json` (model, labels, threshold, last-run counts), surfaced as an **Audio detector** card in the web UI. To exercise the path by hand:

```bash
python capture/audio_service.py            # record (loop only — Ctrl-C to stop)
python detect/audio_detector_service.py --once   # classify the WAV backlog → MP3 + sidecar
```

---

# ⚙ Configuration

Everything lives in `config/config.json`. Notable top-level keys:

| Key | Purpose |
|---|---|
| `mode` | `relay` (XBee to the SPARROW hub) or `direct` (HTTPS to SPARROW Studio) |
| `capture_manager.capture_mode` | `camera-usb` or `sdwire` |
| `dest_dir` | Where imported JPGs land |
| `audio_dir` | Where recorded WAVs land (and post-inference MP3s) |
| `detector.enabled / keep_blanks / conf_threshold` | On-device image detection knobs |
| `audio.enabled / MODE / SCHEDULE / encode_bitrate_kbps` | Audio recorder + MP3 encoder knobs |
| `audio_detector.enabled / keep_blanks / conf_threshold` | On-device audio detection knobs |
| `ftp.enabled / user / password / port` | Built-in FTP receiver for IP-camera push (`:21`). Set by `--ftp-user` / `--ftp-pass` at install; default `camera` / `password`. |
| `email.enabled` | Built-in SMTP receiver for camera email push (`:2525`) |
| `xbee.master_ni` | Hub XBee NI (default `SPARROW_MASTER`, wire-level constant flashed into the peer; don't change) |
| `xbee.dest64` | Auto-filled by first cycle once the hub is discovered |
| `xbee.pool_quantum` | Round-robin sends per pool before switching (default 2) |
| `server.base_url / unit_id / send_metrics` | Direct-mode endpoint + per-cycle metrics POST |
| `server.image_endpoint_path / audio_endpoint_path` | Direct-mode upload paths (`/v1/uploads/image`, `/v1/uploads/audio`) |
| `launcher.loop_sleep_sec` | How often a cycle runs (default 1800) |
| `launcher.min_free_mb` | Low-disk guard: below this free space, capture/uploads pause (backlog keeps draining). Default 200 |

Secrets (gitignored, never echoed):

- `access_key.txt`, API key (mode 600). Written by the onboarding UI or by hand.

---

# 📝 Logs & Operational Notes

Each service writes to `logs/`:

- `logs/<service>.log`, the launcher and uploader **size-rotate** this (`.1/.2/.3`, ~5 MB each) so it can't fill the disk on a long deployment; other services also write a dated `logs/<service>_YYYYMMDD.log`.
- `journalctl -u robin.service -f` is the authoritative live view under systemd (the XBee sender/monitor log to stdout, which journald captures).

**Quarantine.** A file that repeatedly fails to send/detect (corrupt image, undecodable WAV, oversized for the radio, or a permanent cloud reject) is moved to `dest_dir/.quarantine/` (with a `.why.txt` reason) after a few attempts, so one poison file can never wedge the backlog or fill the disk. Safe to inspect or delete.

---

# 🤝 Contributing

This project welcomes contributions and suggestions.  Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

---

# ⁉ SPARROWmini FAQs

<details>
<summary>What is SPARROWmini and what does it do?</summary>

SPARROWmini is a compact, solar-powered field-node platform for biodiversity monitoring, developed by Microsoft's AI for Good Lab. It collects images and audio, runs AI detection on a Raspberry Pi Zero 2 W, and forwards detections through an XBee DigiMesh network to a SPARROW gateway or directly to the SPARROW server over Wi-Fi / 4G.
</details>

<details>
<summary>How do the AI models work?</summary>

MegaDetector V6 YOLOv10-c analyzes images, and MD Audio Birds V1 detects bird presence in audio. Both run on the device using ONNXRuntime. The image detector runs before the audio detector so the two models do not occupy RAM at the same time. Blank images and audio without above-threshold detections can be discarded before transmission. The web UI lets you change whether blank captures are retained.
</details>

<details>
<summary>Does SPARROWmini need its own internet connection?</summary>

In **relay** mode, SPARROWmini forwards JPGs and MP3s over XBee DigiMesh to a SPARROW hub with cellular or satellite backhaul. Neighbouring SPARROWmini nodes can relay the data. In **direct** mode, the node uses Wi-Fi / 4G to upload images, MP3 audio, and detection metadata directly to the SPARROW server. Both modes share the same capture and detection pipeline.
</details>

<details>
<summary>Which radios can be used together?</summary>

The supported families are **XBee SX 868** and **XBee-PRO 900 / 900HP**. All nodes in the same mesh must use compatible radios in the same frequency family: all 868 MHz or all 900 MHz, never mixed. Recommended spacing is **1,500-2,000 m** in open terrain and **1,000-1,200 m** in woodland, dense vegetation, or varying terrain. Actual performance depends on antenna placement, vegetation, terrain, obstructions, and local radio conditions.
</details>

<details>
<summary>How can I check or configure a node in the field?</summary>

Connect to the node's Wi-Fi hotspot and open `http://10.42.0.1:8080`. The status page shows link quality, detector status, backlog, and configuration. Its menu provides radio configuration, hub rediscovery, blank-frame settings, audio recording settings, transport selection, and server onboarding. The **Settings guide** is available at `/help`.
</details>

<details>
<summary>How does SPARROWmini handle privacy and human-related data?</summary>

Device-side scrubbing is not documented in the supplied SPARROWmini README. On-device detection and blank-frame filtering should not be treated as confirmation of human-data scrubbing.

Additionally, the Microsoft Dashboard includes scrubbing software that runs over the data that is uploaded to remove or anonymize any human-related data. If, after scrubbing, Microsoft personnel find any human-related data, it will be deleted.
</details>

<details>
<summary>Can I remove the scrubbing software?</summary>

Microsoft **does not** recommend removal of scrubbing software on your device.  Data uploaded to the Microsoft Dashboard will be scrubbed of personally identifiable information regardless of SPARROWmini device scrubbing.  Dashboard scrubbing and privacy features cannot be altered or removed.
</details>

<details>
<summary>How can I contact support for issues or privacy concerns?</summary>

For dashboard-related issues, Microsoft trademark authorization, privacy concerns, or general feedback, use the **contact form** under the “About” menu in the SPARROWmini dashboard. For development or client-specific issues, reach out via the **GitHub page**.
</details>

<details>
<summary>How often are these FAQs updated?</summary>

These FAQs will be updated on an **annual basis**.
</details>

---

# 🗄️ Data Storage & User Rights

When you register into the **SPARROWmini dashboard**, we store your **email address** solely for the purpose of **providing and maintaining access to our Dashboard**. You have the right to **permanently delete your account**, your email, and any other associated data at any time. Upon your request, all data related to your account will be **irreversibly removed** from our systems.

---

# 📢 Publications & Publicity

Microsoft may only use your data for **model improvement with your explicit consent**. You can opt in at any time using the toggle switch in the dashboard (**default setting is OFF**). All data uploaded to the dashboard remains private and will **never be shared** unless you have provided prior consent. Uploaded data will only be analyzed to ensure compliance with the **Terms of Use, and to fix Dashboard bugs and security issues**.

If you publish any **academic papers**, **articles**, or **research** based on **Data from the SPARROWmini Dashboard**, please **cite our project** in your publication.

You may not issue any press releases, public statements, or other publicity materials referencing Microsoft without our prior written approval.
**Please use the contact form under the "About" section in the SPARROWmini Dashboard to send your request and we will get back to you right away (this is a very quick process).**

---

# 🏷️ Trademarks

This project may contain trademarks or logos for projects, products, or services.  **Use of any Microsoft trademarks or logos is not permitted without prior written consent from Microsoft. Please use the contact form under the "About" section in the dashboard to send your request**. If you have received authorization, **your use of Microsoft
trademarks or logos is subject to and must follow**
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.

---

# 📄 License

SPARROWmini software, documentation, and Bill of Materials are licensed under the **MIT License**.

To view a copy of this license, visit: [MIT License](https://mit-license.org/).