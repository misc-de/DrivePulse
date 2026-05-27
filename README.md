# DrivePulse

<img src="icons/icon.png" alt="DrivePulse" width="128"/>


⚠️ **AI-assisted project**
> **⚠ Under active development — not ready for production use.**
> Features, configuration and data formats may change at any time without notice.

OBD-II dashboard built on GTK4 / libadwaita. Connects to an OBD-Device and reads vehicle data via the OBD-II interface. GPS speed is read in parallel via GeoClue2 or GPSD.

---

## Screenshots
<img width="270" alt="Screenshot from 2026-05-24 14:08:48" src="https://github.com/user-attachments/assets/30c50be2-fbe1-4f8b-b9ba-605b32c2e9a5" />
<img width="270" alt="Screenshot from 2026-05-27 21:18:02" src="https://github.com/user-attachments/assets/819b42c2-6e90-4fc8-adf3-59c406f81e3a" />
<img width="270" alt="Screenshot from 2026-05-27 21:16:25" src="https://github.com/user-attachments/assets/ba8359d4-0cd5-46a6-a494-95d5ceb0845f" />
<img width="270" alt="Screenshot from 2026-05-27 21:19:38" src="https://github.com/user-attachments/assets/b5525b98-b073-493e-bee4-68278b712dab" />
<img width="270" alt="Screenshot from 2026-05-27 12:47:14" src="https://github.com/user-attachments/assets/67adaa48-1a51-4694-bed0-9a753203a4ce" />
<img width="270" alt="Screenshot from 2026-05-21 15:26:27" src="https://github.com/user-attachments/assets/fbc09e8d-91c0-47cb-bbbe-93f4db0fb095" />
<img width="270" alt="Screenshot from 2026-05-21 11:30:55" src="https://github.com/user-attachments/assets/6b4162f0-4d46-411b-b410-d84bb2b4eed9" />
<img width="270" alt="Screenshot from 2026-05-20 15:26:31" src="https://github.com/user-attachments/assets/bfa59330-3453-4d5e-8faf-b8b77d90d1e4" />
<img width="270" alt="Screenshot from 2026-05-20 15:27:08" src="https://github.com/user-attachments/assets/d6b19ceb-3621-487d-bb20-aa61cb94c7a6" />
<img width="270" alt="Screenshot from 2026-05-20 15:28:02" src="https://github.com/user-attachments/assets/c15c68e5-32a9-46e6-a8c0-98df3c190800" />
<img width="270" alt="Screenshot from 2026-05-27 21:23:05" src="https://github.com/user-attachments/assets/7c7d9fdf-30ff-4000-b97d-f042936917a9" />

---

## Features

- Multiple dashboard themes (Analog, Cockpit, Digital, Modern, Neon, Racing, Sport)
- **StopWatch measurement**
  - G-force ball display with real-time longitudinal and lateral G
  - 0–30 / 0–50 / 0–70 / 0–100 / … / 0–200 km/h and 100–200 km/h from OBD and GPS
  - Vmax elapsed times (OBD / GPS / average) shown at end of run
  - replay completed runs with real-time animation
- **Cars / Trips page**
  - lists all vehicles seen, with recorded trips
  - OBD scan history and StopWatch runs per vehicle
  - trip detail includes speed/RPM/G chart + map track
  - StopWatch runs stored per vehicle (date, GPS location, all split times, G-force peaks)
- **Device sync**
  - transfer the full database between two devices over a local Wi-Fi connection
  - server generates a QR code, client scans it in local networks; TLS-encrypted
- **Navigation (Tour)**
  - Address search via Nominatim geocoding, up to 5 waypoints
  - Car routing via Valhalla with OSRM fallback
  - Turn-by-turn maneuver overlay with icon, distance and instruction
  - Scrollable step list panel
  - GPS tracking with auto-follow and map rotation
  - 2D / 3D map view toggle, satellite and night map types
  - Traffic incidents on German motorways (Autobahn)
