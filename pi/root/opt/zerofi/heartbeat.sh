#!/bin/bash
# Zero-Fi — lightweight periodic housekeeping (zerofi-heartbeat.timer, every 5 min).
# Only /proc reads, df, systemctl queries — no heavy I/O, no library walks.

set -uo pipefail

MUSIC_DIR=/mnt/music
SMB_MOUNT=/mnt/smb-source
LOCK_FILE=/run/zerofi/sync.lock

# Single source of truth is Flask's _wlan0_has_lan() (app.py) — any failure
# to reach it (Flask down, timeout) is treated as "no LAN", the safe
# default. Every "does this device have a real home-network address" check
# in this script goes through here rather than re-deriving the
# link-local/AP-subnet exclusion rules in Bash — see _wlan0_has_lan()'s own
# docstring for the history of that drifting once already.
_has_lan() {
    curl -s -m 5 http://127.0.0.1/api/internal/wlan0-has-lan 2>/dev/null | \
        python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("has_lan") else 1)' 2>/dev/null
}

echo "=== Zero-Fi heartbeat ==="

# --- Stale SMB mount ---
# gate on lock, not just mounted — a live sync also has it mounted;
# unmounting under a live rsync is harmful
mkdir -p "$(dirname "$LOCK_FILE")"
exec 8>"$LOCK_FILE"
if mountpoint -q "$SMB_MOUNT"; then
    if flock -n 8; then
        echo "  Stale SMB mount at $SMB_MOUNT (no sync running) — unmounting."
        umount "$SMB_MOUNT" 2>/dev/null || umount -l "$SMB_MOUNT" 2>/dev/null || \
            echo "  WARNING: could not unmount $SMB_MOUNT even with -l"
        flock -u 8
    fi
fi

# --- WiFi power save ---
# re-assert alongside zerofi-wifi-powersave.service: the setting is
# per-interface and can reset on re-association
# tr to lowercase: iw prints "Power save: on" with capital P
if [[ "$(iw dev wlan0 get power_save 2>/dev/null | tr 'A-Z' 'a-z')" == *"power save: on"* ]]; then
    echo "  WARNING: WiFi power save came back on (interface re-associated?) — disabling again."
    iw dev wlan0 set power_save off 2>/dev/null || \
        echo "  WARNING: failed to disable WiFi power save"
fi

# --- Memory ---
# MemAvailable (not MemFree) — accounts for reclaimable cache/buffers
MEM_AVAIL_KB=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
MEM_TOTAL_KB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
if [[ -n "$MEM_AVAIL_KB" && -n "$MEM_TOTAL_KB" && "$MEM_TOTAL_KB" -gt 0 ]]; then
    MEM_AVAIL_PCT=$(( MEM_AVAIL_KB * 100 / MEM_TOTAL_KB ))
    if (( MEM_AVAIL_PCT < 10 )); then
        echo "  WARNING: low memory — ${MEM_AVAIL_PCT}% available (${MEM_AVAIL_KB} KB of ${MEM_TOTAL_KB} KB)"
    fi
fi

# --- Disk ---
for CHECK_MOUNT in / "$MUSIC_DIR"; do
    USE_PCT=$(df --output=pcent "$CHECK_MOUNT" 2>/dev/null | tail -1 | tr -dc '0-9')
    if [[ -n "$USE_PCT" ]] && (( USE_PCT >= 90 )); then
        echo "  WARNING: $CHECK_MOUNT is ${USE_PCT}% full"
    fi
done

# --- Load average ---
# single core — sustained load above 1 means something is queuing/thrashing
LOAD1=$(awk '{print $1}' /proc/loadavg)
if [[ -n "$LOAD1" ]] && awk -v l="$LOAD1" 'BEGIN{exit !(l > 2)}'; then
    echo "  WARNING: load average $LOAD1 (1 CPU core)"
fi

# --- D-state hang detection ---
# D state alone means nothing on this hardware: a plain I/O-bound writer to
# the music partition sits in D for ~94% of samples. Reboot only when the same
# process instance (pid + start time) shows byte-for-byte identical io counters
# and CPU time across DSTATE_STRIKES consecutive checks (~10 min of total
# inactivity). Any counter movement resets strikes to 1.
DSTATE_PREV_FILE=/run/zerofi/heartbeat-dstate-prev
CUR_STATE=""
DSTATE_DESC=""
STUCK_KEYS=""
PREV_STATE=""
[[ -f "$DSTATE_PREV_FILE" ]] && PREV_STATE=$(cat "$DSTATE_PREV_FILE")
DSTATE_STRIKES=3

