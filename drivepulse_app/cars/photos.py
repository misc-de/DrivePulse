"""Photo gallery for saved cars — upload (local / camera), grid view, multi-select, delete."""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from drivepulse_app.common import LOG_DIR, _translate
from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)

PHOTOS_DIR = LOG_DIR / "car_photos"
THUMB_SIZE = 160

# Camera source priority — droidcamsrc first for Halium/Furios phones (FuriPhone),
# then PipeWire, libcamera, V4L2, autovideosrc as fallbacks.
_CAMERA_SOURCES = ("droidcamsrc", "pipewiresrc", "libcamerasrc", "v4l2src", "autovideosrc")

_SHUTTER_CSS = b"""
.dp-cam-shutter {
    background-image: none;
    background-color: #e62b2b;
    color: white;
    border: 4px solid white;
    border-radius: 999px;
    min-width: 80px;
    min-height: 80px;
    padding: 0;
    box-shadow: 0 3px 10px rgba(0,0,0,0.45);
}
.dp-cam-shutter:hover { background-color: #ff3a3a; }
.dp-cam-shutter:active { background-color: #b81d1d; }
.dp-cam-shutter:disabled { background-color: #888888; border-color: #cccccc; }

.dp-cam-thumb {
    padding: 0;
    border-radius: 6px;
    border: 2px solid white;
    background-color: black;
    background-image: none;
    min-width: 30px;
    min-height: 60px;
    box-shadow: 0 2px 5px rgba(0,0,0,0.5);
}
.dp-cam-thumb:hover { border-color: #e0e0e0; }
.dp-cam-thumb picture {
    border-radius: 6px;
}

.dp-cam-viewer-bg {
    background-color: rgba(0,0,0,0.93);
}
.dp-cam-viewer-close {
    background-color: rgba(40,40,40,0.7);
    color: white;
    border-radius: 999px;
    min-width: 36px;
    min-height: 36px;
    padding: 0;
}
.dp-cam-viewer-close:hover { background-color: rgba(80,80,80,0.85); }
"""
_shutter_css_loaded = False


def _ensure_shutter_css() -> None:
    global _shutter_css_loaded
    if _shutter_css_loaded:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_SHUTTER_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _shutter_css_loaded = True


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------


