#!/usr/bin/env python3
# Route incoming Bluetooth audio (Target mode — a phone streaming TO the Pi)
# to whatever PipeWire output is currently selected. Polling-based (3s interval).

import json
import os
import subprocess
import sys
import time

POLL_INTERVAL = 3

# system unit — doesn't inherit XDG_RUNTIME_DIR from the pipewire user session
os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/997")


def log(msg):
    print(f"[bt-audio] {msg}", flush=True)


def pw_dump():
    try:
        result = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5)
        return json.loads(result.stdout)
    except Exception as e:
        log(f"pw-dump failed: {e}")
        return None


def default_sink_name(objects):
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Metadata":
            continue
        if obj.get("props", {}).get("metadata.name") != "default":
            continue
        for entry in obj.get("metadata") or []:
            if entry.get("key") == "default.audio.sink":
                return (entry.get("value") or {}).get("name")
    return None


def find_node(objects, name):
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        if (obj.get("info") or {}).get("props", {}).get("node.name") == name:
            return obj
    return None


def present_bluez_input(objects):
    # match by node presence, not state: bluez_input.* sits in `state: suspended`
    # until linked — waiting for "running" deadlocks (can't run until linked)
    found = []
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        info = obj.get("info") or {}
        name = info.get("props", {}).get("node.name", "")
        if name.startswith("bluez_input."):
            found.append(obj)
    if len(found) > 1:
        names = ", ".join((n.get("info") or {}).get("props", {}).get("node.name", "?") for n in found)
        log(f"WARNING: {len(found)} bluez inputs present simultaneously ({names}) — only routing the last one seen")
    return found[-1] if found else None


def node_ports(objects, node_id, direction):
    ports = []
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Port":
            continue
        props = (obj.get("info") or {}).get("props", {})
        if props.get("node.id") == node_id and props.get("port.direction") == direction:
            ports.append({"id": obj["id"], "channel": props.get("audio.channel", "")})
    return ports


def channel_pairs(src_ports, dst_ports):
    # match by audio.channel (FL/FR/...) where both sides have it — correct
    # regardless of port ordering; falls back to positional if channel info missing
    src_by_chan = {p["channel"]: p for p in src_ports if p["channel"]}
    dst_by_chan = {p["channel"]: p for p in dst_ports if p["channel"]}
    if src_by_chan and dst_by_chan:
        pairs = [(sp["id"], dst_by_chan[chan]["id"]) for chan, sp in src_by_chan.items() if chan in dst_by_chan]
        if pairs:
            return pairs
    return [(sp["id"], dp["id"]) for sp, dp in zip(src_ports, dst_ports)]


current_source_name = None


def teardown(reason):
    global current_source_name
    if current_source_name:
        log(f"{reason} — no longer routing {current_source_name}")
    current_source_name = None
    # no explicit pw-link -d needed: links disappear automatically when either
    # endpoint node is destroyed


while True:
    objects = pw_dump()
    if objects is None:
        time.sleep(POLL_INTERVAL)
        continue

    sink_name = default_sink_name(objects)

    # back off when BT is already in use as an output (Source mode)
    if sink_name and sink_name.startswith("bluez_output."):
        if current_source_name:
            teardown("default sink became a bluez_output (BT now in use as output)")
        time.sleep(POLL_INTERVAL)
        continue

    bluez_input = present_bluez_input(objects)

    if not bluez_input:
        if current_source_name:
            teardown("no bluez input present any more")
        time.sleep(POLL_INTERVAL)
        continue

    source_name = (bluez_input.get("info") or {}).get("props", {}).get("node.name")

    if source_name == current_source_name:
        time.sleep(POLL_INTERVAL)
        continue

    if not sink_name:
        time.sleep(POLL_INTERVAL)
        continue

    sink_node = find_node(objects, sink_name)
    if not sink_node:
        time.sleep(POLL_INTERVAL)
        continue

    src_ports = node_ports(objects, bluez_input["id"], "out")
    dst_ports = node_ports(objects, sink_node["id"], "in")
    if not src_ports or not dst_ports:
        log(f"'{source_name}' has no output ports yet or '{sink_name}' has no input ports — will retry")
        time.sleep(POLL_INTERVAL)
        continue

    log(f"source changed: '{current_source_name}' -> '{source_name}'")
    ok = True
    for src_id, dst_id in channel_pairs(src_ports, dst_ports):
        result = subprocess.run(["pw-link", str(src_id), str(dst_id)], capture_output=True, text=True)
        if result.returncode != 0:
            log(f"pw-link {src_id} -> {dst_id} failed: {result.stderr.strip()}")
            ok = False
    if ok:
        log(f"linked '{source_name}' -> '{sink_name}'")
        current_source_name = source_name
        # pause mpd — same "whoever's plugging in externally takes over" rule as AirPlay
        subprocess.run(["mpc", "pause"], capture_output=True)

    time.sleep(POLL_INTERVAL)
