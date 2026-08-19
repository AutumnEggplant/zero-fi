#!/bin/sh
# Local dev launcher — runs the Flask app without Pi-specific hardware.
# Set MYMPD_URL to point at a real myMPD instance if you want the iframe
# filled with something.
export ZEROFI_DEV=1
export ZEROFI_CONFIG_DIR="${ZEROFI_CONFIG_DIR:-$HOME/.config/zerofi-dev}"
export MYMPD_URL="${MYMPD_URL:-http://localhost:8080/}"
mkdir -p "$ZEROFI_CONFIG_DIR"
exec python3 flask_app/app.py
