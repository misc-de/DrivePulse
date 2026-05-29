"""Car Lab — read-only UDS exploration UI under the car detail view.

Three workflows, all read-only:
  1. Discover a control module (identification DIDs + VAG coding DID) and save it.
  1.1 Browse saved discoveries.
  2. Find functions: capture a stable baseline of a module (ignoring self-changing
     "noise" bytes), let the user toggle something in the car, then diff and record
     the changed byte/bit with the user's description — growing the coding table.

The OBD round-trips run in the window's worker threads via the ``on_carlab_*``
callbacks; results come back on the GTK thread. Nothing here ever writes to the
vehicle.
"""
from __future__ import annotations

import functools
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from drivepulse_app.common import LOG_DIR, _translate
from drivepulse_app.diagnostics import get_logger
from drivepulse_app.obd.coding_diff import ByteChange, diff_snapshots, volatile_bytes
from drivepulse_app.obd.uds import VAG_CODING_DID, candidate_modules

log = get_logger(__name__)

_BASELINE_SAMPLES = 5
_BASELINE_INTERVAL_MS = 400

# Module name (from candidate_modules) → bundled symbolic icon. The legislated
# generic ECUs (ecu_7E2 …) and anything unrecognised fall back to a chip icon.
_ECU_ICONS = {
    "engine": "dp-ecu-engine-symbolic",
    "transmission": "dp-ecu-transmission-symbolic",
    "abs": "dp-ecu-abs-symbolic",
    "airbag": "dp-ecu-airbag-symbolic",
    "instruments": "dp-ecu-cluster-symbolic",
    "steering": "dp-ecu-steering-symbolic",
    "central_electrics": "dp-ecu-electrics-symbolic",
    "comfort": "dp-ecu-comfort-symbolic",
    "gateway": "dp-ecu-gateway-symbolic",
    "mmi": "dp-ecu-mmi-symbolic",
}
_ECU_ICON_FALLBACK = "dp-ecu-generic-symbolic"


def module_icon_name(module: str) -> str:
    """Symbolic icon name for a control-module name (case-insensitive)."""
    return _ECU_ICONS.get((module or "").strip().lower(), _ECU_ICON_FALLBACK)


# Mirrors the map's ``.dp-tour-topnav`` styling so the Car Lab switcher matches
# the tour top-nav (compact padding + small caption labels).
_TOPNAV_CSS = b".dp-tour-topnav { padding: 2px 4px; } .dp-tour-topnav button label { font-size: 11px; }"
_topnav_css_installed = False


def _install_topnav_css() -> None:
    global _topnav_css_installed
    if _topnav_css_installed:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_TOPNAV_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _topnav_css_installed = True


def _hex_snapshot(snap: dict[int, str]) -> dict[int, bytes]:
    """Convert the reader's ``{did: hex}`` snapshot into ``{did: bytes}``."""
    out: dict[int, bytes] = {}
    for did, hex_str in snap.items():
        try:
            out[did] = bytes.fromhex(hex_str)
        except ValueError:
            log.debug("Bad hex for DID 0x%04X: %r", did, hex_str)
    return out


