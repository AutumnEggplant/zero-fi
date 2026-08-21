#!/usr/bin/env python3
"""Zero-Fi sync discovery — walk the SMB source and enqueue qualify jobs.

Runs as a oneshot systemd service (zerofi-sync-discover.service), triggered
by a daily timer just after 2am and optionally on demand from the Settings UI.
Mounts the source, walks it for directories that directly contain audio files,
creates one qualify job per album directory, then unmounts. The sync worker
(zerofi-sync-worker.service) processes the qualify jobs independently.

Also doubles as the SMB reachability check ("Test Library Visibility" in
Settings) via --test: mounts, then immediately unmounts without walking.
Shares mount_smb()/unmount_smb() with the real discovery run rather than
reimplementing the mount separately, so the test actually exercises the same
code path discovery uses — a separate implementation could pass while the
real path is broken, or vice versa.
"""

import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import zerofi_config

MUSIC_DIR       = Path("/mnt/music")
SMB_MOUNT       = Path("/mnt/smb-source")
SMB_CREDENTIALS = Path("/run/zerofi/smb-credentials")
QUEUE_DIR       = Path("/run/zerofi/sync-queue")
QUALIFY_DIR     = QUEUE_DIR / "qualify"

AUDIO_EXTENSIONS = frozenset({
    ".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wv", ".ape"
})

_qualify_seq = itertools.count()


def log(msg):
    print(f"[sync-discover] {msg}", flush=True)


def is_audio(name):
    return Path(name).suffix.lower() in AUDIO_EXTENSIONS


def enqueue_qualify(rel_dir):
    QUALIFY_DIR.mkdir(parents=True, exist_ok=True)
    seq = next(_qualify_seq)
    path = QUALIFY_DIR / f"0050-{seq:010d}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"args": {"dir": rel_dir}}))
    tmp.rename(path)


def has_lan():
    result = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "wlan0"],
        capture_output=True, text=True
    )
    return bool(result.stdout.strip())


def mount_smb(config):
    """Mount the configured SMB source at SMB_MOUNT. Returns (ok, error_message)."""
    smb_source = config.get("smb_source", "")
    if not smb_source:
        return False, None  # caller distinguishes "not configured" from a real failure

    if smb_source.startswith("smb://"):
        smb_source = "//" + smb_source[6:]

    username = config.get("smb_username", "")
    password = config.get("smb_password", "")
    if username or password:
        SMB_CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
        SMB_CREDENTIALS.write_text(f"username={username}\npassword={password}\n")
        os.chmod(SMB_CREDENTIALS, 0o600)
        mount_auth = f"credentials={SMB_CREDENTIALS}"
    else:
        mount_auth = "guest"

    SMB_MOUNT.mkdir(parents=True, exist_ok=True)
    while subprocess.call(["mountpoint", "-q", str(SMB_MOUNT)]) == 0:
        if subprocess.call(["umount", str(SMB_MOUNT)], stderr=subprocess.DEVNULL) != 0:
            subprocess.call(["umount", "-l", str(SMB_MOUNT)], stderr=subprocess.DEVNULL)
            break

    result = subprocess.run(
        ["timeout", "30", "mount", "-t", "cifs", smb_source, str(SMB_MOUNT),
         "-o", f"{mount_auth},ro,iocharset=utf8,soft,echo_interval=15"],
        capture_output=True
    )
    if result.returncode != 0:
        SMB_CREDENTIALS.unlink(missing_ok=True)
        # stderr is often empty when `timeout` kills a hung mount before it
        # can print anything — fall back to stdout and the exit code itself
        # so a bare "Failed to mount: " doesn't repeat with no clue why.
        detail = result.stderr.decode().strip() or result.stdout.decode().strip()
        detail = detail or f"exit code {result.returncode} (timed out or killed, no output)"
        return False, f"Failed to mount {smb_source}: {detail}"

    log(f"SMB mounted: {smb_source}")
    return True, None


def unmount_smb():
    subprocess.call(["umount", str(SMB_MOUNT)], stderr=subprocess.DEVNULL)
    SMB_CREDENTIALS.unlink(missing_ok=True)
    log("SMB unmounted")


def run_test():
    """--test: prove the source is mountable, then unmount without walking."""
    try:
        config = zerofi_config.load_config()
    except Exception as e:
        log(f"config read failed: {e}")
        sys.exit(1)

    if not config.get("smb_source", ""):
        log("no smb_source configured — skipping")
        sys.exit(0)

    ok, error = mount_smb(config)
    if not ok:
        log(error)
        sys.exit(1)
    unmount_smb()
    log("source is reachable")


def main():
    try:
        config = zerofi_config.load_config()
    except Exception as e:
        log(f"config read failed: {e}")
        sys.exit(1)

    if not config.get("sync_enabled"):
        log("sync not enabled — exiting")
        sys.exit(0)

    if not config.get("smb_source", ""):
        log("no smb_source configured — exiting")
        sys.exit(0)

    if not has_lan():
        log("no home LAN — exiting")
        sys.exit(0)

    ok, error = mount_smb(config)
    if not ok:
        log(error or "SMB mount failed")
        sys.exit(1)

    count = 0
    try:
        # Walk the source tree; any directory directly containing audio files
        # is an album directory — create a qualify job for it.
        for root, dirs, files in os.walk(SMB_MOUNT):
            dirs.sort()  # consistent ordering
            if any(is_audio(f) for f in files):
                rel = str(Path(root).relative_to(SMB_MOUNT))
                enqueue_qualify(rel)
                count += 1
    finally:
        unmount_smb()

    log(f"discovery complete: {count} album(s) queued")

    # Kick the worker to start processing immediately
    subprocess.call(
        ["systemctl", "start", "zerofi-sync-worker.service"],
        stderr=subprocess.DEVNULL
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        run_test()
    else:
        main()
