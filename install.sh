#!/usr/bin/env bash
# =============================================================================
#  install.sh  —  one-shot Sparrow Mini bootstrap for a fresh Raspberry Pi
#
#  Brings a freshly-flashed Pi (Pi OS Lite, primary user "sparrow") to a
#  running robin.service in one command. Idempotent — re-runs are safe.
#
#  Usage (run as root, from inside the cloned repo at /home/sparrow/robin):
#    sudo ./install.sh --pass <wifi-psk> [other flags]
#
#  See --help for the full flag list.
# =============================================================================
set -euo pipefail

PROJECT_DIR="/home/sparrow/robin"
SPARROW_USER="sparrow"
REPO_SLUG="microsoft/SPARROWmini"   # owner/repo to fetch if not already cloned (public)

# Required + defaults
WIFI_PASS=""
SSID="SparrowMini"
MODE="relay"
SERVER_URL=""
API_KEY_RAW=""
EMAIL=""
SKIP_XBEE=false
SKIP_REBOOT=false
XBEE_FAMILY="${XBEE_FAMILY:-}"
# FTP push-receiver credentials (for IP cameras). Blank = keep whatever the
# shipped config.json already has (camera/password); pass a flag to override.
FTP_USER=""
FTP_PASS=""

# Internal state
NEEDS_REBOOT=false

usage() {
  cat <<'EOF'
Usage: sudo ./install.sh --pass <wifi-psk> [options]

Required:
  --pass <psk>                WPA2 password for the Pi's Wi-Fi hotspot (8-63 chars)

Optional:
  --ssid <name>               Hotspot SSID. Default: SparrowMini
  --mode relay|direct         Starting transport mode. Default: relay
  --server-url <url>          Cloud endpoint base URL (required if --mode direct)
  --api-key <value|@file>     API key value, or @/path/to/file (required if --mode direct)
  --email <addr>              Onboarding email                (required if --mode direct)
  --ftp-user <name>           FTP username for IP-camera push uploads. Default:
                              camera (leave unset to keep the shipped default).
  --ftp-pass <pass>           FTP password for IP-camera push uploads. Default:
                              password (leave unset to keep the shipped default).
  --no-xbee                   Skip the XBee radio configuration step
  --xbee-family 868|900       XBee module family. 868 = XBee SX 868, 900 = XBee-PRO
                              900 / 900HP (skips BR, which errors on 900-family).
                              If omitted and stdin is a TTY, you'll be prompted;
                              else defaults to 868 (with a warning). Also readable
                              from the XBEE_FAMILY environment variable.
  --no-reboot                 Don't auto-reboot even if I2C was just enabled
  -h, --help                  Print this help

Exit codes:
  0  ok
  2  argument error
  3  not running as root
  4  sparrow user is missing
  5  onboarding failed

Examples:
  Relay-mode install (most common):
    sudo ./install.sh --pass MyHotspotPass

  Direct-mode install with onboarding (key from a file):
    sudo ./install.sh \
        --pass MyHotspotPass \
        --mode direct \
        --server-url https://server.sparrow-earth.com \
        --api-key @/tmp/sparrow-api-key.txt \
        --email you@example.com
EOF
}

log()   { echo "[install] $*"; }
warn()  { echo "[install][WARN] $*" >&2; }
err()   { echo "[install][ERROR] $*" >&2; }

# -----------------------------------------------------------------------------
# Arg parsing
# -----------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pass)        WIFI_PASS="${2:-}";  shift 2 ;;
    --ssid)        SSID="${2:-}";       shift 2 ;;
    --mode)        MODE="${2:-}";       shift 2 ;;
    --server-url)  SERVER_URL="${2:-}"; shift 2 ;;
    --api-key)     API_KEY_RAW="${2:-}";shift 2 ;;
    --email)       EMAIL="${2:-}";      shift 2 ;;
    --no-xbee)     SKIP_XBEE=true;      shift ;;
    --xbee-family) XBEE_FAMILY="${2:-}";shift 2 ;;
    --ftp-user)    FTP_USER="${2:-}";   shift 2 ;;
    --ftp-pass)    FTP_PASS="${2:-}";   shift 2 ;;
    --no-reboot)   SKIP_REBOOT=true;    shift ;;
    -h|--help)     usage; exit 0 ;;
    *)             err "Unknown argument: $1"; usage; exit 2 ;;
  esac
