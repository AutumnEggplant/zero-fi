"""Zero-Fi Flask Management App

Single-instance profile for Pi Zero W.
Music library path is configurable via config file.
"""

import fcntl
import ipaddress
import json
import os
import pwd
import re
import subprocess
import sys
import threading
import time
import zoneinfo
from datetime import datetime
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

# --- Config ---
# Lives on the music partition root — its *absence* is setup mode (is_configured()).
# Deleting it is a factory reset.
MUSIC_MOUNT = Path("/mnt/music")
_DEFAULT_CONFIG_DIR = MUSIC_MOUNT
CONFIG_DIR = Path(os.environ.get("ZEROFI_CONFIG_DIR", str(_DEFAULT_CONFIG_DIR)))
_DEV = bool(os.environ.get("ZEROFI_DEV"))

if not _DEV and CONFIG_DIR == _DEFAULT_CONFIG_DIR and not os.path.ismount(MUSIC_MOUNT):
    raise RuntimeError(f"{MUSIC_MOUNT} is not mounted — refusing to store config on the root filesystem instead")

CONFIG_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = CONFIG_DIR / "zerofi.json"
KNOWN_DEVICES_FILE = CONFIG_DIR / "known_bt_devices.json"
AUTHORIZED_KEYS_FILE = Path("/root/.ssh/authorized_keys")


def is_configured():
    return CONFIG_FILE.exists()

app = Flask(__name__,
            template_folder=str(Path(__file__).resolve().parent.parent / "templates"),
            static_folder=str(Path(__file__).resolve().parent.parent / "static"))

# --- Networking (AP/WiFi-client, LAN detection, admin gating) ---------------
# This concern doesn't cluster into one section the way Bluetooth's routes do
# (see "Routes — Bluetooth" further down) — it has a live entry point at
# nearly every stage of the app, and each needs to react to the current
# AP/LAN state independently: config defaults (search "wifi_client_ssid" in
# load_config), apply_ap_config()/apply_wifi_client()/apply_ap_enabled()
# (HOSTAPD_CONF/WPA_SUPPLICANT_CONF rewriting), the /api/config save handler,
# /api/restore, the boot sequence in __main__, and the lan_only/lan_only_page
# decorators used throughout "Routes". _wlan0_has_lan() below is the one
# piece of logic all of those share — outside this file it's also called
# from heartbeat.sh and sync-worker.py's has_lan() (via
# /api/internal/wlan0-has-lan) rather than re-derived, and build-image.sh
# bakes the same AP_SUBNET value into hostapd.conf/dnsmasq.d — grep for
# "192.168.4" there if that ever needs to change.
#
# guest AP subnet — playback/BT pairing open to AP by design, but admin
# actions (rewriting credentials, forcing factory reset) require the home LAN
AP_SUBNET = ipaddress.ip_network("192.168.4.0/24")


def _is_from_ap(remote_addr):
    try:
        return ipaddress.ip_address(remote_addr) in AP_SUBNET
    except ValueError:
        return False


def _wlan0_has_lan():
    """wlan0 has a real network address (not just link-local, not the guest
    AP subnet itself).

    A link-local (169.254.x.x) address doesn't count — that's dhcpcd's own
    RFC 3927 fallback when DHCP fails, not a real network. An AP_SUBNET
    (192.168.4.x) address doesn't count either — treating either as a real
    LAN self-locks the owner out: the AP-side request that could fix the
    problem gets blocked, with no fallback route to use instead.

    This is THE definition of "has LAN" for the whole app — heartbeat.sh and
    sync-worker.py's has_lan() both call /api/internal/wlan0-has-lan rather
    than re-deriving this themselves, after independent re-derivations in
    Bash drifted from this (excluding link-local but not AP_SUBNET) more
    than once. wifi-ap-fallback.sh is the one deliberate exception — see its
    own comment for why it can't depend on this endpoint."""
    if _DEV:
        return True
    try:
        result = subprocess.run(["ip", "-4", "-o", "addr", "show", "wlan0"],
                                 capture_output=True, text=True, timeout=3)
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            addr = ipaddress.ip_address(line.split()[3].split("/")[0])
            if not addr.is_link_local and addr not in AP_SUBNET:
                return True
        return False
    except Exception:
        return False


def _mympd_call(method, params=None, timeout=3):
    """POST a JSON-RPC request to myMPD's local API, return its "result"
    dict. The one place that builds this request — every myMPD caller in
    this file used to hand-roll the same Request/urlopen/json.loads
    boilerplate independently. Returns {} on any failure (myMPD unreachable,
    timeout, bad JSON) — callers already treat myMPD being briefly
    unavailable as non-fatal, so failure and "no data" look the same."""
    import urllib.request
    req = urllib.request.Request(
        "http://127.0.0.1:8080/api/default",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params or {}}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()).get("result", {})
    except Exception:
        return {}


def lan_only(f):
    """Reject requests over the guest AP, but only once a real home LAN exists.

    Checks live LAN presence, not cfg["mode"] — mode-based gating raced with
    itself: the wizard's /api/config call flips mode to "normal" and its
    follow-up /api/reboot (still over the AP, no LAN yet) would get rejected."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if _wlan0_has_lan() and _is_from_ap(request.remote_addr):
            return jsonify({"error": "requires a home network connection"}), 403
        return f(*args, **kwargs)
    return wrapper


def lan_only_page(f):
    """Same gate as lan_only for HTML page routes — redirects rather than 403."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if _wlan0_has_lan() and _is_from_ap(request.remote_addr):
            return redirect(url_for("toolbar"))
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hex_lighten(hex_color, amount):
    """Lighten (amount > 0) or darken (amount < 0) a hex color by a 0–1 fraction."""
    h = hex_color.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    d = int(amount * 255)
    return '#{:02x}{:02x}{:02x}'.format(
        min(255, max(0, r + d)),
        min(255, max(0, g + d)),
        min(255, max(0, b + d)),
    )


def _hex_rgba(hex_color, alpha):
    h = hex_color.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f'rgba({r},{g},{b},{alpha})'


def palette_css_vars(palette):
    """Return a full dict of :root CSS custom properties for the given palette.

    Derives surface/card/text/border tokens from the bg luminance so dark and
    light palettes both load correctly on page render, not just after JS runs.
    """
    bg = palette.get('bg', '#f4e8cc')
    c1 = palette.get('c1', '#5c2a0a')
    c2 = palette.get('c2', '#e8601a')
    c3 = palette.get('c3', '#f0c218')
    c4 = palette.get('c4', '#9ccce8')

    h = bg.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    luma = (r * 299 + g * 587 + b * 114) / 255000

    h3 = c3.lstrip('#')
    r3, g3, b3 = int(h3[0:2], 16), int(h3[2:4], 16), int(h3[4:6], 16)

    tokens = {
        '--bg': bg, '--c1': c1, '--c2': c2, '--c3': c3, '--c4': c4,
        '--c3-rgb': f'{r3},{g3},{b3}',
        '--border-dirty': c2, '--border-focus': c2,
        '--interactive': c2, '--interactive-text': '#fff',
        '--interactive-dim': _hex_rgba(c2, 0.12 if luma > 0.5 else 0.15),
    }

    h1 = c1.lstrip('#')
    r1, g1, b1 = int(h1[0:2], 16), int(h1[2:4], 16), int(h1[4:6], 16)
    h4 = c4.lstrip('#')
    r4, g4, b4 = int(h4[0:2], 16), int(h4[2:4], 16), int(h4[4:6], 16)

    if luma > 0.5:
        tokens.update({
            '--surface': _hex_lighten(bg, -0.06),
            '--card':    _hex_lighten(bg, 0.03),
            '--card-hi': _hex_lighten(bg, 0.07),
            '--text':    c1,
            '--text-2':  f'rgba({r1},{g1},{b1},0.58)',
            '--text-3':  f'rgba({r1},{g1},{b1},0.38)',
            '--border':  'rgba(92,42,10,0.14)',
            '--border-hi': 'rgba(92,42,10,0.28)',
            '--interactive-hover': '#d05012',
            '--success': '#2a7830', '--success-text': '#184820',
            '--danger':  '#c02020', '--danger-text':  '#780808',
        })
    else:
        tokens.update({
            '--surface': _hex_lighten(bg, 0.06),
            '--card':    _hex_lighten(bg, 0.11),
            '--card-hi': _hex_lighten(bg, 0.16),
            '--text':    c4,
            '--text-2':  f'rgba({r4},{g4},{b4},0.62)',
            '--text-3':  f'rgba({r4},{g4},{b4},0.36)',
            '--border':  'rgba(240,236,248,0.12)',
            '--border-hi': 'rgba(240,236,248,0.28)',
            '--interactive-hover': _hex_lighten(c2, -0.05),
            '--success': '#4a9e40', '--success-text': '#c8f0c0',
            '--danger':  '#cc3030', '--danger-text':  '#f8c8c8',
        })

    return tokens


_MYMPD_CUSTOM_CSS  = Path("/var/lib/private/mympd/config/custom_css")
_MYMPD_CUSTOM_JS   = Path("/var/lib/private/mympd/config/custom_js")

# written once — removes myMPD's cached custom.css link so palette changes
# are visible immediately after iframe reload (fetched fresh via versioned URL)
_MYMPD_CACHE_BUSTER_JS = """\
(function(){
  var links=document.querySelectorAll('link[href^="custom.css"]');
  links.forEach(function(l){l.parentNode.removeChild(l);});
  var s=document.createElement('link');
  s.rel='stylesheet';
  s.href='custom.css?v='+Date.now();
  document.head.appendChild(s);
})();
"""


