#!/bin/bash
# Zero-Fi — pick wlan0_ap's channel before hostapd starts.
#
# Concurrent AP+STA on a single radio only works if both interfaces share
# one channel — the AP can't pick independently once wlan0 is associated.
# So: if wlan0 is already connected to the home network, follow its
# channel. Otherwise (factory-fresh, no home WiFi configured yet) pick the
# least-congested of the three non-overlapping 2.4GHz channels from a scan.

set -uo pipefail

HOSTAPD_CONF=/etc/hostapd/hostapd.conf

pick_least_congested() {
    local c1=0 c6=0 c11=0 freq chan
    while read -r freq; do
        chan=$(( (freq - 2407) / 5 ))
        case "$chan" in
            1) ((c1++)) ;;
            6) ((c6++)) ;;
            11) ((c11++)) ;;
        esac
    done < <(iw dev wlan0 scan 2>/dev/null | awk '/freq:/ {print $2}')

    local best=6 best_count=$c6
    if (( c1 < best_count )); then best=1; best_count=$c1; fi
    if (( c11 < best_count )); then best=11; best_count=$c11; fi
    echo "$best"
}

# Give wlan0 a few seconds to finish associating (if it's going to at all)
# before deciding — avoids picking a scan-based channel that immediately
# conflicts once the STA link comes up moments later.
STA_CHANNEL=""
for _ in 1 2 3 4 5 6 7 8; do
    STA_CHANNEL=$(iw dev wlan0 info 2>/dev/null | awk '/channel/ {print $2; exit}')
    [[ -n "$STA_CHANNEL" ]] && break
    sleep 1
done

if [[ -n "$STA_CHANNEL" ]]; then
    CHANNEL="$STA_CHANNEL"
else
    CHANNEL=$(pick_least_congested)
fi

sed -i "s/^channel=.*/channel=$CHANNEL/" "$HOSTAPD_CONF"
