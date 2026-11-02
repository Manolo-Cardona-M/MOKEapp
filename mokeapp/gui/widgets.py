"""Reusable widgets: status light, sweep parameter form, plot factory, auto-scale, wheel guard."""
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (QAbstractSpinBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QPushButton, QSpinBox, QTabBar, QWidget)

from mokeapp.core.calibration import MT_TO_OE
from mokeapp.core.processing import DRIFT_METHODS
from mokeapp.core.waveforms import WAVEFORMS
from mokeapp.gui import theme

READ_OVERHEAD_S = 0.055  # Keithley read + DAQ capture per point at NPLC 1, no filter (measured ~50 ms)
FAST_READ_S = 0.045      # the Keithley estimate at NPLC 1, no filter, which READ_OVERHEAD_S already covers

COLORS = {"ok": "#2e9d4a", "pass": "#2e9d4a", "warn": "#d99a1e", "fail": "#d33f3f", "off": "#9a9a9a"}


class StatusLight(QLabel):
    def __init__(self, state: str = "off"):
        super().__init__()
        self.setFixedSize(16, 16)
        self.set_state(state)

    def set_state(self, state: str) -> None:
        self.setStyleSheet(f"background:{COLORS[state]}; border-radius:8px;")


def make_plot(title: str, xlabel: str, ylabel: str) -> pg.PlotWidget:
    w = pg.PlotWidget(title=title)
    w.setLabel("bottom", xlabel)
    w.setLabel("left", ylabel)
    w.showGrid(x=True, y=True, alpha=theme.current["grid"])
    w.setMinimumSize(250, 200)
    return w


class AutoScale(QWidget):
    """One switch for live plots: while ON every redraw re-fits the plots, so curves can't get lost
    off-screen. Zooming or panning a plot with the mouse turns it OFF (to look closely while
    measuring); clicking it turns it ON again and re-fits at once."""

    def __init__(self, plots: dict[pg.PlotWidget, str]):
        """plots: plot -> axis to auto-scale ('xy', or 'y' for a strip chart with a fixed time axis)."""
        super().__init__()
        self.plots = plots
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        self.button = QPushButton()
        self.button.setObjectName("autoscale")
        self.button.setCheckable(True)
        self.button.setChecked(True)
        self.hint = QLabel()
        self.hint.setObjectName("hint")
        h.addWidget(self.button)
        h.addWidget(self.hint)
        h.addStretch()
        self.button.toggled.connect(self._toggled)
        for p in plots:
            p.getViewBox().sigRangeChangedManually.connect(lambda *_: self.button.setChecked(False))
        self._toggled(True)

    def _toggled(self, on: bool) -> None:
        self.button.setText("Auto-scale: ON" if on else "Auto-scale: OFF")
        self.hint.setText("plots follow the data" if on else "you zoomed by hand — click to follow the data again")
        if on:
            self.apply()

    @property
    def on(self) -> bool:
        return self.button.isChecked()

    def apply(self) -> None:
        if not self.on:
            return
        for p, axes in self.plots.items():
            if axes == "y":
                p.enableAutoRange(axis=pg.ViewBox.YAxis)
            else:
                p.enableAutoRange()

    def fit(self) -> None:
        self.button.setChecked(True)
        self.apply()


class DriftCombo(QComboBox):
    """Drift correction choice: Off / Close each cycle / Saturation plateaus (robust)."""

    TIPS = {
        "off": "No correction: the signal as measured.",
        "endpoints": "Tilts each cycle so it ends where it started (uses only the first and last point).",
        "plateaus": "Fits the drift on all saturated points of the cycle (both sides, beginning to end), "
                    "so noise or a jump at the start does not tilt the loop. Needs a loop that saturates; "
                    "otherwise it falls back to 'Close each cycle'.",
    }

    def __init__(self):
        super().__init__()
        for key, name in DRIFT_METHODS.items():
            self.addItem(name, key)
            self.setItemData(self.count() - 1, self.TIPS[key], Qt.ToolTipRole)
        self.setToolTip("Laser drift correction, applied per cycle. The raw file is never modified.")

    @property
    def method(self) -> str:
        return self.currentData()

    def set_method(self, method) -> None:
        """Accepts the method name, or the bool that older files stored."""
        key = "endpoints" if method is True else ("off" if method in (False, None) else str(method))
        idx = self.findData(key)
        self.setCurrentIndex(idx if idx >= 0 else 0)


