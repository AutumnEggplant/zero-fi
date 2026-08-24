#!/usr/bin/env bash
# Zero-Fi Image Builder
#
# Builds a pre-installed Zero-Fi image for Pi Zero W using QEMU user-mode
# emulation on x86_64. Output: compressed .img.xz, flashable to any 8 GB+ SD card.
# First boot only expands the music partition — no package downloads, no pip installs.
#
# Usage:
#   sudo bash build/build-image.sh [--release-image]
#
#   --release-image   Deletes any existing zerofi-*.img/.img.xz in the repo
#                      root first, never bakes in a local authorized_keys
#                      (even if pi/root/root/.ssh/authorized_keys exists),
#                      always compresses, and writes a .sha256 file next to
#                      the archive for release notes. Config (zerofi.json)
#                      is never baked at build time regardless — that only
#                      happens at flash time via flash-sd.sh — so a release
#                      image is always fully unconfigured out of the box.
#
# Requirements: curl, xz, parted, losetup, qemu-arm-static, binfmt_misc, python3
# Must be root for loop device + chroot.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORK_DIR="$(mktemp -d)"

RELEASE_IMAGE=0
for arg in "$@"; do
    case "$arg" in
        --release-image) RELEASE_IMAGE=1 ;;
        *) echo "ERROR: unknown argument: $arg" >&2; exit 1 ;;
    esac
done

# pre-declare so cleanup() doesn't hit "unbound variable" under set -u on an
# early failure or a cache-hit run that skips the DietPi root mount
ROOT_MOUNT="" BOOT_MOUNT="" MUSIC_MOUNT=""
DIETPI_MOUNT="" DIETPI_BOOT_MOUNT=""
LOOP_DEV="" DIETPI_LOOP=""

