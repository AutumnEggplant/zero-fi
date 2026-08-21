#!/usr/bin/env python3
"""Zero-Fi config — the one place that reads or writes zerofi.json.

Every component that touches configuration goes through this module:
directly (`from zerofi_config import load_config`) if it's Python — the
Flask app, sync-discover.py, sync-worker.py — or via
`python3 /opt/zerofi/zerofi_config.py get <key>` if it's shell — heartbeat.sh,
wifi-ap-fallback.sh, bluetooth-agent.sh. flash-sd.sh (which runs on the build
host, not the Pi) uses `normalize-file <path>` to canonicalize a prefill file
before writing it to the card. One schema, one file shape, one place that
has to handle a malformed or old-format file — see the 2026-08-21 incident
where a downloaded /api/config/backup bundle was dropped in as the flash
prefill file: every independent parser (six of them, at the time) silently
treated its nested settings as absent and fell back to defaults.
"""

import json
import os
import sys
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("ZEROFI_CONFIG_DIR", "/mnt/music"))
CONFIG_FILE = CONFIG_DIR / "zerofi.json"
AUTHORIZED_KEYS_FILE = Path("/root/.ssh/authorized_keys")


def _default_instance_name():
    """A plain 'Zero-Fi' risks colliding with another device on the same
    network — both the WiFi SSID and the AirPlay name need to be unique to
    avoid confusing clients about which one they're talking to. Suffix with
    a few bytes of wlan0's MAC so the out-of-the-box default works."""
    try:
        mac = Path("/sys/class/net/wlan0/address").read_text().strip()
        suffix = mac.replace(":", "")[-4:].upper()
        return f"Zero-Fi-{suffix}"
    except Exception:
        return "Zero-Fi"


def _defaults():
    return {
        "instance_name": _default_instance_name(),
        "ap_password": "",
        "wifi_client_ssid": "",
        "wifi_client_password": "",
        "wifi_client_enabled": False,
        "paired_bt_mac": "",
        "paired_bt_name": "",
        "sync_enabled": False,
        "smb_source": "",
        "smb_username": "",
        "smb_password": "",
        "compare_enabled": False,
        "timezone": "America/New_York",
        "log_export": "",    # e.g. "192.168.1.50:514" — remote syslog (UDP), empty disables
        "ap_enabled": True,
        # off unless a key was pre-injected at build time — no SSH surface for
        # builders who never intended to use it
        "ssh_enabled": bool(AUTHORIZED_KEYS_FILE.exists() and AUTHORIZED_KEYS_FILE.read_text().strip()),
        "airplay_enabled": True,
        # "source" (connect out to a speaker) or "target" (be a speaker for a phone)
        "bt_mode": "off",
        "ntp_enabled": True,
        # c1/c4 are hand-copied into build-image.sh's baked myMPD home_list
        # icon (search for "bgcolor" there) for the pre-first-boot state,
        # before _push_mympd_webui_settings() ever runs — keep them in sync.
        "palette": {
            "bg": "#f4e8cc",
            "c1": "#5c2a0a",
            "c2": "#e8601a",
            "c3": "#f0c218",
            "c4": "#9ccce8",
        },
    }


def normalize(data):
    """Turn arbitrary parsed JSON into the one canonical flat config shape.

    Accepts either the flat shape directly, or a /api/config/backup bundle
    (its real settings live one level deeper, under "zerofi_config",
    alongside known_bt_devices/authorized_keys that don't belong in this
    schema at all) — unwrapping it here means every caller, read or write,
    gets the same tolerance for free instead of needing its own copy of
    this check.
    """
    if not isinstance(data, dict):
        data = {}
    if isinstance(data.get("zerofi_config"), dict):
        data = data["zerofi_config"]
    defaults = _defaults()
    if "wifi_client_enabled" not in data and data.get("wifi_client_ssid"):
        # upgrading a box that already joined a network — don't silently disconnect it
        data["wifi_client_enabled"] = True
    if "instance_name" not in data and data.get("ap_ssid"):
        data["instance_name"] = data["ap_ssid"]
    defaults.update({k: v for k, v in data.items() if k in defaults})
    return defaults


def load_config():
    if not CONFIG_FILE.exists():
        return _defaults()
    return normalize(json.loads(CONFIG_FILE.read_text()))


def save_config(cfg):
    """Always writes the canonical flat shape — normalize() runs on the way
    out too, not just the way in, so a bad shape can't be written even by
    a caller that built `cfg` incorrectly."""
    cfg = normalize(cfg)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps(cfg, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_FILE)


def _cli_get(key):
    val = load_config().get(key, "")
    if isinstance(val, bool):
        print("true" if val else "false")
    elif val is None:
        print("")
    else:
        print(val)


def _cli_normalize_file(path):
    """Read an arbitrary JSON file (flat config or backup bundle) and print
    its canonical flat form — used by flash-sd.sh to normalize a prefill
    file at flash time, on the build host, before it ever reaches the card."""
    data = json.loads(Path(path).read_text())
    print(json.dumps(normalize(data), indent=2))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "get":
        _cli_get(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == "normalize-file":
        _cli_normalize_file(sys.argv[2])
    else:
        print("usage: zerofi_config.py get <key> | normalize-file <path>", file=sys.stderr)
        sys.exit(1)
