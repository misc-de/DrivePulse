from __future__ import annotations

import sys
import types
from pathlib import Path
from html import escape

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
    END = _EnumValue("end")
    START = _EnumValue("start")


class _PolicyType:
    NEVER = _EnumValue("never")
    AUTOMATIC = _EnumValue("automatic")


class _SelectionMode:
    NONE = _EnumValue("none")


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

    def remove_css_class(self, css_class: str) -> None:
        css_classes = self.props.setdefault("css_classes", [])
        if css_class in css_classes:
            css_classes.remove(css_class)

    def append(self, child: object) -> None:
        self.children.append(child)

    def insert(self, child: object, position: int) -> None:
        self.children.append(child) if position < 0 else self.children.insert(position, child)

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

    def get_style_context(self) -> _StyleContext:
        return _StyleContext()

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

    def set_label(self, text: str) -> None:
        self.text = text

    def get_text(self) -> str:
        return getattr(self, "text", "")


class _Label(_Widget):
    pass


class _Button(_Widget):
    pass


class _Switch(_Widget):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._active = False

    def set_active(self, active: bool) -> None:
        self._active = active

    def get_active(self) -> bool:
        return self._active


class _CheckButton(_Switch):
    pass


class _Adjustment(_Widget):
    def __init__(
        self,
        value: float = 0.0,
        lower: float = 0.0,
        upper: float = 0.0,
        step_increment: float = 1.0,
        **kwargs,
    ) -> None:
        super().__init__(
            value=value,
            lower=lower,
            upper=upper,
            step_increment=step_increment,
            **kwargs,
        )
        self.value = value


class _SpinButton(_Widget):
    def __init__(self, adjustment: _Adjustment | None = None, **kwargs) -> None:
        super().__init__(adjustment=adjustment, **kwargs)
        self.adjustment = adjustment

    def get_value_as_int(self) -> int:
        if self.adjustment is None:
            return 0
        return int(self.adjustment.value)


class _StyleContext:
    def add_provider(self, provider: object, priority: int) -> None:
        pass


class _CssProvider:
    def load_from_data(self, data: bytes) -> None:
        pass


class _Image(_Widget):
    @classmethod
    def new_from_icon_name(cls, icon_name: str) -> "_Image":
        return cls()

    @classmethod
    def new_from_file(cls, filename: str) -> "_Image":
        return cls()


class _Spinner(_Widget):
    def start(self) -> None:
        self.props["spinning"] = True

    def stop(self) -> None:
        self.props["spinning"] = False


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


class _FlowBox(_Widget):
    pass


class _StringList(_Widget):
    def append(self, value: str) -> None:
        self.children.append(value)


class _ListStore(_Widget):
    def __init__(self, item_type=None, **kwargs) -> None:
        super().__init__(item_type=item_type, **kwargs)
        self.item_type = item_type

    def append(self, value: object) -> None:
        self.children.append(value)


class _ComboRow(_Widget):
    def __init__(self, title: str = "") -> None:
        super().__init__(title=title)
        self.selected = 0

    def set_selected(self, selected: int) -> None:
        self.selected = selected

    def get_selected(self) -> int:
        return self.selected


class _ActionRow(_Widget):
    def __init__(self, title: str = "", subtitle: str = "", **kwargs) -> None:
        super().__init__(title=title, subtitle=subtitle, **kwargs)

    def add_suffix(self, widget: object) -> None:
        self.children.append(widget)

    def set_activatable_widget(self, widget: object) -> None:
        self.props["activatable_widget"] = widget


class _SwitchRow(_ActionRow):
    def __init__(self, title: str = "", subtitle: str = "", **kwargs) -> None:
        super().__init__(title=title, subtitle=subtitle, **kwargs)
        self._active = False

    def set_active(self, active: bool) -> None:
        self._active = active

    def get_active(self) -> bool:
        return self._active


class _DrawingArea(_Widget):
    pass


class _GestureSwipe(_Widget):
    pass


class _SignalListItemFactory(_Widget):
    pass


class _ListItem(_Widget):
    pass


class _ListBox(_Widget):
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


class _Stack(_Widget):
    def add_titled(self, child: object, name: str, title: str):
        self.children.append((child, name, title))
        if self.visible_child_name is None:
            self.visible_child_name = name
        return _Widget(child=child, name=name, title=title)

    def set_visible_child_name(self, name: str) -> None:
        self.visible_child_name = name


class _StackSwitcher(_Widget):
    def set_stack(self, stack: object) -> None:
        self.props["stack"] = stack


class _ToolbarView(_Widget):
    def add_top_bar(self, widget: object) -> None:
        self.children.append(widget)

    def add_bottom_bar(self, widget: object) -> None:
        self.children.append(widget)


class _NavigationPage(_Widget):
    def __init__(self, child=None, title="", **kwargs) -> None:
        super().__init__(**kwargs)
        self.props["child"] = child
        self.props["title"] = title

    def get_child(self):
        return self.props.get("child")

    def get_title(self):
        return self.props.get("title", "")


