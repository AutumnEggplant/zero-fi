#!/bin/bash
# Zero-Fi — persistent Bluetooth agent (both Source and Target modes).
#
# Handles the per-profile AuthorizeService step that bluetoothd requires before
# allowing A2DP/Headset access. Without an agent or Trusted flag, the device
# "pairs, then immediately disconnects" with "Access denied: org.bluez.Error.Rejected"
# in the bluetoothd log.
#
# Only Target mode authorizes inbound connections — Off must actually block
# them, not just skip pausing mpd for them (an untrusted phone, or a stray
# auto-reconnect, could otherwise land while Off and mix into playback with
# nothing gating or even detecting it, since the mode-transition cleanup in
# app.py's apply_bt_mode()/_restrict_bt_trust_to_sinks() only runs on a mode
# *change*, not continuously). Source and Off share the same reject path:
# app.py's apply_bt_mode() untrusts phones when entering either one, so only
# a trusted speaker reconnects silently in Source mode (bypasses
# AuthorizeService entirely). Any device that reaches AuthorizeService
# outside Target mode is either an untrusted former-phone or an unwanted
# connection attempt while Off — reject unconditionally, no per-device MAC
# check needed.

set -uo pipefail

FIFO=/run/zerofi/bt-agent.fifo
CONFIG_FILE=/mnt/music/zerofi.json
READY_FLAG=/run/zerofi/bt-agent-ready
AGENT_STATE_FILE=/run/zerofi/bt-agent-state
# cross-process lock shared with app.py's bt_locked() — threading.Lock is insufficient
# because this script and Flask are separate processes touching the same bluetoothd state
BT_LOCK_FILE=/run/zerofi/bt.lock
mkdir -p "$(dirname "$FIFO")"
rm -f "$FIFO" "$READY_FLAG" "$AGENT_STATE_FILE"
mkfifo "$FIFO"
touch "$AGENT_STATE_FILE" "$BT_LOCK_FILE"

# keep FIFO write end open so bluetoothctl (reading it as stdin) never sees
# EOF between commands — fd 9 is never read from, just held open
exec 9<>"$FIFO"

cleanup() { rm -f "$FIFO"; }
trap cleanup EXIT

# grep+tr not python3 — spawning a full interpreter (~tens of ms) inside the
# per-event loop creates a race where `trust <mac>` can still be in-flight
# when the immediately-following AuthorizeService prompt arrives
current_bt_mode() {
    local mode
    mode=$(grep -o '"bt_mode"[[:space:]]*:[[:space:]]*"[a-z]*"' "$CONFIG_FILE" 2>/dev/null | grep -o '"[a-z]*"$' | tr -d '"')
    echo "${mode:-target}"
}

# AUTH_FAIL_THRESHOLD=1: forgetting a device locally leaves the remote side
# with a stale key it will immediately retry — one fail is enough to diagnose
# an asymmetric bond; waiting for 3 means the user never gets a fourth attempt
declare -A auth_fail_count
AUTH_FAIL_THRESHOLD=1

# poll via one-shot `bluetoothctl show` before starting the persistent session —
# never run two bluetoothctl instances concurrently (the race bt_lock exists to
# prevent on the app.py side applies here too)
for _ in $(seq 1 30); do
    bluetoothctl show 2>/dev/null | grep -q "Powered: yes" && break
    sleep 1
done

