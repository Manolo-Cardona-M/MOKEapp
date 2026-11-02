"""Continuous and Hysteresis tabs (both drive the same sweep engine)."""
import logging
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
                               QSpinBox, QSplitter, QVBoxLayout, QWidget)

from mokeapp.core.acquisition import SweepSettings
from mokeapp.core.processing import stability_report
from mokeapp.gui import theme
from mokeapp.gui.state import SweepThread
from mokeapp.gui.widgets import AutoScale, DriftCombo, SweepParams, XAxisToggle, make_plot, to_x, x_label

log = logging.getLogger(__name__)

CONTROLS_WIDTH = 400
DRIVE_MIN_SPAN_V = 0.5      # smallest Y span of the 'Magnet drive' plot


def live_curve(plot, symbols=True):
    c = theme.color("live")
    if symbols:
        return plot.plot(pen=theme.pen("live"), symbol="o", symbolSize=4, symbolBrush=c, symbolPen=None)
    return plot.plot(pen=theme.pen("live"))


class SaveOptions(QGroupBox):
    """'Save data' switch plus sample name and folder. Chosen before Start, locked while measuring."""

    def __init__(self, default_folder: Path, checked: bool = False):
        super().__init__("Save data")
        self.setCheckable(True)
        self.setChecked(checked)
        f = QFormLayout(self)
        self.sample = QLineEdit()
        self.sample.setPlaceholderText("sample name")
        self.folder = QLineEdit(str(default_folder))
        browse = QPushButton("...")
        browse.setObjectName("browse")
        browse.setFixedWidth(34)
        browse.clicked.connect(self._browse)
        h = QHBoxLayout()
        h.addWidget(self.folder)
        h.addWidget(browse)
        f.addRow("Sample", self.sample)
        f.addRow("Folder", h)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Data folder", self.folder.text())
        if d:
            self.folder.setText(d)

    def validate(self, parent) -> bool:
        if self.isChecked() and not self.sample.text().strip():
            QMessageBox.warning(parent, "Save data", "Enter a sample name, or turn off 'Save data'.")
            return False
        return True

    def target(self) -> tuple[str, Path | None]:
        if not self.isChecked():
            return "", None
        return self.sample.text().strip(), Path(self.folder.text())


