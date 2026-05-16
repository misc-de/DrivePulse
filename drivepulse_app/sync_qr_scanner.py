"""GStreamer-based QR code scanner — adapted from Yaga (https://github.com/misc-de/Yaga)."""
from __future__ import annotations

from typing import Any, Callable


class QRScanError(RuntimeError):
    pass


def _gst() -> Any:
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError) as exc:
        raise QRScanError("GStreamer Python bindings not found (python3-gst-1.0)") from exc
    Gst.init(None)
    return Gst


def scan_supported() -> bool:
    try:
        Gst = _gst()
    except QRScanError:
        return False
    return (
        Gst.ElementFactory.find("autovideosrc") is not None
        and Gst.ElementFactory.find("zxing") is not None
    )


class WebcamQRScanner:
    """
    Live QR code scanner using GStreamer + the zxing element.
    Call build_widget() to get the GTK widget to embed, then start().
    on_success(text) is called on the GLib main loop when a code is found.
    on_error(message) is called on timeout or pipeline error.
    """

    def __init__(
        self,
        on_success: Callable[[str], None],
        on_error: Callable[[str], None],
        timeout_seconds: int = 120,
    ) -> None:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk
        self._GLib = GLib
        self._Gtk = Gtk
        self._Gst = _gst()
        self.on_success = on_success
        self.on_error = on_error
        self.timeout_seconds = timeout_seconds
        self._pipeline: Any = None
        self._bus: Any = None
        self._timeout_id: int | None = None
        self._finished = False

        self._picture = Gtk.Picture()
        self._picture.set_hexpand(True)
        self._picture.set_vexpand(True)
        self._picture.set_can_shrink(True)
        self._picture.set_size_request(-1, 200)

        self._status = Gtk.Label(label="", wrap=True, xalign=0.5)
        self._status.add_css_class("dim-label")

    def build_widget(self) -> Any:
        box = self._Gtk.Box(orientation=self._Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.append(self._picture)
        box.append(self._status)
        return box

    def start(self) -> None:
        self._finished = False
        self._stop_pipeline()
        self._build_pipeline()
        if self._pipeline is None:
            return
        result = self._pipeline.set_state(self._Gst.State.PLAYING)
        if result == self._Gst.StateChangeReturn.FAILURE:
            self._fail("Kamera konnte nicht gestartet werden")
            return
        if self._timeout_id is not None:
            self._GLib.source_remove(self._timeout_id)
        self._timeout_id = self._GLib.timeout_add_seconds(
            self.timeout_seconds, self._on_timeout
        )

    def cancel(self) -> None:
        self._finished = True
        self._stop_pipeline()

    def _build_pipeline(self) -> None:
        Gst = self._Gst
        if Gst.ElementFactory.find("autovideosrc") is None:
            self._fail("Keine Kamera gefunden (autovideosrc fehlt)")
            return
        if Gst.ElementFactory.find("zxing") is None:
            self._fail(
                "GStreamer zxing-Element fehlt.\n"
                "Installation: apt install gstreamer1.0-plugins-bad"
            )
            return

        has_preview = Gst.ElementFactory.find("gtk4paintablesink") is not None
        if has_preview:
            desc = (
                "autovideosrc ! videoconvert ! tee name=t "
                "t. ! queue leaky=downstream max-size-buffers=2 ! videoconvert "
                "    ! zxing message=true ! fakesink sync=false "
                "t. ! queue leaky=downstream max-size-buffers=2 ! videoconvert "
                "    ! gtk4paintablesink name=preview"
            )
        else:
            desc = "autovideosrc ! videoconvert ! zxing message=true ! fakesink sync=false"

        try:
            self._pipeline = Gst.parse_launch(desc)
        except Exception as exc:
            self._fail(f"Pipeline-Fehler: {exc}")
            return

        self._bus = self._pipeline.get_bus()
        if self._bus is not None:
            self._bus.add_signal_watch()
            self._bus.connect("message", self._on_message)

        if has_preview:
            sink = self._pipeline.get_by_name("preview")
            if sink is not None:
                try:
                    paintable = sink.get_property("paintable")
                    if paintable is not None:
                        self._picture.set_paintable(paintable)
                        self._status.set_text("QR-Code vor die Kamera halten")
                    else:
                        self._status.set_text("Kamera aktiv — kein Vorschau-Stream")
                except Exception:
                    self._status.set_text("Kamera aktiv")
        else:
            self._status.set_text("Kamera aktiv — QR-Code scannen…")

    def _on_message(self, _bus: Any, message: Any) -> None:
        if self._finished:
            return
        Gst = self._Gst
        if message.type == Gst.MessageType.ERROR:
            err, _dbg = message.parse_error()
            self._fail(f"Kamerafehler: {err}")
            return
        if message.type == Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            if structure is None or structure.get_name() != "barcode":
                return
            symbol = structure.get_value("symbol")
            if not symbol:
                return
            text = str(symbol).strip()
            if not text:
                return
            self._finished = True
            self._stop_pipeline()
            self.on_success(text)

    def _on_timeout(self) -> bool:
        if not self._finished:
            self._fail("Timeout — kein QR-Code erkannt")
        return False

    def _fail(self, message: str) -> None:
        if self._finished:
            return
        self._finished = True
        self._stop_pipeline()
        self.on_error(message)

    def _stop_pipeline(self) -> None:
        if self._timeout_id is not None:
            self._GLib.source_remove(self._timeout_id)
            self._timeout_id = None
        if self._bus is not None:
            try:
                self._bus.remove_signal_watch()
            except Exception:
                pass
            self._bus = None
        if self._pipeline is not None:
            try:
                self._pipeline.set_state(self._Gst.State.NULL)
            except Exception:
                pass
            self._pipeline = None
