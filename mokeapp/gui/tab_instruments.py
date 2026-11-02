"""Instruments tab: setup, connection status, preflight diagnostics, multimeter settings, manual magnet
control."""
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QScrollArea, QSpinBox,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from mokeapp.core.diagnostics import run_diagnostics
from mokeapp.gui.setup_dialog import SetupDialog
from mokeapp.gui.state import FuncThread
from mokeapp.gui.widgets import COLORS, StatusLight

log = logging.getLogger(__name__)

RANGES = ["0.1", "1", "10", "100", "1000", "auto"]


def instrument_labels(cfg) -> list[tuple[str, str]]:
    """Rows of the Instruments box, described from the setup."""
    d = cfg.daq
    inputs = (f", monitors {d.ai_current} / {d.ai_voltage}" if d.monitors else "") + \
             (f", Hall {d.ai_hall}" if d.hall_enabled else "")
    return [("keithley", f"Multimeter ({cfg.keithley.address})"),
            ("daq", f"NI DAQ {d.device} ({d.ao_channel}{inputs})"),
            ("kepco", f"Kepco {cfg.kepco.model} + magnet" + (" (monitors)" if d.monitors else " (no monitors)"))]


class InstrumentsTab(QWidget):
    setup_saved = Signal(object)     # Path of the setup file the user just saved

    def __init__(self, state, config_dir: Path | None = None):
        super().__init__()
        self.state = state
        self.config_dir = config_dir or Path(state.cfg.path or "config").parent
        self._thread = None
        root = QHBoxLayout(self)
        left = QVBoxLayout()
        root.addLayout(left, 3)
        right_widget = QWidget()
        right = QVBoxLayout(right_widget)
        right.setContentsMargins(0, 0, 6, 0)
        scroll = QScrollArea()             # scrolls instead of overflowing on small screens
        scroll.setWidget(right_widget)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setMinimumWidth(380)
        root.addWidget(scroll, 2)

        # --- connection
        box = QGroupBox("Instruments")
        g = QGridLayout(box)
        self.lights, self.infos = {}, {}
        labels = instrument_labels(state.cfg)
        for row, (key, label) in enumerate(labels):
            self.lights[key] = StatusLight()
            self.infos[key] = QLabel("not connected")
            self.infos[key].setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.infos[key].setWordWrap(True)
            name = QLabel(f"<b>{label}</b>")
            name.setWordWrap(True)
            g.addWidget(self.lights[key], row, 0)
            g.addWidget(name, row, 1)
            g.addWidget(self.infos[key], row, 2)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(2, 1)
        cfg_path = Path(state.cfg.path).name if state.cfg.path and Path(state.cfg.path).exists() else "not saved"
        g.addWidget(QLabel(f"Setup: <b>{state.cfg.name}</b> ({cfg_path})"
                           + ("  —  <b style='color:#b23b3b'>SIMULATION</b>" if state.simulate else "")),
                    len(labels), 0, 1, 3)
        buttons = QHBoxLayout()
        self.connect_btn = QPushButton("Connect")
        self.disconnect_btn = QPushButton("Disconnect")
        self.setup_btn = QPushButton("Setup...")
        self.setup_btn.setToolTip("Choose the multimeter, the NI DAQ and its channels, the Kepco limits and "
                                  "whether the Kepco monitors are wired. Disconnect first.")
        buttons.addWidget(self.connect_btn)
        buttons.addWidget(self.disconnect_btn)
        buttons.addStretch()
        buttons.addWidget(self.setup_btn)
        g.addLayout(buttons, len(labels) + 1, 0, 1, 3)
        left.addWidget(box)

        # --- diagnostics
        box = QGroupBox("Preflight diagnostics")
        v = QVBoxLayout(box)
        self.diag_btn = QPushButton("Run diagnostics")
        self.diag_summary = QLabel("Not run yet. Measuring is enabled after diagnostics pass.")
        self.diag_summary.setWordWrap(True)
        h = QHBoxLayout()
        h.addWidget(self.diag_btn)
        h.addWidget(self.diag_summary, 1)
        v.addLayout(h)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Check", "Result", "Details"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        v.addWidget(self.table)
        self.override = QCheckBox("Allow measuring without passing diagnostics (not recommended)")
        v.addWidget(self.override)
        left.addWidget(box, 1)

        # --- live readout
        box = QGroupBox("Live readout")
        f = QFormLayout(box)
        self.live_v = QLabel("-")
        self.live_i = QLabel("-")
        self.live_r = QLabel("-")
        self.live_s = QLabel("-")
        self.live_b = QLabel("-")
        for lbl in (self.live_v, self.live_i, self.live_r, self.live_s, self.live_b):
            lbl.setStyleSheet("font-size:15px; font-family:Consolas;")
        f.addRow("Magnet voltage", self.live_v)
        f.addRow("Magnet current", self.live_i)
        f.addRow("Magnet resistance", self.live_r)
        f.addRow("Photodetector", self.live_s)
        if state.inst.hall_enabled:     # only with a Hall probe wired to the DAQ (config daq.ai_hall)
            f.addRow("Hall probe field", self.live_b)
        right.addWidget(box)

        # --- manual magnet
        box = QGroupBox("Manual magnet control")
        f = QFormLayout(box)
        self.manual_v = QDoubleSpinBox()
        self.manual_v.setDecimals(2)
        self.manual_v.setSingleStep(0.5)
        self.manual_v.setSuffix(" V")
        self.manual_max = QLabel()
        self.manual_max.setStyleSheet("color:#b23b3b;")
        h = QHBoxLayout()
        h.addWidget(self.manual_v)
        self.set_btn = QPushButton("Set")
        h.addWidget(self.set_btn)
        f.addRow("Voltage", h)
        # programmed current = V / nominal R: what to type in the Calibration tab without Kepco monitors
        self.manual_i = QDoubleSpinBox()
        self.manual_i.setDecimals(3)
        self.manual_i.setSingleStep(0.1)
        self.manual_i.setSuffix(" A")
        self.manual_i.setToolTip("Sends V = I × nominal magnet R (setup). This is the 'programmed current'.")
        h = QHBoxLayout()
        h.addWidget(self.manual_i)
        self.set_i_btn = QPushButton("Set")
        h.addWidget(self.set_i_btn)
        f.addRow("Current", h)
        h = QHBoxLayout()
        h.addWidget(self.manual_max, 1)
        self.zero_btn = QPushButton("Zero")
        h.addWidget(self.zero_btn)
        f.addRow("", h)
        right.addWidget(box)

        # --- Keithley settings
        box = QGroupBox("Multimeter settings")
        f = QFormLayout(box)
        k = state.cfg.keithley
        self.k_range = QComboBox()
        self.k_range.addItems(RANGES)
        self.k_range.setCurrentText("auto" if k.range_V == "auto" else f"{float(k.range_V):g}")
        self.k_nplc = QDoubleSpinBox()
        self.k_nplc.setRange(0.01, 10)
        self.k_nplc.setValue(k.nplc)
        self.k_filter = QComboBox()
        self.k_filter.addItems(["off", "repeat"])
        self.k_filter.setCurrentText(k.filter)
        self.k_count = QSpinBox()
        self.k_count.setRange(1, 100)
        self.k_count.setValue(k.filter_count)
        self.k_apply = QPushButton("Apply")
        f.addRow("Range (V)", self.k_range)
        f.addRow("NPLC", self.k_nplc)
        f.addRow("Filter", self.k_filter)
        f.addRow("Filter count", self.k_count)
        f.addRow("", self.k_apply)
        right.addWidget(box)
        right.addStretch()

        self.connect_btn.clicked.connect(self.do_connect)
        self.disconnect_btn.clicked.connect(self.do_disconnect)
        self.setup_btn.clicked.connect(self.open_setup)
        self.diag_btn.clicked.connect(self.do_diagnostics)
        self.override.toggled.connect(self._override_toggled)
        self.set_btn.clicked.connect(self.do_set)
        self.set_i_btn.clicked.connect(self.do_set_current)
        self.zero_btn.clicked.connect(self.do_zero)
        self.k_apply.clicked.connect(self.do_keithley_apply)
        state.busy_changed.connect(lambda _: self._refresh_buttons())
        state.limits_changed.connect(self._update_limits)
        state.connection_changed.connect(self._update_status)
        state.connection_changed.connect(self._refresh_buttons)

        self._poller = None
        self._poll_failing = False
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._poll)
        self._update_limits()
        self._refresh_buttons()

    # ----------------------------------------------------------- helpers
    def _update_limits(self):
        vmax = self.state.max_voltage
        self.manual_v.setRange(-vmax, vmax)
        i_max = vmax / self.state.cfg.kepco.magnet_resistance_ohm
        self.manual_i.setRange(-i_max, i_max)
        self.manual_max.setText(f"max ±{vmax:.1f} V ({vmax / self.state.resistance:.1f} A)")

    def _refresh_buttons(self):
        s = self.state
        connected = s.inst.all_connected
        any_connected = s.inst.keithley.connected or s.inst.daq.connected
        idle = not s.busy
        self.connect_btn.setEnabled(idle and not connected)
        self.disconnect_btn.setEnabled(idle and any_connected)
        self.setup_btn.setEnabled(idle and not any_connected)
        self.diag_btn.setEnabled(idle and any_connected)
        for w in (self.set_btn, self.zero_btn, self.manual_v, self.manual_i, self.set_i_btn):
            w.setEnabled(idle and s.inst.daq.connected)
        self.k_apply.setEnabled(idle and s.inst.keithley.connected)
        has_filter = getattr(s.inst.keithley, "profile", None) is None or s.inst.keithley.profile.has_filter
        for w in (self.k_filter, self.k_count):
            w.setEnabled(has_filter)
            w.setToolTip("" if has_filter else "This multimeter model has no repeat filter")

    def open_setup(self) -> SetupDialog:
        dlg = SetupDialog(self.state.cfg, self.state.simulate, self.config_dir, self)
        if dlg.exec() and dlg.saved_path is not None:
            self.setup_saved.emit(dlg.saved_path)
        return dlg

    def _update_status(self):
        st = self.state.inst.status
        for key in self.lights:
            ok, msg = st[key]
            self.lights[key].set_state("ok" if ok else ("fail" if msg not in ("not connected", "needs DAQ")
                                                        else "off"))
            self.infos[key].setText(msg)

    def _run(self, fn, done, busy_text):
        self.state.set_busy(True)
        if busy_text:
            self.diag_summary.setText(busy_text)
        self._thread = FuncThread(fn, self, lock=self.state.io_lock)
        self._thread.done.connect(lambda r, e: (self.state.set_busy(False), done(r, e)))
        self._thread.start()

    # ----------------------------------------------------------- actions
    def do_connect(self):
        self.timer.stop()

        def done(_, err):
            self._update_status()
            self.state.limits_changed.emit()     # the DAQ's AO range can lower the max magnet voltage
            self.state.connection_changed.emit()
            self._refresh_buttons()
            if self.state.inst.daq.connected or self.state.inst.keithley.connected:
                self.timer.start()
            if err:
                QMessageBox.critical(self, "Connect", str(err))
        for key in self.lights:
            self.lights[key].set_state("warn")
        self._run(self.state.inst.connect, done, "")

    def do_disconnect(self):
        self.timer.stop()
        if self._poller is not None:
            self._poller.wait(10000)
        self.state.inst.close()
        self.state.diagnostics = None
        self.state.diagnostics_changed.emit()
        self._update_status()
        self.state.connection_changed.emit()
        self.diag_summary.setText("Not run yet. Measuring is enabled after diagnostics pass.")
        self.table.setRowCount(0)
        for lbl in (self.live_v, self.live_i, self.live_r, self.live_s, self.live_b):
            lbl.setText("-")
        self._refresh_buttons()

    def do_diagnostics(self):
        def done(report, err):
            if err:
                QMessageBox.critical(self, "Diagnostics", str(err))
                return
            self.state.diagnostics = report
            if report.resistance_ohm:
                self.state.set_resistance(report.resistance_ohm)
            self.table.setRowCount(len(report.checks))
            for row, c in enumerate(report.checks):
                self.table.setItem(row, 0, QTableWidgetItem(c.name))
                item = QTableWidgetItem(c.status.upper())
                item.setForeground(Qt.white)
                item.setBackground(QColor(COLORS[c.status]))
                self.table.setItem(row, 1, item)
                self.table.setItem(row, 2, QTableWidgetItem(c.message))
            self.table.resizeColumnsToContents()
            self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
            n_warn = sum(c.status == "warn" for c in report.checks)
            if report.passed:
                self.diag_summary.setText(f"<b style='color:{COLORS['pass']}'>PASSED</b>"
                                          + (f" with {n_warn} warning(s)" if n_warn else "")
                                          + f". Magnet R = {self.state.resistance:.3f} Ω.")
            else:
                self.diag_summary.setText(f"<b style='color:{COLORS['fail']}'>FAILED</b>. "
                                          "Fix the failed checks before measuring.")
            self.state.diagnostics_changed.emit()
        self.timer.stop()
        self._run(lambda: run_diagnostics(self.state.inst), lambda r, e: (done(r, e), self.timer.start()),
                  "Running diagnostics (the magnet gets a ±1 V test pulse)...")

    def _override_toggled(self, on):
        if on:
            ans = QMessageBox.warning(self, "Override", "Measure without passing diagnostics?\n"
                                      "Safety checks during the measurement still apply.",
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ans != QMessageBox.Yes:
                self.override.setChecked(False)
                return
        self.state.override = on
        self.state.diagnostics_changed.emit()

    def do_set(self):
        try:
            v = self.state.inst.kepco.set_voltage(self.manual_v.value())
            log.info("Manual magnet voltage %.3f V", v)
            self._poll()
        except Exception as e:
            self.state.inst.safe_zero()
            QMessageBox.critical(self, "Set voltage", str(e))

    def do_set_current(self):
        i = self.manual_i.value()
        v = i * self.state.cfg.kepco.magnet_resistance_ohm
        try:
            v = self.state.inst.kepco.set_voltage(v)
            self.manual_v.setValue(v)
            log.info("Manual magnet current %.3f A (programmed) = %.3f V", i, v)
            self._poll()
        except Exception as e:
            self.state.inst.safe_zero()
            QMessageBox.critical(self, "Set current", str(e))

    def do_zero(self):
        self.state.inst.safe_zero()
        self.manual_v.setValue(0.0)
        self.manual_i.setValue(0.0)
        self._poll()

    def do_keithley_apply(self):
        rng = self.k_range.currentText()
        keithley = self.state.inst.keithley
        args = ("auto" if rng == "auto" else float(rng), self.k_nplc.value(), self.k_filter.currentText(),
                self.k_count.value())

        def apply():
            keithley.configure(*args)
            return keithley.errors()

        def done(errs, err):
            if err:
                QMessageBox.critical(self, "Keithley", str(err))
            elif errs:
                QMessageBox.warning(self, "Keithley", "Errors: " + "; ".join(errs))
            else:
                log.info("Keithley settings: range %s, NPLC %g, filter %s x%d (~%.2f s per reading)",
                         *args, keithley.read_time_s)
            self.state.limits_changed.emit()    # time-per-cycle estimates depend on the reading time
        self._run(apply, done, "")

    def _poll(self):
        """Live readout in a background thread: a slow Keithley setting (NPLC 10 + filter = seconds per
        reading) must never block the window."""
        s = self.state
        if s.busy or not self.isVisible() or (self._poller is not None and self._poller.isRunning()):
            return
        inst = s.inst

        def read():
            if not s.io_lock.acquire(blocking=False):
                return None     # a sweep or diagnostics owns the instruments
            try:
                capture = inst.daq.connected and (inst.monitors or inst.hall_enabled)
                vi = inst.kepco.read() if capture else None
                hall = inst.last_hall_mT() if vi is not None else None
                sig = inst.keithley.read() if inst.keithley.connected else None
                return vi, sig, hall, None
            except Exception as e:
                return None, None, None, e
            finally:
                s.io_lock.release()

        self._poller = FuncThread(read, self)
        self._poller.done.connect(lambda values, _: self._show_poll(values))
        self._poller.start()

    def _show_poll(self, values):
        if values is None:
            return
        vi, sig, hall, err = values
        if err is not None:
            # keep polling: the Keithley driver clears itself after a timeout, so the next read usually works.
            # Log only the first failure of a series, not one traceback per second.
            if not self._poll_failing:
                log.error("Live readout failed: %s: %s", type(err).__name__, err)
            self._poll_failing = True
            self.live_s.setText(f"readout error: {type(err).__name__}")
            return
        self._poll_failing = False
        inst = self.state.inst
        if vi is not None and inst.monitors:
            v, i = vi
            self.live_v.setText(f"{v:+8.3f} V")
            self.live_i.setText(f"{i:+8.3f} A")
            self.live_r.setText(f"{v / i:8.3f} Ω" if abs(i) > 0.1 else "-")
        elif inst.daq.connected:    # no monitors: show what is programmed
            v = inst.kepco.output_V
            self.live_v.setText(f"{v:+8.3f} V (set)")
            self.live_i.setText(f"{inst.kepco.programmed_current(v):+8.3f} A (set)")
            self.live_r.setText(f"{inst.cfg.kepco.magnet_resistance_ohm:8.3f} Ω (nominal)")
        if sig is not None:
            self.live_s.setText(f"{sig:+9.5f} V")
        if hall is not None:
            self.live_b.setText(f"{hall:+8.3f} mT")

    def shutdown(self):
        self.timer.stop()
        for t in (self._thread, self._poller):
            if t is not None:
                t.wait(10000)