class CarsPhotosMixin:

    # ---------------------------------------------------------------- render

    def _render_photos_into_view(self) -> None:
        """Swap the content scroll's child to a photo FlowBox (or restore list)."""
        if self.db is None or self._selected_car_id is None:
            self._value_scroll.set_child(self.value_list)
            self.value_list.append(
                self._info_row(_translate(self.language, "cars.photos.empty"))
            )
            return

        try:
            photos = self.db.list_photos_for_car(self._selected_car_id)
        except Exception:
            log.exception("Could not list photos for car id=%s", self._selected_car_id)
            photos = []

        if not photos:
            self._value_scroll.set_child(self.value_list)
            self.value_list.append(
                self._info_row(_translate(self.language, "cars.photos.empty_hint"))
            )
            return

        # --- FlowBox grid ---
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_homogeneous(True)
        flow.set_column_spacing(8)
        flow.set_row_spacing(8)
        flow.set_margin_top(12)
        flow.set_margin_bottom(12)
        flow.set_margin_start(12)
        flow.set_margin_end(12)
        flow.set_max_children_per_line(20)
        flow.set_min_children_per_line(2)

        for photo in photos:
            flow.append(self._make_photo_tile(photo))

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_hexpand(True)
        outer.set_vexpand(True)
        outer.append(flow)
        self._value_scroll.set_child(outer)

    def _make_photo_tile(self, photo: Any) -> Gtk.FlowBoxChild:
        photo_id = int(photo["id"])
        filename = photo["filename"]

        child = Gtk.FlowBoxChild()
        child.set_focusable(True)

        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        tile.add_css_class("card")
        tile.set_size_request(THUMB_SIZE, THUMB_SIZE + 28)
        tile.set_overflow(Gtk.Overflow.HIDDEN)

        # --- thumbnail ---
        picture = Gtk.Picture()
        picture.set_size_request(THUMB_SIZE, THUMB_SIZE)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_can_shrink(True)

        photo_path = self._photo_path(self._selected_car_id, filename)
        if photo_path.exists():
            picture.set_file(Gio.File.new_for_path(str(photo_path)))

        tile.append(picture)

        # --- date label ---
        ts = self._parse_ts(photo["taken_at"])
        date_str = ts.strftime("%d.%m.%Y") if ts else "—"
        lbl = Gtk.Label(label=date_str)
        lbl.add_css_class("caption")
        lbl.add_css_class("dim-label")
        lbl.set_margin_top(4)
        lbl.set_margin_bottom(4)
        tile.append(lbl)

        keys = photo.keys() if hasattr(photo, "keys") else []
        shared_at = photo["shared_at"] if "shared_at" in keys else None
        seen_at = photo["seen_at"] if "seen_at" in keys else None
        is_new_shared = shared_at and not seen_at

        if self._photo_select_mode:
            overlay = Gtk.Overlay()
            overlay.set_child(tile)
            chk = Gtk.CheckButton()
            chk.set_active(photo_id in self._photo_selected_ids)
            chk.set_valign(Gtk.Align.START)
            chk.set_halign(Gtk.Align.START)
            chk.set_margin_top(4)
            chk.set_margin_start(4)
            chk.connect(
                "toggled",
                lambda c, pid=photo_id: self._on_photo_checkbox_toggled(pid, c.get_active()),
            )
            overlay.add_overlay(chk)
            child.set_child(overlay)
            # Tapping anywhere on the tile (outside the checkbox itself,
            # which the overlay hit-tests on top) toggles selection.
            tile_click = Gtk.GestureClick()
            tile_click.connect(
                "released",
                lambda _g, _n, _x, _y, c=chk: c.set_active(not c.get_active()),
            )
            tile.add_controller(tile_click)
        elif is_new_shared:
            overlay = Gtk.Overlay()
            overlay.set_child(tile)
            dot = Gtk.Label(label="●")
            dot.add_css_class("dp-new-dot")
            dot.set_valign(Gtk.Align.START)
            dot.set_halign(Gtk.Align.END)
            dot.set_margin_top(4)
            dot.set_margin_end(4)
            overlay.add_overlay(dot)
            child.set_child(overlay)
            lp = Gtk.GestureLongPress()
            lp.connect(
                "pressed",
                lambda _g, _x, _y, pid=photo_id: self._enter_photo_select_mode(pid),
            )
            child.add_controller(lp)
            click = Gtk.GestureClick()
            click.connect(
                "released",
                lambda _g, _n, _x, _y, pid=photo_id: self._open_photo_viewer(pid),
            )
            child.add_controller(click)
        else:
            child.set_child(tile)
            lp = Gtk.GestureLongPress()
            lp.connect(
                "pressed",
                lambda _g, _x, _y, pid=photo_id: self._enter_photo_select_mode(pid),
            )
            child.add_controller(lp)
            click = Gtk.GestureClick()
            click.connect(
                "released",
                lambda _g, _n, _x, _y, pid=photo_id: self._open_photo_viewer(pid),
            )
            child.add_controller(click)

        return child

    # ---------------------------------------------------------------- upload dialog

    def _open_upload_dialog(self) -> None:
        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "cars.photos.upload_title"),
            body=_translate(self.language, "cars.photos.upload_body"),
        )
        dialog.add_response("cancel", _translate(self.language, "cars.trip.delete_cancel"))
        dialog.add_response("local", _translate(self.language, "cars.photos.upload_local"))
        dialog.add_response("camera", _translate(self.language, "cars.photos.upload_camera"))
        dialog.set_response_appearance("local", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_response_appearance("camera", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("local")
        dialog.set_close_response("cancel")

        def _on_resp(_d: Adw.AlertDialog, resp: str) -> None:
            if resp == "local":
                self._pick_local_files()
            elif resp == "camera":
                self._open_camera_capture()

        dialog.connect("response", _on_resp)
        dialog.present(self)

    # ---------------------------------------------------------------- local file picker

    def _pick_local_files(self) -> None:
        fd = Gtk.FileDialog()
        fd.set_title(_translate(self.language, "cars.photos.pick_title"))

        store = Gio.ListStore.new(Gtk.FileFilter)
        img_f = Gtk.FileFilter()
        img_f.set_name(_translate(self.language, "cars.photos.filter_images"))
        for mime in ("image/jpeg", "image/png", "image/webp", "image/gif",
                     "image/bmp", "image/tiff", "image/heic"):
            img_f.add_mime_type(mime)
        for pat in ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.bmp", "*.tiff", "*.heic"):
            img_f.add_pattern(pat)
        store.append(img_f)
        fd.set_filters(store)
        fd.set_default_filter(img_f)

        root = self.get_root()
        fd.open_multiple(root, None, self._on_local_files_chosen)

    def _on_local_files_chosen(self, fd: Gtk.FileDialog, result: Any) -> None:
        try:
            files = fd.open_multiple_finish(result)
        except Exception:
            log.debug("FileDialog cancelled or failed", exc_info=True)
            return
        if files is None:
            return
        car_id = self._selected_car_id
        if car_id is None:
            return
        imported = 0
        for i in range(files.get_n_items()):
            gfile = files.get_item(i)
            src = Path(gfile.get_path())
            try:
                self._import_photo_file(car_id, src)
                imported += 1
            except Exception:
                log.exception("Could not import photo %s", src)
        if imported and self._detail_pushed and self._selected_category == "photos":
            self._render_detail()

    def _import_photo_file(self, car_id: int, src: Path) -> None:
        dest_dir = PHOTOS_DIR / str(car_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        ext = src.suffix.lower() or ".jpg"
        ts = datetime.now(UTC)
        base = ts.strftime("%Y%m%d_%H%M%S")
        stem = src.stem[-20:] if len(src.stem) > 20 else src.stem
        filename = f"{base}_{stem}{ext}"
        dest = dest_dir / filename
        counter = 1
        while dest.exists():
            dest = dest_dir / f"{base}_{counter}{ext}"
            counter += 1
        shutil.copy2(str(src), str(dest))
        if self.db is not None:
            self.db.add_car_photo(car_id, dest.name, ts.isoformat())

    # ---------------------------------------------------------------- camera

    def _open_camera_capture(self) -> None:
        car_id = self._selected_car_id
        if car_id is None:
            return

        def _on_captured(jpeg_path: Path) -> None:
            try:
                self._import_camera_jpeg(car_id, jpeg_path)
            except Exception:
                log.exception("Could not save camera photo")
                return
            GLib.idle_add(self._after_camera_import)

        CameraPhotoDialog(self.language, self.get_root(), _on_captured)

    def _after_camera_import(self) -> None:
        if self._detail_pushed and self._selected_category == "photos":
            self._render_detail()

    def _import_camera_jpeg(self, car_id: int, src: Path) -> None:
        dest_dir = PHOTOS_DIR / str(car_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC)
        filename = f"{ts.strftime('%Y%m%d_%H%M%S')}_cam.jpg"
        dest = dest_dir / filename
        counter = 1
        while dest.exists():
            dest = dest_dir / f"{ts.strftime('%Y%m%d_%H%M%S')}_cam_{counter}.jpg"
            counter += 1
        shutil.copy2(str(src), str(dest))
        if self.db is not None:
            self.db.add_car_photo(car_id, dest.name, ts.isoformat())

    # ---------------------------------------------------------------- photo viewer

    def _open_photo_viewer(self, photo_id: int) -> None:
        if self.db is None or self._selected_car_id is None:
            return
        try:
            photos = self.db.list_photos_for_car(self._selected_car_id)
            photo = next((p for p in photos if int(p["id"]) == photo_id), None)
        except sqlite3.Error:
            log.warning("Could not load photo list for car_id=%s", self._selected_car_id, exc_info=True)
            return
        if photo is None:
            return
        photo_path = self._photo_path(self._selected_car_id, photo["filename"])
        if not photo_path.exists():
            return

        try:
            self.db.mark_photo_seen(photo_id)
        except Exception:
            log.exception("Could not mark photo seen id=%s", photo_id)

        picture = Gtk.Picture()
        picture.set_file(Gio.File.new_for_path(str(photo_path)))
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_hexpand(True)
        picture.set_vexpand(True)

        ts = self._parse_ts(photo["taken_at"])
        title = ts.strftime("%d.%m.%Y %H:%M") if ts else str(photo_id)

        self._set_trash(lambda: self._confirm_delete_photo(photo_id))

        def _on_share_photo() -> None:
            from drivepulse_app.share.flow import ShareFlow
            ShareFlow(self, self.db, self.language, self.get_sync_client).share_photos(
                self._selected_car_id, [photo_id]
            )

        page = Adw.NavigationPage(
            child=self._wrap_sub_page(
                picture,
                title,
                on_share=_on_share_photo if self._is_sync_active() else None,
            ),
            title=title,
        )
        page.set_tag(f"photo-{photo_id}")
        self._photo_detail_page = page
        self.nav_view.push(page)

    def _confirm_delete_photo(self, photo_id: int) -> None:
        dialog = self._make_delete_dialog(
            "cars.photos.delete_title", "cars.photos.delete_body"
        )
        dialog.connect(
            "response",
            lambda _d, r: self._delete_photo(photo_id) if r == "delete" else None,
        )
        dialog.present(self)

    def _delete_photo(self, photo_id: int) -> None:
        if self.db is None or self._selected_car_id is None:
            return
        try:
            photos = self.db.list_photos_for_car(self._selected_car_id)
            photo = next((p for p in photos if int(p["id"]) == photo_id), None)
            if photo:
                self.db.delete_car_photo(photo_id)
                self._photo_path(self._selected_car_id, photo["filename"]).unlink(missing_ok=True)
        except Exception:
            log.exception("Could not delete photo id=%s", photo_id)
            return
        if self._photo_detail_page is not None:
            self.nav_view.pop()
            self._photo_detail_page = None
        self._render_detail()

    # ---------------------------------------------------------------- multi-select

    def _enter_photo_select_mode(self, photo_id: int) -> None:
        self._photo_select_mode = True
        self._photo_selected_ids = {photo_id}
        self._render_detail()
        self._set_trash(self._confirm_delete_selected_photos)

    def _exit_photo_select_mode(self) -> None:
        self._photo_select_mode = False
        self._photo_selected_ids = set()
        self._render_detail()
        self._update_trash_default()

    def _on_photo_checkbox_toggled(self, photo_id: int, active: bool) -> None:
        if active:
            self._photo_selected_ids.add(photo_id)
        else:
            self._photo_selected_ids.discard(photo_id)
        if not self._photo_selected_ids:
            self._exit_photo_select_mode()

    def _confirm_delete_selected_photos(self) -> None:
        n = len(self._photo_selected_ids)
        if n == 0:
            return
        dialog = self._make_delete_dialog(
            "cars.photos.delete_title", "cars.photos.delete_title"
        )
        dialog.set_body(_translate(self.language, "cars.photos.delete_multi_body", n=n))
        dialog.connect(
            "response",
            lambda _d, r: self._delete_selected_photos() if r == "delete" else None,
        )
        dialog.present(self)

    def _delete_selected_photos(self) -> None:
        if self.db is None or self._selected_car_id is None:
            return
        try:
            photos = self.db.list_photos_for_car(self._selected_car_id)
            to_delete = [p for p in photos if int(p["id"]) in self._photo_selected_ids]
        except Exception:
            log.exception("Could not list photos for deletion")
            return
        for photo in to_delete:
            try:
                self.db.delete_car_photo(int(photo["id"]))
                self._photo_path(self._selected_car_id, photo["filename"]).unlink(missing_ok=True)
            except Exception:
                log.exception("Could not delete photo id=%s", photo["id"])
        self._exit_photo_select_mode()

    # ---------------------------------------------------------------- helpers

    def _photo_path(self, car_id: int, filename: str) -> Path:
        return PHOTOS_DIR / str(car_id) / filename

    def _update_photo_upload_btn_visibility(self) -> None:
        btn = getattr(self, "_photo_upload_btn", None)
        if btn is None:
            return
        show = (
            self._selected_category == "photos"
            and self._selected_source != self.LIVE_ID
            and self._selected_car_id is not None
            and self._detail_pushed
        )
        btn.set_visible(show)


# ---------------------------------------------------------------------------
# Camera capture dialog
# ---------------------------------------------------------------------------


class CameraPhotoDialog:
    """Modal camera window with live preview, bottom-left thumbnail, and a
    fullscreen Adw.Carousel viewer for session photos.

    Photos auto-save on every shutter press (via ``on_captured``).  The dialog
    keeps temp-file copies of each capture for the in-session thumbnail and
    carousel; those temps are removed when the dialog closes.
    """

    def __init__(
        self,
        language: str,
        parent: Gtk.Widget | None,
        on_captured: Callable[[Path], None],
    ) -> None:
        self._language = language
        self._on_captured = on_captured
        self._pipeline: Any = None
        self._Gst: Any = None
        self._window: Gtk.Window | None = None
        self._preview_picture: Gtk.Picture | None = None
        self._status_lbl: Gtk.Label | None = None
        self._capture_btn: Gtk.Button | None = None
        self._raw_sink: Any = None
        # In-session photo list — temp paths kept alive until the dialog closes
        # so the thumbnail and carousel can display them.  Auto-import (via the
        # on_captured callback) has already happened by the time a path lands
        # here, so the originals on disk are independent of these temps.
        self._session_paths: list[Path] = []
        self._thumb_btn: Gtk.Button | None = None
        self._thumb_pic: Gtk.Picture | None = None
        self._viewer_overlay: Gtk.Overlay | None = None
        self._carousel: Any = None

        _ensure_shutter_css()
        self._build_ui(parent)
        GLib.idle_add(self._start_preview)

    def _t(self, key: str, **kw: object) -> str:
        return _translate(self._language, key, **kw)

    def _build_ui(self, parent: Gtk.Widget | None) -> None:
        win = Gtk.Window()
        win.set_title(self._t("cars.photos.camera_title"))
        win.set_default_size(640, 560)
        win.set_modal(True)
        if parent is not None:
            root = parent.get_root() if hasattr(parent, "get_root") else parent
            if isinstance(root, Gtk.Window):
                win.set_transient_for(root)

        def _on_close_request(_w: Gtk.Window) -> bool:
            self._cleanup()
            return False

        win.connect("close-request", _on_close_request)
        self._window = win

        # --- Preview area ------------------------------------------------------
        self._preview_picture = Gtk.Picture()
        self._preview_picture.set_hexpand(True)
        self._preview_picture.set_vexpand(True)
        self._preview_picture.set_can_shrink(True)
        self._preview_picture.set_size_request(-1, 320)

        # --- Thumbnail (lives in the shutter row, left of the shutter) ---------
        # Picture has NO hexpand/vexpand so its expand flag doesn't propagate
        # up to the button — otherwise the row would stretch the button into a
        # rectangle the full width of the row.
        self._thumb_pic = Gtk.Picture()
        self._thumb_pic.set_can_shrink(True)
        self._thumb_pic.set_content_fit(Gtk.ContentFit.COVER)

        self._thumb_btn = Gtk.Button()
        self._thumb_btn.add_css_class("dp-cam-thumb")
        self._thumb_btn.set_child(self._thumb_pic)
        self._thumb_btn.set_size_request(30, 60)
        self._thumb_btn.set_hexpand(False)
        self._thumb_btn.set_vexpand(False)
        self._thumb_btn.set_halign(Gtk.Align.CENTER)
        self._thumb_btn.set_valign(Gtk.Align.CENTER)
        self._thumb_btn.set_overflow(Gtk.Overflow.HIDDEN)  # clip picture to the rounded tile
        self._thumb_btn.set_visible(False)
        self._thumb_btn.connect("clicked", lambda _b: self._show_viewer())

        # --- Status + shutter row ----------------------------------------------
        # Layout: [thumb 60×60] [shutter 80×80] [spacer 60×60]
        # Symmetric spacer keeps the shutter visually centred on the screen
        # even before the first capture (when the thumbnail is hidden but its
        # 60×60 slot is still reserved).
        self._status_lbl = Gtk.Label(label="")
        self._status_lbl.add_css_class("dim-label")

        self._capture_btn = Gtk.Button()
        self._capture_btn.add_css_class("dp-cam-shutter")
        self._capture_btn.set_tooltip_text(self._t("cars.photos.camera_capture"))
        self._capture_btn.set_halign(Gtk.Align.CENTER)
        self._capture_btn.set_valign(Gtk.Align.CENTER)
        self._capture_btn.connect("clicked", lambda _b: self._do_capture())

        thumb_slot = Gtk.Box()
        thumb_slot.set_size_request(30, 60)
        thumb_slot.set_valign(Gtk.Align.CENTER)
        thumb_slot.append(self._thumb_btn)

        spacer = Gtk.Box()
        spacer.set_size_request(30, 60)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        btn_row.set_halign(Gtk.Align.CENTER)
        btn_row.set_margin_top(8)
        btn_row.set_margin_bottom(60)
        btn_row.append(thumb_slot)
        btn_row.append(self._capture_btn)
        btn_row.append(spacer)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        main_box.set_margin_top(8)
        main_box.set_margin_start(8)
        main_box.set_margin_end(8)
        main_box.append(self._preview_picture)
        main_box.append(self._status_lbl)
        main_box.append(btn_row)

        # --- Root overlay: main UI + fullscreen viewer --------------------------
        root_overlay = Gtk.Overlay()
        root_overlay.set_child(main_box)
        root_overlay.add_overlay(self._build_viewer())

        win.set_child(root_overlay)
        win.present()

    def _build_viewer(self) -> Gtk.Overlay:
        """Fullscreen carousel viewer — hidden by default, tap-outside dismisses."""
        viewer = Gtk.Overlay()
        viewer.set_visible(False)
        viewer.set_hexpand(True)
        viewer.set_vexpand(True)
        self._viewer_overlay = viewer

        # Dim background — clicks here (i.e. NOT on the centered carousel) dismiss.
        dim = Gtk.Box()
        dim.set_hexpand(True)
        dim.set_vexpand(True)
        dim.add_css_class("dp-cam-viewer-bg")
        dim_click = Gtk.GestureClick()
        dim_click.connect("released", lambda *_a: self._hide_viewer())
        dim.add_controller(dim_click)
        viewer.set_child(dim)

        # Centered carousel + dot indicator.  The carousel sits inside a vertical
        # box that is centered both horizontally and vertically; clicks landing
        # outside this box hit the dim layer above and dismiss the viewer.
        self._carousel = Adw.Carousel()
        self._carousel.set_hexpand(True)
        self._carousel.set_vexpand(True)
        self._carousel.set_spacing(12)
        self._carousel.set_allow_long_swipes(True)

        dots = Adw.CarouselIndicatorDots()
        dots.set_carousel(self._carousel)
        dots.set_halign(Gtk.Align.CENTER)
        dots.set_margin_top(8)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.set_halign(Gtk.Align.FILL)
        content.set_valign(Gtk.Align.FILL)
        content.set_margin_top(48)
        content.set_margin_bottom(24)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.append(self._carousel)
        content.append(dots)
        viewer.add_overlay(content)

        # Close (X) button anchored top-right of the viewer.
        close_x = Gtk.Button(icon_name="window-close-symbolic")
        close_x.add_css_class("dp-cam-viewer-close")
        close_x.set_halign(Gtk.Align.END)
        close_x.set_valign(Gtk.Align.START)
        close_x.set_margin_top(8)
        close_x.set_margin_end(8)
        close_x.connect("clicked", lambda _b: self._hide_viewer())
        viewer.add_overlay(close_x)

        return viewer

    # ---------- preview pipeline ----------

    def _start_preview(self) -> None:
        try:
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            Gst.init(None)
            self._Gst = Gst
        except Exception:
            self._set_status(self._t("cars.photos.camera_no_camera"))
            if self._capture_btn:
                self._capture_btn.set_sensitive(False)
            return

        Gst = self._Gst
        has_paintable = Gst.ElementFactory.find("gtk4paintablesink") is not None
        sources = [s for s in _CAMERA_SOURCES if Gst.ElementFactory.find(s) is not None]
        if not sources:
            self._set_status(self._t("cars.photos.camera_no_camera"))
            if self._capture_btn:
                self._capture_btn.set_sensitive(False)
            return

        # Capture branch: a continuously-consuming RGB appsink keeps the latest
        # raw frame for the shutter.  Using a valve drop=true here would stall
        # the tee (downstream never prerolls), so instead the appsink always
        # eats buffers and we encode the latest one to JPEG via GdkPixbuf only
        # on shutter press.
        cap_branch = (
            "t. ! queue leaky=downstream max-size-buffers=2 "
            "! videoconvert ! video/x-raw,format=RGB "
            "! appsink name=rawsink max-buffers=1 drop=true sync=false emit-signals=false"
        )
        if has_paintable:
            preview_branch = (
                "t. ! queue leaky=downstream max-size-buffers=2 "
                "! videoconvert ! gtk4paintablesink name=preview sync=false"
            )
        else:
            # No GTK4 paintable sink available — still keep the pipeline flowing
            # via fakesink so the capture branch can pull samples.
            preview_branch = "t. ! queue leaky=downstream max-size-buffers=2 ! fakesink sync=false"

        for src_name in sources:
            try:
                desc = (
                    f"{src_name} ! videoconvert ! tee name=t "
                    f"{preview_branch} "
                    f"{cap_branch}"
                )
                pipe = Gst.parse_launch(desc)
                ret = pipe.set_state(Gst.State.PLAYING)
                if ret == Gst.StateChangeReturn.FAILURE:
                    pipe.set_state(Gst.State.NULL)
                    continue
                if has_paintable:
                    sink = pipe.get_by_name("preview")
                    if sink and self._preview_picture:
                        paintable = sink.get_property("paintable")
                        if paintable:
                            self._preview_picture.set_paintable(paintable)
                self._raw_sink = pipe.get_by_name("rawsink")
                bus = pipe.get_bus()
                bus.add_signal_watch()
                bus.connect("message", self._on_bus_msg)
                self._pipeline = pipe
                log.info("camera preview running: src=%s", src_name)
                break
            except Exception:
                log.debug("Gst source %r unavailable, trying next", src_name, exc_info=True)
                continue

        if self._pipeline is None:
            self._set_status(self._t("cars.photos.camera_no_camera"))
            if self._capture_btn:
                self._capture_btn.set_sensitive(False)

    def _on_bus_msg(self, _bus: Any, message: Any) -> None:
        if self._Gst and message.type == self._Gst.MessageType.ERROR:
            err, _ = message.parse_error()
            GLib.idle_add(self._set_status, str(err))

    def _set_status(self, text: str) -> None:
        if self._status_lbl:
            self._status_lbl.set_text(text)

    # ---------- capture ----------

    def _do_capture(self) -> None:
        if self._capture_btn:
            self._capture_btn.set_sensitive(False)

        # NamedTemporaryFile with delete=False: closed here but kept on disk for capture thread
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)  # noqa: SIM115
        tmp.close()
        tmp_path = Path(tmp.name)

        t = threading.Thread(
            target=self._capture_thread,
            args=(tmp_path,),
            daemon=True,
        )
        t.start()

    def _capture_thread(self, dest: Path) -> None:
        """Pull the latest raw RGB frame from the appsink, encode to JPEG.

        The preview pipeline always consumes raw frames into a max-buffers=1
        drop=true appsink, so the sensor stays warmed up and the freshest frame
        is always immediately available.  We encode that frame to JPEG via
        GdkPixbuf — no second GStreamer pipeline, no valve, no tee-stall risk.
        """
        Gst = self._Gst
        sink = self._raw_sink

        if Gst is None or sink is None:
            log.debug("Camera capture: raw appsink unavailable")
            GLib.idle_add(self._on_capture_failed)
            return

        try:
            # Wait up to 2 s for a fresh sample.  On a healthy pipeline the
            # newest frame is already sitting in the appsink, so this returns
            # immediately.
            sample = sink.emit("try-pull-sample", 2 * Gst.SECOND)
            if sample is None:
                log.warning("Camera capture: no raw sample within timeout")
                GLib.idle_add(self._on_capture_failed)
                return

            caps = sample.get_caps()
            structure = caps.get_structure(0) if caps else None
            if structure is None:
                log.warning("Camera capture: sample has no caps")
                GLib.idle_add(self._on_capture_failed)
                return
            ok_w, width = structure.get_int("width")
            ok_h, height = structure.get_int("height")
            if not (ok_w and ok_h and width > 0 and height > 0):
                log.warning("Camera capture: invalid dimensions w=%s h=%s", width, height)
                GLib.idle_add(self._on_capture_failed)
                return

            buf = sample.get_buffer()
            ok, mi = buf.map(Gst.MapFlags.READ)
            if not ok:
                log.warning("Camera capture: could not map raw buffer")
                GLib.idle_add(self._on_capture_failed)
                return
            try:
                # Copy out of the mapped GStreamer buffer before we unmap.
                raw_bytes = bytes(mi.data)
            finally:
                buf.unmap(mi)

            from gi.repository import GdkPixbuf
            gbytes = GLib.Bytes.new(raw_bytes)
            pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
                gbytes,
                GdkPixbuf.Colorspace.RGB,
                False,        # has_alpha
                8,            # bits_per_sample
                width,
                height,
                width * 3,    # rowstride (RGB = 3 bytes/pixel)
            )
            pixbuf.savev(str(dest), "jpeg", ["quality"], ["92"])
        except Exception:
            log.exception("Camera capture failed")
            GLib.idle_add(self._on_capture_failed)
            return

        if dest.exists() and dest.stat().st_size > 0:
            GLib.idle_add(self._on_capture_done, dest)
        else:
            GLib.idle_add(self._on_capture_failed)

    def _on_capture_done(self, path: Path) -> None:
        # Auto-import the freshly captured JPEG into the car gallery — the
        # session no longer has a separate "Save" step.
        try:
            self._on_captured(path)
        except Exception:
            log.exception("Camera on_captured callback failed")

        self._session_paths.append(path)
        self._refresh_thumbnail(path)
        self._append_to_carousel(path)

        if self._capture_btn:
            self._capture_btn.set_sensitive(True)

    def _on_capture_failed(self) -> None:
        self._set_status(self._t("cars.photos.camera_no_camera"))
        if self._capture_btn:
            self._capture_btn.set_sensitive(True)
        # Preview pipeline is still running, no need to restart.

    # ---------- thumbnail + carousel ----------

    def _refresh_thumbnail(self, latest: Path) -> None:
        if self._thumb_pic is None or self._thumb_btn is None:
            return
        self._thumb_pic.set_file(Gio.File.new_for_path(str(latest)))
        self._thumb_btn.set_visible(True)

    def _append_to_carousel(self, path: Path) -> None:
        if self._carousel is None:
            return
        pic = Gtk.Picture()
        pic.set_file(Gio.File.new_for_path(str(path)))
        pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        pic.set_hexpand(True)
        pic.set_vexpand(True)
        self._carousel.append(pic)

    def _show_viewer(self) -> None:
        if self._viewer_overlay is None or self._carousel is None:
            return
        if not self._session_paths:
            return
        self._viewer_overlay.set_visible(True)
        # Newest photo first: scroll to the last carousel page.
        last_idx = self._carousel.get_n_pages() - 1
        if last_idx >= 0:
            page = self._carousel.get_nth_page(last_idx)
            if page is not None:
                self._carousel.scroll_to(page, False)

    def _hide_viewer(self) -> None:
        if self._viewer_overlay is not None:
            self._viewer_overlay.set_visible(False)

    # ---------- close ----------

    def _close(self) -> None:
        self._cleanup()
        if self._window:
            self._window.close()

    def _cleanup(self) -> None:
        self._stop_pipeline()
        for path in self._session_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.debug("Could not unlink session temp %s", path, exc_info=True)
        self._session_paths.clear()

    def _stop_pipeline(self) -> None:
        if self._pipeline is not None:
            try:
                self._pipeline.set_state(self._Gst.State.NULL)
            except Exception:
                log.debug("Gst pipeline state-reset failed", exc_info=True)
            self._pipeline = None
        self._raw_sink = None
