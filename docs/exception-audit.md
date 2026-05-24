# Exception Audit

DrivePulse talks to hardware, D-Bus, GStreamer, WebKit, local network peers, and
SQLite. Broad exception handling is sometimes useful at those boundaries, but it
should be intentional and logged with enough context to debug field failures.

## Highest-Priority Areas

1. Sync security path
   - Files: `drivepulse_app/sync/client.py`, `drivepulse_app/sync/server.py`,
     `drivepulse_app/sync/crypto.py`
   - Goal: reject bad peer state explicitly; avoid keeping stale certificates,
     stale bearer sessions, or malformed payloads alive after failed validation.

2. Persistent data decode/import
   - Files: `drivepulse_app/db.py`, `drivepulse_app/sync/data.py`,
     `drivepulse_app/share/protocol.py`
   - Goal: distinguish corrupt local JSON, invalid remote payloads, and SQLite
     operational errors. Corrupt user data should be reported without hiding
     unrelated programmer errors.

3. Dashcam and QR scanner
   - Files: `drivepulse_app/dashcam/*`, `drivepulse_app/sync/qr_scanner.py`
   - Goal: keep optional GStreamer support graceful, but report missing camera,
     permission, codec, and pipeline errors separately.

4. OBD and sensor readers
   - Files: `drivepulse_app/obd/*`, `drivepulse_app/sensors/*`
   - Goal: separate optional dependency absence, transient serial failures,
     malformed adapter responses, and unsupported vehicle PIDs.

5. GTK callback boundaries
   - Files: `drivepulse_app/dashboard/*`, `drivepulse_app/cars/*`,
     `drivepulse_app/map/*`
   - Goal: keep UI callbacks resilient while logging actionable context and
     avoiding silent `pass` blocks.

## Current Cleanup Started

- `DriveDB.get_scan_data()` and `DriveDB.get_stopwatch_run()` now catch
  `json.JSONDecodeError` instead of every exception.
- `SyncClient.verify_fingerprint()` clears any previously pinned certificate
  when a new fingerprint check fails.

## Rule Of Thumb

Use broad `except Exception` only at process, thread, hardware, network, or GTK
callback boundaries. Inside pure helpers and data transformations, catch the
specific exception expected by that operation.
