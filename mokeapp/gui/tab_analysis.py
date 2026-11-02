"""Analysis tab: load a CSV or the last (even unsaved) measurement, explore the plot, compute Hc (and
optionally Mr/Ms) on demand, and save the processed curve or the unsaved raw data.

The X axis follows the shared Current (A) / Field (mT) switch. Field comes from the file's B_mT column
(the calibration used when measuring) or, for files without one, from the active calibration.
"""
import logging
from datetime import datetime
from pathlib import Path

import numpy as np
import pyqtgraph as pg
import pyqtgraph.exporters
import yaml
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from mokeapp.core import storage
from mokeapp.core.processing import (analyze_loop, average_cycles, correct_drift, drift_fit_plateaus, normalize,
                                     remove_offset, symmetrize)
from mokeapp.gui import theme
from mokeapp.core.calibration import MT_TO_OE
from mokeapp.gui.widgets import AutoScale, DriftCombo, XAxisToggle, make_plot, to_x, x_label

log = logging.getLogger(__name__)

ALL_CYCLES = "Average of all cycles"
X_FROM_SWITCH = "Current / field (X axis switch)"


class AnalysisTab(QWidget):
    def __init__(self, state, default_folder: Path):
        super().__init__()
        self.state = state
        self.default_folder = default_folder
        self.m: storage.Measurement | None = None
        self.hc_lines = []

        root = QHBoxLayout(self)
        left = QVBoxLayout()
        panel = QWidget()
        panel.setLayout(left)
        left.setContentsMargins(0, 0, 6, 0)
        scroll = QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setFixedWidth(400)
        root.addWidget(scroll)

        h = QHBoxLayout()
        open_btn = QPushButton("Open CSV...")
        self.last_btn = QPushButton("Open last measurement")
        h.addWidget(open_btn)
        h.addWidget(self.last_btn)
        left.addLayout(h)
        self.file_label = QLabel("No file loaded")
        self.file_label.setWordWrap(True)
        left.addWidget(self.file_label)

        box = QGroupBox("Data")
        f = QFormLayout(box)
        self.x_combo = QComboBox()
        self.y_combo = QComboBox()
        self.cycle_combo = QComboBox()
        f.addRow("X", self.x_combo)
        f.addRow("Y", self.y_combo)
        f.addRow("Cycle", self.cycle_combo)
        self.drift = DriftCombo()
        f.addRow("Drift", self.drift)
        self.drift_info = QLabel("")
        self.drift_info.setObjectName("hint")
        self.drift_info.setWordWrap(True)
        f.addRow("", self.drift_info)
        self.offset = QCheckBox("Remove offset")
        self.norm = QCheckBox("Normalize to ±1")
        self.sym = QCheckBox("Odd part (symmetrize)")
        self.sym.setToolTip(
            "Keeps the part of the signal that reverses with the field: odd(H) = [S_up(H) − S_down(−H)] / 2.\n"
            "Removes the offset, quadratic Kerr (∝ M²) and artifacts that do not change sign with the field.\n"
            "The loop becomes centred by construction: a real exchange-bias shift is removed too.\n"
            "Needs a full loop swept symmetrically (±same amplitude).")
        self.show_even = QCheckBox("Even part")
        self.show_even.setToolTip(
            "Draws what the symmetrization removed, [S_up(H) + S_down(−H)] / 2, mean removed (dashed grey).\n"
            "Flat = no even contribution. Parabola over the whole range = optics moved by the magnet (∝ H²).\n"
            "Bump or dip at the switching field although Hc+ = |Hc−| = quadratic Kerr.\n"
            "Rectangular pulse as wide as | |Hc+| − |Hc−| | = the two branches switch at different fields.")
        self.show_even.setEnabled(False)
        for w in (self.offset, self.norm, self.sym, self.show_even):
            f.addRow("", w)
        left.addWidget(box)

        box = QGroupBox("Coercive field")
        v = QVBoxLayout(box)
        self.hc_btn = QPushButton("Compute Hc")
        self.mr = QCheckBox("Also show Mr/Ms (squareness)")
        self.result = QLabel("")
        self.result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result.setStyleSheet("font-family:Consolas;")
        v.addWidget(self.hc_btn)
        v.addWidget(self.mr)
        v.addWidget(self.result)
        left.addWidget(box)

        box = QGroupBox("Save")
        v = QVBoxLayout(box)
        self.save_curve_btn = QPushButton("Save shown curve as CSV...")
        self.save_curve_btn.setToolTip("The curve as plotted: selected cycle(s) averaged, with the drift / "
                                       "offset / normalization options applied. Current and field columns "
                                       "are both saved; the options go in the header.")
        self.save_raw_btn = QPushButton("Save raw data (all cycles)...")
        self.save_raw_btn.setToolTip("Every point of the measurement, unmodified")
        self.save_status = QLabel("")
        self.save_status.setWordWrap(True)
        self.save_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.save_curve_btn)
        v.addWidget(self.save_raw_btn)
        v.addWidget(self.save_status)
        left.addWidget(box)

        export_btn = QPushButton("Export PNG...")
        left.addWidget(export_btn)
        self.meta = QPlainTextEdit()
        self.meta.setReadOnly(True)
        self.meta.setPlaceholderText("File metadata")
        self.meta.setMinimumHeight(120)
        left.addWidget(self.meta, 1)

        right = QVBoxLayout()
        self.plot = make_plot("", "Magnet current (A)", "Signal (V)")
        self.curve = self.plot.plot(pen=theme.pen("live", 1.5), symbol="o", symbolSize=4,
                                    symbolBrush=theme.color("live"), symbolPen=None)
        # even part of the symmetrization (mean removed), drawn under the loop for diagnosis
        self.even_curve = self.plot.plot(pen=theme.pen("cursor", 1.5, style=Qt.DashLine))
        self.autoscale = AutoScale({self.plot: "xy"})
        self.xaxis = XAxisToggle(state, self._field_available, self._measured_available,
                                 self._programmed_available)
        bar = QHBoxLayout()
        bar.addWidget(self.autoscale, 1)
        bar.addWidget(self.xaxis)
        self.x_note = QLabel("")
        self.x_note.setObjectName("hint")
        self.x_note.setWordWrap(True)
        self.cursor = QLabel(" ")
        self.cursor.setStyleSheet("font-family:Consolas;")
        right.addLayout(bar)
        right.addWidget(self.x_note)
        right.addWidget(self.plot, 1)
        right.addWidget(self.cursor)
        root.addLayout(right, 1)
        self.vline = pg.InfiniteLine(angle=90, movable=False, pen=theme.pen("cursor", 1, style=Qt.DashLine))
        self.hline = pg.InfiniteLine(angle=0, movable=False, pen=theme.pen("cursor", 1, style=Qt.DashLine))
        self.plot.addItem(self.vline, ignoreBounds=True)
        self.plot.addItem(self.hline, ignoreBounds=True)
        self.plot.scene().sigMouseMoved.connect(self._mouse_moved)

        open_btn.clicked.connect(self._open_dialog)
        self.last_btn.clicked.connect(lambda: self.load(state.last_measurement))
        export_btn.clicked.connect(self._export)
        self.save_curve_btn.clicked.connect(self.save_curve)
        self.save_raw_btn.clicked.connect(self.save_raw)
        self.hc_btn.clicked.connect(self.compute)
        for w in (self.x_combo, self.y_combo, self.cycle_combo):
            w.currentIndexChanged.connect(self.redraw)
        self.drift.currentIndexChanged.connect(self.redraw)
        for w in (self.offset, self.norm, self.sym, self.show_even):
            w.toggled.connect(self.redraw)
        self.sym.toggled.connect(self.show_even.setEnabled)
        self.mr.toggled.connect(lambda _: self.result.text() and self.compute())
        state.x_mode_changed.connect(lambda _: self._x_changed())
        state.calibration_changed.connect(self._x_changed)
        state.measurement_ready.connect(lambda: self.last_btn.setEnabled(True))
        self.last_btn.setEnabled(False)
        self._update_save_buttons()

    # -------------------------------------------------------------- loading
    def _open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open measurement", str(self.default_folder), "CSV (*.csv)")
        if path:
            self.load(path)

    def load(self, source):
        """source: a CSV path, or a storage.Measurement (the last run, possibly never saved)."""
        if source is None or source == "":
            return
        if isinstance(source, storage.Measurement):
            m = source
        else:
            try:
                m = storage.load_measurement(source)
            except Exception as e:
                QMessageBox.critical(self, "Open", f"Could not read {source}:\n{e}")
                return
        if len(m.data) == 0:
            QMessageBox.warning(self, "Open", "The file has no data rows.")
            return
        self.m = m
        self.file_label.setText(f"<b>{m.path.name}</b>" if m.path is not None
                                else "<b>Last measurement</b> — <span style='color:#b45309'>not saved yet</span>")
        self.save_status.setText("")
        self._update_save_buttons()
        self.meta.setPlainText(yaml.safe_dump({k: v for k, v in m.metadata.items() if k != "config"},
                                              sort_keys=False, allow_unicode=True))
        # X: the current/field switch by default; any other column can still be chosen
        x_cols = {m.x_column, m.field_column, storage.COL_CURRENT, storage.COL_ISET, *storage.FIELD_COLUMNS}
        others = [c for c in m.columns if c not in x_cols]
        self.x_combo.blockSignals(True)
        self.x_combo.clear()
        if m.x_column:
            self.x_combo.addItem(X_FROM_SWITCH)
        self.x_combo.addItems(others)
        self.x_combo.blockSignals(False)
        self.y_combo.blockSignals(True)
        self.y_combo.clear()
        self.y_combo.addItems(m.columns)
        if m.y_column:
            self.y_combo.setCurrentText(m.y_column)
        self.y_combo.blockSignals(False)
        self.cycle_combo.blockSignals(True)
        self.cycle_combo.clear()
        if m.has(storage.COL_CYCLE):
            self.cycle_combo.addItem(ALL_CYCLES)
            self.cycle_combo.addItems([str(int(c)) for c in np.unique(m.column(storage.COL_CYCLE))])
            self.cycle_combo.setEnabled(True)
            # start from what the run used, so the curve matches the live 'Average' plot
            self.drift.blockSignals(True)
            self.drift.set_method(m.metadata.get("drift_correction", "off"))
            self.drift.blockSignals(False)
        else:
            self.cycle_combo.addItem("single (averaged file)")
            self.cycle_combo.setEnabled(False)
        self.cycle_combo.blockSignals(False)
        self.result.setText("")
        self.xaxis.refresh()
        self.redraw()
        self.autoscale.fit()

    # -------------------------------------------------------------- X axis
    def _field_available(self) -> bool:
        if self.m is None:
            return self.state.field_available
        return self.m.field_column is not None or (self.m.x_column is not None
                                                   and self.state.calibration is not None)

    def _measured_available(self) -> bool:
        return self.state.inst.monitors if self.m is None else self.m.has(storage.COL_CURRENT)

    def _programmed_available(self) -> bool:
        return True if self.m is None else self.m.programmed_current() is not None

    def _x_from_switch(self) -> bool:
        return self.x_combo.currentText() == X_FROM_SWITCH

    def _x_changed(self):
        self.xaxis.refresh()
        self.redraw()
        self.autoscale.fit()    # amperes and millitesla have nothing in common: re-fit

    def _x_unit(self) -> str:
        if self._x_from_switch():
            return "A" if self.xaxis.mode in ("current", "current_set") else self.xaxis.mode
        return self.x_combo.currentText().replace(storage.X_TAG, "").split("_")[-1]

    # -------------------------------------------------------------- data
    def _columns(self) -> dict[str, np.ndarray]:
        """Raw arrays by role: I (measured current), Iset (programmed current), B (field: file column or
        active calibration), x (other column), y, vp."""
        m = self.m
        out = {"y": m.column(self.y_combo.currentText())}
        if m.has(storage.COL_CURRENT):
            out["I"] = m.column(storage.COL_CURRENT)
        iset = m.programmed_current()
        if iset is not None:
            out["Iset"] = iset
        elif m.x_column and "I" not in out:     # files from other tools: their [X] column
            out["I"] = m.column(m.x_column)
        self._b_source = None   # "hall" (measured), "file" (calibrated when measured) or "active" (now)
        if m.field_column:
            out["B"] = m.column(m.field_column)
            self._b_source = "hall" if m.field_column == storage.COL_HALL else "file"
        elif ("I" in out or "Iset" in out) and self.state.calibration is not None:
            out["B"] = self.state.calibration.field_for(out.get("I"), out.get("Iset", out.get("I")))
            self._b_source = "active"
        if not self._x_from_switch():
            out["x"] = m.column(self.x_combo.currentText())
        if m.has(storage.COL_VPROG):
            out["vp"] = m.column(storage.COL_VPROG)
        return out

    def series_all(self) -> dict[str, np.ndarray]:
        """Every role after the cycle selection and the chosen corrections ('x' = what is plotted)."""
        cols = self._columns()
        m = self.m
        method = self.drift.method
        sweep = next((cols[k] for k in ("I", "Iset", "vp", "x") if k in cols), None)   # for the plateaus
        if sweep is None:
            sweep = np.arange(len(cols["y"]), dtype=float)
        times = m.column(storage.COL_TIME) if m.has(storage.COL_TIME) else None
        if m.has(storage.COL_CYCLE):
            cyc = m.column(storage.COL_CYCLE)
            choice = self.cycle_combo.currentText()
            groups = [cyc == c for c in np.unique(cyc)] if choice == ALL_CYCLES else [cyc == float(choice)]
            n = max(g.sum() for g in groups)
            groups = [g for g in groups if g.sum() == n]   # complete cycles only
        else:
            groups = [np.ones(len(cols["y"]), dtype=bool)]
        self._drift_fits = [drift_fit_plateaus(cols["y"][g], sweep[g], None if times is None else times[g])
                            for g in groups]
        if method != "off":
            y = cols["y"].copy()
            for g in groups:
                y[g] = correct_drift(method, cols["y"][g], sweep[g], None if times is None else times[g])
            cols["y"] = y
        if len(groups) > 1 or not groups[0].all():
            for key, a in cols.items():
                cols[key] = average_cycles([a[g] for g in groups])
        if self.offset.isChecked():
            cols["y"] = remove_offset(cols["y"])
        if self.norm.isChecked():
            cols["y"] = normalize(cols["y"])
        if "B" in cols:
            cols["H"] = cols["B"] * MT_TO_OE
        if "x" not in cols:
            mode = self.xaxis.mode
            if mode == "current":
                cols["x"] = cols["I"]
            elif mode == "current_set":
                cols["x"] = cols["Iset"]
            else:
                cols["x"] = to_x(mode, cols["B"])
        self._even, self._sym_error = None, ""
        if self.sym.isChecked():
            self._symmetrize(cols)
        return cols

    def _symmetrize(self, cols: dict) -> None:
        """Replace the loop by its odd part (last step, after drift/offset/normalize); keep the even part."""
        if not self._x_from_switch():
            self._sym_error = "Symmetrization needs X = current / field (X axis switch)."
            return
        try:
            s = symmetrize(cols["x"], cols["y"], cols.get("vp"))
        except ValueError as e:
            self._sym_error = f"Not symmetrized: {e}."
            return
        self._even = (cols["x"][s.up], s.even - s.even.mean())
        for key in ("x", "I", "Iset", "B", "H", "vp"):    # field-like columns: H going up, -H going down
            if key in cols:
                cols[key] = s.mirror(cols[key])
        cols["y"] = np.concatenate([s.odd, -s.odd])

    def series(self):
        """(x, y, v_programmed) as plotted."""
        s = self.series_all()
        return s["x"], s["y"], s.get("vp")

    def redraw(self):
        if self.m is None or not self.y_combo.currentText():
            return
        for line in self.hc_lines:
            self.plot.removeItem(line)
        self.hc_lines = []
        self.result.setText("")
        self.xaxis.setEnabled(self._x_from_switch())
        x, y, _ = self.series()
        self.curve.setData(x, y)
        if self._even is not None and self.show_even.isChecked():
            self.even_curve.setData(*self._even)
        else:
            self.even_curve.setData([], [])
        self.autoscale.apply()
        self._show_drift()
        if self._x_from_switch():
            self.plot.setLabel("bottom", x_label(self.xaxis.mode))
        else:
            self.plot.setLabel("bottom", self.x_combo.currentText().replace(storage.X_TAG, ""))
        note = ""
        if self._x_from_switch() and self.xaxis.mode != "current":
            if self._b_source == "active":
                note = (f"Field computed with the active calibration ({self.state.calibration.summary()}): "
                        "this file has no field column.")
            elif self._b_source == "hall":
                note = "Field measured by the Hall probe at every point."
        elif (self._x_from_switch() and self.xaxis.mode == "current_set"
              and not self.m.has(storage.COL_ISET)):
            note = (f"Programmed current rebuilt as V_programmed / {self.m.nominal_resistance():g} Ω "
                    "(this file predates the I_set column).")
        if self.sym.isChecked():
            sym_note = self._sym_error or ("Odd part shown: centred by construction (a real loop shift is "
                                           "removed too)." + ("  Dashed grey: even part, mean removed "
                                                              "(hover 'Even part' for how to read it)."
                                                              if self.show_even.isChecked() else ""))
            note = f"{note}  {sym_note}".strip()
        self.x_note.setText(note)
        self.plot.setLabel("left", self.y_combo.currentText().replace(storage.Y_TAG, "")
                           + (" (normalized)" if self.norm.isChecked() else "")
                           + (", odd part" if self._even is not None else ""))

    def _show_drift(self):
        """How much the raw signal drifted per cycle, measured on the saturation plateaus."""
        fits = [f for f in self._drift_fits if f.has_loop]
        if not fits:
            self.drift_info.setText("Drift: no clear saturated loop found (does it saturate? is the laser on?)")
            return
        drift = float(np.mean([f.drift for f in fits]))
        step = float(np.mean([abs(f.step) for f in fits]))
        self.drift_info.setText(f"Measured drift: {drift * 1000:+.3f} mV per cycle = {abs(drift) / step * 100:.0f} % "
                                f"of the {step * 1000:.2f} mV Kerr step")

    def compute(self):
        if self.m is None:
            return
        x, y, vp = self.series()
        r = analyze_loop(x, y, vp)
        for line in self.hc_lines:
            self.plot.removeItem(line)
        self.hc_lines = []
        if r.hc is None:
            self.result.setText("Could not find a zero crossing on both branches.\n"
                                "Is this a full hysteresis loop?")
            return
        unit = self._x_unit()
        lines = [f"Hc (half width)  = {r.hc:.4g} {unit}",
                 f"Hc ascending     = {r.hc_ascending:+.4g} {unit}",
                 f"Hc descending    = {r.hc_descending:+.4g} {unit}",
                 f"Loop shift       = {r.shift:+.4g} {unit}"
                 + ("  (0 by symmetrization)" if self._even is not None else "")]
        if self.mr.isChecked():
            lines.append(f"Mr/Ms            = {r.squareness:.3f}" if r.squareness is not None
                         else "Mr/Ms            = n/a")
        self.result.setText("\n".join(lines))
        for xv in (r.hc_ascending, r.hc_descending):
            line = pg.InfiniteLine(xv, angle=90, pen=theme.pen("marker", 1.5, style=Qt.DashLine))
            self.plot.addItem(line)
            self.hc_lines.append(line)

    # -------------------------------------------------------------- saving
    def _update_save_buttons(self):
        self.save_curve_btn.setEnabled(self.m is not None)
        self.save_raw_btn.setEnabled(self.m is not None and self.m.path is None)

    def _ask_path(self, kind: str) -> Path | None:
        sample = str(self.m.metadata.get("sample") or "")
        default = storage.make_path(self.default_folder, sample, kind, datetime.now())
        path, _ = QFileDialog.getSaveFileName(self, "Save measurement", str(default), "CSV (*.csv)")
        return Path(path) if path else None

    def _saved(self, path: Path, what: str):
        log.info("Analysis: saved %s to %s", what, path)
        self.save_status.setText(f"Saved {what}:\n{path.name}")

    def save_curve(self):
        """Save the curve exactly as plotted (current and field columns), processing options in the header."""
        if self.m is None:
            return
        path = self._ask_path("analysis")
        if path is None:
            return
        s = self.series_all()
        symmetrized = self._even is not None
        yname = (self.y_combo.currentText().replace(storage.Y_TAG, "")
                 + ("_normalized" if self.norm.isChecked() else "") + ("_odd" if symmetrized else ""))
        columns, data = [storage.COL_POINT], [np.arange(len(s["y"]))]
        if "Iset" in s:
            columns.append(storage.COL_ISET)
            data.append(s["Iset"])
        if "I" in s:
            columns.append(storage.COL_CURRENT)
            data.append(s["I"])
        if "B" in s:
            hall = self._b_source == "hall"
            columns += [storage.COL_HALL, storage.COL_HALL_OE] if hall else [storage.COL_FIELD, storage.COL_FIELD_OE]
            data += [s["B"], s["H"]]
        if not self._x_from_switch():
            xname = self.x_combo.currentText().replace(storage.X_TAG, "")
            columns.append(xname + storage.X_TAG)
            data.append(s["x"])
        ycol = yname + storage.Y_TAG
        columns.append(ycol)
        data.append(s["y"])
        if "vp" in s and storage.COL_VPROG not in columns:
            columns.append(storage.COL_VPROG)
            data.append(s["vp"])

        meta = dict(self.m.metadata)
        field_from_active = "B" in s and self._b_source == "active"
        descr = storage.describe_columns(columns, self.state.calibration if field_from_active else None)
        old = self.m.metadata.get("columns") if isinstance(self.m.metadata.get("columns"), dict) else {}
        for c in columns:
            if not descr.get(c):
                descr[c] = old.get(c, "")
        if self.norm.isChecked():
            descr[ycol] = "Y AXIS: photodetector signal normalized to ±1 (2/98 percentiles), dimensionless."
        if symmetrized:
            descr[ycol] = (descr.get(ycol) or "Y AXIS: photodetector signal.") + (
                " Odd part only (symmetrized): odd(H) = [S_up(H) - S_down(-H)] / 2, rows in the ascending half "
                "then the mirrored descending half; offset and even-in-field contributions removed, loop "
                "centred by construction.")
        meta["columns"] = descr
        if field_from_active:
            meta["calibration"] = self.state.calibration.header()
        meta["x_axis"] = [c for c in columns if c.endswith(storage.X_TAG)]
        meta["analysis"] = {
            "saved": datetime.now().isoformat(timespec="seconds"),
            "source": self.m.path.name if self.m.path is not None else "last measurement (not saved)",
            "cycles": self.cycle_combo.currentText(),
            "drift_correction": self.drift.method,
            "offset_removed": self.offset.isChecked(),
            "normalized": self.norm.isChecked(),
            "symmetrized": symmetrized,
            "plotted_x": self.plot.getAxis("bottom").labelText,
        }
        if field_from_active:
            meta["analysis"]["field_note"] = "B_mT computed from the current with the active calibration"
        if self.result.text():
            meta["analysis"]["result"] = self.result.text().splitlines()
        try:
            storage.write_table(path, meta, columns, np.column_stack(data))
        except OSError as e:
            QMessageBox.critical(self, "Save", f"Could not write {path}:\n{e}")
            return
        self._saved(path, "curve")

    def save_raw(self):
        """Save an unsaved measurement as it was taken (every point of every cycle)."""
        if self.m is None or self.m.path is not None:
            return
        path = self._ask_path("raw")
        if path is None:
            return
        try:
            storage.write_table(path, self.m.metadata, self.m.columns, self.m.data)
        except OSError as e:
            QMessageBox.critical(self, "Save", f"Could not write {path}:\n{e}")
            return
        self.m.path = path      # same object as state.last_measurement: it now counts as saved
        self.file_label.setText(f"<b>{path.name}</b>")
        self._update_save_buttons()
        self._saved(path, "raw data")

    # -------------------------------------------------------------- misc
    def _mouse_moved(self, pos):
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        p = self.plot.plotItem.vb.mapSceneToView(pos)
        self.vline.setPos(p.x())
        self.hline.setPos(p.y())
        self.cursor.setText(f"x = {p.x():+.5g}    y = {p.y():+.5g}")

    def _export(self):
        if self.m is None:
            return
        base = self.m.path if self.m.path is not None else Path(self.default_folder) / "last_measurement.csv"
        default = str(base.with_suffix(".png"))
        path, _ = QFileDialog.getSaveFileName(self, "Export plot", default, "PNG (*.png)")
        if path:
            exporter = pg.exporters.ImageExporter(self.plot.plotItem)
            exporter.parameters()["width"] = 1600
            exporter.export(path)