class _NavigationView(_Widget):
    def push(self, page: object) -> None:
        self.children.append(page)

    def pop(self) -> None:
        if self.children:
            self.children.pop()

    def replace(self, pages: list) -> None:
        self.children = list(pages)


class _AlertDialog(_Widget):
    def __init__(self, heading="", body="", **kwargs) -> None:
        super().__init__(**kwargs)
        self.props["heading"] = heading
        self.props["body"] = body

    def add_response(self, response_id: str, label: str) -> None:
        self.props.setdefault("responses", []).append((response_id, label))

    def set_response_appearance(self, response_id: str, appearance: object) -> None:
        self.props.setdefault("response_appearances", {})[response_id] = appearance

    def set_default_response(self, response_id: str) -> None:
        self.props["default_response"] = response_id

    def choose(self, parent, cancellable, callback) -> None:
        pass

    def present(self, parent=None) -> None:
        # Modale Restart-Dialoge sind in den Tests rein passiv — die Test-
        # Mocks haben keinen GTK-Loop. Wir merken nur, dass präsentiert wurde.
        self.props["presented"] = True
        self.props["presented_parent"] = parent

    def connect(self, signal: str, handler) -> None:
        self.props.setdefault("signals", {}).setdefault(signal, []).append(handler)


class _EntryRow(_ActionRow):
    def __init__(self, title: str = "", **kwargs) -> None:
        super().__init__(title=title, **kwargs)
        self._text = ""

    def get_text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text


class _ExpanderRow(_ActionRow):
    def __init__(self, title: str = "", subtitle: str = "", **kwargs) -> None:
        super().__init__(title=title, subtitle=subtitle, **kwargs)
        self._expanded = False

    def add_row(self, row: object) -> None:
        self.children.append(row)

    def add_action(self, widget: object) -> None:
        self.children.append(widget)

    def add_prefix(self, widget: object) -> None:
        self.children.append(widget)

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded

    def get_expanded(self) -> bool:
        return self._expanded