def _mympd_palette_css(palette):
    """Generate Bootstrap/myMPD CSS variable overrides derived from the palette."""
    bg = palette.get("bg", "#f4e8cc")
    c1 = palette.get("c1", "#5c2a0a")
    c2 = palette.get("c2", "#e8601a")
    c3 = palette.get("c3", "#f0c218")
    c4 = palette.get("c4", "#9ccce8")

    h = bg.lstrip("#")
    r_bg, g_bg, b_bg = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
    luma = (r_bg*299 + g_bg*587 + b_bg*114) / 255000

    h1 = c1.lstrip("#")
    r_c1, g_c1, b_c1 = int(h1[0:2],16), int(h1[2:4],16), int(h1[4:6],16)

    # Palette-hued tone: blend bg toward white (t→1) or black (t→0).
    # Used for the gray scale AND for deriving text/border colors so every
    # value in the CSS carries the palette's hue rather than neutral gray.
    def _tone_rgb(t):
        if t >= 0.5:
            f = (t - 0.5) * 2
            return (int(r_bg + (255 - r_bg) * f),
                    int(g_bg + (255 - g_bg) * f),
                    int(b_bg + (255 - b_bg) * f))
        else:
            f = t * 2
            return (int(r_bg * f), int(g_bg * f), int(b_bg * f))

    def tone(t):
        r, g, b = _tone_rgb(t)
        return f"#{r:02x}{g:02x}{b:02x}"

    def tone_rgb(t):
        r, g, b = _tone_rgb(t)
        return f"{r},{g},{b}"

    if luma > 0.5:
        surface  = _hex_lighten(bg, -0.06)
        card     = _hex_lighten(bg,  0.03)
        text     = c1
        text_dim = _hex_rgba(c1, 0.60)
        border   = _hex_lighten(bg, -0.18)
        border_t = _hex_rgba(c1, 0.15)
        theme    = "light"
    else:
        surface  = _hex_lighten(bg,  0.07)
        card     = _hex_lighten(bg,  0.13)
        text     = c4
        text_dim = _hex_rgba(c4, 0.62)
        border   = _hex_lighten(bg,  0.22)
        border_t = _hex_rgba(c4, 0.15)
        theme    = "dark"

    r_t, g_t, b_t = [int(text.lstrip("#")[i:i+2], 16) for i in (0,2,4)]

    def rgb(h):
        hh = h.lstrip("#")
        return f"{int(hh[0:2],16)},{int(hh[2:4],16)},{int(hh[4:6],16)}"

    c2_rgb = rgb(c2)
    c3_rgb = rgb(c3)

    # Bootstrap gray-N tone levels mapped from the original luma of each step
    g100 = tone(0.975); g100_rgb = tone_rgb(0.975)
    g200 = tone(0.920)
    g300 = tone(0.878)
    g400 = tone(0.820); g400_rgb = tone_rgb(0.820)
    g500 = tone(0.706); g500_rgb = tone_rgb(0.706)
    g600 = tone(0.461); g600_rgb = tone_rgb(0.461)
    g700 = tone(0.314); g700_rgb = tone_rgb(0.314)
    g800 = tone(0.224); g800_rgb = tone_rgb(0.224)
    g900 = tone(0.137)

    css = f"""\
/* Zero-Fi palette override — do not edit, regenerated on palette save */
html[data-bs-theme="dark"], html[data-bs-theme="light"], html[data-bs-theme] {{
  /* ── Core body ── */
  --bs-body-bg:                {bg};
  --bs-body-bg-rgb:            {r_bg},{g_bg},{b_bg};
  --bs-body-color:             {text};
  --bs-body-color-rgb:         {r_t},{g_t},{b_t};
  --bs-emphasis-color:         {text};
  --bs-emphasis-color-rgb:     {r_t},{g_t},{b_t};
  --bs-secondary-color:        {text_dim};
  --bs-secondary-bg:           {surface};
  --bs-secondary-bg-rgb:       {rgb(surface)};
  --bs-tertiary-bg:            {card};
  --bs-tertiary-bg-rgb:        {rgb(card)};
  --bs-border-color:           {border};
  --bs-border-color-translucent: {border_t};
  --bs-link-color:             {c1};
  --bs-link-color-rgb:         {r_c1},{g_c1},{b_c1};
  --bs-link-hover-color:       {c1};
  --bs-highlight-color:        {text};
  /* ── Palette-hued gray scale (replaces Bootstrap's neutral grays) ── */
  --bs-gray-100:               {g100};
  --bs-gray-200:               {g200};
  --bs-gray-300:               {g300};
  --bs-gray-400:               {g400};
  --bs-gray-500:               {g500};
  --bs-gray-600:               {g600};
  --bs-gray-700:               {g700};
  --bs-gray-800:               {g800};
  --bs-gray-900:               {g900};
  --bs-gray:                   {g600};
  --bs-gray-dark:              {g800};
  /* ── Semantic color aliases ── */
  --bs-secondary:              {g600};
  --bs-secondary-rgb:          {g600_rgb};
  --bs-dark:                   {g800};
  --bs-dark-rgb:               {g800_rgb};
  --bs-light:                  {g100};
  --bs-light-rgb:              {g100_rgb};
  /* ── Subtle variants ── */
  --bs-secondary-bg-subtle:    {g200};
  --bs-secondary-border-subtle:{g400};
  --bs-secondary-text-emphasis:{g600};
  --bs-dark-bg-subtle:         {g400};
  --bs-dark-border-subtle:     {g500};
  --bs-dark-text-emphasis:     {g700};
  --bs-light-bg-subtle:        {g100};
  --bs-light-border-subtle:    {g200};
  --bs-light-text-emphasis:    {g700};
  /* ── Other components ── */
  --bs-dropdown-header-color:  {text_dim};
  --bs-breadcrumb-bg:          {surface};
  /* !important: myMPD's own JS does documentElement.style.setProperty(...)
     for these three on every settings load — an inline style that beats a
     plain stylesheet rule regardless of what value it's setting. Since that
     value comes from webuiSettings (bgColor/highlightColor/-Contrast, pushed
     below to c3/c1/c4), myMPD's own full-replace settings-save bug can null
     those out and this inline call then paints with an invalid value. !important
     is what makes our stylesheet win instead, independent of that value. */
  --mympd-highlightcolor:      {c1} !important;
  --mympd-highlightcolor-contrast: {c4} !important;
  --mympd-backgroundcolor:     {c3} !important;
}}
/* ── Table hover — match specificity of combined.css's html[data-bs-theme="dark"] table rule ── */
html[data-bs-theme="dark"] table,
html[data-bs-theme="light"] table,
html[data-bs-theme] table {{
  --bs-table-hover-bg:    rgba({c3_rgb}, 0.20);
  --bs-table-hover-color: {text};
  --bs-table-striped-bg:  rgba({c3_rgb}, 0.08);
  --bs-table-active-bg:   rgba({c3_rgb}, 0.30);
  --bs-table-color:       {text};
}}
/* ── Navbar ── */
html[data-bs-theme="dark"] .navbar,
html[data-bs-theme="light"] .navbar {{
  background-color: {surface} !important;
}}
html[data-bs-theme="dark"] .navbar button:hover,
html[data-bs-theme="light"] .navbar button:hover,
html[data-bs-theme="dark"] .navbar .btn:hover,
html[data-bs-theme="light"] .navbar .btn:hover {{
  background-color: rgba({c3_rgb}, 0.22) !important;
  border-color: rgba({c3_rgb}, 0.30) !important;
}}
html[data-bs-theme="dark"] #navbar-main div.active,
html[data-bs-theme="light"] #navbar-main div.active {{
  background-color: {c1} !important;
  color: {c4} !important;
}}
html[data-bs-theme="dark"] .navbar a,
html[data-bs-theme="light"] .navbar a {{
  color: {c2};
}}
.queue-playing > td {{
  background-color: {c4} !important;
  --bs-table-bg-state: {c4} !important;
  color: {c1} !important;
}}
.queue-playing [data-col="Action"] {{
  color: {c3} !important;
}}
#badgeQueueItems {{
  background-color: {c1} !important;
  color: {c4} !important;
}}
/* ── Buttons: component-scope vars shadow root vars, re-override here ── */
.btn-secondary {{
  --bs-btn-color: {text};
  --bs-btn-bg: {g600};
  --bs-btn-border-color: {g600};
  --bs-btn-hover-bg: rgba({c3_rgb}, 0.22);
  --bs-btn-hover-border-color: rgba({c3_rgb}, 0.30);
  --bs-btn-active-bg: rgba({c3_rgb}, 0.30);
  --bs-btn-active-border-color: rgba({c3_rgb}, 0.40);
  --bs-btn-disabled-bg: {g600};
  --bs-btn-disabled-border-color: {g600};
}}
.btn-outline-secondary {{
  --bs-btn-color: {g600};
  --bs-btn-border-color: {g600};
  --bs-btn-hover-bg: {g600};
  --bs-btn-hover-border-color: {g600};
  --bs-btn-active-bg: {g700};
  --bs-btn-active-border-color: {g700};
  --bs-btn-disabled-color: {g600};
}}
.btn-dark {{
  --bs-btn-color: {text};
  --bs-btn-bg: {g800};
  --bs-btn-border-color: {g800};
  --bs-btn-hover-bg: {g700};
  --bs-btn-hover-border-color: {g700};
  --bs-btn-active-bg: {g700};
  --bs-btn-active-border-color: {g600};
  --bs-btn-disabled-bg: {g800};
}}
.btn-outline-dark {{
  --bs-btn-color: {g800};
  --bs-btn-border-color: {g800};
  --bs-btn-hover-bg: {g800};
  --bs-btn-hover-border-color: {g800};
  --bs-btn-active-bg: {g900};
  --bs-btn-active-border-color: {g900};
  --bs-btn-disabled-color: {g800};
}}
.btn-light {{
  --bs-btn-color: {text};
  --bs-btn-bg: {g100};
  --bs-btn-border-color: {g100};
  --bs-btn-hover-bg: {g200};
  --bs-btn-hover-border-color: {g200};
  --bs-btn-active-bg: {g200};
  --bs-btn-active-border-color: {g300};
  --bs-btn-disabled-bg: {g100};
}}
.btn-outline-light {{
  --bs-btn-color: {g100};
  --bs-btn-border-color: {g100};
  --bs-btn-hover-bg: {g100};
  --bs-btn-hover-border-color: {g100};
  --bs-btn-active-bg: {g200};
  --bs-btn-active-border-color: {g200};
  --bs-btn-disabled-color: {g100};
}}
.btn-link {{ color: {c1}; }}
/* ── Tables ── */
.table-dark {{
  --bs-table-color: {text};
  --bs-table-bg: {g800};
  --bs-table-border-color: {g600};
  --bs-table-striped-bg: rgba({c3_rgb}, 0.10);
  --bs-table-hover-bg: rgba({c3_rgb}, 0.22);
  --bs-table-active-bg: rgba({c3_rgb}, 0.32);
}}
.table-light {{
  --bs-table-color: {text};
  --bs-table-bg: {g100};
  --bs-table-border-color: {g300};
}}
.table-secondary {{
  --bs-table-color: {text};
  --bs-table-bg: {g200};
  --bs-table-border-color: {g400};
}}
/* ── Dropdowns ── */
.dropdown-menu-dark {{
  --bs-dropdown-color: {text};
  --bs-dropdown-bg: {g800};
  --bs-dropdown-link-color: {text};
  --bs-dropdown-link-hover-color: {text};
  --bs-dropdown-link-hover-bg: rgba({c3_rgb}, 0.20);
  --bs-dropdown-link-active-bg: {g600};
  --bs-dropdown-link-disabled-color: {text_dim};
  --bs-dropdown-divider-bg: {g600};
  --bs-dropdown-header-color: {text_dim};
}}
/* ── Hardcoded color: values that bypass the variable system ── */
caption, .blockquote-footer {{
  color: {text_dim};
}}
.form-floating > :disabled ~ label,
.form-floating > .form-control:disabled ~ label {{
  color: {text_dim};
}}
/* ── Splash screen ── */
#splashScreen {{
  background: {bg} !important;
}}
#splashScreenLogo svg path {{
  fill: {c1} !important;
}}
#splashScreenAlert {{
  color: {text} !important;
}}
"""
    return css, theme


