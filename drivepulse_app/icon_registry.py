"""Icon registration helpers for DrivePulse."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gdk, Gio, Gtk  # noqa: E402

from .common import APP_ID
from .diagnostics import get_logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent
log = get_logger(__name__)


def build_icon_gresource(app_dir: Path | None = None) -> Path | None:
    """Compile icons.gresource.xml → icons.gresource if the source is newer."""
    app_dir = app_dir or PROJECT_ROOT
    src_xml = app_dir / "icons.gresource.xml"
    out_bin = app_dir / "icons.gresource"
    if not src_xml.exists():
        return None
    if out_bin.exists() and out_bin.stat().st_mtime >= src_xml.stat().st_mtime:
        return out_bin
    try:
        subprocess.run(
            ["glib-compile-resources", "--target", str(out_bin), str(src_xml)],
            cwd=str(app_dir),
            check=True,
            capture_output=True,
        )
        return out_bin
    except Exception:
        log.exception("Could not build icon gresource from %s", src_xml)
        return None


def register_local_icon(app_dir: Path | None = None) -> None:
    """Register bundled SVG icons via GResource + app PNG icon for window/taskbar."""
    app_dir = app_dir or PROJECT_ROOT
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())

    # Primary: GResource — icons compiled into a binary bundle at build/run time.
    gresource_bin = build_icon_gresource(app_dir)
    if gresource_bin is not None:
        try:
            resource = Gio.Resource.load(str(gresource_bin))
            Gio.resources_register(resource)
            theme.add_resource_path("/de/cais/DrivePulse/icons")
        except Exception:
            log.exception("Could not register icon gresource %s", gresource_bin)

    # Fallback: filesystem search path (requires icons/hicolor/index.theme).
    icons_dir = app_dir / "icons"
    if icons_dir.is_dir():
        theme.add_search_path(str(icons_dir))

    # App icon (PNG, for window/taskbar).
    local_icon = app_dir / "icon.png"
    if local_icon.exists():
        try:
            cache_dir = app_dir / ".icon-cache" / "hicolor" / "128x128" / "apps"
            cache_dir.mkdir(parents=True, exist_ok=True)
            dest = cache_dir / f"{APP_ID}.png"
            if not dest.exists() or dest.stat().st_mtime < local_icon.stat().st_mtime:
                shutil.copy2(local_icon, dest)
            hicolor_cache = cache_dir.parent.parent
            index = hicolor_cache / "index.theme"
            if not index.exists():
                index.write_text(
                    "[Icon Theme]\nName=hicolor\nHidden=true\n"
                    "Directories=128x128/apps\n\n"
                    "[128x128/apps]\nSize=128\nType=Fixed\n",
                    encoding="utf-8",
                )
            theme.add_search_path(str(hicolor_cache.parent))
        except Exception:
            log.exception("Could not register local app icon %s", local_icon)