done

# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
  err "Must be run as root (sudo). Got EUID=$EUID."
  exit 3
fi

if ! getent passwd "$SPARROW_USER" >/dev/null; then
  err "The '$SPARROW_USER' user does not exist. Create it during Pi OS flashing (set primary user = $SPARROW_USER) or with 'useradd'."
  exit 4
fi

# Fetch the repo into $PROJECT_DIR if it's not already there. The repo is public,
# so this is a plain unauthenticated clone. If install.sh is being run from inside
# an existing clone this is a no-op.
if [[ ! -e "$PROJECT_DIR/install.sh" ]]; then
  log "Repo not found at $PROJECT_DIR — fetching from GitHub ($REPO_SLUG)."
  command -v git >/dev/null 2>&1 || { apt-get update -y && apt-get install -y git; }

  if ! git clone "https://github.com/${REPO_SLUG}.git" "$PROJECT_DIR"; then
    err "Clone failed. Check network access to github.com/$REPO_SLUG."
    exit 2
  fi
  chown -R "$SPARROW_USER:$SPARROW_USER" "$PROJECT_DIR"
  # No re-exec needed: every step below uses $PROJECT_DIR-absolute paths, so the
  # rest of the install runs against the freshly-cloned files regardless of where
  # this script was launched from.
fi

if [[ -z "$WIFI_PASS" ]]; then
  err "--pass is required (WPA2 hotspot password)."
  usage
  exit 2
fi

