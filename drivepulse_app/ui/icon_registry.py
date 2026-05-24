"""Icon registration helpers for DrivePulse."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gdk, Gio, Gtk

from drivepulse_app.common import APP_ID
from drivepulse_app.diagnostics import get_logger

PROJECT_ROOT = Path(__file__).resolve().parents[2]
log = get_logger(__name__)


def build_icon_gresource(app_dir: Path | None = None) -> Path | None:
    """Compile icons.gresource.xml → icons.gresource if the source is newer.

    If glib-compile-resources is not installed (e.g. on mobile), the
    pre-compiled .gresource shipped with the app is used as-is.
    """
    app_dir = app_dir or PROJECT_ROOT
    icons_dir = app_dir / "icons"
    src_xml = icons_dir / "icons.gresource.xml"
    out_bin = icons_dir / "icons.gresource"
    if not src_xml.exists():
        return out_bin if out_bin.exists() else None
    if out_bin.exists() and out_bin.stat().st_mtime >= src_xml.stat().st_mtime:
        return out_bin
    try:
        subprocess.run(
            ["glib-compile-resources", "--target", str(out_bin), str(src_xml)],
            cwd=str(icons_dir),
            check=True,
            capture_output=True,
        )
        return out_bin
    except FileNotFoundError:
        # glib-compile-resources not available (typical on mobile); use the
        # pre-compiled bundle that was shipped with the app.
        if out_bin.exists():
            log.debug("glib-compile-resources not found — using pre-compiled %s", out_bin)
            return out_bin
        log.warning("glib-compile-resources not found and no pre-compiled %s exists", out_bin)
        return None
    except Exception:
        log.exception("Could not build icon gresource from %s", src_xml)
        return out_bin if out_bin.exists() else None


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
    local_icon = app_dir / "icons" / "icon.png"
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