FIELD_MODES = ("mT", "Oe")
CURRENT_MODES = ("current", "current_set")     # measured (Kepco monitor) / programmed (V/R)


class XAxisToggle(QWidget):
    """'I measured | I programmed | mT | Oe' switch for the X axis.

    The choice lives in AppState, so Hysteresis and Analysis always show the same X. Each button is
    only enabled when that quantity exists here: measured current needs the Kepco monitors (or the
    column in the file), the field needs a calibration, a Hall probe or a field column. When the
    shared choice is not available, the plot falls back to the closest one.
    """

    def __init__(self, state, field_available=None, measured_available=None, programmed_available=None):
        super().__init__()
        self.state = state
        self.field_available = field_available or (lambda: state.field_available)
        self.measured_available = measured_available or (lambda: state.inst.monitors)
        self.programmed_available = programmed_available or (lambda: True)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(QLabel("X axis:  "))
        self.current_btn = QPushButton("I measured")
        self.current_btn.setToolTip("Magnet current measured at the Kepco current monitor")
        self.set_btn = QPushButton("I programmed")
        self.set_btn.setToolTip("Current asked for: programmed voltage / nominal magnet R (setup)")
        self.mt_btn = QPushButton("Field (mT)")
        self.oe_btn = QPushButton("Field (Oe)")
        self.buttons = {"current": self.current_btn, "current_set": self.set_btn,
                        "mT": self.mt_btn, "Oe": self.oe_btn}
        for (mode, b), name in zip(self.buttons.items(), ("segLeft", "segMid", "segMid", "segRight")):
            b.setObjectName(name)
            b.setCheckable(True)
            b.clicked.connect(lambda _, m=mode: self.state.set_x_mode(m))
            h.addWidget(b)
        state.x_mode_changed.connect(lambda _: self.refresh())
        state.calibration_changed.connect(self.refresh)
        self.refresh()

    def _available(self, mode: str) -> bool:
        if mode in FIELD_MODES:
            return self.field_available()
        return self.measured_available() if mode == "current" else self.programmed_available()

    @property
    def mode(self) -> str:
        """The X actually plotted: the shared choice, or the closest one available here."""
        want = self.state.x_mode
        for mode in (want, "current", "current_set"):
            if self._available(mode):
                return mode
        return "current_set"

    def refresh(self) -> None:
        for mode, b in self.buttons.items():
            b.setEnabled(self._available(mode))
            b.setChecked(self.mode == mode)
        tip = "" if self.field_available() else ("No field calibration: make one in the Calibration tab "
                                                 "(or wire a Hall probe to the DAQ, see Setup)")
        for mode in FIELD_MODES:
            self.buttons[mode].setToolTip(tip)
        if not self.measured_available():
            self.current_btn.setToolTip("No measured current: the Kepco monitors are not wired (Setup)")


def to_x(mode: str, field_mT):
    """Field in the unit of the X mode (Oe = 10 × mT, i.e. H in air from μ0H)."""
    return field_mT * MT_TO_OE if mode == "Oe" else field_mT


def x_label(mode: str) -> str:
    return {"mT": "Magnetic field μ0H (mT)", "Oe": "Magnetic field H (Oe)",
            "current_set": "Magnet current, programmed (A)"}.get(mode, "Magnet current (A)")


class WheelGuard(QObject):
    """App-wide event filter against accidental mouse-wheel changes.

    - Tab bars ignore the wheel (it used to flip between tabs).
    - Spin boxes and combo boxes only react to the wheel after being clicked; otherwise the wheel
      scrolls the panel instead of silently changing e.g. the magnet amplitude.
    """

    def eventFilter(self, obj, event):
        t = event.type()
        if t == QEvent.Polish and isinstance(obj, (QAbstractSpinBox, QComboBox)):
            obj.setFocusPolicy(Qt.StrongFocus)     # the wheel must not give them focus
        elif t == QEvent.Wheel:
            if isinstance(obj, QTabBar) or (isinstance(obj, (QAbstractSpinBox, QComboBox)) and not obj.hasFocus()):
                event.ignore()      # ignored + filtered: Qt passes the wheel on to the parent (scroll area)
                return True
        return False


