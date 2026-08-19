#!/bin/bash
# Zero-Fi — Expand music partition to fill SD card
# Called from app.py's __main__ on first boot (or after factory reset) when
# no config exists. The size check makes repeat calls a no-op.
#
# Resizing a partition on the same disk the running OS is on: the kernel may
# refuse to re-read the live partition table (EBUSY). Fix: two-pass split by
# a reboot:
#   pass 1: rewrite partition table, exit 75 (caller reboots)
#   pass 2: kernel sees the new size — just format it
# See app.py's handling of exit code 75 for the reboot trigger.

set -euo pipefail

PENDING_MARKER=/var/lib/zerofi/.music-expand-pending-format

echo "=== Zero-Fi: Checking music partition ==="

ROOT_DEV=$(findmnt -n -o SOURCE /)
# mmcblk0p2 → mmcblk0 vs sda2 → sda: two different naming schemes,
# chain both unconditionally would over-strip mmcblk0's own trailing digit
DISK=$(echo "$ROOT_DEV" | sed -E 's/(p[0-9]+|[0-9]+)$//')
MUSIC_PART="${DISK}p3"
[[ ! -b "$MUSIC_PART" ]] && MUSIC_PART="${DISK}3"

if [[ ! -b "$MUSIC_PART" ]]; then
    echo "ERROR: Cannot find music partition"
    exit 1
fi

# parted "Disk: Ns" is the sector *count*; last usable sector is N-1
DISK_END=$(($(parted -s "$DISK" unit s print | grep "^Disk $DISK:" | awk '{print $3}' | sed 's/s//') - 1))
CURRENT_END=$(parted -s "$DISK" unit s print | awk '$1 == "3" {print $3}' | sed 's/s//')

ALREADY_FULL_SIZE=0
[[ -n "$CURRENT_END" ]] && (( DISK_END - CURRENT_END < 2048 )) && ALREADY_FULL_SIZE=1

if [[ -f "$PENDING_MARKER" ]]; then
    if [[ "$ALREADY_FULL_SIZE" -ne 1 ]]; then
        echo "  ERROR: pending-format marker present but partition still isn't full size after a reboot — giving up."
        rm -f "$PENDING_MARKER"
        exit 1
    fi
    echo "  Partition table already resized (post-reboot) — formatting..."
elif [[ "$ALREADY_FULL_SIZE" -eq 1 ]]; then
    # skip if already at full size and mountable — avoids wiping a library on factory reset
    if mountpoint -q /mnt/music || mount /mnt/music 2>/dev/null; then
        echo "  Already expanded and readable — nothing to do."
        exit 0
    fi
    echo "  Already at full size but unreadable — reformatting to recover."
else
    echo "  Resizing partition..."
    mountpoint -q /mnt/music && umount /mnt/music || true
    # tolerate nonzero: kernel may refuse live table re-read even though
    # the on-disk write went through; the reboot handles either outcome
    parted -s "$DISK" resizepart 3 "${DISK_END}s" || true
    mkdir -p "$(dirname "$PENDING_MARKER")"
    touch "$PENDING_MARKER"
    echo "  Partition table updated — reboot required before formatting."
    exit 75
fi

mountpoint -q /mnt/music && umount /mnt/music || true

# no in-place exFAT resize tool on Linux — reformat to new size
echo "  Reformatting filesystem to new size..."
mkfs.exfat -n MUSIC "$MUSIC_PART" >/dev/null
rm -f "$PENDING_MARKER"

mount -a 2>/dev/null || true

if mountpoint -q /mnt/music; then
    cat > /mnt/music/README.txt << 'README'
🎵 Zero-Fi — Music Partition

Drag your music files and folders here. The Pi reads
them from /mnt/music on boot.

Supported formats: MP3, FLAC, AAC, OGG, WAV, and
anything else mpd can play.
README
fi

echo "=== Music partition expanded! ==="