bluetoothctl < "$FIFO" 2>&1 | while IFS= read -r line; do
    echo "[bt-agent] $line"
    case "$line" in
        *"Authorize service"*)
            if [[ "$(current_bt_mode)" == "target" ]]; then
                echo "yes" > "$FIFO"
            else
                echo "no" > "$FIFO"
            fi
            ;;
        *"Confirm passkey"*|*"[agent] Confirm"*|*"Authorize"*)
            echo "yes" > "$FIFO"
            ;;
        *"Paired: yes"*)
            # target mode only — auto-trust via a separate one-shot invocation,
            # not `> "$FIFO"`: writing `trust <mac>` into the same session that's
            # answering live AuthorizeService prompts races with the next prompt
            # arriving — the `yes` reply lands as "Invalid command in menu main: yes"
            # and bluetoothd drops the connection. Backgrounded so this loop can
            # keep answering prompts immediately; retried until confirmed.
            if [[ "$(current_bt_mode)" == "target" ]]; then
                mac=$(echo "$line" | grep -oE '([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}')
                if [[ -n "$mac" ]]; then
                    ( flock -w 15 202 || { echo "[bt-agent] could not acquire BT lock to trust $mac"; exit 1; }
                      for _ in 1 2 3; do
                          bluetoothctl trust "$mac" >/dev/null 2>&1
                          bluetoothctl info "$mac" 2>/dev/null | grep -q "Trusted: yes" && exit 0
                          sleep 1
                      done
                      echo "[bt-agent] could not confirm $mac trusted after 3 attempts" ) 202>"$BT_LOCK_FILE" &
                fi
            fi
            ;;
        *"[NEW] Transport"*)
            # Transport = A2DP/Headset profile actually connected (not just "Connected: yes"
            # which also fires before auth fails) — clearest signal to clear auth_fail_count
            mac=$(echo "$line" | grep -oE '([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}')
            [[ -n "$mac" ]] && unset "auth_fail_count[$mac]"
            # Nudge app.py to react (mpd pause, myMPD curtain) right now
            # instead of waiting for the toolbar's next /api/outputs poll
            # (up to 10s) to notice the same thing on its own.
            curl -s -m 3 -X POST http://127.0.0.1/api/internal/bt-source-changed >/dev/null 2>&1 &
            ;;
        *"Connected: no"*)
            # Same nudge on disconnect — clears the pause/curtain promptly
            # instead of leaving it up until the next poll.
            curl -s -m 3 -X POST http://127.0.0.1/api/internal/bt-source-changed >/dev/null 2>&1 &
            ;;
        *"auth failed with status"*)
            mac=$(echo "$line" | grep -oE '([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}')
            if [[ -n "$mac" ]]; then
                auth_fail_count[$mac]=$(( ${auth_fail_count[$mac]:-0} + 1 ))
                if (( auth_fail_count[$mac] >= AUTH_FAIL_THRESHOLD )); then
                    echo "[bt-agent] $mac failed auth ${auth_fail_count[$mac]}x — removing stale bond so the next attempt starts fresh"
                    # same one-shot + retry pattern as trust above
                    ( flock -w 15 203 || { echo "[bt-agent] could not acquire BT lock to remove $mac"; exit 1; }
                      for _ in 1 2 3; do
                          bluetoothctl remove "$mac" >/dev/null 2>&1
                          bluetoothctl devices 2>/dev/null | grep -q "$mac" || exit 0
                          sleep 1
                      done
                      echo "[bt-agent] could not confirm $mac removed after 3 attempts" ) 203>"$BT_LOCK_FILE" &
                    unset "auth_fail_count[$mac]"
                fi
            fi
            ;;
        *"Default agent request successful"*)
            touch "$READY_FLAG"
            ;;
        *"Agent unregistered"*|*"No agent is registered"*)
            echo "unregistered" >> "$AGENT_STATE_FILE"
            ;;
        *"Agent registered"*)
            echo "registered" >> "$AGENT_STATE_FILE"
            ;;
        *"Agent is already registered"*|*"Failed to register agent object"*)
            echo "reg_failed" >> "$AGENT_STATE_FILE"
            ;;
    esac
done &

# wait_for_state: `Default agent request successful` succeeds against any
# registered agent — can't trust it alone as confirmation that *our* agent
# specifically landed. Check the actual state transition.
wait_for_state() {
    local want="$1" timeout="${2:-3}"
    for _ in $(seq 1 $((timeout * 2))); do
        [[ "$(tail -n1 "$AGENT_STATE_FILE" 2>/dev/null)" == "$want" ]] && return 0
        sleep 0.5
    done
    return 1
}

# `agent off` first: bluetoothctl auto-registers a non-NoInputNoOutput agent on
# connect; must displace it before registering ours. Each step waits for the
# confirmed state transition. Retry up to 5x under BT_LOCK_FILE — this is a
# multi-step sequence that must run atomically against app.py's bt_locked().
for attempt in $(seq 1 5); do
    (
        flock -w 15 201 || { echo "[bt-agent] could not acquire BT lock for agent registration attempt $attempt"; exit 1; }
        rm -f "$READY_FLAG"
        : > "$AGENT_STATE_FILE"
        printf 'agent off\n' > "$FIFO"
        wait_for_state "unregistered" 3
        : > "$AGENT_STATE_FILE"
        printf 'agent NoInputNoOutput\n' > "$FIFO"
        if wait_for_state "registered" 3; then
            printf 'default-agent\n' > "$FIFO"
            for _ in $(seq 1 6); do
                [[ -f "$READY_FLAG" ]] && break
                sleep 0.5
            done
        fi
    ) 201>"$BT_LOCK_FILE"
    if [[ -f "$READY_FLAG" ]]; then
        break
    fi
    echo "[bt-agent] agent registration attempt $attempt didn't confirm — retrying"
    sleep 1
done

wait
