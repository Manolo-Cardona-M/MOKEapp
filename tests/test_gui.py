"""End-to-end GUI flow with simulated instruments (offscreen, no window shown)."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtWidgets import (QApplication, QFileDialog, QInputDialog, QMessageBox,  # noqa: E402
                               QScrollArea, QTableWidgetItem)

from mokeapp.core import storage  # noqa: E402


def wait_until(app, cond, timeout=30.0):
    t0 = time.monotonic()
    while not cond():
        app.processEvents()
        if time.monotonic() - t0 > timeout:
            raise TimeoutError
        time.sleep(0.01)


@pytest.fixture
def window(cfg, tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    dialogs = []
    for name in ("critical", "warning"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: dialogs.append(a[2])))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    from mokeapp.gui.main_window import MainWindow
    win = MainWindow(cfg, simulate=True, data_folder=tmp_path, config_dir=tmp_path)
    win.state.inst.sim_lab.read_delay_s = 0
    yield app, win
    win.close()
    assert dialogs == []


class FakePrefs:
    """Stands in for QSettings (nothing written to the registry)."""
    def __init__(self):
        self.d = {}

    def value(self, key, default=None):
        return self.d.get(key, default)

    def setValue(self, key, value):
        self.d[key] = value


def test_without_monitors_the_x_axis_is_the_programmed_current(cfg, tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    cfg.daq.monitors = False
    from mokeapp.gui.main_window import MainWindow
    win = MainWindow(cfg, simulate=True, data_folder=tmp_path, config_dir=tmp_path)
    win.state.inst.sim_lab.read_delay_s = 0
    try:
        hyst = win.hysteresis
        assert win.state.x_mode == "current_set"
        assert not hyst.xaxis.current_btn.isEnabled() and hyst.xaxis.set_btn.isChecked()
        connect_and_diagnose(app, win)
        assert "monitors not wired" in win.instruments.infos["kepco"].text()
        assert win.state.diagnostics_ok                      # passes, with a warning
        hyst.cycles.setValue(1)
        hyst.params.points.setValue(20)
        hyst.params.settle.setValue(0)
        hyst.start()
        wait_until(app, lambda: hyst.thread is None)
        x, _ = hyst.avg_curve.getData()
        assert np.isfinite(x).all() and x.max() == pytest.approx(
            hyst.params.amplitude.value() / cfg.kepco.magnet_resistance_ohm, rel=1e-6)
        assert "programmed" in hyst.cycle_plot.getAxis("bottom").labelText
        win.instruments._show_poll((None, 0.2, None, None))     # live readout without monitors
        assert "(set)" in win.instruments.live_v.text() and "(nominal)" in win.instruments.live_r.text()
    finally:
        win.close()


def test_x_switch_programmed_current_with_monitors(window):
    app, win = window
    connect_and_diagnose(app, win)
    hyst = win.hysteresis
    hyst.cycles.setValue(1)
    hyst.params.points.setValue(20)
    hyst.params.settle.setValue(0)
    hyst.start()
    wait_until(app, lambda: hyst.thread is None)
    hyst.xaxis.set_btn.click()
    assert win.state.x_mode == "current_set"
    x, _ = hyst.avg_curve.getData()
    np.testing.assert_allclose(x, win.state.last_measurement.column(storage.COL_ISET))
    hyst.xaxis.current_btn.click()
    x, _ = hyst.avg_curve.getData()
    np.testing.assert_allclose(x, win.state.last_measurement.column(storage.COL_CURRENT))


def test_x_mode_is_remembered(cfg, tmp_path):
    from mokeapp.gui.state import AppState
    prefs = FakePrefs()
    s = AppState(cfg, simulate=True, prefs=prefs)
    assert s.x_mode == "current"                # monitors, no calibration
    s.set_x_mode("current_set")
    assert AppState(cfg, simulate=True, prefs=prefs).x_mode == "current_set"


def test_setup_dialog_builds_and_saves_a_new_setup(cfg, tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[2])))
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("moke lab2", True)))
    from mokeapp.config import load_config
    from mokeapp.gui.setup_dialog import SetupDialog
    dlg = SetupDialog(cfg, simulate=True, config_dir=tmp_path)
    assert {"Dev1", "Dev2"} <= set(dlg.daqs)                # simulated scan
    dlg.scan_visa()
    dlg.address.setCurrentText("USB0::0x05E6::0x6500::04400000::INSTR")
    assert "DMM6500" in dlg.identify()
    dlg.device.setCurrentIndex(dlg.device.findData("Dev2"))
    assert dlg.ao.currentText() == "Dev2/ao0" and "USB-6421" in dlg.daq_info.text()
    dlg.monitors.setChecked(True)
    dlg.ai_voltage.setCurrentText(dlg.ai_current.currentText())
    dlg.save(new=True)                                       # same input twice: refused
    assert dlg.saved_path is None and "different inputs" in warnings[-1]
    dlg.monitors.setChecked(False)
    dlg.terminals.setCurrentIndex(dlg.terminals.findData("front"))
    dlg.r_nom.setValue(3.3)
    dlg.save(new=True)
    assert dlg.saved_path == tmp_path / "moke_lab2.yaml"
    c = load_config(dlg.saved_path)
    assert c.keithley.address.startswith("USB0::0x05E6") and c.keithley.terminals == "front"
    assert c.daq.device == "Dev2" and c.daq.model == "USB-6421" and not c.daq.monitors
    assert c.kepco.magnet_resistance_ohm == pytest.approx(3.3)
    assert cfg.daq.device == "Dev1"                          # the running setup was not touched


def test_saving_a_setup_offers_a_restart(window, monkeypatch, tmp_path):
    app, win = window
    prefs = FakePrefs()
    win.prefs = prefs
    restarted = []
    monkeypatch.setattr(win, "restart", lambda p: restarted.append(p))
    win.instruments.setup_saved.emit(tmp_path / "x.yaml")
    assert prefs.d["setup_path"] == str((tmp_path / "x.yaml").resolve()) and restarted == [tmp_path / "x.yaml"]
    assert not win.instruments.setup_btn.isEnabled() or not win.state.inst.all_connected


def test_load_an_older_calibration_from_file(window, tmp_path):
    from mokeapp.core import calibration
    app, win = window
    lib, tab = win.state.library, win.calibration
    old = calibration.fit("Coil A", [-1, 0, 1], [-40, 0, 40])
    old.date = "2026-01-01T00:00:00"
    new = calibration.fit("Coil A", [-1, 0, 1], [-50, 0, 50])
    lib.add(old)
    f_old = lib.file_of(old)
    lib.add(new)
    win.state.refresh_calibration()
    assert win.state.calibration.slope == pytest.approx(50)
    tab.load_file(str(f_old))                     # go back to the older one
    assert win.state.calibration.slope == pytest.approx(40)
    assert calibration.CalibrationLibrary(lib.folder).active.slope == pytest.approx(40)   # remembered
    assert "Coil A" in win.cal_label.text()


def test_new_pc_opens_the_setup_dialog(cfg, tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    from mokeapp.gui.main_window import MainWindow
    from mokeapp.gui.tab_instruments import InstrumentsTab
    opened = []
    monkeypatch.setattr(InstrumentsTab, "open_setup", lambda self: opened.append(True))
    cfg.path = None
    win = MainWindow(cfg, simulate=True, data_folder=tmp_path, config_dir=tmp_path)
    try:
        wait_until(app, lambda: opened, timeout=5)
    finally:
        win.close()


def test_full_flow(window, tmp_path):
    app, win = window
    ins, hyst, ana = win.instruments, win.hysteresis, win.analysis
    assert not hyst.start_btn.isEnabled()

    ins.do_connect()
    wait_until(app, lambda: not win.state.busy)
    assert win.state.inst.all_connected
    assert not hyst.start_btn.isEnabled()          # diagnostics not run yet

    ins.do_diagnostics()
    wait_until(app, lambda: win.state.diagnostics is not None and not win.state.busy)
    assert win.state.diagnostics_ok
    assert hyst.start_btn.isEnabled()
    assert "max ±17.5 V" in hyst.params.max_label.text()

    # not saving: runs, writes nothing
    hyst.cycles.setValue(1)
    hyst.params.points.setValue(20)
    hyst.params.settle.setValue(0)
    assert "not saving" in hyst.start_btn.text()
    hyst.start()
    wait_until(app, lambda: hyst.thread is None)
    assert list(tmp_path.glob("*.csv")) == []
    assert hyst.open_btn.isEnabled()                # still in memory for the Analysis tab

    hyst.save.setChecked(True)
    assert "(saving)" in hyst.start_btn.text()
    hyst.save.sample.setText("gui test")
    hyst.cycles.setValue(2)
    hyst.params.points.setValue(40)
    hyst.params.settle.setValue(0)
    hyst.params.amplitude.setValue(3.0)
    hyst.start()
    assert win.state.busy and not win.continuous.start_btn.isEnabled()
    assert not hyst.save.isEnabled()                # save choice is locked while measuring
    wait_until(app, lambda: hyst.thread is None)
    assert "Finished: 2 complete" in hyst.progress.text()
    assert len(list(tmp_path.glob("*.csv"))) == 2
    assert win.state.inst.sim_lab.ao == 0

    hyst.open_btn.click()
    assert win.tabs.currentWidget() is ana
    ana.compute()
    assert "Hc (half width)" in ana.result.text()
    ana.mr.setChecked(True)
    assert "Mr/Ms" in ana.result.text()

    cont = win.continuous
    assert cont.save is None and cont.start_btn.text() == "Start"   # continuous never saves
    cont.params.points.setValue(20)
    cont.params.settle.setValue(0)
    n_files = len(list(tmp_path.glob("*.csv")))
    cont.start()
    wait_until(app, lambda: len(cont.t) > 50)
    app.processEvents()
    x, _ = cont.signal_curve.getData()
    assert x.max() == 0 and x.min() >= -cont.window.value()   # strip chart: now at 0, older to the left
    _, v = cont.set_curve.getData()
    assert v.min() == pytest.approx(-cont.params.amplitude.value())   # the drive waveform is shown
    cont.stop_btn.click()
    wait_until(app, lambda: cont.thread is None)
    assert win.state.inst.sim_lab.ao == 0
    assert len(list(tmp_path.glob("*.csv"))) == n_files


def test_unsaved_measurement_can_be_analysed_and_saved(window, tmp_path, monkeypatch):
    app, win = window
    ins, hyst, ana = win.instruments, win.hysteresis, win.analysis
    ins.do_connect()
    wait_until(app, lambda: not win.state.busy)
    ins.do_diagnostics()
    wait_until(app, lambda: win.state.diagnostics is not None and not win.state.busy)
    hyst.cycles.setValue(2)
    hyst.params.points.setValue(40)
    hyst.params.settle.setValue(0)
    hyst.params.amplitude.setValue(3.0)
    hyst.drift.set_method("plateaus")
    hyst.start()                                    # 'Save data' is off
    wait_until(app, lambda: hyst.thread is None)
    assert list(tmp_path.glob("*.csv")) == []
    assert hyst.open_btn.isEnabled() and ana.last_btn.isEnabled()

    hyst.open_btn.click()
    assert win.tabs.currentWidget() is ana and ana.m.path is None
    assert ana.drift.method == "plateaus"           # same processing as the run
    assert "Measured drift" in ana.drift_info.text()
    assert ana.save_raw_btn.isEnabled()

    targets = iter([tmp_path / "curve.csv", tmp_path / "raw.csv"])
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(next(targets)), "")))
    ana.norm.setChecked(True)
    ana.compute()
    ana.save_curve()
    curve = storage.load_measurement(tmp_path / "curve.csv")
    assert curve.data.shape[0] == 40 and curve.y_column == "signal_V_normalized [Y]"
    assert curve.metadata["analysis"]["normalized"] and curve.metadata["sample"] == ""
    assert abs(curve.column(curve.y_column)).max() == pytest.approx(1, abs=0.1)

    # symmetrization: odd part, centred loop, even part shown on demand, recorded when saving
    ana.sym.setChecked(True)
    assert ana.show_even.isEnabled() and "Odd part shown" in ana.x_note.text()
    ana.show_even.setChecked(True)
    xe, _ = ana.even_curve.getData()
    assert xe is not None and len(xe) > 0
    ana.compute()
    assert "(0 by symmetrization)" in ana.result.text()
    targets = iter([tmp_path / "sym.csv"])
    ana.save_curve()
    sym = storage.load_measurement(tmp_path / "sym.csv")
    assert sym.metadata["analysis"]["symmetrized"] and sym.y_column == "signal_V_normalized_odd [Y]"
    ana.sym.setChecked(False)
    assert ana.even_curve.getData()[0] is None or len(ana.even_curve.getData()[0]) == 0

    targets = iter([tmp_path / "raw.csv"])
    ana.save_raw()
    raw = storage.load_measurement(tmp_path / "raw.csv")
    assert raw.data.shape[0] == 80 and set(raw.column(storage.COL_CYCLE)) == {0, 1}
    assert win.state.last_measurement.path == tmp_path / "raw.csv" and not ana.save_raw_btn.isEnabled()


def connect_and_diagnose(app, win):
    win.instruments.do_connect()
    wait_until(app, lambda: not win.state.busy)
    win.instruments.do_diagnostics()
    wait_until(app, lambda: win.state.diagnostics is not None and not win.state.busy)


def test_calibration_flow(window, tmp_path, monkeypatch):
    app, win = window
    assert not hasattr(win, "stop_btn")                     # no global STOP in the header any more
    cal_tab, hyst, ana = win.calibration, win.hysteresis, win.analysis
    assert win.state.calibration is None and not hyst.xaxis.mt_btn.isEnabled()
    assert "No field calibration" in win.cal_label.text()
    assert "2.50 V" in hyst.params.amplitude.text()         # decimal point whatever the Windows region

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Coil A", True)))
    cal_tab.new_coil()
    assert cal_tab.coil.currentText() == "Coil A"
    for r, (i, b) in enumerate([("-2", "-100,4"), ("-1", "-50.1"), ("0", "0.2"), ("1", "50"), ("2", "100.3")]):
        cal_tab.table.setItem(r, 0, QTableWidgetItem(i))
        cal_tab.table.setItem(r, 1, QTableWidgetItem(b))
    assert "use '.' for decimals" in cal_tab.result.text()  # '-100,4' is rejected and marked
    assert not cal_tab.save_btn.isEnabled()                 # nothing to save before fitting
    cal_tab.table.item(0, 1).setText("-100.4")
    cal_tab.do_fit()
    assert cal_tab.fit_result.slope == pytest.approx(50.1, abs=0.2)
    cal_tab.save_btn.click()
    cal = win.state.calibration
    assert cal is not None and cal.coil == "Coil A"
    assert win.state.cfg.calibration_path.exists()          # persisted (in the test's tmp folder)
    assert "Coil A" in win.cal_label.text() and hyst.xaxis.mt_btn.isEnabled() and hyst.xaxis.oe_btn.isEnabled()

    # Hysteresis: switch X to field while data is on screen
    connect_and_diagnose(app, win)
    hyst.cycles.setValue(1)
    hyst.params.points.setValue(40)
    hyst.params.settle.setValue(0)
    hyst.params.amplitude.setValue(3.0)
    assert "mT" in hyst.params.current_label.text()
    win.state.set_x_mode("current")
    hyst.start()
    wait_until(app, lambda: hyst.thread is None)
    xi, _ = hyst.avg_curve.getData()
    hyst.xaxis.mt_btn.click()
    assert win.state.x_mode == "mT"
    xb, _ = hyst.avg_curve.getData()
    np.testing.assert_allclose(xb, cal.field(xi))
    assert "mT" in hyst.cycle_plot.getAxis("bottom").labelText
    hyst.xaxis.oe_btn.click()
    xh, _ = hyst.avg_curve.getData()
    np.testing.assert_allclose(xh, 10 * cal.field(xi))
    assert "Oe" in hyst.cycle_plot.getAxis("bottom").labelText

    # Analysis follows the same switch; the saved curve has every X column
    hyst.open_btn.click()
    x, _, _ = ana.series()
    np.testing.assert_allclose(x, ana.m.column(storage.COL_FIELD_OE))   # 1 cycle: the H_Oe column itself
    assert ana.x_note.text() == ""                  # field comes from the measurement, not recomputed
    ana.compute()
    assert " Oe" in ana.result.text()
    ana.xaxis.mt_btn.click()
    ana.compute()
    assert " mT" in ana.result.text()
    ana.xaxis.current_btn.click()
    ana.compute()
    assert " A" in ana.result.text() and win.state.x_mode == "current"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(tmp_path / "curve.csv"), "")))
    ana.save_curve()
    curve = storage.load_measurement(tmp_path / "curve.csv")
    assert {storage.COL_CURRENT, storage.COL_FIELD, storage.COL_FIELD_OE, storage.COL_SIGNAL} <= set(curve.columns)
    assert curve.metadata["columns"][storage.COL_FIELD].startswith("X AXIS (calibrated)")
    np.testing.assert_allclose(curve.column(storage.COL_FIELD), cal.field(curve.column(storage.COL_CURRENT)),
                               atol=1e-6)
    np.testing.assert_allclose(curve.column(storage.COL_FIELD_OE), 10 * curve.column(storage.COL_FIELD))

    # 'Use no calibration' falls back to current everywhere
    cal_tab.use_none()
    assert win.state.calibration is None and win.state.x_mode == "current"
    assert not hyst.xaxis.mt_btn.isEnabled()


def test_stability_test(window, monkeypatch):
    app, win = window
    connect_and_diagnose(app, win)
    cont = win.continuous
    monkeypatch.setattr(type(cont), "STABILITY_MIN_S", 0.5)
    win.state.inst.sim_lab.read_delay_s = 0.002
    cont.start_stability(duration_s=1.5)
    assert cont.thread is not None and not cont.stab_btn.isEnabled()
    wait_until(app, lambda: cont.thread is None, timeout=15)
    assert win.state.inst.sim_lab.ao == 0                       # magnet stayed at 0 V
    _, v = cont.set_curve.getData()
    assert np.all(v == 0)
    assert "mV/min" in cont.stab_result.text() and cont.stab_btn.isEnabled()


def test_panels_fit_the_lab_screen(window):
    """Everything in the measuring panels is visible without scrolling at the lab PC's maximized size."""
    app, win = window
    win.resize(1366, 689)
    win.show()
    for tab in (win.continuous, win.hysteresis):
        win.tabs.setCurrentWidget(tab)
        app.processEvents()
        scroll = tab.findChild(QScrollArea)
        assert scroll.verticalScrollBar().maximum() == 0, type(tab).__name__


