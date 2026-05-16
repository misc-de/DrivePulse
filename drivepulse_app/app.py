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

import os
import signal
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

from .common import (
    APP_ID,
    SETTINGS_FILE,
    THEMES_DIR,
    _detect_language,
    _make_label_responsive,
    _normalize_language,
    _translate,
)
from .gauge import Gauge, GAUGE_THEMES, load_user_themes
from .dashboard import DashboardCanvas, DASHBOARD_THEMES
from .acceleration import AccelerationPage
from .dashboard_window import DashboardWindow
from .icon_registry import register_local_icon
from .obd_reader import ObdReader
from .startup_info import print_required_python_packages



class ObdDashboardApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        self.window: DashboardWindow | None = None

    def do_activate(self) -> None:
        register_local_icon()
        load_user_themes(THEMES_DIR)
        if self.window is None:
            self.window = DashboardWindow(self)
        self.window.present()


def main() -> int:
    print_required_python_packages()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = ObdDashboardApp()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
