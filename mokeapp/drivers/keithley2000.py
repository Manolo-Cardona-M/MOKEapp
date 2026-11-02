"""Compatibility module: the multimeter driver now lives in drivers/multimeter.py (per-model profiles)."""
from mokeapp.drivers.multimeter import ScpiMultimeter as Keithley2000, estimate_read_time  # noqa: F401
