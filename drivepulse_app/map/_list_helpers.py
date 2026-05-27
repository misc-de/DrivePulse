"""Shared widget builders for the tour-history / saved-tour list pages.

Both pages render a paginated list with the same multi-select chrome
(header trash + share buttons that toggle visibility with select mode) and
the same dim "list is empty" placeholder row. These helpers exist so the
two mixin modules don't drift apart whenever the chrome style changes."""
from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gtk


def make_empty_dim_row(text: str, *, margin: int = 14) -> Gtk.ListBoxRow:
    """Non-selectable placeholder row shown when a list has no entries."""
    row = Gtk.ListBoxRow()
    row.set_activatable(False)
    row.set_selectable(False)
    lbl = Gtk.Label(label=text)
    lbl.add_css_class("dim-label")
    lbl.set_margin_top(margin)
    lbl.set_margin_bottom(margin)
    lbl.set_wrap(True)
    row.set_child(lbl)
    return row


def make_bulk_select_header(
    *,
    trash_tooltip: str,
    share_tooltip: str,
    on_trash: Callable[..., None],
    on_share: Callable[..., None],
) -> tuple[Adw.HeaderBar, Gtk.Button, Gtk.Button]:
    """Header bar with trash + share suffix buttons (hidden by default).

    Both buttons start invisible; the list mixin flips them on once the user
    enters bulk-select mode. Returned tuple is ``(header, trash_btn, share_btn)``
    so the caller can stash the buttons on ``self`` for later show/hide.
    """
    header = Adw.HeaderBar()

    trash_btn = Gtk.Button(icon_name="user-trash-symbolic")
    trash_btn.add_css_class("destructive-action")
    trash_btn.set_tooltip_text(trash_tooltip)
    trash_btn.set_visible(False)
    trash_btn.connect("clicked", on_trash)
    header.pack_end(trash_btn)

    share_btn = Gtk.Button(icon_name="share-alt-symbolic")
    share_btn.add_css_class("flat")
    share_btn.set_tooltip_text(share_tooltip)
    share_btn.set_visible(False)
    share_btn.connect("clicked", on_share)
    header.pack_end(share_btn)

    return header, trash_btn, share_btn
