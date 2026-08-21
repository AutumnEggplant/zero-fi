#!/usr/bin/env python3
"""Zero-Fi sync worker — processes one bounded cycle of the job queue.

Stop conditions (checked every BATCH_SIZE jobs):
  0    acquire exclusive sync lock (non-blocking, exit immediately if held)
  0.5  MPD is playing — stop
  1    WiFi client not connected — stop
  2    SMB source host unreachable (port 445) — stop
  3    cycle has been running > 30 minutes — stop
  4    dequeue in priority order: qualify → compare → sync → prune
  5    queue empty — stop

SMB is mounted once at the start of the cycle and held for the
duration; unmounted on exit (clean or otherwise).
"""

import fcntl
import itertools
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

MUSIC_DIR      = Path("/mnt/music")
SMB_MOUNT      = Path("/mnt/smb-source")
SMB_CREDENTIALS = Path("/run/zerofi/smb-credentials")
LOCK_FILE      = Path("/run/zerofi/sync.lock")
QUEUE_DIR      = Path("/run/zerofi/sync-queue")
MPC_UPDATE_FLAG = Path("/run/zerofi/mpc-update-desired")
CONFIG_FILE    = MUSIC_DIR / "zerofi.json"

QUALIFY_DIR = QUEUE_DIR / "qualify"
COMPARE_DIR = QUEUE_DIR / "compare"
SYNC_DIR    = QUEUE_DIR / "sync"
PRUNE_DIR   = QUEUE_DIR / "prune"

CHUNK_SIZE = 20   # max files coalesced into one copy pass (see _collect_sync_siblings)
BATCH_SIZE = 5    # jobs between preflight checks

_enqueue_seq = itertools.count()
AUDIO_EXTENSIONS = frozenset({
    ".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wv", ".ape"
})
CYCLE_LIMIT = 1800  # 30 minutes


