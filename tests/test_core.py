from dataclasses import asdict
from pathlib import Path

import numpy as np
import pytest
import yaml

from mokeapp.config import ConfigError, load_config
from mokeapp.core import calibration, storage
from mokeapp.core.acquisition import SweepRunner, SweepSettings
from mokeapp.core.diagnostics import run_diagnostics
from mokeapp.core.processing import analyze_loop, drift_correct, drift_fit_plateaus, normalize, stability_report
from mokeapp.core.waveforms import make_waveform
from mokeapp.drivers import Instruments
from mokeapp.drivers.keithley2000 import estimate_read_time
from mokeapp.drivers.simulated import SIM_MT_PER_A, SimLab


@pytest.mark.parametrize("kind", ["triangle", "sine", "square"])
def test_waveforms_start_at_negative_amplitude(kind):
    w = make_waveform(kind, 3.0, 100)
    assert len(w) == 100
    assert w[0] == pytest.approx(-3.0)
    assert w.max() == pytest.approx(3.0)
    assert w.min() == pytest.approx(-3.0)


def test_triangle_reaches_max_at_half_cycle():
    w = make_waveform("triangle", 1.0, 8)
    assert list(w) == pytest.approx([-1, -0.5, 0, 0.5, 1, 0.5, 0, -0.5])


def test_drift_correct_closes_cycle():
    s = np.linspace(0, 1, 50) + 0.3
    c = drift_correct(s)
    assert c[0] == pytest.approx(c[-1])


def test_normalize_range():
    y = normalize(np.concatenate([np.full(50, 2.0), np.full(50, 4.0)]))
    assert y.min() == pytest.approx(-1) and y.max() == pytest.approx(1)


def test_analyze_synthetic_loop():
    v = make_waveform("triangle", 3.0, 400)
    i = v / 2.0
    branch = np.sign(np.gradient(v))
    y = np.tanh((i - branch * 0.5 - 0.1) / 0.05)
    r = analyze_loop(i, y, v)
    assert r.hc == pytest.approx(0.5, abs=0.02)
    assert r.shift == pytest.approx(0.1, abs=0.02)
    assert r.squareness == pytest.approx(1.0, abs=0.05)


def test_analyze_handles_inverting_supply():
    v = make_waveform("triangle", 3.0, 400)
    i = -v / 2.0                       # Kepco inverts
    up = np.sign(np.gradient(i))
    y = np.tanh((i - up * 0.5) / 0.05)
    assert analyze_loop(i, y, v).hc == pytest.approx(0.5, abs=0.02)


def test_csv_roundtrip(tmp_path):
    p = tmp_path / "a.csv"
    n = len(storage.AVG_COLUMNS)
    data = np.arange(3 * n, dtype=float).reshape(3, n)
    storage.write_table(p, {"sample": "x", "nested": {"a": 1}}, storage.AVG_COLUMNS, data)
    m = storage.load_measurement(p)
    assert m.metadata["sample"] == "x"
    assert m.x_column == storage.COL_CURRENT and m.y_column == storage.COL_SIGNAL
    np.testing.assert_allclose(m.data, data)


def test_old_setup_file_migrates_and_saves_round_trip(tmp_path):
    from mokeapp.config import save_config
    old = tmp_path / "old.yaml"     # v0.1.x format: [keithley] section, no monitors/profile keys
    old.write_text("name: Old\nkeithley:\n  address: GPIB0::26::INSTR\n  nplc: 1.0\n  filter: \"off\"\n"
                   "daq:\n  device: Dev1\n  ao_channel: Dev1/ao0\nkepco:\n  gain: -5.0\n", encoding="utf-8")
    c = load_config(old)
    assert c.keithley.address == "GPIB0::26::INSTR" and c.daq.monitors and c.keithley.profile == "auto"
    p = save_config(c, tmp_path / "copy.yaml")
    text = p.read_text(encoding="utf-8")
    assert "multimeter:" in text and "\nkeithley:" not in text
    again = load_config(p)
    assert again.to_dict() == c.to_dict()
    assert again.calibration_path == tmp_path.parent / "calibrations"    # app folder, not per setup