def _push_mympd_webui_settings(palette):
    """Push the JSON-RPC side of the palette into myMPD: accent colours,
    theme, and background color via webuiSettings + PARTITION_SAVE.

    Cheap and idempotent — a handful of small HTTP POSTs, no restart, no
    filesystem writes. Safe (and expected) to call on every heartbeat cycle:
    MYMPD_API_SETTINGS_SET is a full-replace, not a merge, so *any* save
    through myMPD's own Settings dialog — even something unrelated like the
    startpage — silently drops whatever fields that particular save's payload
    didn't carry (confirmed live: saving just startupView wiped bgColor/theme
    to null entirely). Re-running this reconciles the whole webuiSettings
    blob back to a known-good state every time, rather than chasing each
    individual field myMPD's UI happens to clobber.

    No-op in dev mode (no myMPD running locally).
    """
    if _DEV:
        return

    c1 = palette.get("c1", "#5c2a0a")
    c3 = palette.get("c3", "#f0c218")
    c4 = palette.get("c4", "#9ccce8")

    _, theme = _mympd_palette_css(palette)

    # Fetch current webui_settings and merge — MYMPD_API_SETTINGS_SET replaces
    # the entire blob, so sending only our 4 fields would wipe user preferences.
    existing_ws = _mympd_call("MYMPD_API_SETTINGS_GET").get("webuiSettings", {})

    # Sane defaults that differ from myMPD's own — applied as the base layer
    # under existing_ws (not gated on existing_ws being empty) so a genuinely
    # missing key gets refilled every reconciliation pass, whether that's
    # because this is a true first boot or because myMPD's own full-replace
    # settings save dropped it. existing_ws still wins per-key, so a value the
    # user actually set (present, even if False) is never overridden.
    sane_defaults = {
        "endlessScroll": True,       # smoother on Pi Zero
        "footerVolumeLevel": True,   # first boot may be 100% — important to show the slider
        "startupView": "Database",   # most useful first view for a music player
    }

    _mympd_call("MYMPD_API_SETTINGS_SET",
                {"webuiSettings": {
                    **sane_defaults,
                    **existing_ws,
                    "highlightColor": c1, "highlightColorContrast": c4,
                    "bgColor": c3, "theme": theme,
                }})
    _mympd_call("MYMPD_API_PARTITION_SAVE",
                {"highlightColor": c1, "highlightColorContrast": c4})

    # re-color home icons: any icon with explicit bgcolor/color gets c1/c4
    try:
        icons = _mympd_call("MYMPD_API_HOME_ICON_LIST").get("data", [])
        for i, icon in enumerate(icons):
            if icon.get("type") == "icon" and icon.get("bgcolor") and icon.get("color"):
                _mympd_call("MYMPD_API_HOME_ICON_SAVE", {
                    "replace": True, "oldpos": i,
                    "name": icon["name"], "ligature": icon.get("ligature", ""),
                    "bgcolor": c1, "color": c4,
                    "image": icon.get("image", ""),
                    "cmd": icon["cmd"], "options": icon["options"],
                })
    except Exception as e:
        print(f"WARNING: could not re-color home icons: {e}", flush=True)


def apply_palette_to_mympd(palette):
    """Full palette apply: the cheap webuiSettings push above, plus custom CSS
    for the full structural palette (bg, surfaces, text, borders) and the
    myMPD restart that requires. Call this on an actual palette change or at
    Flask boot — NOT on a timer (the restart drops the myMPD iframe's live
    connection). For periodic drift reconciliation, use
    _push_mympd_webui_settings() instead, via /api/internal/mympd-resync.

    No-op in dev mode (no myMPD running locally).
    """
    if _DEV:
        return
    _push_mympd_webui_settings(palette)
    css, _ = _mympd_palette_css(palette)

    try:
        _MYMPD_CUSTOM_CSS.write_text(css)
    except Exception as e:
        print(f"WARNING: could not write myMPD custom CSS: {e}", flush=True)

    try:
        _MYMPD_CUSTOM_JS.write_text(_MYMPD_CACHE_BUSTER_JS)
    except Exception as e:
        print(f"WARNING: could not write myMPD custom JS: {e}", flush=True)

    # myMPD holds custom_css in memory from startup — restart to pick up new file
    try:
        import subprocess
        subprocess.run(["systemctl", "restart", "mympd"], check=True, timeout=10)
    except Exception as e:
        print(f"WARNING: could not restart myMPD: {e}", flush=True)


_mpd_update_lock = threading.Lock()


def mpd_update_and_wait(timeout=180):
    """The one place that runs an MPD library update — every caller (boot,
    sync-worker via /api/internal/mpd-update) funnels through here.

    MPD and myMPD are logically one database, not two: MPD's tag_cache is
    the source of truth, and myMPD derives its own Album/Artist grids from
    it, refreshing only when it observes MPD's update-finished idle event.
    Two update cycles started close together (e.g. a sync run that spans
    two 30-minute worker cycles, each ending in its own update) can leave
    myMPD mid-rebuild from the first when the second lands, so it ends up
    serving a grid built off a partial snapshot — blank cards, undercounted
    albums, artists missing despite having visible albums — even though
    MPD's own database is complete and correct. The lock below serializes
    updates so only one is ever in flight, and this function does not
    return until myMPD's own album grid has stopped changing, so no caller
    can observe it mid-rebuild.

    Note: myMPD's own album count is NOT expected to equal MPD's `stats`
    albums count — MPD counts distinct `Album` tag values, myMPD's grid
    groups by AlbumArtist+Album (or similar), so myMPD's number is
    routinely higher (confirmed live: 267 vs 276 on a real library, stable
    both before and after a fresh update). Convergence here means the
    reading is unchanged across consecutive polls, not that it matches
    MPD's count.
    """
    if _DEV:
        return

    def _mympd_album_total():
        data = _mympd_call("MYMPD_API_DATABASE_ALBUM_LIST", {
            "offset": 0, "limit": 1, "expression": "",
            "sort": "AlbumArtist", "sortdesc": False, "fields": ["Album"],
        }, timeout=5)
        return data.get("totalEntities")

    # Log contention explicitly (not just the eventual result) so repeated
    # backups are visible in the journal rather than only inferrable from
    # request latency after the fact.
    if not _mpd_update_lock.acquire(blocking=False):
        wait_start = time.monotonic()
        print("mpd_update_and_wait: another update already in progress, waiting for it", flush=True)
        _mpd_update_lock.acquire()
        print(f"mpd_update_and_wait: acquired lock after waiting {time.monotonic() - wait_start:.1f}s", flush=True)
    try:
        try:
            subprocess.run(["mpc", "-h", "/run/mpd/socket", "update", "--wait"],
                           capture_output=True, timeout=timeout)
        except Exception as e:
            print(f"WARNING: mpc update failed: {e}", flush=True)
            return

        # Explicit trigger rather than relying solely on myMPD's own idle
        # client noticing the update — makes the rebuild start deterministic.
        # Fire-and-forget: myMPD gives no synchronous response for this method,
        # and _mympd_call() already swallows failures.
        _mympd_call("MYMPD_API_CACHES_CREATE")

        STABLE_READS_REQUIRED = 3
        stable_count = 0
        last_seen = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = _mympd_album_total()
            if current is not None and current == last_seen:
                stable_count += 1
                if stable_count >= STABLE_READS_REQUIRED:
                    return
            else:
                stable_count = 0
            last_seen = current
            time.sleep(1)
        print("WARNING: myMPD album cache did not settle before timeout", flush=True)
    finally:
        _mpd_update_lock.release()


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