while read -r pid; do
    [[ -z "$pid" ]] && continue
    # comm field is parenthesized and can contain spaces/parens — anchor on
    # last ')' so positional counting doesn't misalign; starttime=field 22
    stat_rest=$(sed -E 's/^[0-9]+ \(.*\) //' "/proc/$pid/stat" 2>/dev/null)
    [[ -z "$stat_rest" ]] && continue
    read -r starttime cputime <<< "$(awk '{print $20, $12 + $13}' <<< "$stat_rest")"
    [[ -z "$starttime" ]] && continue
    comm=$(cat "/proc/$pid/comm" 2>/dev/null)
    # rchar/wchar/syscr/syscw: frozen for a real hang, ticking for anything
    # merely slow; missing /proc/PID/io means process exited → fails safe
    io=$(awk '/^(rchar|wchar|syscr|syscw):/{printf "%s,", $2}' "/proc/$pid/io" 2>/dev/null)
    key="$pid:$starttime"
    sig="$io$cputime"

    strikes=1
    prev_line=$(grep -F "$key	" <<< "$PREV_STATE" | head -1)
    if [[ -n "$prev_line" ]]; then
        prev_sig=$(cut -f2 <<< "$prev_line")
        prev_strikes=$(cut -f3 <<< "$prev_line")
        if [[ "$prev_sig" == "$sig" ]]; then
            strikes=$(( prev_strikes + 1 ))
        fi
    fi

    CUR_STATE+="$key	$sig	$strikes"$'\n'
    DSTATE_DESC+="$pid($comm, strike $strikes/$DSTATE_STRIKES) "
    (( strikes >= DSTATE_STRIKES )) && STUCK_KEYS+="$key($comm) "
done < <(ps -eo pid,stat --no-headers | awk '$2 ~ /^D/ {print $1}')

if [[ -n "$DSTATE_DESC" ]]; then
    echo "  Note: process(es) in uninterruptible sleep (D state): $DSTATE_DESC"
fi

if [[ -n "$CUR_STATE" ]]; then
    printf '%s' "$CUR_STATE" > "$DSTATE_PREV_FILE"
else
    rm -f "$DSTATE_PREV_FILE"
fi

