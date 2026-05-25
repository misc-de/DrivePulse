"""Inline CSS for map overlays — banner, steps panel, speed sign, coord chip."""
from __future__ import annotations

from gi.repository import Gdk, Gtk

# Inline CSS for the in-tour navigation banner.  Adwaita's ".osd"/".card" classes
# on a Box don't reliably paint a dark translucent background under the labels —
# we inject our own so the white text always reads against the map underneath.
_MANEUVER_CSS = b"""
.dp-maneuver-banner {
  background-color: rgba(20, 24, 32, 0.82);
  color: #ffffff;
  border-radius: 18px;
  padding: 16px 26px;
  box-shadow: 0 6px 16px rgba(0, 0, 0, 0.40);
}
.dp-maneuver-banner label { color: #ffffff; }
/* Symbolic icons recolor via the widget's CSS color - tint the arrows light
   blue so they pop against the dark banner without inheriting the label white. */
.dp-maneuver-banner image { color: #8FCFFF; }
.dp-maneuver-banner .dp-maneuver-distance {
  font-size: 32px;
  font-weight: 800;
}
.dp-maneuver-banner .dp-maneuver-instr {
  font-size: 20px;
  font-weight: 500;
  opacity: 0.95;
}
.dp-map-state {
  background-color: rgba(50, 50, 50, 0.80);
  color: #ffffff;
  border-radius: 8px;
  padding: 6px 10px;
  font-family: monospace;
  font-size: 13px;
}
.dp-map-state label { color: #ffffff; }
.dp-steps-panel {
  background-color: rgba(20, 24, 32, 0.82);
  border-radius: 14px;
  padding: 6px;
  box-shadow: 0 6px 16px rgba(0, 0, 0, 0.40);
}
.dp-steps-panel, .dp-steps-panel label { color: #f5f7fa; }
.dp-steps-panel list,
.dp-steps-panel list > row { background: transparent; }
.dp-steps-row { padding: 8px 10px; border-radius: 10px; }
.dp-steps-row image { color: #B6DEFF; }
.dp-steps-row-active { background-color: rgba(143, 207, 255, 0.30); }
.dp-steps-row-active label { color: #ffffff; }
.dp-steps-row-done { opacity: 0.65; }
.dp-steps-distance { font-weight: 700; color: #ffffff; }
.dp-steps-instr { opacity: 1.0; }
.dark .dp-steps-panel {
  background-color: rgba(8, 10, 14, 0.96);
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
}
.dp-tour-topnav { padding: 2px 4px; }
.dp-tour-topnav button label { font-size: 11px; }
/* Lane guidance row inside the maneuver banner */
.dp-lane-row { padding: 4px 12px 2px 12px; }
.dp-lane {
  border-radius: 8px;
  padding: 6px 8px;
  min-width: 42px;
  min-height: 42px;
}
.dp-lane image { color: rgba(255,255,255,0.28); }
.dp-lane-valid {
  background-color: rgba(30, 136, 229, 0.55);
}
.dp-lane-valid image { color: #ffffff; }
/* Green icon for the currently-loaded tour entry in Recent/Load-Tour lists */
.dp-tour-loaded-icon { color: #3db065; }
/* GPS coordinate chip - transparent icon, box background shows through */
.dp-coord-chip {
  background-color: rgba(20, 24, 32, 0.72);
  border-radius: 8px;
  padding: 3px 8px;
}
.dp-coord-chip label {
  color: #ffffff;
  font-family: monospace;
  font-size: 12px;
  background-color: transparent;
}
.dp-coord-chip image { color: rgba(255, 255, 255, 0.80); }
/* Route info card (duration + distance) - dark grey box matching OSD buttons */
.dp-route-info {
  background-color: rgba(20, 24, 32, 0.82);
  border-radius: 12px;
  padding: 6px 14px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.35);
}
.dp-route-info label {
  color: #ffffff;
  font-size: 15px;
  font-weight: 500;
}
/* Speed-limit sign - classic European round white/red circle */
.dp-speed-sign {
  background-color: #ffffff;
  border: 6px solid #cc0000;
  border-radius: 9999px;
  min-width: 130px;
  min-height: 130px;
  box-shadow: 0 3px 10px rgba(0,0,0,0.45);
}
.dp-speed-sign label {
  color: #111111;
  font-size: 54px;
  font-weight: 900;
  padding-top: 8px;
}
"""
_maneuver_css_installed = False


def _install_maneuver_css() -> None:
    global _maneuver_css_installed
    if _maneuver_css_installed:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_MANEUVER_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _maneuver_css_installed = True
