"""QR code generator using system qrencode."""
from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gi.repository import GdkPixbuf


def make_svg(text: str, module_size: int = 8, margin: int = 4) -> str:
    result = subprocess.run(
        ["qrencode", "-t", "SVG", "-o", "-", "-s", str(module_size), "-m", str(margin), "-l", "M", "--", text],
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("utf-8")


def make_pixbuf(text: str) -> GdkPixbuf.Pixbuf:
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
    svg = make_svg(text)
    data = svg.encode("utf-8")
    loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
    loader.write(data)
    loader.close()
    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        raise RuntimeError("GdkPixbuf konnte QR-SVG nicht rendern (librsvg fehlt?)")
    return pixbuf
