#!/bin/bash
# shairport-sync before_play hook — pause mpd so AirPlay doesn't double-play
mpc pause >/dev/null 2>&1 || true