if (( ${#WIFI_PASS} < 8 || ${#WIFI_PASS} > 63 )); then
  err "--pass must be 8-63 characters (WPA2-PSK requirement). Got ${#WIFI_PASS}."
  exit 2
fi

if [[ "$MODE" != "relay" && "$MODE" != "direct" ]]; then
  err "--mode must be 'relay' or 'direct'. Got '$MODE'."
  exit 2
fi

if [[ "$MODE" == "direct" ]]; then
  for required in SERVER_URL API_KEY_RAW EMAIL; do
    if [[ -z "${!required}" ]]; then
      err "--mode direct requires --server-url, --api-key, and --email. Missing: --${required,,}."
      exit 2
    fi
  done
fi

log "Sparrow Mini install starting"
log "  project dir   : $PROJECT_DIR"
log "  user          : $SPARROW_USER"
log "  mode          : $MODE"
log "  ssid          : $SSID"
[[ "$MODE" == "direct" ]] && log "  server url    : $SERVER_URL"
[[ "$MODE" == "direct" ]] && log "  onboarding to : $EMAIL"

cd "$PROJECT_DIR"

# -----------------------------------------------------------------------------
# Step: apt — install OS dependencies
# -----------------------------------------------------------------------------
step_apt() {
  log "Step 1/10: installing OS packages"
  apt-get update -y
  apt-get install -y \
    python3-venv \
    python3-pip \
    git \
    curl \
    i2c-tools \
    network-manager \
    libsndfile1 \
    alsa-utils \
    usbutils \
    uhubctl
  # alsa-utils  -> arecord, for the audio recorder (audio.enabled defaults true)
  # usbutils    -> lsusb, used by sd_service.py in SDWire capture mode
  # Optional JPEG tools — don't fail the whole install if these vary by distro.
  apt-get install -y jpegoptim libjpeg-turbo-progs || warn "jpegoptim / libjpeg-turbo-progs install failed (optional)."
}

# -----------------------------------------------------------------------------
# Boot config path — /boot/firmware/config.txt on Bookworm/Trixie, else legacy
# /boot/config.txt. Echoes the path, or nothing if neither exists.
# -----------------------------------------------------------------------------
detect_boot_cfg() {
  if   [[ -f /boot/firmware/config.txt ]]; then echo "/boot/firmware/config.txt"
  elif [[ -f /boot/config.txt           ]]; then echo "/boot/config.txt"
  fi
}

# Kernel cmdline path — /boot/firmware/cmdline.txt on Bookworm/Trixie, else
# legacy /boot/cmdline.txt. Echoes the path, or nothing if neither exists.
detect_cmdline() {
  if   [[ -f /boot/firmware/cmdline.txt ]]; then echo "/boot/firmware/cmdline.txt"
  elif [[ -f /boot/cmdline.txt           ]]; then echo "/boot/cmdline.txt"
  fi
}

# -----------------------------------------------------------------------------
# Step: i2c — enable in boot config (idempotent, sets NEEDS_REBOOT if changed)
# -----------------------------------------------------------------------------
step_i2c() {
  log "Step 2/10: enabling I2C"
  local boot_cfg; boot_cfg="$(detect_boot_cfg)"
  if [[ -z "$boot_cfg" ]]; then
    warn "Could not find a Pi boot config.txt — skipping I2C enable. BME688 sensor may not work until i2c-dev is loaded."
    return 0
  fi

  if grep -qE '^\s*dtparam=i2c_arm=on\s*$' "$boot_cfg"; then
    log "  I2C already enabled in $boot_cfg"
  else
    echo "dtparam=i2c_arm=on" >> "$boot_cfg"
    log "  Appended 'dtparam=i2c_arm=on' to $boot_cfg"
    NEEDS_REBOOT=true
  fi

  mkdir -p /etc/modules-load.d
  if [[ ! -f /etc/modules-load.d/i2c-dev.conf ]] || ! grep -qE '^\s*i2c-dev\s*$' /etc/modules-load.d/i2c-dev.conf; then
    echo "i2c-dev" >> /etc/modules-load.d/i2c-dev.conf
    log "  Ensured i2c-dev loads at boot"
  fi
  # Best-effort load now so non-reboot scenarios get I2C immediately.
  modprobe i2c-dev || true
}

# -----------------------------------------------------------------------------
# Step: power — disable unused radios/peripherals to save field power.
# Bluetooth (config.txt overlay + service) and HDMI (kernel cmdline connector
# disable). Both take effect on the next reboot. WiFi is left ON — it serves
# the status-page hotspot. Idempotent.
# -----------------------------------------------------------------------------
step_power() {
  log "Step 3/10: power tweaks — disable Bluetooth + HDMI"

  # --- Bluetooth: kill the modem in firmware, then stop the service ---
  # Sparrow Mini's XBee is on /dev/ttyUSB0 (USB), not the GPIO UART, so disable-bt is
  # safe — it leaves WiFi and the serial console untouched.
  local boot_cfg; boot_cfg="$(detect_boot_cfg)"
  if [[ -n "$boot_cfg" ]]; then
    if grep -qE '^[[:space:]]*dtoverlay=miniuart-bt[[:space:]]*$' "$boot_cfg"; then
      sed -i 's/^[[:space:]]*dtoverlay=miniuart-bt[[:space:]]*$/#&  # disabled by sparrow mini install (save power)/' "$boot_cfg"
      log "  commented dtoverlay=miniuart-bt in $boot_cfg"
      NEEDS_REBOOT=true
    fi
    if ! grep -qE '^[[:space:]]*dtoverlay=disable-bt[[:space:]]*$' "$boot_cfg"; then
      echo "dtoverlay=disable-bt" >> "$boot_cfg"
      log "  added dtoverlay=disable-bt to $boot_cfg"
      NEEDS_REBOOT=true
    fi
  else
    warn "no boot config.txt found — skipping Bluetooth overlay disable"
  fi
  # hciuart doesn't exist on Trixie; guard both so neither aborts the install.
  systemctl disable --now bluetooth 2>/dev/null || true
  systemctl disable --now hciuart  2>/dev/null || true

  # --- HDMI: disable the connector(s) at the KMS/kernel level via cmdline ---
  # vcgencmd display_power is a no-op under the full-KMS (vc4-kms-v3d) driver —
  # the kernel DRM owns the display, so we disable each HDMI connector with a
  # `video=<conn>:d` cmdline param instead. Headless field node needs no output.
  local cmdline; cmdline="$(detect_cmdline)"
  if [[ -n "$cmdline" ]]; then
    local found_conn=false
    for conn in /sys/class/drm/card*-HDMI-A-*; do
      [[ -e "$conn" ]] || continue
      found_conn=true
      local name; name="$(basename "$conn")"; name="${name#*-}"   # card0-HDMI-A-1 -> HDMI-A-1
      if ! grep -qF "video=${name}:d" "$cmdline"; then
        sed -i "s|\$| video=${name}:d|" "$cmdline"                # append to the single line
        log "  disabled HDMI connector ${name} via $cmdline"
        NEEDS_REBOOT=true
      else
        log "  HDMI connector ${name} already disabled in $cmdline"
      fi
    done
    $found_conn || warn "no HDMI connector found under /sys/class/drm — skipping HDMI disable"
  else
    warn "no kernel cmdline.txt found — skipping HDMI disable"
  fi
}

# -----------------------------------------------------------------------------
# Step: venv — create the .venv and install Python deps (runs as sparrow)
# -----------------------------------------------------------------------------
# Inlined from the former setup_venv.sh. OS-level deps (libsndfile1, jpeg tools)
# are handled by step_apt above, so this step is pure venv + pip + smoke-test.
step_venv() {
  log "Step 4/10: bootstrapping Python venv + deps"
  chown -R "$SPARROW_USER:$SPARROW_USER" "$PROJECT_DIR"
  sudo -u "$SPARROW_USER" PROJECT_DIR="$PROJECT_DIR" bash <<'VENV'
set -euo pipefail
PROJECT_DIR="${PROJECT_DIR:-$HOME/robin}"   # fallback if sudo didn't forward the env
VENV_DIR="$PROJECT_DIR/.venv"
cd "$PROJECT_DIR"

# 1) Ensure venv exists
if [[ ! -d "$VENV_DIR" ]]; then
  echo "[install]   creating venv at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
else
  echo "[install]   venv already exists: $VENV_DIR"
fi

# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"
echo "[install]   python: $(which python) ($(python -V 2>&1))"

# 2) Upgrade build tooling
python -m pip install --upgrade pip setuptools wheel

# 3) Python deps (keep in sync with imports across the services):
#    RPi.GPIO (GPIO) Pillow (images) pyserial (XBee) aiosmtpd/pyftpdlib
#    (push servers) smbus2 (BME688) numpy/soundfile/onnxruntime/librosa/lameenc
#    (audio+inference) requests (direct upload) psutil (metrics)
echo "[install]   installing Python packages"
python -m pip install --upgrade \
  RPi.GPIO \
  Pillow \
  pyserial \
  aiosmtpd \
  pyftpdlib \
  smbus2 \
  numpy \
  soundfile \
  onnxruntime \
  requests \
  librosa==0.10.2.post1 \
  lameenc \
  psutil

# 4) Smoke-test imports — fail the install loudly if a dep didn't land
python - <<'PY'
mods = ["RPi.GPIO","PIL","serial","aiosmtpd","pyftpdlib","smbus2","numpy",
        "soundfile","onnxruntime","requests","librosa","lameenc","psutil"]
failed = []
for m in mods:
    try:
        __import__(m); print(f"[install]   OK  {m}")
    except Exception as e:
        failed.append((m, str(e))); print(f"[install]   FAIL {m}: {e}")
if failed:
    raise SystemExit("import check failed: " + ", ".join(m for m, _ in failed))
print("[install]   all imports OK")
PY
VENV
}

# -----------------------------------------------------------------------------
# Step: device_id — first-run UUID (idempotent via ensure_device_id)
# -----------------------------------------------------------------------------
step_device_id() {
  log "Step 5/10: ensuring device_id.txt"
  sudo -u "$SPARROW_USER" "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/lib/device_id_util.py" >/dev/null \
    || warn "lib/device_id_util.py returned non-zero"
  local dev_id=""
  dev_id="$(cat "$PROJECT_DIR/device_id.txt" 2>/dev/null || true)"   # never abort the install here
  if [[ -n "$dev_id" ]]; then
    log "  device_id = $dev_id"
  else
    warn "device_id.txt not found at $PROJECT_DIR — the launcher will create it on first start"
  fi
}

# -----------------------------------------------------------------------------
# Step: ftp — write IP-camera FTP credentials into config.json (optional)
# -----------------------------------------------------------------------------
# Only runs when --ftp-user / --ftp-pass are given. Otherwise the shipped default
# (camera/password) is left as-is, so a plain install needs no extra steps.
step_ftp() {
  if [[ -z "$FTP_USER" && -z "$FTP_PASS" ]]; then
    log "Step: FTP creds — using shipped default (camera/password)"
    return 0
  fi
  log "Step: setting FTP push-receiver credentials in config.json"
  sudo -u "$SPARROW_USER" "$PROJECT_DIR/.venv/bin/python" - "$PROJECT_DIR" "$FTP_USER" "$FTP_PASS" <<'PY'
import json, os, sys
project_dir, user, passwd = sys.argv[1], sys.argv[2], sys.argv[3]
p = os.path.join(project_dir, "config", "config.json")
try:
    c = json.load(open(p))
    ftp = c.setdefault("ftp", {})
    if user:
        ftp["user"] = user
    if passwd:
        ftp["password"] = passwd
    tmp = p + ".tmp"
    json.dump(c, open(tmp, "w"), indent=2); open(tmp, "a").write("\n")
    os.replace(tmp, p)
    print(f"[install]   FTP set: user={ftp.get('user')} password={'*' * len(str(ftp.get('password') or ''))}")
except Exception as e:
    print(f"[install][WARN] could not set FTP creds: {e}")
PY
}

# -----------------------------------------------------------------------------
# Step: ap — Wi-Fi hotspot setup via NetworkManager (inlined from robin_setup.sh)
# -----------------------------------------------------------------------------
# Creates/updates a NetworkManager AP connection on wlan0: WPA2-PSK, ipv4 shared
# (NAT + DHCP), autoconnect, AP-isolation off, a stable DHCP range, and an
# upstream-DNS dnsmasq drop-in. SSID/password already validated up top.
step_ap() {
  log "Step 6/10: configuring Wi-Fi AP (SSID='$SSID')"
  local iface="wlan0"
  local con="$SSID"
  local band="bg"   # 2.4GHz

  if ! command -v nmcli >/dev/null 2>&1; then
    err "nmcli not found — NetworkManager should have been installed in step_apt."
    exit 2
  fi
  # Wait for wlan0. The Pi Zero 2 W's brcmfmac SDIO WiFi is sometimes slow to
  # bring up wlan0 after boot, or fails its firmware load outright ("Downloaded
  # RAM image is corrupted"). If we check once and give up, the AP is silently
  # skipped and the node ships with no hotspot. So: poll for the interface, and
  # reload the driver once mid-wait to recover from a failed firmware load.
  local waited=0 reloaded=false
  until nmcli -t -f DEVICE dev status | grep -qx "$iface"; do
    if (( waited >= 15 )) && ! $reloaded; then
      warn "  '$iface' not up after ${waited}s — reloading brcmfmac (WiFi firmware may have failed at boot)"
      modprobe -r brcmfmac >/dev/null 2>&1 || true
      sleep 1
      modprobe brcmfmac >/dev/null 2>&1 || true
      reloaded=true
    fi
    if (( waited >= 45 )); then
      warn "interface '$iface' not found after ${waited}s — skipping AP setup. Bring it up later with 'nmcli' or re-run install.sh once wlan0 is present."
      return 0
    fi
    log "  waiting for '$iface' to come up... (${waited}s)"
    sleep 5
    waited=$((waited + 5))
  done

  systemctl enable --now NetworkManager >/dev/null 2>&1 || true

  if nmcli -t -f NAME connection show | grep -qx "$con"; then
    log "  updating existing connection '$con'"
    nmcli connection modify "$con" 802-11-wireless.ssid "$SSID"
    nmcli connection modify "$con" 802-11-wireless.mode ap
    nmcli connection modify "$con" 802-11-wireless.band "$band"
    nmcli connection modify "$con" ipv4.method shared
    nmcli connection modify "$con" ipv6.method ignore
    nmcli connection modify "$con" wifi-sec.key-mgmt wpa-psk
    nmcli connection modify "$con" wifi-sec.psk "$WIFI_PASS"
    nmcli connection modify "$con" connection.autoconnect yes
    nmcli connection modify "$con" connection.autoconnect-priority 100
    nmcli connection modify "$con" connection.interface-name "$iface"
  else
    log "  creating new connection '$con'"
    nmcli connection add type wifi ifname "$iface" con-name "$con" autoconnect yes ssid "$SSID" >/dev/null
    nmcli connection modify "$con" 802-11-wireless.mode ap 802-11-wireless.band "$band" ipv4.method shared >/dev/null
    nmcli connection modify "$con" ipv6.method ignore >/dev/null
    nmcli connection modify "$con" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$WIFI_PASS" >/dev/null
  fi

  # Both branches: make the AP autoconnect and win the interface over any home
  # Wi-Fi client connection (e.g. one set in Raspberry Pi Imager). Without the
  # high priority, a client at the default priority 0 can take wlan0 on boot and
  # the status-page AP never comes up.
  nmcli connection modify "$con" connection.autoconnect yes connection.autoconnect-priority 100 >/dev/null 2>&1 || true

  # Pi fixes: disable AP isolation, pin a DHCP range, advertise upstream DNS.
  nmcli connection modify "$con" 802-11-wireless.ap-isolation 0 >/dev/null 2>&1 || true
  nmcli connection modify "$con" ipv4.shared-dhcp-range "10.42.0.10,10.42.0.200" >/dev/null 2>&1 || true
  mkdir -p /etc/NetworkManager/dnsmasq-shared.d
  cat >/etc/NetworkManager/dnsmasq-shared.d/10-upstream-dns.conf <<'DNS'
dhcp-option=6,8.8.8.8,1.1.1.1
DNS

  log "  restarting NetworkManager + bringing up '$con'"
  systemctl restart NetworkManager >/dev/null 2>&1 || true
  nmcli connection up "$con" >/dev/null 2>&1 || warn "could not bring up '$con' now (will autoconnect once wlan0 settles)."
}

# -----------------------------------------------------------------------------
# Step: xbee — configure the XBee radio (skipped if no /dev/ttyUSB0)
# -----------------------------------------------------------------------------
step_xbee() {
  log "Step 7/10: configuring XBee radio"
  if $SKIP_XBEE; then
    log "  --no-xbee given, skipping"
    return 0
  fi
  if [[ ! -e /dev/ttyUSB0 ]]; then
    warn "/dev/ttyUSB0 not present — XBee USB adapter not plugged in? Skipping. Re-run via the web UI's 'Configure XBee' button when ready."
    return 0
  fi

  # Resolve XBee family: --xbee-family flag > XBEE_FAMILY env > interactive prompt
  # (if TTY) > 868 default (with a warning if we had to guess).
  case "${XBEE_FAMILY:-}" in
    868|900)
      log "  XBee family: $XBEE_FAMILY"
      ;;
    "")
      if [[ -t 0 ]]; then
        echo "  Which XBee radio family is this node using?"
        echo "    1) XBee SX 868 (default)"
        echo "    2) XBee-PRO 900 / 900HP"
        read -r -p "  Choice [1]: " ans
        case "${ans:-1}" in
          2|900) XBEE_FAMILY=900 ;;
          *)     XBEE_FAMILY=868 ;;
        esac
        log "  XBee family: $XBEE_FAMILY"
      else
        warn "  --xbee-family not given and no TTY for interactive prompt; defaulting to 868. Re-run with --xbee-family 900 (or set XBEE_FAMILY=900) if this node uses an XBee-PRO 900."
        XBEE_FAMILY=868
      fi
      ;;
    *)
      warn "  --xbee-family='$XBEE_FAMILY' is invalid; defaulting to 868."
      XBEE_FAMILY=868
      ;;
  esac

  # Persist the family so the web UI's "Configure XBee" re-config uses it too
  # (it reads xbee.family from config and passes --family).
  sudo -u "$SPARROW_USER" "$PROJECT_DIR/.venv/bin/python" - "$XBEE_FAMILY" "$PROJECT_DIR" <<'PY'
