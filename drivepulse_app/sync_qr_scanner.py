"""GStreamer-based webcam QR scanner — based on HA-Matter/qr_tools.py."""
from __future__ import annotations

from typing import Any, Callable

from .common import _translate
from .diagnostics import get_logger

log = get_logger(__name__)


class QRScanError(RuntimeError):
    pass


def _import_gstreamer() -> Any:
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
        Gst = _import_gstreamer()
    except QRScanError:
        return False
    return (
        Gst.ElementFactory.find("autovideosrc") is not None
        and Gst.ElementFactory.find("zxing") is not None
    )


def _barcode_text_candidates(barcode: Any) -> list[str]:
    candidates: list[str] = []
    for attr in ("text", "bytes", "data", "content"):
        value = getattr(barcode, attr, None)
        if value is None:
            continue
        if isinstance(value, bytes):
            decoded = value.decode("utf-8", errors="ignore").strip()
        else:
            decoded = str(value).strip()
        if decoded:
            candidates.append(decoded)
    return candidates


class WebcamQRScanner:
    def __init__(
        self,
        on_success: Callable[[str], None],
        on_error: Callable[[str], None],
        language: str = "de",
        timeout_seconds: int = 120,
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
        if Gst.ElementFactory.find("autovideosrc") is None:
            raise QRScanError(self._t("sync.scanner.no_camera"))
        if Gst.ElementFactory.find("zxing") is None:
            raise QRScanError(self._t("sync.scanner.no_zxing"))

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
            raise QRScanError(self._t("sync.scanner.pipeline_failed", error=str(exc))) from exc

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
                        self._status.set_text(self._t("sync.scanner.hold_qr"))
                    else:
                        self._status.set_text(self._t("sync.scanner.active_no_preview"))
                except Exception:
                    self._status.set_text(self._t("sync.scanner.active"))
        else:
            self._status.set_text(self._t("sync.scanner.active"))

    def _on_message(self, _bus: Any, message: Any) -> None:
        if self._finished:
            return
        Gst = self._Gst
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            log.error("GStreamer webcam error: %s — %s", err, debug)
            self._fail(self._t("sync.scanner.camera_error", error=err))
            return
        if message.type == Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            if structure is None:
                return
            sname = structure.get_name()
            if sname != "barcode":
                log.debug("GStreamer ELEMENT message: %s", sname)
                return
            # "symbol" ist in gst-plugins-bad ein G_TYPE_STRING; get_string() ist
            # in PyGObject zuverlässiger als get_value() für String-Felder.
            text = ""
            for field in ("symbol", "text"):
                try:
                    val = structure.get_string(field)
                    # PyGObject gibt get_string als reinen str zurück (oder None)
                    if isinstance(val, str) and val.strip():
                        text = val.strip()
                        break
                except Exception:
                    pass
            if not text:
                # Fallback über get_value für ältere Bindungen
                raw = structure.get_value("symbol")
                if raw:
                    candidates = _barcode_text_candidates(raw) or [str(raw).strip()]
                    text = candidates[0].strip() if candidates else ""
            if not text:
                log.warning("barcode message ohne lesbaren Text: %s", structure.to_string())
                return
            log.info("QR-Code erkannt: %s…", text[:60])
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
            except Exception:
                pass
            self._bus = None
        if self._pipeline is not None:
            try:
                self._pipeline.set_state(self._Gst.State.NULL)
            except Exception:
                pass
            self._pipeline = None
