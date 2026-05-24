# Credits & Third-Party Projects

DrivePulse builds on the following open-source projects and services.

---

## Python Libraries

### python-OBD
OBD-II ELM327 serial communication — decodes live vehicle sensor data.
- GitHub: https://github.com/brendan-w/python-OBD
- License: GNU GPL v2

### pyserial
Serial port communication (USB, Bluetooth RFCOMM).
- GitHub: https://github.com/pyserial/pyserial
- Docs: https://pyserial.readthedocs.io
- License: BSD

### Requests
HTTP client used for routing, geocoding, VIN lookups, and update checks.
- GitHub: https://github.com/psf/requests
- Docs: https://requests.readthedocs.io
- License: Apache 2.0

### cryptography
TLS certificate generation and encryption for the device-sync flow.
- GitHub: https://github.com/pyca/cryptography
- Docs: https://cryptography.io
- License: Apache 2.0 / BSD

### Piper TTS *(optional)*
Offline neural text-to-speech for navigation voice guidance. Voices are
downloaded from Hugging Face on demand.
- GitHub: https://github.com/rhasspy/piper
- License: MIT

---

## System Libraries (GObject Introspection)

### GTK 4
Primary UI toolkit.
- Homepage: https://gtk.org
- GitLab: https://gitlab.gnome.org/GNOME/gtk
- License: LGPL 2.1+

### libadwaita
GNOME Human Interface Guidelines widgets (Adw.ApplicationWindow,
AdwPreferencesDialog, etc.).
- Homepage: https://gnome.pages.gitlab.gnome.org/libadwaita
- GitLab: https://gitlab.gnome.org/GNOME/libadwaita
- License: LGPL 2.1+

### WebKitGTK *(optional, preferred map backend)*
WebKit port for GTK — used to render the vector/3D map via MapLibre GL JS.
- Homepage: https://webkitgtk.org
- License: LGPL 2.0+

### Shumate *(optional, raster map fallback)*
GTK4 map widget for raster tile display when WebKit is unavailable.
- Homepage: https://gnome.pages.gitlab.gnome.org/libshumate
- GitLab: https://gitlab.gnome.org/GNOME/libshumate
- License: LGPL 2.1+

### GStreamer *(optional)*
Multimedia pipeline used for dashcam recording, live preview, and QR-code
scanning via webcam.
- Homepage: https://gstreamer.freedesktop.org
- GitLab: https://gitlab.freedesktop.org/gstreamer/gstreamer
- License: LGPL 2.0+

---

## System CLI Tools

### qrencode
Generates QR code images (SVG) for device-pairing in the sync flow.
- Homepage: https://fukuchi.org/works/qrencode
- GitHub: https://github.com/fukuchi/libqrencode
- License: LGPL 2.1+

### eSpeak NG *(optional TTS fallback)*
Lightweight speech synthesiser used when Piper is not available.
- GitHub: https://github.com/espeak-ng/espeak-ng
- License: GPL 3.0

### v4l2-ctl *(optional)*
Video4Linux2 utility — enumerates available camera devices for dashcam setup.
- Part of v4l-utils: https://git.linuxtv.org/v4l-utils.git
- License: GPL 2.0

---

## Web Services & APIs

### OpenStreetMap
Map tiles (standard layer) and geodata.
- Homepage: https://www.openstreetmap.org
- Tile usage policy: https://operations.osmfoundation.org/policies/tiles
- License: ODbL 1.0

### Nominatim (OpenStreetMap)
Address geocoding (text → coordinates).
- Homepage: https://nominatim.org
- GitLab: https://github.com/osm-search/Nominatim
- Usage policy: https://operations.osmfoundation.org/policies/nominatim

### Valhalla (valhalla.openstreetmap.de)
Routing engine — turn-by-turn navigation with speed limits and manoeuvre text.
- GitHub: https://github.com/valhalla/valhalla
- Public instance: https://valhalla.openstreetmap.de
- License: MIT

### OSRM (router.project-osrm.org)
Fallback routing engine when Valhalla is unavailable.
- Homepage: http://project-osrm.org
- GitHub: https://github.com/Project-OSRM/osrm-backend
- License: BSD 2-Clause

### Autobahn App API (Autobahn GmbH)
Live traffic incidents and roadworks on German motorways.
- API docs: https://autobahn.api.bund.dev
- Provider: Autobahn GmbH des Bundes

### Esri / ArcGIS World Imagery *(optional tile layer)*
Satellite imagery tile layer.
- Homepage: https://www.esri.com
- Tile usage: https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer

### CARTO Dark Matter *(optional tile layer)*
Dark map tile layer served via Fastly CDN.
- Homepage: https://carto.com
- Basemap info: https://carto.com/basemaps

### NHTSA vPIC
Free VIN decoder provided by the US National Highway Traffic Safety Administration.
- API: https://vpic.nhtsa.dot.gov/api
- No API key required.

### auto.dev *(optional, API key required)*
Extended VIN decoding with make, model, trim, and spec data.
- Homepage: https://auto.dev

### vindecoder.eu *(optional, API key required)*
European VIN decoder with detailed vehicle specifications.
- Homepage: https://vindecoder.eu

### Hugging Face — Piper Voices
Repository hosting Piper TTS voice models downloaded on demand.
- Model repo: https://huggingface.co/rhasspy/piper-voices
- License: varies per voice (mostly MIT / CC0)

---

## Fonts & Themes

### Adwaita Icon Theme
System icon theme used throughout the GTK4 interface.
- GitLab: https://gitlab.gnome.org/GNOME/adwaita-icon-theme
- License: LGPL 3.0 / CC BY-SA 3.0
