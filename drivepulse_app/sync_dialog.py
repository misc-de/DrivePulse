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
from .sync_data import export_all, import_data
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

# (mode_constant, title_key, subtitle_key, is_destructive)
_CLIENT_OPTIONS = [
    (MODE_MERGE,           "sync.opt.merge",          "sync.opt.merge.sub",            False),
    (MODE_LOCAL_WINS,      "sync.opt.send_new",        "sync.opt.send_new.sub.client",  False),
    (MODE_REMOTE_WINS,     "sync.opt.fetch_new",       "sync.opt.fetch_new.sub.client", False),
    (MODE_LOCAL_WINS_ALL,  "sync.opt.push_all",        "sync.opt.push_all.sub.client",  True),
    (MODE_REMOTE_WINS_ALL, "sync.opt.pull_all.client", "sync.opt.pull_all.sub.client",  True),
]
# Modes are stored as client-perspective (what the client will execute)
_SERVER_OPTIONS = [
    (MODE_MERGE,            "sync.opt.merge",          "sync.opt.merge.sub",            False),
    (MODE_REMOTE_WINS,      "sync.opt.send_new",       "sync.opt.send_new.sub.server",  False),
    (MODE_LOCAL_WINS,       "sync.opt.fetch_new",      "sync.opt.fetch_new.sub.server", False),
    (MODE_REMOTE_WINS_ALL,  "sync.opt.push_all",       "sync.opt.push_all.sub.server",  True),
    (MODE_LOCAL_WINS_ALL,   "sync.opt.pull_all.server","sync.opt.pull_all.sub.server",  True),
]
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
        on_connected: Callable[[str, str], None] | None = None,
        on_disconnected: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(tag="sync")
        self._language = language
        self._db = db
        self._on_sync_complete = on_sync_complete
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._server: SyncServer | None = None
        self._server_lock = threading.RLock()
        self._server_start_requested = False
        self._server_start_generation = 0
        self._closed = False
        self._pushing_subpage = False  # True während outer_nav.push() läuft
        self._server_survived_dialog = False  # Server läuft weiter nach Dialog-Schließen
        self._client_paired = False  # Client hat erfolgreich gepairt
        self._scanner: WebcamQRScanner | None = None
        self._sync_mode: str = MODE_MERGE
        self._keepalive_stop = threading.Event()
        self._sync_feedback_label: Gtk.Label | None = None
        # Outer app NavigationView — all sub-pages are pushed here so swipe
        # always works; there is no inner NavigationView.
        self._outer_nav: Adw.NavigationView | None = getattr(parent, "nav_view", None)

        self.set_title(self._t("sync.title"))
        self.set_child(self._build_home_content())
        self.connect("hiding", self._on_hiding)
        self.connect("showing", self._on_showing)

        if initial_mode == "client":
            GLib.idle_add(self._push_qr_scan_page)
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
        client_btn.connect("clicked", lambda _b: self._push_qr_scan_page())
        client_row.add_suffix(client_btn)
        client_row.set_activatable_widget(client_btn)
        start_group.add(client_row)

        box.append(start_group)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(box)
        toolbar_view.set_content(scroll)
        return toolbar_view

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

        page.connect("hiding", lambda _p: None if self._server_survived_dialog else self._stop_server())
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
                self._server_survived_dialog = True
                if self._on_connected:
                    _name = device_info.get("hostname") or local_ip
                    _ip = device_info.get("client_ip", "")
                    GLib.idle_add(self._on_connected, _name, _ip)
                GLib.idle_add(self._close_sync_dialog)

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
                self._server_survived_dialog = False
                if self._on_disconnected:
                    GLib.idle_add(self._on_disconnected)

            def _on_vehicle_check(vin_hash: str) -> bool:
                return self._db.get_car_by_vin_hash(vin_hash) is not None

            def _on_share_import(payload: dict) -> dict:
                from .share_protocol import share_import as _share_import
                result = _share_import(self._db, payload)
                added = (
                    result.get("trips_added", 0)
                    + result.get("runs_added", 0)
                    + result.get("scans_added", 0)
                )
                if self._on_sync_complete and added > 0:
                    GLib.idle_add(self._on_sync_complete)
                return result

            server = SyncServer(
                CERT_PATH, KEY_PATH,
                pairing_token=pairing_token,
                session_token=session_token,
                on_paired_cb=_on_paired,
                get_export_fn=lambda: export_all(self._db),
                on_import_fn=_on_import,
                on_timeout_cb=_on_timeout,
                on_vehicle_check_fn=_on_vehicle_check,
                on_share_import_fn=_on_share_import,
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

    def _push_qr_scan_page(self) -> None:
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
                log.warning("_do_pair: QR code expired")
                GLib.idle_add(_set, self._t("sync.client.expired"))
                return
            except ValueError as exc:
                log.warning("_do_pair: URL invalid: %s", exc)
                GLib.idle_add(_set, self._t("sync.error", error=str(exc)))
                return

            log.info("_do_pair: pairing ok, host=%s port=%s", pairing.host, pairing.port)
            device_id = get_or_create_device_id()
            client = SyncClient(pairing.host, pairing.port, pairing.spki_fingerprint, device_id)

            log.info("_do_pair: verify fingerprint…")
            if not client.verify_fingerprint():
                log.warning("_do_pair: fingerprint verification failed")
                GLib.idle_add(_set, self._t("sync.client.fp_error"))
                return
            log.info("_do_pair: Fingerprint ok, pairing…")
            if not client.pair(pairing.pairing_token):
                log.warning("_do_pair: pairing failed")
                GLib.idle_add(_set, self._t("sync.error", error="Pairing failed"))
                return
            log.info("_do_pair: pairing successful")

            self._active_client = client
            self._keepalive_stop.clear()
            threading.Thread(
                target=self._keepalive_loop, args=(client,), daemon=True, name="sync-keepalive"
            ).start()
            self._active_host = pairing.host
            self._active_port = pairing.port
            self._active_spki_fp = pairing.spki_fingerprint
            self._active_server_hostname = client.server_hostname or pairing.host
            self._client_paired = True
            if self._on_connected:
                _name = self._active_server_hostname or self._active_host
                GLib.idle_add(self._on_connected, _name, self._active_host)
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
        hostname = getattr(self, "_active_server_hostname", self._active_host)
        page, self._paired_status_label = self._build_paired_page(hostname, self._active_host)
        self._push_nav(page)
        return False

    def _build_paired_page(self, hostname: str, ip: str) -> tuple[Adw.NavigationPage, Gtk.Label]:
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=hostname or ip))
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        toolbar_view.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(16)
        box.set_margin_end(16)

        sync_icon = Gtk.Image.new_from_icon_name("arrows-loop-symbolic")
        sync_icon.set_pixel_size(64)
        sync_icon.add_css_class("success")
        sync_icon.set_halign(Gtk.Align.CENTER)
        box.append(sync_icon)

        connected_label = Gtk.Label(label=self._t("sync.paired.connected_to", host=hostname or ip))
        connected_label.add_css_class("title-2")
        connected_label.set_halign(Gtk.Align.CENTER)
        connected_label.set_wrap(True)
        connected_label.set_justify(Gtk.Justification.CENTER)
        box.append(connected_label)

        if hostname and hostname != ip and ip:
            ip_label = Gtk.Label(label=ip)
            ip_label.add_css_class("dim-label")
            ip_label.set_halign(Gtk.Align.CENTER)
            box.append(ip_label)

        status_label = Gtk.Label(label="")
        status_label.set_wrap(True)
        status_label.set_halign(Gtk.Align.CENTER)
        status_label.set_justify(Gtk.Justification.CENTER)
        box.append(status_label)

        complete_btn = Gtk.Button(label=self._t("sync.paired.complete_btn"))
        complete_btn.add_css_class("suggested-action")
        complete_btn.set_halign(Gtk.Align.FILL)
        complete_btn.connect("clicked", lambda _b: self._complete_without_sync())
        box.append(complete_btn)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(box)
        toolbar_view.set_content(scroll)

        page = Adw.NavigationPage()
        page.set_tag("sync-paired")
        page.set_title(hostname or ip)
        page.set_child(toolbar_view)
        return page, status_label

    def _close_sync_dialog(self) -> bool:
        """Pop all sync sub-pages, then pop the sync home page itself."""
        # Nur grau schalten wenn kein Pairing stattgefunden hat (abgebrochen).
        # Server-Disconnect wird über den Session-Timeout gemeldet.
        if not self._server_survived_dialog and not self._client_paired:
            if self._on_disconnected:
                self._on_disconnected()
        if self._outer_nav is None:
            return False
        if self._outer_nav.find_page("sync") is not None:
            self._outer_nav.pop_to_tag("sync")
            self._outer_nav.pop()
        return False  # compatible with GLib.timeout_add

    def _complete_without_sync(self) -> None:
        if self._on_sync_complete:
            self._on_sync_complete()
        self._close_sync_dialog()

    def set_sync_feedback_label(self, label: Gtk.Label) -> None:
        self._sync_feedback_label = label

    def _show_sync_options_dialog(self, status_label: Gtk.Label, is_server: bool = False) -> None:
        root = self._outer_nav.get_root() if self._outer_nav else None
        parent_window = root if isinstance(root, Gtk.Window) else None

        options = _SERVER_OPTIONS if is_server else _CLIENT_OPTIONS

        dialog = Adw.MessageDialog.new(parent_window, self._t("sync.options.title"), "")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        content.set_margin_top(8)
        content.set_margin_bottom(4)

        mode_group = Adw.PreferencesGroup()
        content.append(mode_group)

        selected_idx = [0]
        checks: list[tuple[int, Gtk.CheckButton]] = []
        first_check: Gtk.CheckButton | None = None

        for i, (_, title_key, sub_key, _destr) in enumerate(options):
            check = Gtk.CheckButton()
            check.set_valign(Gtk.Align.CENTER)
            if first_check is None:
                check.set_active(True)
                first_check = check
            else:
                check.set_group(first_check)
            row = Adw.ActionRow()
            row.set_title(self._t(title_key))
            row.set_subtitle(self._t(sub_key))
            row.add_prefix(check)
            row.set_activatable_widget(check)
            mode_group.add(row)
            checks.append((i, check))

        def _on_toggled(*_: Any) -> None:
            for idx, chk in checks:
                if chk.get_active():
                    selected_idx[0] = idx
                    break

        for _, chk in checks:
            chk.connect("toggled", _on_toggled)

        dialog.set_extra_child(content)
        dialog.add_response("cancel", self._t("sync.options.cancel_btn"))
        dialog.add_response("next", self._t("sync.options.start_btn"))
        dialog.set_response_appearance("next", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("next")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.MessageDialog, response_id: str) -> None:
            if response_id != "next":
                return
            mode, title_key, sub_key, is_destructive = options[selected_idx[0]]
            self._show_sync_confirm_dialog(
                parent_window, mode, title_key, sub_key, is_destructive, status_label, is_server
            )

        dialog.connect("response", _on_response)
        dialog.present()

    def _show_sync_confirm_dialog(
        self,
        parent_window: Any,
        mode: str,
        title_key: str,
        sub_key: str,
        is_destructive: bool,
        status_label: Gtk.Label,
        is_server: bool,
    ) -> None:
        body = self._t(sub_key)
        if is_destructive:
            body = f"{body}\n\n{self._t('sync.confirm.destructive')}"
        confirm = Adw.MessageDialog.new(parent_window, self._t(title_key), body)
        confirm.add_response("cancel", self._t("sync.confirm.cancel_btn"))
        confirm.add_response("start", self._t("sync.confirm.start_btn"))
        confirm.set_response_appearance(
            "start",
            Adw.ResponseAppearance.DESTRUCTIVE if is_destructive else Adw.ResponseAppearance.SUGGESTED,
        )
        confirm.set_default_response("cancel" if is_destructive else "start")
        confirm.set_close_response("cancel")

        def _on_confirm(_d: Adw.MessageDialog, response_id: str) -> None:
            if response_id != "start":
                return
            if is_server:
                server = getattr(self, "_server", None)
                if server is not None:
                    server.pending_sync_mode = mode
                    status_label.set_text(self._t("sync.server.sync_requested"))
            else:
                status_label.set_text(self._t("sync.client.connecting"))
                threading.Thread(
                    target=self._do_sync,
                    args=(self._active_client, mode, status_label),
                    daemon=True,
                ).start()

        confirm.connect("response", _on_confirm)
        confirm.present()

    def _do_sync(
        self,
        client: SyncClient,
        mode: str,
        status_label: Gtk.Label,
        sync_btn: Gtk.Button | None = None,
        close_after: bool = True,
    ) -> None:
        def _set(msg: str) -> bool:
            status_label.set_text(msg)
            return False

        def _done(msg: str) -> bool:
            status_label.set_text(msg)
            if sync_btn is not None:
                sync_btn.set_sensitive(True)
            return False

        try:
            result = perform_sync(self._db, client, mode)

            msg = self._t(
                "sync.complete",
                cars=result["cars"],
                trips=result["trips"],
                samples=result["samples"],
            )
            GLib.idle_add(_done, msg)
            if self._on_sync_complete:
                GLib.idle_add(self._on_sync_complete)
            if close_after:
                GLib.timeout_add(1500, self._close_sync_dialog)

        except Exception as exc:
            log.exception("Sync operation failed")
            GLib.idle_add(_done, self._t("sync.error", error=str(exc)))

    def _trigger_pending_sync(self, mode: str) -> bool:
        label = self._sync_feedback_label
        if label is None:
            label = Gtk.Label()
        label.set_text(self._t("sync.client.connecting"))
        client = getattr(self, "_active_client", None)
        if client is None:
            return False
        threading.Thread(
            target=self._do_sync,
            args=(client, mode, label),
            kwargs={"close_after": False},
            daemon=True,
        ).start()
        return False

    def _pull_and_import_share(self, client: SyncClient) -> None:
        try:
            payload = client.pull_pending_share()
            if payload is None:
                return
            from .share_protocol import share_import as _share_import
            result = _share_import(self._db, payload)
            added = (
                result.get("trips_added", 0)
                + result.get("runs_added", 0)
                + result.get("scans_added", 0)
                + result.get("photos_added", 0)
                + result.get("tours_added", 0)
            )
            if self._on_sync_complete and added > 0:
                GLib.idle_add(self._on_sync_complete)
        except Exception:
            log.exception("Could not pull/import pending share from server")

    def _keepalive_loop(self, client: SyncClient) -> None:
        """Pingt den Server alle 10 Sekunden.
        False (ECONNREFUSED) → sofort trennen.
        None (flüchtiger Fehler) → erst nach 3 aufeinanderfolgenden Fehlern trennen."""
        failures = 0
        while not self._keepalive_stop.wait(10):
            result = client.ping()
            if result is True:
                failures = 0
                pending = client.get_pending_sync()
                if pending:
                    client.mark_pending_scheduled()
                    GLib.idle_add(lambda m=pending: self._trigger_pending_sync(m))
                if client.get_pending_share():
                    client.mark_pending_share_scheduled()
                    threading.Thread(
                        target=self._pull_and_import_share,
                        args=(client,),
                        daemon=True,
                    ).start()
            elif result is False:
                log.info("Server actively disconnected — disconnecting immediately")
                GLib.idle_add(self.disconnect)
                return
            else:
                failures += 1
                log.warning("Keepalive ping transient error (%d/3)", failures)
                if failures >= 3:
                    log.info("3 consecutive errors — disconnecting")
                    GLib.idle_add(self.disconnect)
                    return

    def disconnect(self) -> None:
        """Verbindung sofort trennen — stoppt den Server und benachrichtigt on_disconnected."""
        self._keepalive_stop.set()
        if self._client_paired:
            client = getattr(self, "_active_client", None)
            if client is not None:
                threading.Thread(target=client.disconnect, daemon=True).start()
        self._stop_server()
        self._server_survived_dialog = False
        self._client_paired = False
        if self._on_disconnected:
            self._on_disconnected()

    def get_last_contact(self) -> float:
        """Gibt den Zeitstempel des letzten Datenaustausches zurück (0 = unbekannt)."""
        server = getattr(self, "_server", None)
        if server is not None and server.last_activity > 0:
            return server.last_activity
        client = getattr(self, "_active_client", None)
        if client is not None and client.last_contact > 0:
            return client.last_contact
        return 0.0

    def get_last_ping(self) -> float:
        """Gibt den Zeitstempel des letzten Keepalive-Pings zurück (0 = unbekannt)."""
        server = getattr(self, "_server", None)
        if server is not None and server.last_ping > 0:
            return server.last_ping
        client = getattr(self, "_active_client", None)
        if client is not None and client.last_ping > 0:
            return client.last_ping
        return 0.0

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
        if not self._server_survived_dialog:
            self._stop_server()


class ServerShareClient:
    """Wraps SyncServer so share icons work when this device is the server.

    Stores the payload in the server's pending-share queue; the connected
    client picks it up on its next keepalive ping via /share/pending.
    """

    def __init__(self, server: "SyncServer") -> None:
        self._server = server

    def vehicle_check(self, vin_hash: str) -> bool | None:
        return True

    def share_import(self, payload: dict) -> dict | None:
        self._server.set_pending_share(payload)
        return {"ok": True, "queued": True}
        self._cancel_scanner()
