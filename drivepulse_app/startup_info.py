"""Startup diagnostics for DrivePulse."""
from __future__ import annotations

from importlib import metadata, util

from .common import OBD_BAUDRATE, OBD_FAST, OBD_PORT, OBD_TIMEOUT_SECONDS


REQUIRED_PYTHON_PACKAGES = (
    ("PyGObject", "gi", "GTK/libadwaita Python-Bindings"),
    ("pyserial", "serial", "serielle Bluetooth/USB-Port-Anbindung"),
    ("obd", "obd", "OBD-II Dongle-Anbindung"),
)


def python_package_status(package_name: str, module_name: str) -> str:
    installed = util.find_spec(module_name) is not None
    if not installed:
        return "fehlt"

    try:
        return f"installiert ({metadata.version(package_name)})"
    except metadata.PackageNotFoundError:
        return "installiert"


def print_required_python_packages() -> None:
    print("Benötigte Python-Pakete:")
    for package_name, module_name, description in REQUIRED_PYTHON_PACKAGES:
        status = python_package_status(package_name, module_name)
        print(f"  - {package_name}: {status} - {description}")
    print("OBD-Konfiguration:")
    print(f"  - OBD_PORT: {OBD_PORT or 'auto (/dev/rfcomm*, /dev/ttyUSB*, /dev/ttyACM*)'}")
    print(f"  - OBD_BAUDRATE: {OBD_BAUDRATE or 'auto'}")
    print(f"  - OBD_TIMEOUT: {OBD_TIMEOUT_SECONDS:.1f}s")
    print(f"  - OBD_FAST: {'an' if OBD_FAST else 'aus'}")
    if OBD_PORT is None:
        print("  - Bluetooth-Hinweis: ELM327 koppeln und z. B. mit OBD_PORT=/dev/rfcomm0 starten.")
        print("  - Direktes BT: OBD_BT_ADDR=AA:BB:CC:DD:EE:FF (oder AA:BB:CC:DD:EE:FF:Kanal)")
        print("  - socat-Brücke: OBD_SOCKET_URL=socket://localhost:35000")
