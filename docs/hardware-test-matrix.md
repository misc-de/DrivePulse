# Hardware Test Matrix

This matrix defines the minimum checks before calling DrivePulse production-ready.

## Platforms

| Platform | Required checks |
| --- | --- |
| Desktop Linux, GTK4/libadwaita | App starts, dashboard renders, settings persist, mock mode works |
| Phosh/Mobian portrait | Bottom navigation, rotation handling, touch gestures, narrow car detail view |
| Phosh/Mobian landscape | Dashboard readability, map controls, stopwatch layout, no clipped labels |

## OBD

| Device class | Required checks |
| --- | --- |
| USB ELM327 | Connect, live RPM/speed/coolant, disconnect/reconnect, trip recording |
| Bluetooth RFCOMM ELM327 | Pair, select port, reconnect after adapter sleep |
| STN/STPX-compatible adapter | Batch query path, scan results, malformed-line handling |
| No adapter connected | Graceful fallback, clear status, mock mode remains available |

## GPS And Orientation

| Source | Required checks |
| --- | --- |
| GeoClue2 | First fix, stale-fix timeout, speed/heading update |
| GPSD | Socket reconnect, invalid JSON handling, no-fix state |
| IIO orientation sensor | Rotation changes only in mobile form factor |
| No GPS source | Dashboard remains usable, GPS indicator reports unavailable |

## Maps And Navigation

| Backend | Required checks |
| --- | --- |
| WebKit/MapLibre | 2D/3D toggle, route rendering, marker updates, map tap handling |
| Shumate fallback | Raster map loads, route polyline, zoom/follow controls |
| No map backend | Placeholder is usable and does not block other pages |
| Offline or failing routing API | Search/route errors shown without freezing the UI |

## Dashcam And QR

| Component | Required checks |
| --- | --- |
| V4L2 camera | Preview, rolling segment creation, event save |
| GPS/speed OSD | Correct overlay values, disabled state leaves clean video |
| Missing camera permission | User-visible error, no crash |
| QR scanner | Camera open, QR decode, camera unavailable path |

## Sync And Sharing

| Scenario | Required checks |
| --- | --- |
| Same Wi-Fi pairing | QR generation, TLS 1.3 connect, SPKI pinning, bearer session |
| Wrong fingerprint | Pairing rejected and stale pinned cert cleared |
| Interrupted sync | Timeout, retry behavior, no partial import corruption |
| Large database | Payload limit behavior and user-visible failure |

## Data Integrity

| Area | Required checks |
| --- | --- |
| Existing old database | Migrations apply once and preserve rows |
| Corrupt JSON blob | Affected record is skipped/reported, app continues |
| Concurrent telemetry writes | WAL remains healthy, trip sample counts match |
| Import conflict | Conflict table receives actionable payload |