class MeasureTab(QWidget):
    """Shared Start/Stop, saving and redraw logic. Subclasses build the plots and handle points/cycles.

    save_option=False (Continuous tab): no saving and no 'Open in Analysis'.
    """

    start_label = "Start"

    def __init__(self, state, default_folder: Path, save_option: bool = True):
        super().__init__()
        self.state = state
        self.thread: SweepThread | None = None
        self.params = SweepParams(state)
        self.save = SaveOptions(default_folder) if save_option else None
        self.start_btn = QPushButton()
        self.start_btn.setProperty("role", "primary")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setProperty("role", "danger")
        self.progress = QLabel("Idle")
        self.progress.setWordWrap(True)
        self.saved = QLabel("")
        self.saved.setWordWrap(True)
        self.saved.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.open_btn = QPushButton("Open in Analysis")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(lambda: state.open_in_analysis.emit(state.last_measurement))
        self.warnings = QPlainTextEdit()
        self.warnings.setReadOnly(True)
        self.warnings.setMaximumHeight(60)
        # only shown when there is something to show, so the panel fits a 768-px-high screen
        for w in (self.warnings, self.saved, self.open_btn):
            w.setVisible(False)
        self.start_btn.clicked.connect(lambda: self.start())
        self.stop_btn.clicked.connect(self.stop)
        if self.save is not None:
            self.save.toggled.connect(lambda _: self._update_start_label())
        for sig in (state.busy_changed, state.connection_changed, state.diagnostics_changed):
            sig.connect(lambda *_: self.refresh())
        self._update_start_label()
        # Points only append data; the plots redraw at most every 50 ms
        self._dirty = False
        self._last_point = None
        self.redraw_timer = QTimer(self)
        self.redraw_timer.setInterval(50)
        self.redraw_timer.timeout.connect(self._redraw_if_dirty)

    def _update_start_label(self):
        if self.save is None:
            self.start_btn.setText(self.start_label)
        else:
            self.start_btn.setText(f"{self.start_label}  (saving)" if self.save.isChecked()
                                   else f"{self.start_label}  (not saving)")

    def _redraw_if_dirty(self):
        if self._dirty:
            self._dirty = False
            self.update_plots(self._last_point)
            self.autoscale.apply()

    def _point(self, p):
        self._last_point = p
        self._dirty = True
        self.on_point(p)

    def controls_box(self, *extra: QWidget) -> QWidget:
        """Settings in a scroll area; Start/Stop pinned below it so they are always on screen."""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 6, 0)
        v.addWidget(self.params)
        for e in extra:
            v.addWidget(e)
        if self.save is not None:
            v.addWidget(self.save)
        v.addWidget(self.warnings)
        if self.save is not None:
            v.addWidget(self.saved)
            v.addWidget(self.open_btn)
        v.addStretch()
        scroll = QScrollArea()
        scroll.setWidget(w)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.NoFrame)
        self.warnings.setLineWrapMode(QPlainTextEdit.WidgetWidth)

        panel = QWidget()
        panel.setFixedWidth(CONTROLS_WIDTH)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.addWidget(scroll, 1)
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 6, 0)
        h.addWidget(self.start_btn, 2)
        h.addWidget(self.stop_btn, 1)
        pv.addLayout(h)
        pv.addWidget(self.progress)
        return panel

    def plots_area(self, plots: dict, orientation=Qt.Horizontal, sizes: list[int] | None = None,
                   extra: QWidget | None = None) -> QWidget:
        """Plots in a splitter the user can drag, with the auto-scale switch (and `extra`) on top."""
        self.autoscale = AutoScale(plots)
        splitter = QSplitter(orientation)
        splitter.setChildrenCollapsible(False)
        for p in plots:
            splitter.addWidget(p)
        if sizes:
            splitter.setSizes(sizes)
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.addWidget(self.autoscale, 1)
        if extra is not None:
            bar.addWidget(extra)
        v.addLayout(bar)
        v.addWidget(splitter, 1)
        return w

    def refresh(self):
        running = self.thread is not None
        self.start_btn.setEnabled(self.state.can_measure and not running)
        self.stop_btn.setEnabled(running)
        if self.save is not None:
            self.save.setEnabled(not running)
        if not running:
            if not self.state.inst.all_connected:
                self.progress.setText("Connect the instruments in the Instruments tab")
            elif not (self.state.diagnostics_ok or self.state.override):
                self.progress.setText("Run diagnostics in the Instruments tab first")
            elif self.progress.text().startswith(("Connect", "Run diag")):
                self.progress.setText("Ready")

    def settings(self) -> SweepSettings:
        raise NotImplementedError

    def start(self, s: SweepSettings | None = None):
        """Start a run with the tab's settings, or with `s` (e.g. the stability test)."""
        if self.save is not None and not self.save.validate(self):
            return
        s = s or self.settings()
        self.warnings.clear()
        self.warnings.setVisible(False)
        self.saved.setText("Saving to " + str(s.folder) if s.folder else "Not saving to disk (you can still "
                                                                         "open it in Analysis and save it there)")
        self.saved.setVisible(self.save is not None)
        self.open_btn.setEnabled(False)
        self.on_start(s)
        self.autoscale.fit()    # new parameters, new ranges: never start with the old zoom
        self.thread = SweepThread(self.state.inst, s, self, lock=self.state.io_lock)
        self.state.runner = self.thread.runner
        self.thread.point.connect(self._point)
        self.thread.cycle.connect(self.on_cycle)
        self.thread.warning.connect(self._warning)
        self.thread.result.connect(self._finished)
        self.state.set_busy(True)
        log.info("%s started: %s", type(self).__name__, s)
        self.thread.start()
        self.redraw_timer.start()
        self.refresh()

    def _warning(self, text: str):
        self.warnings.setVisible(True)
        self.warnings.appendPlainText(text)

    def stop(self):
        if self.thread is not None:
            self.progress.setText("Stopping, ramping magnet to 0...")
            self.thread.runner.stop()

    def _finished(self, result):
        self.thread.wait()
        self.thread = None
        self.redraw_timer.stop()
        self._redraw_if_dirty()
        self.state.runner = None
        self.state.set_busy(False)
        log.info("%s finished: %s", type(self).__name__, result)
        files = [p for p in (result.average_path, result.raw_path) if p]
        if result.measurement is not None:
            self.state.last_measurement = result.measurement
            self.state.measurement_ready.emit()
            self.open_btn.setEnabled(True)
            self.open_btn.setVisible(self.save is not None)
        self.saved.setText("Saved:\n" + "\n".join(Path(p).name for p in files) if files
                           else "Not saved to disk" + (" (open it in Analysis to save it)"
                                                       if result.measurement is not None else ""))
        self.on_finished(result)
        if not self.state.inst.all_connected:   # e.g. the DAQ dropped off USB during the run
            self.state.connection_changed.emit()
        self.refresh()
        if result.error:
            QMessageBox.critical(self, "Measurement stopped", result.error + "\n\nThe magnet was ramped to 0 V.")

    def on_start(self, s): ...
    def on_point(self, p): ...
    def update_plots(self, p): ...
    def on_cycle(self, cd, avg): ...
    def on_finished(self, result): ...

    def shutdown(self):
        if self.thread is not None:
            self.thread.runner.stop()
            self.thread.wait(30000)


