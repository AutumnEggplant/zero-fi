#!/usr/bin/env bash
# Zero-Fi SD Card Flasher
#
# Writes a pre-built Zero-Fi image to an SD card, then expands the music
# partition to fill the card and optionally pre-populates config and music.
# Handles both .img (uncompressed) and .img.xz (compressed) images.
#
# Usage:
#   sudo bash build/flash-sd.sh /dev/sdX [zerofi-image[.xz]] [--music DIR]
#
# Config pre-population:
#   Drop a zerofi.json next to this script (build/zerofi.json — gitignored).
#   It will be copied to the music partition on flash so the Pi boots already
#   configured. Same idea as the build-time SSH key injection.
#
# Music pre-population:
#   --music DIR   rsync DIR's contents onto the music partition after flashing.
#                 Useful for reflashes where you want to skip the sync cycle.

set -euo pipefail

MUSIC_SRC=""

# Parse flags before positional args
POSITIONAL=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --music) MUSIC_SRC="$2"; shift 2 ;;
        *) POSITIONAL+=("$1"); shift ;;
    esac
done
set -- "${POSITIONAL[@]}"

if [[ $# -lt 1 ]]; then
    echo "Usage: sudo bash $0 /dev/sdX [zerofi-image[.xz]] [--music DIR]"
    echo "  e.g. sudo bash $0 /dev/sda"
    echo "  e.g. sudo bash $0 /dev/sda zerofi-0.1.0.img"
    echo "  e.g. sudo bash $0 /dev/sda --music /mnt/nas/Music"
    exit 1
fi

DEVICE="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Find image — prefer uncompressed, fall back to compressed
if [[ $# -ge 2 ]]; then
    IMAGE="$2"
else
    IMAGE=$(ls -t "$REPO_DIR"/zerofi-*.img 2>/dev/null | head -1)
    if [[ -z "$IMAGE" ]]; then
        IMAGE=$(ls -t "$REPO_DIR"/zerofi-*.img.xz 2>/dev/null | head -1)
    fi
fi

if [[ -z "$IMAGE" || ! -f "$IMAGE" ]]; then
    echo "ERROR: No Zero-Fi image found. Build one first:"
    echo "  sudo bash build/build-image.sh"
    exit 1
fi

if [[ ! -b "$DEVICE" ]]; then
    echo "ERROR: $DEVICE is not a block device"
    exit 1
fi

if mount | grep -q "$DEVICE"; then
    echo "ERROR: $DEVICE has mounted partitions — unmount first"
    exit 1
fi

if [[ -n "$MUSIC_SRC" && ! -d "$MUSIC_SRC" ]]; then
    echo "ERROR: --music directory not found: $MUSIC_SRC"
    exit 1
fi

CONFIG_PREFILL="$SCRIPT_DIR/zerofi.json"

# Show device facts before the confirm prompt. "Connected" is the device
# node's ctime: devtmpfs creates it fresh when a USB reader is plugged in,
# so a just-inserted card reads as seconds old vs an always-there internal
# disk which reads as boot-uptime-old.
DEV_SIZE=$(lsblk -dn -o SIZE "$DEVICE" 2>/dev/null | sed 's/^ *//;s/ *$//')
DEV_MODEL=$(lsblk -dn -o MODEL "$DEVICE" 2>/dev/null | sed 's/^ *//;s/ *$//')
DEV_VENDOR=$(lsblk -dn -o VENDOR "$DEVICE" 2>/dev/null | sed 's/^ *//;s/ *$//')
DEV_TRAN=$(lsblk -dn -o TRAN "$DEVICE" 2>/dev/null)
DEV_DESC="${DEV_VENDOR} ${DEV_MODEL}"
DEV_DESC="$(echo "$DEV_DESC" | sed 's/^ *//;s/ *$//')"
[[ -z "$DEV_DESC" ]] && DEV_DESC="(no vendor/model reported)"
[[ -n "$DEV_TRAN" ]] && DEV_DESC="$DEV_DESC [$DEV_TRAN]"

NOW=$(date +%s)
NODE_CTIME=$(stat -c %Z "$DEVICE" 2>/dev/null || echo "$NOW")
ELAPSED=$((NOW - NODE_CTIME))
if (( ELAPSED < 60 )); then
    CONNECTED="${ELAPSED}s ago"
elif (( ELAPSED < 3600 )); then
    CONNECTED="$((ELAPSED / 60))m ago"
elif (( ELAPSED < 86400 )); then
    CONNECTED="$((ELAPSED / 3600))h ago"
else
    CONNECTED="$((ELAPSED / 86400))d ago"
fi

echo "=== Zero-Fi SD Card Flasher ==="
echo "  Device:      $DEVICE"
echo "  Size:        ${DEV_SIZE:-unknown}"
echo "  Description: $DEV_DESC"
echo "  Connected:   $CONNECTED"
echo "  Image:       $IMAGE ($(ls -lh "$IMAGE" | awk '{print $5}'))"
[[ -f "$CONFIG_PREFILL" ]] && echo "  Config:      $CONFIG_PREFILL (will be pre-populated)"
[[ -n "$MUSIC_SRC"      ]] && echo "  Music:       $MUSIC_SRC (will be rsynced)"
echo ""

if (( ELAPSED > 3600 )); then
    echo "NOTE: this device has been connected for a while — if you expected"
    echo "a card you just inserted, double check this is the right one."
    echo ""
fi

echo "WARNING: This will completely overwrite $DEVICE!"
echo ""
read -rp "Type 'yes' to continue: " CONFIRM
if [[ "$CONFIRM" != "yes" ]]; then
    echo "Aborted."
    exit 1
fi

echo ""
echo "Flashing..."

if [[ "$IMAGE" == *.xz ]]; then
    xzcat "$IMAGE" | dd of="$DEVICE" bs=4M status=progress oflag=direct
else
    dd if="$IMAGE" of="$DEVICE" bs=4M status=progress oflag=direct
fi
sync

# ── Expand music partition ────────────────────────────────────────────────────
# On the flash host the card is a USB reader, not the boot disk, so the kernel
# will re-read the partition table immediately after parted — no reboot needed.
echo ""
echo "Expanding music partition..."

partprobe "$DEVICE"
udevadm settle

P3="${DEVICE}p3"
[[ ! -b "$P3" ]] && P3="${DEVICE}3"
if [[ ! -b "$P3" ]]; then
    echo "ERROR: cannot find partition 3 on $DEVICE — skipping expansion"
    echo "  First boot will expand it instead."
else
    parted -s "$DEVICE" resizepart 3 100% >/dev/null
    partprobe "$DEVICE"
    udevadm settle
    mkfs.exfat -n MUSIC "$P3" >/dev/null
    echo "  Partition expanded and formatted."

    MUSIC_MNT=$(mktemp -d)
    MUSIC_MOUNTED=0
    cleanup_music() { (( MUSIC_MOUNTED )) && umount "$MUSIC_MNT" 2>/dev/null || true; rmdir "$MUSIC_MNT" 2>/dev/null || true; }
    trap cleanup_music EXIT

    if mount "$P3" "$MUSIC_MNT"; then
        MUSIC_MOUNTED=1

        cat > "$MUSIC_MNT/README.txt" << 'README'
🎵 Zero-Fi — Music Partition

Drag your music files and folders here. The Pi reads
them from /mnt/music on boot.

Supported formats: MP3, FLAC, AAC, OGG, WAV, and
anything else mpd can play.
README

        if [[ -f "$CONFIG_PREFILL" ]]; then
            cp "$CONFIG_PREFILL" "$MUSIC_MNT/zerofi.json"
            echo "  Config pre-populated from $(basename "$CONFIG_PREFILL")."
        fi

        if [[ -n "$MUSIC_SRC" ]]; then
            echo "  Pre-populating music from $MUSIC_SRC..."
            rsync -a --info=progress2 "${MUSIC_SRC%/}/" "$MUSIC_MNT/"
            echo "  Music copied."
        fi

        sync
        umount "$MUSIC_MNT"
        MUSIC_MOUNTED=0
    else
        echo "  WARNING: could not mount $P3 — skipping pre-population."
    fi

    rmdir "$MUSIC_MNT"
    trap - EXIT
fi

echo ""
echo "=== Done! ==="
echo "SD card ready. Insert into Pi Zero W and power on."
echo ""
echo "First boot:"
if [[ -f "$CONFIG_PREFILL" ]]; then
    echo "  1. Pi opens AP using your pre-populated config — connect and go"
else
    echo "  1. Pi opens AP 'Zero-Fi' — usable immediately, configure via Settings"
fi
echo "  2. Visit http://192.168.4.1 or http://zerofi.local (captive portal should pop up automatically)"
