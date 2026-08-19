#!/usr/bin/env python3
# Extract embedded cover art to cover.jpg in each album directory that
# doesn't already have one. Called from sync-worker.py's process_sync() after
# copying an album's files, so MPD's albumart command finds a standalone file
# instead of opening each audio file — much faster on a Pi Zero W's SD card.
#
# Incremental: skips any directory that already has cover.jpg, so repeated
# runs over a stable library are a fast no-op.

import os
import sys

try:
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4
except ImportError:
    print("  Cover art extraction skipped (python3-mutagen not available)")
    sys.exit(0)

AUDIO_EXTS = {'.flac', '.mp3', '.m4a', '.mp4', '.aac', '.ogg', '.opus'}

music_dir = sys.argv[1] if len(sys.argv) > 1 else '/mnt/music'
extracted = 0
skipped = 0

for dirpath, dirnames, filenames in os.walk(music_dir):
    dirnames.sort()

    if os.path.exists(os.path.join(dirpath, 'cover.jpg')):
        continue

    audio_files = sorted(f for f in filenames
                         if os.path.splitext(f)[1].lower() in AUDIO_EXTS)
    if not audio_files:
        continue

    art_data = None
    for fname in audio_files:
        fpath = os.path.join(dirpath, fname)
        ext = os.path.splitext(fname)[1].lower()
        try:
            if ext == '.flac':
                tags = FLAC(fpath)
                pics = tags.pictures
                front = next((p for p in pics if p.type == 3), None)
                art_data = (front or (pics[0] if pics else None))
                if art_data:
                    art_data = art_data.data
            elif ext == '.mp3':
                tags = ID3(fpath)
                apic_tags = [t for t in tags.values() if t.FrameID == 'APIC']
                front = next((t for t in apic_tags if t.type == 3), None)
                tag = front or (apic_tags[0] if apic_tags else None)
                if tag:
                    art_data = tag.data
            elif ext in ('.m4a', '.mp4', '.aac'):
                tags = MP4(fpath)
                if 'covr' in tags:
                    art_data = bytes(tags['covr'][0])
        except Exception:
            continue

        if art_data:
            break

    if art_data:
        cover_path = os.path.join(dirpath, 'cover.jpg')
        try:
            with open(cover_path, 'wb') as f:
                f.write(art_data)
            extracted += 1
        except Exception as e:
            skipped += 1
    else:
        skipped += 1

print(f"  Cover art: {extracted} extracted, {skipped} dirs with no embeds found")
