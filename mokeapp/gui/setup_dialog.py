"""Instrument setup dialog: pick the multimeter and the NI DAQ from what is connected, the Kepco limits,
and whether the Kepco monitors / a Hall probe are wired. Saves the setup YAML (one per MOKE / PC)."""
import copy
import logging
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from mokeapp.config import ConfigError, SetupConfig, _validate, save_config
from mokeapp.drivers import discovery
from mokeapp.drivers.multimeter import PROFILES, detect_profile

log = logging.getLogger(__name__)

NO_HALL = "(none)"


def _spin(lo, hi, value, decimals=3, suffix="", step=None) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setValue(value)
    if suffix:
        s.setSuffix(suffix)
    if step:
        s.setSingleStep(step)
    return s


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("hint")
    lbl.setWordWrap(True)
    return lbl


class SetupDialog(QDialog):
    """Edits a copy of the setup; `saved_path` is set when the user saved it."""

    def __init__(self, cfg: SetupConfig, simulate: bool, config_dir: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Instrument setup")
        self.cfg = copy.deepcopy(cfg)
        self.simulate = simulate
        self.config_dir = Path(config_dir)
        self.saved_path: Path | None = None
        self.daqs: dict[str, discovery.DaqInfo] = {}
        c = self.cfg

        body = QWidget()
        v = QVBoxLayout(body)

        box = QGroupBox("Setup")
        f = QFormLayout(box)
        self.name = QLineEdit(c.name)
        f.addRow("Name", self.name)
        exists = c.path is not None and Path(c.path).exists()
        f.addRow("File", QLabel(Path(c.path).name if exists else "(new, not saved yet)"))
        v.addWidget(box)

        # --- multimeter
        box = QGroupBox("Multimeter (reads the photodetector)")
        f = QFormLayout(box)
        self.address = QComboBox()
        self.address.setEditable(True)
        self.address.addItem(c.keithley.address)
        scan = QPushButton("Scan")
        ident = QPushButton("Identify")
        h = QHBoxLayout()
        h.addWidget(self.address, 1)
        h.addWidget(scan)
        h.addWidget(ident)
        f.addRow("Address", h)
        self.idn = _hint("Scan lists what VISA sees (GPIB0::n::INSTR, USB0::...). Identify asks the model.")
        f.addRow("", self.idn)
        self.profile = QComboBox()
        self.profile.addItem("Auto (from the model)", "auto")
        for p in PROFILES.values():
            self.profile.addItem(p.name, p.key)
        self.profile.setCurrentIndex(max(0, self.profile.findData(c.keithley.profile)))
        f.addRow("Commands", self.profile)
        self.terminals = QComboBox()
        for key, text in (("rear", "Rear"), ("front", "Front"), ("any", "Don't check")):
            self.terminals.addItem(text, key)
        self.terminals.setCurrentIndex(max(0, self.terminals.findData(c.keithley.terminals)))
        f.addRow("Photodetector on", self.terminals)
        v.addWidget(box)
        scan.clicked.connect(self.scan_visa)
        ident.clicked.connect(self.identify)

        # --- DAQ
        box = QGroupBox("NI DAQ (drives the Kepco)")
        f = QFormLayout(box)
        self.device = QComboBox()
        dscan = QPushButton("Scan")
        h = QHBoxLayout()
        h.addWidget(self.device, 1)
        h.addWidget(dscan)
        f.addRow("Device", h)
        self.daq_info = _hint("")
        f.addRow("", self.daq_info)
        self.ao = QComboBox()
        f.addRow("Output to Kepco", self.ao)
        self.monitors = QCheckBox("Kepco current/voltage monitors are wired to the DAQ")
        self.monitors.setChecked(c.daq.monitors)
        self.monitors.setToolTip("With monitors: real magnet current, resistance and safety checks.\n"
                                 "Without: the current is the programmed V/R and those checks are off.")
        f.addRow("", self.monitors)
        self.ai_current = QComboBox()
        self.ai_voltage = QComboBox()
        self.terminal = QComboBox()
        f.addRow("Current monitor input", self.ai_current)
        f.addRow("Voltage monitor input", self.ai_voltage)
        f.addRow("Input configuration", self.terminal)
        f.addRow("", _hint("In DIFF each input pairs with its return channel (e.g. ai0 with ai4 on a USB-6002; "
                           "see the DAQ's pinout)."))
        self.ai_hall = QComboBox()
        self.hall_scale = _spin(-1e6, 1e6, c.daq.hall_mT_per_V, 4, " mT/V")
        self.hall_offset = _spin(-1e4, 1e4, c.daq.hall_offset_mT, 4, " mT")
        f.addRow("Hall probe input", self.ai_hall)
        f.addRow("Hall probe scale", self.hall_scale)
        f.addRow("Hall probe offset", self.hall_offset)
        v.addWidget(box)
        dscan.clicked.connect(self.scan_daq)
        self.device.currentIndexChanged.connect(lambda _: self._device_changed())
        self.monitors.toggled.connect(lambda _: self._enable())
        self.ai_hall.currentIndexChanged.connect(lambda _: self._enable())

        # --- Kepco
        box = QGroupBox("Kepco BOP and magnet")
        f = QFormLayout(box)
        k = c.kepco
        self.k_model = QLineEdit(k.model)
        self.gain = _spin(-1000, 1000, k.gain, 3, " V/V")
        self.gain.setToolTip("Magnet volts per programming volt (negative if the Kepco inverts)")
        self.v_max = _spin(0.1, 1000, k.v_max, 2, " V")
        self.i_max = _spin(0.01, 100, k.i_max, 3, " A")
        self.r_nom = _spin(0.001, 1000, k.magnet_resistance_ohm, 4, " Ω")
        self.r_nom.setToolTip("Magnet resistance: sets the current limit and the programmed current I = V/R")
        self.slew = _spin(0.1, 10000, k.slew_rate_V_per_s, 1, " V/s")
        self.i_mon = _spin(1e-4, 1000, k.current_monitor_A_per_V, 4, " A/V")
        self.v_mon = _spin(1e-4, 1000, k.voltage_monitor_V_per_V, 4, " V/V")
        f.addRow("Model", self.k_model)
        f.addRow("Gain", self.gain)
        f.addRow("Max voltage", self.v_max)
        f.addRow("Max current", self.i_max)
        f.addRow("Magnet resistance", self.r_nom)
        f.addRow("Max slew rate", self.slew)
        f.addRow("Current monitor scale", self.i_mon)
        f.addRow("Voltage monitor scale", self.v_mon)
        v.addWidget(box)

        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        buttons = QDialogButtonBox()
        self.save_btn = buttons.addButton("Save", QDialogButtonBox.AcceptRole)
        self.save_as_btn = buttons.addButton("Save as new setup...", QDialogButtonBox.ActionRole)
        buttons.addButton(QDialogButtonBox.Cancel)
        self.save_btn.clicked.connect(lambda: self.save(new=False))
        self.save_as_btn.clicked.connect(lambda: self.save(new=True))
        buttons.rejected.connect(self.reject)
        root = QVBoxLayout(self)
        root.addWidget(scroll, 1)
        root.addWidget(buttons)
        avail = QApplication.primaryScreen().availableGeometry()
        self.resize(min(620, avail.width() - 40), min(760, avail.height() - 60))

        # offer the configured device even before scanning, then look for the real ones
        self._fill_device_list([])
        self.scan_daq(quiet=True)
        self._enable()

    # ------------------------------------------------------------ discovery
    def scan_visa(self):
        try:
            found = discovery.visa_resources(self.simulate)
        except Exception as e:
            QMessageBox.warning(self, "Scan", f"VISA could not list instruments:\n{e}\n\nIs NI-VISA installed?")
            return
        current = self.address.currentText()
        self.address.clear()
        self.address.addItems(found)
        self.address.setCurrentText(current if current in found or not found else found[0])
        self.idn.setText(f"Found: {', '.join(found)}" if found else "VISA sees no instruments: check cables, "
                                                                     "power and the GPIB/USB driver.")

    def identify(self) -> str | None:
        address = self.address.currentText().strip()
        try:
            idn = discovery.visa_identify(address, simulate=self.simulate)
        except Exception as e:
            self.idn.setText(f"<span style='color:#b91c1c'>No answer at {address}: {e}</span>")
            return None
        key = detect_profile(idn)
        self.idn.setText(f"<b>{idn}</b><br>Commands: {PROFILES[key].name}")
        return idn

    def scan_daq(self, quiet: bool = False):
        try:
            found = discovery.daq_devices(self.simulate)
        except Exception as e:
            if not quiet:
                QMessageBox.warning(self, "Scan", f"NI-DAQmx could not list devices:\n{e}\n\n"
                                                  "Is NI-DAQmx installed?")
            found = []
        self.daqs = {d.name: d for d in found}
        self._fill_device_list(found)

    def _fill_device_list(self, found):
        current = self.device.currentData() or self.cfg.daq.device
        self.device.blockSignals(True)
        self.device.clear()
        for d in found:
            self.device.addItem(f"{d.name}  —  {d.product_type}", d.name)
        if current not in self.daqs:
            self.device.addItem(f"{current}  —  not connected now", current)
        self.device.setCurrentIndex(max(0, self.device.findData(current)))
        self.device.blockSignals(False)
        self._device_changed()

    def _device_changed(self):
        name = self.device.currentData()
        info = self.daqs.get(name)
        c = self.cfg.daq
        if info is not None:
            ao, ai, terms = info.ao_channels, info.ai_channels, info.terminals
            self.daq_info.setText(info.describe())
        else:
            ao = [c.ao_channel]
            ai = sorted({c.ai_current, c.ai_voltage} | ({c.ai_hall} if c.ai_hall else set()))
            terms = ["DIFF", "RSE", "NRSE"]
            self.daq_info.setText("Not connected now: the saved channels are kept. Scan after plugging it in.")
        same = name == c.device     # keep the saved channels when re-scanning the same device
        for combo, items, saved in ((self.ao, ao, c.ao_channel), (self.ai_current, ai, c.ai_current),
                                    (self.ai_voltage, ai, c.ai_voltage), (self.terminal, terms, c.ai_terminal),
                                    (self.ai_hall, [NO_HALL] + ai, c.ai_hall or NO_HALL)):
            combo.clear()
            combo.addItems(items)
            if same and saved in items:
                combo.setCurrentText(saved)
        if not same and len(ai) > 1:      # a different DAQ: sensible first guess
            self.ai_voltage.setCurrentIndex(1)
        self._enable()

    def _enable(self):
        mon = self.monitors.isChecked()
        for w in (self.ai_current, self.ai_voltage, self.i_mon, self.v_mon):
            w.setEnabled(mon)
        hall = self.ai_hall.currentText() not in ("", NO_HALL)
        for w in (self.hall_scale, self.hall_offset):
            w.setEnabled(hall)
        self.terminal.setEnabled(mon or hall)

    # ------------------------------------------------------------ save
    def _build(self) -> SetupConfig:
        c = copy.deepcopy(self.cfg)
        c.name = self.name.text().strip() or "MOKE"
        m = c.keithley
        m.address = self.address.currentText().strip()
        m.profile = self.profile.currentData()
        m.terminals = self.terminals.currentData()
        d = c.daq
        d.device = self.device.currentData() or ""
        info = self.daqs.get(d.device)
        if info is not None:
            d.model = info.product_type
        d.ao_channel = self.ao.currentText()
        d.monitors = self.monitors.isChecked()
        d.ai_current = self.ai_current.currentText()
        d.ai_voltage = self.ai_voltage.currentText()
        d.ai_terminal = self.terminal.currentText() or d.ai_terminal
        hall = self.ai_hall.currentText()
        d.ai_hall = "" if hall in ("", NO_HALL) else hall
        d.hall_mT_per_V = self.hall_scale.value() if d.ai_hall else 0.0
        d.hall_offset_mT = self.hall_offset.value() if d.ai_hall else 0.0
        k = c.kepco
        k.model = self.k_model.text().strip() or k.model
        k.gain, k.v_max, k.i_max = self.gain.value(), self.v_max.value(), self.i_max.value()
        k.magnet_resistance_ohm, k.slew_rate_V_per_s = self.r_nom.value(), self.slew.value()
        k.current_monitor_A_per_V, k.voltage_monitor_V_per_V = self.i_mon.value(), self.v_mon.value()
        return c

    def problems(self, c: SetupConfig) -> tuple[list[str], list[str]]:
        """(errors that block saving, warnings to confirm)."""
        errors, warnings = [], []
        try:
            _validate(c)
        except ConfigError as e:
            errors.append(str(e))
        d = c.daq
        if not d.device:
            errors.append("Choose the NI DAQ device.")
        if d.monitors and d.ai_current == d.ai_voltage:
            errors.append("The current and voltage monitors must use different inputs.")
        if d.ai_hall and d.monitors and d.ai_hall in (d.ai_current, d.ai_voltage):
            errors.append("The Hall probe must use its own input.")
        if d.ai_hall and d.hall_mT_per_V == 0:
            errors.append("Set the Hall probe scale (mT per volt of its analog output).")
        info = self.daqs.get(d.device)
        if info is None:
            warnings.append(f"The DAQ {d.device} is not connected now, so its channels were not checked.")
        else:
            if d.ao_channel not in info.ao_channels:
                errors.append(f"{d.ao_channel} is not an analog output of {d.device}.")
            if (d.monitors or d.ai_hall) and d.ai_terminal not in info.terminals:
                errors.append(f"{d.device} does not support {d.ai_terminal} inputs ({'/'.join(info.terminals)}).")
        if not d.monitors:
            warnings.append("Without the Kepco monitors the magnet current is not measured: the current and "
                            "overheating checks are off, and the X axis uses the programmed current V/R.")
        return errors, warnings

    def _target(self, new: bool, name: str) -> Path | None:
        if not new and self.cfg.path is not None and Path(self.cfg.path).exists():
            return Path(self.cfg.path)
        text, ok = QInputDialog.getText(self, "Save setup", "Name of the setup file (e.g. moke_lab2):",
                                        text=re.sub(r"[^\w\-]+", "_", name.lower()).strip("_") or "moke_setup")
        if not ok or not text.strip():
            return None
        path = self.config_dir / f"{re.sub(r'[^\w\-]+', '_', text.strip())}.yaml"
        if path.exists() and QMessageBox.question(self, "Save setup", f"{path.name} exists. Replace it?") \
                != QMessageBox.Yes:
            return None
        return path

    def save(self, new: bool):
        c = self._build()
        errors, warnings = self.problems(c)
        if errors:
            QMessageBox.warning(self, "Setup", "Cannot save:\n\n• " + "\n• ".join(errors))
            return
        if warnings and QMessageBox.question(self, "Setup", "• " + "\n• ".join(warnings) + "\n\nSave anyway?") \
                != QMessageBox.Yes:
            return
        path = self._target(new, c.name)
        if path is None:
            return
        try:
            save_config(c, path)
        except OSError as e:
            QMessageBox.critical(self, "Setup", f"Could not write {path}:\n{e}")
            return
        self.cfg = c
        self.saved_path = path
        self.accept()