import json, os, sys
fam, project_dir = sys.argv[1], sys.argv[2]
p = os.path.join(project_dir, "config", "config.json")
try:
    c = json.load(open(p))
    c.setdefault("xbee", {})["family"] = fam
    tmp = p + ".tmp"
    json.dump(c, open(tmp, "w"), indent=2); open(tmp, "a").write("\n")
    os.replace(tmp, p)
    print(f"[install]   persisted xbee.family={fam} to config.json")
except Exception as e:
    print(f"[install][WARN] could not persist xbee.family: {e}")
PY

  sudo -u "$SPARROW_USER" "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/transport/xbee_configure.py" \
      --port /dev/ttyUSB0 --baud 115200 --api 1 --rf 1 --router 0 --netid 1234 \
      --family "$XBEE_FAMILY"
}

# -----------------------------------------------------------------------------
# Step: systemd — install + enable the unit
# -----------------------------------------------------------------------------
step_systemd() {
  log "Step 8/10: installing systemd unit"
  install -m 644 -o root -g root "$PROJECT_DIR/systemd/robin.service" /etc/systemd/system/robin.service
  systemctl daemon-reload
  systemctl enable robin.service
}

# -----------------------------------------------------------------------------
# Step: onboarding — direct-mode only. Writes access_key + onboards with server.
# -----------------------------------------------------------------------------
step_onboarding() {
  log "Step 9/10: mode setup"
  if [[ "$MODE" != "direct" ]]; then
    log "  relay mode (default) — nothing to onboard. Operator can flip to direct via the web UI."
    return 0
  fi

  # Resolve the API key value (literal or @/path/to/file).
  local key=""
  if [[ "$API_KEY_RAW" == @* ]]; then
    local key_file="${API_KEY_RAW:1}"
    if [[ ! -f "$key_file" ]]; then
      err "API key file not found: $key_file"
      exit 5
    fi
    key="$(tr -d '\r\n' < "$key_file")"
  else
    key="$API_KEY_RAW"
  fi
  if [[ -z "$key" ]]; then
    err "API key is empty."
    exit 5
  fi

  # Write access_key.txt — root-owned because robin.service runs as root.
  install -m 600 -o root -g root /dev/null "$PROJECT_DIR/access_key.txt"
  printf '%s\n' "$key" > "$PROJECT_DIR/access_key.txt"
  log "  wrote $PROJECT_DIR/access_key.txt (mode 600)"

  # One Python script handles: config.json update + POST /v1/onboarding +
  # onboarded.json write. Values come in via env vars to avoid heredoc-quoting
  # bugs with whatever the server returns.
  local dev_id
  dev_id="$(cat "$PROJECT_DIR/device_id.txt")"
  log "  POST $SERVER_URL/v1/onboarding (unit_id=$dev_id)"

  ROBIN_DIR="$PROJECT_DIR" \
  ROBIN_SERVER_URL="$SERVER_URL" \
  ROBIN_API_KEY="$key" \
  ROBIN_EMAIL="$EMAIL" \
  ROBIN_UNIT_ID="$dev_id" \
  sudo -u "$SPARROW_USER" --preserve-env=ROBIN_DIR,ROBIN_SERVER_URL,ROBIN_API_KEY,ROBIN_EMAIL,ROBIN_UNIT_ID \
      "$PROJECT_DIR/.venv/bin/python" - <<'PY' || { err "Onboarding failed (see message above)"; exit 5; }
import json, os, sys, time
import urllib.request, urllib.error

project_dir = os.environ["ROBIN_DIR"]
base_url    = os.environ["ROBIN_SERVER_URL"].rstrip("/")
api_key     = os.environ["ROBIN_API_KEY"]
email       = os.environ["ROBIN_EMAIL"]
unit_id     = os.environ["ROBIN_UNIT_ID"]

# 1. Update config.json: mode=direct, server.base_url=<flag>. Atomic .tmp -> rename.
cfg_path = os.path.join(project_dir, "config", "config.json")
with open(cfg_path) as f:
    cfg = json.load(f)
cfg["mode"] = "direct"
cfg.setdefault("server", {})["base_url"] = base_url
tmp = cfg_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.replace(tmp, cfg_path)
print(f"[install]   config.json updated: mode=direct, server.base_url={base_url}")

# 2. POST /v1/onboarding (stdlib, no requests dep needed at install time).
body = json.dumps({"unit_id": unit_id}).encode("utf-8")
req = urllib.request.Request(
    base_url + "/v1/onboarding",
    data=body,
    method="POST",
    headers={
        "Content-Type": "application/json",
        "X-API-Key":    api_key,
        "X-Email":      email,
    },
)
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        status, raw = r.status, r.read().decode("utf-8", "replace")
except urllib.error.HTTPError as e:
    status, raw = e.code, (e.read() or b"").decode("utf-8", "replace")
except Exception as e:
    print(f"[install][ERROR] onboarding request failed: {e}", file=sys.stderr)
    sys.exit(1)

if status not in (200, 201):
    print(f"[install][ERROR] onboarding HTTP {status}: {raw}", file=sys.stderr)
    sys.exit(1)
print(f"[install]   onboarding ok ({status}): {raw}")

# 3. Write onboarded.json for the web UI's "is_onboarded" pill.
try:
    resp = json.loads(raw)
except Exception:
    resp = {}
out_path = os.path.join(project_dir, "onboarded.json")
out = {
    "project_id":   resp.get("project_id"),
    "device_id":    resp.get("device_id"),
    "unit_id":      resp.get("unit_id") or unit_id,
    "idempotent":   bool(resp.get("idempotent")),
    "base_url":     base_url,
    "onboarded_at": time.time(),
}
tmp = out_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(out, f)
os.replace(tmp, out_path)
print(f"[install]   wrote {out_path}")
PY
}

