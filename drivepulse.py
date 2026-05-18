#!/usr/bin/env python3
"""Compatibility launcher for DrivePulse.

Running this file starts the app. Importing ``drivepulse`` returns the real
application module so existing tests and external imports keep working.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

sys.dont_write_bytecode = True

_PROJECT_ROOT = Path(__file__).resolve().parent
for _pycache in _PROJECT_ROOT.rglob("__pycache__"):
    shutil.rmtree(_pycache, ignore_errors=True)

_XDG_CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
for _sub in ("de.cais.DrivePulse", "WebKitGTK", "webkitgtk"):
    shutil.rmtree(_XDG_CACHE / _sub, ignore_errors=True)

from drivepulse_app import app as _app

if __name__ == "__main__":
    raise SystemExit(_app.main())

sys.modules[__name__] = _app
