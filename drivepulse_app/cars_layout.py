"""Layout construction for the Cars page."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .common import _translate
from .cars_metadata import CATEGORIES


class CarsLayoutMixin:
    def _build_list_page(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_margin_top(16)
        page.set_margin_bottom(16)
        page.set_margin_start(16)
        page.set_margin_end(16)

        # Live-Verbindung wird oben fest verankert — bleibt sichtbar wenn die
        # Autoliste unten scrollt.
        self._live_list_box = Gtk.ListBox()
        self._live_list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._live_list_box.add_css_class("boxed-list")
        self._live_list_box.set_valign(Gtk.Align.START)
        page.append(self._live_list_box)

        # Scroll-Bereich nur für die Auto-Liste
        scroll_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_valign(Gtk.Align.START)
        scroll_inner.append(self._list_box)

        self._empty_label = Gtk.Label(xalign=0.0)
        self._empty_label.add_css_class("dim-label")
        self._empty_label.set_wrap(True)
        self._empty_label.set_visible(False)
        scroll_inner.append(self._empty_label)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_hexpand(True)
        scroll.set_child(scroll_inner)
        page.append(scroll)

        self._list_page = Adw.NavigationPage(
            child=page,
            title=_translate(self.language, "nav.cars"),
        )
        self._list_page.set_tag("list")
        self._split_view.set_sidebar(self._list_page)
        self._refresh_list_texts()

    def _refresh_list_texts(self) -> None:
        self._empty_label.set_text(_translate(self.language, "cars.empty"))

    # ---------------------------------------------------- Detail-Aufbau

    def _build_detail_page(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.set_margin_start(8)
        head.set_margin_top(8)
        head.set_margin_end(12)
        head.set_margin_bottom(4)
        self._detail_back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        self._detail_back_btn.add_css_class("flat")
        self._detail_back_btn.set_tooltip_text(_translate(self.language, "cars.back"))
        self._detail_back_btn.connect("clicked", lambda _b: self._on_detail_back())
        self._detail_title = Gtk.Label(xalign=0.0)
        self._detail_title.add_css_class("title-3")
        self._detail_title.set_hexpand(True)

        self._rename_btn = Gtk.Button(icon_name="document-edit-symbolic")
        self._rename_btn.add_css_class("flat")
        self._rename_btn.set_visible(False)
        self._rename_btn.connect("clicked", lambda _b: self._open_rename_dialog())

        self._vin_refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        self._vin_refresh_btn.add_css_class("flat")
        self._vin_refresh_btn.set_visible(False)
        self._vin_refresh_btn.set_tooltip_text(_translate(self.language, "cars.vin_refresh.tooltip"))
        self._vin_refresh_btn.connect("clicked", lambda _b: self._reset_and_refetch_vin())

        self._add_live_vehicle_btn = Gtk.Button(icon_name="list-add-symbolic")
        self._add_live_vehicle_btn.add_css_class("suggested-action")
        self._add_live_vehicle_btn.set_visible(False)
        self._add_live_vehicle_btn.set_tooltip_text(_translate(self.language, "cars.live.add.tooltip"))
        self._add_live_vehicle_btn.connect("clicked", lambda _b: self._confirm_add_live_vehicle())

        self._detail_share_btn = Gtk.Button(icon_name="share-alt-symbolic")
        self._detail_share_btn.add_css_class("flat")
        self._detail_share_btn.set_visible(False)
        self._detail_share_btn.connect("clicked", lambda _b: self._on_share_btn_clicked())

        self._photo_upload_btn = Gtk.Button(icon_name="list-add-symbolic")
        self._photo_upload_btn.add_css_class("flat")
        self._photo_upload_btn.set_visible(False)
        self._photo_upload_btn.set_tooltip_text(_translate(self.language, "cars.photos.upload_tooltip"))
        self._photo_upload_btn.connect("clicked", lambda _b: self._open_upload_dialog())

        self._detail_trash_btn = Gtk.Button(icon_name="user-trash-symbolic")
        self._detail_trash_btn.add_css_class("flat")
        self._detail_trash_btn.set_visible(False)
        self._detail_trash_handler: int | None = None

        head.append(self._detail_back_btn)
        head.append(self._detail_title)
        head.append(self._add_live_vehicle_btn)
        head.append(self._rename_btn)
        head.append(self._vin_refresh_btn)
        head.append(self._detail_share_btn)
        head.append(self._photo_upload_btn)
        head.append(self._detail_trash_btn)
        outer.append(head)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.set_hexpand(True)
        body.set_vexpand(True)
        self._detail_body = body

        self._sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._sidebar.set_margin_top(12)
        self._sidebar.set_margin_bottom(12)
        self._sidebar.set_margin_start(8)
        self._sidebar.set_margin_end(4)

        self.category_list = Gtk.ListBox()
        self.category_list.set_selection_mode(Gtk.SelectionMode.BROWSE)
        self.category_list.add_css_class("navigation-sidebar")
        self.category_list.connect("row-selected", self._on_category_selected)
        self._cat_section_rows: list[Gtk.ListBoxRow] = []

        # First row: scan timestamp of the currently loaded vehicle data
        # rendered as a three-line centred stack (year dimmed / MM.DD /
        # HH:MM via Pango markup). Hidden for the live view and when no
        # scan date is available.
        scan_date_row = Gtk.ListBoxRow()
        scan_date_row.set_selectable(False)
        scan_date_row.set_activatable(False)
        self._scan_date_label = Gtk.Label()
        self._scan_date_label.set_use_markup(True)
        self._scan_date_label.set_justify(Gtk.Justification.CENTER)
        self._scan_date_label.set_xalign(0.5)
        self._scan_date_label.set_halign(Gtk.Align.CENTER)
        self._scan_date_label.add_css_class("caption-heading")
        self._scan_date_label.set_margin_top(6)
        self._scan_date_label.set_margin_bottom(6)
        scan_date_row.set_child(self._scan_date_label)
        scan_date_row.set_visible(False)
        self._scan_date_row = scan_date_row
        self.category_list.append(scan_date_row)
        # Categories before this key get appended to the top of the list;
        # everything from this key onward sits below an "OBD Daten" section
        # divider — those are the live-OBD-derived value groups.
        _OBD_SECTION_FIRST = "engine"
        for cat_key, cat_name_key, icon_name, _items in CATEGORIES:
            if cat_key == _OBD_SECTION_FIRST:
                sep_row = Gtk.ListBoxRow()
                sep_row.set_selectable(False)
                sep_row.set_activatable(False)
                sep_lbl = Gtk.Label(
                    label=_translate(self.language, "cars.section.obd_data"),
                    xalign=0.0,
                )
                sep_lbl.add_css_class("dim-label")
                sep_lbl.add_css_class("caption-heading")
                sep_lbl.set_margin_top(14)
                sep_lbl.set_margin_bottom(4)
                sep_lbl.set_margin_start(8)
                sep_row.section_label_key = "cars.section.obd_data"  # type: ignore[attr-defined]
                sep_row.section_label_widget = sep_lbl  # type: ignore[attr-defined]
                sep_row.set_child(sep_lbl)
                self.category_list.append(sep_row)
                self._cat_section_rows.append(sep_row)
            row = Gtk.ListBoxRow()
            row.cat_key = cat_key  # type: ignore[attr-defined]
            row.cat_label_key = cat_name_key  # type: ignore[attr-defined]

            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            hbox.set_margin_top(8)
            hbox.set_margin_bottom(8)
            hbox.set_margin_start(8)
            hbox.set_margin_end(8)

            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.set_pixel_size(18)
            hbox.append(icon)

            lbl = Gtk.Label(label=_translate(self.language, cat_name_key), xalign=0.0)
            lbl.set_hexpand(True)
            hbox.append(lbl)

            row.cat_label_widget = lbl  # type: ignore[attr-defined]
            row.cat_icon_widget = icon  # type: ignore[attr-defined]
            row.cat_hbox = hbox  # type: ignore[attr-defined]
            row.set_tooltip_text(_translate(self.language, cat_name_key))

            row.set_child(hbox)
            self.category_list.append(row)
            self._cat_rows.append(row)

        cat_scroll = Gtk.ScrolledWindow()
        cat_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        cat_scroll.set_vexpand(True)
        cat_scroll.set_child(self.category_list)
        self._sidebar.append(cat_scroll)

        self._sidebar_separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)

        self._apply_narrow_to_sidebar()

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        content.set_hexpand(True)
        content.set_vexpand(True)
        self._detail_content = content

        self.content_title = Gtk.Label(xalign=0.0)
        self.content_title.add_css_class("title-2")
        self.content_title.set_margin_top(12)
        self.content_title.set_margin_start(16)
        content.append(self.content_title)

        self.content_subtitle = Gtk.Label(xalign=0.0)
        self.content_subtitle.add_css_class("dim-label")
        self.content_subtitle.set_margin_start(16)
        self.content_subtitle.set_margin_bottom(8)
        content.append(self.content_subtitle)

        self.value_list = Gtk.ListBox()
        self.value_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.value_list.add_css_class("boxed-list")
        self.value_list.set_margin_start(16)
        self.value_list.set_margin_end(16)
        self.value_list.set_margin_bottom(16)
        self.value_list.set_valign(Gtk.Align.START)

        value_scroll = Gtk.ScrolledWindow()
        value_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        value_scroll.set_vexpand(True)
        value_scroll.set_hexpand(True)
        value_scroll.set_child(self.value_list)
        self._value_scroll = value_scroll
        content.append(value_scroll)


        # Initiale Anordnung gemäß Einstellung
        self._apply_sidebar_side_to_body()
        outer.append(body)

        self._detail_page = Adw.NavigationPage(
            child=outer,
            title=_translate(self.language, "nav.cars"),
        )
        self._detail_page.set_tag("detail")

        first_row = self.category_list.get_row_at_index(0)
        if first_row is not None:
            self.category_list.select_row(first_row)

    # ---------------------------------------------------- Sub-page back button

    def _wrap_sub_page(
        self,
        content: Gtk.Widget,
        title: str,
        on_rename: "Callable[[Gtk.Label], None] | None" = None,
        on_share: "Callable[[], None] | None" = None,
        on_delete: "Callable[[], None] | None" = None,
        on_back: "Callable[[], None] | None" = None,
    ) -> Gtk.Widget:
        """Wrap content with a title + back-button header for sub-pages (trip, scan, accel run).

        ``on_back`` overrides the default behaviour of popping ``self.nav_view``
        — useful when the wrapped content is shown inline (e.g. inside the
        cars detail value area on desktop) and there's no NavigationView page
        to pop.
        """
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.set_margin_start(8)
        head.set_margin_top(8)
        head.set_margin_end(12)
        head.set_margin_bottom(4)
        back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        back_btn.add_css_class("flat")
        if on_back is not None:
            back_btn.connect("clicked", lambda _b: on_back())
        else:
            back_btn.connect("clicked", lambda _b: self.nav_view.pop())
        title_lbl = Gtk.Label(label=title, xalign=0.0)
        title_lbl.add_css_class("title-3")
        title_lbl.set_hexpand(True)
        head.append(back_btn)
        head.append(title_lbl)
        if on_rename is not None:
            rename_btn = Gtk.Button(icon_name="document-edit-symbolic")
            rename_btn.add_css_class("flat")
            rename_btn.connect("clicked", lambda _b: on_rename(title_lbl))
            head.append(rename_btn)
        if on_share is not None:
            share_btn = Gtk.Button(icon_name="share-alt-symbolic")
            share_btn.add_css_class("flat")
            share_btn.connect("clicked", lambda _b: on_share())
            head.append(share_btn)
        if on_delete is not None:
            trash_btn = Gtk.Button(icon_name="user-trash-symbolic")
            trash_btn.add_css_class("flat")
            trash_btn.connect("clicked", lambda _b: on_delete())
            head.append(trash_btn)
        outer.append(head)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        outer.append(content)
        return outer

    # ---------------------------------------------------- öffentliche API

    def is_detail_open(self) -> bool:
        """True wenn der Detail-Bereich die Liste verdeckt.

        Im Split-View (Desktop) sind Liste und Detail immer beide sichtbar;
        die Liste wird nicht „verdeckt", daher False. Im collapsed-Modus
        (Mobile) ist Detail offen, sobald show-content gesetzt ist.
        """
        if not self._split_view.get_collapsed():
            return False
        return self._split_view.get_show_content()

    def set_narrow(self, narrow: bool) -> None:
        """Auf Smartphone-Breiten: Labels ausblenden, nur Icons zeigen."""
        if narrow == self._narrow:
            return
        self._narrow = narrow
        self._apply_narrow_to_sidebar()

    def _apply_sidebar_side_to_body(self) -> None:
        """Sidebar links oder rechts vom Content anordnen."""
        body = self._detail_body
        sidebar = self._sidebar
        sep = self._sidebar_separator
        content = self._detail_content
        for w in (sidebar, sep, content):
            if w.get_parent() == body:
                body.remove(w)
        if self._sidebar_side == "right":
            body.append(content)
            body.append(sep)
            body.append(sidebar)
        else:
            body.append(sidebar)
            body.append(sep)
            body.append(content)

    def set_sidebar_side(self, side: str) -> None:
        if side == self._sidebar_side:
            return
        self._sidebar_side = side
        self._apply_sidebar_side_to_body()

    def _apply_narrow_to_sidebar(self) -> None:
        narrow = self._narrow
        # Sidebar-Breite umstellen
        self._sidebar.set_size_request(56 if narrow else 220, -1)
        # Section dividers (e.g. "OBD Daten") are pure text — hide when narrow.
        for sep in getattr(self, "_cat_section_rows", []):
            sep.set_visible(not narrow)
        scan_row = getattr(self, "_scan_date_row", None)
        if scan_row is not None and narrow:
            scan_row.set_visible(False)
        for row in self._cat_rows:
            lbl = getattr(row, "cat_label_widget", None)
            hbox = getattr(row, "cat_hbox", None)
            if lbl is not None:
                lbl.set_visible(not narrow)
            if hbox is not None:
                hbox.set_halign(Gtk.Align.CENTER if narrow else Gtk.Align.FILL)