class CarsCarLabMixin:
    # Provided by CarsPage / other mixins.
    language: str
    db: Any
    nav_view: Any
    _selected_car_id: int | None
    # Set by the window: (tx, rx, on_done) / (tx, rx, dids, on_done).
    on_carlab_discover: Callable[[str, str, Callable[[dict], None]], None] | None = None
    on_carlab_snapshot: Callable[[str, str, list[int], Callable[[dict], None]], None] | None = None
    # Mock only: flip a simulated coding bit so a capture produces a diff.
    on_carlab_mock_toggle: Callable[[], None] | None = None
    # Run a module scan (probe known addresses); on_done gets a list of
    # {"name","tx","rx"} dicts for the modules that answered.
    on_carlab_scan: Callable[[Callable[[list], None]], None] | None = None
    mock_mode: bool = False

    if TYPE_CHECKING:
        # Provided at runtime by sibling mixins / Gtk.Box — declared here only so
        # the type checker sees them. Must NOT define bodies, or they would
        # shadow the real implementations (e.g. Gtk.Box.get_root) at runtime.
        def _is_selected_car_mock(self) -> bool: ...  # page.py
        def _wrap_sub_page(self, *a: Any, **k: Any) -> Gtk.Widget: ...  # layout.py
        def get_root(self) -> Any: ...  # Gtk.Widget

    # --- launcher visibility ------------------------------------------------

    def _update_carlab_btn_visibility(self) -> None:
        # Visible for a real car detail. Demo/mock cars normally hide it, but in
        # app mock mode we show it so the workflow can be tried without hardware.
        visible = (
            getattr(self, "_is_real_car", False)
            and getattr(self, "_detail_pushed", False)
            and (not self._is_selected_car_mock() or getattr(self, "mock_mode", False))
        )
        # The Car Lab dongle launcher lives in the Car-Navi sidebar.
        for attr in ("_carlab_sidebar_row", "_carlab_sidebar_gap_row"):
            row = getattr(self, attr, None)
            if row is not None:
                row.set_visible(visible)

    def _carlab_push(self, content: Gtk.Widget, title: str) -> None:
        page = Adw.NavigationPage(child=self._wrap_sub_page(content, title), title=title)
        self.nav_view.push(page)

    def _carlab_candidates(self) -> dict[str, tuple[str, str]]:
        """All probe-able module addresses keyed by name (not just VAG)."""
        return {m.name: (m.tx, m.rx) for m in candidate_modules()}

    def _carlab_module_dropdown(self) -> tuple[Gtk.DropDown, list[str]]:
        names = sorted(self._carlab_candidates())
        return Gtk.DropDown.new_from_strings(names), names

    def _carlab_t(self, key: str, **values: Any) -> str:
        return _translate(self.language, key, **values)

    def _carlab_status_label(self) -> Gtk.Label:
        lbl = Gtk.Label(xalign=0.0)
        lbl.set_wrap(True)
        lbl.set_margin_top(8)
        lbl.add_css_class("dim-label")
        return lbl

    # --- landing (top-nav + stacked views) ----------------------------------

    # name → (icon, short nav label key, builder)
    def _carlab_nav_items(self) -> list[tuple[str, str, str, Callable[[], Gtk.Widget]]]:
        return [
            ("scan", "edit-find-symbolic", "cars.carlab.nav.scan", self._build_module_scan),
            ("discover", "dp-ecu-generic-symbolic", "cars.carlab.nav.discover", self._build_discover),
            ("discoveries", "view-list-symbolic", "cars.carlab.nav.discoveries", self._build_discoveries_list),
            ("find", "edit-find-replace-symbolic", "cars.carlab.nav.find", self._build_find_functions),
            ("findings", "notepad-symbolic", "cars.carlab.nav.findings", self._build_findings),
        ]

    def _cl_scroller(self, child: Gtk.Widget) -> Gtk.ScrolledWindow:
        """Wrap a page body so it scrolls vertically without forcing width."""
        sc = Gtk.ScrolledWindow()
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc.set_vexpand(True)
        sc.set_hexpand(True)
        sc.set_child(child)
        return sc

    def _carlab_top_nav(self, stack: Gtk.Stack) -> Gtk.Widget:
        """Page switcher built like the map's tour top-nav: flat buttons with a
        symbol over a small caption, spread evenly across the bar."""
        _install_topnav_css()
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        bar.add_css_class("dp-tour-topnav")
        bar.set_margin_start(4)
        bar.set_margin_end(4)
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)

        def _child(icon_name: str, label_key: str) -> Gtk.Box:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_halign(Gtk.Align.CENTER)
            img = Gtk.Image.new_from_icon_name(icon_name)
            img.set_pixel_size(22)
            lbl = Gtk.Label(label=self._carlab_t(label_key))
            lbl.add_css_class("caption")
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            box.append(img)
            box.append(lbl)
            return box

        group: Gtk.ToggleButton | None = None
        for name, icon_name, label_key, _builder in self._carlab_nav_items():
            btn = Gtk.ToggleButton()
            btn.set_child(_child(icon_name, label_key))
            btn.add_css_class("flat")
            btn.set_hexpand(True)
            if group is None:
                group = btn
                btn.set_active(True)
            else:
                btn.set_group(group)
            btn.connect("toggled", self._on_carlab_nav_toggled, stack, name)
            bar.append(btn)
        return bar

    def _on_carlab_nav_toggled(self, btn: Gtk.ToggleButton, stack: Gtk.Stack, name: str) -> None:
        if not btn.get_active():
            return
        stack.set_visible_child_name(name)
        # Data-driven views are rebuilt on entry so they stay current.
        if name == "discoveries":
            self._populate_discoveries()
        elif name == "findings":
            self._populate_findings()

    def _open_car_lab(self) -> None:
        if self._selected_car_id is None:
            return
        stack = Gtk.Stack()
        stack.set_vexpand(True)
        stack.set_hexpand(True)
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._cl_stack = stack
        for name, _icon, _label_key, builder in self._carlab_nav_items():
            stack.add_named(self._cl_scroller(builder()), name)
        stack.set_visible_child_name("scan")

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.append(self._carlab_top_nav(stack))
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        outer.append(stack)
        self._carlab_push(outer, self._carlab_t("cars.carlab.title"))

    def _carlab_page_box(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        return box

    # --- module scan (which control units are present) ----------------------

    def _cl_module_row(self, name: str, tx: str, rx: str, on_click: Callable[[], None]) -> Gtk.Button:
        """A left-aligned module button: a per-module symbol, the name on top
        and a tx/rx caption below. Wraps cleanly on mobile instead of
        overflowing the row."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_halign(Gtk.Align.START)
        icon = Gtk.Image.new_from_icon_name(module_icon_name(name))
        icon.set_pixel_size(24)
        icon.set_valign(Gtk.Align.CENTER)
        row.append(icon)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        inner.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(label=name, xalign=0.0)
        title.set_wrap(True)
        sub = Gtk.Label(label=f"tx={tx} · rx={rx}", xalign=0.0)
        sub.add_css_class("caption")
        sub.add_css_class("dim-label")
        inner.append(title)
        inner.append(sub)
        row.append(inner)
        b = Gtk.Button()
        b.set_child(row)
        b.add_css_class("flat")
        b.set_halign(Gtk.Align.FILL)
        b.connect("clicked", lambda _b: on_click())
        return b

    def _build_module_scan(self) -> Gtk.Widget:
        box = self._carlab_page_box()
        intro = Gtk.Label(label=self._carlab_t("cars.carlab.scan.intro"), xalign=0.0)
        intro.set_wrap(True)
        intro.add_css_class("dim-label")
        box.append(intro)

        status = self._carlab_status_label()
        results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        run = Gtk.Button(label=self._carlab_t("cars.carlab.scan.run"))
        run.add_css_class("suggested-action")
        run.set_halign(Gtk.Align.START)

        def on_run(_b: Gtk.Button) -> None:
            if self.on_carlab_scan is None:
                status.set_text(self._carlab_t("cars.carlab.no_connection"))
                return
            status.set_text(self._carlab_t("cars.carlab.scan.running"))
            run.set_sensitive(False)

            def done(modules: list) -> None:
                run.set_sensitive(True)
                child = results.get_first_child()
                while child is not None:
                    results.remove(child)
                    child = results.get_first_child()
                if not modules:
                    status.set_text(self._carlab_t("cars.carlab.scan.none"))
                    return
                status.set_text(self._carlab_t("cars.carlab.scan.found", n=len(modules)))
                for m in modules:
                    name, tx, rx = m["name"], m["tx"], m["rx"]
                    results.append(self._cl_module_row(
                        name, tx, rx,
                        functools.partial(self._run_discover_for, name, tx, rx),
                    ))

            self.on_carlab_scan(done)

        run.connect("clicked", on_run)
        box.append(run)
        box.append(status)
        box.append(results)
        return box

    def _run_discover_for(
        self, name: str, tx: str, rx: str,
        on_result: Callable[[dict], None] | None = None,
    ) -> None:
        """Discover one module by address and save it. By default the result is
        pushed as a detail page; pass ``on_result`` to handle it inline instead
        (it is always called, even on an empty result, so callers can reset UI)."""
        if self.on_carlab_discover is None or self._selected_car_id is None:
            return
        car_id = self._selected_car_id

        def done(result: dict) -> None:
            if result:
                result["module"] = name
                try:
                    self.db.add_discovery(car_id, result, label=name)
                except Exception:
                    log.exception("Could not save discovery")
            if on_result is not None:
                on_result(result)
            elif result:
                self._open_discovery_detail(result)

        self.on_carlab_discover(tx, rx, done)

    # --- 1. discover --------------------------------------------------------

    def _build_discover(self) -> Gtk.Widget:
        box = self._carlab_page_box()
        dropdown, names = self._carlab_module_dropdown()
        box.append(Gtk.Label(label=self._carlab_t("cars.carlab.module"), xalign=0.0))
        box.append(dropdown)
        status = self._carlab_status_label()
        run = Gtk.Button(label=self._carlab_t("cars.carlab.discover.run"))
        run.add_css_class("suggested-action")
        run.set_halign(Gtk.Align.START)
        # The discovery result is rendered inline here, right below the button.
        result_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        result_box.set_margin_top(6)

        def on_run(_b: Gtk.Button) -> None:
            if self.on_carlab_discover is None or self._selected_car_id is None:
                status.set_text(self._carlab_t("cars.carlab.no_connection"))
                return
            module = names[dropdown.get_selected()]
            tx, rx = self._carlab_candidates()[module]
            status.set_text(self._carlab_t("cars.carlab.discover.running", module=module))
            run.set_sensitive(False)
            self._cl_clear(result_box)

            def on_result(data: dict) -> None:
                run.set_sensitive(True)
                self._cl_clear(result_box)
                if not data:
                    status.set_text(self._carlab_t("cars.carlab.no_data"))
                    return
                status.set_text("")
                result_box.append(self._build_discovery_content(data))

            self._run_discover_for(module, tx, rx, on_result=on_result)

        run.connect("clicked", on_run)
        box.append(run)
        box.append(status)
        box.append(result_box)
        return box

    def _build_discoveries_list(self) -> Gtk.Widget:
        box = self._carlab_page_box()
        box.set_spacing(4)
        self._cl_discoveries_list = box
        self._populate_discoveries()
        return box

    def _cl_clear(self, box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            box.remove(child)
            child = box.get_first_child()

    def _cl_list_button(
        self, label: str, on_click: Callable[[], None], icon: str | None = None
    ) -> Gtk.Button:
        """A left-aligned, full-width flat list button, optionally icon-led."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        if icon is not None:
            img = Gtk.Image.new_from_icon_name(icon)
            img.set_pixel_size(20)
            row.append(img)
        lbl = Gtk.Label(label=label, xalign=0.0)
        lbl.set_wrap(True)
        lbl.set_hexpand(True)
        row.append(lbl)
        b = Gtk.Button()
        b.set_child(row)
        b.add_css_class("flat")
        b.set_halign(Gtk.Align.FILL)
        b.connect("clicked", lambda _b: on_click())
        return b

    def _populate_discoveries(self) -> None:
        box = getattr(self, "_cl_discoveries_list", None)
        if box is None:
            return
        self._cl_clear(box)
        rows = []
        if self.db is not None and self._selected_car_id is not None:
            rows = self.db.list_discoveries_for_car(self._selected_car_id)
        if not rows:
            box.append(Gtk.Label(label=self._carlab_t("cars.carlab.discoveries.empty"), xalign=0.0))
            return
        for row in rows:
            label = f"{row['label'] or '?'}  ·  {row['created_at']}"
            did = int(row["id"])
            box.append(self._cl_list_button(
                label, functools.partial(self._open_discovery_by_id, did),
                icon=module_icon_name(row["label"] or ""),
            ))

    def _open_discovery_by_id(self, did: int) -> None:
        self._open_discovery_detail(self.db.get_discovery_data(did))

    def _build_discovery_content(self, data: dict) -> Gtk.Box:
        """The discovery result body (header + identification + DIDs), reused
        both as an inline panel and as a pushed detail page."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        module = str(data.get("module", "?"))
        head_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        head_row.set_halign(Gtk.Align.START)
        head_icon = Gtk.Image.new_from_icon_name(module_icon_name(module))
        head_icon.set_pixel_size(24)
        head_row.append(head_icon)
        head = f"{module}  (tx={data.get('tx')} rx={data.get('rx')})"
        head_row.append(Gtk.Label(label=head, xalign=0.0))
        box.append(head_row)

        ident = data.get("identification") or {}
        if ident:
            box.append(self._carlab_section(self._carlab_t("cars.carlab.identification")))
            for name, entry in ident.items():
                val = entry.get("ascii") or entry.get("hex", "")
                box.append(Gtk.Label(label=f"{name}: {val}", xalign=0.0, selectable=True))

        responses = data.get("did_responses") or {}
        box.append(self._carlab_section(self._carlab_t("cars.carlab.dids")))
        for key, entry in responses.items():
            if "hex" in entry:
                extra = f"  \"{entry['ascii']}\"" if entry.get("ascii") else ""
                box.append(Gtk.Label(label=f"DID {key}: {entry['hex']}{extra}", xalign=0.0, selectable=True))
            else:
                box.append(Gtk.Label(label=f"DID {key}: -- {entry.get('nrc_name', '')}", xalign=0.0))
        return box

    def _open_discovery_detail(self, data: dict) -> None:
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        box = self._build_discovery_content(data)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        scroll.set_child(box)
        self._carlab_push(scroll, self._carlab_t("cars.carlab.discovery_detail"))

    def _carlab_section(self, text: str) -> Gtk.Label:
        lbl = Gtk.Label(label=text, xalign=0.0)
        lbl.add_css_class("heading")
        lbl.set_margin_top(10)
        return lbl

    # --- 2. find functions --------------------------------------------------

    def _carlab_watch_dids(self, module: str) -> list[int]:
        """DIDs to watch while diffing: the VAG coding DID plus whatever the
        latest discovery of this module found responding."""
        dids: set[int] = {VAG_CODING_DID}
        if self.db is not None and self._selected_car_id is not None:
            for row in self.db.list_discoveries_for_car(self._selected_car_id):
                if row["label"] != module:
                    continue
                data = self.db.get_discovery_data(int(row["id"]))
                for key, entry in (data.get("did_responses") or {}).items():
                    if "hex" in entry:
                        try:
                            dids.add(int(key, 16))
                        except ValueError:
                            pass
                break
        return sorted(dids)

    def _build_find_functions(self) -> Gtk.Widget:
        box = self._carlab_page_box()
        dropdown, names = self._carlab_module_dropdown()
        box.append(Gtk.Label(label=self._carlab_t("cars.carlab.module"), xalign=0.0))
        box.append(dropdown)

        status = self._carlab_status_label()
        baseline_btn = Gtk.Button(label=self._carlab_t("cars.carlab.baseline"))
        baseline_btn.add_css_class("suggested-action")
        baseline_btn.set_halign(Gtk.Align.START)
        capture_btn = Gtk.Button(label=self._carlab_t("cars.carlab.capture"))
        capture_btn.set_halign(Gtk.Align.START)
        capture_btn.set_sensitive(False)

        # Per-session state.
        self._cl_baseline: dict[int, bytes] = {}
        self._cl_volatile: set[tuple[int, int]] = set()
        self._cl_samples: list[dict[int, bytes]] = []

        def start_baseline(_b: Gtk.Button) -> None:
            if self.on_carlab_snapshot is None or self._selected_car_id is None:
                status.set_text(self._carlab_t("cars.carlab.no_connection"))
                return
            module = names[dropdown.get_selected()]
            tx, rx = self._carlab_candidates()[module]
            dids = self._carlab_watch_dids(module)
            self._cl_module, self._cl_tx, self._cl_rx, self._cl_dids = module, tx, rx, dids
            self._cl_samples = []
            capture_btn.set_sensitive(False)
            baseline_btn.set_sensitive(False)
            self._carlab_take_baseline(1, status, baseline_btn, capture_btn)

        def capture(_b: Gtk.Button) -> None:
            if self.on_carlab_snapshot is None:
                return
            status.set_text(self._carlab_t("cars.carlab.capturing"))

            def done(snap: dict) -> None:
                after = _hex_snapshot(snap)
                changes = diff_snapshots(self._cl_baseline, after, self._cl_volatile)
                if not changes:
                    status.set_text(self._carlab_t("cars.carlab.no_change"))
                    return
                self._cl_baseline = after  # subsequent captures diff against this
                status.set_text(self._carlab_t("cars.carlab.changes", n=len(changes)))
                self._carlab_describe_changes(changes)

            self.on_carlab_snapshot(self._cl_tx, self._cl_rx, self._cl_dids, done)

        baseline_btn.connect("clicked", start_baseline)
        capture_btn.connect("clicked", capture)
        box.append(baseline_btn)
        box.append(capture_btn)
        # Mock helper: simulate the user changing a function in the car.
        if getattr(self, "mock_mode", False):
            toggle = Gtk.Button(label=self._carlab_t("cars.carlab.mock_toggle"))
            toggle.set_halign(Gtk.Align.START)
            toggle.add_css_class("dim-label")

            def on_toggle(_b: Gtk.Button) -> None:
                if self.on_carlab_mock_toggle is not None:
                    self.on_carlab_mock_toggle()
                status.set_text(self._carlab_t("cars.carlab.mock_toggled"))

            toggle.connect("clicked", on_toggle)
            box.append(toggle)
        box.append(status)
        return box

    def _carlab_take_baseline(
        self, i: int, status: Gtk.Label, baseline_btn: Gtk.Button, capture_btn: Gtk.Button
    ) -> None:
        status.set_text(self._carlab_t("cars.carlab.baseline.progress", i=i, n=_BASELINE_SAMPLES))
        assert self.on_carlab_snapshot is not None

        def done(snap: dict) -> None:
            self._cl_samples.append(_hex_snapshot(snap))
            if i < _BASELINE_SAMPLES:
                GLib.timeout_add(
                    _BASELINE_INTERVAL_MS,
                    self._carlab_baseline_tick, i + 1, status, baseline_btn, capture_btn,
                )
            else:
                self._cl_volatile = volatile_bytes(self._cl_samples)
                self._cl_baseline = self._cl_samples[-1] if self._cl_samples else {}
                baseline_btn.set_sensitive(True)
                if self._cl_baseline:
                    capture_btn.set_sensitive(True)
                    status.set_text(self._carlab_t("cars.carlab.baseline.ready"))
                else:
                    status.set_text(self._carlab_t("cars.carlab.no_data"))

        self.on_carlab_snapshot(self._cl_tx, self._cl_rx, self._cl_dids, done)

    def _carlab_baseline_tick(
        self, i: int, status: Gtk.Label, baseline_btn: Gtk.Button, capture_btn: Gtk.Button
    ) -> bool:
        self._carlab_take_baseline(i, status, baseline_btn, capture_btn)
        return False  # one-shot timeout

    def _carlab_describe_changes(self, changes: list[ByteChange]) -> None:
        dialog = Adw.AlertDialog()
        dialog.set_heading(self._carlab_t("cars.carlab.describe.heading"))
        dialog.set_body("\n".join(c.describe() for c in changes))
        entry = Adw.EntryRow(title=self._carlab_t("cars.carlab.describe.label"))
        lb = Gtk.ListBox()
        lb.set_selection_mode(Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        lb.set_margin_top(8)
        lb.append(entry)
        dialog.set_extra_child(lb)
        dialog.add_response("cancel", self._carlab_t("cars.carlab.cancel"))
        dialog.add_response("save", self._carlab_t("cars.carlab.save"))
        dialog.set_default_response("save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d: Adw.AlertDialog, response: str) -> None:
            if response != "save":
                return
            desc = entry.get_text().strip()
            car_id = self._selected_car_id
            if car_id is None:
                return
            for c in changes:
                try:
                    self.db.add_finding(car_id, {
                        "module": getattr(self, "_cl_module", ""),
                        "tx": getattr(self, "_cl_tx", ""), "rx": getattr(self, "_cl_rx", ""),
                        "did": c.did, "byte_index": c.byte_index, "bit_mask": c.bit_mask,
                        "before_hex": f"{c.before:02X}", "after_hex": f"{c.after:02X}",
                        "description": desc,
                    })
                except Exception:
                    log.exception("Could not save finding")

        dialog.connect("response", on_response)
        root = self.get_root()
        if root is not None:
            dialog.present(root)

    # --- findings table + export -------------------------------------------

    def _build_findings(self) -> Gtk.Widget:
        box = self._carlab_page_box()
        box.set_spacing(4)

        status = self._carlab_status_label()
        export = Gtk.Button(label=self._carlab_t("cars.carlab.export"))
        export.set_halign(Gtk.Align.START)
        export.connect("clicked", lambda _b: self._carlab_export_findings(status))
        box.append(export)
        box.append(status)

        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._cl_findings_list = list_box
        box.append(list_box)
        self._populate_findings()
        return box

    def _populate_findings(self) -> None:
        list_box = getattr(self, "_cl_findings_list", None)
        if list_box is None:
            return
        self._cl_clear(list_box)
        rows = []
        if self.db is not None and self._selected_car_id is not None:
            rows = self.db.list_findings_for_car(self._selected_car_id)
        if not rows:
            list_box.append(Gtk.Label(label=self._carlab_t("cars.carlab.findings.empty"), xalign=0.0))
            return
        for row in rows:
            line = (
                f"{row['module'] or '?'}  DID {int(row['did']):04X} "
                f"byte {row['byte_index']} bit {row['bit_mask']:02X}: "
                f"{row['before_hex']}→{row['after_hex']}  —  {row['description'] or ''}"
            )
            rb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lbl = Gtk.Label(label=line, xalign=0.0, selectable=True)
            lbl.set_wrap(True)
            lbl.set_hexpand(True)
            rb.append(lbl)
            trash = Gtk.Button(icon_name="user-trash-symbolic")
            trash.add_css_class("flat")
            trash.set_valign(Gtk.Align.START)
            fid = int(row["id"])
            trash.connect("clicked", lambda _b, f=fid: self._carlab_delete_finding(f))
            rb.append(trash)
            list_box.append(rb)

    def _carlab_delete_finding(self, finding_id: int) -> None:
        if self.db is not None:
            self.db.delete_finding(finding_id)
        self._populate_findings()

    def _carlab_export_findings(self, status: Gtk.Label) -> None:
        if self.db is None or self._selected_car_id is None:
            return
        rows = self.db.list_findings_for_car(self._selected_car_id)
        data = [dict(r) for r in rows]
        export_dir = LOG_DIR / "exports"
        try:
            export_dir.mkdir(parents=True, exist_ok=True)
            out = export_dir / f"findings_car_{self._selected_car_id}.json"
            out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            status.set_text(self._carlab_t("cars.carlab.exported", path=str(out)))
        except Exception:
            log.exception("Could not export findings")
            status.set_text(self._carlab_t("cars.carlab.export_failed"))
