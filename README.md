# DrivePulse

<img src="icon.png" alt="DrivePulse" width="128"/>

> **⚠ Under active development — not ready for production use.**
> Features, configuration and data formats may change at any time without notice.

OBD-II dashboard built on GTK4 / libadwaita. Connects to an ELM327 adapter and reads vehicle data via the OBD-II interface. GPS speed is read in parallel via GPSD.

---

## Screenshots
<img width="270" alt="Screenshot from 2026-05-14 20:43:08" src="https://github.com/user-attachments/assets/e9435da4-cbc9-4a1d-85e8-aa5989fda3fd" />
<img width="270" alt="Screenshot from 2026-05-14 20:44:44" src="https://github.com/user-attachments/assets/9e1c5c0a-825d-4eba-904d-fc0c28693049" />

---

## Features

- Circular gauges for RPM, speed and coolant temperature
- Automatic landscape / portrait layout switching
- Acceleration measurement (0–30 / 0–50 / 0–70 / 0–100 / 0–150 / 0–200 km/h and 100–200 km/h) from OBD and GPS data
- GPS integration via GPSD (indicator turns green on active fix)
- Settings: units (km/h / mph), language (English / German), mock mode toggle

---

## Requirements

### System

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-pip
python3 -m pip install --user obd
```

### GPS

DrivePulse supports two GPS sources simultaneously:

| Source | How it works |
|---|---|
| **GeoClue2** | D-Bus system service — the standard on Linux phones (Phosh / Mobian). No setup required, the system provides it. |
| **GPSD** | TCP socket on `localhost:2947` — common on desktop Linux with an external GPS receiver. |

Both sources are tried automatically on startup. Whichever delivers a fix first lights the GPS indicator green. If neither is available the indicator stays grey.

---

## Running

```bash
python3 drivepulse.py
```

---

## Installation (desktop integration)

```bash
bash install.sh
```

Installs the icon and `.desktop` file to `~/.local/share/` so DrivePulse appears in the application menu.

```bash
bash uninstall.sh   # to remove
```

---

## Project structure

| File | Contents |
|---|---|
| `drivepulse.py` | Main file: OBD reader, GPS reader, settings, application window |
| `gauge.py` | Circular gauge widget (Cairo drawing) |
| `acceleration.py` | Acceleration measurement page |
| `common.py` | Shared constants, translations, utility functions |
| `install.sh` / `uninstall.sh` | Desktop integration |
| `tests/` | Pytest test suite |

---

## Tests

```bash
python -m pytest tests/
```

---

## License

MIT — see [LICENSE](LICENSE).