def wheel(app, widget, dy=-120):
    pos = QPointF(widget.rect().center())
    ev = QWheelEvent(pos, QPointF(widget.mapToGlobal(pos.toPoint())), QPoint(), QPoint(0, dy), Qt.NoButton,
                     Qt.NoModifier, Qt.NoScrollPhase, False)
    app.sendEvent(widget, ev)


def test_wheel_does_not_change_tabs_or_values(window):
    app, win = window
    win.show()
    app.processEvents()
    win.tabs.setCurrentIndex(1)
    wheel(app, win.tabs.tabBar())
    assert win.tabs.currentIndex() == 1
    amp = win.hysteresis.params.amplitude
    before = amp.value()
    wheel(app, amp, dy=120)
    assert amp.value() == before    # not focused: the wheel scrolls the panel instead


def test_autoscale_comes_back_after_zoom(window):
    app, win = window
    hyst = win.hysteresis
    vb = hyst.cycle_plot.getViewBox()
    vb.sigRangeChangedManually.emit([True, True])   # what a mouse zoom emits
    vb.disableAutoRange()
    assert not hyst.autoscale.on and "OFF" in hyst.autoscale.button.text()
    hyst.autoscale.button.click()                   # one click: back on and re-fitted
    assert hyst.autoscale.on and all(vb.state["autoRange"])
