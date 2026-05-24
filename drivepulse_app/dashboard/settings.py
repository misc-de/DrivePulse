"""Settings and dialog callbacks for the dashboard window."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from drivepulse_app.sync.dialog import SyncDialog

from drivepulse_app.app_settings import load_settings, save_settings
from drivepulse_app.common import LOG_DIR, _detect_language, _normalize_language, _translate
from drivepulse_app.dashboard.page import DASHBOARD_THEMES
from drivepulse_app.diagnostics import get_logger, set_log_enabled
from drivepulse_app.settings.dialog import SettingsDialog
from drivepulse_app.ui.gauge import load_user_themes

log = get_logger(__name__)


class DashboardSettingsMixin:
    # Declared so mypy treats the concrete DashboardWindow.last_update_check
    # (str | None) as compatible with what _set_last_update_check assigns.
    last_update_check: str | None

    def _load_settings(self) -> dict[str, Any]:
        return load_settings()

    def _load_units(self) -> str:
        return self._load_settings()["units"]

    def _save_settings(self) -> None:
        try:
            save_settings({
                "units": getattr(self, "units", "metric"),
                "language": getattr(self, "language", _detect_language()),
                "mock_mode": getattr(self, "mock_mode", False),
                "obd_port": getattr(self, "obd_port", None),
                "gauge_theme": getattr(self, "gauge_theme", "cockpit"),
                "sidebar_side": getattr(self, "sidebar_side", "left"),
                "engage_threshold": getattr(self, "engage_threshold", 0.20),
                "theme_mode": getattr(self, "theme_mode", "auto"),
                "force_webkit_map": getattr(self, "force_webkit_map", False),
                "map_traffic_visible": getattr(self, "map_traffic_visible", False),
                "map_traffic_bundesweit": getattr(self, "map_traffic_bundesweit", True),
                "map_traffic_nrw": getattr(self, "map_traffic_nrw", False),
                "map_3d_view": getattr(self, "map_3d_view", True),
                "last_update_check": getattr(self, "last_update_check", None),
                "dashcam_camera": getattr(self, "dashcam_camera", "/dev/video0"),
                "dashcam_resolution": getattr(self, "dashcam_resolution", "1280x720"),
                "dashcam_fps": getattr(self, "dashcam_fps", 25),
                "dashcam_seg_minutes": getattr(self, "dashcam_seg_minutes", 3),
                "dashcam_max_segments": getattr(self, "dashcam_max_segments", 10),
                "dashcam_dim_timeout": getattr(self, "dashcam_dim_timeout", 30),
                "dashcam_rolling_dir": getattr(self, "dashcam_rolling_dir", ""),
                "dashcam_saved_dir": getattr(self, "dashcam_saved_dir", ""),
                "nav_position": getattr(self, "nav_position", "bottom"),
                "dashcam_gps_osd": getattr(self, "dashcam_gps_osd", False),
                "dashcam_speed_osd": getattr(self, "dashcam_speed_osd", False),
                "rotation_mode": getattr(self, "rotation_mode", "follow_sensor"),
                "tts_enabled": getattr(self, "tts_enabled", False),
                "tts_backend": getattr(self, "tts_backend", "espeak"),
                "tts_language": getattr(self, "tts_language", "auto"),
                "tts_voice": getattr(self, "tts_voice", "female"),
                "tts_quality": getattr(self, "tts_quality", "high"),
                "log_app_enabled": getattr(self, "log_app_enabled", True),
                "log_obd_enabled": getattr(self, "log_obd_enabled", True),
                "vindecoder_api_key": getattr(self, "vindecoder_api_key", ""),
                "vindecoder_secret_key": getattr(self, "vindecoder_secret_key", ""),
                "autodev_api_key": getattr(self, "autodev_api_key", ""),
                "last_cars_source": getattr(self, "last_cars_source", None),
                "last_cars_category": getattr(self, "last_cars_category", None),
                "last_cars_scan_id": getattr(self, "last_cars_scan_id", None),
            })
        except Exception:
            log.exception("Could not save dashboard settings")

    def _on_engage_threshold_changed(self, value: float) -> None:
        self.engage_threshold = value
        self._save_settings()

    def _on_stopwatch_run_complete(self, results: dict, samples: list) -> bool:
        trip_recorder = getattr(self, "trip_recorder", None)
        car_id = trip_recorder.car_id if trip_recorder else None
        if car_id is None:
            return False
        try:
            self.db.add_stopwatch_run(
                car_id=car_id,
                results=results,
                samples=samples,
                lat=self._last_gps_lat,
                lon=self._last_gps_lon,
            )
            self.cars_page.refresh_if_showing_car(car_id)
            return True
        except Exception:
            log.exception("Could not persist stopwatch run")
            return False

    def _save_units(self) -> None:
        self._save_settings()

    def _open_settings(self, *_args: Any) -> None:
        # Guard: if already pushed, do nothing (NavigationView handles it)
        if self.nav_view.find_page("settings") is not None:
            return
        load_user_themes(LOG_DIR / "themes", self.language)
        page = SettingsDialog(
            self, self.units, self.language,
            self._set_units, self._set_language,
            self.mock_mode, self._set_mock_mode,
            current_obd_port=self.obd_port,
            on_obd_port_changed=self._set_obd_port,
            current_gauge_theme=self.gauge_theme,
            on_gauge_theme_changed=self._set_gauge_theme,
            current_sidebar_side=self.sidebar_side,
            on_sidebar_side_changed=self._set_sidebar_side,
            current_theme_mode=getattr(self, "theme_mode", "auto"),
            on_theme_mode_changed=self._set_theme_mode,
            current_force_webkit_map=getattr(self, "force_webkit_map", False),
            on_force_webkit_map_changed=self._set_force_webkit_map,
            current_traffic_bundesweit=getattr(self, "map_traffic_bundesweit", True),
            on_traffic_bundesweit_changed=self._set_map_traffic_bundesweit,
            current_traffic_nrw=getattr(self, "map_traffic_nrw", False),
            on_traffic_nrw_changed=self._set_map_traffic_nrw,
            current_last_check=getattr(self, "last_update_check", None),
            on_last_check_updated=self._set_last_update_check,
            current_dashcam_camera=getattr(self, "dashcam_camera", "/dev/video0"),
            on_dashcam_camera_changed=self._set_dashcam_camera,
            current_dashcam_resolution=getattr(self, "dashcam_resolution", "1280x720"),
            on_dashcam_resolution_changed=self._set_dashcam_resolution,
            current_dashcam_fps=getattr(self, "dashcam_fps", 25),
            on_dashcam_fps_changed=self._set_dashcam_fps,
            current_dashcam_seg_minutes=getattr(self, "dashcam_seg_minutes", 3),
            on_dashcam_seg_minutes_changed=self._set_dashcam_seg_minutes,
            current_dashcam_max_segments=getattr(self, "dashcam_max_segments", 10),
            on_dashcam_max_segments_changed=self._set_dashcam_max_segments,
            current_dashcam_dim_timeout=getattr(self, "dashcam_dim_timeout", 30),
            on_dashcam_dim_timeout_changed=self._set_dashcam_dim_timeout,
            current_dashcam_rolling_dir=getattr(self, "dashcam_rolling_dir", ""),
            on_dashcam_rolling_dir_changed=self._set_dashcam_rolling_dir,
            current_dashcam_saved_dir=getattr(self, "dashcam_saved_dir", ""),
            on_dashcam_saved_dir_changed=self._set_dashcam_saved_dir,
            current_nav_position=getattr(self, "nav_position", "bottom"),
            on_nav_position_changed=self._set_nav_position,
            current_dashcam_gps_osd=getattr(self, "dashcam_gps_osd", False),
            on_dashcam_gps_osd_changed=self._set_dashcam_gps_osd,
            current_dashcam_speed_osd=getattr(self, "dashcam_speed_osd", False),
            on_dashcam_speed_osd_changed=self._set_dashcam_speed_osd,
            current_rotation_mode=getattr(self, "rotation_mode", "follow_sensor"),
            on_rotation_mode_changed=self._set_rotation_mode,
            current_tts_enabled=getattr(self, "tts_enabled", False),
            on_tts_enabled_changed=self._set_tts_enabled,
            current_tts_backend=getattr(self, "tts_backend", "espeak"),
            on_tts_backend_changed=self._set_tts_backend,
            current_tts_language=getattr(self, "tts_language", "auto"),
            on_tts_language_changed=self._set_tts_language,
            current_tts_voice=getattr(self, "tts_voice", "female"),
            on_tts_voice_changed=self._set_tts_voice,
            current_tts_quality=getattr(self, "tts_quality", "high"),
            on_tts_quality_changed=self._set_tts_quality,
            current_log_app_enabled=getattr(self, "log_app_enabled", True),
            on_log_app_enabled_changed=self._set_log_app_enabled,
            current_log_obd_enabled=getattr(self, "log_obd_enabled", True),
            on_log_obd_enabled_changed=self._set_log_obd_enabled,
            current_obd_auto_record=getattr(self, "obd_auto_record", True),
            on_obd_auto_record_changed=self._set_obd_auto_record,
            current_vindecoder_api_key=getattr(self, "vindecoder_api_key", ""),
            on_vindecoder_api_key_changed=self._set_vindecoder_api_key,
            current_vindecoder_secret_key=getattr(self, "vindecoder_secret_key", ""),
            on_vindecoder_secret_key_changed=self._set_vindecoder_secret_key,
            current_autodev_api_key=getattr(self, "autodev_api_key", ""),
            on_autodev_api_key_changed=self._set_autodev_api_key,
        )

        def _on_page_hidden(_p: object) -> None:
            from drivepulse_app.tts import service as _tts
            _tts.set_download_callback(self._on_piper_dl_progress)

        page.connect("hidden", _on_page_hidden)
        self.nav_view.push(page)

    def _set_dashcam_camera(self, value: str) -> None:
        self.dashcam_camera = value
        self._save_settings()
        self.dashcam_page.set_camera(value)

    def _set_dashcam_resolution(self, value: str) -> None:
        self.dashcam_resolution = value
        self._save_settings()
        self.dashcam_page.set_resolution(value)

    def _set_dashcam_fps(self, value: int) -> None:
        self.dashcam_fps = value
        self._save_settings()
        self.dashcam_page.set_fps(value)

    def _set_dashcam_seg_minutes(self, value: int) -> None:
        self.dashcam_seg_minutes = value
        self._save_settings()
        self.dashcam_page.set_segment_minutes(value)

    def _set_dashcam_max_segments(self, value: int) -> None:
        self.dashcam_max_segments = value
        self._save_settings()
        self.dashcam_page.set_max_segments(value)

    def _set_dashcam_dim_timeout(self, value: int) -> None:
        self.dashcam_dim_timeout = value
        self._save_settings()
        self.dashcam_page.set_dim_timeout(value)

    def _set_dashcam_rolling_dir(self, value: str) -> None:
        self.dashcam_rolling_dir = value
        self._save_settings()
        self.dashcam_page.set_rolling_dir(value)

    def _set_dashcam_saved_dir(self, value: str) -> None:
        self.dashcam_saved_dir = value
        self._save_settings()
        self.dashcam_page.set_saved_dir(value)

    def _set_dashcam_gps_osd(self, enabled: bool) -> None:
        self.dashcam_gps_osd = enabled
        self._save_settings()
        self.dashcam_page.set_gps_osd(enabled)

    def _set_dashcam_speed_osd(self, enabled: bool) -> None:
        self.dashcam_speed_osd = enabled
        self._save_settings()
        self.dashcam_page.set_speed_osd(enabled)

    def _set_nav_position(self, position: str) -> None:
        if position == getattr(self, "nav_position", "bottom"):
            return
        self.nav_position = position
        self._save_settings()
        self._apply_nav_position(position)

    def _set_rotation_mode(self, mode: str) -> None:
        if mode == getattr(self, "rotation_mode", "follow_sensor"):
            return
        self.rotation_mode = mode
        self._save_settings()
        self.rotation.set_mode(mode)

    def _set_tts_enabled(self, enabled: bool) -> None:
        self.tts_enabled = enabled
        self._save_settings()
        if hasattr(self, "map_page"):
            self.map_page.set_tts_enabled(enabled)

    def _set_tts_backend(self, backend: str) -> None:
        self.tts_backend = backend
        self._save_settings()
        from drivepulse_app.tts import service as tts_service
        tts_service.set_backend(backend)
        if backend == "piper":
            lang = getattr(self, "tts_language", "auto")
            voice = getattr(self, "tts_voice", "female")
            quality = getattr(self, "tts_quality", "high")
            tts_service.ensure_models(lang, voice, quality)

    def _set_tts_language(self, language: str) -> None:
        self.tts_language = language
        self._save_settings()
        if hasattr(self, "map_page"):
            self.map_page.set_tts_language(language)
        if getattr(self, "tts_backend", "espeak") == "piper":
            from drivepulse_app.tts import service as tts_service
            tts_service.ensure_models(language, getattr(self, "tts_voice", "female"), getattr(self, "tts_quality", "high"))

    def _set_tts_voice(self, voice: str) -> None:
        self.tts_voice = voice
        self._save_settings()
        if hasattr(self, "map_page"):
            self.map_page.set_tts_voice(voice)
        if getattr(self, "tts_backend", "espeak") == "piper":
            from drivepulse_app.tts import service as tts_service
            tts_service.ensure_models(getattr(self, "tts_language", "auto"), voice, getattr(self, "tts_quality", "high"))

    def _set_tts_quality(self, quality: str) -> None:
        self.tts_quality = quality
        self._save_settings()
        if hasattr(self, "map_page"):
            self.map_page.set_tts_quality(quality)
        if getattr(self, "tts_backend", "espeak") == "piper":
            from drivepulse_app.tts import service as tts_service
            tts_service.ensure_models(getattr(self, "tts_language", "auto"), getattr(self, "tts_voice", "female"), quality)

    def _set_log_app_enabled(self, enabled: bool) -> None:
        self.log_app_enabled = enabled
        self._save_settings()
        set_log_enabled(enabled)

    def _set_log_obd_enabled(self, enabled: bool) -> None:
        self.log_obd_enabled = enabled
        self._save_settings()
        self.reader.set_obd_log_enabled(enabled)

    def _set_obd_auto_record(self, enabled: bool) -> None:
        self.obd_auto_record = enabled
        self.settings["obd_auto_record"] = enabled
        self._save_settings()

    def _set_vindecoder_api_key(self, value: str) -> None:
        self.vindecoder_api_key = value
        self._save_settings()
        self.cars_page._vindecoder_api_key = value or None

    def _set_vindecoder_secret_key(self, value: str) -> None:
        self.vindecoder_secret_key = value
        self._save_settings()
        self.cars_page._vindecoder_secret_key = value or None

    def _set_autodev_api_key(self, value: str) -> None:
        self.autodev_api_key = value
        self._save_settings()
        self.cars_page._autodev_api_key = value or None

    def _open_sync(self, *_args: Any) -> None:
        if getattr(self, "_sync_is_online", False):
            self._push_sync_status_page()
            return
        if self.nav_view.find_page("sync") is not None:
            return
        from drivepulse_app.sync.dialog import SyncDialog

        def _on_sync_complete() -> None:
            self.cars_page.refresh_profiles()
            self._update_conflict_badge()

        page = SyncDialog(
            self, self.language, self.db,
            on_sync_complete=_on_sync_complete,
            on_connected=self._on_sync_connected,
            on_disconnected=self._on_sync_disconnected,
        )
        self._active_sync_dialog: SyncDialog | None = page
        self.nav_view.push(page)

    def _on_sync_connected(self, name: str, ip: str) -> None:
        self._sync_is_online = True
        self._sync_connected_name = name
        self._sync_connected_ip = ip
        self._sync_connected_at = time.time()
        self._set_sync_icon_online(True)

    def _on_sync_disconnected(self) -> None:
        self._sync_is_online = False
        self._active_sync_dialog = None
        self._set_sync_icon_online(False)
        if self.nav_view.find_page("sync-status") is not None:
            self.nav_view.pop()

    def _push_sync_status_page(self) -> None:
        if self.nav_view.find_page("sync-status") is not None:
            return
        self.nav_view.push(self._build_sync_status_page())

    def _build_sync_status_page(self) -> Any:
        import datetime

        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, GLib, Gtk

        def t(key, **kw):
            return _translate(self.language, key, **kw)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=t("sync.status.title")))
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        toolbar_view.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(16)
        box.set_margin_end(16)

        icon = Gtk.Image.new_from_icon_name("arrows-loop-symbolic")
        icon.set_pixel_size(48)
        icon.add_css_class("dp-sync-online")
        icon.set_halign(Gtk.Align.CENTER)
        box.append(icon)

        name = getattr(self, "_sync_connected_name", "") or ""
        if name:
            name_label = Gtk.Label(label=name)
            name_label.add_css_class("title-2")
            name_label.set_halign(Gtk.Align.CENTER)
            name_label.set_wrap(True)
            name_label.set_justify(Gtk.Justification.CENTER)
            box.append(name_label)

        group = Adw.PreferencesGroup()
        box.append(group)

        ip = getattr(self, "_sync_connected_ip", "") or ""
        if ip:
            ip_row = Adw.ActionRow()
            ip_row.set_title(t("sync.status.ip_label"))
            ip_row.set_subtitle(ip)
            group.add(ip_row)

        connected_at = getattr(self, "_sync_connected_at", None)
        if connected_at:
            ts_str = datetime.datetime.fromtimestamp(connected_at).strftime("%H:%M:%S")
            time_row = Adw.ActionRow()
            time_row.set_title(t("sync.status.connected_since"))
            time_row.set_subtitle(ts_str)
            group.add(time_row)

        lc_row = Adw.ActionRow()
        lc_row.set_title(t("sync.status.last_contact"))
        lc_row.set_subtitle("—")
        group.add(lc_row)

        def _fmt_ts(ts: float) -> str:
            return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")

        _timer_id: list[int] = [0]

        def _refresh_last_contact() -> bool:
            dialog = getattr(self, "_active_sync_dialog", None)
            lc = dialog.get_last_contact() if dialog is not None else 0.0
            lc_row.set_subtitle(_fmt_ts(lc) if lc > 0 else "—")
            return True

        _refresh_last_contact()
        _timer_id[0] = GLib.timeout_add_seconds(1, _refresh_last_contact)

        sync_status_label = Gtk.Label(label="")
        sync_status_label.set_wrap(True)
        sync_status_label.set_halign(Gtk.Align.CENTER)
        sync_status_label.set_justify(Gtk.Justification.CENTER)
        box.append(sync_status_label)

        _active_dialog = getattr(self, "_active_sync_dialog", None)
        if _active_dialog is not None:
            _active_dialog.set_sync_feedback_label(sync_status_label)

        def _open_sync_opts(_b: Any) -> None:
            d = getattr(self, "_active_sync_dialog", None)
            if d is not None:
                d._show_sync_options_dialog(
                    sync_status_label,
                    is_server=getattr(d, "_server", None) is not None,
                )

        sync_opts_btn = Gtk.Button(label=t("sync.paired.sync_options_btn"))
        sync_opts_btn.set_halign(Gtk.Align.FILL)
        sync_opts_btn.connect("clicked", _open_sync_opts)
        box.append(sync_opts_btn)

        disconnect_btn = Gtk.Button(label=t("sync.status.disconnect_btn"))
        disconnect_btn.add_css_class("destructive-action")
        disconnect_btn.set_halign(Gtk.Align.FILL)
        disconnect_btn.set_margin_top(8)
        disconnect_btn.connect("clicked", lambda _b: self._disconnect_sync())
        box.append(disconnect_btn)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(box)
        toolbar_view.set_content(scroll)

        page = Adw.NavigationPage()
        page.set_tag("sync-status")
        page.set_title(t("sync.status.title"))
        page.set_child(toolbar_view)

        def _on_page_hiding(_p: Any) -> None:
            if _timer_id[0]:
                GLib.source_remove(_timer_id[0])
                _timer_id[0] = 0

        page.connect("hiding", _on_page_hiding)
        return page

    def _disconnect_sync(self) -> None:
        dialog = getattr(self, "_active_sync_dialog", None)
        if dialog is not None:
            self._active_sync_dialog = None
            dialog.disconnect()
        else:
            self._on_sync_disconnected()

    def _set_sync_icon_online(self, online: bool) -> None:
        btn = getattr(self, "_sync_btn", None)
        if btn is None:
            return
        if online:
            btn.remove_css_class("dp-sync-offline")
            btn.add_css_class("dp-sync-online")
        else:
            btn.remove_css_class("dp-sync-online")
            btn.add_css_class("dp-sync-offline")

    def _set_obd_port(self, port: str | None) -> None:
        if port == self.obd_port:
            return
        self.obd_port = port
        self._save_settings()
        self.reader.set_configured_port(port)

    def _set_units(self, units: str) -> None:
        if units == self.units:
            return
        self.units = units
        self._save_units()
        self.dashboard_canvas.set_units(units)
        self.dashcam_page.set_units(units)
        if hasattr(self, "map_page"):
            self.map_page.set_units(units)

        if self.units == "metric":
            self.speed_gauge.state.unit = "km/h"
            self.speed_gauge.state.max_value = 240
        else:
            self.speed_gauge.state.unit = "mph"
            self.speed_gauge.state.max_value = 150

        if self.last_payload is not None:
            self._update_from_payload(self.last_payload)
        else:
            self.speed_gauge.queue_draw()

    def _set_gauge_theme(self, theme: str) -> None:
        if theme == self.gauge_theme:
            return
        self.gauge_theme = theme
        self._save_settings()
        is_dashboard = theme in DASHBOARD_THEMES
        self.gauge_box.set_visible(not is_dashboard)
        self.dashboard_canvas.set_visible(is_dashboard)
        # Dashboard themes fill the screen edge-to-edge; gauge themes need breathing room
        margin = 0 if is_dashboard else 12
        for setter in (
            self.dashboard_page.set_margin_top,
            self.dashboard_page.set_margin_bottom,
            self.dashboard_page.set_margin_start,
            self.dashboard_page.set_margin_end,
        ):
            setter(margin)
        if is_dashboard:
            self.dashboard_canvas.set_theme(theme)
        else:
            for gauge in (self.rpm_gauge, self.speed_gauge, self.temp_gauge):
                gauge.set_theme(theme)
        self.stopwatch_page.set_theme(theme)
        self._apply_window_theme(theme)

    def _set_mock_mode(self, mock_mode: bool) -> None:
        if mock_mode == self.mock_mode:
            return
        self.mock_mode = mock_mode
        self._save_settings()
        self.reader.set_force_mock(mock_mode)
        if hasattr(self, "gps_reader"):
            self.gps_reader.set_mock_mode(mock_mode)
        if hasattr(self, "map_page"):
            self.map_page.set_mock_mode(mock_mode)
        if hasattr(self, "cars_page"):
            self.cars_page.set_mock_mode(mock_mode)
        if mock_mode:
            try:
                from drivepulse_app.mock.seed import seed_mock_data
                added = seed_mock_data(self.db)
                if added and hasattr(self, "cars_page"):
                    self.cars_page.refresh()
            except Exception:
                log.exception("Could not seed mock vehicle data")

    def _set_sidebar_side(self, side: str) -> None:
        if side == self.sidebar_side:
            return
        self.sidebar_side = side
        self._save_settings()
        self.cars_page.set_sidebar_side(side)

    def _set_theme_mode(self, mode: str) -> None:
        if mode == getattr(self, "theme_mode", "auto"):
            return
        self.theme_mode = mode
        self._save_settings()
        self._apply_theme_mode(mode)
        self._apply_window_theme(self.gauge_theme)

    def _set_force_webkit_map(self, force_webkit: bool) -> None:
        if force_webkit == getattr(self, "force_webkit_map", False):
            return
        self.force_webkit_map = force_webkit
        self._save_settings()

    def _set_map_3d_view(self, enabled: bool) -> None:
        if enabled == getattr(self, "map_3d_view", True):
            return
        self.map_3d_view = enabled
        self._save_settings()

    def _set_map_traffic_visible(self, visible: bool) -> None:
        if visible == getattr(self, "map_traffic_visible", False):
            return
        self.map_traffic_visible = visible
        self._save_settings()

    def _set_map_traffic_bundesweit(self, enabled: bool) -> None:
        if enabled == getattr(self, "map_traffic_bundesweit", True):
            return
        self.map_traffic_bundesweit = enabled
        self._save_settings()
        if hasattr(self, "map_page"):
            self.map_page.set_traffic_sources(
                bundesweit=enabled,
                nrw=getattr(self, "map_traffic_nrw", False),
            )

    def _set_map_traffic_nrw(self, enabled: bool) -> None:
        if enabled == getattr(self, "map_traffic_nrw", False):
            return
        self.map_traffic_nrw = enabled
        self._save_settings()
        if hasattr(self, "map_page"):
            self.map_page.set_traffic_sources(
                bundesweit=getattr(self, "map_traffic_bundesweit", True),
                nrw=enabled,
            )

    def _set_last_update_check(self, timestamp: str) -> None:
        self.last_update_check = timestamp
        self._save_settings()

    def _set_language(self, language: str) -> None:
        language = _normalize_language(language)
        if language == self.language:
            return
        self.language = language
        self._save_settings()
        self.rpm_gauge.title = _translate(self.language, "gauge.rpm")
        self.speed_gauge.title = _translate(self.language, "gauge.speed")
        self.temp_gauge.title = _translate(self.language, "gauge.coolant")
        self.title_label.set_text(_translate(self.language, "window.title"))
        self.settings_button.set_tooltip_text(_translate(self.language, "settings.tooltip"))
        self.obd_indicator["label"].set_text(_translate(self.language, "status.obd"))
        self.gps_indicator["label"].set_text(_translate(self.language, "status.gps"))
        self.dashboard_stack_page.set_title(_translate(self.language, "nav.gauges"))
        self.stopwatch_stack_page.set_title(_translate(self.language, "nav.stopwatch"))
        self.cars_stack_page.set_title(_translate(self.language, "nav.cars"))
        self.map_stack_page.set_title(_translate(self.language, "nav.map"))
        self.dashcam_stack_page.set_title(_translate(self.language, "nav.dashcam"))
        self.stopwatch_page.set_language(self.language)
        self.map_page.set_language(self.language)
        self.dashcam_page.set_language(self.language)
        self.dashboard_canvas.set_language(self.language)
        self.cars_page.set_language(self.language)
        if self.last_payload is not None:
            self._update_from_payload(self.last_payload)
        else:
            self.status_label.set_text(_translate(self.language, "status.connecting"))
        for gauge in (self.rpm_gauge, self.speed_gauge, self.temp_gauge):
            gauge.queue_draw()
