"""Piper TTS model-download progress overlay for the main dashboard window.

A small OSD bar with model name + progress bar + cancel button that shows
while a Piper voice model is being downloaded in the background.
``tts_service.set_download_callback`` is wired to ``_on_piper_dl_progress``
elsewhere in :class:`DashboardWindow.__init__`; this mixin provides the
widget construction and progress-update handling.
"""
from __future__ import annotations

from gi.repository import Adw, Gtk


class DashboardPiperOverlayMixin:
    """Build + drive the Piper download-progress overlay."""

    def _build_piper_dl_overlay(self) -> Gtk.Box:
        """Build the Piper download-progress overlay widget (initially hidden)."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.START)
        box.set_margin_top(12)
        box.add_css_class("osd")
        box.add_css_class("piper-dl-overlay")

        # Custom CSS for rounded corners + padding — applied lazily on first show.
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(b".piper-dl-overlay { border-radius: 14px; padding: 10px 16px; }")
        self._piper_dl_css_provider = css_provider
        self._piper_dl_css_installed = False

        icon = Gtk.Image(icon_name="emblem-downloads-symbolic")
        box.append(icon)

        self._piper_dl_label = Gtk.Label(label="Piper: …")
        box.append(self._piper_dl_label)

        self._piper_dl_bar = Gtk.ProgressBar()
        self._piper_dl_bar.set_size_request(140, -1)
        box.append(self._piper_dl_bar)

        self._piper_dl_cancel_btn = Gtk.Button(icon_name="process-stop-symbolic")
        self._piper_dl_cancel_btn.add_css_class("flat")
        self._piper_dl_cancel_btn.set_tooltip_text("Download abbrechen")
        box.append(self._piper_dl_cancel_btn)

        box.set_visible(False)
        self._piper_dl_overlay = box
        return box

    def _on_piper_dl_progress(self, model_name: str, fraction: float) -> bool:
        """Handle Piper download progress updates (called via GLib.idle_add from bg thread)."""
        if fraction >= 0.0 and fraction <= 1.0:
            # Active download progress
            self._piper_dl_current_model = model_name
            self._piper_dl_label.set_text(f"Piper: {model_name}")
            self._piper_dl_bar.set_fraction(fraction)
            # Re-wire cancel button to current model
            try:
                self._piper_dl_cancel_btn.disconnect_by_func(self._piper_dl_cancel_clicked)
            except Exception:
                pass
            self._piper_dl_cancel_btn.connect("clicked", self._piper_dl_cancel_clicked)
            self._piper_dl_overlay.set_visible(True)
            # Install CSS on first show if not yet realized
            if not getattr(self, "_piper_dl_css_installed", False):
                try:
                    display = self.get_display()
                    Gtk.StyleContext.add_provider_for_display(
                        display,
                        self._piper_dl_css_provider,
                        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
                    )
                    self._piper_dl_css_installed = True
                except Exception:
                    pass
        elif fraction == -1.0:
            # Cancelled or error
            self._piper_dl_overlay.set_visible(False)
            self._piper_dl_current_model = None
        elif fraction == 2.0:
            # Done
            self._piper_dl_overlay.set_visible(False)
            self._piper_dl_current_model = None
            try:
                self.add_toast(Adw.Toast(title="Piper bereit ✓"))
            except Exception:
                pass
        return False

    def _piper_dl_cancel_clicked(self, _btn: Gtk.Button) -> None:
        """Cancel the current Piper model download."""
        from drivepulse_app.tts import service as _tts_svc
        model = self._piper_dl_current_model
        if model:
            _tts_svc.cancel_download(model)
