"""Startup diagnostics for DrivePulse."""
from __future__ import annotations

import shutil
from importlib import metadata, util

from .common import OBD_BAUDRATE, OBD_FAST, OBD_PORT, OBD_TIMEOUT_SECONDS
from .diagnostics import get_logger


log = get_logger(__name__)


def _emit(line: str) -> None:
    """Print to stdout and write the same line to drivepulse.log."""
    print(line)
    log.info("%s", line)


REQUIRED_PYTHON_PACKAGES = (
    ("PyGObject",     "gi",           "GTK4 / libadwaita Python-Bindings"),
    ("pycairo",       "cairo",        "Cairo-Zeichenbibliothek (Gauges, Dashboard)"),
    ("pyserial",      "serial",       "Serielle Bluetooth/USB-Port-Anbindung"),
    ("obd",           "obd",          "OBD-II Dongle-Anbindung"),
    ("requests",      "requests",     "HTTP-Client (Routing, Geocoding, Updates)"),
    ("cryptography",  "cryptography", "TLS-Verschlüsselung (Gerätesync)"),
)

OPTIONAL_PYTHON_PACKAGES = (
    ("urllib3", "urllib3", "HTTP-Pool (wird mit requests mitgeliefert)"),
)

# (display_name, gi_namespace, gi_version, apt_hint, description, required)
GI_LIBRARIES = (
    ("GTK 4",       "Gtk",      "4.0", "gir1.2-gtk-4.0",       "GTK4-Widgets",                            True),
    ("libadwaita",  "Adw",      "1",   "gir1.2-adw-1",          "GNOME-Design-Widgets",                    True),
    ("GdkPixbuf",   "GdkPixbuf","2.0", "gir1.2-gdkpixbuf-2.0", "Bildformate (QR-Code-Anzeige)",           True),
    ("WebKit 6",    "WebKit",   "6.0", "gir1.2-webkit-6.0",     "Karten-Backend (Vektor-3D, bevorzugt)",  False),
    ("Shumate",     "Shumate",  "1.0", "gir1.2-shumate-1.0",    "Karten-Backend (Raster, Fallback)",      False),
    ("GStreamer",   "Gst",      "1.0", "gir1.2-gstreamer-1.0",  "Dashcam-Aufnahme & QR-Scanner",          False),
)

SYSTEM_BINARIES = (
    ("espeak-ng", "Sprachausgabe — einfach, immer verfügbar (Fallback)", False),
    ("piper",     "Sprachausgabe — natürliche neuronale Stimmen (empfohlen)", False),
    ("aplay",     "Audio-Wiedergabe für Piper TTS (ALSA)", False),
)


def _py_status(package_name: str, module_name: str) -> tuple[bool, str]:
    installed = util.find_spec(module_name) is not None
    if not installed:
        return False, "fehlt"
    try:
        return True, f"installiert ({metadata.version(package_name)})"
    except metadata.PackageNotFoundError:
        return True, "installiert"


def _gi_status(namespace: str, version: str) -> tuple[bool, str]:
    try:
        import gi
        gi.require_version(namespace, version)
        getattr(__import__("gi.repository", fromlist=[namespace]), namespace)
        return True, "verfügbar"
    except Exception:
        return False, "fehlt"


def print_required_python_packages() -> None:
    _emit("Python-Pakete (erforderlich):")
    all_ok = True
    for pkg, mod, desc in REQUIRED_PYTHON_PACKAGES:
        ok, status = _py_status(pkg, mod)
        if not ok:
            all_ok = False
        mark = "✓" if ok else "✗"
        _emit(f"  {mark} {pkg}: {status} — {desc}")

    _emit("Python-Pakete (optional):")
    for pkg, mod, desc in OPTIONAL_PYTHON_PACKAGES:
        ok, status = _py_status(pkg, mod)
        mark = "✓" if ok else "–"
        _emit(f"  {mark} {pkg}: {status} — {desc}")

    _emit("GI-Bibliotheken:")
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
        label = "erforderlich" if required else "optional"
        _emit(f"  {mark} {name} ({label}): {status} — {desc}{hint}")

    _emit("Systemprogramme:")
    piper_found = False
    for binary, desc, required in SYSTEM_BINARIES:
        found = shutil.which(binary) is not None
        if binary == "piper":
            piper_found = found
        if required and not found:
            all_ok = False
        mark = "✓" if found else ("✗" if required else "–")
        status = "gefunden" if found else "nicht installiert"
        label = "erforderlich" if required else "optional"
        hint = f"  →  sudo apt install {binary}" if not found and binary != "piper" else ""
        _emit(f"  {mark} {binary} ({label}): {status} — {desc}{hint}")
    if not piper_found:
        _emit("  ℹ  Piper (empfohlen): pip install piper-tts")
        _emit("     Stimmen: https://huggingface.co/rhasspy/piper-voices/tree/main")
        _emit("     Ablage:  ~/.local/share/piper/<modell>.onnx")

    if not all_ok:
        _emit("  ⚠  Einige erforderliche Pakete fehlen – die App startet möglicherweise nicht korrekt.")

    _emit("OBD-Konfiguration:")
    _emit(f"  - OBD_PORT: {OBD_PORT or 'auto (/dev/rfcomm*, /dev/ttyUSB*, /dev/ttyACM*)'}")
    _emit(f"  - OBD_BAUDRATE: {OBD_BAUDRATE or 'auto'}")
    _emit(f"  - OBD_TIMEOUT: {OBD_TIMEOUT_SECONDS:.1f}s")
    _emit(f"  - OBD_FAST: {'an' if OBD_FAST else 'aus'}")
    if OBD_PORT is None:
        _emit("  - Bluetooth-Hinweis: ELM327 koppeln und z. B. mit OBD_PORT=/dev/rfcomm0 starten.")
        _emit("  - Direktes BT: OBD_BT_ADDR=AA:BB:CC:DD:EE:FF (oder AA:BB:CC:DD:EE:FF:Kanal)")
        _emit("  - socat-Brücke: OBD_SOCKET_URL=socket://localhost:35000")