cleanup() {
    pkill -f "qemu-arm-static.*$ROOT_MOUNT" 2>/dev/null || true
    for _ in 1 2 3; do
        umount "$ROOT_MOUNT/proc" 2>/dev/null || true
        umount "$ROOT_MOUNT/sys" 2>/dev/null || true
        umount "$ROOT_MOUNT/dev" 2>/dev/null || true
        umount "$ROOT_MOUNT/run" 2>/dev/null || true
        umount "$ROOT_MOUNT/var/cache/apt/archives" 2>/dev/null || true
    done
    umount "$ROOT_MOUNT" 2>/dev/null || true
    umount "$BOOT_MOUNT" 2>/dev/null || true
    umount "$MUSIC_MOUNT" 2>/dev/null || true
    umount "$DIETPI_MOUNT" 2>/dev/null || true
    umount "$DIETPI_BOOT_MOUNT" 2>/dev/null || true
    losetup -d "$LOOP_DEV" 2>/dev/null || true
    losetup -d "$DIETPI_LOOP" 2>/dev/null || true
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

preflight_cleanup() {
    for pid in $(pgrep -f "qemu-arm-static.*/bin/bash" 2>/dev/null || true); do
        kill "$pid" 2>/dev/null || true
    done
    # detach loops with no backing file OR pointing at our persistent images
    # (those never disappear between runs, so a killed build's loop looks live)
    for loop in /dev/loop*; do
        [[ -b "$loop" ]] || continue
        BACKING_FILE=$(losetup -nO BACK-FILE "$loop" 2>/dev/null || true)
        if [[ -z "$BACKING_FILE" || ! -f "$BACKING_FILE" ]]; then
            losetup -d "$loop" 2>/dev/null || true
        elif [[ "$BACKING_FILE" == "$REPO_DIR"/zerofi-*.img || "$BACKING_FILE" == "$REPO_DIR"/.cache/DietPi_*.img ]]; then
            losetup -d "$loop" 2>/dev/null || true
        fi
    done
}
preflight_cleanup

VERSION="1.0.0"
OUTPUT_IMAGE="$REPO_DIR/zerofi-${VERSION}.img"
OUTPUT_ARCHIVE="${OUTPUT_IMAGE}.xz"

if [[ "$RELEASE_IMAGE" -eq 1 ]]; then
    echo "Release mode: removing any existing built images..."
    rm -f "$REPO_DIR"/zerofi-*.img "$REPO_DIR"/zerofi-*.img.xz "$REPO_DIR"/zerofi-*.img.xz.sha256
fi

CACHE_DIR="$REPO_DIR/.cache"
CACHE_ROOTFS="$CACHE_DIR/rootfs.tar"  # uncompressed — local cache, not for transfer
CACHE_MARKER="$CACHE_DIR/.build-hash"

# .debs survive across builds here (bind-mounted over /var/cache/apt/archives);
# helps cache-miss rebuilds when the install list is changing
APT_CACHE_DIR="$CACHE_DIR/apt-archives"

# Partition sizes (MB)
BOOT_SIZE=50
ROOT_SIZE=2048   # ~1 GB in use after firmware purge; 2 GB gives ~1 GB headroom
MUSIC_SIZE=128  # minimal — expanded on first boot
MUSIC_START=$((BOOT_SIZE + ROOT_SIZE + 2))  # +2 for alignment
IMAGE_SIZE=$((MUSIC_START + MUSIC_SIZE))

echo "=== Zero-Fi Image Builder v${VERSION} ==="
echo ""

# ── Sanity checks ──────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Must run as root (for losetup + chroot)"
    exit 1
fi

if ! command -v qemu-arm-static &>/dev/null; then
    echo "ERROR: qemu-arm-static not found. Install qemu-user-static."
    exit 1
fi

if ! ls /proc/sys/fs/binfmt_misc/qemu-arm* &>/dev/null 2>&1; then
    echo "ERROR: binfmt_misc not registered for ARM."
    echo "  Arch/Manjaro:  pacman -S qemu-user-static-binfmt && systemctl restart systemd-binfmt"
    echo "  Debian/Ubuntu: apt install qemu-user-static binfmt-support"
    echo "  Verify:        ls /proc/sys/fs/binfmt_misc/qemu-arm*"
    exit 1
fi

# ── Download DietPi ─────────────────────────────────────────────────────
DIETPI_URL="https://dietpi.com/downloads/images/DietPi_RPi1-ARMv6-Trixie.img.xz"
# cached in $CACHE_DIR (persists across runs) — not $WORK_DIR which is fresh every invocation
DIETPI_IMAGE="$CACHE_DIR/DietPi_RPi1-ARMv6-Trixie.img"

mkdir -p "$CACHE_DIR"

echo "[1/6] Downloading DietPi for Pi Zero W..."
if [[ -f "$DIETPI_IMAGE" ]]; then
    echo "  Using cached image: $DIETPI_IMAGE"
else
    curl -#Lo "$WORK_DIR/dietpi.img.xz" "$DIETPI_URL"
    echo "  Extracting..."
    xz -d "$WORK_DIR/dietpi.img.xz"
    mv "$WORK_DIR/dietpi.img" "$DIETPI_IMAGE"
fi

# ── Create sparse image ─────────────────────────────────────────────────
echo "[2/6] Creating ${IMAGE_SIZE}M sparse image..."
truncate -s "${IMAGE_SIZE}M" "$OUTPUT_IMAGE"

LOOP_DEV=$(losetup --find --show -P "$OUTPUT_IMAGE")
echo "  Loop device: $LOOP_DEV"

echo "[3/6] Partitioning..."
parted -s "$LOOP_DEV" mklabel msdos
parted -s "$LOOP_DEV" mkpart primary fat32 1M "${BOOT_SIZE}M"
parted -s "$LOOP_DEV" set 1 boot on
parted -s "$LOOP_DEV" mkpart primary linux-swap "${BOOT_SIZE}M" "$((BOOT_SIZE + ROOT_SIZE))M"
parted -s "$LOOP_DEV" mkpart primary fat32 "${MUSIC_START}M" "100%"
sync
sleep 1

if echo "$LOOP_DEV" | grep -q 'loop'; then
    P1="${LOOP_DEV}p1"
    P2="${LOOP_DEV}p2"
    P3="${LOOP_DEV}p3"
else
    P1="${LOOP_DEV}1"
    P2="${LOOP_DEV}2"
    P3="${LOOP_DEV}3"
fi

# ── Format partitions ──────────────────────────────────────────────────
echo "[4/6] Formatting partitions..."
mkfs.vfat -F 32 -n BOOT "$P1" >/dev/null 2>&1
mkfs.ext4 -F -L ROOT "$P2" >/dev/null 2>&1
mkfs.exfat -n MUSIC "$P3" >/dev/null 2>&1

# ── Mount and populate root ────────────────────────────────────────────
echo "[5/6] Populating root filesystem..."

ROOT_MOUNT="$WORK_DIR/root"
mkdir -p "$ROOT_MOUNT"
mount "$P2" "$ROOT_MOUNT"

BOOT_MOUNT="$WORK_DIR/boot"
mkdir -p "$BOOT_MOUNT"
mount "$P1" "$BOOT_MOUNT"

MUSIC_MOUNT="$WORK_DIR/music"
mkdir -p "$MUSIC_MOUNT"
mount "$P3" "$MUSIC_MOUNT"

# cache key covers both the base image AND this script — hashing only the image
# meant the cache had no awareness of the install list changing
DIETPI_HASH=$(cat <(sha256sum "$DIETPI_IMAGE" 2>/dev/null) <(sha256sum "$0" 2>/dev/null) | sha256sum | cut -d' ' -f1 || echo "none")
CACHE_HIT=0
if [[ -f "$CACHE_ROOTFS" && -f "$CACHE_MARKER" && "$(cat "$CACHE_MARKER" 2>/dev/null)" == "$DIETPI_HASH" ]]; then
    CACHE_HIT=1
fi

# DietPi image: p1=boot (FAT), p2=root (ext4)
DIETPI_LOOP=$(losetup --find --show -P "$DIETPI_IMAGE")
if echo "$DIETPI_LOOP" | grep -q 'loop'; then
    DIETPI_ROOT="${DIETPI_LOOP}p2"
    DIETPI_BOOT="${DIETPI_LOOP}p1"
else
    DIETPI_ROOT="${DIETPI_LOOP}2"
    DIETPI_BOOT="${DIETPI_LOOP}1"
fi

if [[ "$CACHE_HIT" -eq 1 ]]; then
    echo "  Restoring cached rootfs (hash match, skipping base copy)..."
    tar xf "$CACHE_ROOTFS" -C "$ROOT_MOUNT"
else
    echo "  Copying DietPi rootfs..."
    DIETPI_MOUNT="$WORK_DIR/dietpi-root"
    mkdir -p "$DIETPI_MOUNT"
    mount "$DIETPI_ROOT" "$DIETPI_MOUNT"

    rsync -aHAX "$DIETPI_MOUNT/" "$ROOT_MOUNT/" \
        --exclude=/boot \
        --exclude=/proc \
        --exclude=/sys \
        --exclude=/dev \
        --exclude=/tmp \
        --exclude=/run \
        --exclude=/mnt \
        --exclude=/media \
        --exclude=/lost+found

    umount "$DIETPI_MOUNT"
    rmdir "$DIETPI_MOUNT"
fi

# always copy boot files (small FAT partition, fast)
DIETPI_BOOT_MOUNT="$WORK_DIR/dietpi-boot"
mkdir -p "$DIETPI_BOOT_MOUNT"
mount "$DIETPI_BOOT" "$DIETPI_BOOT_MOUNT"
cp -r "$DIETPI_BOOT_MOUNT"/* "$BOOT_MOUNT/"
umount "$DIETPI_BOOT_MOUNT"
rmdir "$DIETPI_BOOT_MOUNT"
losetup -d "$DIETPI_LOOP"

# QEMU chroot needed regardless of cache hit (service enable/linking below)
cp "$(command -v qemu-arm-static)" "$ROOT_MOUNT/usr/bin/"
mkdir -p "$ROOT_MOUNT/proc" "$ROOT_MOUNT/sys" "$ROOT_MOUNT/dev" "$ROOT_MOUNT/run" "$ROOT_MOUNT/tmp"
chmod 1777 "$ROOT_MOUNT/tmp"
mount --bind /proc "$ROOT_MOUNT/proc"
mount --bind /sys "$ROOT_MOUNT/sys"
mount --bind /dev "$ROOT_MOUNT/dev"
mount --bind /run "$ROOT_MOUNT/run"

mkdir -p "$APT_CACHE_DIR" "$ROOT_MOUNT/var/cache/apt/archives"
mount --bind "$APT_CACHE_DIR" "$ROOT_MOUNT/var/cache/apt/archives"

# ── Chroot: install software (skipped on cache hit) ────────────────────
if [[ "$CACHE_HIT" -eq 0 ]]; then
    echo "  Installing software (QEMU chroot)..."
    chroot "$ROOT_MOUNT" /bin/bash << 'CHROOT'
set -euo pipefail

# chroot doesn't source /etc/profile — /usr/sbin (usermod, chpasswd) may not be in PATH
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

export DEBIAN_FRONTEND=noninteractive
export LANG=C.UTF-8

# skip fsync() on every file during unpack — meaningful speedup under QEMU syscall overhead
echo 'DPkg::Options { "--force-unsafe-io"; }' > /etc/apt/apt.conf.d/local-no-fsync

# myMPD has no official Debian package — maintainer publishes armv6-hardfloat prebuilds
# for this exact target via OBS; gnupg (not just gpgv) required for `gpg --dearmor`
apt-get install -y gnupg
curl -fsSL https://download.opensuse.org/repositories/home:/jcorporation/Raspbian_13/Release.key \
    | gpg --dearmor --output /usr/share/keyrings/jcorporation.gpg
chmod 644 /usr/share/keyrings/jcorporation.gpg
cat > /etc/apt/sources.list.d/jcorporation.list << 'EOF'
deb [signed-by=/usr/share/keyrings/jcorporation.gpg] https://download.opensuse.org/repositories/home:/jcorporation/Raspbian_13/ ./
EOF

echo "  [chroot] Updating package lists..."
apt-get update -qq

# upgrade before installing — prevents 404s when the index has moved past
# what the base image still thinks is current (libexpat1 hit this)
echo "  [chroot] Upgrading base image packages..."
apt-get upgrade -y -qq

install_packages() {
    apt-get install -y \
        mpd \
        mpc \
        mympd \
        hostapd \
        dnsmasq \
        pipewire \
        pipewire-bin \
        pipewire-alsa \
        wireplumber \
        libspa-0.2-bluetooth \
        bluez \
        wpasupplicant \
        dhcpcd \
        avahi-daemon \
        rsyslog \
        shairport-sync \
        curl \
        openssh-client \
        cifs-utils \
        python3-flask \
        python3-cryptography \
        iw \
        exfatprogs \
        python3-requests \
        python3-mutagen \
        ffmpeg
}

echo "  [chroot] Installing mpd, myMPD, hostapd, dnsmasq, PipeWire..."
for attempt in 1 2 3; do
    if install_packages; then
        break
    elif [[ $attempt -eq 3 ]]; then
        echo "  [chroot] apt-get install failed after 3 attempts"
        df -h / 2>/dev/null || true
        exit 1
    else
        echo "  [chroot] apt-get install failed (attempt $attempt/3) — repairing dpkg state and retrying..."
        # dpkg may be mid-transaction (e.g. ENOSPC during unpack) — must repair before retrying
        dpkg --configure -a || true
        apt-get update -qq
        sleep 5
    fi
done

# drop local-file rules — we only want rsyslog as a remote-forward relay
rm -f /etc/rsyslog.d/50-default.conf

# package-shipped rsyslog.service has no network ordering — it starts before wlan0
# has an address, causing "Temporary failure in name resolution" on every boot
mkdir -p /etc/systemd/system/rsyslog.service.d
cat > /etc/systemd/system/rsyslog.service.d/wait-for-network.conf << 'DROPIN'
[Unit]
After=network-online.target
Wants=network-online.target
DROPIN

# RAM-only journald, forwarded to rsyslog's local socket; constant log writes
# are a significant SD wear and torn-write risk on a device like this
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/zerofi.conf << 'JOURNALD'
[Journal]
Storage=volatile
RuntimeMaxUse=16M
ForwardToSyslog=yes
JOURNALD

# hardware watchdog — bcm2835-wdt is present but RuntimeWatchdogSec=0 by default
# (nothing pets it). 120s not 10-30s: single-core Pi under heavy sync genuinely
# starves things for long stretches; a spurious reboot costs more than a slower
# recovery from a real one.
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/zerofi-watchdog.conf << 'WATCHDOG'
[Manager]
RuntimeWatchdogSec=120
WATCHDOG

# DietPi base ships firmware for every common WiFi chipset; we only need the
# Broadcom blob for the Pi Zero W's BCM43430. Purge the rest (~380 MB combined).
apt-get purge -y firmware-iwlwifi firmware-atheros firmware-mediatek firmware-realtek 2>/dev/null || true

# NOT apt-get clean — /var/cache/apt/archives is the persistent APT_CACHE_DIR
# bind-mount from the build host; cleaning here erases the whole point of caching it
rm -rf /var/lib/apt/lists/*

# shairport-sync postinst gates its useradd on `[ -d /run/systemd/system ]` (systemd
# running as PID 1) — never true in a chroot, so the service user silently never
# gets created. Symptom: shairport-sync.service crash-loops with status=217/USER.
# Create the user manually, mirroring the postinst's own useradd call.
id -u shairport-sync &>/dev/null || useradd -rMU -G audio -d /nonexistent -s /usr/sbin/nologin shairport-sync

# dedicated pipewire system user — mpd and shairport-sync run as this user too
# so they can reach the PipeWire session with no cross-user socket permission handling
id -u pipewire &>/dev/null || useradd -rm -g pipewire -G audio,bluetooth -d /home/pipewire -s /usr/sbin/nologin pipewire

# start pipewire/wireplumber at boot with no interactive login ever happening —
# `loginctl enable-linger` needs a live systemd-logind; this is the exact on-disk
# marker that command creates, read identically by systemd at real boot
mkdir -p /var/lib/systemd/linger
touch /var/lib/systemd/linger/pipewire

# DietPi ships systemd-logind *masked* (/etc/systemd/system/systemd-logind.service
# -> /dev/null). With logind masked, loginctl/lingering/-M user@ all fail with a
# confusing "File exists" error rather than pointing at the real cause.
rm -f /etc/systemd/system/systemd-logind.service
CHROOT

    echo "  Saving rootfs to cache..."
    tar cf "$CACHE_ROOTFS" -C "$ROOT_MOUNT" \
        --exclude=./proc --exclude=./sys --exclude=./dev --exclude=./run --exclude=./tmp \
        --exclude=./var/cache/apt/archives \
        .
    echo "$DIETPI_HASH" > "$CACHE_MARKER"
fi

# ── Inject Zero-Fi files ──────────────────────────────────────────────
echo "  Injecting Zero-Fi files..."

cp "$REPO_DIR/pi/boot/dietpi.txt" "$BOOT_MOUNT/dietpi.txt"

echo "" >> "$BOOT_MOUNT/config.txt"
echo "# Zero-Fi: force HDMI output" >> "$BOOT_MOUNT/config.txt"
echo "hdmi_force_hotplug=1" >> "$BOOT_MOUNT/config.txt"

# ramoops reserves a DRAM region the kernel mirrors its console ring into;
# survives a hard reset because nothing re-initialises it. console-size=64K captures
# hung_task reports ("INFO: task kworker blocked for more than N seconds" + call
# trace) that name the wedged subsystem — without it, a watchdog reset leaves
# zero evidence. Entirely in RAM while running — only disk write is systemd-pstore
# copying the recovered buffer on next boot.
echo "" >> "$BOOT_MOUNT/config.txt"
echo "# Zero-Fi: preserve kernel console across hard resets (ramoops/pstore)" >> "$BOOT_MOUNT/config.txt"
echo "dtoverlay=ramoops,total-size=131072,console-size=65536,record-size=16384" >> "$BOOT_MOUNT/config.txt"

# match PARTUUID=, UUID=, LABEL=, or PARTLABEL= — a denylist covering only
# the one form already seen would miss others a future DietPi build might ship
if [[ -f "$BOOT_MOUNT/cmdline.txt" ]]; then
    sed -i -E 's#root=(PARTUUID|UUID|LABEL|PARTLABEL)=[^ ]*#root=/dev/mmcblk0p2#g' "$BOOT_MOUNT/cmdline.txt"
    echo "  Fixed cmdline.txt: root=/dev/mmcblk0p2"
else
    cat > "$BOOT_MOUNT/cmdline.txt" << 'CMDLINE'
root=/dev/mmcblk0p2 rootfstype=ext4 rootwait fsck.repair=yes net.ifnames=0 logo.nologo console=serial0,115200 console=tty1
CMDLINE
    echo "  Created cmdline.txt: root=/dev/mmcblk0p2"
fi

if [[ ! -f "$BOOT_MOUNT/config.txt" ]]; then
    cat > "$BOOT_MOUNT/config.txt" << 'CONFIG'
# Zero-Fi — Raspberry Pi Zero W
arm_64bit=0
gpu_mem=16
CONFIG
fi

# config is baked in the flashing step (flash-sd.sh copies build/zerofi.json to
# the music partition if present) — not here. Its absence is what puts app.py
# into setup mode (is_configured()).

cat > "$BOOT_MOUNT/DO-NOT-EDIT-THIS-PARTITION.txt" << 'README'
╔══════════════════════════════════════════════════════╗
║              Zero-Fi — Raspberry Pi Zero W          ║
╚══════════════════════════════════════════════════════╝

This is the BOOT partition of a Zero-Fi SD card. It holds
the Raspberry Pi's firmware and kernel — the files the Pi
reads before Linux even starts. That's also why it has to
be FAT32; it's a hardware requirement, not a Zero-Fi one.

⚠  DO NOT edit or remove ANYTHING on this partition.

📁 Zero-Fi's config lives on the MUSIC partition instead,
   as zerofi.json — that one's meant to be editable (e.g.
   to recover from a bad WiFi password by plugging the card
   into any computer).

🎵 Music files also go on the MUSIC partition.
README

mkdir -p "$ROOT_MOUNT/opt/zerofi"
cp -r "$REPO_DIR/flask_app" "$ROOT_MOUNT/opt/zerofi/"
cp -r "$REPO_DIR/templates" "$ROOT_MOUNT/opt/zerofi/"
cp -r "$REPO_DIR/static" "$ROOT_MOUNT/opt/zerofi/"
cp "$REPO_DIR/requirements.txt" "$ROOT_MOUNT/opt/zerofi/"
mkdir -p "$ROOT_MOUNT/opt/zerofi/docs"
cp "$REPO_DIR/docs/index.html" "$ROOT_MOUNT/opt/zerofi/docs/"
# Ship the whole pi/root/opt/zerofi/ tree rather than a hand-maintained
# per-file list — that list had already drifted (extract-covers.py was
# copied but missing from the matching chmod list below). __pycache__ is
# local build/dev-run noise, not something to ship. Anything with a shebang
# gets +x automatically instead of needing its own chmod line.
rsync -a --exclude='__pycache__' "$REPO_DIR/pi/root/opt/zerofi/" "$ROOT_MOUNT/opt/zerofi/"
find "$ROOT_MOUNT/opt/zerofi" -maxdepth 1 -type f -exec sh -c 'head -c2 "$1" | grep -q "^#!"' _ {} \; -print0 | \
    xargs -0 -r chmod +x

# /etc/bashrc.d/dietpi.bash sources /boot/dietpi/func/dietpi-globals which doesn't
# exist in this image — produces "[FAILED] DietPi-Login" on every SSH login
rm -f "$ROOT_MOUNT/etc/bashrc.d/dietpi.bash"
cp "$REPO_DIR/pi/root/etc/motd" "$ROOT_MOUNT/etc/motd"

if [[ "$RELEASE_IMAGE" -eq 1 ]]; then
    if [[ -f "$REPO_DIR/pi/root/root/.ssh/authorized_keys" ]]; then
        echo "  Release mode: NOT baking in local authorized_keys."
    fi
elif [[ -f "$REPO_DIR/pi/root/root/.ssh/authorized_keys" ]]; then
    mkdir -p "$ROOT_MOUNT/root/.ssh"
    cp "$REPO_DIR/pi/root/root/.ssh/authorized_keys" "$ROOT_MOUNT/root/.ssh/"
    chmod 700 "$ROOT_MOUNT/root/.ssh"
    chmod 600 "$ROOT_MOUNT/root/.ssh/authorized_keys"
fi

# set directly rather than relying on dietpi.txt's AUTO_SETUP_GLOBAL_PASSWORD alone;
# absolute path — outside the earlier heredoc's chroot session, back to host PATH
echo "root:zerofi" | chroot "$ROOT_MOUNT" /usr/sbin/chpasswd

mkdir -p "$ROOT_MOUNT/etc/systemd/system"

cat > "$ROOT_MOUNT/etc/systemd/system/zerofi-flask.service" << 'SERVICE'
[Unit]
Description=Zero-Fi Flask Management App
# mpd.service: boot-time apply_bt_mode() enables zerofi-bt-audio.service which
# waits on mpd — without this, Flask can reach that call before mpd is ready
# and the systemctl call can block until it times out.
# zerofi-ap.service: creates the wlan0_ap interface hostapd binds to (ExecStartPre);
# apply_ap_config() is called at boot and restarts hostapd.
After=network.target mpd.service zerofi-ap.service mnt-music.mount mympd.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/zerofi/flask_app/app.py
WorkingDirectory=/opt/zerofi
Environment=ZEROFI_CONFIG_DIR=/mnt/music
# system unit doesn't inherit XDG_RUNTIME_DIR — set explicitly so PipeWire
# calls (pw_dump, pw_set_default_sink) reach the pipewire user's session.
# UID 997 matches the useradd earlier in this script.
Environment=XDG_RUNTIME_DIR=/run/user/997
Restart=on-failure
RestartSec=5
User=root
# lowest CPU priority — settings-page requests are never latency-sensitive
CPUWeight=50

[Install]
WantedBy=multi-user.target
SERVICE

# WirePlumber config: two headless-specific overrides needed:
#
# 1. seat-monitoring = disabled: WirePlumber's Bluetooth monitor gates on
#    seat_state == "active" (interactive-login semantics). The pipewire user
#    never has a real login, so its seat stays at "online" forever — bluetoothd
#    never gets an A2DP endpoint registered, causing "pairs then disconnects."
#    Also restrict to SBC only: leaving the full codec set enabled
#    (LDAC/aptX/opus/faststream) caused "SEP in bad state for resume" and
#    connection stuttering; SBC alone is stable on BCM43430A1.
#
# 2. bluez5.roles: expose both sink (Target mode) and source (Source mode)
mkdir -p "$ROOT_MOUNT/etc/wireplumber/wireplumber.conf.d"
cat > "$ROOT_MOUNT/etc/wireplumber/wireplumber.conf.d/51-zerofi-headless.conf" << 'WPCONF'
wireplumber.profiles = {
  main = {
    monitor.bluez.seat-monitoring = disabled
  }
}

monitor.bluez.properties = {
  bluez5.codecs = [ sbc ]
  bluez5.roles = [ a2dp_sink a2dp_source ]
}
WPCONF

# bluetoothd's D-Bus policy only grants MediaEndpoint1/Agent1/Profile1 to root —
# the pipewire user can send to org.bluez but can't expose endpoints for bluetoothd
# to call back into. Without this, WirePlumber's bluez5 plugin can reach bluetoothd
# but never registers a working media endpoint.
cat > "$ROOT_MOUNT/etc/dbus-1/system.d/zerofi-pipewire-bluez.conf" << 'DBUSPOLICY'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy user="pipewire">
    <allow send_destination="org.bluez"/>
    <allow send_interface="org.bluez.AdvertisementMonitor1"/>
    <allow send_interface="org.bluez.Agent1"/>
    <allow send_interface="org.bluez.MediaEndpoint1"/>
    <allow send_interface="org.bluez.MediaPlayer1"/>
    <allow send_interface="org.bluez.Profile1"/>
    <allow send_interface="org.bluez.GattCharacteristic1"/>
    <allow send_interface="org.bluez.GattDescriptor1"/>
    <allow send_interface="org.bluez.LEAdvertisement1"/>
  </policy>
</busconfig>
DBUSPOLICY

# CPU priority chain: bluetooth.service (signaling) > pipewire.service (encode/mix)
# > mpd.service (decode/resample) > flask/mympd. Pi Zero W is single-core — a
# scheduling slot stolen from the audio encoder during BT playback is an audible pop.
# user unit — drop-in under /etc/systemd/user/ (applies system-wide to that unit name)
mkdir -p "$ROOT_MOUNT/etc/systemd/user/pipewire.service.d"
cat > "$ROOT_MOUNT/etc/systemd/user/pipewire.service.d/cpu-priority.conf" << 'DROPIN'
[Service]
CPUWeight=200
DROPIN

# bluetooth.service: highest in the chain — A2DP signaling staying responsive keeps the encode fed
mkdir -p "$ROOT_MOUNT/etc/systemd/system/bluetooth.service.d"
cat > "$ROOT_MOUNT/etc/systemd/system/bluetooth.service.d/cpu-priority.conf" << 'DROPIN'
[Service]
CPUWeight=300
DROPIN

# 0x240414: standard CoD for "Loudspeaker" (Major: Rendering+Audio; Minor: Loudspeaker)
# — same value real BT speakers advertise; makes this look like a speaker to phones
sed -i '/^\[General\]/a Class = 0x240414' "$ROOT_MOUNT/etc/bluetooth/main.conf"

# DisablePlugins in main.conf is silently ignored on this BlueZ version
# ("Unknown key DisablePlugins for group General") — -P is the actual mechanism
cat > "$ROOT_MOUNT/etc/systemd/system/bluetooth.service.d/no-sap.conf" << 'DROPIN'
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/bluetoothd -P sap
DROPIN

# BCM43430A1 Bluetooth firmware — without it the chip comes up with placeholder
# BD address AA:AA:AA:AA:AA:AA. Not in Debian (no pi-bluetooth package here);
# fetched from same upstream source Raspberry Pi OS uses.
mkdir -p "$ROOT_MOUNT/lib/firmware/brcm"
curl -fsSL -o "$ROOT_MOUNT/lib/firmware/brcm/BCM43430A1.hcd" \
    https://raw.githubusercontent.com/RPi-Distro/bluez-firmware/master/broadcom/BCM43430A1.hcd

# mpd runs as the pipewire user (same as the PipeWire session) so it reaches
# PipeWire's native output with no cross-user socket handling. Must NOT also set
# `user "mpd"` in mpd.conf — mpd's own internal privilege-drop after systemd's
# User= has already dropped fails ("Failed to set group: Operation not permitted").
mkdir -p "$ROOT_MOUNT/etc/systemd/system/mpd.service.d"
cat > "$ROOT_MOUNT/etc/systemd/system/mpd.service.d/zerofi.conf" << 'DROPIN'
[Unit]
After=network.target mnt-music.mount

[Service]
User=pipewire
Group=pipewire
SupplementaryGroups=audio
# system unit — XDG_RUNTIME_DIR not inherited; must be set explicitly
Environment=XDG_RUNTIME_DIR=/run/user/997
# between PipeWire and flask: decode/resample feeds real-time mixing
CPUWeight=150
DROPIN

# myMPD: MYMPD_SSL=false because myMPD defaults to HTTPS and redirects — fine for
# a top-level load, silently broken inside an <iframe> (no click-through surface for
# cert warning in an embedded frame). Env vars read only on genuine first start;
# myMPD persists to /var/lib/mympd/config/ and ignores them on every later boot.
mkdir -p "$ROOT_MOUNT/etc/systemd/system/mympd.service.d"
cat > "$ROOT_MOUNT/etc/systemd/system/mympd.service.d/zerofi.conf" << 'DROPIN'
[Unit]
After=mpd.service
Wants=mpd.service

[Service]
Environment=MYMPD_HTTP_PORT=8080
Environment=MYMPD_SSL=false
Environment=MYMPD_MPD_HOST=127.0.0.1
Environment=MYMPD_MPD_PORT=6600
CPUWeight=50
DROPIN

# bgcolor/color here are the default palette's c1/c4 (flask_app/app.py's
# load_config() defaults) baked in by hand for the pre-first-boot state —
# they're only seen until _push_mympd_webui_settings() re-colors this same
# home-screen icon from the live config at first apply. If the default
# palette in app.py changes, update these two literals to match or the
# factory-fresh icon will look wrong for the few seconds before that runs.
mkdir -p "$ROOT_MOUNT/var/lib/mympd/state"
printf '%s\n' \
    '{"type":"icon","name":"Zero-Fi","ligature":"code","bgcolor":"#5c2a0a","color":"#9ccce8","image":"","cmd":"openExternalLink","options":["https://github.com/AutumnEggplant/zero-fi","true"]}' \
    > "$ROOT_MOUNT/var/lib/mympd/state/home_list"
chroot "$ROOT_MOUNT" chown nobody:nogroup /var/lib/mympd/state/home_list

cat > "$ROOT_MOUNT/etc/systemd/system/zerofi-bt-audio.service" << 'SERVICE'
[Unit]
Description=Zero-Fi incoming Bluetooth audio router
After=network.target

[Service]
Type=simple
ExecStart=/opt/zerofi/bluetooth-audio-router.sh
# system unit — XDG_RUNTIME_DIR not inherited
Environment=XDG_RUNTIME_DIR=/run/user/997
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

# persistent BT agent — handles per-profile AuthorizeService step (A2DP/Headset)
# that bluetoothd requires; without it or Trusted flag, device "pairs then disconnects".
# Runs in both Source and Target mode (enforces Source-mode rejection too).
cat > "$ROOT_MOUNT/etc/systemd/system/zerofi-bt-agent.service" << 'SERVICE'
[Unit]
Description=Zero-Fi Bluetooth pairing/authorization agent
After=bluetooth.service
Wants=bluetooth.service

[Service]
Type=simple
ExecStart=/opt/zerofi/bluetooth-agent.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE


cat > "$ROOT_MOUNT/etc/systemd/system/zerofi-heartbeat.service" << 'SERVICE'
[Unit]
Description=Zero-Fi housekeeping heartbeat

[Service]
Type=oneshot
ExecStart=/opt/zerofi/heartbeat.sh
# lightest thing on the box — must never contend with playback on a single-core Pi
CPUWeight=10
IOWeight=10
Nice=19
SERVICE

cat > "$ROOT_MOUNT/etc/systemd/system/zerofi-heartbeat.timer" << 'TIMER'
[Unit]
Description=Zero-Fi heartbeat every 5 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
TIMER

cat > "$ROOT_MOUNT/etc/systemd/system/zerofi-sync-worker.service" << 'SERVICE'
[Unit]
Description=Zero-Fi sync worker (one cycle)

[Service]
Type=simple
ExecStart=/opt/zerofi/sync-worker.py
CPUWeight=25
IOWeight=25
Nice=10
SERVICE

# anacron-like behavior: Persistent=true fires a missed run as soon as the next
# opportunity arises (device was off or WiFi unavailable at the scheduled time)
cat > "$ROOT_MOUNT/etc/systemd/system/zerofi-sync-discover.service" << 'SERVICE'
[Unit]
Description=Zero-Fi sync discovery
# A failed SMB mount fails fast (~1 min) rather than hanging, so retrying
# is just re-running the whole short-lived unit after a cooldown — no
# long-lived process for a reboot or watchdog to clobber mid-attempt.
# Burst cap bounds retries to the same day; the daily timer covers the rest.
StartLimitIntervalSec=6h
StartLimitBurst=4

[Service]
Type=oneshot
ExecStart=/opt/zerofi/sync-discover.py
CPUWeight=25
IOWeight=25
Nice=10
Restart=on-failure
RestartSec=30min
SERVICE

cat > "$ROOT_MOUNT/etc/systemd/system/zerofi-sync-discover.timer" << 'TIMER'
[Unit]
Description=Zero-Fi sync discovery — daily, and shortly after boot

[Timer]
OnCalendar=daily
OnBootSec=3min
Persistent=true

[Install]
WantedBy=timers.target
TIMER

cat > "$ROOT_MOUNT/etc/systemd/system/zerofi-ap.service" << 'SERVICE'
[Unit]
Description=Zero-Fi WiFi Access Point (concurrent with client)
After=network.target
Before=zerofi-flask.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=-/sbin/iw dev wlan0 interface add wlan0_ap type __ap
ExecStartPre=/sbin/ip link set wlan0_ap up
ExecStartPre=-/sbin/ip addr add 192.168.4.1/24 dev wlan0_ap
ExecStartPre=/opt/zerofi/select-ap-channel.sh
ExecStart=/bin/systemctl start dnsmasq
ExecStop=/bin/systemctl stop hostapd dnsmasq
ExecStopPost=-/sbin/iw dev wlan0_ap del

[Install]
WantedBy=multi-user.target
SERVICE

cat > "$ROOT_MOUNT/etc/mpd.conf" << 'MPDCONF'
music_directory     "/mnt/music"
db_file             "/var/lib/mpd/tag_cache"
log_file            "syslog"
pid_file            "/run/mpd/pid"
state_file          "/var/lib/mpd/state"
sticker_file        "/var/lib/mpd/sticker.sql"

# off deliberately — inotify watches staging churn during a sync, not just
# real library changes, triggering a myMPD cache rebuild worker and a
# "No such file or directory" error on every renamed-away temp file.
# sync-worker.py runs one explicit `mpc update` at the end of a cycle instead.
auto_update         "no"

bind_to_address     "/run/mpd/socket"
bind_to_address     "0.0.0.0"
port                "6600"

replaygain                  "auto"

audio_output {
    type            "pipewire"
    name            "Zero-Fi"
}
MPDCONF

# /var/lib/mpd created and owned by mpd:audio by the package postinst —
# now inaccessible since mpd.service.d/zerofi.conf runs mpd as the pipewire user
chroot "$ROOT_MOUNT" chown -R pipewire:audio /var/lib/mpd

# mDNS advertisement for MPD — not via mpd.conf zeroconf_enabled: mpd runs under
# socket activation (`mpd --systemd` inherits the already-open fd), so it has no
# "own port" to hand to its built-in zeroconf and logs "No global port, disabling
# zeroconf." A static Avahi service file works regardless of how mpd's socket arrived.
mkdir -p "$ROOT_MOUNT/etc/avahi/services"
cat > "$ROOT_MOUNT/etc/avahi/services/mpd.service" << 'AVAHISVC'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name>Zero-Fi-Configuring</name>
  <service>
    <type>_mpd._tcp</type>
    <port>6600</port>
  </service>
</service-group>
AVAHISVC

# output_backend=alsa: redirected to PipeWire transparently by pipewire-alsa's
# own /etc/alsa/conf.d/99-pipewire-default.conf. No pa backend in this build.
# name= is a placeholder — apply_ap_config() rewrites it to match the AP SSID.
cat > "$ROOT_MOUNT/etc/shairport-sync.conf" << 'SHAIRPORT'
general =
{
  name = "Zero-Fi-Configuring";
  output_backend = "alsa";
};

sessioncontrol =
{
  run_this_before_play_begins = "/opt/zerofi/airplay-session-start.sh";
  run_this_after_play_ends = "/opt/zerofi/airplay-session-end.sh";
  wait_for_completion = "no";
};
SHAIRPORT

# shairport-sync runs as pipewire user (same as mpd) to reach the PipeWire session
mkdir -p "$ROOT_MOUNT/etc/systemd/system/shairport-sync.service.d"
cat > "$ROOT_MOUNT/etc/systemd/system/shairport-sync.service.d/zerofi.conf" << 'DROPIN'
[Service]
User=pipewire
Group=pipewire
SupplementaryGroups=audio
Environment=XDG_RUNTIME_DIR=/run/user/997
DROPIN

# shairport-sync's D-Bus policy files only grant its MPRIS/native interfaces to
# "root" and "shairport-sync" — running as "pipewire" produces "could not acquire
# an MPRIS interface" warnings. Patch in place with sed (keeps the rest of the
# package-shipped policy untouched) rather than overwriting the whole file.
for f in "$ROOT_MOUNT/etc/dbus-1/system.d/shairport-sync-dbus-policy.conf" \
         "$ROOT_MOUNT/etc/dbus-1/system.d/shairport-sync-mpris-policy.conf"; do
    if [ -f "$f" ] && ! grep -q '<policy user="pipewire">' "$f"; then
        sed -i 's#\(\s*\)<policy user="shairport-sync">#\1<policy user="pipewire">\n\1  <allow own="org.gnome.ShairportSync"/>\n\1  <allow own="org.mpris.MediaPlayer2.ShairportSync"/>\n\1</policy>\n\n\1<policy user="shairport-sync">#' "$f"
    fi
done

echo "zerofi" > "$ROOT_MOUNT/etc/hostname"
if grep -q '^127\.0\.1\.1' "$ROOT_MOUNT/etc/hosts" 2>/dev/null; then
    sed -i 's/^127\.0\.1\.1.*/127.0.1.1\tzerofi/' "$ROOT_MOUNT/etc/hosts"
else
    echo -e "127.0.1.1\tzerofi" >> "$ROOT_MOUNT/etc/hosts"
fi

chroot "$ROOT_MOUNT" systemctl enable zerofi-ap.service zerofi-flask.service mpd.service mympd.service avahi-daemon.service zerofi-heartbeat.timer rsyslog.service shairport-sync.service zerofi-bt-audio.service zerofi-bt-agent.service bluetooth.service dhcpcd.service zerofi-sync-discover.timer 2>/dev/null || true
chroot "$ROOT_MOUNT" systemctl disable hostapd dnsmasq 2>/dev/null || true

# dhcpcd manages every interface by default — wlan0_ap gets a static 192.168.4.1
# from zerofi-ap.service; no reason to let dhcpcd try an always-futile lease there
echo "denyinterfaces wlan0_ap" >> "$ROOT_MOUNT/etc/dhcpcd.conf"

# Home routers that run a DHCPv6 server with no addresses configured (or none
# at all, relying on RA-only SLAAC) make dhcpcd retry DHCPv6 indefinitely,
# logging "DHCPv6 REPLY: No Addresses Available" every cycle even once wlan0
# already has a healthy v4 address and routes. `nodhcp6` stops dhcpcd from
# requesting a DHCPv6 address/prefix at all — SLAAC (`ipv6rs`, on by default)
# is untouched, so IPv6 itself still works if the network offers it that way.
echo "nodhcp6" >> "$ROOT_MOUNT/etc/dhcpcd.conf"

# dietpi-ramlog/dietpi-preboot/dietpi-postboot all reference /boot/dietpi/ which
# doesn't exist in this image — show as "failed" on every boot otherwise.
# `systemctl mask` silently no-ops in chroot (no live systemd/D-Bus) — confirmed
# on real hardware that masked units were still plainly "enabled." Direct symlink
# to /dev/null is what mask actually creates; do it directly.
for unit in dietpi-ramlog.service dietpi-preboot.service dietpi-postboot.service; do
    rm -f "$ROOT_MOUNT/etc/systemd/system/$unit"
    ln -sf /dev/null "$ROOT_MOUNT/etc/systemd/system/$unit"
done

# dietpi-fs_partition_resize.service is enabled via a static .wants symlink
# (ignores dietpi.txt's AUTO_SETUP_ROOTFS_SIZE=0 — that gates a different mechanism).
# It runs Before=local-fs-pre.target and calls `partx -uv` on mmcblk0p2, which
# causes a transient remove/re-add of all partition device nodes on real hardware —
# racing mnt-music.mount's device dependency and failing it ("Dependency failed"),
# which then starves zerofi-flask.service. Same systemctl mask no-op issue — direct
# symlink instead.
rm -f "$ROOT_MOUNT/etc/systemd/system/local-fs.target.wants/dietpi-fs_partition_resize.service"
rm -f "$ROOT_MOUNT/etc/systemd/system/dietpi-fs_partition_resize.service"
ln -sf /dev/null "$ROOT_MOUNT/etc/systemd/system/dietpi-fs_partition_resize.service"

# Dropbear: -s disables password auth (keys only). Dropbear, not openssh — no
# ssh.service alias; only dropbear.service exists.
sed -i "s|^#DROPBEAR_EXTRA_ARGS=.*|DROPBEAR_EXTRA_ARGS=\"-s\"|" "$ROOT_MOUNT/etc/default/dropbear"

# channel=1 is a placeholder — zerofi-ap.service runs select-ap-channel.sh before
# every start, which rewrites this line based on wlan0's live state
cat > "$ROOT_MOUNT/etc/hostapd/hostapd.conf" << 'HOSTAPD'
interface=wlan0_ap
driver=nl80211
ssid=Zero-Fi-Configuring
hw_mode=g
channel=1
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=0
HOSTAPD

mkdir -p "$ROOT_MOUNT/etc/dnsmasq.d"
cat > "$ROOT_MOUNT/etc/dnsmasq.d/zerofi-ap.conf" << 'DNSMASQ'
interface=wlan0_ap
bind-interfaces
dhcp-range=192.168.4.2,192.168.4.20,255.255.255.0,24h
dhcp-option=3,192.168.4.1
dhcp-option=6,192.168.4.1
address=/#/192.168.4.1
DNSMASQ

# full allowlist instead of selective strip — DietPi's fstab PARTUUIDs reference
# the original image's disk identifier (replaced by our `parted mklabel msdos`).
# A denylist that misses one UUID form hangs boot in emergency mode on timeout.
cat > "$ROOT_MOUNT/etc/fstab" << 'FSTAB'
/dev/mmcblk0p1  /boot       vfat    defaults                                          0  2
/dev/mmcblk0p2  /           ext4    defaults,noatime                                  0  1
/dev/mmcblk0p3  /mnt/music  exfat   defaults,noatime,nofail,x-systemd.device-timeout=30  0  0
tmpfs           /var/log    tmpfs   defaults,noatime,nosuid,nodev,size=16M            0  0
FSTAB
mkdir -p "$ROOT_MOUNT/mnt/music"

# passno=0 deliberately — systemd-fsck would run a full exFAT scan on every boot
# (potentially minutes on a large card). fsck-music.sh reads the VolumeDirty bit
# from the boot sector (nearly free) and only scans when the last shutdown was unclean.
# Wants= not Requires=: a failed check must not stop the library from mounting.
cat > "$ROOT_MOUNT/etc/systemd/system/zerofi-fsck-music.service" << 'SERVICE'
[Unit]
Description=Zero-Fi music partition integrity check
DefaultDependencies=no
After=dev-mmcblk0p3.device systemd-modules-load.service
Wants=dev-mmcblk0p3.device
Before=mnt-music.mount
ConditionPathExists=/dev/mmcblk0p3

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/zerofi/fsck-music.sh
TimeoutStartSec=900
SERVICE

mkdir -p "$ROOT_MOUNT/etc/systemd/system/mnt-music.mount.d"
cat > "$ROOT_MOUNT/etc/systemd/system/mnt-music.mount.d/fsck.conf" << 'DROPIN'
[Unit]
Wants=zerofi-fsck-music.service
After=zerofi-fsck-music.service
DROPIN

# force load at boot — on a single ARM11 core starting multiple services concurrently,
# modprobe-on-demand was racing the mount's device-timeout and losing
echo exfat > "$ROOT_MOUNT/etc/modules-load.d/exfat.conf"

# brcmfmac enables WiFi power save by default; it is a known cause of SDIO bus stalls
# on BCM43430A1 — confirmed via captured hang forensics:
#   wchan: mmc_wait_for_req_done
#   [<0>] brcmf_sdiod_sglist_rw+0x2c8/0x6b0 [brcmfmac]
# nothing to save on a mains-powered appliance; re-asserted by heartbeat.sh every
# 5 minutes since the setting can reset on re-association
cat > "$ROOT_MOUNT/etc/systemd/system/zerofi-wifi-powersave.service" << 'SERVICE'
[Unit]
Description=Zero-Fi disable WiFi power save (brcmfmac SDIO stall mitigation)
After=network.target
Wants=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
# `|| true` — wlan0 may not exist yet; heartbeat.sh re-assert covers the gap
ExecStart=/bin/sh -c '/sbin/iw dev wlan0 set power_save off || true'

[Install]
WantedBy=multi-user.target
SERVICE
chroot "$ROOT_MOUNT" systemctl enable zerofi-wifi-powersave.service >/dev/null 2>&1

# wpa_cli -a registers an action script called on every wpa_supplicant
# connection-state change. On DISCONNECTED, wifi-ap-fallback.sh waits 30s then
# checks if still disconnected + client was expected + AP is down — if all three,
# starts the AP. Safety net for "driving away" without re-enabling the AP first.
chmod +x "$ROOT_MOUNT/opt/zerofi/wifi-ap-fallback.sh"
cat > "$ROOT_MOUNT/etc/systemd/system/zerofi-wifi-watch.service" << 'SERVICE'
[Unit]
Description=Zero-Fi WiFi AP fallback monitor
After=wpa_supplicant@wlan0.service
BindsTo=wpa_supplicant@wlan0.service

[Service]
Type=simple
ExecStart=/sbin/wpa_cli -i wlan0 -a /opt/zerofi/wifi-ap-fallback.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE
chroot "$ROOT_MOUNT" systemctl enable zerofi-wifi-watch.service >/dev/null 2>&1

cat > "$MUSIC_MOUNT/README.txt" << 'README'
🎵 Zero-Fi — Music Partition

Drag your music files and folders here. The Pi reads
them from /mnt/music on boot.

Supported formats: MP3, FLAC, AAC, OGG, WAV, and
anything else mpd can play.
README

# synthesized test tone — something to play on fresh install without a real import;
# generated with Python stdlib (no new build dep, no binary in the repo, no license question)
python3 - "$MUSIC_MOUNT/Zero-Fi-Test-Tone-440Hz.wav" << 'PYEOF'
import sys, wave, struct, math

path = sys.argv[1]
rate, duration, freq, fade_ms = 44100, 4, 440.0, 50
n = int(rate * duration)
fade_n = int(rate * fade_ms / 1000)

with wave.open(path, "w") as f:
    f.setnchannels(1)
    f.setsampwidth(2)
    f.setframerate(rate)
    frames = bytearray()
    for i in range(n):
        amp = 0.3
        if i < fade_n:
            amp *= i / fade_n
        elif i > n - fade_n:
            amp *= (n - i) / fade_n
        frames += struct.pack("<h", int(32767 * amp * math.sin(2 * math.pi * freq * i / rate)))
    f.writeframes(frames)
PYEOF

# ── Cleanup ────────────────────────────────────────────────────────────
echo "[6/6] Cleaning up..."

rm -f "$ROOT_MOUNT/usr/bin/qemu-arm-static"

umount "$ROOT_MOUNT/proc" 2>/dev/null || true
umount "$ROOT_MOUNT/sys" 2>/dev/null || true
umount "$ROOT_MOUNT/dev" 2>/dev/null || true
umount "$ROOT_MOUNT/run" 2>/dev/null || true
umount "$ROOT_MOUNT/var/cache/apt/archives" 2>/dev/null || true

umount "$ROOT_MOUNT" 2>/dev/null || true
umount "$BOOT_MOUNT" 2>/dev/null || true
umount "$MUSIC_MOUNT" 2>/dev/null || true

losetup -d "$LOOP_DEV" 2>/dev/null || true

if [[ "$RELEASE_IMAGE" -eq 1 || -n "${COMPRESS:-}" ]]; then
    echo "  Compressing image..."
    xz -T0 -f "$OUTPUT_IMAGE"
    FINAL_ARTIFACT="$OUTPUT_ARCHIVE"
    FINAL_SIZE=$(ls -lh "$FINAL_ARTIFACT" | awk '{print $5}')
else
    FINAL_ARTIFACT="$OUTPUT_IMAGE"
    FINAL_SIZE=$(ls -lh "$FINAL_ARTIFACT" | awk '{print $5}')
fi

if [[ "$RELEASE_IMAGE" -eq 1 ]]; then
    echo "  Computing SHA256SUM..."
    SHA_FILE="${FINAL_ARTIFACT}.sha256"
    ( cd "$REPO_DIR" && sha256sum "$(basename "$FINAL_ARTIFACT")" > "$SHA_FILE" )
fi

echo ""
echo "=== Build complete! ==="
echo "  Image: $FINAL_ARTIFACT"
echo "  Size:  $FINAL_SIZE"
if [[ "$RELEASE_IMAGE" -eq 1 ]]; then
    echo "  SHA256SUM (for release notes):"
    echo "    $(cat "$SHA_FILE")"
fi
echo ""
echo "To flash to SD card:"
echo "  sudo bash build/flash-sd.sh /dev/sdX"
echo ""
if [[ "$FINAL_ARTIFACT" == *.xz ]]; then
    echo "Or, to test end user experience (raw write, skips config/music prefill and"
    echo "partition pre-expansion — first boot handles all of that itself):"
    echo "  xzcat $FINAL_ARTIFACT | sudo dd of=/dev/sdX bs=4M status=progress oflag=direct && sync"
else
    echo "Or, to test end user experience (raw write, skips config/music prefill and"
    echo "partition pre-expansion — first boot handles all of that itself):"
    echo "  sudo dd if=$FINAL_ARTIFACT of=/dev/sdX bs=4M status=progress oflag=direct && sync"
fi
echo ""
echo "First boot:"
echo "  1. Pi projects open AP 'Zero-Fi' — no setup wizard, it's usable immediately"
echo "  2. Music partition auto-expands to fill the SD card"
echo "  3. Visit http://192.168.4.1 or http://zerofi.local (captive portal should pop up automatically)"
echo "  4. Add home WiFi (optional) any time via Settings"