def test_config_rejects_both_multimeter_names(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("keithley:\n  nplc: 1\nmultimeter:\n  nplc: 1\n")
    with pytest.raises(ConfigError, match="multimeter"):
        load_config(p)


def test_run_without_kepco_monitors(cfg, tmp_path):
    cfg.daq.monitors = False
    inst = Instruments(cfg, simulate=True, sim_lab=SimLab(cfg.kepco))
    inst.connect()
    try:
        assert "monitors not wired" in inst.status["kepco"][1]
        rep = run_diagnostics(inst)
        assert rep.passed and rep.resistance_ohm is None
        assert any(c.name == "Kepco monitors" and c.status == "warn" for c in rep.checks)
        res = SweepRunner(inst, SweepSettings("triangle", 3.0, 40, 0, cycles=2, average=True, folder=tmp_path,
                                              sample="nomon")).run()
        assert res.error == ""
        for path in (res.raw_path, res.average_path):
            m = storage.load_measurement(path)
            assert storage.COL_CURRENT not in m.columns and storage.COL_VKEPCO not in m.columns
            np.testing.assert_allclose(m.column(storage.COL_ISET),
                                       m.column(storage.COL_VPROG) / cfg.kepco.magnet_resistance_ohm)
            assert m.x_column == storage.COL_ISET and m.metadata["kepco_monitors"] is False
        # documented limit: without monitors an open magnet circuit cannot be detected
        inst.sim_lab.magnet_connected = False
        assert SweepRunner(inst, SweepSettings("triangle", 3.0, 20, 0, cycles=1)).run().error == ""
    finally:
        inst.close()


def test_without_monitors_a_hall_probe_checks_the_magnet(cfg):
    cfg.daq.monitors = False
    cfg.daq.ai_hall, cfg.daq.hall_mT_per_V = "Dev1/ai2", 100.0
    inst = Instruments(cfg, simulate=True, sim_lab=SimLab(cfg.kepco))
    inst.connect()
    try:
        assert run_diagnostics(inst).passed
        inst.sim_lab.magnet_connected = False
        rep = run_diagnostics(inst)
        assert not rep.passed and any(c.name == "Hall probe" and c.status == "fail" for c in rep.checks)
    finally:
        inst.close()


def test_field_uses_the_current_the_coil_was_calibrated_with(sim, tmp_path):
    # calibrated against the programmed current: B must follow I_set, not the measured current
    sim.cfg.calibration = calibration.fit("C", [-2, -1, 0, 1, 2], [-100, -50, 0, 50, 100],
                                          current_source="programmed")
    sim.sim_lab.resistance_ohm = 2.5      # magnet hotter than its nominal 2.19 Ω: measured I < I_set
    res = SweepRunner(sim, SweepSettings("triangle", 3.0, 40, 0, cycles=1, folder=tmp_path, sample="c")).run()
    m = storage.load_measurement(res.raw_path)
    np.testing.assert_allclose(m.column(storage.COL_FIELD), 50 * m.column(storage.COL_ISET), atol=1e-6)
    assert not np.allclose(m.column(storage.COL_FIELD), 50 * m.column(storage.COL_CURRENT), atol=0.5)


def test_measured_calibration_without_monitors_falls_back_with_warning(cfg):
    cfg.daq.monitors = False
    cfg.calibration = calibration.fit("C", [-2, -1, 0, 1, 2], [-100, -50, 0, 50, 100])   # measured current
    inst = Instruments(cfg, simulate=True, sim_lab=SimLab(cfg.kepco))
    inst.connect()
    try:
        res = SweepRunner(inst, SweepSettings("triangle", 1.0, 20, 0, cycles=1)).run()
        assert any("monitors are off" in w for w in res.warnings)
        np.testing.assert_allclose(res.measurement.column(storage.COL_FIELD),
                                   50 * res.measurement.column(storage.COL_ISET), atol=1e-6)
    finally:
        inst.close()


def test_old_files_rebuild_the_programmed_current():
    cols = [storage.COL_POINT, storage.COL_VPROG, storage.COL_VKEPCO, storage.COL_CURRENT, storage.COL_SIGNAL]
    data = np.array([[0, -1.0, -1.0, -0.45, 0.2], [1, 1.0, 1.0, 0.45, 0.21]])
    m = storage.Measurement(None, {"config": {"kepco": {"magnet_resistance_ohm": 2.0}}}, cols, data)
    np.testing.assert_allclose(m.programmed_current(), [-0.5, 0.5])
    assert m.x_column == storage.COL_CURRENT


class _FakeInst:
    def __init__(self, idn, replies=None):
        self.idn, self.replies, self.writes, self.timeout = idn, replies or {}, [], 0

    def query(self, cmd):
        if cmd == "*IDN?":
            return self.idn
        if "ERR" in cmd:
            return '+0,"No error"'
        return self.replies.get(cmd, "+1.234E-01")

    def write(self, cmd):
        self.writes.append(cmd)

    def control_ren(self, *_):
        pass

    def clear(self):
        pass

    def close(self):
        pass


class _FakeRM:
    def __init__(self, inst):
        self.inst = inst

    def open_resource(self, _):
        return self.inst

    def close(self):
        pass


@pytest.mark.parametrize("idn, key", [
    ("KEITHLEY INSTRUMENTS INC.,MODEL 2000,0738697,A13  /A02", "k2000"),
    ("KEITHLEY INSTRUMENTS INC.,MODEL 2010,1,A", "k2000"),
    ("KEITHLEY INSTRUMENTS,MODEL DMM6500,04400000,1.7", "dmm6500"),
    ("KEITHLEY INSTRUMENTS INC.,MODEL 2100,1,01.08-01-01", "k2100"),
    ("Agilent Technologies,34401A,0,11-5-2", "scpi"),
])
def test_multimeter_profile_detection(idn, key):
    from mokeapp.drivers.multimeter import detect_profile
    assert detect_profile(idn) == key


def test_multimeter_profiles_send_their_own_commands(cfg):
    from mokeapp.drivers.multimeter import ScpiMultimeter
    k2000 = _FakeInst("KEITHLEY INSTRUMENTS INC.,MODEL 2000,0,A", {":SYST:FRSW?": "0"})
    m = ScpiMultimeter(cfg.keithley, lambda: _FakeRM(k2000))
    m.connect()
    assert ":INIT:CONT OFF" in k2000.writes and m.input_terminal() == "REAR" and m.read() == pytest.approx(0.1234)
    m.configure(10.0, 1.0, "repeat", 5)
    assert ":SENS:VOLT:DC:AVER:COUN 5" in k2000.writes
    m.close()

    cfg.keithley.filter = "off"
    k2100 = _FakeInst("KEITHLEY INSTRUMENTS INC.,MODEL 2100,1,01")
    m = ScpiMultimeter(cfg.keithley, lambda: _FakeRM(k2100))
    m.connect()
    assert "CONF:VOLT:DC" in k2100.writes and not any("INIT:CONT" in w for w in k2100.writes)
    m.configure(10.0, 1.0, "repeat", 5)     # no repeat filter on a 2100: ignored, not sent
    assert cfg.keithley.filter == "off" and not any("AVER" in w for w in k2100.writes)
    assert m.input_terminal() is None
    m.close()

    dmm = _FakeInst("KEITHLEY INSTRUMENTS,MODEL DMM6500,1,1", {":ROUT:TERM?": "FRON"})
    m = ScpiMultimeter(cfg.keithley, lambda: _FakeRM(dmm))
    m.connect()
    assert m.input_terminal() == "FRONT"
    m.close()


def test_daq_ao_limit_and_single_channel_capture(cfg):
    from mokeapp.drivers.nidaq import NiDaq, device_ao_limit

    class Dev:
        def __init__(self, rngs):
            self.ao_voltage_rngs = rngs
    assert device_ao_limit(Dev([-10.0, 10.0, -5.0, 5.0])) == 10.0
    assert device_ao_limit(Dev([-5.0, 5.0])) == 5.0
    assert device_ao_limit(object()) == 10.0         # unknown: ±10 V

    class Task:     # NI-DAQmx returns a flat list when the task has one channel
        def read(self, **_):
            return [0.5, 0.7]

        def stop(self):
            pass
    cfg.daq.monitors = False
    d = NiDaq(cfg.daq)
    d._ai, d._ai_roles = Task(), ["hall"]
    i, v = d.finish_capture()
    assert np.isnan(i) and np.isnan(v) and d.last_hall_V == pytest.approx(0.6)


def test_config_rejects_unknown_key(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("kepco:\n  gian: -5\n")
    with pytest.raises(ConfigError, match="gian"):
        load_config(p)


def test_max_voltage_from_current_limit(cfg):
    assert cfg.max_magnet_voltage() == pytest.approx(8 * 2.19)


def test_kepco_clamps_to_max(sim):
    assert sim.kepco.set_voltage(1000) == pytest.approx(sim.kepco.max_voltage)
    sim.safe_zero()
    assert sim.sim_lab.ao == 0


def test_hysteresis_run_saves_files_and_zeroes(sim, tmp_path):
    seen = []
    s = SweepSettings("triangle", 3.0, 40, 0, cycles=2, average=True, drift_correction=True,
                      sample="test sample", folder=tmp_path)
    res = SweepRunner(sim, s, on_cycle=lambda c, a: seen.append((c, a))).run()
    assert res.error == "" and res.completed_cycles == 2
    assert sim.sim_lab.ao == 0
    raw = storage.load_measurement(res.raw_path)
    avg = storage.load_measurement(res.average_path)
    assert raw.data.shape[0] == 80 and avg.data.shape[0] == 40
    assert set(raw.column(storage.COL_CYCLE)) == {0, 1}
    assert len(seen) == 2 and seen[1][1] is not None
    r = analyze_loop(avg.column(avg.x_column), avg.column(avg.y_column), avg.column(storage.COL_VPROG))
    assert r.hc == pytest.approx(0.6, abs=0.1)


def test_disconnected_magnet_stops_run(sim, tmp_path):
    sim.sim_lab.magnet_connected = False
    res = SweepRunner(sim, SweepSettings("triangle", 3.0, 40, 0, cycles=1, folder=tmp_path)).run()
    assert "does not match" in res.error
    assert sim.sim_lab.ao == 0
    assert res.raw_path is None and not list(tmp_path.iterdir())   # no header-only CSV left behind


def test_single_bad_monitor_reading_is_remeasured(sim):
    # 1st glitch hits the wake-up capture, 2nd the first point (-3 V, should read ~-1.4 A)
    sim.sim_lab.monitor_glitches = 2
    res = SweepRunner(sim, SweepSettings("triangle", 3.0, 40, 0, cycles=1)).run()
    assert res.error == "" and res.completed_cycles == 1
    assert res.monitor_glitches == 1
    assert any("re-measured" in w for w in res.warnings)


def test_linear_calibration_fit():
    i = np.linspace(-3, 3, 13)
    b = 52.0 * i + 1.5 + np.random.default_rng(0).normal(0, 0.05, i.size)
    cal = calibration.fit("coil", i, b, 1)
    assert cal.slope == pytest.approx(52.0, rel=1e-3) and cal.coefficients[0] == pytest.approx(1.5, abs=0.05)
    assert cal.r2 > 0.9999 and cal.current_range == [-3.0, 3.0]
    assert float(cal.field(1.0)) == pytest.approx(53.5, abs=0.1)
    with pytest.raises(calibration.CalibrationError):
        calibration.fit("coil", [0, 1], [0, 50], 1)     # too few points


def test_fit_through_origin_ignores_offset():
    i = np.array([0.2, 0.7, 1.2, 1.7, 2.2, 2.6])
    b = 4.6 * i + 0.4                               # e.g. Hall probe not zeroed
    free = calibration.fit("c", i, b, 1)
    forced = calibration.fit("c", i, b, 1, through_origin=True)
    assert free.coefficients[0] == pytest.approx(0.4)
    assert forced.coefficients[0] == 0 and float(forced.field(0)) == 0
    assert "+0.4" not in forced.equation() and forced.through_origin


def test_polynomial_fit_follows_saturation():
    i = np.linspace(-6, 6, 25)
    b = 300 * np.tanh(i / 5)    # iron core saturating
    lin = calibration.fit("c", i, b, 1)
    cub = calibration.fit("c", i, b, 3)
    assert lin.r2 < 0.999 < cub.r2 and cub.max_residual_mT < lin.max_residual_mT / 5


def _cal(coil, slope, date):
    c = calibration.fit(coil, [-2, -1, 0, 1, 2], [-2 * slope, -slope, 0, slope, 2 * slope])
    c.date = date
    return c


def test_calibration_library_roundtrip(tmp_path):
    folder = tmp_path / "calibrations"
    lib = calibration.CalibrationLibrary(folder)
    lib.add(_cal("Coil A", 50, "2026-10-01T10:00:00"))
    lib.add(_cal("Coil A", 55, "2026-10-02T10:00:00"))
    lib.add(_cal("Coil B", 10, "2026-10-03T10:00:00"))
    assert len(list(folder.glob("Coil_*.yaml"))) == 3        # one file per calibration
    lib = calibration.CalibrationLibrary(folder)             # reload from disk
    assert lib.active.coil == "Coil B" and lib.coils() == ["Coil A", "Coil B"]
    assert [c.slope for c in lib.history("Coil A")] == pytest.approx([55, 50])   # newest first
    lib.set_active("Coil A")
    assert calibration.CalibrationLibrary(folder).active.slope == pytest.approx(55)
    lib.use(lib.history("Coil A")[1])                        # an older calibration, used again
    assert calibration.CalibrationLibrary(folder).active.slope == pytest.approx(50)
    lib.delete_coil("Coil A")
    again = calibration.CalibrationLibrary(folder)
    assert again.active is None and again.coils() == ["Coil B"]
    assert len(list((folder / "deleted").glob("*.yaml"))) == 2   # moved, never destroyed


def test_old_per_setup_libraries_are_imported(tmp_path):
    """The bug of v0.2.0: saving a setup under a new name hid the calibrations of the old one."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    old = {"active": "Usedcoil", "coils": {"Usedcoil": [asdict(_cal("Usedcoil", 4.8, "2026-10-29T15:45:04"))],
                                           "Uusualcoil": [asdict(_cal("Uusualcoil", 4.6, "2026-10-29T15:18:53"))]}}
    (config_dir / "calibrations_moke_lab1.yaml").write_text(yaml.safe_dump(old), encoding="utf-8")
    (config_dir / "l-moke.yaml").write_text("name: L-MOKE\n", encoding="utf-8")   # a setup with a new name
    cfg = load_config(config_dir / "l-moke.yaml")
    assert cfg.calibration is not None and cfg.calibration.coil == "Usedcoil"
    lib = calibration.CalibrationLibrary(tmp_path / "calibrations")
    assert lib.coils() == ["Usedcoil", "Uusualcoil"]
    assert (config_dir / "calibrations_moke_lab1.yaml").exists()      # the old file is left as it was
    assert lib.import_legacy(config_dir) == 0                          # importing twice adds nothing


def test_run_with_calibration_saves_field_and_header(sim, tmp_path):
    # calibrated only up to ±1 A; the 3 V sweep reaches ±1.37 A
    sim.cfg.calibration = calibration.fit("Coil A", [-1, -0.5, 0, 0.5, 1], [-50, -25, 0, 25, 50])
    s = SweepSettings("triangle", 3.0, 40, 0, cycles=2, average=True, sample="cal", folder=tmp_path)
    res = SweepRunner(sim, s).run()
    for path in (res.raw_path, res.average_path):
        m = storage.load_measurement(path)
        assert m.field_column == storage.COL_FIELD and m.x_column == storage.COL_CURRENT
        np.testing.assert_allclose(m.column(storage.COL_FIELD), 50 * m.column(storage.COL_CURRENT), atol=1e-6)
        assert m.metadata["calibration"]["coil"] == "Coil A"
        assert m.metadata["columns"][storage.COL_FIELD].startswith("X AXIS (calibrated)")
        assert m.metadata["columns"][storage.COL_SIGNAL].startswith("Y AXIS")
    assert any("outside the calibrated range" in w for w in res.warnings)
    assert res.measurement.field_column == storage.COL_FIELD


def test_old_calibration_section_is_ignored(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("calibration:\n  unit: ''\n  coefficients: []\n")
    cfg = load_config(p)
    assert cfg.calibration is None and cfg.calibration_path == tmp_path.parent / "calibrations"


def _loop_with_drift(n=200, drift=-3e-3, jump=0.0):
    """Triangle cycle from -1 A: square-ish loop (step 4 mV) + linear drift + optional start jump."""
    from mokeapp.core.waveforms import make_waveform as mw
    i = mw("triangle", 1.0, n)
    t = np.linspace(0, 20, n)
    up = np.r_[1, np.diff(i)] > 0
    m = np.where(up, np.tanh((i - 0.3) / 0.05), np.tanh((i + 0.3) / 0.05))
    y = 0.2 + 2e-3 * m + drift * t / 20
    y[:3] += jump
    return i, t, y, 0.2 + 2e-3 * m


def test_plateau_drift_fit_recovers_drift_and_ignores_start_jump():
    i, t, y, clean = _loop_with_drift(jump=1.5e-3)
    fit = drift_fit_plateaus(y, i, t)
    assert fit.robust and fit.drift == pytest.approx(-3e-3, rel=0.05)
    assert fit.step == pytest.approx(4e-3, rel=0.05)
    # away from the 3 jumped points themselves, the robust fit restores the clean loop; 'close each cycle'
    # trusts the jumped first point and tilts the whole loop
    rest = slice(3, None)
    robust_err = np.abs((fit.corrected - fit.corrected[10]) - (clean - clean[10]))[rest].max()
    ends = drift_correct(y)
    endpoint_err = np.abs((ends - ends[10]) - (clean - clean[10]))[rest].max()
    assert robust_err < 0.2e-3 < endpoint_err


def test_no_loop_no_drift_ratio():
    # laser off / no sample: just noise, so 'Kerr step' and drift-vs-step mean nothing
    i, t, _, _ = _loop_with_drift()
    noise = 0.2 + np.random.default_rng(3).normal(0, 1e-4, i.size)
    assert not drift_fit_plateaus(noise, i, t).has_loop
    _, _, y, _ = _loop_with_drift()
    assert drift_fit_plateaus(y, i, t).has_loop


def test_plateau_drift_falls_back_without_saturation():
    fit = drift_fit_plateaus(np.linspace(0, 1, 50), np.zeros(50))
    assert not fit.robust


def test_drift_warning_during_run(sim):
    sim.sim_lab.drift_V_per_s = 0.05    # ~40 mV over the ~0.8 s cycle, against a 0.1 V simulated Kerr step
    res = SweepRunner(sim, SweepSettings("triangle", 3.0, 40, 20, cycles=1, drift_correction="plateaus")).run()
    assert res.error == ""
    assert any("drifted" in w for w in res.warnings)


def _loop(h, shift=0.0):
    """Ascending then descending square-ish loop over the field values h (one triangle cycle)."""
    up = np.r_[1, np.diff(h)] > 0
    return np.where(up, np.tanh((h - 0.3 - shift) / 0.05), np.tanh((h + 0.3 - shift) / 0.05))


def test_symmetrize_removes_even_contributions():
    from mokeapp.core.processing import symmetrize
    h = make_waveform("triangle", 1.0, 400)
    m = _loop(h)
    # offset + Kerr loop + quadratic-Kerr-like term (∝ M², even) + optics moved by the magnet (∝ H², even)
    s = 0.2 + 2e-3 * m + 0.5e-3 * m ** 2 + 0.8e-3 * h ** 2
    sym = symmetrize(h, s, h)
    np.testing.assert_allclose(sym.odd, 2e-3 * m[sym.up], atol=2e-5)          # only the Kerr loop is left
    np.testing.assert_allclose(sym.even, (0.2 + 0.5e-3 * m ** 2 + 0.8e-3 * h ** 2)[sym.up], atol=2e-5)
    x = sym.mirror(h)
    r = analyze_loop(x, np.concatenate([sym.odd, -sym.odd]), x)
    assert r.hc == pytest.approx(0.3, abs=0.01) and r.shift == pytest.approx(0, abs=1e-9)


def test_symmetrize_centres_a_real_shift_and_needs_both_branches():
    from mokeapp.core.processing import symmetrize
    h = make_waveform("triangle", 1.0, 400)
    sym = symmetrize(h, _loop(h, shift=0.1), h)                 # exchange-bias-like shift of 0.1
    x = sym.mirror(h)
    r = analyze_loop(x, np.concatenate([sym.odd, -sym.odd]), x)
    assert r.shift == pytest.approx(0, abs=1e-9)                 # removed by construction (documented)
    with pytest.raises(ValueError):
        symmetrize(h[:150], _loop(h)[:150], h[:150])             # ascending branch only


def test_stability_report():
    t = np.linspace(0, 120, 1201)
    s = 0.5 + 1e-4 * t / 60 + np.random.default_rng(1).normal(0, 2e-5, t.size)
    r = stability_report(t, s)
    assert r.drift_V_per_min == pytest.approx(1e-4, rel=0.1) and r.noise_V == pytest.approx(2e-5, rel=0.15)
    assert r.drift_pct_per_min == pytest.approx(0.02, rel=0.1)


def test_hall_probe_field_is_recorded(cfg, tmp_path):
    cfg.daq.ai_hall, cfg.daq.hall_mT_per_V = "Dev1/ai2", 100.0
    inst = Instruments(cfg, simulate=True, sim_lab=SimLab(cfg.kepco))
    inst.connect()
    try:
        rep = run_diagnostics(inst)
        assert any(c.name == "Hall probe" and c.status == "pass" for c in rep.checks)
        res = SweepRunner(inst, SweepSettings("triangle", 3.0, 40, 0, cycles=2, average=True, folder=tmp_path,
                                              sample="hall")).run()
        for path in (res.raw_path, res.average_path):
            m = storage.load_measurement(path)
            assert m.field_column == storage.COL_HALL
            np.testing.assert_allclose(m.column(storage.COL_HALL), SIM_MT_PER_A * m.column(storage.COL_CURRENT),
                                       atol=0.1)
            assert m.metadata["hall_probe"]["channel"] == "Dev1/ai2"
    finally:
        inst.close()


def test_hall_probe_off_by_default(cfg):
    assert not cfg.daq.hall_enabled


def test_keithley_read_time_estimate():
    # measured on the lab Keithley: 0.042 s and 8.4 s
    assert estimate_read_time(1, "off", 10) == pytest.approx(0.042, rel=0.1)
    assert estimate_read_time(10, "repeat", 10) == pytest.approx(8.4, rel=0.1)


def test_stop_ramps_to_zero(sim):
    runner = SweepRunner(sim, SweepSettings("sine", 3.0, 40, 0, cycles=None))
    runner.on_point = lambda p: runner.stop() if p.index == 10 else None
    res = runner.run()
    assert res.stopped and res.error == ""
    assert sim.sim_lab.ao == 0


def test_diagnostics_pass_and_detect_disconnection(sim):
    rep = run_diagnostics(sim)
    assert rep.passed and rep.resistance_ohm == pytest.approx(2.19, rel=0.02)
    sim.sim_lab.magnet_connected = False
    rep = run_diagnostics(sim)
    assert not rep.passed
