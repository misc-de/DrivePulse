"""Startup diagnostics for DrivePulse."""
from __future__ import annotations

import shutil
from importlib import metadata, util

from drivepulse_app.common import OBD_BAUDRATE, OBD_FAST, OBD_PORT, OBD_TIMEOUT_SECONDS
from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)


def _emit(line: str) -> None:
    """Print to stdout and write the same line to drivepulse.log."""
    print(line)
    log.info("%s", line)


REQUIRED_PYTHON_PACKAGES = (
    ("PyGObject",     "gi",           "GTK4 / libadwaita Python bindings"),
    ("pycairo",       "cairo",        "Cairo drawing library (gauges, dashboard)"),
    ("pyserial",      "serial",       "Serial Bluetooth/USB port support"),
    ("obd",           "obd",          "OBD-II dongle support"),
    ("requests",      "requests",     "HTTP client (routing, geocoding, updates)"),
    ("cryptography",  "cryptography", "TLS encryption (device sync)"),
)

OPTIONAL_PYTHON_PACKAGES = (
    ("urllib3", "urllib3", "HTTP pool (bundled with requests)"),
)

# (display_name, gi_namespace, gi_version, apt_hint, description, required)
GI_LIBRARIES = (
    ("GTK 4",       "Gtk",      "4.0", "gir1.2-gtk-4.0",       "GTK4 widgets",                           True),
    ("libadwaita",  "Adw",      "1",   "gir1.2-adw-1",          "GNOME design widgets",                   True),
    ("GdkPixbuf",   "GdkPixbuf","2.0", "gir1.2-gdkpixbuf-2.0", "Image formats (QR code display)",        True),
    ("WebKit 6",    "WebKit",   "6.0", "gir1.2-webkit-6.0",     "Map backend (vector 3D, preferred)",    False),
    ("Shumate",     "Shumate",  "1.0", "gir1.2-shumate-1.0",    "Map backend (raster, fallback)",        False),
    ("GStreamer",   "Gst",      "1.0", "gir1.2-gstreamer-1.0",  "Dashcam recording & QR scanner",        False),
)

SYSTEM_BINARIES = (
    ("espeak-ng", "Text-to-speech — simple, always available (fallback)", False),
    ("piper",     "Text-to-speech — natural neural voices (recommended)", False),
    ("aplay",     "Audio playback for Piper TTS (ALSA)", False),
)


def _py_status(package_name: str, module_name: str) -> tuple[bool, str]:
    installed = util.find_spec(module_name) is not None
    if not installed:
        return False, "missing"
    try:
        return True, f"installed ({metadata.version(package_name)})"
    except metadata.PackageNotFoundError:
        return True, "installed"


def _gi_status(namespace: str, version: str) -> tuple[bool, str]:
    try:
        import gi
        gi.require_version(namespace, version)
        getattr(__import__("gi.repository", fromlist=[namespace]), namespace)
        return True, "available"
    except Exception:
        return False, "missing"


def get_missing_required() -> list[tuple[str, str, str]]:
    """Return (display_name, install_cmd, description) for every missing required dep."""
    missing: list[tuple[str, str, str]] = []
    for pkg, mod, desc in REQUIRED_PYTHON_PACKAGES:
        ok, _ = _py_status(pkg, mod)
        if not ok:
            missing.append((pkg, f"pip install {pkg}", desc))
    for name, ns, ver, apt, desc, required in GI_LIBRARIES:
        if not required:
            continue
        ok, _ = _gi_status(ns, ver)
        if not ok:
            missing.append((name, f"sudo apt install {apt}", desc))
    return missing


def print_required_python_packages() -> None:
    _emit("Python packages (required):")
    all_ok = True
    for pkg, mod, desc in REQUIRED_PYTHON_PACKAGES:
        ok, status = _py_status(pkg, mod)
        if not ok:
            all_ok = False
        mark = "✓" if ok else "✗"
        _emit(f"  {mark} {pkg}: {status} — {desc}")

    _emit("Python packages (optional):")
    for pkg, mod, desc in OPTIONAL_PYTHON_PACKAGES:
        ok, status = _py_status(pkg, mod)
        mark = "✓" if ok else "–"
        _emit(f"  {mark} {pkg}: {status} — {desc}")

    _emit("GI libraries:")
    for name, ns, ver, apt, desc, required in GI_LIBRARIES:
        ok, status = _gi_status(ns, ver)
        if required and not ok:
            all_ok = False
        if ok:
            mark = "✓"
            hint = ""
        elif required:
            mark = "✗"
            hint = f"  →  sudo apt install {apt}"
        else:
            mark = "–"
            hint = f"  →  sudo apt install {apt}"
        label = "required" if required else "optional"
        _emit(f"  {mark} {name} ({label}): {status} — {desc}{hint}")

    _emit("System binaries:")
    piper_found = False
    for binary, desc, required in SYSTEM_BINARIES:
        found = shutil.which(binary) is not None
        if binary == "piper":
            piper_found = found
        if required and not found:
            all_ok = False
        mark = "✓" if found else ("✗" if required else "–")
        status = "found" if found else "not installed"
        label = "required" if required else "optional"
        hint = f"  →  sudo apt install {binary}" if not found and binary != "piper" else ""
        _emit(f"  {mark} {binary} ({label}): {status} — {desc}{hint}")
    if not piper_found:
        _emit("  ℹ  Piper (recommended): pip install piper-tts")
        _emit("     Voices: https://huggingface.co/rhasspy/piper-voices/tree/main")
        _emit("     Path:   ~/.local/share/piper/<model>.onnx")

    if not all_ok:
        _emit("  ⚠  Some required packages are missing – the app may not start correctly.")

    _emit("OBD configuration:")
    _emit(f"  - OBD_PORT: {OBD_PORT or 'auto (/dev/rfcomm*, /dev/ttyUSB*, /dev/ttyACM*)'}")
    _emit(f"  - OBD_BAUDRATE: {OBD_BAUDRATE or 'auto'}")
    _emit(f"  - OBD_TIMEOUT: {OBD_TIMEOUT_SECONDS:.1f}s")
    _emit(f"  - OBD_FAST: {'on' if OBD_FAST else 'off'}")
    if OBD_PORT is None:
        _emit("  - Bluetooth hint: pair ELM327 and start with e.g. OBD_PORT=/dev/rfcomm0")
        _emit("  - Direct BT: OBD_BT_ADDR=AA:BB:CC:DD:EE:FF (or AA:BB:CC:DD:EE:FF:channel)")
        _emit("  - socat bridge: OBD_SOCKET_URL=socket://localhost:35000")