def load_config():
    defaults = {
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
        "bt_mode": "target",
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
    if CONFIG_FILE.exists():
        data = json.loads(CONFIG_FILE.read_text())
        if "wifi_client_enabled" not in data and data.get("wifi_client_ssid"):
            # upgrading a box that already joined a network — don't silently disconnect it
            data["wifi_client_enabled"] = True
        if "instance_name" not in data and data.get("ap_ssid"):
            data["instance_name"] = data["ap_ssid"]
        defaults.update(data)
    return defaults


def _write_json_atomic(path, data):
    """Write via temp file + rename — exFAT has no journal; a power cut mid-write
    without this leaves a truncated/corrupt file. Also re-creates the parent dir
    if missing: setup_complete() reformats this partition before its own save_config()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps(data, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_config(cfg):
    _write_json_atomic(CONFIG_FILE, cfg)


def load_known_devices():
    """Return list of known BT devices."""
    if KNOWN_DEVICES_FILE.exists():
        return json.loads(KNOWN_DEVICES_FILE.read_text())
    return []


def save_known_devices(devices):
    _write_json_atomic(KNOWN_DEVICES_FILE, devices)


# PipeWire runs as a dedicated `pipewire` system user's --user session.
# Root can always read another user's runtime dir — XDG_RUNTIME_DIR is the
# whole bridge needed to reach it.
try:
    PIPEWIRE_UID = pwd.getpwnam("pipewire").pw_uid
except KeyError:
    PIPEWIRE_UID = os.getuid()
_PW_ENV = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{PIPEWIRE_UID}"}


def pw_dump():
    """Full PipeWire object graph as parsed JSON (nodes, ports, links, metadata)."""
    if _DEV:
        return []
    try:
        result = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=5, env=_PW_ENV,
        )
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return []


def pw_set_default_sink(sink_name):
    """Set PipeWire's default sink via pw-metadata, not wpctl set-default —
    wpctl set-default fails outright ("Node not found") for every valid sink id
    on this headless WirePlumber session; pw-metadata writing the same key works."""
    if _DEV:
        return True
    value = json.dumps({"name": sink_name})
    try:
        result = subprocess.run(
            ["pw-metadata", "-n", "default", "0", "default.audio.sink", value, "Spa:String:JSON"],
            capture_output=True, text=True, timeout=5, env=_PW_ENV,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _pw_default_sink_name(objects):
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Metadata":
            continue
        if obj.get("props", {}).get("metadata.name") != "default":
            continue
        for entry in obj.get("metadata") or []:
            if entry.get("key") == "default.audio.sink":
                return (entry.get("value") or {}).get("name")
    return None


def _pw_node_ports(objects, node_id, direction):
    ports = []
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Port":
            continue
        props = (obj.get("info") or {}).get("props", {})
        if props.get("node.id") == node_id and props.get("port.direction") == direction:
            ports.append({"id": obj["id"], "channel": props.get("audio.channel", "")})
    return ports


def get_sinks():
    """Return list of PipeWire Audio/Sink nodes."""
    sinks = []
    for obj in pw_dump():
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props", {})
        if props.get("media.class") != "Audio/Sink":
            continue
        name = props.get("node.name", "")
        label = "Bluetooth" if name.startswith("bluez_output.") else "AUX (USB DAC)"
        sinks.append({"id": str(obj["id"]), "name": name, "label": label})
    return sinks


def get_connected_bt_device():
    """Which known device (if any) currently has a live bluez_output sink."""
    known = load_known_devices()
    for s in get_sinks():
        if s["name"].startswith("bluez_output."):
            mac = s["name"].split(".")[1].replace("_", ":").upper()
            dev = next((d for d in known if d["mac"].upper() == mac), None)
            if dev:
                return dev
    return None


def get_connected_bt_source():
    """Which known device is actively streaming TO this box right now.

    A bluez_input.* node sits in `state: suspended` until something links to
    its output ports — gating on state == "running" means this never shows as
    connected even while a phone is actively streaming."""
    known = load_known_devices()
    for obj in pw_dump():
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        info = obj.get("info") or {}
        name = info.get("props", {}).get("node.name", "")
        if name.startswith("bluez_input."):
            mac = name.split(".")[1].replace("_", ":").upper()
            return next((d for d in known if d["mac"].upper() == mac), None)
    return None


def _restore_dac_if_needed(sinks=None):
    """Fall back to USB DAC when the current sink is gone or audio streams are unlinked.

    WirePlumber may update the default.audio.sink metadata when a BT sink disappears,
    making the current sink look valid — but MPD's stream is left unlinked and reports
    "Failed to open audio output". Checking for unlinked streams catches that case too."""
    if _DEV:
        return
    if sinks is None:
        sinks = get_sinks()
    objects = pw_dump()
    current = _pw_default_sink_name(objects)
    sink_names = {s["name"] for s in sinks}

    linked_output_nodes = {
        (obj.get("info") or {}).get("output-node-id")
        for obj in objects
        if obj.get("type") == "PipeWire:Interface:Link"
    }
    has_unlinked_stream = any(
        obj.get("type") == "PipeWire:Interface:Node"
        and (obj.get("info") or {}).get("props", {}).get("media.class") == "Stream/Output/Audio"
        and obj["id"] not in linked_output_nodes
        for obj in objects
    )

    dac = next((s["name"] for s in sinks if s["label"] == "AUX (USB DAC)"), None)
    if not dac:
        return
    if (current not in sink_names) or has_unlinked_stream:
        _set_default_sink(dac)


def _set_default_sink(sink_name):
    """Set the PipeWire default sink and re-link already-playing streams.

    Setting the default alone only affects future streams — WirePlumber links
    a stream to the default at creation time and doesn't re-link an already-
    connected stream when the default changes later."""
    objects = pw_dump()
    sink_node = next(
        (o for o in objects if o.get("type") == "PipeWire:Interface:Node"
         and (o.get("info") or {}).get("props", {}).get("node.name") == sink_name),
        None,
    )
    if not sink_node:
        return False

    if not pw_set_default_sink(sink_name):
        return False

    dst_ports = _pw_node_ports(objects, sink_node["id"], "in")
    dst_by_chan = {p["channel"]: p for p in dst_ports if p["channel"]}

    links_by_output_node = {}
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Link":
            continue
        info = obj.get("info") or {}
        links_by_output_node.setdefault(info.get("output-node-id"), []).append(info)

    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props", {})
        if props.get("media.class") != "Stream/Output/Audio":
            continue
        node_id = obj["id"]
        if node_id == sink_node["id"]:
            continue
        existing_links = links_by_output_node.get(node_id, [])
        if existing_links and all(l["input-node-id"] == sink_node["id"] for l in existing_links):
            continue  # already on the target sink
        for link in existing_links:
            subprocess.run(["pw-link", "-d", str(link["output-port-id"]), str(link["input-port-id"])],
                            capture_output=True, env=_PW_ENV)
        src_ports = _pw_node_ports(objects, node_id, "out")
        src_by_chan = {p["channel"]: p for p in src_ports if p["channel"]}
        pairs = [(sp["id"], dst_by_chan[chan]["id"]) for chan, sp in src_by_chan.items() if chan in dst_by_chan]
        if not pairs:
            pairs = list(zip((p["id"] for p in src_ports), (p["id"] for p in dst_ports)))
        for src_id, dst_id in pairs:
            subprocess.run(["pw-link", str(src_id), str(dst_id)], capture_output=True, env=_PW_ENV)

    return True


def get_output_options():
    """Toolbar output picker options: live sinks (switch by name) plus known
    BT devices that aren't currently connected (connect-then-switch by mac,
    since a BT sink doesn't exist until it's connected)."""
    sinks = get_sinks()
    known = load_known_devices()
    connected_macs = set()
    options = []
    for s in sinks:
        label = s["label"]
        if s["name"].startswith("bluez_output."):
            mac = s["name"].split(".")[1].replace("_", ":").upper()
            connected_macs.add(mac)
            dev = next((d for d in known if d["mac"].upper() == mac), None)
            if dev:
                label = dev["alias"]
        options.append({"kind": "sink", "value": s["name"], "label": label})
    for d in known:
        if d["mac"].upper() not in connected_macs:
            options.append({"kind": "device", "value": d["mac"], "label": f'{d["alias"]} (tap to connect)'})
    return options


def wait_for_bt_sink(mac, tries=10, interval=1):
    """Poll for the bluez_output sink a device's connection produces — a BT
    sink doesn't exist until the moment it's actually connected."""
    expected_prefix = "bluez_output." + mac.replace(":", "_")
    for _ in range(tries):
        match = next((s for s in get_sinks() if s["name"].startswith(expected_prefix)), None)
        if match:
            return match
        time.sleep(interval)
    return None


# cross-process flock shared with bluetooth-agent.sh — threading.Lock is
# insufficient because this process and that script are separate processes
# both touching the same bluetoothd D-Bus state
BT_LOCK_FILE = Path("/run/zerofi/bt.lock")


class BtBusyError(Exception):
    pass


@contextmanager
def bt_locked(timeout=15):
    """Hold an exclusive flock on BT_LOCK_FILE for the duration of the
    `with` block, or raise BtBusyError if another operation is already holding it.
    Polls with LOCK_NB to give a "busy" report after `timeout` seconds."""
    if _DEV:
        yield
        return
    BT_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(BT_LOCK_FILE, os.O_CREAT | os.O_RDWR)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BtBusyError("Bluetooth is busy with another operation — try again in a moment.")
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


HOSTAPD_CONF = Path("/etc/hostapd/hostapd.conf")
SHAIRPORT_CONF = Path("/etc/shairport-sync.conf")
MPD_AVAHI_SVC = Path("/etc/avahi/services/mpd.service")
SSH_KEY_PREFIXES = ("ssh-rsa", "ssh-ed25519", "ssh-dss", "ecdsa-sha2-")


def _to_hostname(name):
    """Slug-ify a display name for use as a system hostname or DHCP client ID."""
    slug = re.sub(r"[^a-zA-Z0-9-]+", "-", name).strip("-").lower()
    return slug or "zerofi"


def apply_instance_name(name):
    """Apply the instance name to hostname, /etc/hosts, AP SSID, AirPlay name,
    MPD Avahi service name, and Bluetooth alias. DHCP renews to advertise the
    new hostname to the router's lease table."""
    if _DEV or not name:
        return
    hostname = _to_hostname(name)
    try:
        subprocess.run(["hostnamectl", "set-hostname", hostname],
                       capture_output=True, timeout=5)
    except Exception as e:
        print(f"WARNING: hostnamectl failed: {e}", flush=True)
    try:
        hosts = Path("/etc/hosts")
        text = hosts.read_text()
        new_text = re.sub(r"127\.0\.1\.1[ \t]+\S+", f"127.0.1.1\t{hostname}", text)
        if new_text == text and "127.0.1.1" not in text:
            new_text = text.rstrip("\n") + f"\n127.0.1.1\t{hostname}\n"
        hosts.write_text(new_text)
    except Exception as e:
        print(f"WARNING: /etc/hosts update failed: {e}", flush=True)
    cfg = load_config()
    apply_ap_config(name, cfg.get("ap_password", ""))
    subprocess.run(["dhcpcd", "-n", "wlan0"], capture_output=True, timeout=15)


