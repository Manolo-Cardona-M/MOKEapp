"""Main window: header with status, tabs; restarts itself when a new instrument setup is saved."""
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QLocale, QProcess, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QTabWidget,
                               QVBoxLayout, QWidget)

import mokeapp
from mokeapp.gui import theme
from mokeapp.gui.state import AppState
from mokeapp.gui.tab_analysis import AnalysisTab
from mokeapp.gui.tab_calibration import CalibrationTab
from mokeapp.gui.tab_instruments import InstrumentsTab
from mokeapp.gui.tab_measure import ContinuousTab, HysteresisTab
from mokeapp.gui.widgets import WheelGuard

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, cfg, simulate: bool, data_folder: Path, prefs=None, config_dir: Path | None = None):
        """prefs: QSettings remembering choices on this PC (None in tests). config_dir: where setups live."""
        # decimal point everywhere, whatever the Windows region (Spanish shows "2,50" otherwise).
        # Must happen before any widget exists: widgets copy the default locale when created.
        locale = QLocale(QLocale.English, QLocale.UnitedStates)
        locale.setNumberOptions(QLocale.OmitGroupSeparator | QLocale.RejectGroupSeparator)
        QLocale.setDefault(locale)
        super().__init__()
        self.setLocale(locale)
        self.simulate = simulate
        self.prefs = prefs
        self.state = AppState(cfg, simulate, prefs)
        self._wheel_guard = WheelGuard(self)
        QApplication.instance().installEventFilter(self._wheel_guard)
        self.setWindowTitle(f"MOKEapp v{mokeapp.__version__} — {cfg.name}"
                            + ("  [SIMULATION]" if simulate else ""))
        # fit the screen's work area (this PC: 1366x720 usable); main.py opens it maximized
        avail = QGuiApplication.primaryScreen().availableGeometry()
        self.setMinimumSize(1000, 600)
        self.resize(min(1400, avail.width()), min(850, avail.height() - 40))

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        header = QFrame()
        header.setObjectName("header")
        top = QHBoxLayout(header)
        top.setContentsMargins(16, 8, 16, 8)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("MOKEapp")
        title.setObjectName("appTitle")
        subtitle = QLabel(f"{cfg.name}  ·  v{mokeapp.__version__}" + ("  ·  SIMULATION" if simulate else ""))
        subtitle.setObjectName("appSubtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        top.addLayout(titles)
        top.addSpacing(24)
        self.banner = QLabel()
        self.banner.setObjectName("statusPill")
        top.addWidget(self.banner)
        top.addStretch(1)
        # no global STOP here: each measuring tab has its own red Stop (magnet ramps to 0)
        self.cal_label = QLabel()
        self.cal_label.setObjectName("appSubtitle")
        top.addWidget(self.cal_label)
        v.addWidget(header)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.instruments = InstrumentsTab(self.state, config_dir)
        self.calibration = CalibrationTab(self.state)
        self.continuous = ContinuousTab(self.state, data_folder)
        self.hysteresis = HysteresisTab(self.state, data_folder)
        self.analysis = AnalysisTab(self.state, data_folder)
        self.tabs.addTab(self.instruments, "Instruments")
        self.tabs.addTab(self.calibration, "Calibration")
        self.tabs.addTab(self.continuous, "Continuous")
        self.tabs.addTab(self.hysteresis, "Hysteresis")
        self.tabs.addTab(self.analysis, "Analysis")
        v.addWidget(self.tabs)
        self.setCentralWidget(central)

        self.state.open_in_analysis.connect(self._open_in_analysis)
        for sig in (self.state.connection_changed, self.state.diagnostics_changed, self.state.busy_changed):
            sig.connect(lambda *_: self._update_banner())
        self.state.calibration_changed.connect(self._update_cal_label)
        self.instruments.setup_saved.connect(self._setup_saved)
        self._update_banner()
        self._update_cal_label()
        if cfg.path is None or not Path(cfg.path).exists():     # first start on this PC: no setup yet
            QTimer.singleShot(0, self._first_setup)

    def _first_setup(self):
        QMessageBox.information(self, "MOKEapp", "No instrument setup on this PC yet.\n\nChoose the multimeter, "
                                                 "the NI DAQ and the Kepco settings, then save the setup.")
        self.instruments.open_setup()

    def _setup_saved(self, path: Path):
        if self.prefs is not None:
            self.prefs.setValue("setup_path", str(Path(path).resolve()))
        ans = QMessageBox.question(self, "Setup saved", f"{Path(path).name} saved.\n\nRestart MOKEapp now to use it?")
        if ans == QMessageBox.Yes:
            self.restart(path)

    def restart(self, setup_path: Path) -> None:
        """Start a fresh MOKEapp with this setup and close this one (instruments released, magnet at 0 V)."""
        script = Path(sys.argv[0]).resolve()
        args = [str(script), "--config", str(setup_path)] + (["--sim"] if self.simulate else [])
        if QProcess.startDetached(sys.executable, args)[0]:
            log.info("Restarting with %s", setup_path)
            self.close()
        else:
            QMessageBox.warning(self, "Restart", "Could not restart automatically: close MOKEapp and open it again.")

    def _update_cal_label(self):
        cal = self.state.calibration
        self.cal_label.setText(f"Field calibration: {cal.coil}  ({cal.date[:10]})" if cal
                               else "No field calibration")

    def _update_banner(self):
        s = self.state
        if not s.inst.all_connected:
            text, state = "Instruments not connected", "off"
        elif s.busy:
            text, state = "Busy: magnet in use", "warn"
        elif s.diagnostics_ok:
            text, state = "Ready to measure", "ok"
        elif s.override:
            text, state = "Diagnostics overridden", "warn"
        else:
            text, state = "Connected — run diagnostics", "warn"
        self.banner.setText(f"●  {text}")
        self.banner.setProperty("state", state)
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)
        if not theme.current.get("styled"):
            color = {"ok": "#2e9d4a", "warn": "#d99a1e", "off": "#9a9a9a"}[state]
            self.banner.setStyleSheet(f"color:{color}; font-size:14px; font-weight:bold;")

    def _open_in_analysis(self, measurement):
        if measurement is not None:
            self.analysis.load(measurement)
            self.tabs.setCurrentWidget(self.analysis)

    def closeEvent(self, event):
        if self.state.busy:
            ans = QMessageBox.question(self, "Quit", "A measurement is running. Stop it, ramp the magnet "
                                       "to 0 and quit?")
            if ans != QMessageBox.Yes:
                event.ignore()
                return
        for tab in (self.continuous, self.hysteresis, self.instruments, self.calibration):
            tab.shutdown()
        self.state.inst.close()
        log.info("Application closed, magnet at 0 V")
        event.accept()
