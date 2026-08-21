# Zero-Fi — Agent / AI assistant context

## What this is

Zero-Fi turns a Raspberry Pi Zero W into a standalone jukebox. It ships as a
flashable SD image: DietPi base + PipeWire/WirePlumber + myMPD + a Flask web UI.
No package installs on first boot — everything is pre-baked.

## Repo layout

```
build/              image builder (build-image.sh) and flash helper (flash-sd.sh)
flask_app/app.py    the entire Flask backend — all routes, system calls,
                    config persistence, palette engine, BT/PipeWire management,
                    sync coordination
templates/          Jinja2 templates
  settings.html     admin settings page — all card UIs, card controller JS
  toolbar.html      player toolbar (myMPD iframe + output picker + clock)
static/             CSS, SVGs, fonts
pi/boot/            dietpi.txt — DietPi first-run config baked into image
pi/root/            files overlaid onto the rootfs verbatim at build time
  opt/zerofi/
    sync-worker.py    processes bounded 30-min cycles of the job queue
    sync-discover.py  walks SMB source, enqueues qualify jobs; runs daily + on demand.
                      also the SMB reachability check (--test, from settings UI)
    heartbeat.sh      every 5 min: watchdog, DAC restore, sync re-drive, drift checks
    extract-covers.py cover art extraction (called by sync-worker per album dir)
    expand-music.sh   first-boot: grows the music partition to fill the card
    fsck-music.sh     boot-time exFAT integrity check, runs before mnt-music.mount
    bluetooth-agent.sh  bluetoothd agent: auto-trust in target mode, reject in source mode
    bluetooth-audio-router.sh  routes incoming BT audio (target mode) to the selected PipeWire output
    airplay-session-start.sh / -end.sh  shairport-sync hooks: pause/resume mpd around AirPlay
    select-ap-channel.sh  picks wlan0_ap's channel to match wlan0 (concurrent AP+STA needs same channel)
    wifi-ap-fallback.sh  wpa_cli event hook: drops to AP mode when the WiFi client connection is lost
docs/index.html     single-page project site (also served at /help)
```

**Systemd units** are written as heredocs inside `build/build-image.sh` — look for
`cat > "$ROOT_MOUNT/etc/systemd/system/..."` blocks. Key units:
- `zerofi-flask.service` — the Flask app on port 80
- `zerofi-heartbeat.timer` — fires every 5 minutes, runs `heartbeat.sh`
- `zerofi-sync-discover.timer` — daily at midnight (`OnCalendar=daily`), runs `sync-discover.py`
- `zerofi-sync-worker.service` — one-shot; started by discover and by heartbeat
- `zerofi-sync-discover.service`, `zerofi-ap.service`, `zerofi-bt-audio.service`, `zerofi-bt-agent.service`
- `zerofi-fsck-music.service` — runs `fsck-music.sh` before `mnt-music.mount`
- `zerofi-wifi-watch.service` — runs `wpa_cli -a` to drive `wifi-ap-fallback.sh` on connection events
- `zerofi-wifi-powersave.service` — one-shot: disables wlan0 power save at boot (brcmfmac SDIO stall mitigation); heartbeat re-asserts it

## Key design facts

- **Config**: `/mnt/music/zerofi.json` on the Pi — lives on the exFAT music
  partition (3rd SD partition, `/dev/mmcblk0p3`). Its absence means first-boot
  (factory-reset-by-deleting). User-editable by mounting the partition on any
  computer. Writes are atomic (tmp + rename) — exFAT has no journal.
- **myMPD**: runs on port 8080; the Flask toolbar (port 80) embeds it in an
  iframe. Flask calls the myMPD JSON API at `127.0.0.1:8080` internally for
  palette management. Palette CSS is written to
  `/var/lib/private/mympd/config/custom_css` — myMPD caches it in memory, so
  changes require `systemctl restart mympd`. The palette apply path already does
  this.
- **AP subnet**: `192.168.4.0/24` — fixed. Admin routes (`@lan_only`) are blocked
  from AP clients once a real home LAN address exists on wlan0.
- **Two UI surfaces**: `/toolbar` (player, public — visible from AP) and
  `/settings` (admin, LAN-only once WiFi is joined).
- **PipeWire**: runs as a dedicated `pipewire` system user's `--user` session.
  Access from root via `XDG_RUNTIME_DIR=/run/user/<pipewire_uid>` (see `_PW_ENV`
  in app.py). Use `pw-metadata` to set the default sink — `wpctl set-default`
  doesn't work on this headless session.
- **Bluetooth modes**: `source` = Pi connects out to a BT speaker; `target` = Pi
  is the speaker (phones stream to it via A2DP). `bluetooth-agent.sh` enforces
  auto-trust in target mode and rejection in source mode.
- **`_DEV` mode**: `ZEROFI_DEV=1` (via `dev.sh`) runs Flask without Pi hardware.
  System-call paths are no-ops; config stored in `~/.config/zerofi-dev/`.
- **Build cache**: DietPi base image and rootfs tarballs in `.cache/` (gitignored).

## Testing — hardware required