if [[ -n "$STUCK_KEYS" ]]; then
    echo "  CRITICAL: process(es) in D state with zero syscall/CPU progress across $DSTATE_STRIKES consecutive checks ($STUCK_KEYS) — recovering via reboot."

    # Dump forensics to root fs before rebooting — they survive the reset.
    # No stat() of $MUSIC_DIR or $SMB_MOUNT anywhere: statting a wedged CIFS
    # mount blocks on the very thing being recovered from.
    FORENSICS_DIR=/var/lib/zerofi/hang-forensics
    mkdir -p "$FORENSICS_DIR"
    FORENSICS_FILE="$FORENSICS_DIR/$(date +%Y%m%d-%H%M%S).txt"
    {
        echo "=== Zero-Fi hang forensics — $(date -Is) ==="
        echo "stuck: $STUCK_KEYS"
        echo "uptime: $(cat /proc/uptime)"
        echo "loadavg: $(cat /proc/loadavg)"
        echo
        echo "=== memory ==="
        grep -E '^(MemTotal|MemAvailable|MemFree|Dirty|Writeback):' /proc/meminfo
        echo
        for entry in $STUCK_KEYS; do
            spid=${entry%%:*}
            echo "=== pid $spid ==="
            echo "comm:    $(timeout 5 cat /proc/$spid/comm 2>/dev/null)"
            echo "cmdline: $(timeout 5 tr '\0' ' ' < /proc/$spid/cmdline 2>/dev/null)"
            echo "wchan:   $(timeout 5 cat /proc/$spid/wchan 2>/dev/null)"
            echo "--- stack ---"
            timeout 5 cat "/proc/$spid/stack" 2>/dev/null || echo "(unavailable)"
            echo "--- io ---"
            timeout 5 cat "/proc/$spid/io" 2>/dev/null
            echo "--- status ---"
            timeout 5 grep -E '^(State|Threads|voluntary|nonvoluntary)' "/proc/$spid/status" 2>/dev/null
            echo
        done
        echo "=== all D-state processes ==="
        timeout 10 ps -eo pid,stat,wchan:24,etime,args --no-headers 2>/dev/null | awk '$2 ~ /^D/'
        echo
        echo "=== mounts (from /proc/self/mounts — no stat()) ==="
        grep -E 'cifs|exfat|/mnt/' /proc/self/mounts
        echo
        echo "=== dmesg tail ==="
        timeout 10 dmesg 2>/dev/null | tail -80
    } > "$FORENSICS_FILE" 2>&1
    sync
    echo "  Forensics written to $FORENSICS_FILE"

    # keep the 10 most recent incidents
    ls -1t "$FORENSICS_DIR"/*.txt 2>/dev/null | tail -n +11 | xargs -r rm -f

    # cap at 3 reboots per rolling hour — a recurring fault shouldn't
    # produce an unbounded reboot loop
    REBOOT_LOG=/var/lib/zerofi/heartbeat-reboots
    mkdir -p "$(dirname "$REBOOT_LOG")"
    touch "$REBOOT_LOG"
    NOW=$(date +%s)
    RECENT=$(awk -v now="$NOW" '$1 > now-3600' "$REBOOT_LOG")
    RECENT_COUNT=0
    [[ -n "$RECENT" ]] && RECENT_COUNT=$(grep -c . <<< "$RECENT")

    if (( RECENT_COUNT < 3 )); then
        { [[ -n "$RECENT" ]] && printf '%s\n' "$RECENT"; echo "$NOW"; } > "$REBOOT_LOG.tmp" && mv "$REBOOT_LOG.tmp" "$REBOOT_LOG"
        # a D-state process can block ordinary shutdown too — SysRq forces
        # an immediate reboot if the clean path doesn't complete in 30s
        ( sleep 30; echo 1 > /proc/sys/kernel/sysrq 2>/dev/null; echo b > /proc/sysrq-trigger 2>/dev/null ) & disown
        echo "  Rebooting now (SysRq fallback armed for 30s)."
        systemctl reboot
    else
        echo "  CRITICAL: already rebooted $RECENT_COUNT time(s) in the last hour — NOT rebooting again. Needs hands-on investigation."
    fi
fi

# --- Crash-looping units ---
# NRestarts check: a unit mid-restart-loop isn't in --failed state
CRASHY_UNITS=(hostapd dnsmasq mpd mympd zerofi-flask zerofi-ap
    zerofi-bt-audio zerofi-bt-agent shairport-sync)
for UNIT in "${CRASHY_UNITS[@]}"; do
    NRESTARTS=$(systemctl show "$UNIT.service" -p NRestarts --value 2>/dev/null)
    if [[ -n "$NRESTARTS" ]] && (( NRESTARTS > 20 )); then
        echo "  WARNING: $UNIT.service has restarted $NRESTARTS times — likely crash-looping"
    fi
done

# pipewire/wireplumber are user units — -M pipewire@ queries that user's
# systemd instance from root
for UNIT in pipewire wireplumber; do
    NRESTARTS=$(systemctl --user -M pipewire@ show "$UNIT.service" -p NRestarts --value 2>/dev/null)
    if [[ -n "$NRESTARTS" ]] && (( NRESTARTS > 20 )); then
        echo "  WARNING: $UNIT.service (pipewire user session) has restarted $NRESTARTS times — likely crash-looping"
    fi
done

FAILED=$(systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | tr '\n' ' ')
if [[ -n "${FAILED// /}" ]]; then
    echo "  WARNING: failed systemd unit(s): $FAILED"
fi

# --- mpd's Unix socket ---
# mpd.socket can become inactive while mpd.service keeps running (overlapping
# restarts trigger "already active, refusing"). Only myMPD (which uses the
# Unix socket) breaks; mpd's TCP port stays up. Fix: stop both before restarting.
if ! mpc -h /run/mpd/socket status >/dev/null 2>&1; then
    echo "  WARNING: mpd Unix socket not responding — restarting mpd to restore it"
    systemctl stop mpd.service mpd.socket >/dev/null 2>&1 || true
    systemctl start mpd.socket >/dev/null 2>&1 || true
fi

# --- Config-vs-service drift enforcement ---
# Flask preflight is authoritative on boot; heartbeat backstops ongoing drift.
# Reads config directly — no Flask involvement. Bidirectional: starts services
# that should be running and stops services that should not be.
ZEROFI_CONFIG=/mnt/music/zerofi.json
if [[ -f "$ZEROFI_CONFIG" ]]; then
    read -r _AP_EN _WIFI_EN _SSH_EN _AIRPLAY_EN < <(
        python3 -c "
import json, sys
try:
    c = json.load(open('$ZEROFI_CONFIG'))
    print(
        str(c.get('ap_enabled', True)).lower(),
        str(c.get('wifi_client_enabled', False)).lower(),
        str(c.get('ssh_enabled', False)).lower(),
        str(c.get('airplay_enabled', False)).lower(),
    )
except Exception:
    sys.exit(1)
" 2>/dev/null
    ) || _AP_EN=""

    if [[ -n "$_AP_EN" ]]; then
        # AP: never kill it if it's the only access path — leaves it up an
        # extra cycle rather than risk dropping the only access path.
        if [[ "$_AP_EN" == "false" ]]; then
            if _has_lan; then
                for _SVC in hostapd zerofi-ap.service; do
                    if systemctl is-active --quiet "$_SVC" 2>/dev/null; then
                        echo "  WARNING: $_SVC running but ap_enabled=false — stopping."
                        systemctl stop "$_SVC" 2>/dev/null || true
                    fi
                done
            else
                echo "  Note: ap_enabled=false but no LAN address yet — keeping AP up as only access path."
            fi
        elif ! systemctl is-active --quiet zerofi-ap.service 2>/dev/null; then
            echo "  WARNING: zerofi-ap.service inactive but ap_enabled=true — starting."
            systemctl start zerofi-ap.service 2>/dev/null || true
        fi

        # WiFi client
        if [[ "$_WIFI_EN" == "true" ]]; then
            if ! systemctl is-active --quiet wpa_supplicant@wlan0.service 2>/dev/null; then
                echo "  WARNING: wpa_supplicant@wlan0 inactive but wifi_client_enabled=true — restarting."
                systemctl restart wpa_supplicant@wlan0.service 2>/dev/null || true
            fi
        elif systemctl is-active --quiet wpa_supplicant@wlan0.service 2>/dev/null; then
            echo "  WARNING: wpa_supplicant@wlan0 active but wifi_client_enabled=false — stopping."
            systemctl disable --now wpa_supplicant@wlan0.service 2>/dev/null || true
        fi

        # SSH (Dropbear)
        if [[ "$_SSH_EN" == "true" ]]; then
            if ! systemctl is-active --quiet dropbear.service 2>/dev/null; then
                echo "  WARNING: dropbear inactive but ssh_enabled=true — starting."
                systemctl start dropbear.service 2>/dev/null || true
            fi
        elif systemctl is-active --quiet dropbear.service 2>/dev/null; then
            echo "  WARNING: dropbear active but ssh_enabled=false — stopping."
            systemctl disable --now dropbear.service 2>/dev/null || true
        fi

        # AirPlay
        if [[ "$_AIRPLAY_EN" == "true" ]]; then
            if ! systemctl is-active --quiet shairport-sync.service 2>/dev/null; then
                echo "  WARNING: shairport-sync inactive but airplay_enabled=true — starting."
                systemctl start shairport-sync.service 2>/dev/null || true
            fi
        elif systemctl is-active --quiet shairport-sync.service 2>/dev/null; then
            echo "  WARNING: shairport-sync active but airplay_enabled=false — stopping."
            systemctl disable --now shairport-sync.service 2>/dev/null || true
        fi
    fi
fi

# --- myMPD settings reconciliation ---
# MYMPD_API_SETTINGS_SET is a full-replace, not a merge — confirmed live:
# saving just the startpage through myMPD's own Settings dialog wiped
# bgColor/theme/highlightColor (and can just as easily drop endlessScroll,
# startupView, or anything else) since that save's payload didn't carry
# them. Rather than detecting and patching each field myMPD's UI happens to
# clobber, just re-run the reconciliation every cycle — it's cheap (no
# mympd restart, just a couple of small JSON-RPC calls) and idempotent
# (existing myMPD settings are read and merged, not blindly overwritten).
if [[ -f "$ZEROFI_CONFIG" ]]; then
    curl -s -m 10 -X POST http://127.0.0.1/api/internal/mympd-resync >/dev/null 2>&1 || true
fi

# --- Sync worker kick ---
SYNC_QUEUE=/run/zerofi/sync-queue
if _has_lan; then
    if find "$SYNC_QUEUE" -name "*.json" -maxdepth 2 -print -quit 2>/dev/null | grep -q .; then
        echo "  Starting sync worker (queue has jobs)."
        systemctl start zerofi-sync-worker.service 2>/dev/null || true
    fi
fi

echo "=== Zero-Fi heartbeat complete ==="
