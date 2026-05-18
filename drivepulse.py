#!/usr/bin/env python3
"""Compatibility launcher for DrivePulse.

Running this file starts the app. Importing ``drivepulse`` returns the real
application module so existing tests and external imports keep working.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from drivepulse_app import app as _app

if __name__ == "__main__":
    raise SystemExit(_app.main())

sys.modules[__name__] = _app
