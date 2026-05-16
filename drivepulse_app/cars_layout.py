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
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_margin_top(16)
        outer.set_margin_bottom(16)
        outer.set_margin_start(16)
        outer.set_margin_end(16)

        self._list_intro = Gtk.Label(xalign=0.0)
        self._list_intro.add_css_class("dim-label")
        self._list_intro.set_wrap(True)
        outer.append(self._list_intro)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_valign(Gtk.Align.START)
        outer.append(self._list_box)

        self._empty_label = Gtk.Label(xalign=0.0)
        self._empty_label.add_css_class("dim-label")
        self._empty_label.set_wrap(True)
        self._empty_label.set_visible(False)
        outer.append(self._empty_label)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_hexpand(True)
        scroll.set_child(outer)

        self._list_page = Adw.NavigationPage(
            child=scroll,
            title=_translate(self.language, "nav.cars"),
        )
        self._list_page.set_tag("list")
        self.nav_view.add(self._list_page)
        self._refresh_list_texts()

    def _refresh_list_texts(self) -> None:
        self._list_intro.set_text(_translate(self.language, "cars.list.intro"))
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
        self._detail_back_btn.connect("clicked", lambda _b: self.nav_view.pop())
        self._detail_title = Gtk.Label(xalign=0.0)
        self._detail_title.add_css_class("title-3")
        self._detail_title.set_hexpand(True)

        self._rename_btn = Gtk.Button(icon_name="document-edit-symbolic")
        self._rename_btn.add_css_class("flat")
        self._rename_btn.set_visible(False)
        self._rename_btn.connect("clicked", lambda _b: self._open_rename_dialog())

        head.append(self._detail_back_btn)
        head.append(self._detail_title)
        head.append(self._rename_btn)
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

        self._categories_label = Gtk.Label(xalign=0.0)
        self._categories_label.add_css_class("heading")
        self._sidebar.append(self._categories_label)

        self.category_list = Gtk.ListBox()
        self.category_list.set_selection_mode(Gtk.SelectionMode.BROWSE)
        self.category_list.add_css_class("navigation-sidebar")
        self.category_list.connect("row-selected", self._on_category_selected)
        for cat_key, cat_name_key, icon_name, _items in CATEGORIES:
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
        content.append(value_scroll)

        # Selection action bar (trips multi-select mode)
        self._select_count_lbl = Gtk.Label(xalign=0.0)
        self._select_count_lbl.set_hexpand(True)

        self._select_delete_btn = Gtk.Button()
        self._select_delete_btn.add_css_class("destructive-action")
        self._select_delete_btn.connect("clicked", lambda _b: self._confirm_delete_selected_trips())

        _sel_cancel_btn = Gtk.Button(label="")
        _sel_cancel_btn.add_css_class("flat")
        _sel_cancel_btn.connect("clicked", lambda _b: self._exit_trip_select_mode())

        self._select_cancel_btn = _sel_cancel_btn

        sel_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sel_bar.set_margin_start(16)
        sel_bar.set_margin_end(16)
        sel_bar.set_margin_top(8)
        sel_bar.set_margin_bottom(8)
        sel_bar.append(self._select_count_lbl)
        sel_bar.append(self._select_delete_btn)
        sel_bar.append(_sel_cancel_btn)

        self._select_revealer = Gtk.Revealer()
        self._select_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self._select_revealer.set_reveal_child(False)
        self._select_revealer.set_child(sel_bar)
        content.append(self._select_revealer)

        # Initiale Anordnung gemäß Einstellung
        self._apply_sidebar_side_to_body()
        outer.append(body)

        self._categories_label.set_text(_translate(self.language, "cars.categories"))

        self._detail_page = Adw.NavigationPage(child=outer, title="")
        self._detail_page.set_tag("detail")

        first_row = self.category_list.get_row_at_index(0)
        if first_row is not None:
            self.category_list.select_row(first_row)

    # ---------------------------------------------------- Sub-page back button

    def _wrap_sub_page(self, content: Gtk.Widget, title: str) -> Gtk.Widget:
        """Wrap content with a title + back-button header for sub-pages (trip, scan, accel run)."""
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.set_margin_start(8)
        head.set_margin_top(8)
        head.set_margin_end(12)
        head.set_margin_bottom(4)
        back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        back_btn.add_css_class("flat")
        back_btn.connect("clicked", lambda _b: self.nav_view.pop())
        title_lbl = Gtk.Label(label=title, xalign=0.0)
        title_lbl.add_css_class("title-3")
        title_lbl.set_hexpand(True)
        head.append(back_btn)
        head.append(title_lbl)
        outer.append(head)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        outer.append(content)
        return outer

    # ---------------------------------------------------- öffentliche API

    def is_detail_open(self) -> bool:
        """True, solange die Detail-Seite im NavigationView gepusht ist."""
        return self._detail_pushed

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
        # Überschrift „Kategorien" ausblenden, wenn schmal
        self._categories_label.set_visible(not narrow)
        for row in self._cat_rows:
            lbl = getattr(row, "cat_label_widget", None)
            hbox = getattr(row, "cat_hbox", None)
            if lbl is not None:
                lbl.set_visible(not narrow)
            if hbox is not None:
                hbox.set_halign(Gtk.Align.CENTER if narrow else Gtk.Align.FILL)