class ContinuousTab(MeasureTab):
    """Live view for aligning and tuning: the photodetector signal as a strip chart (large) and, below,
    the waveform actually sent to the magnet. Nothing is saved; loops belong to the Hysteresis tab."""

    MAX_POINTS = 20000
    STABILITY_MAX_POINTS = 4000     # the test slows its sampling down so a long test fits in memory
    STABILITY_MIN_S = 10.0          # shorter (stopped early) tests give no meaningful drift

    def __init__(self, state, default_folder: Path):
        super().__init__(state, default_folder, save_option=False)
        box = QGroupBox("Display")
        f = QFormLayout(box)
        self.window = QDoubleSpinBox()
        self.window.setRange(5, 3600)
        self.window.setValue(30)
        self.window.setDecimals(0)
        self.window.setSuffix(" s")
        f.addRow("Time window", self.window)

        stab = QGroupBox("Laser stability test")
        sv = QVBoxLayout(stab)      # not a form: a multi-line result label must be able to grow
        sf = QFormLayout()
        sv.addLayout(sf)
        self.stab_minutes = QDoubleSpinBox()
        self.stab_minutes.setRange(0.5, 60)
        self.stab_minutes.setSingleStep(1)
        self.stab_minutes.setDecimals(1)
        self.stab_minutes.setValue(5)
        self.stab_minutes.setSuffix(" min")
        self.stab_btn = QPushButton("Run")
        self.stab_btn.setToolTip("Records the photodetector with the magnet at 0 V and reports drift and noise.\n"
                                 "Run it before measuring, e.g. after switching the laser on.")
        h = QHBoxLayout()
        h.addWidget(self.stab_minutes, 1)
        h.addWidget(self.stab_btn)
        sf.addRow("Duration", h)
        self.stab_result = QLabel("Magnet stays at 0 V.")
        self.stab_result.setObjectName("hint")
        self.stab_result.setWordWrap(True)
        self.stab_result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sv.addWidget(self.stab_result)
        self.stab_btn.clicked.connect(lambda: self.start_stability())
        self._stability_s = None        # test duration while a stability test runs
        self._stab_timer = QTimer(self)
        self._stab_timer.setSingleShot(True)
        self._stab_timer.timeout.connect(self.stop)

        root = QHBoxLayout(self)
        root.addWidget(self.controls_box(box, stab))
        self.signal_plot = make_plot("Photodetector signal", "", "Signal (V)")
        self.drive_plot = make_plot("Magnet drive", "Time (s, 0 = now)", "Magnet voltage (V)")
        self.drive_plot.setMinimumHeight(140)
        # at 0 V (stability test) the Kepco's ~10 mV offset and monitor noise would otherwise be
        # auto-scaled to fill the plot and look like a big deviation from the programmed 0 V
        self.drive_plot.getViewBox().setLimits(minYRange=DRIVE_MIN_SPAN_V)
        self.drive_plot.setXLink(self.signal_plot)
        self.drive_plot.addLegend(offset=(10, 5))
        root.addWidget(self.plots_area({self.signal_plot: "y", self.drive_plot: "y"}, Qt.Vertical, [700, 250]), 1)
        self.signal_curve = live_curve(self.signal_plot, symbols=False)
        self.set_curve = self.drive_plot.plot(pen=theme.pen("prev", 2, style=Qt.DashLine), name="programmed")
        self.meas_curve = self.drive_plot.plot(pen=theme.pen("avg", 1.5), name="measured (Kepco)")
        self._set_x_range()
        self.window.valueChanged.connect(lambda _: self._set_x_range())
        self.t, self.sig, self.vset, self.vmeas = [], [], [], []
        self.refresh()

    def _set_x_range(self):
        self.signal_plot.setXRange(-self.window.value(), 0, padding=0)

    def settings(self):
        return SweepSettings(**self.params.values(), cycles=None, average=False, folder=None)

    def refresh(self):
        super().refresh()
        if hasattr(self, "stab_btn"):
            self.stab_btn.setEnabled(self.start_btn.isEnabled())

    # ------------------------------------------------------------ stability test
    def start_stability(self, duration_s: float | None = None):
        """Magnet at 0 V, photodetector recorded for the chosen time, then drift and noise are reported."""
        duration = duration_s or self.stab_minutes.value() * 60
        per_point = self.params.read_time_s()
        settle_ms = max(0.0, (duration / self.STABILITY_MAX_POINTS - per_point) * 1000)
        s = SweepSettings("triangle", 0.0, 100, settle_ms, cycles=None, average=False, folder=None)
        self._stability_s = duration
        self.window.setValue(max(self.window.minimum(), min(self.window.maximum(), duration)))
        self.stab_result.setText("Running... keep the setup undisturbed.")
        log.info("Stability test started: %.1f min, %.0f ms between points", duration / 60, settle_ms)
        self.start(s)
        if self.thread is not None:
            self._stab_timer.start(int(duration * 1000))

    def _finish_stability(self):
        duration, self._stability_s = self._stability_s, None
        self._stab_timer.stop()
        if len(self.t) < 10 or self.t[-1] - self.t[0] < self.STABILITY_MIN_S:
            self.stab_result.setText(f"Stopped too early: run at least {self.STABILITY_MIN_S:.0f} s.")
            return
        r = stability_report(self.t, self.sig)
        cycle_s = self.params.cycle_time_s()
        per_cycle = r.drift_V_per_min / 60 * cycle_s
        partial = "" if r.duration_s >= 0.95 * duration else f" (stopped after {r.duration_s / 60:.1f} min)"
        self.stab_result.setText(
            f"<b>Drift {r.drift_V_per_min * 1e3:+.3f} mV/min ({r.drift_pct_per_min:+.3f} %/min)</b>{partial}<br>"
            f"Noise {r.noise_V * 1e3:.3f} mV rms · peak-to-peak {r.peak_to_peak_V * 1e3:.3f} mV<br>"
            f"Mean {r.mean_V:.5f} V. One {cycle_s:.0f} s cycle with these sweep settings would drift "
            f"≈ {per_cycle * 1e3:+.3f} mV: compare it with your Kerr step.")
        log.info("Stability test: %s", r.summary())

    def on_start(self, s):
        self.t, self.sig, self.vset, self.vmeas = [], [], [], []
        for c in (self.signal_curve, self.set_curve, self.meas_curve):
            c.setData([], [])

    def on_point(self, p):
        self.t.append(p.time_s)
        self.sig.append(p.signal)
        self.vset.append(p.v_programmed)
        self.vmeas.append(p.v_kepco)
        if len(self.t) > self.MAX_POINTS:    # keep only what fits in the largest selectable window
            for a in (self.t, self.sig, self.vset, self.vmeas):
                del a[:5000]

    def update_plots(self, p):
        # strip chart: newest point at 0 on the right, older points move left at constant speed
        t = np.asarray(self.t) - self.t[-1]
        keep = t >= -self.window.value()
        t = t[keep]
        self.signal_curve.setData(t, np.asarray(self.sig)[keep])
        self.set_curve.setData(t, np.asarray(self.vset)[keep])
        self.meas_curve.setData(t, np.asarray(self.vmeas)[keep])
        if self.autoscale.on:
            self._set_x_range()
        if self._stability_s is not None:
            el = self.t[-1]
            self.progress.setText(f"Stability test {el // 60:.0f}:{el % 60:02.0f} / "
                                  f"{self._stability_s // 60:.0f}:{self._stability_s % 60:02.0f}   "
                                  f"signal = {p.signal:+.5f} V")
            return
        self.progress.setText(f"Cycle {p.cycle + 1}, point {p.index + 1}   V = {p.v_kepco:+.2f} V   "
                              f"I = {p.current:+.3f} A   signal = {p.signal:+.5f} V")

    def on_finished(self, result):
        self.progress.setText("Stopped" if not result.error else "Stopped by error")
        if self._stability_s is not None:
            if result.error:
                self._stability_s = None
                self._stab_timer.stop()
                self.stab_result.setText("Stopped by an error, see the message.")
            else:
                self._finish_stability()


