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

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk

from drivepulse_app.common import LOG_DIR, _translate
from drivepulse_app.diagnostics import get_logger
from drivepulse_app.obd.coding_diff import ByteChange, diff_snapshots, volatile_bytes
from drivepulse_app.obd.uds import VAG_CODING_DID, candidate_modules

log = get_logger(__name__)

_BASELINE_SAMPLES = 5
_BASELINE_INTERVAL_MS = 400


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

    # --- header button ------------------------------------------------------

    def _update_carlab_btn_visibility(self) -> None:
        btn = getattr(self, "_carlab_btn", None)
        if btn is None:
            return
        # Visible for a real car detail. Demo/mock cars normally hide it, but in
        # app mock mode we show it so the workflow can be tried without hardware.
        btn.set_visible(
            getattr(self, "_is_real_car", False)
            and getattr(self, "_detail_pushed", False)
            and (not self._is_selected_car_mock() or getattr(self, "mock_mode", False))
        )

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

    # --- landing ------------------------------------------------------------

    def _open_car_lab(self) -> None:
        if self._selected_car_id is None:
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        intro = Gtk.Label(label=self._carlab_t("cars.carlab.intro"), xalign=0.0)
        intro.set_wrap(True)
        intro.add_css_class("dim-label")
        box.append(intro)

        def add_btn(label_key: str, handler: Callable[[], None]) -> None:
            b = Gtk.Button(label=self._carlab_t(label_key))
            b.add_css_class("pill")
            b.set_halign(Gtk.Align.START)
            b.connect("clicked", lambda _b: handler())
            box.append(b)

        add_btn("cars.carlab.scan_modules", self._open_module_scan)
        add_btn("cars.carlab.discover", self._open_discover)
        add_btn("cars.carlab.discoveries", self._open_discoveries_list)
        add_btn("cars.carlab.find", self._open_find_functions)
        add_btn("cars.carlab.findings", self._open_findings)
        self._carlab_push(box, self._carlab_t("cars.carlab.title"))

    # --- module scan (which control units are present) ----------------------

    def _open_module_scan(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12); box.set_margin_bottom(12)
        box.set_margin_start(12); box.set_margin_end(12)
        box.append(Gtk.Label(label=self._carlab_t("cars.carlab.scan.intro"), xalign=0.0))

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
                    b = Gtk.Button(label=f"{name}  (tx={tx} rx={rx})")
                    b.add_css_class("flat")
                    b.set_halign(Gtk.Align.FILL)
                    b.connect("clicked", lambda _b, n=name, t=tx, r=rx: self._run_discover_for(n, t, r))
                    results.append(b)

            self.on_carlab_scan(done)

        run.connect("clicked", on_run)
        box.append(run)
        box.append(status)
        box.append(results)
        self._carlab_push(box, self._carlab_t("cars.carlab.scan_modules"))

    def _run_discover_for(self, name: str, tx: str, rx: str) -> None:
        """Discover one module by address and save it, then show the result."""
        if self.on_carlab_discover is None or self._selected_car_id is None:
            return
        car_id = self._selected_car_id

        def done(result: dict) -> None:
            if not result:
                return
            result["module"] = name
            try:
                self.db.add_discovery(car_id, result, label=name)
            except Exception:
                log.exception("Could not save discovery")
            self._open_discovery_detail(result)

        self.on_carlab_discover(tx, rx, done)

    # --- 1. discover --------------------------------------------------------

    def _open_discover(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12); box.set_margin_bottom(12)
        box.set_margin_start(12); box.set_margin_end(12)
        dropdown, names = self._carlab_module_dropdown()
        box.append(Gtk.Label(label=self._carlab_t("cars.carlab.module"), xalign=0.0))
        box.append(dropdown)
        status = self._carlab_status_label()
        run = Gtk.Button(label=self._carlab_t("cars.carlab.discover.run"))
        run.add_css_class("suggested-action")
        run.set_halign(Gtk.Align.START)

        def on_run(_b: Gtk.Button) -> None:
            if self.on_carlab_discover is None or self._selected_car_id is None:
                status.set_text(self._carlab_t("cars.carlab.no_connection"))
                return
            module = names[dropdown.get_selected()]
            tx, rx = self._carlab_candidates()[module]
            status.set_text(self._carlab_t("cars.carlab.discover.running", module=module))
            self._run_discover_for(module, tx, rx)

        run.connect("clicked", on_run)
        box.append(run)
        box.append(status)
        self._carlab_push(box, self._carlab_t("cars.carlab.discover"))

    def _open_discoveries_list(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(12); box.set_margin_bottom(12)
        box.set_margin_start(12); box.set_margin_end(12)
        rows = []
        if self.db is not None and self._selected_car_id is not None:
            rows = self.db.list_discoveries_for_car(self._selected_car_id)
        if not rows:
            box.append(Gtk.Label(label=self._carlab_t("cars.carlab.discoveries.empty"), xalign=0.0))
        for row in rows:
            label = f"{row['label'] or '?'}  ·  {row['created_at']}"
            b = Gtk.Button(label=label)
            b.add_css_class("flat")
            b.set_halign(Gtk.Align.FILL)
            did = int(row["id"])
            b.connect("clicked", lambda _b, d=did: self._open_discovery_detail(self.db.get_discovery_data(d)))
            box.append(b)
        self._carlab_push(box, self._carlab_t("cars.carlab.discoveries"))

    def _open_discovery_detail(self, data: dict) -> None:
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(12); box.set_margin_bottom(12)
        box.set_margin_start(12); box.set_margin_end(12)
        head = f"{data.get('module', '?')}  (tx={data.get('tx')} rx={data.get('rx')})"
        box.append(Gtk.Label(label=head, xalign=0.0))

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

    def _open_find_functions(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12); box.set_margin_bottom(12)
        box.set_margin_start(12); box.set_margin_end(12)
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
        self._carlab_push(box, self._carlab_t("cars.carlab.find"))

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

    def _open_findings(self) -> None:
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(12); box.set_margin_bottom(12)
        box.set_margin_start(12); box.set_margin_end(12)

        status = self._carlab_status_label()
        export = Gtk.Button(label=self._carlab_t("cars.carlab.export"))
        export.set_halign(Gtk.Align.START)
        export.connect("clicked", lambda _b: self._carlab_export_findings(status))
        box.append(export)
        box.append(status)

        rows = []
        if self.db is not None and self._selected_car_id is not None:
            rows = self.db.list_findings_for_car(self._selected_car_id)
        if not rows:
            box.append(Gtk.Label(label=self._carlab_t("cars.carlab.findings.empty"), xalign=0.0))
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
            fid = int(row["id"])
            trash.connect("clicked", lambda _b, f=fid: self._carlab_delete_finding(f))
            rb.append(trash)
            box.append(rb)
        scroll.set_child(box)
        self._carlab_push(scroll, self._carlab_t("cars.carlab.findings"))

    def _carlab_delete_finding(self, finding_id: int) -> None:
        if self.db is not None:
            self.db.delete_finding(finding_id)
        self.nav_view.pop()
        self._open_findings()

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
