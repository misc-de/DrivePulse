from __future__ import annotations

import threading
import time
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from .common import _translate
from .db import DriveDB
from .diagnostics import get_logger
from .sync_crypto import (
    generate_tls_keypair,
    generate_token,
    get_local_ip,
    get_spki_fingerprint,
)
from .sync_identity import CERT_PATH, KEY_PATH, SYNC_DIR, get_or_create_device_id
from .sync_data import (
    export_all,
    import_data,
    load_paired_devices,
    save_paired_devices,
    upsert_paired_device,
)
from .sync_qr_scanner import WebcamQRScanner, scan_supported
from .sync_server import SyncServer
from .sync_client import SyncClient
from .sync_flow import parse_pairing_url, perform_sync

# Sync mode constants
MODE_MERGE = "merge"
MODE_REMOTE_WINS = "remote_wins"
MODE_LOCAL_WINS = "local_wins"
MODE_REMOTE_WINS_ALL = "remote_wins_all"
MODE_LOCAL_WINS_ALL = "local_wins_all"
log = get_logger(__name__)

# Server waits this many seconds for a client before auto-closing.
_SERVER_TIMEOUT_S = 180  # 3 minutes


class SyncDialog(Adw.NavigationPage):
    __gtype_name__ = "SyncDialog"

    def __init__(
        self,
        parent: Any,
        language: str,
        db: DriveDB,
        initial_mode: str | None = None,
        on_sync_complete: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(tag="sync")
        self._language = language
        self._db = db
        self._on_sync_complete = on_sync_complete
        self._server: SyncServer | None = None
        self._server_lock = threading.RLock()
        self._server_start_requested = False
        self._server_start_generation = 0
        self._closed = False
        self._pushing_subpage = False  # True während outer_nav.push() läuft
        self._scanner: WebcamQRScanner | None = None
        self._sync_mode: str = MODE_MERGE
        # Outer app NavigationView — all sub-pages are pushed here so swipe
        # always works; there is no inner NavigationView.
        self._outer_nav: Adw.NavigationView | None = getattr(parent, "nav_view", None)

        self.set_title(self._t("sync.title"))
        self.set_child(self._build_home_content())
        self.connect("hiding", self._on_hiding)
        self.connect("showing", self._on_showing)

        if initial_mode == "client":
            GLib.idle_add(self._push_client_page)
        elif initial_mode == "server":
            GLib.idle_add(self._push_server_page)

    def _t(self, key: str, **kw: Any) -> str:
        return _translate(self._language, key, **kw)

    def _stop_server(self) -> None:
        with self._server_lock:
            self._server_start_requested = False
            self._server_start_generation += 1
            server = self._server
            self._server = None
        if server is None:
            return
        try:
            server.stop()
        except Exception:
            log.exception("Could not stop sync server")

    def _cancel_scanner(self) -> None:
        scanner = self._scanner
        self._scanner = None
        if scanner is None:
            return
        try:
            scanner.cancel()
        except Exception:
            log.exception("Could not cancel sync QR scanner")

    def _server_start_is_current(self, generation: int) -> bool:
        with self._server_lock:
            return not self._closed and self._server_start_generation == generation

    def _push_nav(self, page: Adw.NavigationPage) -> None:
        """Pusht eine Seite auf den outer_nav und schützt _on_hiding währenddessen."""
        if self._outer_nav is None:
            return
        self._pushing_subpage = True
        try:
            self._outer_nav.push(page)
        finally:
            self._pushing_subpage = False

    # ------------------------------------------------------------------ home

    def _build_home_content(self) -> Gtk.Widget:
        """Build the home page content and return the top-level widget."""
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=self._t("sync.title")))
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        toolbar_view.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        start_group = Adw.PreferencesGroup()
        start_group.set_title(self._t("sync.title"))

        server_row = Adw.ActionRow()
        server_row.set_title(self._t("sync.server.row_title"))
        server_row.set_subtitle(self._t("sync.server.row_subtitle"))
        server_btn = Gtk.Button(label="→")
        server_btn.set_valign(Gtk.Align.CENTER)
        server_btn.connect("clicked", self._push_server_page)
        server_row.add_suffix(server_btn)
        server_row.set_activatable_widget(server_btn)
        start_group.add(server_row)

        client_row = Adw.ActionRow()
        client_row.set_title(self._t("sync.client.row_title"))
        client_row.set_subtitle(self._t("sync.client.row_subtitle"))
        client_btn = Gtk.Button(label="→")
        client_btn.set_valign(Gtk.Align.CENTER)
        client_btn.connect("clicked", self._push_client_page)
        client_row.add_suffix(client_btn)
        client_row.set_activatable_widget(client_btn)
        start_group.add(client_row)

        box.append(start_group)

        self._devices_group = Adw.PreferencesGroup()
        self._devices_group.set_title(self._t("sync.devices.title"))
        box.append(self._devices_group)
        self._populate_devices()

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(box)
        toolbar_view.set_content(scroll)
        return toolbar_view

    def _populate_devices(self) -> None:
        for row in getattr(self, "_home_device_rows", []):
            self._devices_group.remove(row)
        self._home_device_rows: list[Adw.ActionRow] = []

        devices = load_paired_devices()
        if not devices:
            empty_row = Adw.ActionRow()
            empty_row.set_title(self._t("sync.devices.empty"))
            self._devices_group.add(empty_row)
            self._home_device_rows.append(empty_row)
            return

        for device in devices:
            did = device.get("device_id", "")
            name = device.get("name") or did[:12] or "Device"
            host = device.get("host", "")
            port = device.get("port", SyncServer.PORT)
            last_seen = device.get("last_seen", "")
            spki_fp = device.get("spki_fingerprint", "")
            subtitle = f"{host}:{port}"
            if last_seen:
                subtitle += f" · {self._t('sync.device.last_seen', ts=last_seen[:16])}"

            row = Adw.ActionRow()
            row.set_title(name)
            row.set_subtitle(subtitle)
            row.set_subtitle_lines(0)

            connect_btn = Gtk.Button(label=self._t("sync.device.connect"))
            connect_btn.set_valign(Gtk.Align.CENTER)
            connect_btn.add_css_class("suggested-action")
            connect_btn.connect(
                "clicked",
                lambda _b, h=host, p=port, fp=spki_fp: self._push_qr_scan_page(h, p, fp),
            )
            row.add_suffix(connect_btn)

            del_btn = Gtk.Button(label="×")
            del_btn.set_valign(Gtk.Align.CENTER)
            del_btn.add_css_class("destructive-action")
            del_btn.set_tooltip_text(self._t("sync.device.remove"))
            del_btn.connect("clicked", lambda _b, d_id=did: self._delete_device(d_id))
            row.add_suffix(del_btn)

            self._devices_group.add(row)
            self._home_device_rows.append(row)

    def _delete_device(self, device_id: str) -> None:
        devices = load_paired_devices()
        devices = [d for d in devices if d.get("device_id") != device_id]
        save_paired_devices(devices)
        if hasattr(self, "_devices_group"):
            self._populate_devices()
        if hasattr(self, "_known_devices_group"):
            self._refresh_known_devices_group()

    # ------------------------------------------------------------------ server page

    def start_server_from_user_action(self) -> None:
        self._push_server_page()

    def _push_server_page(self, *_args: Any) -> None:
        if self._outer_nav is None:
            return
        if self._outer_nav.find_page("sync-server") is not None:
            return
        self._stop_server()
        result = self._build_server_page()
        page = result[0]
        self._server_status_label = result[1]
        self._server_qr_picture = result[2]
        self._server_spinner = result[3]
        self._server_instr_label = result[4]

        page.connect("hiding", lambda _p: self._stop_server())
        self._push_nav(page)

        with self._server_lock:
            if self._closed:
                return
            self._server_start_generation += 1
            generation = self._server_start_generation
            self._server_start_requested = True
        threading.Thread(target=self._start_server_mode, args=(generation,), daemon=True).start()

    def _build_server_page(self) -> tuple[Adw.NavigationPage, Gtk.Label, Gtk.Picture, Gtk.Spinner, Gtk.Label]:
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=self._t("sync.server.title")))
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        toolbar_view.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.START)

        status_label = Gtk.Label(label=self._t("sync.server.generating"))
        status_label.set_wrap(True)
        status_label.set_justify(Gtk.Justification.CENTER)
        status_label.set_halign(Gtk.Align.CENTER)
        box.append(status_label)

        qr_picture = Gtk.Picture()
        qr_picture.set_size_request(280, 280)
        qr_picture.set_halign(Gtk.Align.CENTER)
        qr_picture.set_visible(False)
        box.append(qr_picture)

        spinner = Gtk.Spinner()
        spinner.start()
        spinner.set_halign(Gtk.Align.CENTER)
        box.append(spinner)

        instr_label = Gtk.Label(label=self._t("sync.server.instructions"))
        instr_label.set_wrap(True)
        instr_label.set_justify(Gtk.Justification.CENTER)
        instr_label.set_halign(Gtk.Align.CENTER)
        instr_label.add_css_class("dim-label")
        instr_label.set_visible(False)
        box.append(instr_label)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(box)
        toolbar_view.set_content(scroll)

        page = Adw.NavigationPage()
        page.set_tag("sync-server")
        page.set_title(self._t("sync.server.title"))
        page.set_child(toolbar_view)
        return page, status_label, qr_picture, spinner, instr_label

    def _start_server_mode(self, generation: int | None = None) -> None:
        with self._server_lock:
            is_requested = (
                not self._closed
                and self._server_start_requested
                and (generation is None or generation == self._server_start_generation)
            )
            if is_requested:
                self._server_start_requested = False
        if not is_requested:
            log.warning("Blocked sync server start without explicit user request")
            return

        try:
            SYNC_DIR.mkdir(parents=True, exist_ok=True)
            generate_tls_keypair(CERT_PATH, KEY_PATH)
            pairing_token = generate_token(32)
            session_token = generate_token(32)
            spki_fp = get_spki_fingerprint(CERT_PATH)
            local_ip = get_local_ip()
            expiry = int(time.time()) + _SERVER_TIMEOUT_S
            if generation is not None and not self._server_start_is_current(generation):
                return

            def _on_paired(device_info: dict) -> None:
                client_hostname = device_info.get("hostname", "") or device_info.get("device_id", "")[:12] or "Device"
                upsert_paired_device(
                    device_id=device_info.get("device_id", client_hostname),
                    name=client_hostname,
                    spki_fingerprint="",
                    host="",
                    port=0,
                )
                GLib.idle_add(
                    lambda: self._server_status_label.set_text(self._t("sync.server.connected"))
                )

            def _on_import(data: dict) -> None:
                srv_mode = data.get("import_mode", "merge")
                try:
                    result = import_data(self._db, data, mode=srv_mode)
                    msg = self._t(
                        "sync.complete",
                        cars=result["cars_added"] + result["cars_updated"],
                        trips=result["trips_added"],
                        samples=result["samples_added"],
                    )
                    GLib.idle_add(lambda: self._server_status_label.set_text(msg))
                    if self._on_sync_complete:
                        GLib.idle_add(self._on_sync_complete)
                    GLib.idle_add(lambda: (self._stop_server(), False)[1])
                except Exception as exc:
                    log.exception("Could not import sync data on server side")
                    _err = str(exc)
                    GLib.idle_add(
                        lambda: self._server_status_label.set_text(
                            self._t("sync.error", error=_err)
                        )
                    )
                    raise

            def _on_timeout() -> None:
                with self._server_lock:
                    self._server = None
                GLib.idle_add(
                    lambda: self._server_status_label.set_text(
                        self._t("sync.server.timeout")
                    )
                )

            server = SyncServer(
                CERT_PATH, KEY_PATH,
                pairing_token=pairing_token,
                session_token=session_token,
                on_paired_cb=_on_paired,
                get_export_fn=lambda: export_all(self._db),
                on_import_fn=_on_import,
                on_timeout_cb=_on_timeout,
            )
            with self._server_lock:
                if self._closed or (
                    generation is not None and generation != self._server_start_generation
                ):
                    return
                self._server = server
            server.start()
            with self._server_lock:
                should_stop = self._closed or (
                    generation is not None and generation != self._server_start_generation
                )
                if should_stop and self._server is server:
                    self._server = None
            if should_stop:
                server.stop()
                return

            qr_url = (
                f"drivepulse://pair?v=1&h={local_ip}&p={server.actual_port}"
                f"&fp={spki_fp}&t={pairing_token}&exp={expiry}"
            )

            from .sync_qrgen import make_pixbuf as _make_qr_pixbuf
            pixbuf = _make_qr_pixbuf(qr_url)

            def _show_qr() -> bool:
                try:
                    self._server_qr_picture.set_pixbuf(pixbuf)
                    self._server_qr_picture.set_visible(True)
                    self._server_spinner.stop()
                    self._server_spinner.set_visible(False)
                    self._server_instr_label.set_visible(True)
                    self._server_status_label.set_text(self._t("sync.server.waiting"))

                except Exception as exc:
                    log.exception("Could not display sync QR image")
                    self._server_status_label.set_text(self._t("sync.error", error=str(exc)))
                return False

            GLib.idle_add(_show_qr)

        except Exception as exc:
            log.exception("Could not start sync server mode")
            _err = str(exc)
            GLib.idle_add(
                lambda: self._server_status_label.set_text(self._t("sync.error", error=_err))
            )

    # ------------------------------------------------------------------ client: known devices

    def _push_client_page(self, *_args: Any) -> None:
        if self._outer_nav is None:
            return
        if self._outer_nav.find_page("sync-client") is not None:
            return
        self._push_nav(self._build_known_devices_page())

    def _build_known_devices_page(self) -> Adw.NavigationPage:
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=self._t("sync.client.title")))
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        toolbar_view.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        qr_btn = Gtk.Button(label=self._t("sync.client.scan_camera"))
        qr_btn.add_css_class("suggested-action")
        qr_btn.set_hexpand(True)
        qr_btn.connect("clicked", lambda _b: self._push_qr_scan_page("", 0, ""))
        box.append(qr_btn)

        group = Adw.PreferencesGroup()
        group.set_title(self._t("sync.devices.title"))
        box.append(group)
        self._known_devices_group = group
        self._refresh_known_devices_group()

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(box)
        toolbar_view.set_content(scroll)

        page = Adw.NavigationPage()
        page.set_tag("sync-client")
        page.set_title(self._t("sync.client.title"))
        page.set_child(toolbar_view)
        return page

    def _refresh_known_devices_group(self) -> None:
        group = self._known_devices_group
        for row in getattr(self, "_known_device_rows", []):
            group.remove(row)
        self._known_device_rows: list[Adw.ActionRow] = []

        devices = load_paired_devices()
        if not devices:
            empty_row = Adw.ActionRow()
            empty_row.set_title(self._t("sync.devices.empty"))
            group.add(empty_row)
            self._known_device_rows.append(empty_row)
            return

        for device in devices:
            did = device.get("device_id", "")
            name = device.get("name") or did[:12] or "Device"
            host = device.get("host", "")
            port = device.get("port", SyncServer.PORT)
            last_seen = device.get("last_seen", "")
            spki_fp = device.get("spki_fingerprint", "")
            subtitle = f"{host}:{port}"
            if last_seen:
                subtitle += f" · {self._t('sync.device.last_seen', ts=last_seen[:16])}"

            row = Adw.ActionRow()
            row.set_title(GLib.markup_escape_text(name))
            row.set_subtitle(GLib.markup_escape_text(subtitle))
            row.set_subtitle_lines(0)

            connect_btn = Gtk.Button(label=self._t("sync.device.connect"))
            connect_btn.set_valign(Gtk.Align.CENTER)
            connect_btn.add_css_class("suggested-action")
            connect_btn.connect(
                "clicked",
                lambda _b, h=host, p=port, fp=spki_fp: self._push_qr_scan_page(h, p, fp),
            )
            row.add_suffix(connect_btn)

            del_btn = Gtk.Button(label="×")
            del_btn.set_valign(Gtk.Align.CENTER)
            del_btn.add_css_class("destructive-action")
            del_btn.set_tooltip_text(self._t("sync.device.remove"))
            del_btn.connect("clicked", lambda _b, d_id=did: self._delete_device(d_id))
            row.add_suffix(del_btn)

            group.add(row)
            self._known_device_rows.append(row)

    def _push_qr_scan_page(self, host: str, port: int, spki_fp: str) -> None:
        if self._outer_nav is None:
            return
        if self._outer_nav.find_page("sync-qr-scan") is not None:
            return
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=self._t("sync.client.title")))
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        toolbar_view.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        scanner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scanner_box.set_vexpand(True)
        box.append(scanner_box)

        status_label = Gtk.Label(label="")
        status_label.set_wrap(True)
        status_label.set_halign(Gtk.Align.CENTER)
        box.append(status_label)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(box)
        toolbar_view.set_content(scroll)

        page = Adw.NavigationPage()
        page.set_tag("sync-qr-scan")
        page.set_title(self._t("sync.client.title"))
        page.set_child(toolbar_view)
        page.connect("hiding", lambda _p: self._cancel_scanner())
        self._push_nav(page)

        def _on_success(text: str) -> None:
            self._cancel_scanner()
            status_label.set_text(self._t("sync.client.pairing"))
            threading.Thread(target=self._do_pair, args=(text, status_label), daemon=True).start()

        if not scan_supported():
            fallback = Gtk.Label(label=self._t("sync.client.no_camera"))
            fallback.set_halign(Gtk.Align.CENTER)
            fallback.add_css_class("dim-label")
            scanner_box.append(fallback)
            return

        def _on_error(msg: str) -> None:
            status_label.set_text(msg)

        self._scanner = WebcamQRScanner(
            on_success=_on_success,
            on_error=_on_error,
            language=self._language,
            filter_fn=lambda t: t.startswith("drivepulse://"),
        )
        scanner_box.append(self._scanner.build_widget())
        self._scanner.start()

    def _do_pair(self, url_text: str, status_label: Gtk.Label) -> None:
        def _set(msg: str) -> bool:
            status_label.set_text(msg)
            return False

        log.info("_do_pair: URL=%s…", url_text[:80])
        try:
            try:
                pairing = parse_pairing_url(url_text, SyncServer.PORT)
            except TimeoutError:
                log.warning("_do_pair: QR-Code abgelaufen")
                GLib.idle_add(_set, self._t("sync.client.expired"))
                return
            except ValueError as exc:
                log.warning("_do_pair: URL ungültig: %s", exc)
                GLib.idle_add(_set, self._t("sync.error", error=str(exc)))
                return

            log.info("_do_pair: pairing ok, host=%s port=%s", pairing.host, pairing.port)
            device_id = get_or_create_device_id()
            client = SyncClient(pairing.host, pairing.port, pairing.spki_fingerprint, device_id)

            log.info("_do_pair: verify fingerprint…")
            if not client.verify_fingerprint():
                log.warning("_do_pair: Fingerprint-Verifikation fehlgeschlagen")
                GLib.idle_add(_set, self._t("sync.client.fp_error"))
                return
            log.info("_do_pair: Fingerprint ok, pairing…")
            if not client.pair(pairing.pairing_token):
                log.warning("_do_pair: Pairing fehlgeschlagen")
                GLib.idle_add(_set, self._t("sync.error", error="Pairing failed"))
                return
            log.info("_do_pair: Pairing erfolgreich")

            self._active_client = client
            self._active_host = pairing.host
            self._active_port = pairing.port
            self._active_spki_fp = pairing.spki_fingerprint
            GLib.idle_add(self._push_paired_page)

        except Exception as exc:
            log.exception("Could not pair with sync URL")
            GLib.idle_add(_set, self._t("sync.error", error=str(exc)))

    # ------------------------------------------------------------------ paired page (step 2: mode + sync)

    def _push_paired_page(self) -> bool:
        if self._outer_nav is None:
            return False
        if self._outer_nav.find_page("sync-paired") is not None:
            return False
        page, self._paired_status_label = self._build_paired_page(self._active_host)
        self._push_nav(page)
        return False

    def _build_paired_page(self, host: str) -> tuple[Adw.NavigationPage, Gtk.Label]:
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=self._t("sync.paired.title")))
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        toolbar_view.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(16)
        box.set_margin_end(16)

        ok_icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
        ok_icon.set_pixel_size(48)
        ok_icon.add_css_class("success")
        ok_icon.set_halign(Gtk.Align.CENTER)
        box.append(ok_icon)

        connected_label = Gtk.Label(label=self._t("sync.paired.connected_to", host=host))
        connected_label.add_css_class("title-2")
        connected_label.set_halign(Gtk.Align.CENTER)
        connected_label.set_wrap(True)
        connected_label.set_justify(Gtk.Justification.CENTER)
        box.append(connected_label)

        hint_label = Gtk.Label(label=self._t("sync.paired.hint"))
        hint_label.add_css_class("dim-label")
        hint_label.set_halign(Gtk.Align.CENTER)
        hint_label.set_wrap(True)
        box.append(hint_label)

        mode_group = Adw.PreferencesGroup()
        mode_group.set_title(self._t("sync.mode.label"))
        box.append(mode_group)

        self._sync_mode = MODE_MERGE
        merge_check = Gtk.CheckButton()
        merge_check.set_active(True)
        merge_check.set_valign(Gtk.Align.CENTER)

        def _make_mode_row(check: Gtk.CheckButton, title_key: str, sub_key: str) -> Adw.ActionRow:
            row = Adw.ActionRow()
            row.set_title(self._t(title_key))
            row.set_subtitle(self._t(sub_key))
            row.set_subtitle_lines(0)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            return row

        mode_group.add(_make_mode_row(merge_check, "sync.mode.merge", "sync.mode.merge.subtitle"))

        remote_check = Gtk.CheckButton()
        remote_check.set_group(merge_check)
        remote_check.set_valign(Gtk.Align.CENTER)
        mode_group.add(_make_mode_row(remote_check, "sync.mode.remote_wins", "sync.mode.remote_wins.subtitle"))

        local_check = Gtk.CheckButton()
        local_check.set_group(merge_check)
        local_check.set_valign(Gtk.Align.CENTER)
        mode_group.add(_make_mode_row(local_check, "sync.mode.local_wins", "sync.mode.local_wins.subtitle"))

        remote_all_check = Gtk.CheckButton()
        remote_all_check.set_group(merge_check)
        remote_all_check.set_valign(Gtk.Align.CENTER)
        mode_group.add(_make_mode_row(remote_all_check, "sync.mode.remote_wins_all", "sync.mode.remote_wins_all.subtitle"))

        local_all_check = Gtk.CheckButton()
        local_all_check.set_group(merge_check)
        local_all_check.set_valign(Gtk.Align.CENTER)
        mode_group.add(_make_mode_row(local_all_check, "sync.mode.local_wins_all", "sync.mode.local_wins_all.subtitle"))

        def _on_mode_toggled(*_: Any) -> None:
            if remote_check.get_active():
                self._sync_mode = MODE_REMOTE_WINS
            elif local_check.get_active():
                self._sync_mode = MODE_LOCAL_WINS
            elif remote_all_check.get_active():
                self._sync_mode = MODE_REMOTE_WINS_ALL
            elif local_all_check.get_active():
                self._sync_mode = MODE_LOCAL_WINS_ALL
            else:
                self._sync_mode = MODE_MERGE

        for chk in (merge_check, remote_check, local_check, remote_all_check, local_all_check):
            chk.connect("toggled", _on_mode_toggled)

        status_label = Gtk.Label(label="")
        status_label.set_wrap(True)
        status_label.set_halign(Gtk.Align.CENTER)
        status_label.set_justify(Gtk.Justification.CENTER)
        box.append(status_label)

        sync_btn = Gtk.Button(label=self._t("sync.paired.sync_btn"))
        sync_btn.add_css_class("suggested-action")
        sync_btn.set_halign(Gtk.Align.FILL)
        sync_btn.connect(
            "clicked",
            lambda _b: self._on_sync_start(sync_btn, status_label),
        )
        box.append(sync_btn)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(box)
        toolbar_view.set_content(scroll)

        page = Adw.NavigationPage()
        page.set_tag("sync-paired")
        page.set_title(self._t("sync.paired.title"))
        page.set_child(toolbar_view)
        return page, status_label

    def _on_sync_start(self, sync_btn: Gtk.Button, status_label: Gtk.Label) -> None:
        sync_btn.set_sensitive(False)
        status_label.set_text(self._t("sync.client.connecting"))
        threading.Thread(
            target=self._do_sync,
            args=(self._active_client, self._sync_mode, status_label, sync_btn),
            daemon=True,
        ).start()

    def _do_sync(
        self,
        client: SyncClient,
        mode: str,
        status_label: Gtk.Label,
        sync_btn: Gtk.Button,
    ) -> None:
        def _set(msg: str) -> bool:
            status_label.set_text(msg)
            return False

        def _done(msg: str) -> bool:
            status_label.set_text(msg)
            sync_btn.set_sensitive(True)
            return False

        try:
            result = perform_sync(self._db, client, mode)

            server_name = getattr(client, "server_hostname", "") or self._active_host
            upsert_paired_device(
                device_id=self._active_host,
                name=server_name,
                spki_fingerprint=self._active_spki_fp,
                host=self._active_host,
                port=self._active_port,
            )

            msg = self._t(
                "sync.complete",
                cars=result["cars"],
                trips=result["trips"],
                samples=result["samples"],
            )
            GLib.idle_add(_done, msg)
            if self._on_sync_complete:
                GLib.idle_add(self._on_sync_complete)

        except Exception as exc:
            log.exception("Sync operation failed")
            GLib.idle_add(_done, self._t("sync.error", error=str(exc)))

    # ------------------------------------------------------------------ cleanup

    def _on_showing(self, *_args: Any) -> None:
        # Zurückgekehrt von einer Sub-Seite — closed-Flag zurücksetzen
        with self._server_lock:
            self._closed = False

    def _on_hiding(self, *_args: Any) -> None:
        # _push_nav() setzt _pushing_subpage=True während push() läuft.
        # In diesem Fall versteckt sich die sync-Seite hinter einer eigenen
        # Sub-Seite — kein Cleanup, sonst bricht der Server-Thread sofort ab.
        if self._pushing_subpage:
            return
        with self._server_lock:
            self._closed = True
        self._stop_server()
        self._cancel_scanner()
