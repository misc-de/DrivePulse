from __future__ import annotations

import logging


def _reload_diagnostics(monkeypatch, tmp_path):
    """Re-import diagnostics with a clean LOG_DIR + reset root state."""
    import importlib
    import sys

    # Clear any previously-attached handlers so this test starts fresh.
    root = logging.getLogger("drivepulse_app")
    for h in list(root.handlers):
        root.removeHandler(h)

    monkeypatch.setenv("OBD_LOG_DIR", str(tmp_path))
    if "drivepulse_app.diagnostics" in sys.modules:
        return importlib.reload(sys.modules["drivepulse_app.diagnostics"])
    from drivepulse_app import diagnostics
    return diagnostics


def test_get_logger_attaches_a_single_handler_for_the_whole_package(monkeypatch, tmp_path):
    """Regression: previously every get_logger() call attached its own
    FileHandler to the named logger. With 76 modules that meant 76 file
    descriptors writing to drivepulse.log. After the refactor, the
    rotating file handler lives on the drivepulse_app parent logger and
    children propagate to it."""
    diag = _reload_diagnostics(monkeypatch, tmp_path)
    diag._root_configured = False  # force re-setup with our tmp dir

    a = diag.get_logger("drivepulse_app.foo")
    b = diag.get_logger("drivepulse_app.bar")
    root = logging.getLogger("drivepulse_app")

    assert len(root.handlers) == 1, "exactly one handler on the parent logger"
    assert a.handlers == [] and b.handlers == [], "no per-module handlers"
    assert a.propagate is True and b.propagate is True
    assert a.getEffectiveLevel() == logging.INFO


def test_file_handler_rotates(monkeypatch, tmp_path):
    """The handler must be a RotatingFileHandler so drivepulse.log cannot
    grow unbounded over months of in-car use."""
    import logging.handlers

    diag = _reload_diagnostics(monkeypatch, tmp_path)
    diag._root_configured = False

    diag.get_logger("drivepulse_app.test_rotation")
    root = logging.getLogger("drivepulse_app")

    assert len(root.handlers) == 1
    handler = root.handlers[0]
    assert isinstance(handler, logging.handlers.RotatingFileHandler), (
        f"expected RotatingFileHandler, got {type(handler).__name__}"
    )
    assert handler.maxBytes > 0
    assert handler.backupCount > 0


def test_set_log_enabled_disables_all_child_loggers(monkeypatch, tmp_path):
    diag = _reload_diagnostics(monkeypatch, tmp_path)
    diag._root_configured = False

    child = diag.get_logger("drivepulse_app.child")
    assert child.getEffectiveLevel() == logging.INFO

    diag.set_log_enabled(False)
    assert child.getEffectiveLevel() == logging.CRITICAL

    diag.set_log_enabled(True)
    assert child.getEffectiveLevel() == logging.INFO
