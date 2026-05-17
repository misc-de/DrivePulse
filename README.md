# DrivePulse

<img src="icon.png" alt="DrivePulse" width="128"/>

> **⚠ Under active development — not ready for production use.**
> Features, configuration and data formats may change at any time without notice.

OBD-II dashboard built on GTK4 / libadwaita. Connects to an ELM327 adapter and reads vehicle data via the OBD-II interface. GPS speed is read in parallel via GeoClue2 or GPSD.

---

## Screenshots
<img width="270" alt="Screenshot from 2026-05-17 11:36:19" src="https://github.com/user-attachments/assets/456afc1d-a73f-4cc7-bc72-8de18db14cbe" />
<img width="270" alt="Screenshot from 2026-05-16 10:25:28" src="https://github.com/user-attachments/assets/a1651ab5-811c-4952-9659-40f9b69cc513" />
<img width="270" alt="Screenshot from 2026-05-16 10:26:13" src="https://github.com/user-attachments/assets/bbea574a-7eb5-47ef-8397-38fe56e37c47" />
<img width="270" alt="Screenshot from 2026-05-16 10:26:23" src="https://github.com/user-attachments/assets/caabff31-6dd5-4549-8be0-e9179ee343b3" />
<img width="270" alt="Screenshot from 2026-05-16 10:26:32" src="https://github.com/user-attachments/assets/8333b4ce-918b-46cd-85b9-bf4ea4dca611" />
<img width="270" alt="Screenshot from 2026-05-16 10:26:56" src="https://github.com/user-attachments/assets/bb4c858e-9ab1-4496-99d6-2d87f940eff1" />

---

## Features

⚠️ **AI-assisted project**

- Multiple dashboard themes (Analog, Cockpit, Digital, Modern, Neon, Racing, Sport)
- Circular and halfmoon gauges for RPM, speed, coolant temperature, fuel level, battery voltage
- Analog theme: halfmoon fuel gauge (left edge) and battery voltage gauge (right edge) with color-coded danger zones
- Automatic landscape / portrait layout switching
- **Acceleration measurement** — 0–30 / 0–50 / 0–70 / 0–100 / 0–110 / … / 0–200 km/h and 100–200 km/h from OBD and GPS data; replay completed runs with real-time animation
- GPS integration via GeoClue2 (D-Bus) and GPSD; indicator turns green on active fix
- **Cars / Trips page** — lists all vehicles seen, with recorded trips, OBD scan history and acceleration runs per vehicle; trip detail includes speed/RPM/G chart + map track
- OBD scan history with DTC fault codes, supported PIDs and trend comparison between scans
- Acceleration runs stored per vehicle (date, GPS location, all split times, G-force peaks)
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

```
drivepulse.py              Launcher (imports drivepulse_app.app)
drivepulse_app/
  app.py                   Main application window, OBD/GPS dispatch loop
  acceleration.py          Acceleration measurement page and replay
  cars.py                  Vehicles / trips / scans / acceleration runs page
  cars_metadata.py         OBD PID catalogue and category definitions
  cars_profiles.py         OBD profile loader
  cars_trip_widgets.py     Trip detail chart + map widget
  cars_scan_widgets.py     Scan detail widget
  common.py                Translations, shared constants, utility functions
  dashboard.py             Dashboard canvas and DashData dataclass
  db.py                    SQLite storage (cars, trips, samples, scans, acceleration_runs)
  draw_helpers.py          Shared Cairo drawing utilities
  gauge.py                 Circular gauge widget (Cairo)
  gps_reader.py            GeoClue2 + GPSD reader
  mock_obd.py              Mock OBD simulator for development
  obd_devices.py           ELM327 device detection
  obd_polling.py           OBD polling loop
  obd_scanner.py           Full OBD scan (PIDs, DTCs, identity)
  orientation_reader.py    Screen orientation/rotation sensor
  settings_dialog.py       Settings UI
  telemetry_utils.py       Telemetry helpers
  bluetooth_bridge.py      Bluetooth RFCOMM helper
themes/
  analog.py                Analog halfmoon dashboard theme
  cockpit.py               Cockpit theme
  digital.py               Digital theme
  modern.py                Modern gauge theme
  neon.py                  Neon theme
  racing.py                Racing theme
  sport.py                 Sport theme
icons/
  hicolor/symbolic/actions/  SVG icons (currentColor, 16×16)
icons.gresource.xml        GResource manifest
icons.gresource            Compiled icon bundle
```

### DashData variables (used by dashboard themes)

| Field | Type | Source | Description |
|---|---|---|---|
| `speed_kmh` | `float\|None` | OBD/GPS | Current speed (km/h) |
| `obd_speed_kmh` | `float\|None` | OBD | Speed from OBD sensor |
| `gps_speed_kmh` | `float\|None` | GPS | Speed from GPS |
| `speed_active` | `bool` | — | Speed value is live |
| `speed_label` | `str` | — | Formatted speed string |
| `rpm` | `float\|None` | OBD | Engine RPM |
| `rpm_active` | `bool` | — | RPM is live |
| `rpm_label` | `str` | — | Formatted RPM string |
| `coolant_c` | `float\|None` | OBD | Coolant temperature (°C) |
| `coolant_active` | `bool` | — | Coolant value is live |
| `coolant_label` | `str` | — | Formatted temperature string |
| `fuel_pct` | `float\|None` | OBD | Fuel level (%) |
| `fuel_active` | `bool` | — | Fuel value is live |
| `fuel_label` | `str` | — | Formatted fuel string |
| `voltage_v` | `float\|None` | OBD | Battery/adapter voltage (V) |
| `voltage_active` | `bool` | — | Voltage value is live |
| `voltage_label` | `str` | — | Formatted voltage string |
| `throttle_pct` | `float\|None` | OBD | Throttle position (%) |
| `engine_load` | `float\|None` | OBD | Engine load (%) |
| `heading_deg` | `float\|None` | GPS | Compass heading (°) |
| `gps_lat` | `float\|None` | GPS | Latitude |
| `gps_lon` | `float\|None` | GPS | Longitude |
| `gps_altitude_m` | `float\|None` | GPS | Altitude (m) |
| `acceleration_g` | `float\|None` | OBD | Longitudinal acceleration (g) |
| `source_label` | `str` | — | Speed source indicator ("OBD" / "GPS") |
| `language` | `str` | — | Active UI language ("en" / "de") |
| `units` | `str` | — | Speed unit ("km/h" / "mph") |

---

## Database schema

SQLite file at `~/.local/share/DrivePulse/drivepulse.db`:

| Table | Contents |
|---|---|
| `cars` | One row per vehicle (identified by VIN or OBD profile path) |
| `trips` | One row per recorded drive, linked to a car |
| `samples` | ~1–2 Hz telemetry points (OBD + GPS merged), linked to a trip |
| `scans` | Full OBD scan snapshots with PIDs and DTCs, linked to a car |
| `acceleration_runs` | Completed acceleration measurement runs with split times and GPS position, linked to a car |

---

## Tests

```bash
python -m pytest tests/
```

---

## License

MIT — see [LICENSE](LICENSE).
