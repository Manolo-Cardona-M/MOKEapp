"""Calibration tab: Hall-probe points (current A, field mT) per coil, polynomial fit, saved library.

The saved fit of the active coil converts magnet current to field everywhere in the app (X axis switch
in Hysteresis/Analysis and the B_mT column of every CSV) until the coil is recalibrated or switched.
"""
import logging
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QInputDialog, QLabel,
                               QMessageBox, QPushButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from mokeapp.core.calibration import DEGREE_NAMES, CalibrationError, fit, read_calibration_file
from mokeapp.gui import theme
from mokeapp.gui.widgets import AutoScale, make_plot

log = logging.getLogger(__name__)

EMPTY_ROWS = 12
GOOD_R2 = 0.999


def _number(text: str) -> float:
    """Decimal point only: '1,25' is rejected rather than guessed (it could be a list separator)."""
    if "," in text:
        raise ValueError("use '.' for decimals")
    return float(text.strip())


class CalibrationTab(QWidget):
    def __init__(self, state):
        super().__init__()
        self.state = state
        self.library = state.library
        self.fit_result = None      # CoilCalibration shown in the plot (fresh fit or the saved one)
        self.unsaved = False

        root = QHBoxLayout(self)
        left_w = QWidget()
        left = QVBoxLayout(left_w)
        left.setContentsMargins(0, 0, 6, 0)
        scroll = QScrollArea()
        scroll.setWidget(left_w)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setFixedWidth(400)
        root.addWidget(scroll)

        # --- coil
        box = QGroupBox("Coil")
        v = QVBoxLayout(box)
        self.coil = QComboBox()
        self.coil.setPlaceholderText("no coil yet: press New coil")
        h = QHBoxLayout()
        self.new_btn = QPushButton("New coil...")
        self.delete_btn = QPushButton("Delete")
        h.addWidget(self.coil, 1)
        h.addWidget(self.new_btn)
        h.addWidget(self.delete_btn)
        v.addLayout(h)
        self.active_label = QLabel()
        self.active_label.setWordWrap(True)
        v.addWidget(self.active_label)
        h = QHBoxLayout()
        self.use_btn = QPushButton("Use this coil")
        self.none_btn = QPushButton("Use no calibration")
        h.addWidget(self.use_btn)
        h.addWidget(self.none_btn)
        v.addLayout(h)
        h = QHBoxLayout()
        self.load_btn = QPushButton("Load from file...")
        self.load_btn.setToolTip("Use a saved calibration again (any file in the calibrations folder, an older "
                                 "one, or one copied from another PC)")
        self.folder_btn = QPushButton("Open folder")
        self.folder_btn.setToolTip(str(self.library.folder))
        h.addWidget(self.load_btn)
        h.addWidget(self.folder_btn)
        v.addLayout(h)
        left.addWidget(box)

        # --- points
        box = QGroupBox("Hall probe points")
        v = QVBoxLayout(box)
        self.source = QComboBox()
        self.source.addItem("Measured (Kepco monitor)", "measured")
        self.source.addItem("Programmed (V/R)", "programmed")
        self.source.setToolTip("Which current you type in the table. Measured: the value in Instruments → "
                               "Live readout.\nProgrammed: the current you set in Manual magnet control "
                               "(V / nominal R); use it when the Kepco monitors are not wired.")
        self.source.setCurrentIndex(0 if state.inst.monitors else 1)
        h = QHBoxLayout()
        h.addWidget(QLabel("Current"))
        h.addWidget(self.source, 1)
        v.addLayout(h)
        self.hint = QLabel()
        self.hint.setObjectName("hint")
        self.hint.setWordWrap(True)
        v.addWidget(self.hint)
        self.table = QTableWidget(EMPTY_ROWS, 2)
        self.table.setHorizontalHeaderLabels(["Current (A)", "Field (mT)"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setDefaultSectionSize(150)
        self.table.setMinimumHeight(260)
        v.addWidget(self.table)
        h = QHBoxLayout()
        self.add_btn = QPushButton("Add row")
        self.remove_btn = QPushButton("Remove row")
        self.clear_btn = QPushButton("Clear")
        self.paste_btn = QPushButton("Paste")
        self.paste_btn.setToolTip("Paste two columns copied from Excel (current, field)")
        for b in (self.add_btn, self.remove_btn, self.clear_btn, self.paste_btn):
            h.addWidget(b)
        v.addLayout(h)
        left.addWidget(box)

        # --- fit
        box = QGroupBox("Fit")
        f = QFormLayout(box)
        self.model = QComboBox()
        for degree, name in DEGREE_NAMES.items():
            self.model.addItem(name.capitalize(), degree)
        self.fit_btn = QPushButton("Fit")
        self.fit_btn.setProperty("role", "primary")
        h = QHBoxLayout()
        h.addWidget(self.model, 1)
        h.addWidget(self.fit_btn)
        f.addRow("Model", h)
        self.origin = QCheckBox("Force B = 0 at I = 0")
        self.origin.setToolTip("Ignore a constant offset (Hall probe zero, Earth's field, yoke remanence).\n"
                               "Without it, an offset b in B = a·I + b shifts every loop by b.")
        f.addRow("", self.origin)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f.addRow(self.result)
        self.save_btn = QPushButton("Save calibration")
        f.addRow(self.save_btn)
        self.history = QLabel("")
        self.history.setObjectName("hint")
        self.history.setWordWrap(True)
        f.addRow(self.history)
        left.addWidget(box)
        left.addStretch()

        # --- plots
        self.plot = make_plot("Field vs current", "Magnet current (A)", "Field (mT)")
        self.resid_plot = make_plot("Residuals", "Magnet current (A)", "Field − fit (mT)")
        self.resid_plot.setMinimumHeight(140)
        self.resid_plot.setXLink(self.plot)
        self.points_curve = self.plot.plot(pen=None, symbol="o", symbolSize=8, symbolBrush=theme.color("live"),
                                           symbolPen=None)
        self.fit_curve = self.plot.plot(pen=theme.pen("avg", 2))
        self.resid_curve = self.resid_plot.plot(pen=None, symbol="o", symbolSize=6,
                                                symbolBrush=theme.color("avg"), symbolPen=None)
        self.resid_plot.addLine(y=0, pen=theme.pen("prev", 1))
        self.autoscale = AutoScale({self.plot: "xy", self.resid_plot: "xy"})
        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.plot)
        splitter.addWidget(self.resid_plot)
        splitter.setSizes([600, 220])
        right = QVBoxLayout()
        right.addWidget(self.autoscale)
        right.addWidget(splitter, 1)
        root.addLayout(right, 1)

        self.new_btn.clicked.connect(self.new_coil)
        self.delete_btn.clicked.connect(self.delete_coil)
        self.use_btn.clicked.connect(self.use_coil)
        self.none_btn.clicked.connect(self.use_none)
        self.load_btn.clicked.connect(lambda: self.load_file())
        self.folder_btn.clicked.connect(self.open_folder)
        self.coil.currentTextChanged.connect(self._coil_selected)
        self.add_btn.clicked.connect(lambda: self.table.insertRow(self.table.rowCount()))
        self.remove_btn.clicked.connect(self._remove_row)
        self.clear_btn.clicked.connect(self._clear)
        self.paste_btn.clicked.connect(self.paste)
        self.fit_btn.clicked.connect(self.do_fit)
        self.save_btn.clicked.connect(self.save)
        self.table.itemChanged.connect(self._points_edited)
        self.model.currentIndexChanged.connect(lambda _: self._points_edited())
        self.origin.toggled.connect(lambda _: self._points_edited())
        self.source.currentIndexChanged.connect(lambda _: (self._update_hint(), self._points_edited()))
        self._update_hint()
        state.calibration_changed.connect(self._update_status)

        self._reload_coils(self.library.active_coil)

    def _update_hint(self):
        measured = self.source.currentData() == "measured"
        self.hint.setText(("Current as read in Instruments → Live readout (Kepco monitor)" if measured else
                           "Current set in Instruments → Manual magnet control → Set current (V / nominal R)")
                          + ", field from the Hall probe. Cover the full range you will measure, both signs, "
                            "e.g. 10–20 points.")
        self.table.setHorizontalHeaderLabels(
            ["Current (A, measured)" if measured else "Current (A, programmed)", "Field (mT)"])
        if measured and not self.state.inst.monitors:
            self.hint.setText(self.hint.text() + " <b>The Kepco monitors are not wired in this setup.</b>")

    # -------------------------------------------------------------- coils
    def _reload_coils(self, select: str | None = None):
        self.coil.blockSignals(True)
        self.coil.clear()
        self.coil.addItems(self.library.coils())
        self.coil.blockSignals(False)
        if select and select in self.library.coils():
            self.coil.setCurrentText(select)
        elif self.coil.count():
            self.coil.setCurrentIndex(0)
        self._coil_selected(self.coil.currentText())

    def _coil_selected(self, coil: str):
        cal = self.library.latest(coil) if coil else None
        self._fill_table(cal.points if cal else [])
        if cal:
            for w in (self.model, self.origin, self.source):
                w.blockSignals(True)
            self.model.setCurrentIndex(self.model.findData(cal.degree))
            self.origin.setChecked(cal.through_origin)
            self.source.setCurrentIndex(self.source.findData(cal.current_source))
            for w in (self.model, self.origin, self.source):
                w.blockSignals(False)
            self._update_hint()
        self._show_fit(cal, saved=True)
        self._update_status()

    def new_coil(self):
        name, ok = QInputDialog.getText(self, "New coil", "Coil name (e.g. 'Big coil, 2 mm gap'):")
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in self.library.coils():
            QMessageBox.warning(self, "New coil", f"'{name}' already exists: select it to recalibrate.")
            return
        self.library.add_coil(name)
        self._reload_coils(name)

    def delete_coil(self):
        coil = self.coil.currentText()
        if not coil:
            return
        n = len(self.library.history(coil))
        ans = QMessageBox.question(self, "Delete coil", f"Delete '{coil}' and its {n} saved calibration(s)?")
        if ans != QMessageBox.Yes:
            return
        self.library.delete_coil(coil)
        self.state.refresh_calibration()
        self._reload_coils(self.library.active_coil)

    def use_coil(self):
        coil = self.coil.currentText()
        try:
            self.library.set_active(coil)
        except CalibrationError as e:
            QMessageBox.warning(self, "Use coil", str(e))
            return
        self.state.refresh_calibration()

    def use_none(self):
        self.library.set_active(None)
        self.state.refresh_calibration()

    def load_file(self, path: str | None = None):
        """Pick a calibration file (or one entry of an old multi-calibration file) and use it."""
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Load calibration", str(self.library.folder),
                                                  "Calibration (*.yaml)")
            if not path:
                return
        try:
            cals = read_calibration_file(Path(path))
        except CalibrationError as e:
            QMessageBox.warning(self, "Load calibration", str(e))
            return
        cal = cals[0]
        if len(cals) > 1:
            labels = [f"{c.summary()} (R² {c.r2:.5f})" for c in cals]
            choice, ok = QInputDialog.getItem(self, "Load calibration", "This file holds several calibrations:",
                                              labels, 0, False)
            if not ok:
                return
            cal = cals[labels.index(choice)]
        self.library.use(cal)
        self.state.refresh_calibration()
        self._reload_coils(cal.coil)
        self._fill_table(cal.points)
        self._show_fit(cal, saved=True)
        self._update_status()
        log.info("Calibration loaded from %s: %s", path, cal.summary())

    def open_folder(self):
        self.library.folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.library.folder)))

    def _update_status(self):
        active = self.library.active
        coil = self.coil.currentText()
        if active is None:
            self.active_label.setText("<b>No calibration in use</b>: the X axis is the magnet current (A).")
        else:
            self.active_label.setText(f"In use: <b>{active.coil}</b> — {active.equation()} "
                                      f"(R² = {active.r2:.5f}, {active.date[:10]})")
        has_cal = bool(coil) and self.library.latest(coil) is not None
        self.use_btn.setEnabled(has_cal and coil != self.library.active_coil)
        self.none_btn.setEnabled(active is not None)
        self.delete_btn.setEnabled(bool(coil))
        self.fit_btn.setEnabled(bool(coil))
        self.save_btn.setText(f"Save calibration for '{coil}'" if coil else "Save calibration")
        self.save_btn.setEnabled(bool(coil) and self.unsaved and self.fit_result is not None)
        hist = self.library.history(coil) if coil else []
        self.history.setText("History: " + "; ".join(f"{c.date[:16].replace('T', ' ')} R²={c.r2:.4f}"
                                                     for c in hist[:5]) if hist else "")

    # -------------------------------------------------------------- table
    def _fill_table(self, points):
        self.table.blockSignals(True)
        self.table.clearContents()
        self.table.setRowCount(max(EMPTY_ROWS, len(points) + 2))
        for r, (i, b) in enumerate(points):
            self.table.setItem(r, 0, QTableWidgetItem(f"{i:.6g}"))
            self.table.setItem(r, 1, QTableWidgetItem(f"{b:.6g}"))
        self.table.blockSignals(False)
        self._plot_points()

    def _remove_row(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True) or [self.table.rowCount() - 1]
        for r in rows:
            self.table.removeRow(r)
        self._points_edited()

    def _clear(self):
        self._fill_table([])
        self._points_edited()

    def paste(self):
        """Two columns copied from Excel (tab separated; decimal comma or point)."""
        text = QApplication.clipboard().text()
        rows = []
        for line in text.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t") if "\t" in line else (line.split(";") if ";" in line else line.split())
            if len(parts) >= 2:
                rows.append(parts[:2])
        if not rows:
            QMessageBox.warning(self, "Paste", "The clipboard has no two-column data.")
            return
        start = len(self.points_raw())
        self.table.blockSignals(True)
        if self.table.rowCount() < start + len(rows) + 1:
            self.table.setRowCount(start + len(rows) + 1)
        for k, (a, b) in enumerate(rows):
            self.table.setItem(start + k, 0, QTableWidgetItem(a.strip()))
            self.table.setItem(start + k, 1, QTableWidgetItem(b.strip()))
        self.table.blockSignals(False)
        self._points_edited()

    def points_raw(self) -> list[tuple[int, str, str]]:
        """Non-empty rows as (row, current text, field text)."""
        out = []
        for r in range(self.table.rowCount()):
            texts = [(self.table.item(r, c).text() if self.table.item(r, c) else "").strip() for c in (0, 1)]
            if any(texts):
                out.append((r, *texts))
        return out

    def points(self) -> tuple[np.ndarray, np.ndarray]:
        i, b = [], []
        for r, ti, tb in self.points_raw():
            try:
                i.append(_number(ti))
                b.append(_number(tb))
            except ValueError:
                hint = " (use '.' for decimals, e.g. 1.25)" if "," in ti + tb else ""
                raise CalibrationError(f"Row {r + 1}: '{ti}' / '{tb}' is not a pair of numbers{hint}") from None
        return np.array(i), np.array(b)

    def _mark_invalid_cells(self) -> None:
        """Light red background on cells that are not plain numbers with a decimal point."""
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            for c in (0, 1):
                item = self.table.item(r, c)
                if item is None:
                    continue
                text = item.text().strip()
                try:
                    ok = not text or _number(text) is not None
                except ValueError:
                    ok = False
                item.setBackground(QColor("#fde2e2") if not ok else QColor(0, 0, 0, 0))
                item.setToolTip("" if ok else "Not a number: use '.' for decimals, e.g. 1.25")
        self.table.blockSignals(False)

    def _points_edited(self, *_):
        self.fit_result = None
        self.unsaved = False
        self._mark_invalid_cells()
        try:
            self.points()
            self.result.setText("Points changed: press Fit.")
        except CalibrationError as e:
            self.result.setText(f"<span style='color:#b91c1c'>{e}</span>")
        self.fit_curve.setData([], [])
        self.resid_curve.setData([], [])
        self._plot_points()
        self._update_status()

    def _plot_points(self):
        try:
            i, b = self.points()
        except CalibrationError:
            return
        self.points_curve.setData(i, b)
        self.autoscale.apply()

    # -------------------------------------------------------------- fit
    def do_fit(self):
        coil = self.coil.currentText()
        try:
            i, b = self.points()
            cal = fit(coil, i, b, self.model.currentData(), self.origin.isChecked(), self.source.currentData())
        except CalibrationError as e:
            QMessageBox.warning(self, "Fit", str(e))
            return
        self._show_fit(cal, saved=False)
        self._update_status()

    def _show_fit(self, cal, saved: bool):
        self.fit_result = cal
        self.unsaved = cal is not None and not saved
        if cal is None:
            self.result.setText("")
            self.fit_curve.setData([], [])
            self.resid_curve.setData([], [])
            return
        pts = np.array(cal.points)
        lo, hi = cal.current_range
        xs = np.linspace(lo, hi, 200)
        self.points_curve.setData(pts[:, 0], pts[:, 1])
        self.fit_curve.setData(xs, cal.field(xs))
        self.resid_curve.setData(pts[:, 0], pts[:, 1] - cal.field(pts[:, 0]))
        lines = [f"<b>{cal.equation()}</b>",
                 f"Slope at 0 A: {cal.slope:.4g} mT/A",
                 f"R² = {cal.r2:.6f},  max residual = {cal.max_residual_mT:.3g} mT",
                 f"Valid for {lo:.3g} … {hi:.3g} A ({len(pts)} points), {cal.current_source} current"]
        warn = "<span style='color:#b45309'>{}</span>"
        if cal.r2 < GOOD_R2 and cal.degree == 1:
            lines.append(warn.format("The points curve away from a line (iron saturation?): try Polynomial 3, "
                                     "or calibrate a smaller current range."))
        if lo >= 0 or hi <= 0:
            lines.append(warn.format("All points have the same sign: the other half of every loop is "
                                     "extrapolated. Measure negative currents too."))
        offset = cal.coefficients[0]
        if not cal.through_origin and abs(offset) > 0.02 * np.max(np.abs(pts[:, 1])):
            lines.append(warn.format(f"Offset {offset:+.3g} mT at 0 A: every loop in mT/Oe is shifted by this "
                                     "much. If it is the Hall probe zero, zero the probe and recalibrate, or tick "
                                     "'Force B = 0 at I = 0'."))
        lines.append("<i>Saved calibration</i>" if saved else
                     "<span style='color:#b45309'>Not saved yet</span>")
        self.result.setText("<br>".join(lines))
        self.autoscale.fit()

    def save(self):
        cal = self.fit_result
        if cal is None or not self.unsaved:
            return
        if self.library.latest(cal.coil) is not None:
            ans = QMessageBox.question(self, "Save calibration",
                                       f"Replace the calibration in use for '{cal.coil}'?\n"
                                       "The previous one stays in the history.")
            if ans != QMessageBox.Yes:
                return
        self.library.add(cal, make_active=True)
        self.unsaved = False
        self._show_fit(cal, saved=True)
        self.state.refresh_calibration()

    def shutdown(self):
        pass