- **Voice navigation (TTS) under heavy construction !**
  - Reads out OSRM maneuver instructions via `espeak-ng` (no street names, just direction + distance)
  - Announces at 500 m, 200 m and 50 m before each maneuver
  - Language: English or German (or follow app language)
  - Voice: male or female
- **Dashcam**
  - Continuous rolling buffer recording (configurable segment length and count)
  - One-tap event save to preserve clips around an incident
  - Configurable camera, resolution and screen dim timeout
  - Optional GPS/speed OSD burned into the video
- Settings: units (km/h / mph), language (English / German), gauge theme, mock mode toggle

---

## Requirements

### Required packages

```bash
sudo apt install \
  python3-gi python3-cairo \
  gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gdkpixbuf-2.0 \
  python3-pip

pip install --user obd pyserial requests cryptography
```

### Optional packages

| Package | Install | Used for |
|---|---|---|
| `gir1.2-webkit-6.0` | `sudo apt install gir1.2-webkit-6.0` | Vector/3D maps (preferred) |
| `gir1.2-shumate-1.0` | `sudo apt install gir1.2-shumate-1.0` | Raster maps (fallback) |
| `gir1.2-gstreamer-1.0` | `sudo apt install gir1.2-gstreamer-1.0` | Dashcam & QR scanner |
| `espeak-ng` | `sudo apt install espeak-ng` | Voice navigation (simple, always works) |
| `piper-tts` | `pip install --user piper-tts` | Voice navigation (natural neural voices, recommended) |
| `alsa-utils` | `sudo apt install alsa-utils` | Audio playback for piper (`aplay`) |

