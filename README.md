# DrivePulse

<img src="icon.png" alt="DrivePulse" width="128"/>

> **⚠ Under active development — not ready for production use.**
> Features, configuration and data formats may change at any time without notice.

OBD-II dashboard built on GTK4 / libadwaita. Connects to an ELM327 adapter and reads vehicle data via the OBD-II interface. GPS speed is read in parallel via GPSD.

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

On smartphones GPSD is provided by the system and already running — no setup required. DrivePulse connects automatically to `localhost:2947` and shows the GPS indicator in green as soon as a valid fix is present (mode ≥ 2).

---

## Running

```bash
python3 drivepulse.py
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `OBD_PORT` | auto | Serial port of the adapter — detected automatically, override only if needed |
| `OBD_BAUDRATE` | auto | Baud rate, e.g. `38400` |
| `OBD_TIMEOUT` | `2.0` | Timeout in seconds |
| `OBD_FAST` | `0` | Fast mode (`1` to enable) |
| `OBD_POLL_INTERVAL` | `0.5` | Poll interval in seconds |
| `OBD_LOG_DIR` | `~/.local/state/drivepulse` | Directory for logs and settings |
| `DRIVEPULSE_LANG` | system language | Override language (`en` or `de`) |

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

## Logs

| File | Contents |
|---|---|
| `obd-log.jsonl` | All OBD / GPS readings (JSONL, one line per poll) |
| `connection-log.jsonl` | Connection events including supported PIDs reported by the vehicle |
| `settings.json` | Saved settings (units, language, mock mode) |

---

## Tests

```bash
python -m pytest tests/
```

---

## License

MIT — see [LICENSE](LICENSE).