class _Toast(_Widget):
    def __init__(self, title: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.props["title"] = title


@pytest.fixture
def drivepulse_module(monkeypatch):
    gi = types.ModuleType("gi")
    gi.require_version = lambda *args: None

    gtk = types.SimpleNamespace(
        AccessibleRole=types.SimpleNamespace(),
        Adjustment=_Adjustment,
        Align=_Align,
        ApplicationWindow=_ApplicationWindow,
        Box=_Box,
        Button=_Button,
        CheckButton=_CheckButton,
        ContentFit=types.SimpleNamespace(
            FILL=_EnumValue("fill"),
            CONTAIN=_EnumValue("contain"),
            COVER=_EnumValue("cover"),
            SCALE_DOWN=_EnumValue("scale-down"),
        ),
        CssProvider=_CssProvider,
        ComboRow=_ComboRow,
        DragSource=_Widget,
        DrawingArea=_DrawingArea,
        DropDown=_Widget,
        DropTarget=_Widget,
        Entry=_Widget,
        EventControllerMotion=_Widget,
        EventSequenceState=types.SimpleNamespace(
            CLAIMED=_EnumValue("claimed"),
            DENIED=_EnumValue("denied"),
            NONE=_EnumValue("none"),
        ),
        FileChooserAction=types.SimpleNamespace(
            OPEN=_EnumValue("open"),
            SAVE=_EnumValue("save"),
            SELECT_FOLDER=_EnumValue("select-folder"),
        ),
        FileChooserNative=_Widget,
        FileDialog=_Widget,
        FileFilter=_Widget,
        FlowBox=_FlowBox,
        FlowBoxChild=_Widget,
        GestureClick=_Widget,
        GestureDrag=_Widget,
        GestureLongPress=_Widget,
        GestureSwipe=_GestureSwipe,
        GestureZoom=_Widget,
        Grid=_Grid,
        IconTheme=types.SimpleNamespace(get_for_display=lambda *a: _Widget()),
        Image=_Image,
        Justification=types.SimpleNamespace(
            LEFT=_EnumValue("left"),
            RIGHT=_EnumValue("right"),
            CENTER=_EnumValue("center"),
            FILL=_EnumValue("fill"),
        ),
        Label=_Label,
        ListBox=_ListBox,
        ListBoxRow=_Widget,
        ListItem=_ListItem,
        MenuButton=_Widget,
        Orientation=_Orientation,
        Overflow=types.SimpleNamespace(
            VISIBLE=_EnumValue("visible"),
            HIDDEN=_EnumValue("hidden"),
        ),
        Overlay=_Widget,
        Picture=_Widget,
        PolicyType=_PolicyType,
        Popover=_Widget,
        PositionType=types.SimpleNamespace(
            LEFT=_EnumValue("left"),
            RIGHT=_EnumValue("right"),
            TOP=_EnumValue("top"),
            BOTTOM=_EnumValue("bottom"),
        ),
        ProgressBar=_Widget,
        PropagationPhase=types.SimpleNamespace(
            BUBBLE=_EnumValue("bubble"),
            CAPTURE=_EnumValue("capture"),
            NONE=_EnumValue("none"),
            TARGET=_EnumValue("target"),
        ),
        ResponseType=types.SimpleNamespace(
            ACCEPT=_EnumValue("accept"),
            CANCEL=_EnumValue("cancel"),
            DELETE_EVENT=_EnumValue("delete-event"),
        ),
        ScrolledWindow=_Widget,
        SelectionMode=_SelectionMode,
        Separator=_Widget,
        SignalListItemFactory=_SignalListItemFactory,
        SpinButton=_SpinButton,
        Spinner=_Spinner,
        Stack=_Stack,
        StackSwitcher=_StackSwitcher,
        StackTransitionType=types.SimpleNamespace(
            NONE=_EnumValue("none"),
            SLIDE_LEFT_RIGHT=_EnumValue("slide-left-right"),
            CROSSFADE=_EnumValue("crossfade"),
        ),
        StringList=_StringList,
        StyleContext=types.SimpleNamespace(add_provider_for_display=lambda *a: None),
        Switch=_Switch,
        ToggleButton=_Widget,
        Widget=_Widget,
        Window=_Widget,
        STYLE_PROVIDER_PRIORITY_APPLICATION=600,
    )
    _style_manager_instance = types.SimpleNamespace(
        connect=lambda *a: None,
        get_dark=lambda: False,
        set_color_scheme=lambda scheme: None,
        get_color_scheme=lambda: _EnumValue("default"),
    )
    adw = types.SimpleNamespace(
        ActionRow=_ActionRow,
        AlertDialog=_AlertDialog,
        Application=_Application,
        ApplicationWindow=_ApplicationWindow,
        ColorScheme=types.SimpleNamespace(
            DEFAULT=_EnumValue("default"),
            FORCE_DARK=_EnumValue("force_dark"),
            FORCE_LIGHT=_EnumValue("force_light"),
        ),
        ComboRow=_ComboRow,
        Dialog=_Widget,
        EntryRow=_EntryRow,
        ExpanderRow=_ExpanderRow,
        HeaderBar=_Widget,
        MessageDialog=_Widget,
        NavigationPage=_NavigationPage,
        NavigationView=_NavigationView,
        PreferencesDialog=_Widget,
        PreferencesGroup=_Widget,
        PreferencesPage=_Widget,
        ResponseAppearance=types.SimpleNamespace(
            SUGGESTED=_EnumValue("suggested"),
            DESTRUCTIVE=_EnumValue("destructive"),
        ),
        StyleManager=types.SimpleNamespace(get_default=lambda: _style_manager_instance),
        SwitchRow=_SwitchRow,
        Toast=_Toast,
        ToolbarView=_ToolbarView,
        ViewStack=_ViewStack,
        ViewSwitcher=_Widget,
        ViewSwitcherBar=_Widget,
        ViewSwitcherPolicy=types.SimpleNamespace(
            NARROW=_EnumValue("narrow"),
            WIDE=_EnumValue("wide"),
        ),
    )
    glib = types.SimpleNamespace(
        idle_add=lambda callback, *args: callback(*args),
        markup_escape_text=lambda text: escape(str(text)),
    )
    gobject = types.SimpleNamespace(Object=object)
    gio = types.SimpleNamespace(ListStore=_ListStore)
    pango = types.SimpleNamespace(WrapMode=_WrapMode)
    gdk = types.SimpleNamespace(Display=types.SimpleNamespace(get_default=lambda: None))

    repository = types.ModuleType("gi.repository")
    repository.Gtk = gtk
    repository.Adw = adw
    repository.GLib = glib
    repository.GObject = gobject
    repository.Gio = gio
    repository.Pango = pango
    repository.Gdk = gdk
    repository.Gsk = types.SimpleNamespace(Transform=types.SimpleNamespace(new=lambda: None))
    repository.Graphene = types.SimpleNamespace(Point=types.SimpleNamespace(alloc=lambda: types.SimpleNamespace(init=lambda x, y: None)))

    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    monkeypatch.setitem(sys.modules, "obd", None)
    for _mod in list(sys.modules):
        if _mod == "drivepulse" or _mod.startswith("drivepulse_app"):
            sys.modules.pop(_mod, None)

    import drivepulse

    yield drivepulse
    for _mod in list(sys.modules):
        if _mod == "drivepulse" or _mod.startswith("drivepulse_app"):
            sys.modules.pop(_mod, None)


@pytest.fixture
def tmp_log_paths(monkeypatch, drivepulse_module, tmp_path: Path):
    from drivepulse_app import app_settings
    from drivepulse_app.obd import reader as obd_reader

    log_dir = tmp_path / "state"
    monkeypatch.setattr(drivepulse_module, "SETTINGS_FILE", log_dir / "settings.json")
    monkeypatch.setattr(obd_reader, "LOG_DIR", log_dir)
    monkeypatch.setattr(obd_reader, "LOG_FILE", log_dir / "obd-log.jsonl")
    monkeypatch.setattr(obd_reader, "CONNECTION_LOG_FILE", log_dir / "connection-log.jsonl")
    monkeypatch.setattr(app_settings, "LOG_DIR", log_dir)
    monkeypatch.setattr(app_settings, "SETTINGS_FILE", log_dir / "settings.json")
    return log_dir
