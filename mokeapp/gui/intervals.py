"""'Variable intervals' panel of the Hysteresis tab: X intervals, each with its own points and settle time.

The intervals are kept in magnet volts (what the sweep engine needs) and shown in the active X unit: A, or
mT / Oe when a field calibration exists. Switching the X axis only changes how they are displayed.
"""
import json
import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QPushButton, QSizePolicy,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from mokeapp.core.calibration import MT_TO_OE, CalibrationError
from mokeapp.core.waveforms import Interval, make_interval_cycle, validate_intervals
from mokeapp.gui.widgets import FIELD_MODES

PREF_ROWS = "interval_rows_V"
PREF_ON = "intervals_on"
COLUMNS = ("From", "To", "Points", "Settle (ms)", "")
DECIMALS = {"A": 3, "mT": 2, "Oe": 1}


class IntervalPanel(QWidget):
    changed = Signal()

    def __init__(self, state):
        super().__init__()
        self.state = state
        self.toggle = QPushButton()
        self.toggle.setCheckable(True)
        self.toggle.setObjectName("browse")
        self.toggle.setToolTip("Sweep the X axis in intervals, each with its own points and settle time:\n"
                               "e.g. coarse and fast in the saturations, fine and slow around the switching.")
        self.body = QWidget()
        v = QVBoxLayout(self.body)
        v.setContentsMargins(0, 4, 0, 0)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.AllEditTriggers)
        self.table.itemChanged.connect(self._edited)
        h = self.table.horizontalHeader()
        for col in range(4):
            h.setSectionResizeMode(col, QHeaderView.Stretch)
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        v.addWidget(self.table)
        row = QHBoxLayout()
        self.add_btn = QPushButton("Add interval")
        self.preset_btn = QPushButton("Fine around 0")
        self.preset_btn.setToolTip("Three intervals from the amplitude: coarse and fast in both saturations, "
                                   "fine and slow over the middle third.")
        row.addWidget(self.add_btn)
        row.addWidget(self.preset_btn)
        row.addStretch()
        v.addLayout(row)
        self.summary = QLabel()
        self.summary.setObjectName("hint")
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.toggle)
        root.addWidget(self.body)
        # take only the height of what is shown: folding the table must give the space back to the panel
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

        self._volts: list[Interval] = []
        self._loading = False
        self.error = ""
        self.toggle.toggled.connect(self._toggled)
        self.add_btn.clicked.connect(self.add_interval)
        self.preset_btn.clicked.connect(lambda: self.preset(self._amplitude()))
        state.x_mode_changed.connect(lambda _: self._render())
        state.calibration_changed.connect(self._render)
        state.limits_changed.connect(self._render)
        self._restore()

    # ------------------------------------------------------------ units
    @property
    def unit(self) -> str:
        """Unit of the From/To columns: the X axis', when it can be converted to magnet volts."""
        mode = self.state.x_mode
        return mode if mode in FIELD_MODES and self.state.calibration is not None else "A"

    def _resistance(self) -> float:
        """Ω converting current to magnet volts: the measured R for the measured-current axis, else nominal."""
        if self.state.x_mode == "current" and self.state.inst.monitors:
            return self.state.resistance
        return self.state.cfg.kepco.magnet_resistance_ohm

    def to_volts(self, x: float) -> float:
        unit = self.unit
        if unit == "A":
            return x * self._resistance()
        mT = x / MT_TO_OE if unit == "Oe" else x
        return self.state.calibration.current_for(mT) * self._resistance()

    def from_volts(self, v: float) -> float:
        amps = v / self._resistance()
        if self.unit == "A":
            return amps
        mT = float(self.state.calibration.field(amps))
        return mT * MT_TO_OE if self.unit == "Oe" else mT

    def _amplitude(self) -> float:
        params = getattr(self, "params", None)
        return params.amplitude.value() if params is not None else self.state.recommended_voltage

    # ------------------------------------------------------------ model
    @property
    def active(self) -> bool:
        return self.toggle.isChecked()

    def intervals(self) -> list[Interval]:
        """In magnet volts."""
        return list(self._volts)

    def rows(self) -> list[dict]:
        """The table as shown (X unit), for the CSV header."""
        return [{"from": round(self.from_volts(i.x_from), 6), "to": round(self.from_volts(i.x_to), 6),
                 "unit": self.unit, "points": i.points, "settle_ms": i.settle_ms,
                 "from_V": round(i.x_from, 6), "to_V": round(i.x_to, 6)} for i in self._volts]

    def set_intervals(self, intervals: list[Interval]) -> None:
        self._volts = list(intervals)
        self._render()
        self._changed()

    def preset(self, amplitude_V: float) -> None:
        a = abs(amplitude_V) or 1.0
        self.set_intervals([Interval(-a, -a / 3, 10, 20), Interval(-a / 3, a / 3, 60, 80),
                            Interval(a / 3, a, 10, 20)])

    def add_interval(self) -> None:
        last = self._volts[-1] if self._volts else Interval(-1.0, 0.0, 10, 50)
        step = (last.x_to - last.x_from) or 1.0
        self.set_intervals(self._volts + [Interval(last.x_to, last.x_to + step, last.points, last.settle_ms)])

    def remove_interval(self, row: int) -> None:
        self.set_intervals([iv for k, iv in enumerate(self._volts) if k != row])

    # ------------------------------------------------------------ table
    def _render(self) -> None:
        """Rebuild the table from the model, in the current unit (plain editable cells: compact)."""
        self._loading = True
        self.table.setRowCount(len(self._volts))
        dec = DECIMALS[self.unit]
        for r, iv in enumerate(self._volts):
            try:
                x_from, x_to = f"{self.from_volts(iv.x_from):.{dec}f}", f"{self.from_volts(iv.x_to):.{dec}f}"
            except CalibrationError:
                x_from = x_to = "?"
            for c, text in enumerate((x_from, x_to, str(iv.points), f"{iv.settle_ms:g}")):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, item)
            rm = QPushButton("✕")
            rm.setObjectName("browse")
            rm.setFixedWidth(28)
            rm.setToolTip("Remove this interval")
            rm.clicked.connect(lambda _, r=r: self.remove_interval(r))
            self.table.setCellWidget(r, 4, rm)
        self.table.setHorizontalHeaderLabels([f"From ({self.unit})", f"To ({self.unit})", "Points", "Settle (ms)", ""])
        rows_h = sum(self.table.rowHeight(r) for r in range(self.table.rowCount()))
        self.table.setFixedHeight(self.table.horizontalHeader().height() + rows_h + 4)
        self._loading = False
        self._update_summary()

    def set_cell(self, row: int, col: int, value) -> None:
        """Type a value into a cell (as the user would)."""
        self.table.item(row, col).setText(str(value))

    def _edited(self, item) -> None:
        if self._loading:
            return
        row = item.row()
        texts = [self.table.item(row, c).text().strip().replace(",", ".") for c in range(4)]
        try:
            x_from, x_to, settle = float(texts[0]), float(texts[1]), float(texts[3])
            points = int(float(texts[2]))
        except ValueError:
            self.error = f"Interval {row + 1}: type numbers (decimal point)."
            self.summary.setText(f"<span style='color:#b91c1c'>{self.error}</span>")
            self.changed.emit()
            return
        try:
            v_from, v_to = self.to_volts(x_from), self.to_volts(x_to)
        except CalibrationError as e:
            self.error = f"Interval {row + 1}: {e}"
            self.summary.setText(f"<span style='color:#b91c1c'>{self.error}</span>")
            self.changed.emit()
            return
        self._volts[row] = Interval(v_from, v_to, points, settle)
        self._changed()

    def _changed(self) -> None:
        self._update_summary()
        self._save()
        self.changed.emit()

    def _update_summary(self) -> None:
        self.error = validate_intervals(self._volts, self.state.max_voltage) if self.active else ""
        if self.error:
            self.summary.setText(f"<span style='color:#b91c1c'>{self.error}</span>")
            return
        if not self._volts:
            self.summary.setText("")
            return
        v, settles = make_interval_cycle(self._volts)
        read_s = self.params.read_time_s() if hasattr(self, "params") else 0.0
        t = float(settles.sum()) + len(v) * read_s
        self.summary.setText(f"{len(v)} points per cycle (up, then back down through the same points) · "
                             f"≈ {t:.0f} s per cycle · up to ±{max(abs(x) for x in v):.2f} V")

    def cycle(self):
        """(volts, settle seconds) of one cycle, or None when off or invalid."""
        if not self.active or self.error or not self._volts:
            return None
        return make_interval_cycle(self._volts)

    def _toggled(self, on: bool) -> None:
        self.toggle.setText(("▾  " if on else "▸  ") + "Variable intervals" + (": on" if on else ""))
        self.body.setVisible(on)
        self.updateGeometry()      # the scroll panel re-lays out at once
        if on and not self._volts:
            self.preset(self._amplitude())
        self._update_summary()
        self._save()
        self.changed.emit()

    # ------------------------------------------------------------ remembered per PC
    def _save(self) -> None:
        prefs = self.state.prefs
        if prefs is None or self._loading:
            return
        prefs.setValue(PREF_ON, "1" if self.active else "0")
        prefs.setValue(PREF_ROWS, json.dumps([[i.x_from, i.x_to, i.points, i.settle_ms] for i in self._volts]))

    def _restore(self) -> None:
        prefs = self.state.prefs
        self._loading = True
        try:
            rows = json.loads(prefs.value(PREF_ROWS) or "[]") if prefs is not None else []
            self._volts = [Interval(float(a), float(b), int(n), float(s)) for a, b, n, s in rows
                           if all(math.isfinite(float(x)) for x in (a, b, s))]
        except (ValueError, TypeError):
            self._volts = []
        on = prefs is not None and prefs.value(PREF_ON) == "1"
        self._loading = False
        self.toggle.setChecked(on)
        self._toggled(on)
        self._render()
