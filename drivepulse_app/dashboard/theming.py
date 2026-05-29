"""CSS install, palette overrides, color-scheme handling and theme cycling
for DashboardWindow."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gi.repository import Adw, Gtk

from drivepulse_app.ui.gauge import all_theme_options, get_theme_css


class DashboardThemingMixin:
    # Concrete-class state surfaced to this mixin. See project_mixin_typing.md.
    language: str
    theme_mode: str
    gauge_theme: str
    ui_scale: int
    _base_xft_dpi: int
    _theme_css_provider: Gtk.CssProvider
    _nav_rotation_css: Gtk.CssProvider
    _light_palette_css: Gtk.CssProvider

    # GTK widget methods (DashboardWindow inherits Gtk.Widget).
    get_display: Callable[[], Any]
    get_css_classes: Callable[[], list[str]]
    add_css_class: Callable[[str], None]
    remove_css_class: Callable[[str], None]

    # Settings setter from DashboardSettingsMixin.
    _set_gauge_theme: Callable[[str], None]

    # Softens libadwaita's stock light palette toward a warm mid-grey while
    # keeping its layering intact: view/card/popover sit *above* the window in
    # luminance (paper-on-frame), headerbar/sidebar sit *below*. Same set of
    # tokens the established adw-colors themes (Plano2/Nord/Solarized) touch.
    _LIGHT_PALETTE_OVERRIDES = (
        b"@define-color window_bg_color #c8c6c2;"
        b"@define-color view_bg_color #d6d4d0;"
        b"@define-color headerbar_bg_color #b8b6b2;"
        b"@define-color headerbar_backdrop_color @window_bg_color;"
        b"@define-color sidebar_bg_color #b8b6b2;"
        b"@define-color sidebar_backdrop_color @window_bg_color;"
        b"@define-color secondary_sidebar_bg_color #c0beba;"
        b"@define-color secondary_sidebar_backdrop_color @window_bg_color;"
        b"@define-color card_bg_color @view_bg_color;"
        b"@define-color popover_bg_color @view_bg_color;"
        b"@define-color dialog_bg_color @view_bg_color;"
        b"@define-color thumbnail_bg_color @window_bg_color;"
    )

    def _on_realize_install_css(self, *_args: Any) -> None:
        # Capture the unscaled font DPI before applying any UI scale so 100 %
        # restores the exact system baseline (incl. HiDPI text scaling).
        self._apply_ui_scale(getattr(self, "ui_scale", 100))
        display = self.get_display()
        Gtk.StyleContext.add_provider_for_display(
            display, self._theme_css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        Gtk.StyleContext.add_provider_for_display(
            display, self._nav_rotation_css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        Gtk.StyleContext.add_provider_for_display(
            display, self._light_palette_css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        _global_css = Gtk.CssProvider()
        _global_css.load_from_data(
            b".dp-table-row { border-radius: 0; }"
            b".dp-sync-online { color: #33d17a; }"
        )
        Gtk.StyleContext.add_provider_for_display(
            display, _global_css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self._apply_theme_mode(self.theme_mode)
        self._apply_window_theme(self.gauge_theme)
        Adw.StyleManager.get_default().connect("notify::dark", self._on_system_dark_changed)

    def _apply_light_palette(self) -> None:
        is_dark = Adw.StyleManager.get_default().get_dark()
        self._light_palette_css.load_from_data(
            b"" if is_dark else self._LIGHT_PALETTE_OVERRIDES
        )

    def _apply_ui_scale(self, scale: int) -> None:
        """Shrink/restore the whole UI by scaling the global font DPI.

        100 % keeps the captured system baseline; lower values shrink text and
        the Adwaita widget metrics that derive from it, so more content fits
        (50 % ≈ double the content per axis). Uses ``gtk-xft-dpi`` — the same
        lever GNOME's text-scaling uses — so the change is live and reversible.
        """
        settings = Gtk.Settings.get_default()
        if settings is None:
            return
        # Latch the baseline once, before the first scaling write touches it.
        if not hasattr(self, "_base_xft_dpi"):
            current = int(settings.get_property("gtk-xft-dpi"))
            self._base_xft_dpi = current if current > 0 else 96 * 1024
        factor = max(25, min(100, int(scale))) / 100.0
        settings.set_property("gtk-xft-dpi", int(self._base_xft_dpi * factor))

    def _apply_theme_mode(self, mode: str) -> None:
        manager = Adw.StyleManager.get_default()
        if mode == "dark":
            manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        elif mode == "light":
            manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        else:
            manager.set_color_scheme(Adw.ColorScheme.DEFAULT)
        self._apply_light_palette()
        if hasattr(self, "stopwatch_page"):
            effective = "dark" if manager.get_dark() else "light"
            self.stopwatch_page.set_theme_mode(effective)

    def _on_system_dark_changed(self, _manager: Any, _param: Any) -> None:
        if getattr(self, "theme_mode", "auto") == "auto":
            self._apply_light_palette()
            self._apply_window_theme(self.gauge_theme)
            if hasattr(self, "stopwatch_page"):
                effective = "dark" if Adw.StyleManager.get_default().get_dark() else "light"
                self.stopwatch_page.set_theme_mode(effective)

    def _apply_window_theme(self, theme: str) -> None:
        for cls in list(self.get_css_classes()):
            if cls.startswith("dp-theme-"):
                self.remove_css_class(cls)
        safe = theme.replace(":", "-").replace("_", "-")
        self.add_css_class(f"dp-theme-{safe}")
        mode = getattr(self, "theme_mode", "auto")
        is_dark = mode == "dark" or (mode == "auto" and Adw.StyleManager.get_default().get_dark())
        # Theme CSS contains broad window/toolbarview/scrolledwindow selectors that
        # override the entire app background.  Only load it when the gauge theme
        # variant matches the app colour scheme — a light gauge theme in a dark app
        # (or vice versa) would otherwise repaint the whole UI the wrong colour.
        # The gauge's Cairo drawing controls its own colours regardless of this CSS.
        is_light_theme = "_light" in theme
        # Dark themes always apply their background CSS — the user explicitly chose a dark
        # theme and expects a dark canvas regardless of the system colour scheme.
        # Light themes are suppressed in dark mode to avoid painting a white background
        # over the dark UI.
        load_css = not is_light_theme or not is_dark
        if load_css:
            css = get_theme_css(theme)
            self._theme_css_provider.load_from_data(css.encode() if css else b"")
        else:
            self._theme_css_provider.load_from_data(b"")

    def _cycle_theme(self, up: bool) -> None:
        """Cycle to the next/previous theme via vertical swipe."""
        options = [tid for tid, _ in all_theme_options(self.language)]
        if not options:
            return
        try:
            idx = options.index(self.gauge_theme)
        except ValueError:
            idx = 0
        idx = (idx + (1 if up else -1)) % len(options)
        self._set_gauge_theme(options[idx])
