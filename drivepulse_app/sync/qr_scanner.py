"""GStreamer-based webcam QR scanner — based on HA-Matter/qr_tools.py."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from drivepulse_app.common import _translate
from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)

_GSTREAMER_ERRORS = (AttributeError, TypeError, RuntimeError)


class QRScanError(RuntimeError):
    pass


def _import_gstreamer() -> Any:
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError) as exc:
        raise QRScanError("GStreamer Python bindings not found (python3-gst-1.0)") from exc
    try:
        Gst.init(None)
    except _GSTREAMER_ERRORS as exc:
        raise QRScanError("GStreamer could not be initialised") from exc
    return Gst


def _element_available(Gst: Any, name: str) -> bool:
    try:
        return Gst.ElementFactory.find(name) is not None
    except _GSTREAMER_ERRORS:
        log.debug("Could not query Gst element factory %s", name, exc_info=True)
        return False


def scan_supported() -> bool:
    try:
        Gst = _import_gstreamer()
    except QRScanError:
        return False
    return _element_available(Gst, "autovideosrc") and _element_available(Gst, "zxing")


class WebcamQRScanner:
    def __init__(
        self,
        on_success: Callable[[str], None],
        on_error: Callable[[str], None],
        language: str = "de",
        timeout_seconds: int = 120,
        filter_fn: Callable[[str], bool] | None = None,
    ) -> None:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk
        self._GLib = GLib
        self._Gtk = Gtk
        self._Gst = _import_gstreamer()
        self._language = language
        self.on_success = on_success
        self.on_error = on_error
        self.timeout_seconds = max(1, int(timeout_seconds))
        self._pipeline: Any = None
        self._bus: Any = None
        self._timeout_id: int | None = None
        self._filter_fn = filter_fn
        self._finished = False

        self._picture = Gtk.Picture()
        self._picture.set_hexpand(True)
        self._picture.set_vexpand(True)
        self._picture.set_can_shrink(True)
        self._picture.set_size_request(-1, 200)

        self._status = Gtk.Label(label="", wrap=True, xalign=0.5)
        self._status.add_css_class("dim-label")

    def _t(self, key: str, **kw: Any) -> str:
        return _translate(self._language, key, **kw)

    def build_widget(self) -> Any:
        box = self._Gtk.Box(orientation=self._Gtk.Orientation.VERTICAL, spacing=8)
        box.append(self._picture)
        box.append(self._status)
        return box

    def start(self) -> None:
        self._finished = False
        self._stop_pipeline()
        try:
            self._build_pipeline()
        except QRScanError as exc:
            self._fail(str(exc))
            return
        if self._pipeline is None:
            return
        result = self._pipeline.set_state(self._Gst.State.PLAYING)
        if result == self._Gst.StateChangeReturn.FAILURE:
            self._fail(self._t("sync.scanner.start_failed"))
            return
        if self._timeout_id is not None:
            self._GLib.source_remove(self._timeout_id)
        self._timeout_id = self._GLib.timeout_add_seconds(
            self.timeout_seconds, self._on_timeout
        )

    def cancel(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._stop_pipeline()

    def _build_pipeline(self) -> None:
        Gst = self._Gst
        if not _element_available(Gst, "autovideosrc"):
            raise QRScanError(self._t("sync.scanner.no_camera"))
        if not _element_available(Gst, "zxing"):
            raise QRScanError(self._t("sync.scanner.no_zxing"))

        has_preview = _element_available(Gst, "gtk4paintablesink")
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
        except _GSTREAMER_ERRORS as exc:
            raise QRScanError(self._t("sync.scanner.pipeline_failed", error=str(exc))) from exc

        self._bus = self._pipeline.get_bus()
        if self._bus is not None:
            self._bus.add_signal_watch()
            self._bus.connect("message", self._on_bus_message)

        if has_preview:
            sink = self._pipeline.get_by_name("preview")
            if sink is not None:
                try:
                    paintable = sink.get_property("paintable")
                    if paintable is not None:
                        self._picture.set_paintable(paintable)
                        self._status.set_text(self._t("sync.scanner.hold_qr"))
                    else:
                        self._status.set_text(self._t("sync.scanner.active_no_preview"))
                except _GSTREAMER_ERRORS:
                    self._status.set_text(self._t("sync.scanner.active"))
        else:
            self._status.set_text(self._t("sync.scanner.active"))

    def _on_bus_message(self, _bus: Any, message: Any) -> None:
        if self._finished:
            return
        Gst = self._Gst
        if message.type == Gst.MessageType.ERROR:
            try:
                err, debug = message.parse_error()
            except _GSTREAMER_ERRORS as exc:
                log.exception("Could not parse GStreamer webcam error")
                self._fail(self._t("sync.scanner.camera_error", error=exc))
                return
            log.error("GStreamer webcam error: %s — %s", err, debug)
            self._fail(self._t("sync.scanner.camera_error", error=err))
            return
        if message.type == Gst.MessageType.ELEMENT:
            try:
                structure = message.get_structure()
            except _GSTREAMER_ERRORS:
                log.debug("Could not read GStreamer barcode message", exc_info=True)
                return
            if structure is None or structure.get_name() != "barcode":
                return
            try:
                symbol = structure.get_value("symbol")
            except _GSTREAMER_ERRORS:
                log.debug("Could not read barcode symbol from GStreamer structure", exc_info=True)
                return
            if not symbol:
                log.warning("barcode message without readable text: %s", structure.to_string())
                return
            text = str(symbol).strip()
            if not text:
                return
            log.info("QR code read: %s…", text[:80])
            if self._filter_fn and not self._filter_fn(text):
                log.info("QR code ignored (no match)")
                return
            log.info("QR code accepted")
            self._finished = True
            self._stop_pipeline()
            self.on_success(text)

    def _on_timeout(self) -> bool:
        if not self._finished:
            self._fail(self._t("sync.scanner.timeout"))
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
            except _GSTREAMER_ERRORS:
                log.debug("Could not remove Gst bus signal watch", exc_info=True)
            self._bus = None
        if self._pipeline is not None:
            try:
                self._pipeline.set_state(self._Gst.State.NULL)
            except _GSTREAMER_ERRORS:
                log.debug("Could not reset Gst pipeline to NULL", exc_info=True)
            self._pipeline = None
