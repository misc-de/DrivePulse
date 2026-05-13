from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


class _EnumValue:
    def __init__(self, name: str) -> None:
        self.name = name


class _Orientation:
    HORIZONTAL = _EnumValue("horizontal")
    VERTICAL = _EnumValue("vertical")


class _Align:
    FILL = _EnumValue("fill")
    CENTER = _EnumValue("center")
    START = _EnumValue("start")


class _PolicyType:
    NEVER = _EnumValue("never")
    AUTOMATIC = _EnumValue("automatic")


class _WrapMode:
    WORD_CHAR = _EnumValue("word-char")


class _Widget:
    def __init__(self, *args, **kwargs) -> None:
        self.children = []
        self.props = dict(kwargs)
        self.visible_child_name = None
        self.width = kwargs.get("width", 0)
        self.height = kwargs.get("height", 0)
        if "label" in kwargs:
            self.text = kwargs["label"]

    def __getattr__(self, name: str):
        if name.startswith("set_"):
            key = name[4:]

            def setter(*args):
                self.props[key] = args[0] if len(args) == 1 else args

            return setter
        if name.startswith("get_"):
            key = name[4:]

            def getter(*args):
                return self.props.get(key, 0)

            return getter
        raise AttributeError(name)

    def add_css_class(self, css_class: str) -> None:
        self.props.setdefault("css_classes", []).append(css_class)

    def append(self, child: object) -> None:
        self.children.append(child)

    def add(self, child: object) -> None:
        self.children.append(child)

    def attach(self, child: object, *args) -> None:
        self.children.append((child, args))

    def connect(self, *args) -> None:
        self.props.setdefault("signals", []).append(args)

    def add_controller(self, controller: object) -> None:
        self.props["controller"] = controller

    def add_tick_callback(self, callback):
        self.props["tick_callback"] = callback

    def queue_draw(self) -> None:
        self.props["queued_draw"] = True

    def set_size_request(self, width: int, height: int) -> None:
        self.props["size_request"] = (width, height)

    def get_width(self) -> int:
        return int(self.props.get("width", self.width))

    def get_height(self) -> int:
        return int(self.props.get("height", self.height))

    def set_text(self, text: str) -> None:
        self.text = text

    def get_text(self) -> str:
        return getattr(self, "text", "")


class _Label(_Widget):
    pass


class _Button(_Widget):
    pass


class _Box(_Widget):
    def __init__(self, orientation=None, spacing=0, **kwargs) -> None:
        super().__init__(**kwargs)
        self.props["orientation"] = orientation
        self.props["spacing"] = spacing

    def set_orientation(self, orientation) -> None:
        self.props["orientation"] = orientation


class _Grid(_Widget):
    def __init__(self, column_spacing=0, row_spacing=0, **kwargs) -> None:
        super().__init__(**kwargs)
        self.props["column_spacing"] = column_spacing
        self.props["row_spacing"] = row_spacing


class _StringList(_Widget):
    def append(self, value: str) -> None:
        self.children.append(value)


class _ComboRow(_Widget):
    def __init__(self, title: str = "") -> None:
        super().__init__(title=title)
        self.selected = 0

    def set_selected(self, selected: int) -> None:
        self.selected = selected

    def get_selected(self) -> int:
        return self.selected


class _DrawingArea(_Widget):
    pass


class _GestureSwipe(_Widget):
    pass


class _ApplicationWindow(_Widget):
    pass


class _Application(_Widget):
    def __init__(self, application_id: str | None = None) -> None:
        super().__init__(application_id=application_id)

    def run(self, _args=None) -> int:
        return 0


class _ViewStack(_Widget):
    def add_titled_with_icon(self, child: object, name: str, title: str, icon_name: str):
        self.children.append((child, name, title, icon_name))
        if self.visible_child_name is None:
            self.visible_child_name = name
        return _Widget(child=child, name=name, title=title, icon_name=icon_name)

    def set_visible_child_name(self, name: str) -> None:
        self.visible_child_name = name

    def get_visible_child_name(self) -> str | None:
        return self.visible_child_name


@pytest.fixture
def drivepulse_module(monkeypatch):
    gi = types.ModuleType("gi")
    gi.require_version = lambda *args: None

    gtk = types.SimpleNamespace(
        AccessibleRole=types.SimpleNamespace(),
        Align=_Align,
        ApplicationWindow=_ApplicationWindow,
        Box=_Box,
        Button=_Button,
        ComboRow=_ComboRow,
        DrawingArea=_DrawingArea,
        GestureSwipe=_GestureSwipe,
        Grid=_Grid,
        Label=_Label,
        Orientation=_Orientation,
        PolicyType=_PolicyType,
        ScrolledWindow=_Widget,
        StringList=_StringList,
        Window=_Widget,
    )
    adw = types.SimpleNamespace(
        Application=_Application,
        ApplicationWindow=_ApplicationWindow,
        ComboRow=_ComboRow,
        HeaderBar=_Widget,
        PreferencesDialog=_Widget,
        PreferencesGroup=_Widget,
        PreferencesPage=_Widget,
        ToolbarView=_Widget,
        ViewStack=_ViewStack,
        ViewSwitcherBar=_Widget,
    )
    glib = types.SimpleNamespace(idle_add=lambda callback, *args: callback(*args))
    gobject = types.SimpleNamespace(Object=object)
    gio = types.SimpleNamespace()
    pango = types.SimpleNamespace(WrapMode=_WrapMode)

    repository = types.ModuleType("gi.repository")
    repository.Gtk = gtk
    repository.Adw = adw
    repository.GLib = glib
    repository.GObject = gobject
    repository.Gio = gio
    repository.Pango = pango

    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    monkeypatch.setitem(sys.modules, "obd", None)
    sys.modules.pop("drivepulse", None)

    import drivepulse

    yield drivepulse
    sys.modules.pop("drivepulse", None)


@pytest.fixture
def tmp_log_paths(monkeypatch, drivepulse_module, tmp_path: Path):
    log_dir = tmp_path / "state"
    monkeypatch.setattr(drivepulse_module, "LOG_DIR", log_dir)
    monkeypatch.setattr(drivepulse_module, "LOG_FILE", log_dir / "obd-log.jsonl")
    monkeypatch.setattr(drivepulse_module, "CONNECTION_LOG_FILE", log_dir / "connection-log.jsonl")
    monkeypatch.setattr(drivepulse_module, "SETTINGS_FILE", log_dir / "settings.json")
    return log_dir
