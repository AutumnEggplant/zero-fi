#!/bin/bash
# Zero-Fi — WiFi client drop → AP fallback.
# Called by wpa_cli -a (zerofi-wifi-watch.service) on every wpa_supplicant
# state change. $1 = interface, $2 = event (CONNECTED/DISCONNECTED/etc.).
#
# The "driving away" case: user configured the AP off at home, then leaves.
# Home WiFi drops. Without this, the box becomes unreachable until they
# either get home or do a power-cycle. With it, the AP comes up within ~30s
# of losing the home network.
#
# Does NOT auto-disable the AP on reconnect — one-way safety net, not a toggle.

set -uo pipefail

IFACE="${1:-wlan0}"
EVENT="${2:-}"
CONFIG_FILE=/mnt/music/zerofi.json

[[ "$EVENT" != "DISCONNECTED" ]] && exit 0

# Grace period: transient drops during re-association shouldn't trigger this
sleep 30

# Still disconnected? Deliberately not app.py's _wlan0_has_lan() /
# /api/internal/wlan0-has-lan (see that docstring for the address-parsing
# history this class of check has drifted on before): this is a recovery
# script for exactly the scenarios where Flask might be unreachable or slow,
# so it can't depend on it. It also doesn't need the AP-subnet exclusion
# that check has — $IFACE here is always wlan0 (the client radio), never
# wlan0_ap (the AP's own interface), so a 192.168.4.x address can't appear
# on it by construction.
if ip -4 -o addr show "$IFACE" 2>/dev/null | \
        grep -qv '169\.254\.'; then
    exit 0  # reconnected during grace period
fi

wifi_client_enabled=$(python3 -c "
import json, sys
try:
    print('true' if json.load(open('$CONFIG_FILE')).get('wifi_client_enabled') else 'false')
except Exception:
    print('false')
" 2>/dev/null)
[[ "$wifi_client_enabled" != "true" ]] && exit 0

systemctl is-active --quiet zerofi-ap.service && exit 0

logger -t zerofi-wifi-watch "home WiFi gone after 30s grace — starting AP fallback"
systemctl start zerofi-ap.service
