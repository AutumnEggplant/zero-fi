#!/usr/bin/env bash
# Push local changes straight to a live Zero-Fi test device, without a full
# image rebuild. Previously this was a set of ad-hoc per-file `scp -O`
# commands the operator had to remember by hand — easy to forget a whole
# directory (a redrawn favicon.svg went undeployed for a full session because
# nothing covered static/ as a unit). One command, everything that matters.
set -euo pipefail

DEVICE="${1}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Deploying $REPO_DIR to $DEVICE"

rsync -az --exclude='__pycache__' "$REPO_DIR/flask_app/" "$DEVICE:/opt/zerofi/flask_app/"
rsync -az "$REPO_DIR/templates/" "$DEVICE:/opt/zerofi/templates/"
rsync -az "$REPO_DIR/static/" "$DEVICE:/opt/zerofi/static/"
rsync -az "$REPO_DIR/docs/" "$DEVICE:/opt/zerofi/docs/"
rsync -az --exclude='__pycache__' "$REPO_DIR/pi/root/opt/zerofi/" "$DEVICE:/opt/zerofi/"

# Local checkouts of the pi/root scripts aren't executable (git doesn't
# track that bit reliably for scp/rsync round-trips here — see the
# sync-worker.py permissions history), so fix it on the device the same way
# build-image.sh does: shebang presence, not filename, decides +x.
ssh "$DEVICE" bash -s <<'REMOTE'
set -e
find /opt/zerofi -maxdepth 1 -type f \
    -exec sh -c 'head -c2 "$1" | grep -q "^#!"' _ {} \; -print0 | \
    xargs -0 -r chmod +x
systemctl restart zerofi-flask
REMOTE

echo "==> Done. zerofi-flask restarted to pick up flask_app/templates/static changes."
echo "    sync-worker.py / sync-discover.py / heartbeat.sh pick up their changes on next run — no restart needed."
