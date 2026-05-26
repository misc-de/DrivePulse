"""Settings dialog callbacks for the text-to-speech section: backend / voice /
volume rows plus the piper-model download progress."""
from __future__ import annotations

from typing import Any

from gi.repository import Adw, Gtk

from drivepulse_app.tts import service as tts_service


class SettingsTtsMixin:
    def _on_tts_enabled_toggled(self, row: Adw.SwitchRow, _param: Any) -> None:
        if self.on_tts_enabled_changed is not None:
            self.on_tts_enabled_changed(row.get_active())

    def _on_tts_backend_selected(self, *_args: Any) -> None:
        idx = self.tts_backend_row.get_selected()
        backend = self._TTS_BACKENDS[idx] if 0 <= idx < len(self._TTS_BACKENDS) else "espeak"
        piper = backend == "piper"
        self.tts_language_row.set_visible(piper)
        self.tts_voice_row.set_visible(piper)
        self.tts_quality_row.set_visible(piper)
        if self.on_tts_backend_changed is not None:
            self.on_tts_backend_changed(backend)

    def _on_tts_language_selected(self, *_args: Any) -> None:
        if self.on_tts_language_changed is not None:
            idx = self.tts_language_row.get_selected()
            lang = self._TTS_LANGUAGES[idx] if 0 <= idx < len(self._TTS_LANGUAGES) else "auto"
            self.on_tts_language_changed(lang)

    def _on_tts_voice_selected(self, *_args: Any) -> None:
        if self.on_tts_voice_changed is not None:
            idx = self.tts_voice_row.get_selected()
            voice = self._TTS_VOICES[idx] if 0 <= idx < len(self._TTS_VOICES) else "female"
            self.on_tts_voice_changed(voice)

    def _on_tts_quality_selected(self, *_args: Any) -> None:
        if self.on_tts_quality_changed is not None:
            idx = self.tts_quality_row.get_selected()
            quality = self._TTS_QUALITIES[idx] if 0 <= idx < len(self._TTS_QUALITIES) else "high"
            self.on_tts_quality_changed(quality)

    def _on_tts_volume_changed(self, *_args: Any) -> None:
        if self.on_tts_volume_pct_changed is not None:
            self.on_tts_volume_pct_changed(int(self.tts_volume_row.get_value()))

    def _on_tts_duck_pct_changed(self, *_args: Any) -> None:
        if self.on_tts_duck_pct_changed is not None:
            self.on_tts_duck_pct_changed(int(self.tts_duck_row.get_value()))

    def _on_tts_duck_pre_ms_changed(self, *_args: Any) -> None:
        if self.on_tts_duck_pre_ms_changed is not None:
            self.on_tts_duck_pre_ms_changed(int(self.tts_duck_pre_row.get_value()))

    def _on_piper_dl_progress(self, model_name: str, fraction: float) -> None:
        """Callback from tts_service — runs on GLib main loop."""
        if fraction == 2.0:
            self._piper_dl_row.set_visible(False)
            self._piper_dl_bar.set_fraction(0.0)
            self._piper_dl_bar.set_text(None)
        elif fraction == -1.0:
            self._piper_dl_row.set_visible(False)
            self._piper_dl_bar.set_text(None)
        else:
            self._piper_dl_row.set_title(f"Piper: {model_name}")
            self._piper_dl_bar.set_fraction(max(0.0, min(1.0, fraction)))
            pct = int(fraction * 100)
            self._piper_dl_bar.set_text(f"{pct} %")
            self._piper_dl_row.set_visible(True)

    def _on_piper_dl_cancel(self, _btn: Gtk.Button) -> None:
        for model in tts_service.active_downloads():
            tts_service.cancel_download(model)