_cycle_start = time.monotonic()
_smb_mounted = False


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
    print(f"[sync-worker] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception as e:
        log(f"config read failed: {e}")
        return {}


# ---------------------------------------------------------------------------
# Queue helpers
# ---------------------------------------------------------------------------

def enqueue(queue_dir, args, priority=50):
    """Write a job to queue_dir. Normal priority starts at slot 50; 0-49 reserved.

    _enqueue_seq resets to 0 every process start, but queued jobs survive
    across cycles (new process each cycle) — so a fresh cycle's numbering
    can collide with a still-pending job from a previous cycle. Slide to
    the next free seq in that case rather than overwrite it: we never
    modify in-flight jobs, and the exact filename doesn't matter as long
    as ordering within a priority tier is preserved.
    """
    queue_dir.mkdir(parents=True, exist_ok=True)
    seq = next(_enqueue_seq)
    path = queue_dir / f"{priority:04d}-{seq:010d}.json"
    while path.exists():
        seq = next(_enqueue_seq)
        path = queue_dir / f"{priority:04d}-{seq:010d}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"args": args}))
    tmp.rename(path)


def dequeue(queue_dir, sort_key=None):
    """Return next valid job dict or None.

    Deduplicates by args (removes all matching jobs including the chosen one,
    then returns its data). Skips and removes malformed files.
    """
    try:
        candidates = [f for f in queue_dir.iterdir() if f.suffix == ".json"]
    except OSError:
        return None
    candidates.sort(key=sort_key or (lambda f: f.name))

    for candidate in candidates:
        try:
            job = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log(f"malformed job {candidate.name}, skipping: {e}")
            candidate.unlink(missing_ok=True)
            continue

        # Sweep duplicates (including self) before returning
        try:
            for other in queue_dir.iterdir():
                if other.suffix != ".json":
                    continue
                try:
                    if json.loads(other.read_text()).get("args") == job.get("args"):
                        other.unlink()
                except (OSError, json.JSONDecodeError):
                    pass
        except OSError:
            pass

        return job

    return None


# ---------------------------------------------------------------------------
# SMB session
# ---------------------------------------------------------------------------

def ensure_smb_mounted(config):
    global _smb_mounted
    if _smb_mounted:
        return True

    smb_source = config.get("smb_source", "")
    if not smb_source:
        log("no smb_source in config")
        return False

    if smb_source.startswith("smb://"):
        smb_source = "//" + smb_source[6:]

    username = config.get("smb_username", "")
    password = config.get("smb_password", "")
    guest = not username and not password

    if guest:
        mount_auth = "guest"
    else:
        SMB_CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
        SMB_CREDENTIALS.write_text(f"username={username}\npassword={password}\n")
        os.chmod(SMB_CREDENTIALS, 0o600)
        mount_auth = f"credentials={SMB_CREDENTIALS}"

    SMB_MOUNT.mkdir(parents=True, exist_ok=True)
    # Clear stale mount (a prior run killed before its cleanup trap fired)
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
        log(f"SMB mount failed: {result.stderr.decode().strip()}")
        return False

    _smb_mounted = True
    log(f"SMB mounted: {smb_source}")
    return True


def unmount_smb():
    global _smb_mounted
    if not _smb_mounted:
        return
    subprocess.call(["umount", str(SMB_MOUNT)], stderr=subprocess.DEVNULL)
    _smb_mounted = False
    SMB_CREDENTIALS.unlink(missing_ok=True)
    log("SMB unmounted")


# ---------------------------------------------------------------------------
# File comparison
# ---------------------------------------------------------------------------

def is_audio(name):
    return Path(name).suffix.lower() in AUDIO_EXTENSIONS


def _probe_audio(path):
    """Return (duration, tags) from a single ffprobe header read (≤64 KB).

    Tags and duration live in container headers for all common formats —
    FLAC VORBIS_COMMENT blocks, MP3 ID3v2, OGG comment packets — so the
    64 KB probe limit covers them without touching audio data.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet",
             "-probesize", "65536", "-analyzeduration", "0",
             "-show_entries", "format=duration:format_tags",
             "-of", "json",
             str(path)],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout).get("format", {})
        raw_dur = data.get("duration")
        duration = round(float(raw_dur), 1) if raw_dur else None
        tags = data.get("tags") or None
        return duration, tags
    except Exception:
        return None, None


def file_sig(path):
    """Return a comparable dict for deciding whether to re-sync a file."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    duration, tags = _probe_audio(path)
    sig = {"size": st.st_size}
    if duration is not None:
        sig["duration"] = duration
    if tags:
        sig["tags"] = tags
    return sig


# ---------------------------------------------------------------------------
# Job handlers
# ---------------------------------------------------------------------------

def process_qualify(job, config):
    rel_dir = job["args"]["dir"]
    remote_dir = SMB_MOUNT / rel_dir
    local_dir  = MUSIC_DIR / rel_dir

    try:
        remote_files = sorted(
            f.name for f in remote_dir.iterdir()
            if f.is_file() and is_audio(f.name)
        )
    except OSError as e:
        log(f"qualify: can't read {rel_dir}: {e}")
        return

    if not remote_files:
        log(f"qualify: no audio in {rel_dir}")
        return

    enqueue(COMPARE_DIR, {"dir": rel_dir, "files": remote_files})

    # Detect prune candidates (local files absent from source)
    if config.get("prune_absent") and local_dir.exists():
        remote_set = set(remote_files)
        orphans = [
            str(local_dir / f.name)
            for f in local_dir.iterdir()
            if f.is_file() and is_audio(f.name) and f.name not in remote_set
        ]
        if orphans:
            enqueue(PRUNE_DIR, {"files": orphans, "parent_dir": str(local_dir)})

    log(f"qualify: {rel_dir}: {len(remote_files)} files → 1 compare job")


def process_compare(job, config):
    rel_dir = job["args"]["dir"]
    files   = job["args"]["files"]
    remote_dir = SMB_MOUNT / rel_dir
    local_dir  = MUSIC_DIR / rel_dir
    compare_enabled = config.get("compare_enabled", False)

    # New files always get pulled; changed-file detection only runs when
    # compare_enabled is on (it's the expensive ffprobe path — see
    # file_sig — off by default since content changes are rare).
    new_files = []
    updated_files = []
    for fname in files:
        local_path  = local_dir / fname
        remote_path = remote_dir / fname
        if not local_path.exists():
            log(f"compare: {rel_dir}/{fname}: missing locally")
            new_files.append(fname)
            continue
        if not compare_enabled:
            continue
        rsig = file_sig(remote_path)
        lsig = file_sig(local_path)
        if rsig is None or lsig is None:
            log(f"compare: {rel_dir}/{fname}: unreadable sig (r={rsig is not None} l={lsig is not None})")
            updated_files.append(fname)
        elif rsig != lsig:
            # Log the first field that actually differs
            for field in ("size", "duration", "tags"):
                rv, lv = rsig.get(field), lsig.get(field)
                if rv != lv:
                    log(f"compare: {rel_dir}/{fname}: {field} differs (remote={rv!r} local={lv!r})")
                    break
            updated_files.append(fname)
        else:
            log(f"compare: {rel_dir}/{fname}: up to date")

    # New files get a lower (higher-priority) slot than updates so a fresh
    # album a user is waiting on doesn't queue behind a backlog of re-checks.
    for fname in new_files:
        enqueue(SYNC_DIR, {"dir": rel_dir, "file": fname}, priority=40)
    for fname in updated_files:
        enqueue(SYNC_DIR, {"dir": rel_dir, "file": fname}, priority=50)

    total = len(new_files) + len(updated_files)
    log(f"compare: {rel_dir}: {total}/{len(files)} queued for sync "
        f"({len(new_files)} new, {len(updated_files)} updated)")


def _collect_sync_siblings(rel_dir):
    """Remove and return up to CHUNK_SIZE-1 additional sync jobs for rel_dir."""
    fnames = []
    try:
        candidates = sorted(
            (f for f in SYNC_DIR.iterdir() if f.suffix == ".json"),
            key=lambda f: f.name,
        )
    except OSError:
        return fnames
    for candidate in candidates:
        if len(fnames) >= CHUNK_SIZE - 1:
            break
        try:
            job = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError):
            candidate.unlink(missing_ok=True)
            continue
        if job.get("args", {}).get("dir") == rel_dir:
            fnames.append(job["args"]["file"])
            candidate.unlink(missing_ok=True)
    return fnames


