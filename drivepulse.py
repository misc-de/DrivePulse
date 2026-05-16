#!/usr/bin/env python3
"""Compatibility launcher for DrivePulse.

Running this file starts the app. Importing ``drivepulse`` returns the real
application module so existing tests and external imports keep working.
"""
from __future__ import annotations

import sys

from drivepulse_app import app as _app

if __name__ == "__main__":
    raise SystemExit(_app.main())

sys.modules[__name__] = _app