def apply_ap_config(ssid, password):
    """Rewrite hostapd SSID/password, shairport-sync AirPlay name, mpd Avahi
    service name, and BT alias to all match — one identity, one place to change.

    Empty password or password < 8 chars → wpa=0 (open AP). hostapd rejects
    passwords shorter than 8 chars and fails to restart — no password beats no AP."""
    if _DEV or not ssid:
        return

    lines = [l for l in HOSTAPD_CONF.read_text().splitlines()
             if not l.startswith(("ssid=", "ssid2=", "utf8_ssid=", "wpa=", "wpa_passphrase=", "wpa_key_mgmt=", "wpa_pairwise="))]
    # ssid2=<hex> accepts any byte sequence — avoids hostapd config parsing
    # hazards (#, whitespace, non-ASCII) and the 32-byte limit
    ssid_hex = ("🎵 " + ssid).encode("utf-8")[:32].hex()
    lines.append(f"utf8_ssid=1")
    lines.append(f"ssid2={ssid_hex}")
    if password and len(password) >= 8:
        lines += ["wpa=2", "wpa_key_mgmt=WPA-PSK", "wpa_pairwise=CCMP", f"wpa_passphrase={password}"]
    else:
        lines.append("wpa=0")
    HOSTAPD_CONF.write_text("\n".join(lines) + "\n")
    # restart hostapd only if the AP interface (wlan0_ap, created by
    # zerofi-ap.service ExecStartPre) is currently up — restarting when the
    # AP is off crash-loops hostapd ("No such device" on every attempt)
    ap_service_active = subprocess.run(["systemctl", "is-active", "zerofi-ap.service"],
                                        capture_output=True, text=True, timeout=5).stdout.strip()
    if ap_service_active == "active":
        subprocess.run(["systemctl", "restart", "hostapd"], capture_output=True, timeout=10)

    if SHAIRPORT_CONF.exists():
        text = re.sub(r'name\s*=\s*"[^"]*"', f'name = "{ssid}"', SHAIRPORT_CONF.read_text())
        SHAIRPORT_CONF.write_text(text)
        subprocess.run(["systemctl", "restart", "shairport-sync"], capture_output=True, timeout=10)

    if MPD_AVAHI_SVC.exists():
        # no restart needed — avahi-daemon watches its services directory and
        # picks up edits on its own
        text = re.sub(r"<name>[^<]*</name>", f"<name>{ssid}</name>", MPD_AVAHI_SVC.read_text())
        MPD_AVAHI_SVC.write_text(text)

    with bt_locked():
        for _ in range(3):
            subprocess.run(["bluetoothctl", "system-alias", ssid], capture_output=True, timeout=5)
            result = subprocess.run(["bluetoothctl", "show"], capture_output=True, text=True, timeout=5)
            if f"Alias: {ssid}" in result.stdout:
                break
            time.sleep(1)
        else:
            print(f"WARNING: bluetoothctl system-alias never confirmed as '{ssid}'", flush=True)


def get_wifi_status():
    """Live wlan0 client status for the Home WiFi panel."""
    try:
        result = subprocess.run(["wpa_cli", "-i", "wlan0", "status"],
                                 capture_output=True, text=True, timeout=5)
        info = dict(line.split("=", 1) for line in result.stdout.strip().split("\n") if "=" in line)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        info = {}
    connected = info.get("wpa_state") == "COMPLETED" and bool(info.get("ip_address"))
    return {
        "connected": connected,
        "ssid": info.get("ssid", ""),
        "ip": info.get("ip_address", ""),
    }


WPA_SUPPLICANT_CONF = Path("/etc/wpa_supplicant/wpa_supplicant-wlan0.conf")


def apply_wifi_client(ssid, password):
    """Join wlan0 to a home network."""
    if _DEV or not ssid:
        return
    escape = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    WPA_SUPPLICANT_CONF.parent.mkdir(parents=True, exist_ok=True)
    WPA_SUPPLICANT_CONF.write_text(
        "ctrl_interface=/run/wpa_supplicant\n"
        "update_config=1\n\n"
        "network={\n"
        f'    ssid="{escape(ssid)}"\n'
        f'    psk="{escape(password)}"\n'
        "}\n"
    )
    WPA_SUPPLICANT_CONF.chmod(0o600)
    # This early in boot, wlan0 may not exist yet — brcmfmac (Pi Zero W's SDIO
    # WiFi chip driver) can still be loading firmware, and systemd blocks
    # "--now" waiting for the device unit. Confirmed on real hardware: a flat
    # 10s timeout aborts this whole function (the caller's except swallows
    # it), abandoning the join entirely for that boot — only heartbeat's next
    # 5-min reconciliation retries it. Retry with backoff instead of bailing.
    for attempt, wait in enumerate((10, 20, 30)):
        try:
            subprocess.run(["systemctl", "enable", "--now", "wpa_supplicant@wlan0.service"],
                            capture_output=True, timeout=wait, check=True)
            break
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            if attempt == 2:
                raise
    subprocess.run(["systemctl", "restart", "wpa_supplicant@wlan0.service"],
                    capture_output=True, timeout=10)
    subprocess.run(["dhcpcd", "-n", "wlan0"], capture_output=True, timeout=15)
    # dhcpcd doesn't nudge timesyncd when a new route appears — left alone it
    # retries on its own backoff schedule, leaving the clock unsynced for minutes
    subprocess.run(["systemctl", "restart", "systemd-timesyncd"], capture_output=True, timeout=10)


def apply_wifi_client_enabled(enabled, ssid, password):
    """On/off toggle for Home WiFi — clearing the SSID alone doesn't tear down
    an existing connection."""
    if _DEV:
        return
    if enabled:
        apply_wifi_client(ssid, password)
    else:
        subprocess.run(["systemctl", "disable", "--now", "wpa_supplicant@wlan0.service"],
                        capture_output=True, timeout=10)
        subprocess.run(["dhcpcd", "-k", "wlan0"], capture_output=True, timeout=10)


def apply_timezone(tz):
    if _DEV or not tz:
        return
    subprocess.run(["timedatectl", "set-timezone", tz], capture_output=True, timeout=10)


DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?")


def apply_datetime(dt_str):
    """Manual clock correction for when there's no network to sync from.
    Flips NTP off just long enough to force the value in, then back on."""
    if _DEV or not dt_str or not DATETIME_RE.fullmatch(dt_str):
        return
    subprocess.run(["timedatectl", "set-ntp", "false"], capture_output=True, timeout=10)
    subprocess.run(["timedatectl", "set-time", dt_str], capture_output=True, timeout=10)
    subprocess.run(["timedatectl", "set-ntp", "true"], capture_output=True, timeout=10)


LOG_EXPORT_CONF = Path("/etc/rsyslog.d/60-zerofi-forward.conf")


def apply_log_export(destination):
    """Configure rsyslog to forward all logs to a remote syslog collector over UDP.
    Empty destination disables forwarding."""
    if _DEV:
        return
    if destination and not re.fullmatch(r"[\w.\-]+:\d+", destination):
        return  # doesn't look like host:port — ignore rather than write garbage into rsyslog's config
    if destination:
        LOG_EXPORT_CONF.write_text(f"*.* @{destination}\n")
    else:
        LOG_EXPORT_CONF.unlink(missing_ok=True)
    subprocess.run(["systemctl", "restart", "rsyslog"], capture_output=True, timeout=10)


def _toggle_service(unit, enabled):
    if _DEV:
        return
    # exception here is in __main__, not a request handler — it kills the whole
    # process before app.run() and takes down the admin UI needed to diagnose it
    action = ["enable", "--now"] if enabled else ["disable", "--now"]
    try:
        subprocess.run(["systemctl"] + action + [unit], capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        pass


def apply_ap_enabled(enabled):
    """Guest WiFi AP on/off. Refuses to disable it while it's the only access
    path — lan_only exempts the AP when there's no LAN yet, so a request over
    the AP could otherwise turn off its own only path.

    Returns whether it actually applied, so callers can correct a persisted
    value that the guard refused to honor."""
    if not enabled and not _wlan0_has_lan():
        return False
    if not enabled and not _DEV:
        subprocess.run(["systemctl", "stop", "hostapd"], capture_output=True, timeout=10)
    _toggle_service("zerofi-ap.service", enabled)
    if enabled and not _DEV:
        subprocess.run(["systemctl", "start", "hostapd"], capture_output=True, timeout=10)
    return True


def apply_ssh_enabled(enabled):
    # Dropbear, not openssh — no "ssh.service" alias; only dropbear.service exists
    _toggle_service("dropbear.service", enabled)


def apply_airplay_enabled(enabled):
    _toggle_service("shairport-sync", enabled)


def _bt_device_is_sink(mac):
    """Is this a real audio sink (speaker/headphones) rather than an audio
    source (phone)? Real speakers advertise "Audio Sink" UUID (0000110b);
    a phone streamed to us in Target mode advertises "Audio Source" instead."""
    if _DEV:
        return False
    result = subprocess.run(["bluetoothctl", "info", mac], capture_output=True, text=True, timeout=5)
    return "Audio Sink" in result.stdout


def _restrict_bt_trust_to_sinks():
    """Entering Source mode: untrust anything that isn't an audio sink so any
    future reconnect attempt has to go through the agent, which rejects it while
    in Source mode. A Trusted device bypasses AuthorizeService entirely on
    reconnect — discoverable/pairable off only gates new pairing, not reconnects
    from already-bonded devices. Untrust (not unpair) — still shown in Settings,
    still manually connectable as an output."""
    if _DEV:
        return
    result = subprocess.run(["bluetoothctl", "devices", "Trusted"], capture_output=True, text=True, timeout=10)
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) < 2 or parts[0] != "Device":
            continue
        mac = parts[1]
        if not _bt_device_is_sink(mac):
            # retried and read back — this is the actual security boundary for
            # Source mode; a device not confirmed untrusted can still reconnect
            for _ in range(3):
                subprocess.run(["bluetoothctl", "untrust", mac], capture_output=True, timeout=5)
                subprocess.run(["bluetoothctl", "disconnect", mac], capture_output=True, timeout=5)
                info = subprocess.run(["bluetoothctl", "info", mac], capture_output=True, text=True, timeout=5)
                if "Trusted: no" in info.stdout:
                    break
                time.sleep(1)
            else:
                print(f"WARNING: could not confirm {mac} untrusted after 3 attempts — "
                      "it may still be able to silently reconnect in Source mode", flush=True)