def process_sync(job):
    import shutil
    rel_dir   = job["args"]["dir"]
    local_dir = MUSIC_DIR / rel_dir

    # Coalesce all queued sync jobs for this directory into one pass.
    fnames = [job["args"]["file"]] + _collect_sync_siblings(rel_dir)
    if len(fnames) > 1:
        log(f"sync: {rel_dir} — coalescing {len(fnames)} files")

    local_dir.mkdir(parents=True, exist_ok=True)
    for fname in fnames:
        src = SMB_MOUNT / rel_dir / fname
        dst = local_dir / fname
        # Copy to a .part sibling and rename into place only once it's
        # complete and fsynced. A crash mid-copy (the box can reboot itself
        # under sustained WiFi/SD contention — see hang-forensics) then
        # leaves either a finished file at dst or nothing at all, never a
        # truncated one — "missing locally" in process_compare is the only
        # signal that needs to stay true, instead of also detecting partial
        # writes after the fact.
        tmp = dst.with_name(dst.name + ".part")
        try:
            shutil.copy2(str(src), str(tmp))
            fd = os.open(str(tmp), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(str(tmp), str(dst))
            # Give the card a moment before the next file — mmcblk write
            # buffers can fill faster than the card drains them on a run of
            # many small files, with no per-file backpressure otherwise.
            time.sleep(3)
            log(f"sync: {rel_dir}/{fname}")
        except OSError as e:
            log(f"sync: copy failed {rel_dir}/{fname}: {e}")
            tmp.unlink(missing_ok=True)

    if not (local_dir / "cover.jpg").exists():
        subprocess.call(
            ["python3", "/opt/zerofi/extract-covers.py", str(local_dir)],
            stderr=subprocess.DEVNULL
        )

    MPC_UPDATE_FLAG.parent.mkdir(parents=True, exist_ok=True)
    MPC_UPDATE_FLAG.touch()


def process_prune(job):
    files      = job["args"]["files"]
    parent_dir = Path(job["args"]["parent_dir"])
    removed = []
    for f in files:
        try:
            Path(f).unlink()
            removed.append(f)
            log(f"prune: removed {f}")
        except OSError as e:
            log(f"prune: could not remove {f}: {e}")

    if removed:
        try:
            remaining = [x for x in parent_dir.iterdir() if is_audio(x.name)]
            if not remaining:
                parent_dir.rmdir()
                log(f"prune: removed empty dir {parent_dir}")
        except OSError:
            pass
        MPC_UPDATE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        MPC_UPDATE_FLAG.touch()


# ---------------------------------------------------------------------------
# Stop-condition helpers
# ---------------------------------------------------------------------------

def is_playing():
    try:
        out = subprocess.run(
            ["mpc", "status"], capture_output=True, text=True, timeout=5
        ).stdout
        return "[playing]" in out
    except Exception:
        return False


def smb_host_reachable(config):
    """TCP connect to the SMB host on port 445 with a short timeout."""
    import socket
    smb_source = config.get("smb_source", "")
    if not smb_source:
        return False
    # Accepts //host/share or smb://host/share
    host = smb_source.lstrip("smb:").lstrip("/").split("/")[0]
    if not host:
        return False
    try:
        with socket.create_connection((host, 445), timeout=3):
            return True
    except OSError:
        return False


def has_lan():
    """Single source of truth is Flask's _wlan0_has_lan() (app.py) — routed
    through the same internal endpoint heartbeat.sh uses, rather than
    re-deriving the link-local/AP-subnet exclusion rules here. A raw address
    check (any address at all on wlan0, including the AP subnet) is exactly
    the class of bug that check's docstring documents already happening once.
    Any failure to reach Flask (down, timeout) is treated as "no LAN".
    """
    try:
        req = urllib.request.Request("http://127.0.0.1/api/internal/wlan0-has-lan")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return bool(json.loads(resp.read()).get("has_lan"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main cycle loop
# ---------------------------------------------------------------------------

def queue_counts():
    """Return dict of phase → job count and a compact summary string."""
    counts = {}
    for name, d in [("qualify", QUALIFY_DIR), ("compare", COMPARE_DIR),
                    ("sync", SYNC_DIR), ("prune", PRUNE_DIR)]:
        try:
            counts[name] = sum(1 for f in d.iterdir() if f.suffix == ".json")
        except OSError:
            counts[name] = 0
    total = sum(counts.values())
    parts = [f"{n}:{v}" for n, v in counts.items() if v]
    summary = f"{total} jobs ({', '.join(parts)})" if parts else "0 jobs"
    return counts, summary


def main():
    # --- Step 0: acquire exclusive sync lock ---
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("gated: lock held by another cycle — exiting")
        lock_fd.close()
        sys.exit(0)

    jobs_done = 0
    stop_reason = None
    try:
        config = load_config()

        if not config.get("sync_enabled"):
            log("gated: sync not enabled — exiting")
            return

        if not has_lan():
            log("gated: no home LAN — exiting")
            return

        # Orphaned .part files only happen if a prior cycle died mid-copy —
        # the lock above guarantees no other cycle is writing one right now.
        for stale in MUSIC_DIR.rglob("*.part"):
            stale.unlink(missing_ok=True)
            log(f"cleanup: removed orphaned partial file {stale.relative_to(MUSIC_DIR)}")

        _, summary = queue_counts()
        log(f"starting cycle: {summary}")

        sort_by_slot = lambda f: int(f.stem.split("-")[0])

        def require_smb():
            if _smb_mounted:
                return True
            if not smb_host_reachable(config):
                log("gated: SMB source host unreachable")
                return False
            if not ensure_smb_mounted(config):
                log("gated: SMB mount failed")
                return False
            return True

        while True:
            # --- Preflight checks (every BATCH_SIZE jobs) ---
            if is_playing():
                stop_reason = "music playing"
                log("gated: music is playing — stopping")
                break
            if not has_lan():
                stop_reason = "WiFi lost"
                log("gated: WiFi client lost — stopping")
                break
            if time.monotonic() - _cycle_start > CYCLE_LIMIT:
                stop_reason = "time limit"
                log("gated: 30-minute cycle limit reached — stopping")
                break

            # --- Tight inner loop: BATCH_SIZE jobs without preflights ---
            for _ in range(BATCH_SIZE):
                if is_playing():
                    stop_reason = "music playing"
                    log("gated: music is playing — stopping")
                    break
                if job := dequeue(QUALIFY_DIR, sort_key=sort_by_slot):
                    if not require_smb(): break
                    process_qualify(job, config)
                elif job := dequeue(COMPARE_DIR, sort_key=sort_by_slot):
                    if not require_smb(): break
                    process_compare(job, config)
                elif job := dequeue(SYNC_DIR, sort_key=sort_by_slot):
                    if not require_smb(): break
                    process_sync(job)
                elif job := dequeue(PRUNE_DIR, sort_key=sort_by_slot):
                    process_prune(job)
                else:
                    stop_reason = "queue empty"
                    break
                jobs_done += 1
            else:
                continue  # full batch done — loop back to preflights
            break          # inner loop exited early — propagate out

    finally:
        unmount_smb()

        if MPC_UPDATE_FLAG.exists():
            try:
                # Goes through Flask's single serialized update helper (see
                # mpd_update_and_wait() in app.py) rather than running `mpc
                # update` here directly, so a rescan kicked off by this
                # worker can never race one kicked off by another caller
                # (boot, another cycle) and leave myMPD's own album/artist
                # cache rebuilt off a partial snapshot.
                req = urllib.request.Request(
                    "http://127.0.0.1/api/internal/mpd-update", data=b"",
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=200)
                MPC_UPDATE_FLAG.unlink(missing_ok=True)
                log("MPD library rescan triggered")
            except Exception as e:
                log(f"mpd update request failed: {e}")

        _, remaining = queue_counts()
        gate_msg = f" [{stop_reason}]" if stop_reason else ""
        log(f"cycle done: {jobs_done} job(s) processed{gate_msg}, {remaining}")
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
