# Credits & Third-Party Projects

DrivePulse stands on a lot of open-source work. The projects below are
either bundled with the app, required at runtime, or queried as web
services. Thank you to everyone behind them.

---

## Python libraries

### python-OBD *(optional)*
ELM327 serial communication and decoding of live vehicle data.
- https://github.com/brendan-w/python-OBD — GNU GPL v2
- **Optional** dependency (`pip install drivepulse[obd]`). When present it is
  preferred for its broader PID/protocol/adapter coverage; when absent,
  DrivePulse falls back to its own GPL-free native ELM327 backend
  (`drivepulse_app/obd/native.py`).
- Deliberately **never bundled** with DrivePulse. This keeps the project's
  PolyForm Noncommercial license free of GPL v2 copyleft obligations on the
  combined work — do not vendor or redistribute python-OBD alongside the app
  (e.g. in the Flatpak).

### pyserial
Serial transport for USB and Bluetooth RFCOMM links to the adapter.
- https://github.com/pyserial/pyserial — BSD

### PyGObject & pycairo
Python bindings for GTK/GLib/GIO and Cairo — every widget and every
custom-drawn gauge, chart and map tile goes through them.
- https://pygobject.gnome.org — LGPL 2.1+
- https://github.com/pygobject/pycairo — LGPL 2.1 / MPL 1.1

### dbus-python
Bluetooth pairing agent for OBD adapters (`obd/_pair_agent.py`).
- https://gitlab.freedesktop.org/dbus/dbus-python — MIT

### Requests
HTTP client used for routing, geocoding, VIN lookups and update checks.
- https://github.com/psf/requests — Apache 2.0

### urllib3
Retry/back-off policy of the shared HTTP session (`http_client.py`).
- https://github.com/urllib3/urllib3 — MIT

### cryptography
TLS key-pair generation and on-the-wire encryption for the device-sync
flow.
- https://github.com/pyca/cryptography — Apache 2.0 / BSD

### Piper TTS *(optional)*
Offline neural text-to-speech for voice navigation. Voice models are
downloaded from Hugging Face on demand.
- https://github.com/rhasspy/piper — MIT

### NumPy *(optional)*
Speeds up greyscaling OSM tiles for the trip map previews.
- https://numpy.org — BSD 3-Clause

---

## Bundled JavaScript

### MapLibre GL JS
WebGL vector and 3D map renderer. Vendored under
`drivepulse_app/map/vendor/maplibre-gl-4.7.1/` so the WebKit map backend
starts without a network round-trip.
- https://github.com/maplibre/maplibre-gl-js — BSD 3-Clause

---

## System libraries (GObject Introspection)

### GTK 4
Primary UI toolkit.
- https://gtk.org — LGPL 2.1+

### libadwaita
GNOME HIG widgets — `Adw.ApplicationWindow`, `Adw.PreferencesDialog` and
friends.
- https://gnome.pages.gitlab.gnome.org/libadwaita — LGPL 2.1+

### WebKitGTK *(optional, preferred map backend)*
GTK port of WebKit — hosts MapLibre GL JS for vector and 3D maps.
- https://webkitgtk.org — LGPL 2.0+

### libshumate *(optional, raster map fallback)*
GTK4-native map widget used when WebKit is unavailable.
- https://gnome.pages.gitlab.gnome.org/libshumate — LGPL 2.1+

### GStreamer *(optional)*
Multimedia pipeline for dashcam recording, live preview and the webcam
QR-code scanner.
- https://gstreamer.freedesktop.org — LGPL 2.0+

### zxing-cpp *(optional, via `gst-plugin-zxing`)*
Bar-/QR-code decoder backing the GStreamer `zxing` element used during
device pairing.
- https://github.com/zxing-cpp/zxing-cpp — Apache 2.0

### libsecret *(optional)*
Keeps API keys (VIN decoders) in the desktop keyring instead of settings.json.
- https://gitlab.gnome.org/GNOME/libsecret — LGPL 2.1+

---

## System services

### BlueZ
Linux Bluetooth stack — pairing and RFCOMM links to OBD adapters
(`bluetoothctl`, `hciconfig`, D-Bus agent).
- https://www.bluez.org — GPL 2.0+

### GeoClue2
Position and speed on Linux phones (D-Bus).
- https://gitlab.freedesktop.org/geoclue/geoclue — GPL 2.0+ / LGPL 2.1+

### gpsd
Position from external GPS receivers (TCP 2947), alongside GeoClue.
- https://gpsd.io — BSD 2-Clause

### iio-sensor-proxy
Accelerometer orientation fallback (`net.hadess.SensorProxy`).
- https://gitlab.freedesktop.org/hadess/iio-sensor-proxy — GPL 3.0