def _bt_set_flag(flag, value, tries=5, interval=1):
    """Set a bluetoothctl boolean adapter flag and verify it took — retrying
    the command itself, not just re-checking."""
    if _DEV:
        return True
    want = "yes" if value else "no"
    label = flag.capitalize() + ":"
    for _ in range(tries):
        subprocess.run(["bluetoothctl", flag, "on" if value else "off"], capture_output=True, timeout=5)
        result = subprocess.run(["bluetoothctl", "show"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith(label):
                if line.split(":", 1)[1].strip().lower() == want:
                    return True
                break
        time.sleep(interval)
    return False


def apply_bt_mode(mode):
    """Configure the radio as Source (connect out to speaker) or Target (be a speaker).

    bluetoothd defaults are Discoverable=no / Pairable=no — Target mode requires
    explicitly enabling both, plus discoverable-timeout 0 so it stays visible
    permanently rather than reverting after BlueZ's default 180s.

    zerofi-bt-agent.service is not toggled here — it runs in both modes
    (it enforces Source-mode rejection, not just Target-mode auto-trust) so
    it's enabled unconditionally at build time."""
    if _DEV:
        return
    target = (mode == "target")
    with bt_locked():
        subprocess.run(["bluetoothctl", "discoverable-timeout", "0"], capture_output=True, timeout=5)
        if not _bt_set_flag("discoverable", target):
            print(f"WARNING: bluetoothctl discoverable never confirmed {'on' if target else 'off'}", flush=True)
        if not _bt_set_flag("pairable", target):
            print(f"WARNING: bluetoothctl pairable never confirmed {'on' if target else 'off'}", flush=True)
        if not target:
            _restrict_bt_trust_to_sinks()
    _toggle_service("zerofi-bt-audio.service", target)
    if target or mode == "off":
        # Target/Off: must output to a real physical sink, not a BT/AirPlay hop.
        # Force unconditionally — the BT sink may still be live when mode changes.
        sinks = get_sinks()
        dac = next((s["name"] for s in sinks if s["label"] == "AUX (USB DAC)"), None)
        if dac:
            _set_default_sink(dac)
        else:
            # DAC not yet available; _restore_dac_if_needed will catch it on next poll.
            print("WARNING: apply_bt_mode: no USB DAC sink found, will restore on next poll", flush=True)


_BT_SOURCE_ACTIVE_FLAG = Path("/run/zerofi/bt-source-active")


def _handle_bt_source_connect(source):
    """Stop MPD when a BT source connects to us in target mode.

    Polled on each /api/outputs call. Uses a flag file in /run (tmpfs) so
    the stop fires exactly once per connect, not on every poll. Flag is
    cleared when the source disconnects or when mode leaves target."""
    cfg = load_config()
    if cfg.get("bt_mode") != "target":
        _BT_SOURCE_ACTIVE_FLAG.unlink(missing_ok=True)
        return
    was_active = _BT_SOURCE_ACTIVE_FLAG.exists()
    if source and not was_active:
        if not _DEV:
            subprocess.run(["mpc", "-h", "/run/mpd/socket", "stop"],
                           capture_output=True, timeout=5)
        try:
            _BT_SOURCE_ACTIVE_FLAG.parent.mkdir(parents=True, exist_ok=True)
            _BT_SOURCE_ACTIVE_FLAG.touch()
        except Exception:
            pass
    elif not source and was_active:
        _BT_SOURCE_ACTIVE_FLAG.unlink(missing_ok=True)


MAINTENANCE_START_HOUR = 2
MAINTENANCE_END_HOUR = 5


def is_maintenance_window():
    return MAINTENANCE_START_HOUR <= datetime.now().hour < MAINTENANCE_END_HOUR



# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return redirect(url_for("toolbar"))


@app.route("/api/config", methods=["GET"])
@lan_only
def get_config_api():
    """Lets the settings UI re-render just the card it saved, rather than
    reloading the whole page, while still reflecting what the server actually
    persisted (e.g. a too-short AP password gets silently ignored server-side)."""
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
@lan_only
def save_config_api():
    data = request.get_json(force=True)
    cfg = load_config()
    cfg.update(data)
    save_config(cfg)
    # wifi_client before ap_enabled — same reasoning as the boot sequence in
    # __main__: apply_ap_enabled(False) refuses to disable the AP unless
    # wlan0 already has a real LAN address, so joining home WiFi first (and
    # giving it a bounded moment to actually get an address, not just start
    # the join) is what lets that guard see a successful join instead of
    # always seeing "no LAN yet" and silently keeping the AP on. This request
    # can carry both fields at once (e.g. the first-run wizard flipping WiFi
    # client on and the AP off together), so the same race applies here that
    # motivated the boot-side fix.
    if "wifi_client_ssid" in data or "wifi_client_password" in data or "wifi_client_enabled" in data:
        apply_wifi_client_enabled(cfg.get("wifi_client_enabled", False),
                                   cfg.get("wifi_client_ssid", ""),
                                   cfg.get("wifi_client_password", ""))
    # ap_enabled before instance_name/ap_password: apply_ap_config's hostapd
    # restart is a no-op unless zerofi-ap.service is already active — enabling
    # the AP service first means the SSID/password get picked up in the same request
    if "ap_enabled" in data:
        # only worth the wait when we're actually trying to turn the AP off;
        # a no-op if WiFi was already connected before this request (the
        # first check succeeds immediately)
        if cfg.get("wifi_client_enabled", False) and cfg.get("ap_enabled", True) is False:
            for _ in range(20):
                if _wlan0_has_lan():
                    break
                time.sleep(1)
        if not apply_ap_enabled(cfg.get("ap_enabled", True)) and cfg.get("ap_enabled") is False:
            cfg["ap_enabled"] = True
            save_config(cfg)
    if "instance_name" in data:
        apply_instance_name(cfg.get("instance_name", ""))
    elif "ap_password" in data:
        apply_ap_config(cfg.get("instance_name", ""), cfg.get("ap_password", ""))
    if "timezone" in data:
        apply_timezone(cfg.get("timezone", ""))
    if "log_export" in data:
        apply_log_export(cfg.get("log_export", ""))
    if "ssh_enabled" in data:
        apply_ssh_enabled(cfg.get("ssh_enabled", False))
    if "airplay_enabled" in data:
        apply_airplay_enabled(cfg.get("airplay_enabled", True))
    if "bt_mode" in data:
        apply_bt_mode(cfg.get("bt_mode", "target"))
    if "palette" in data:
        apply_palette_to_mympd(cfg.get("palette", {}))
    return jsonify({"status": "ok"})


@app.route("/api/internal/mympd-resync", methods=["POST"])
@lan_only
def mympd_resync_api():
    """Periodic reconciliation target for heartbeat.sh, not the settings UI.

    myMPD's own Settings dialog does a full-replace of webuiSettings — any
    save through it, even something unrelated like the startpage, can drop
    whatever this call would otherwise preserve. Rather than detecting and
    patching each field myMPD's UI happens to clobber one at a time, this
    just re-runs the cheap (no restart) half of the palette push every
    heartbeat cycle so the whole blob stays reconciled. lan_only still lets
    127.0.0.1 through — see _is_from_ap()."""
    _push_mympd_webui_settings(load_config().get("palette", {}))
    return jsonify({"status": "ok"})


@app.route("/api/internal/mpd-update", methods=["POST"])
@lan_only
def mpd_update_api():
    """MPD library update target for sync-worker.py — see mpd_update_and_wait()
    for why this is a single serialized in-process call rather than
    sync-worker running `mpc update` itself. lan_only still lets 127.0.0.1
    through — see _is_from_ap()."""
    mpd_update_and_wait()
    return jsonify({"status": "ok"})


@app.route("/api/internal/wlan0-has-lan", methods=["GET"])
@lan_only
def wlan0_has_lan_api():
    """Single source of truth for "does wlan0 have a real LAN address" —
    heartbeat.sh queries this instead of re-deriving the address-parsing and
    subnet-exclusion rules in Bash. See _wlan0_has_lan(). lan_only still
    lets 127.0.0.1 through — see _is_from_ap()."""
    return jsonify({"has_lan": _wlan0_has_lan()})


@app.route("/api/wifi/status", methods=["GET"])
def api_wifi_status():
    return jsonify(get_wifi_status())


@app.route("/api/time", methods=["GET"])
def api_time():
    """Powers the toolbar clock — shows the Pi's own time, not the viewer's,
    so a wrong Pi timezone surfaces immediately."""
    now = datetime.now()
    try:
        tz = subprocess.run(["timedatectl", "show", "-p", "Timezone", "--value"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        tz = None
    return jsonify({
        "time": now.strftime("%H:%M"),
        "datetime_local": now.strftime("%Y-%m-%dT%H:%M"),
        "timezone": tz,
        "maintenance_active": is_maintenance_window(),
    })


@app.route("/api/datetime", methods=["POST"])
@lan_only
def api_datetime():
    data = request.get_json(force=True)
    apply_datetime(data.get("datetime", "").replace("T", " "))
    return jsonify({"status": "ok"})


@app.route("/api/config/backup", methods=["GET"])
@lan_only
def config_backup():
    """Download config, known BT devices, and authorized_keys as a restorable bundle."""
    payload = {
        "zerofi_config": load_config(),
        "known_bt_devices": load_known_devices(),
        "authorized_keys": AUTHORIZED_KEYS_FILE.read_text() if AUTHORIZED_KEYS_FILE.exists() else "",
    }
    body = json.dumps(payload, indent=2)
    return Response(body, mimetype="application/json", headers={
        "Content-Disposition": "attachment; filename=zerofi-config-backup.json",
    })


@app.route("/api/config/restore", methods=["POST"])
@lan_only
def config_restore():
    """Re-apply a previously downloaded backup. Re-runs apply_* calls (not
    just writes the JSON) since a restore after a reformat also needs to
    reinstate the running wpa_supplicant/hostapd/sshd state."""
    data = request.get_json(force=True)
    cfg_in = data.get("zerofi_config", data)
    defaults = load_config()
    cfg = dict(defaults)
    cfg.update({k: v for k, v in cfg_in.items() if k in defaults})

    # force AP on — a restore is exactly when WiFi creds might not work yet,
    # and guaranteeing a fallback access path is worth more than honoring
    # a stale "AP was off" from the backup
    cfg["ap_enabled"] = True
    save_config(cfg)

    # run every step regardless of earlier failures — one bad value should not
    # prevent the rest from landing; report which (if any) failed
    steps = [
        # ap_enabled before instance_name — same reasoning as save_config_api
        ("access point enabled", lambda: apply_ap_enabled(True)),
        ("instance name", lambda: apply_instance_name(cfg.get("instance_name", ""))),
        ("home WiFi", lambda: apply_wifi_client_enabled(cfg.get("wifi_client_enabled", False),
                                                          cfg.get("wifi_client_ssid", ""),
                                                          cfg.get("wifi_client_password", ""))),
        ("timezone", lambda: apply_timezone(cfg.get("timezone", ""))),
        ("log export", lambda: apply_log_export(cfg.get("log_export", ""))),
        ("SSH", lambda: apply_ssh_enabled(cfg.get("ssh_enabled", False))),
        ("AirPlay", lambda: apply_airplay_enabled(cfg.get("airplay_enabled", True))),
        ("Bluetooth mode", lambda: apply_bt_mode(cfg.get("bt_mode", "target"))),
    ]
    warnings = []
    for label, step in steps:
        try:
            step()
        except Exception as e:
            warnings.append(f"{label}: {e}")

    devices_in = data.get("known_bt_devices")
    if isinstance(devices_in, list):
        save_known_devices(devices_in)

    # empty string = no keys in backup; no-op rather than clobber existing keys
    keys = data.get("authorized_keys", "")
    lines = [l for l in keys.splitlines() if l.strip() and not l.strip().startswith("#")]
    if lines and any(l.strip().startswith(SSH_KEY_PREFIXES) for l in lines):
        AUTHORIZED_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        text = keys if keys.endswith("\n") else keys + "\n"
        tmp = AUTHORIZED_KEYS_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, AUTHORIZED_KEYS_FILE)
        AUTHORIZED_KEYS_FILE.chmod(0o600)

    if warnings:
        return jsonify({"status": "ok", "warnings": warnings})
    return jsonify({"status": "ok"})


@app.route("/api/factory-reset", methods=["POST"])
@lan_only
def factory_reset():
    """Delete the persistent config. Its absence triggers auto-configure-with-defaults
    on next start — no wizard, no separate mode flag."""
    CONFIG_FILE.unlink(missing_ok=True)
    KNOWN_DEVICES_FILE.unlink(missing_ok=True)
    return jsonify({"status": "ok"})


@app.route("/api/reboot", methods=["POST"])
@lan_only
def api_reboot():
    if not _DEV:
        subprocess.Popen(["/bin/bash", "-c", "sleep 1 && reboot"])
    return jsonify({"status": "ok"})


@app.route("/api/shutdown", methods=["POST"])
@lan_only
def api_shutdown():
    # no power button on this hardware — coming back requires a physical unplug
    if not _DEV:
        subprocess.Popen(["/bin/bash", "-c", "sleep 1 && poweroff"])
    return jsonify({"status": "ok"})


@app.route("/api/ssh/authorized-keys", methods=["POST"])
@lan_only
def set_authorized_keys():
    """Runtime authorized_keys editing, LAN-only.

    authorized_keys lives on root (ext4), not a FAT/exFAT partition — unlike
    most config it isn't recoverable by popping the SD card into a computer.
    A bad write here is a real lockout. Requiring at least one recognizable
    key line before accepting the write is a cheap guard against that."""
    data = request.get_json(force=True)
    keys = data.get("keys", "")
    lines = [l for l in keys.splitlines() if l.strip() and not l.strip().startswith("#")]
    if not lines or not any(l.strip().startswith(SSH_KEY_PREFIXES) for l in lines):
        return jsonify({"error": "no recognizable SSH public key found — refusing to write"}), 400
    AUTHORIZED_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = keys if keys.endswith("\n") else keys + "\n"
    tmp = AUTHORIZED_KEYS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, AUTHORIZED_KEYS_FILE)
    AUTHORIZED_KEYS_FILE.chmod(0o600)
    return jsonify({"status": "ok"})


@app.route("/api/library/sync", methods=["POST"])
@lan_only
def library_sync():
    """Kick off one cycle of sync work."""
    if not _DEV:
        subprocess.run(["systemctl", "start", "zerofi-sync-worker.service"],
                       capture_output=True, timeout=5)
    return jsonify({"status": "ok"})


@app.route("/api/library/discover", methods=["POST"])
@lan_only
def library_discover():
    """Trigger an immediate discovery run (walks SMB source, enqueues qualify jobs)."""
    if not _DEV:
        subprocess.run(["systemctl", "start", "zerofi-sync-discover.service"],
                       capture_output=True, timeout=5)
    return jsonify({"status": "ok"})


SYNC_QUEUE_DIR = Path("/run/zerofi/sync-queue")
_SYNC_QUEUE_PHASES = ("qualify", "compare", "sync", "prune")


@app.route("/api/library/status", methods=["GET"])
def library_status():
    """Polled by the settings Library Sync card for live queue depth.

    Queue directory contents are the only source of truth for sync progress —
    there used to also be a self-reported sync-status.json (state/percent/
    started_at/etc, written by sync-worker.py), but nothing ever read those
    fields; the frontend only ever used the queue counts below. Removed
    rather than kept as unread dead weight.

    No @lan_only — read-only."""
    queue = {}
    for phase in _SYNC_QUEUE_PHASES:
        try:
            queue[phase] = sum(1 for f in (SYNC_QUEUE_DIR / phase).iterdir()
                               if f.suffix == ".json")
        except OSError:
            queue[phase] = 0
    return jsonify({"queue": queue})


def _meaningful_rsync_error(output):
    """rsync failure output is a cascade — the real root cause appears early,
    followed by generic protocol-desync noise as rsync notices the remote side
    is gone. Prefer the first line matching a known-informative failure signature."""
    lines = [l for l in output.strip().split("\n") if l.strip()]
    if not lines:
        return None
    signatures = (
        "permission denied", "host key verification failed",
        "could not resolve hostname", "connection refused",
        "connection timed out", "no route to host", "operation timed out",
        "mount error", "failed to mount",
    )
    for line in lines:
        if any(sig in line.lower() for sig in signatures):
            return line
    return lines[-1]


@app.route("/api/library/test", methods=["POST"])
@lan_only
def library_test():
    """'Test Library Visibility' — proves the SMB source is mountable without
    walking the library's contents. Runs in the foreground since the whole
    point is showing the result. Shares sync-discover.py's own mount_smb()
    rather than a separate implementation — otherwise this isn't actually
    testing the same code path the real sync uses."""
    if _DEV:
        return jsonify({"status": "ok", "message": "Dev mode: reachability test skipped."})
    try:
        result = subprocess.run(["/opt/zerofi/sync-discover.py", "--test"],
                                 capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out after 30s — source may be slow to respond."}), 500

    output = result.stdout + result.stderr
    if result.returncode != 0:
        return jsonify({"error": _meaningful_rsync_error(output) or "Test failed (no output)"}), 500

    if "skipping" in output.lower():
        return jsonify({"error": "Nothing to test — is a source configured?"}), 400

    return jsonify({"status": "ok", "message": "Source is reachable."})


# ---------------------------------------------------------------------------
# Routes — Bluetooth
# ---------------------------------------------------------------------------

@app.route("/api/bt/scan", methods=["POST"])
@lan_only
def bt_scan():
    """Start BT discovery and return nearby devices."""
    try:
        with bt_locked():
            subprocess.run(["bluetoothctl", "--timeout", "10", "scan", "on"],
                           capture_output=True, timeout=12)
            result = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True, timeout=5)
        devices = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split(" ", 2)
            if len(parts) == 3 and parts[0] == "Device":
                mac, name = parts[1], parts[2]
                if name != mac:
                    devices.append({"mac": mac, "name": name})
        return jsonify({"devices": devices})
    except BtBusyError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bt/pair", methods=["POST"])
@lan_only
def bt_pair():
    """Pair with a device by MAC."""
    data = request.get_json(force=True)
    mac = data.get("mac", "")
    if not mac:
        return jsonify({"error": "no MAC"}), 400
    try:
        with bt_locked():
            subprocess.run(["bluetoothctl", "pair", mac], capture_output=True, timeout=15)
            subprocess.run(["bluetoothctl", "trust", mac], capture_output=True, timeout=5)
            subprocess.run(["bluetoothctl", "connect", mac], capture_output=True, timeout=10)
            result = subprocess.run(
                ["bluetoothctl", "info", mac], capture_output=True, text=True, timeout=5
            )
            name = mac
            for line in result.stdout.split("\n"):
                if "Name:" in line:
                    name = line.split("Name:")[1].strip()
                    break
            cfg = load_config()
            cfg["paired_bt_mac"] = mac
            cfg["paired_bt_name"] = name
            save_config(cfg)
            known = load_known_devices()
            if not any(d["mac"] == mac for d in known):
                known.append({
                    "name": name,
                    "mac": mac,
                    "alias": name,
                    "last_seen": int(time.time()),
                })
                save_known_devices(known)
            sink = wait_for_bt_sink(mac)
            if not sink:
                return jsonify({"error": "paired, but didn't connect as an audio device"}), 500
            _set_default_sink(sink["name"])
        return jsonify({"status": "ok", "name": name, "alias": name, "mac": mac})
    except BtBusyError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bt/connect", methods=["POST"])
@lan_only
def bt_connect():
    """Connect to a known device by MAC."""
    data = request.get_json(force=True)
    mac = data.get("mac", "")
    if not mac:
        return jsonify({"error": "no MAC"}), 400
    try:
        with bt_locked():
            result = subprocess.run(["bluetoothctl", "connect", mac], capture_output=True, text=True, timeout=10)
            sink = wait_for_bt_sink(mac)
        if not sink:
            detail = (result.stdout or result.stderr or "").strip().splitlines()
            reason = detail[-1] if detail else ""
            return jsonify({"error": "device did not connect" + (f" ({reason})" if reason else "")}), 500
        _set_default_sink(sink["name"])
        known = load_known_devices()
        dev = next((d for d in known if d["mac"].upper() == mac.upper()), None)
        return jsonify({"status": "ok", "alias": dev["alias"] if dev else mac, "mac": mac})
    except BtBusyError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bt/disconnect", methods=["POST"])
@lan_only
def bt_disconnect():
    """Disconnect a connected device by MAC — doesn't touch pairing/trust."""
    data = request.get_json(force=True)
    mac = data.get("mac", "")
    if not mac:
        return jsonify({"error": "no MAC"}), 400
    try:
        with bt_locked():
            subprocess.run(["bluetoothctl", "disconnect", mac], capture_output=True, timeout=10)
            expected_prefix = "bluez_output." + mac.replace(":", "_")
            for _ in range(5):
                sinks = get_sinks()
                if not any(s["name"].startswith(expected_prefix) for s in sinks):
                    _restore_dac_if_needed(sinks)
                    return jsonify({"status": "ok"})
                time.sleep(1)
        return jsonify({"error": "device did not disconnect"}), 500
    except BtBusyError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bt/known", methods=["POST"])
@lan_only
def bt_known_update():
    """Update alias or remove a known device."""
    data = request.get_json(force=True)
    known = load_known_devices()
    mac = data.get("mac", "")
    if data.get("action") == "remove":
        known = [d for d in known if d["mac"] != mac]
    elif data.get("alias"):
        for d in known:
            if d["mac"] == mac:
                d["alias"] = data["alias"]
                break
    save_known_devices(known)
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Routes — Audio Output
# ---------------------------------------------------------------------------

@app.route("/api/outputs", methods=["GET"])
def list_outputs():
    cfg = load_config()
    source = get_connected_bt_source()
    _handle_bt_source_connect(source)
    sinks = get_sinks()
    _restore_dac_if_needed(sinks)
    return jsonify({
        "options": get_output_options(),
        "bt_mode": cfg.get("bt_mode", "target"),
        "connected_bt_source": source,
        "current_sink": _pw_default_sink_name(pw_dump()),
    })


@app.route("/api/output/switch", methods=["POST"])
def switch_output():
    """Switch audio output. Accepts a live sink name, or a known device's
    MAC — the latter connects first (BT sink doesn't exist until connected),
    then switches to whichever sink appears."""
    data = request.get_json(force=True)
    sink_name = data.get("sink", "")
    mac = data.get("mac", "")

    if mac and not sink_name:
        try:
            with bt_locked():
                subprocess.run(["bluetoothctl", "connect", mac], capture_output=True, timeout=10)
                match = wait_for_bt_sink(mac)
        except BtBusyError as e:
            return jsonify({"error": str(e)}), 503
        if not match:
            return jsonify({"error": "device did not connect"}), 500
        sink_name = match["name"]

    if not sink_name:
        return jsonify({"error": "no sink"}), 400

    if not _set_default_sink(sink_name):
        return jsonify({"error": "failed to set sink"}), 500

    return jsonify({"status": "ok", "sink": sink_name})


# ---------------------------------------------------------------------------
# Routes — Toolbar (Normal Mode)
# ---------------------------------------------------------------------------

@app.route("/help")
def help_page():
    docs = Path(__file__).parent.parent / "docs" / "index.html"
    if not docs.exists():
        docs = Path("/opt/zerofi/docs/index.html")
    try:
        return docs.read_text(), 200, {"Content-Type": "text/html; charset=utf-8"}
    except OSError:
        return "Documentation not found", 404


@app.route("/toolbar")
def toolbar():
    cfg = load_config()
    outputs = get_output_options()
    mympd_url = os.environ.get("MYMPD_URL", f"http://{request.host.split(':')[0]}:8080/")
    show_settings = not (_wlan0_has_lan() and _is_from_ap(request.remote_addr))
    return render_template("toolbar.html", config=cfg, outputs=outputs, mympd_url=mympd_url,
                            show_settings=show_settings,
                            palette_vars=palette_css_vars(cfg.get("palette", {})))


@app.route("/settings")
@lan_only_page
def settings():
    cfg = load_config()
    known = load_known_devices()
    for d in known:
        d["role"] = "speaker" if _bt_device_is_sink(d["mac"]) else "phone"
    sinks = get_sinks()
    authorized_keys = AUTHORIZED_KEYS_FILE.read_text() if AUTHORIZED_KEYS_FILE.exists() else ""
    timezones = sorted(zoneinfo.available_timezones())
    current_datetime = datetime.now().strftime("%Y-%m-%dT%H:%M")
    connected_bt = get_connected_bt_device()
    return render_template("settings.html", config=cfg, known_devices=known, sinks=sinks,
                            authorized_keys=authorized_keys,
                            timezones=timezones, current_datetime=current_datetime,
                            connected_bt=connected_bt,
                            palette_vars=palette_css_vars(cfg.get("palette", {})))


# ---------------------------------------------------------------------------
# Captive portal catch-all (any unknown path → toolbar)
# ---------------------------------------------------------------------------

@app.route("/<path:path>")
def catch_all(path):
    return redirect(url_for("toolbar"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not _DEV and not is_configured():
        # First boot (or after factory reset) — auto-configure with defaults.
        # Expand the partition BEFORE writing config onto it — expand-music.sh
        # reformats it (no in-place exFAT resize tool exists), which would wipe
        # a config file written first.
        result = subprocess.run(["/opt/zerofi/expand-music.sh"],
                                 capture_output=True, text=True, timeout=60)
        if result.returncode == 75:
            # expand-music.sh rewrote the partition table; kernel won't see the
            # new size until a fresh boot — reboot now, it formats on the next run
            print("Music partition resized — rebooting to pick it up...", flush=True)
            subprocess.run(["systemctl", "reboot"])
            sys.exit(0)
        if result.returncode != 0:
            # do NOT save_config() on expand failure — config absence is what
            # makes is_configured() false, ensuring expand-music.sh gets retried
            # on the next boot rather than locking the box into "configured" with
            # the partition never actually expanded
            print(f"WARNING: music partition expand failed: {(result.stdout + result.stderr).strip()}", flush=True)
            cfg = load_config()
        else:
            cfg = load_config()
            save_config(cfg)
    else:
        cfg = load_config()
    # each boot-time apply_* is independently error-isolated — an exception here
    # is in __main__, not a request handler; it kills the whole process before
    # app.run() and takes down the admin UI needed to diagnose the failure
    try:
        apply_ap_config(cfg["instance_name"], cfg["ap_password"])
    except Exception as e:
        print(f"WARNING: apply_ap_config failed at boot: {e}", flush=True)
    try:
        # wifi client before ap_enabled: apply_ap_enabled(False) refuses to
        # disable the AP unless wlan0 already has a real LAN address (its
        # "don't cut your only path" guard) — joining home WiFi first gives
        # that guard a chance to see a successful join instead of always
        # seeing "no LAN yet" and silently keeping the AP on.
        apply_wifi_client_enabled(cfg.get("wifi_client_enabled", False),
                                  cfg.get("wifi_client_ssid", ""),
                                  cfg.get("wifi_client_password", ""))
    except Exception as e:
        print(f"WARNING: apply_wifi_client_enabled failed at boot: {e}", flush=True)
    try:
        # apply_wifi_client_enabled() above returns as soon as its subprocess
        # calls exit — it does not wait for the interface to actually finish
        # reassociating and get a lease. Restarting wpa_supplicant tears the
        # link down and rebuilds it (~3s+ on real hardware: deinit, associate,
        # WPA handshake, DHCP), so checking _wlan0_has_lan() immediately after
        # can still see "no LAN yet" even on a join that's about to succeed.
        # Give it a bounded window before the AP-disable guard evaluates it —
        # only worth the wait when we're actually trying to turn the AP off.
        if cfg.get("wifi_client_enabled", False) and cfg.get("ap_enabled", True) is False:
            for _ in range(20):
                if _wlan0_has_lan():
                    break
                time.sleep(1)
        # correct a lying config the same way save_config_api does: if the
        # guard still refused to disable the AP (join genuinely didn't get a
        # LAN address in time), the persisted value must reflect that the AP
        # is actually still on, not the value the user asked for.
        if not apply_ap_enabled(cfg.get("ap_enabled", True)) and cfg.get("ap_enabled") is False:
            cfg["ap_enabled"] = True
            save_config(cfg)
    except Exception as e:
        print(f"WARNING: apply_ap_enabled failed at boot: {e}", flush=True)
    try:
        apply_timezone(cfg.get("timezone", ""))
    except Exception as e:
        print(f"WARNING: apply_timezone failed at boot: {e}", flush=True)
    try:
        apply_bt_mode(cfg.get("bt_mode", "target"))
    except Exception as e:
        print(f"WARNING: apply_bt_mode failed at boot: {e}", flush=True)
    try:
        apply_ssh_enabled(cfg.get("ssh_enabled", False))
    except Exception as e:
        print(f"WARNING: apply_ssh_enabled failed at boot: {e}", flush=True)
    try:
        apply_airplay_enabled(cfg.get("airplay_enabled", False))
    except Exception as e:
        print(f"WARNING: apply_airplay_enabled failed at boot: {e}", flush=True)
    try:
        apply_log_export(cfg.get("log_export", ""))
    except Exception as e:
        print(f"WARNING: apply_log_export failed at boot: {e}", flush=True)
    try:
        if not _DEV:
            for _attempt in range(20):
                try:
                    urllib.request.urlopen("http://127.0.0.1:8080/", timeout=2)
                    break
                except Exception:
                    time.sleep(2)
        apply_palette_to_mympd(cfg.get("palette", {}))
    except Exception as e:
        print(f"WARNING: apply_palette_to_mympd failed at boot: {e}", flush=True)
    if not _DEV:
        # Backgrounded — don't hold up serving the web UI for a boot-time
        # library scan. Goes through the same serialized helper as every
        # other caller; see mpd_update_and_wait().
        threading.Thread(target=mpd_update_and_wait, daemon=True).start()
    port = int(os.environ.get("ZEROFI_PORT", "5000" if _DEV else "80"))
    app.run(host="0.0.0.0", port=port, threaded=True, debug=_DEV)
