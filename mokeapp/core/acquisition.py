"""Point-by-point sweep engine (no GUI code, so it can be tested and scripted).

For every point: set magnet voltage (ramped) -> wait settle time -> read the Keithley while the DAQ
captures the Kepco monitors -> safety checks -> report. The magnet always ends at 0 V.
"""
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

import mokeapp
from mokeapp.core import storage
from mokeapp.core.calibration import MT_TO_OE
from mokeapp.core.processing import average_cycles, correct_drift, drift_fit_plateaus
from mokeapp.core.waveforms import make_waveform

log = logging.getLogger(__name__)

NAN = float("nan")
MONITOR_RETRIES = 2
DRIFT_WARN = 0.2        # warn when the drift within one cycle exceeds 20 % of the Kerr step


class SafetyError(Exception):
    pass


@dataclass
class SweepSettings:
    waveform: str = "triangle"
    amplitude_V: float = 2.5
    points_per_cycle: int = 200
    settle_ms: float = 50.0
    cycles: int | None = 5          # None = run until stopped (continuous mode)
    average: bool = True
    drift_correction: str = "off"   # processing.DRIFT_METHODS: off | endpoints | plateaus
    sample: str = ""
    folder: Path | None = None      # None = do not save


@dataclass
class Point:
    index: int
    time_s: float
    cycle: int
    v_programmed: float
    v_kepco: float                  # NaN without Kepco monitors
    current: float                  # measured magnet current; NaN without Kepco monitors
    signal: float
    hall_mT: float | None = None    # only with a Hall probe wired to the DAQ
    i_set: float = float("nan")     # programmed current V / nominal R

    @property
    def x_current(self) -> float:
        """Measured current when there are monitors, else the programmed one."""
        return self.i_set if np.isnan(self.current) else self.current


@dataclass
class CycleData:
    cycle: int
    v_programmed: np.ndarray
    v_kepco: np.ndarray
    current: np.ndarray
    signal: np.ndarray
    hall_mT: np.ndarray | None = None
    i_set: np.ndarray | None = None


@dataclass
class SweepResult:
    completed_cycles: int = 0
    stopped: bool = False
    error: str = ""
    raw_path: Path | None = None
    average_path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    monitor_glitches: int = 0       # bad monitor captures that a retry corrected
    # every point of a hysteresis run, kept in memory so it can be analysed (and saved later) even
    # when 'Save data' was off; path = the raw CSV if it was saved, else None
    measurement: storage.Measurement | None = field(default=None, repr=False)   # keep the log readable


def _noop(*_):
    pass


