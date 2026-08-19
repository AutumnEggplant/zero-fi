// Zero-Fi — shared palette-derivation logic.
//
// Single source of truth for turning a 5-color palette (bg, c1-c4) into the
// full set of CSS custom properties the Flask-rendered pages (toolbar,
// settings) use. Formulas here MUST match flask_app/app.py's
// palette_css_vars() exactly — that Python function computes the same
// tokens server-side for the initial (pre-JS) page render, so the page
// doesn't flash unstyled before this script runs. If you change one, change
// the other. (myMPD's palette CSS is a separate, larger token set generated
// entirely server-side in app.py's _mympd_palette_css() — myMPD has no
// access to this script — so it isn't unified here.)

function hexLuma(hex) {
  const h = hex.replace('#', '');
  const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
  return (r * 299 + g * 587 + b * 114) / 255000;
}

function hexLighten(hex, amount) {
  const h = hex.replace('#', '');
  const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
  const d = Math.round(amount * 255);
  const clamp = v => Math.max(0, Math.min(255, v));
  return '#' + [r + d, g + d, b + d].map(v => clamp(v).toString(16).padStart(2, '0')).join('');
}

function hexToRgba(hex, alpha) {
  const h = hex.replace('#', '');
  const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function paletteTokens(p) {
  const luma = hexLuma(p.bg);
  const tokens = {
    '--bg': p.bg, '--c1': p.c1, '--c2': p.c2, '--c3': p.c3, '--c4': p.c4,
    '--border-dirty': p.c2, '--border-focus': p.c2,
    '--interactive': p.c2, '--interactive-text': '#fff',
    '--interactive-dim': hexToRgba(p.c2, luma > 0.5 ? 0.12 : 0.15),
  };
  if (luma > 0.5) {
    Object.assign(tokens, {
      '--surface': hexLighten(p.bg, -0.06), '--card': hexLighten(p.bg, 0.03),
      '--card-hi': hexLighten(p.bg, 0.07), '--text': p.c1,
      '--text-2': hexToRgba(p.c1, 0.58), '--text-3': hexToRgba(p.c1, 0.38),
      '--border': 'rgba(92,42,10,0.14)', '--border-hi': 'rgba(92,42,10,0.28)',
      '--interactive-hover': '#d05012',
      '--success': '#2a7830', '--success-text': '#184820',
      '--danger': '#c02020', '--danger-text': '#780808',
    });
  } else {
    Object.assign(tokens, {
      '--surface': hexLighten(p.bg, 0.06), '--card': hexLighten(p.bg, 0.11),
      '--card-hi': hexLighten(p.bg, 0.16), '--text': p.c4,
      '--text-2': hexToRgba(p.c4, 0.62), '--text-3': hexToRgba(p.c4, 0.36),
      '--border': 'rgba(240,236,248,0.12)', '--border-hi': 'rgba(240,236,248,0.28)',
      '--interactive-hover': hexLighten(p.c2, -0.05),
      '--success': '#4a9e40', '--success-text': '#c8f0c0',
      '--danger': '#cc3030', '--danger-text': '#f8c8c8',
    });
  }
  return tokens;
}

function applyPaletteVars(p) {
  const root = document.documentElement;
  for (const [k, v] of Object.entries(paletteTokens(p))) root.style.setProperty(k, v);
}