This project runs on specific Pi Zero W hardware with a custom image. There is
no useful software-only test path for most features (PipeWire, BT, SMB sync,
partition management). Check your memory for a live test device on the network.
If one exists, deploy changed files directly rather than waiting for a full
rebuild:
- `bash build/deploy-dev.sh [user@host]` — rsyncs flask_app/, templates/,
  static/, and pi/root/opt/zerofi/ in one shot, fixes +x on any script with a
  shebang (local checkouts aren't executable), and restarts zerofi-flask.
  Defaults to `root@zero-fi.local` if no host is given.
- Heartbeat/sync-worker/sync-discover pick up their changes on their next
  run automatically — no restart needed. To force one sooner: `systemctl
  start zerofi-heartbeat` (or wait for its 5-min timer).
- SSH to the device requires `dangerouslyDisableSandbox: true` (sandbox blocks
  outbound SSH even to allowlisted hosts in practice)

Full rebuild: `sudo bash build/build-image.sh` — takes 20–30 min. Only needed
for changes to the DietPi base, package set, or systemd unit definitions.

### First-boot testing: two scenarios, two different addresses

Before checking reachability on a first-boot device, determine which of these
is actually being tested — they are not interchangeable and checking the
wrong address wastes a round trip:

- **Config pre-seeded** (`build/zerofi.json` placed before `flash-sd.sh` ran)
  — the device may join home WiFi per that config.  If this file is present, assume it is live on the test device as well.
- **Blank / truly from-scratch flash** (no pre-seeded config) — the device
  stays on **AP-only indefinitely**. The only reachable address is the AP itself: **`192.168.4.1`**.

If it's not obvious from context which scenario is live, ask before checking
either address. When genuinely unsure, check `192.168.4.1` first.
The AP's own SSID is `Zero-Fi-XXXX` unless a pre-seeded config set a custom `instance_name`.

## Sync pipeline

The Pi Zero W's `brcmfmac` WiFi chip is fundamentally unstable under sustained
SDIO load (syncing over the network to a slow SD card is exactly that load) —
it can wedge for minutes with no software fix. The heartbeat's D-state watchdog
detects this and reboots to clear it. The whole sync pipeline is designed
around that reboot being routine and fully recoverable, not a failure: the
goal is a box that's always there and playable when you go to use it, that
quietly finishes syncing on its own time regardless of how many times the
radio needs to be kicked along the way.

```
zerofi-sync-discover (daily timer + OnBootSec=3min + on-demand)
  → mounts SMB, walks dirs, writes qualify jobs to /run/zerofi/sync-queue/qualify/
  → unmounts, starts sync-worker

zerofi-sync-worker (one-shot, 30-min cycle limit)
  → qualify → compare (ffprobe sig diff) → sync (copy to .part, fsync, rename) → prune
  → heartbeat re-starts worker every 5 min while queue has jobs
  → stops if: music playing, WiFi lost, SMB unreachable, time limit, queue empty

Queue: /run/zerofi/sync-queue/{qualify,compare,sync,prune}/ (tmpfs — cleared on
reboot, deliberately not persisted to the SD card). Recovery is by
re-derivation, not by remembering: the boot-time discovery run re-walks the
source and re-qualifies everything, so a sync cut off by a watchdog reboot
just resumes a few minutes later. Per-file copies land at a `.part` sibling
and only get renamed to their real name once fully written and fsynced, so a
reboot mid-file leaves either a finished track or nothing at all — never a
truncated file that "missing locally" compare logic could mistake for done.
Any orphaned `.part` left by a reboot between copy and rename is swept at the
start of the next cycle.

No separate status file — the queue itself (job counts per phase) is the
only source of truth for sync progress; GET /api/library/status just counts
files in each queue dir.
```

## Build

```
sudo bash build/build-image.sh        # produces zerofi-<date>.img.xz
bash build/flash-sd.sh zerofi-*.img.xz /dev/sdX
```

Requires: `curl xz parted losetup qemu-arm-static binfmt_misc python3` and root.

## Dev run

```
bash dev.sh        # ZEROFI_DEV=1, config in ~/.config/zerofi-dev/
```

Set `MYMPD_URL` to point at a real myMPD instance if you want the player toolbar
to do anything.

## Baking files into the image

Files under `pi/root/` are overlaid onto the rootfs verbatim at build time —
the directory tree mirrors the Pi filesystem:

```
pi/root/opt/zerofi/foo.sh   →  /opt/zerofi/foo.sh  on the Pi
pi/root/etc/motd            →  /etc/motd
```

Files under `pi/boot/` go to the FAT boot partition:

```
pi/boot/dietpi.txt          →  /boot/dietpi.txt
```

Systemd units are heredocs in `build/build-image.sh`, not files in `pi/root/`.

**Pre-seeding a config**: place `build/zerofi.json` (gitignored) before running
`flash-sd.sh` — the flash script copies it to the music partition so the Pi
boots already configured. The image itself contains no config; its absence is
what triggers first-boot setup. Schema: see `load_config()` defaults in
`flask_app/app.py`.

**SSH keys**: place an `authorized_keys` file at
`pi/root/root/.ssh/authorized_keys` (gitignored path) before building.
The builder copies it in and enables SSH in the baked config automatically.

## Coding conventions

- Flask backend is a single file (`flask_app/app.py`). Keep it that way.
- No ORM — config is JSON, system state is read from the filesystem.
- Palette CSS is generated server-side in `_mympd_palette_css()` and injected as
  a `<style>` block. The `--c3-rgb` token enables `rgba()` tinting in CSS.
  `c3` is the hover/accent color throughout — use `rgba(var(--c3-rgb), 0.18–0.22)`
  for new hover states.
- Don't add comments explaining *what* code does — only *why* when non-obvious.
- This is a shared project, NEVER commit details of the local user or network to any tracked file.  If you are doing active development and need a place to store local context, use user-scope locations not project-scope for anything identifiable.

## Releases

Releases consist of three components, all managed via GitHub's "Releases" functionality:

1. A Release tag (e.g. v1.0.0) of the SHA that maps to the code from which the release was built.
2. A GitHub release page, with a change log since the last release.
3. A binary version of the image built from the tagged SHA, compressed, with an accurate SHA256SUM of the file included in the release page for verification.

Once released, a release can never be altered, only deleted if necessary for some reason.  This is not due to technical limitations, but is a policy of this repo.