# -----------------------------------------------------------------------------
# Step: start — bring robin.service up
# -----------------------------------------------------------------------------
step_start() {
  log "Step 10/10: starting robin.service"
  systemctl restart robin.service
  sleep 4
  if systemctl is-active --quiet robin.service; then
    log "  robin.service: active"
  else
    err "robin.service failed to come up. Try: journalctl -u robin.service -n 80 --no-pager"
    exit 1
  fi
}

# -----------------------------------------------------------------------------
# Step: summary — print where to go from here
# -----------------------------------------------------------------------------
step_summary() {
  local lan_ip
  lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  cat <<EOF

==============================================================================
[install] Sparrow Mini install complete.

  Web UI         http://10.42.0.1:8080     (over the Pi's Wi-Fi AP '$SSID')
${lan_ip:+                  http://$lan_ip:8080         (over LAN)}
  Logs           sudo journalctl -u robin.service -f
  Service        sudo systemctl status robin.service

EOF
  if [[ "$MODE" == "relay" ]]; then
    cat <<EOF
  Mode is 'relay' (XBee). To switch to 'direct' (HTTPS upload):
    open the web UI → burger menu → Transport mode → 4G.
    Then 'Server & Onboarding' to paste an API key + run onboarding.

EOF
  else
    cat <<EOF
  Mode is 'direct'. Onboarding done. First image cycle runs within
  launcher.loop_sleep_sec (default 1800s) or 'sudo systemctl restart robin'.

EOF
  fi

  if $NEEDS_REBOOT; then
    if $SKIP_REBOOT; then
      cat <<EOF
  *** REBOOT RECOMMENDED ***  I2C was just enabled in /boot config; some
  sensors won't appear on /dev/i2c-1 reliably until you reboot. Run:
      sudo reboot
EOF
    else
      cat <<EOF
  *** REBOOTING in 8 seconds ***  I2C was just enabled in /boot config.
  Ctrl-C now if you want to defer (then 'sudo reboot' yourself).
EOF
      sleep 8
      systemctl reboot
    fi
  fi
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
step_apt
step_i2c
step_power
step_venv
step_device_id
step_ftp
step_ap
step_xbee
step_systemd
step_onboarding
step_start
step_summary
