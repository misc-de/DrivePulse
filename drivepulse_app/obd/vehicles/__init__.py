"""Generic OBD-II PID definitions (SAE J1979 Mode 01)."""
from drivepulse_app.obd.vehicles.registry import PidDefinition, load_standard_pids, pids_by_category

__all__ = ["PidDefinition", "load_standard_pids", "pids_by_category"]