---

## System CLI tools

### qrencode
Renders the pairing QR code (SVG) for the sync server.
- https://fukuchi.org/works/qrencode — LGPL 2.1+

### eSpeak NG *(optional, voice fallback)*
Lightweight speech synthesiser used when Piper is not installed.
- https://github.com/espeak-ng/espeak-ng — GPL 3.0

### v4l-utils (`v4l2-ctl`) *(optional)*
Enumerates available camera devices for dashcam setup.
- https://git.linuxtv.org/v4l-utils.git — GPL 2.0

### FFmpeg *(optional, dashcam fallback)*
Records the dashcam when GStreamer is unavailable.
- https://ffmpeg.org — LGPL 2.1+ / GPL 2.0+ (depending on build)

### alsa-utils (`aplay`) *(optional)*
Plays Piper's speech output.
- https://github.com/alsa-project/alsa-utils — GPL 2.0+

---

## Web services & APIs

### OpenStreetMap
Map tiles and the underlying geodata that everything else builds on.
- https://www.openstreetmap.org — ODbL 1.0
- Tile usage policy: https://operations.osmfoundation.org/policies/tiles

### Nominatim
Address geocoding (text → coordinates), OSM-hosted instance.
- https://nominatim.org
- Usage policy: https://operations.osmfoundation.org/policies/nominatim

### OpenFreeMap
Keyless vector map styles (Liberty, Dark) for the WebKit/MapLibre map.
- https://openfreemap.org — data © OpenStreetMap contributors (ODbL),
  schema/styles OpenMapTiles

### Overpass API
Speed limits along a planned route (overpass-api.de, with the community
mirrors overpass.kumi.systems and overpass.private.coffee).
- https://overpass-api.de — data © OpenStreetMap contributors (ODbL)

### Valhalla (`valhalla.openstreetmap.de`)
Primary routing engine — turn-by-turn instructions with speed limits.
- https://github.com/valhalla/valhalla — MIT
- Public instance run by FOSSGIS e.V. — https://www.fossgis.de

### OSRM (`router.project-osrm.org`)
Fallback routing engine when Valhalla is unreachable.
- https://github.com/Project-OSRM/osrm-backend — BSD 2-Clause

### Autobahn App API
Live traffic incidents and roadworks on German motorways.
- https://autobahn.api.bund.dev — provided by Autobahn GmbH des Bundes

### Stadt Köln — Verkehrslage & Verkehrskalender
Live congestion per road section and the city's closures/roadworks calendar
for the traffic layer.
- https://www.offenedaten-koeln.de — Datenlizenz Deutschland – Zero – 2.0
  (Amt für Verkehrsmanagement Stadt Köln)

### Bundesstadt Bonn — Straßenverkehrslage (Realtime)
Live congestion per road section for the traffic layer.
- https://opendata.bonn.de — CC0. Datenquelle: Bundesstadt Bonn, Amt 66,
  https://opendata.bonn.de

### Esri / ArcGIS *(optional tile layers)*
World Imagery for the satellite map style; World Dark/Light Gray Canvas
(base + reference labels) for the dark and grayscale styles of the raster
map. © Esri, Maxar, Earthstar Geographics.
- https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer
- https://server.arcgisonline.com/ArcGIS/rest/services/Canvas

### NHTSA vPIC
Free VIN decoder — no API key required.
- https://vpic.nhtsa.dot.gov/api

### auto.dev *(optional, API key)*
Extended VIN decoding with make, model, trim and spec data.
- https://auto.dev

### vindecoder.eu *(optional, API key)*
European VIN decoder with detailed vehicle specifications.
- https://vindecoder.eu

### Hugging Face — Piper voices
Hosts the Piper TTS voice models downloaded on demand.
- https://huggingface.co/rhasspy/piper-voices — varies per voice (mostly MIT / CC0)

---

## Reference data

### Wikipedia — OBD-II PIDs
Basis of the SAE J1979 Mode 01 table (`obd/vehicles/standard_pids.toml`).
- https://en.wikipedia.org/wiki/OBD-II_PIDs — CC BY-SA 4.0

### Ross-Tech Wiki and VAG coding communities
Coding/adaptation functions for the Audi A6 4G
(`obd/vehicles/audi_a6_4g_coding.toml`), from public VCDS/OBDeleven
references (Ross-Tech wiki, motor-talk and blafusel coding threads).
- https://wiki.ross-tech.com

---

## Icons & themes

### Adwaita Icon Theme
System icon theme used throughout the GTK4 interface.
- https://gitlab.gnome.org/GNOME/adwaita-icon-theme — LGPL 3.0 / CC BY-SA 3.0