> For maps to work, at least one of WebKit 6 or Shumate must be installed. Voice guidance stays silent if neither espeak-ng nor piper is found.

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
bash scripts/install.sh
```

Installs the icon and `.desktop` file to `~/.local/share/` so DrivePulse appears in the application menu.

```bash
bash scripts/uninstall.sh   # to remove
```

---

## Project structure

```
drivepulse.py              Launcher (imports drivepulse_app.app)
drivepulse_app/
  app.py                   Application entry point (Adw.Application subclass)
  app_settings.py          Load / save persistent user settings (JSON)
  common.py                Shared constants and utility functions
  translations.py          Translation catalog (EN / DE) and _translate helper
  diagnostics.py           Logging helpers
  http_client.py           Shared HTTP client (connection pooling, per-host rate limiting)

  dashboard_window.py      Main application window (Adw.ApplicationWindow)
  dashboard_layout.py      Responsive gauge layout mixin (portrait / landscape)
  dashboard_settings.py    Settings callbacks mixin
  dashboard_telemetry.py   OBD + GPS payload dispatch mixin
  dashboard_data.py        DashData dataclass used by dashboard themes
  dashboard.py             Dashboard canvas and theme dispatcher

  gauge.py                 Circular gauge widget (Cairo)
  draw_helpers.py          Shared Cairo drawing utilities
  rotated_container.py     Single-child container with 0/90/180/270° rotation
  rotation.py              Screen rotation state (sensor + manual override)

  stopwatch.py             StopWatch measurement page (GTK widget)
  stopwatch_canvas.py      G-force ball canvas widget
  stopwatch_processing.py  Payload processing mixin (timing, G logic)
  stopwatch_replay.py      Run replay mixin

  cars.py                  Vehicles / trips / scans / stopwatch runs page
  cars_layout.py           Cars page layout mixin (sidebar / detail split)
  cars_detail_render.py    Car detail content renderer
  cars_metadata.py         OBD PID catalogue and category definitions
  cars_profiles.py         Vehicle profile loader from the SQLite database
  cars_actions.py          Car CRUD actions (rename, delete)
  cars_photos.py           Vehicle photo gallery (upload, grid view, delete)
  cars_trips.py            Trip list and detail widgets
  cars_trip_widgets.py     Trip detail chart + map widget
  cars_trip_visuals.py     Trip chart drawing helpers
  cars_scans.py            Scan list and detail widgets
  cars_scan_widgets.py     Scan detail widget
  cars_stopwatch_runs.py   StopWatch run list and detail widgets

  map_page.py              Navigation/Tour page (GPS tracking, routing, turn-by-turn)
  map_services.py          Map data helpers (routing, traffic, geometry)
  map_webkit.py            WebKit vector/3D map backend (MapLibre GL JS)
  map_shumate.py           Shumate raster map backend (GTK4 native, fallback)
  mock_tour.py             Mock tour simulator (drives OSRM route, emits GPS payloads)

  dashcam_page.py          Dashcam page (live preview, loop recording, screen dimmer)
  dashcam_recorder.py      Dashcam loop recorder (segmented recording, event save)

  db.py                    SQLite storage (cars, trips, samples, scans, acceleration_runs)
  trip_recorder.py         Ongoing trip recording logic

  obd_reader.py            OBD connection manager (real + mock)
  obd_polling.py           OBD polling loop
  obd_scanner.py           Full OBD scan (PIDs, DTCs, identity)
  obd_devices.py           ELM327 device detection
  mock_obd.py              Mock OBD simulator for development
  bluetooth_bridge.py      Bluetooth RFCOMM helper

  gps_reader.py            GeoClue2 + GPSD reader
  orientation_reader.py    Accelerometer sensor reader (g-force data)

  sync_dialog.py           Sync UI (server / client flow, QR display)
  sync_server.py           HTTPS sync server (TLS, port auto-select 8765+)
  sync_client.py           Sync client (TLS, certificate pinning)
  sync_flow.py             Pairing URL parsing and sync protocol
  sync_data.py             Database export / import and paired-device registry
  sync_crypto.py           TLS key-pair generation, SPKI fingerprint, token helpers
  sync_identity.py         Persistent device identity (ID + certificate paths)
  sync_poller.py           Background poller (checks reachability of known sync devices)
  sync_qrgen.py            Pure-Python QR code generator (SVG → GdkPixbuf)
  sync_qr_scanner.py       Webcam QR scanner via GStreamer + zxing

  share_flow.py            GTK UI flow for share operations
  share_protocol.py        Share protocol (payload builders, VIN helpers, server-side import)

  settings_dialog.py       Settings UI (Adw.PreferencesDialog)
  tts_service.py           Text-to-speech service (espeak-ng / piper backend, non-blocking)
  icon_registry.py         Bundled SVG icon registration
  startup_info.py          Python package dependency checker
  updater.py               Update checker and installer (git pull)
  telemetry_utils.py       Telemetry helpers

themes/
  analog.py                Analog halfmoon dashboard theme
  analog_light.py          Analog halfmoon theme (light variant)
  cockpit.py               Cockpit theme
  cockpit_light.py         Cockpit theme (light variant)
  digital.py               Digital theme
  digital_light.py         Digital theme (light variant)
  modern.py                Modern gauge theme
  modern_light.py          Modern gauge theme (light variant)
  neon.py                  Neon theme
  neon_light.py            Neon theme (light variant)
  racing.py                Racing theme
  racing_light.py          Racing theme (light variant)
  sport.py                 Sport theme
  sport_light.py           Sport theme (light variant)
  _minimal.py              Minimal theme skeleton
  _vorlage.py              Theme template / boilerplate
icons/
  icon.png                 App icon (128×128 PNG)
  icons.gresource.xml      GResource manifest
  icons.gresource          Compiled icon bundle
  hicolor/symbolic/actions/  SVG icons (currentColor, 16×16)
scripts/
  install.sh, uninstall.sh  Desktop-Entry installer
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

SQLite file at `~/.local/state/drivepulse/drives.sqlite3` by default
(`OBD_LOG_DIR` can override the base directory):

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