class SweepRunner:
    def __init__(self, instruments, settings: SweepSettings,
                 on_point: Callable[[Point], None] = _noop,
                 on_cycle: Callable[[CycleData, CycleData | None], None] = _noop,
                 on_warning: Callable[[str], None] = _noop):
        self.inst = instruments
        self.cfg = instruments.cfg
        self.s = settings
        self.on_point = on_point
        self.on_cycle = on_cycle
        self.on_warning = on_warning
        self.stop_event = threading.Event()
        self.result = SweepResult()
        self.cal = self.cfg.calibration     # frozen for the whole run, even if the active coil changes
        self.hall = instruments.hall_enabled    # a Hall probe on the DAQ: its field is recorded per point
        self.monitors = instruments.monitors    # Kepco monitors wired: measured current + safety checks
        dc = settings.drift_correction      # older callers passed a bool
        self.drift_method = "endpoints" if dc is True else ("off" if dc is False else dc)
        self._warned: set[str] = set()

    def stop(self) -> None:
        self.stop_event.set()

    # ---------------------------------------------------------------- safety
    def _warn(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.result.warnings.append(msg)
            log.warning(msg)
            self.on_warning(msg)

    def _monitor_problem(self, v_set: float, v_meas: float, i_meas: float) -> str:
        """'' if the Kepco monitors agree with the setpoint, else the reason (also flags a hot magnet)."""
        sc = self.cfg.safety
        r_ref = self.inst.kepco.resistance_ohm
        i_exp = v_set / r_ref
        if abs(i_exp) < sc.min_current_for_checks_A:
            return ""
        tol = max(sc.current_tolerance_A, sc.current_tolerance_frac * abs(i_exp))
        if abs(i_meas - i_exp) > tol:
            return (f"Magnet current {i_meas:+.3f} A does not match expected {i_exp:+.3f} A "
                    f"(at {v_set:+.2f} V, Kepco reads {v_meas:+.2f} V). Check the magnet connection and the Kepco.")
        if abs(i_meas) > sc.min_current_for_checks_A:
            rise = (v_meas / i_meas) / r_ref - 1
            if rise > sc.resistance_warn_frac:
                return "hot"
        return ""

    def _confirm_monitors(self, v_set: float, v_meas: float, i_meas: float) -> tuple[float, float]:
        """Re-measure when the monitors disagree with the setpoint, before believing it.

        The USB-6002 occasionally returns one bad capture (seen as 0 A on the first point after the
        app sat idle, or a fake 16 % resistance rise). A real fault (open magnet, Kepco off) persists
        through the retries and still stops the run, ~0.2 s later.
        """
        problem = self._monitor_problem(v_set, v_meas, i_meas)
        if not problem:
            return v_meas, i_meas
        kepco, daq = self.inst.kepco, self.inst.daq
        for attempt in range(1, MONITOR_RETRIES + 1):
            log.warning("Monitor check failed (%s, V=%+.3f, I=%+.3f): retry %d", problem, v_meas, i_meas, attempt)
            daq.write_ao(kepco.output_V / kepco.cfg.gain)   # re-send the setpoint in case the write was lost
            time.sleep(max(self.s.settle_ms / 1000.0, 0.03))
            v_meas, i_meas = kepco.to_physical(*daq.read_monitors())
            problem = self._monitor_problem(v_set, v_meas, i_meas)
            if not problem:
                self.result.monitor_glitches += 1
                log.warning("Monitor reading recovered on retry %d (V=%+.3f, I=%+.3f)", attempt, v_meas, i_meas)
                return v_meas, i_meas
        if problem != "hot":
            raise SafetyError(problem)
        return v_meas, i_meas

    def _check_drift(self, cycle: int, signal, current, times) -> None:
        """Warn (once) when the signal drifts by more than DRIFT_WARN of the Kerr step within a cycle."""
        if self.s.cycles is None:   # Continuous: live view (and the stability test runs at 0 A)
            return
        fit = drift_fit_plateaus(signal, current, times)
        if fit.has_loop and abs(fit.drift) > DRIFT_WARN * abs(fit.step):
            self._warn("drift", f"Signal drifted {fit.drift * 1000:+.2f} mV during cycle {cycle + 1} "
                                f"({abs(fit.drift / fit.step) * 100:.0f} % of the {abs(fit.step) * 1000:.2f} mV "
                                "Kerr step): use drift correction, or check the laser with the stability test.")

    def _check(self, p: Point) -> None:
        sc = self.cfg.safety
        if self.monitors:   # without monitors there is no measured current: these checks cannot run
            r_ref = self.inst.kepco.resistance_ohm
            v_meas, i_meas = p.v_kepco, p.current
            if abs(p.v_programmed / r_ref) >= sc.min_current_for_checks_A and abs(i_meas) > sc.min_current_for_checks_A:
                r = v_meas / i_meas
                rise = r / r_ref - 1
                if rise > sc.resistance_stop_frac:
                    raise SafetyError(f"Magnet resistance rose {rise * 100:.0f} % ({r:.2f} Ω): magnet overheating.")
                if rise > sc.resistance_warn_frac:
                    self._warn("hot", f"Magnet resistance rose {rise * 100:.0f} % ({r:.2f} Ω): magnet is heating up.")
            if abs(i_meas) >= 0.98 * self.cfg.kepco.i_max:
                self._warn("ilimit", "Kepco is at its current limit: the field no longer follows the setpoint.")
        if self.cal:
            i_cal = self._calibration_current(p)
            if not self.cal.in_range(i_cal):
                lo, hi = self.cal.current_range
                self._warn("calrange", f"Current {i_cal:+.2f} A is outside the calibrated range ({lo:.2f} to "
                                       f"{hi:.2f} A): the field there is extrapolated.")
        signal = p.signal
        rng = self.cfg.keithley.range_V
        if rng != "auto" and abs(signal) > 0.95 * float(rng):
            self._warn("sat", f"Photodetector signal {signal:.3f} V is near the Keithley range ({rng} V).")

    # ------------------------------------------------------------------ run
    def _calibration_current(self, p: Point) -> float:
        """The current of the kind the coil was calibrated with (measured or programmed)."""
        if self.cal.current_source == "measured" and not self.monitors:
            self._warn("calsource", "The field calibration was made with the measured current, but the Kepco "
                                    "monitors are off: the programmed current is used instead.")
        return p.current if self.cal.current_source == "measured" and self.monitors else p.i_set

    def _fields(self, p: Point) -> list[float]:
        """[B in mT, H in Oe] for one point."""
        b = float(self.cal.field(self._calibration_current(p)))
        return [b, b * MT_TO_OE]

    def _metadata(self, started: datetime, columns: list[str]) -> dict:
        s = self.s
        cal = self.cal
        return {
            "app": f"MOKEapp v{mokeapp.__version__}",
            "date": started.isoformat(timespec="seconds"),
            "setup": self.cfg.name,
            "sample": s.sample,
            "columns": storage.describe_columns(columns, cal),
            "x_axis": [c for c in columns if c.endswith(storage.X_TAG)],
            "y_axis": storage.COL_SIGNAL,
            "calibration": cal.header() if cal else "none (field calibration not active: X is current only)",
            **({"hall_probe": {"channel": self.cfg.daq.ai_hall, "mT_per_V": self.cfg.daq.hall_mT_per_V,
                               "offset_mT": self.cfg.daq.hall_offset_mT}} if self.hall else {}),
            "waveform": s.waveform,
            "amplitude_V": s.amplitude_V,
            "points_per_cycle": s.points_per_cycle,
            "settle_ms": s.settle_ms,
            "cycles": s.cycles,
            "averaged": s.average,
            "drift_correction": self.drift_method,     # applied to the averaged file only; raw is untouched
            "kepco_monitors": self.monitors,
            "magnet_resistance_ohm": round(self.inst.kepco.resistance_ohm, 4),     # last measured (diagnostics)
            "nominal_magnet_resistance_ohm": self.cfg.kepco.magnet_resistance_ohm,  # used for I_set = V/R
            "config": self.cfg.to_dict(),
        }

    def run(self) -> SweepResult:
        s = self.s
        cal = self.cal
        kepco, keithley, daq = self.inst.kepco, self.inst.keithley, self.inst.daq
        wave = make_waveform(s.waveform, kepco.clamp(s.amplitude_V), s.points_per_cycle)
        settle = s.settle_ms / 1000.0
        started = datetime.now()
        raw_writer = None
        field_columns = ([storage.COL_FIELD, storage.COL_FIELD_OE] if cal else []) + \
                        ([storage.COL_HALL, storage.COL_HALL_OE] if self.hall else [])
        raw_columns = storage.raw_columns(self.monitors) + field_columns
        capture = self.monitors or self.hall    # anything to read on the DAQ's analog inputs
        complete: list[CycleData] = []
        all_rows: list[list] = []
        try:
            if s.folder is not None:
                kind = "raw" if s.cycles else "continuous"
                self.result.raw_path = storage.make_path(s.folder, s.sample, kind, started)
                raw_writer = storage.CsvWriter(self.result.raw_path, self._metadata(started, raw_columns),
                                               raw_columns)
            if capture:
                daq.read_monitors()         # throw-away capture: wakes the DAQ after idle time
            kepco.set_voltage(wave[0])
            time.sleep(max(settle, 0.2))
            t0 = time.monotonic()
            cycle = 0
            while not self.stop_event.is_set() and (s.cycles is None or cycle < s.cycles):
                pts: list[Point] = []
                for k, v in enumerate(wave):
                    if self.stop_event.is_set():
                        break
                    v_set = kepco.set_voltage(v)
                    if settle:
                        time.sleep(settle)
                    if capture:
                        daq.start_capture()
                    signal = keithley.read()
                    v_meas, i_meas = kepco.to_physical(*daq.finish_capture()) if capture else (NAN, NAN)
                    if self.monitors:
                        v_meas, i_meas = self._confirm_monitors(v_set, v_meas, i_meas)
                    hall = self.inst.last_hall_mT() if self.hall else None    # same capture as the monitors
                    p = Point(k, time.monotonic() - t0, cycle, v_set, v_meas, i_meas, signal, hall,
                              kepco.programmed_current(v_set))
                    pts.append(p)
                    self._check(p)
                    self.on_point(p)
                rows = [self._row(p) for p in pts]
                if raw_writer and rows:
                    raw_writer.write_rows(rows)
                if s.cycles is not None:
                    all_rows += rows
                if len(pts) < len(wave):
                    break
                arr = np.array([[p.v_programmed, p.v_kepco, p.current, p.signal, p.i_set] for p in pts])
                times = np.array([p.time_s for p in pts])
                sweep = arr[:, 2] if self.monitors else arr[:, 4]   # field-like axis for the drift plateaus
                self._check_drift(cycle, arr[:, 3], sweep, times)
                sig = correct_drift(self.drift_method, arr[:, 3], sweep, times)
                hall = np.array([p.hall_mT for p in pts]) if self.hall else None
                cd = CycleData(cycle, arr[:, 0], arr[:, 1], arr[:, 2], sig, hall, arr[:, 4])
                if s.average and s.cycles is not None:
                    complete.append(cd)
                    avg = CycleData(cycle, **{n: average_cycles([getattr(c, n) for c in complete])
                                              for n in self._cycle_fields()})
                else:
                    avg = None
                self.result.completed_cycles = cycle + 1
                self.on_cycle(cd, avg)
                cycle += 1
            self.result.stopped = self.stop_event.is_set()
        except SafetyError as e:
            self.result.error = str(e)
            log.error("Sweep stopped by safety check: %s", e)
        except Exception as e:
            self.result.error = f"{type(e).__name__}: {e}"
            log.exception("Sweep failed")
        finally:
            self.inst.safe_zero()
            if raw_writer:
                raw_writer.close()
                if raw_writer.rows == 0:    # stopped before the first cycle: leave no header-only file
                    raw_writer.path.unlink(missing_ok=True)
                    self.result.raw_path = None
            if self.result.monitor_glitches:
                self._warn("glitch", f"{self.result.monitor_glitches} bad Kepco monitor reading(s) were "
                                     "re-measured and corrected (see log).")
        if complete and s.average and s.folder is not None:
            self._save_average(complete, started)
        if all_rows:
            self.result.measurement = storage.Measurement(
                self.result.raw_path, self._metadata(started, raw_columns), raw_columns,
                np.array(all_rows, dtype=float))
        return self.result

    def _row(self, p: Point) -> list:
        """One raw CSV row, in the order of storage.raw_columns(monitors) + field columns."""
        return ([p.index, p.time_s, p.cycle, p.v_programmed, p.i_set]
                + ([p.v_kepco, p.current] if self.monitors else []) + [p.signal]
                + (self._fields(p) if self.cal else [])
                + ([p.hall_mT, p.hall_mT * MT_TO_OE] if self.hall else []))

    def _cycle_fields(self) -> tuple[str, ...]:
        return ("v_programmed", "v_kepco", "current", "signal", "i_set") + (("hall_mT",) if self.hall else ())

    def _save_average(self, cycles: list[CycleData], started: datetime) -> None:
        cal = self.cal
        cols = {n: average_cycles([getattr(c, n) for c in cycles]) for n in self._cycle_fields()}
        n = len(cols["signal"])
        data = [np.arange(n), cols["v_programmed"], cols["i_set"]] \
            + ([cols["v_kepco"], cols["current"]] if self.monitors else []) + [cols["signal"]]
        columns = storage.avg_columns(self.monitors)
        if cal:
            measured = cols["current"] if self.monitors else None
            b = cal.field_for(measured, cols["i_set"])
            data += [b, b * MT_TO_OE]
            columns += [storage.COL_FIELD, storage.COL_FIELD_OE]
        if self.hall:
            data += [cols["hall_mT"], cols["hall_mT"] * MT_TO_OE]
            columns += [storage.COL_HALL, storage.COL_HALL_OE]
        meta = self._metadata(started, columns)
        meta["cycles_averaged"] = len(cycles)
        self.result.average_path = storage.make_path(self.s.folder, self.s.sample, "average", started)
        storage.write_table(self.result.average_path, meta, columns, np.column_stack(data))
