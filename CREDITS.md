# Credits & Third-Party Projects

DrivePulse stands on a lot of open-source work. The projects below are
either bundled with the app, required at runtime, or queried as web
services. Thank you to everyone behind them.

---

## Python libraries

### python-OBD
ELM327 serial communication and decoding of live vehicle data.
- https://github.com/brendan-w/python-OBD — GNU GPL v2

### pyserial
Serial transport for USB and Bluetooth RFCOMM links to the adapter.
- https://github.com/pyserial/pyserial — BSD

### Requests
HTTP client used for routing, geocoding, VIN lookups and update checks.
- https://github.com/psf/requests — Apache 2.0

### cryptography
TLS key-pair generation and on-the-wire encryption for the device-sync
flow.
- https://github.com/pyca/cryptography — Apache 2.0 / BSD

### Piper TTS *(optional)*
Offline neural text-to-speech for voice navigation. Voice models are
downloaded from Hugging Face on demand.
- https://github.com/rhasspy/piper — MIT

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

### Valhalla (`valhalla.openstreetmap.de`)
Primary routing engine — turn-by-turn instructions with speed limits.
- https://github.com/valhalla/valhalla — MIT

### OSRM (`router.project-osrm.org`)
Fallback routing engine when Valhalla is unreachable.
- https://github.com/Project-OSRM/osrm-backend — BSD 2-Clause

### Autobahn App API
Live traffic incidents and roadworks on German motorways.
- https://autobahn.api.bund.dev — provided by Autobahn GmbH des Bundes

### Esri / ArcGIS World Imagery *(optional tile layer)*
Satellite imagery for the satellite map style.
- https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer

### CARTO Dark Matter *(optional tile layer)*
Dark basemap tiles served via Fastly CDN.
- https://carto.com/basemaps

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

## Icons & themes

### Adwaita Icon Theme
System icon theme used throughout the GTK4 interface.
- https://gitlab.gnome.org/GNOME/adwaita-icon-theme — LGPL 3.0 / CC BY-SA 3.0
