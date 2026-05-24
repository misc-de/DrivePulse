#!/usr/bin/env python3
"""
OBD-II Dashboard auf GTK4 / libadwaita-Basis.

Funktionen:
- Verbindung zu einem ELM327/OBD-II-Dongle via python-OBD.
- Anzeige von Drehzahl, Geschwindigkeit und Kühlmitteltemperatur als Tachos.
- Querformat: drei Tachos nebeneinander.
- Hochformat: drei Tachos untereinander.
- Zusätzliche OBD-Werte werden in JSONL geschrieben, damit sie später leicht eingebaut werden können.
- Mock-Modus, falls kein Dongle oder python-OBD verfügbar ist.

Debian/Ubuntu-Abhängigkeiten:
  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-pip
  python3 -m pip install --user obd

Start:
  python3 drivepulse.py

Optional mit Port:
  OBD_PORT=/dev/rfcomm0 python3 drivepulse.py
  OBD_PORT=/dev/ttyUSB0 python3 drivepulse.py

Bluetooth-ELM327:
  Adapter zuerst per Bluetooth koppeln und als seriellen Port binden, z. B. /dev/rfcomm0.
  Danach: OBD_PORT=/dev/rfcomm0 python3 drivepulse.py
"""

from __future__ import annotations

import fcntl
import os
import signal
import sys
import time
from pathlib import Path

# Register bundled icons via XDG_DATA_DIRS **before** GTK is imported so the
# icon theme engine picks them up on its first initialisation pass.
# icons/hicolor/index.theme tells GTK which sub-directories contain SVGs.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_APP_DIR = str(_PROJECT_ROOT)
_xdg_data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
if _APP_DIR not in _xdg_data_dirs.split(":"):
    os.environ["XDG_DATA_DIRS"] = f"{_APP_DIR}:{_xdg_data_dirs}"

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from drivepulse_app.common import (
    APP_ID,
    SETTINGS_FILE,
    THEMES_DIR,
    _detect_language,
    _make_label_responsive,
    _normalize_language,
    _translate,
)
from drivepulse_app.ui.gauge import Gauge, GAUGE_THEMES, load_user_themes
from drivepulse_app.dashboard.page import DashboardCanvas, DASHBOARD_THEMES
from drivepulse_app.stopwatch.page import StopWatchPage
from drivepulse_app.dashboard.window import DashboardWindow
from drivepulse_app.ui.icon_registry import register_local_icon
from drivepulse_app.obd.reader import ObdReader
from drivepulse_app.startup_info import get_missing_required, print_required_python_packages
from drivepulse_app.diagnostics import get_logger


log = get_logger(__name__)


def _build_missing_deps_window(
    app: "ObdDashboardApp",
    missing: list[tuple[str, str, str]],
) -> Adw.ApplicationWindow:
    win = Adw.ApplicationWindow(application=app)
    win.set_default_size(480, 360)
    win.set_title("DrivePulse")

    toolbar_view = Adw.ToolbarView()
    toolbar_view.add_top_bar(Adw.HeaderBar())

    # Compact header: warning icon on the left, title + short description on
    # the right. Replaces Adw.StatusPage which centres a huge icon block.
    header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
    header.set_margin_top(16)
    header.set_margin_start(16)
    header.set_margin_end(16)
    header.set_margin_bottom(8)

    icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
    icon.set_pixel_size(36)
    icon.set_valign(Gtk.Align.START)
    header.append(icon)

    text_block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    text_block.set_hexpand(True)
    text_block.set_valign(Gtk.Align.CENTER)
    title_lbl = Gtk.Label(label="Fehlende Abhängigkeiten", xalign=0)
    title_lbl.add_css_class("title-3")
    desc_lbl = Gtk.Label(
        label="Einige Pakete fehlen. Installiere sie und starte die App erneut.",
        xalign=0,
        wrap=True,
    )
    desc_lbl.add_css_class("dim-label")
    desc_lbl.add_css_class("caption")
    text_block.append(title_lbl)
    text_block.append(desc_lbl)
    header.append(text_block)

    list_box = Gtk.ListBox()
    list_box.set_selection_mode(Gtk.SelectionMode.NONE)
    list_box.add_css_class("boxed-list")
    list_box.set_margin_start(16)
    list_box.set_margin_end(16)
    list_box.set_margin_top(8)
    list_box.set_margin_bottom(16)

    for _name, cmd, desc in missing:
        row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        row_box.set_margin_start(12)
        row_box.set_margin_end(12)
        row_box.set_margin_top(8)
        row_box.set_margin_bottom(8)

        desc_label = Gtk.Label(label=desc, xalign=0, wrap=True)
        desc_label.add_css_class("dim-label")
        cmd_label = Gtk.Label(label=cmd, selectable=True, xalign=0, wrap=True)
        cmd_label.add_css_class("monospace")
        cmd_label.add_css_class("caption")

        row_box.append(desc_label)
        row_box.append(cmd_label)
        list_box.append(row_box)

    content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    content_box.append(header)
    content_box.append(list_box)

    scroll = Gtk.ScrolledWindow(vexpand=True)
    scroll.set_child(content_box)
    toolbar_view.set_content(scroll)

    # Button bar
    btn_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    btn_bar.set_margin_start(16)
    btn_bar.set_margin_end(16)
    btn_bar.set_margin_top(12)
    btn_bar.set_margin_bottom(12)
    btn_bar.set_halign(Gtk.Align.FILL)
    btn_bar.set_homogeneous(True)

    cancel_btn = Gtk.Button(label="Abbrechen", hexpand=True)
    cancel_btn.connect("clicked", lambda _: app.quit())

    continue_btn = Gtk.Button(label="Trotzdem starten", hexpand=True)
    continue_btn.add_css_class("suggested-action")

    def _on_continue(_btn: Gtk.Button) -> None:
        win.close()
        app.window = DashboardWindow(app)
        app.window.present()

    continue_btn.connect("clicked", _on_continue)

    btn_bar.append(cancel_btn)
    btn_bar.append(continue_btn)
    toolbar_view.add_bottom_bar(btn_bar)

    win.set_content(toolbar_view)
    return win


class ObdDashboardApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        self.window: DashboardWindow | None = None

    def do_activate(self) -> None:
        register_local_icon()
        load_user_themes(THEMES_DIR)
        if self.window is None:
            missing = get_missing_required()
            if missing:
                log.warning("Missing required dependencies: %s", [m[0] for m in missing])
                self.window = _build_missing_deps_window(self, missing)
            else:
                self.window = DashboardWindow(self)
        self.window.present()


_lock_fh: "object | None" = None


def _acquire_lock() -> bool:
    """Returns True if this is the only running instance, False otherwise."""
    global _lock_fh
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    lock_path = Path(runtime_dir) / "drivepulse.lock"
    try:
        fh = lock_path.open("w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(os.getpid()))
        fh.flush()
        _lock_fh = fh  # keep reference so lock lives as long as this process
        return True
    except (BlockingIOError, OSError):
        return False


def main() -> int:
    log.info("DrivePulse startup pid=%s argv=%s cwd=%s", os.getpid(), sys.argv, os.getcwd())
    print_required_python_packages()
    if not _acquire_lock():
        log.info("DrivePulse startup refused because another instance holds the lock")
        print("DrivePulse is already running.", file=sys.stderr)
        return 1
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = ObdDashboardApp()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