class SweepParams(QGroupBox):
    """Waveform, amplitude (with max allowed label), points and settle time."""

    def __init__(self, state, title="Sweep"):
        super().__init__(title)
        self.state = state
        d = state.cfg.defaults
        form = QFormLayout(self)
        form.setVerticalSpacing(6)

        self.waveform = QComboBox()
        self.waveform.addItems(WAVEFORMS)
        self.waveform.setCurrentText(d.waveform)
        form.addRow("Waveform", self.waveform)

        self.amplitude = QDoubleSpinBox()
        self.amplitude.setDecimals(2)
        self.amplitude.setSingleStep(0.5)
        self.amplitude.setSuffix(" V")
        self.max_label = QLabel()
        self.max_label.setStyleSheet("color:#b23b3b;")
        h = QHBoxLayout()
        h.addWidget(self.amplitude, 1)
        h.addWidget(self.max_label)
        form.addRow("Magnet amplitude", h)
        self.current_label = QLabel()
        # must wrap: a long unwrapped label sets the panel's minimum width, and when a scroll bar appears
        # the right edge of every box gets clipped
        self.current_label.setWordWrap(True)
        form.addRow("", self.current_label)

        self.points = QSpinBox()
        self.points.setRange(4, 10000)
        self.points.setValue(d.points_per_cycle)
        form.addRow("Points per cycle", self.points)

        self.settle = QDoubleSpinBox()
        self.settle.setRange(0, 10000)
        self.settle.setDecimals(0)
        self.settle.setSuffix(" ms")
        self.settle.setValue(d.settle_ms)
        form.addRow("Settle time", self.settle)

        self.time_label = QLabel()
        form.addRow("Time per cycle", self.time_label)

        self.update_limits()
        self.amplitude.setValue(min(d.amplitude_V, state.max_voltage))
        for sig in (self.amplitude.valueChanged, self.points.valueChanged, self.settle.valueChanged):
            sig.connect(self._update_labels)
        state.limits_changed.connect(self.update_limits)
        state.calibration_changed.connect(self._update_labels)
        self._update_labels()

    def update_limits(self) -> None:
        vmax = self.state.max_voltage
        self.amplitude.setRange(0.0, vmax)
        self.max_label.setText(f"max ±{vmax:.1f} V")
        self.max_label.setToolTip(f"Kepco current limit: {vmax / self.state.resistance:.1f} A with the magnet's "
                                  f"R = {self.state.resistance:.2f} Ω")
        self._update_labels()

    def _update_labels(self) -> None:
        i = self.amplitude.value() / self.state.resistance
        text = f"≈ ±{i:.2f} A"
        cal = self.state.calibration
        if cal is not None:
            b = max(abs(float(cal.field(i))), abs(float(cal.field(-i))))
            text += f" · ±{b:.3g} mT · ±{b * MT_TO_OE:.3g} Oe"
            if not (cal.in_range(i) and cal.in_range(-i)):
                text += " (extrapolated)"
        self.current_label.setText(text)
        self.current_label.setToolTip(f"Magnet R = {self.state.resistance:.2f} Ω"
                                      + (f"; calibration: {cal.summary()}" if cal else ""))
        read_s = self.read_time_s()
        t = self.cycle_time_s()
        self.time_label.setText(f"≈ {t:.0f} s" + (f"  ({read_s:.2f} s per point)" if read_s > 0.2 else ""))

    def read_time_s(self) -> float:
        """Keithley read + DAQ capture per point; slow Keithley settings (high NPLC, filter) add to it."""
        return READ_OVERHEAD_S + max(0.0, getattr(self.state.inst.keithley, "read_time_s", 0.0) - FAST_READ_S)

    def cycle_time_s(self) -> float:
        return self.points.value() * (self.settle.value() / 1000 + self.read_time_s())

    def values(self) -> dict:
        return {"waveform": self.waveform.currentText(), "amplitude_V": self.amplitude.value(),
                "points_per_cycle": self.points.value(), "settle_ms": self.settle.value()}
