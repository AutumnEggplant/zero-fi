#!/bin/bash
# Zero-Fi — boot-time integrity check for the music partition.
#
# Runs before mnt-music.mount while /dev/mmcblk0p3 is still unmounted
# (fsck on a mounted filesystem corrupts it). Reads exFAT's VolumeDirty bit
# from the boot sector — nearly free — and only runs the full scan when the
# last shutdown was actually unclean. Avoids the fstab passno approach, which
# would scan 116GB on every boot.

set -uo pipefail

MUSIC_DEV=/dev/mmcblk0p3
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

echo "=== Zero-Fi: music partition check ==="

if [[ ! -b "$MUSIC_DEV" ]]; then
    echo "  $MUSIC_DEV not present — skipping."
    exit 0
fi

# /proc/self/mounts (plain text) not `mountpoint` — can't block on a
# misbehaving device the way a stat()-based check can
if grep -q "^$MUSIC_DEV " /proc/self/mounts; then
    echo "  WARNING: $MUSIC_DEV is already mounted — refusing to fsck a live filesystem."
    exit 0
fi

# exFAT VolumeFlags at offset 0x6A: bit 1 = VolumeDirty, bit 2 = MediaFailure
read -r FS_NAME DIRTY MEDIA_FAILURE <<< "$(python3 - "$MUSIC_DEV" <<'PYEOF'
import struct, sys
try:
    with open(sys.argv[1], "rb") as f:
        bs = f.read(512)
    flags, = struct.unpack_from("<H", bs, 0x6A)
    print(bs[3:11].decode("ascii", "replace").strip(), (flags >> 1) & 1, (flags >> 2) & 1)
except Exception as e:
    # unreadable boot sector → run fsck rather than silently skip
    print("UNREADABLE", 1, 0)
PYEOF
)"

if [[ "$FS_NAME" != "EXFAT" ]]; then
    echo "  WARNING: $MUSIC_DEV doesn't look like exFAT (read '$FS_NAME') — running fsck to find out."
fi
if [[ "$MEDIA_FAILURE" == "1" ]]; then
    echo "  WARNING: exFAT MediaFailure flag is set — the driver has recorded bad clusters on $MUSIC_DEV. This is an SD card problem, not just a dirty unmount."
fi

if [[ "$DIRTY" != "1" && "$FORCE" -eq 0 ]]; then
    echo "  Clean (volume was unmounted properly) — no check needed."
    exit 0
fi

if [[ "$FORCE" -eq 1 ]]; then
    echo "  Forced check requested."
else
    echo "  Volume is marked dirty (last shutdown wasn't clean) — repairing."
fi

# -p: fix what's unambiguously fixable without prompting (no interactive
# console at boot); anything it can't fix gets logged and the mount proceeds
START=$(date +%s)
fsck.exfat -p "$MUSIC_DEV"
FSCK_STATUS=$?
ELAPSED=$(( $(date +%s) - START ))

if [[ $FSCK_STATUS -eq 0 ]]; then
    echo "  Check complete in ${ELAPSED}s — no problems left unfixed."
else
    echo "  WARNING: fsck.exfat exited $FSCK_STATUS after ${ELAPSED}s — some damage could not be repaired automatically. Mounting anyway; the library may have missing or truncated files."
fi

echo "=== Music partition check complete ==="
exit 0