class HysteresisTab(MeasureTab):
    def __init__(self, state, default_folder: Path):
        super().__init__(state, default_folder)
        box = QGroupBox("Cycles")
        f = QFormLayout(box)
        self.cycles = QSpinBox()
        self.cycles.setRange(1, 10000)
        self.cycles.setValue(state.cfg.defaults.cycles)
        self.average = QCheckBox("Average")
        self.average.setChecked(True)
        self.drift = DriftCombo()
        h = QHBoxLayout()
        h.addWidget(self.cycles, 1)
        h.addSpacing(10)
        h.addWidget(self.average)
        f.addRow("Cycles", h)
        f.addRow("Drift", self.drift)

        root = QHBoxLayout(self)
        root.addWidget(self.controls_box(box))
        self.cycle_plot = make_plot("Current cycle", "", "Signal (V)")
        self.avg_plot = make_plot("Average", "", "Signal (V)")
        self.xaxis = XAxisToggle(state)
        root.addWidget(self.plots_area({self.cycle_plot: "xy", self.avg_plot: "xy"}, extra=self.xaxis), 1)
        self.prev_curve = self.cycle_plot.plot(pen=theme.pen("prev", 1))
        self.live_curve = live_curve(self.cycle_plot)
        self.avg_curve = self.avg_plot.plot(pen=theme.pen("avg"), symbol="o", symbolSize=4,
                                            symbolBrush=theme.color("avg"), symbolPen=None)
        self.average.toggled.connect(self.avg_plot.setVisible)
        # data is kept as measured (currents, and Hall field if a probe is wired); the X switch converts
        # only for display
        self.live, self.prev, self.avg = self._empty(), self._empty(), self._empty()
        self.n_cycles = 0
        state.x_mode_changed.connect(lambda _: self._x_mode_changed())
        state.calibration_changed.connect(self._x_mode_changed)
        self._set_x_labels()
        self.refresh()

    @staticmethod
    def _empty() -> dict:
        """One curve as measured: measured current, programmed current, Hall field, signal."""
        return {"im": [], "iset": [], "h": [], "y": []}

    def _to_x(self, d: dict):
        """X values to plot: measured or programmed current, or field in mT/Oe (Hall probe if measured,
        else the calibration, from the kind of current it was made with)."""
        im = np.asarray(d["im"], dtype=float)
        iset = np.asarray(d["iset"], dtype=float)
        mode = self.xaxis.mode
        if mode == "current":
            return im
        if mode == "current_set":
            return iset
        h = d["h"]
        if h is not None and len(h) == len(iset) and all(v is not None for v in h):
            b = np.asarray(h, dtype=float)
        elif self.state.calibration is not None:
            b = self.state.calibration.field_for(im if self.state.inst.monitors else None, iset)
        else:
            b = np.full(len(iset), np.nan)
        return to_x(mode, b)

    def _show(self, curve, d: dict):
        curve.setData(self._to_x(d), np.asarray(d["y"], dtype=float))

    def _set_x_labels(self):
        for p in (self.cycle_plot, self.avg_plot):
            p.setLabel("bottom", x_label(self.xaxis.mode))

    def _x_mode_changed(self):
        self._set_x_labels()
        self._show(self.prev_curve, self.prev)
        self._show(self.live_curve, self.live)
        self._show(self.avg_curve, self.avg)
        self.autoscale.fit()    # amperes and millitesla have nothing in common: re-fit

    def settings(self):
        sample, folder = self.save.target()
        return SweepSettings(**self.params.values(), cycles=self.cycles.value(),
                             average=self.average.isChecked(), drift_correction=self.drift.method,
                             sample=sample, folder=folder)

    def on_start(self, s):
        self.n_cycles = s.cycles
        self.live, self.prev, self.avg = self._empty(), self._empty(), self._empty()
        for c in (self.prev_curve, self.live_curve, self.avg_curve):
            c.setData([], [])

    def on_point(self, p):
        if p.index == 0 and self.live["y"]:
            self.prev = self.live
            self._show(self.prev_curve, self.prev)
            self.live = self._empty()
        self.live["im"].append(p.current)
        self.live["iset"].append(p.i_set)
        self.live["h"].append(p.hall_mT)
        self.live["y"].append(p.signal)

    def update_plots(self, p):
        self._show(self.live_curve, self.live)
        cal = self.state.calibration
        if p.hall_mT is not None:
            b = f"  B = {p.hall_mT:+.2f} mT (Hall)"
        elif cal is not None:
            b = f"  B = {float(cal.field_for(p.current if self.state.inst.monitors else None, p.i_set)):+.2f} mT"
        else:
            b = ""
        self.progress.setText(f"Cycle {p.cycle + 1}/{self.n_cycles}, point {p.index + 1}/"
                              f"{self.params.points.value()}   I = {p.x_current:+.3f} A{b}")

    def on_cycle(self, cd, avg):
        if avg is not None:
            self.avg = {"im": avg.current, "iset": avg.i_set, "h": avg.hall_mT, "y": avg.signal}
            self._show(self.avg_curve, self.avg)
            self.avg_plot.setTitle(f"Average of {cd.cycle + 1} cycle(s)")
            self.autoscale.apply()

    def on_finished(self, result):
        status = "Stopped" if result.stopped else ("Stopped by error" if result.error else "Finished")
        self.progress.setText(f"{status}: {result.completed_cycles} complete cycle(s)")
