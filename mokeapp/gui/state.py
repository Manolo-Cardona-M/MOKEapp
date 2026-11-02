"""Shared application state and background threads."""
import logging
import threading

from PySide6.QtCore import QObject, QThread, Signal

from mokeapp.core.acquisition import SweepRunner
from mokeapp.core.calibration import CalibrationLibrary
from mokeapp.drivers import Instruments

log = logging.getLogger(__name__)

X_MODES = ("current", "current_set", "mT", "Oe")


class AppState(QObject):
    connection_changed = Signal()
    diagnostics_changed = Signal()
    busy_changed = Signal(bool)
    limits_changed = Signal()
    measurement_ready = Signal()            # a new hysteresis measurement is in last_measurement
    open_in_analysis = Signal(object)       # storage.Measurement to show in the Analysis tab
    calibration_changed = Signal()          # active field calibration saved / switched
    x_mode_changed = Signal(str)            # "current", "mT" or "Oe": X axis shared by Hysteresis and Analysis

    def __init__(self, cfg, simulate: bool, prefs=None):
        """prefs: a QSettings (remembers choices on this PC) or None (nothing remembered, e.g. tests)."""
        super().__init__()
        self.cfg = cfg
        self.simulate = simulate
        self.prefs = prefs
        self.library = self._open_library(cfg.calibration_path)
        self.inst = Instruments(cfg, simulate)
        # X axis: "current" (measured), "current_set" (programmed V/R), "mT" or "Oe"
        saved = prefs.value("x_mode") if prefs is not None else None
        self.x_mode = saved if saved in X_MODES else self._default_x_mode()
        self.diagnostics = None
        self.override = False
        self.busy = False
        self.runner: SweepRunner | None = None
        self.last_measurement = None        # storage.Measurement of the last hysteresis run, saved or not
        # one user of the instruments at a time: sweeps, diagnostics, connect and the live readout
        self.io_lock = threading.Lock()

    @staticmethod
    def _open_library(folder):
        """The calibrations/ folder (unreadable files are skipped and logged, never overwritten)."""
        return CalibrationLibrary(folder)

    @property
    def calibration(self):
        """Active coil calibration (core.calibration.CoilCalibration) or None."""
        return self.cfg.calibration

    @property
    def field_available(self) -> bool:
        """The X axis can show field: from a Hall probe on the DAQ, or from the active calibration."""
        return self.inst.hall_enabled or self.cfg.calibration is not None

    def _default_x_mode(self) -> str:
        if self.field_available:
            return "mT"
        return "current" if self.inst.monitors else "current_set"

    def refresh_calibration(self) -> None:
        self.cfg.calibration = self.library.active
        if not self.field_available and self.x_mode in ("mT", "Oe"):
            self.set_x_mode(self._default_x_mode())
        self.calibration_changed.emit()

    def set_x_mode(self, mode: str) -> None:
        if mode != self.x_mode:
            self.x_mode = mode
            if self.prefs is not None:
                self.prefs.setValue("x_mode", mode)
            self.x_mode_changed.emit(mode)

    @property
    def max_voltage(self) -> float:
        return self.inst.kepco.max_voltage

    @property
    def resistance(self) -> float:
        return self.inst.kepco.resistance_ohm

    def set_resistance(self, r: float) -> None:
        self.inst.update_limits(r)
        self.limits_changed.emit()

    @property
    def diagnostics_ok(self) -> bool:
        return self.diagnostics is not None and self.diagnostics.passed

    @property
    def can_measure(self) -> bool:
        return self.inst.all_connected and (self.diagnostics_ok or self.override) and not self.busy

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.busy_changed.emit(busy)

    def emergency_stop(self) -> None:
        log.warning("STOP pressed")
        if self.runner is not None:
            self.runner.stop()          # the sweep thread ramps to 0 in its finally block
        elif not self.busy:
            self.inst.safe_zero()


class FuncThread(QThread):
    """Run a blocking call off the GUI thread; emits (result, exception)."""
    done = Signal(object, object)

    def __init__(self, fn, parent=None, lock: threading.Lock | None = None):
        super().__init__(parent)
        self.fn = fn
        self.lock = lock

    def run(self):
        try:
            if self.lock is None:
                result = self.fn()
            else:
                with self.lock:
                    result = self.fn()
            self.done.emit(result, None)
        except Exception as e:
            log.exception("Background task failed")
            self.done.emit(None, e)


class SweepThread(QThread):
    point = Signal(object)
    cycle = Signal(object, object)
    warning = Signal(str)
    result = Signal(object)

    def __init__(self, inst, settings, parent=None, lock: threading.Lock | None = None):
        super().__init__(parent)
        self.lock = lock or threading.Lock()
        self.runner = SweepRunner(inst, settings, on_point=self.point.emit,
                                  on_cycle=self.cycle.emit, on_warning=self.warning.emit)

    def run(self):
        with self.lock:     # waits for a live readout still in progress
            self.result.emit(self.runner.run())
