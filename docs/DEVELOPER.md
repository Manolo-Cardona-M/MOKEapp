# MOKEapp — developer guide

How the code is organised, how data flows, and how to change things without breaking the lab.
User-facing instructions are in the [README](../README.md); version history in [CHANGELOG](../CHANGELOG.md).

## Layout

```
main.py                     entry point: logging, setup file choice (per-PC memory), theme, safety hooks
mokeapp/
  config.py                 SetupConfig dataclasses, load_config (validation, old-file migration), save_config
  core/                     no Qt here: testable and scriptable
    acquisition.py          SweepRunner: point-by-point sweep, safety checks, CSV writing, in-memory result
    calibration.py          CoilCalibration, fit(), CalibrationLibrary (calibrations/ folder)
    diagnostics.py          preflight checks, adapted to the setup (monitors / Hall probe / multimeter model)
    processing.py           drift correction, averaging, normalisation, symmetrisation, Hc, stability report
    storage.py              CSV format (YAML '#' header + table), column names and descriptions
    waveforms.py            triangle / sine / square, each cycle starting at -amplitude
  drivers/
    __init__.py             Instruments: the set of instruments of one setup (real or simulated), safe_zero
    multimeter.py           ScpiMultimeter + per-model command profiles (detect_profile from *IDN?)
    nidaq.py                NiDaq: AO to the Kepco, optional AI (monitors, Hall); AO range from the device
    kepco.py                KepcoBOP: slew-limited set_voltage, monitor scaling, programmed current V/R
    discovery.py            VISA resources, *IDN?, NI-DAQmx devices (used by the Setup dialog)
    simulated.py            SimLab/SimKeithley/SimDaq with the same interface (tests, --sim)
    keithley2000.py         compatibility alias for the old import path
  gui/
    main_window.py          header, tabs, restart after a setup change
    state.py                AppState (signals, io_lock, prefs, calibration), FuncThread, SweepThread
    tab_instruments.py      connection, diagnostics, live readout, manual magnet, multimeter settings
    setup_dialog.py         instrument setup (scan, identify, validate, save)
    tab_calibration.py      Hall-probe points, fit, calibration files
    tab_measure.py          MeasureTab base, ContinuousTab (live view, stability test), HysteresisTab
    tab_analysis.py         load CSV / last run, X switch, drift, symmetrisation, Hc, save
    widgets.py              SweepParams, AutoScale, XAxisToggle, DriftCombo, WheelGuard, plot factory
    theme.py                light / dark / classic stylesheets and plot colours
    assets/                 icon
config/example_setup.yaml   template setup (in git); each PC saves its own config/<name>.yaml (not in git)
calibrations/               field calibrations of this PC's magnet (one YAML per calibration, active.yaml; not in git)
data/  logs/                measurements and session logs (not in git)
tests/                      pytest, all with simulated instruments (GUI tests run offscreen)
install/setup_pc.ps1        new-PC installer (drivers check, venv, shortcut)
docs/                       this guide and the README screenshots (docs/screenshots/)
CLAUDE.md                   working rules for whoever continues the project (CLAUDE.local.md: lab notes, local only)
```

## Main ideas

**One measurement engine.** `SweepRunner.run()` (core/acquisition.py) does every point the same way:
set the magnet voltage (slew-limited ramp) → wait the settle time → start the DAQ capture → read the
multimeter → finish the capture → safety checks → report the point. Continuous, Hysteresis and the
stability test are all `SweepSettings` variations (cycles=None runs until stopped; amplitude 0 for the
stability test). The magnet always ends at 0 V (`finally: inst.safe_zero()`).

**The setup decides the hardware.** `SetupConfig` (config.py) holds the multimeter, DAQ, Kepco, safety
limits and defaults; YAML section names: `multimeter` (old files: `keithley`, migrated on load), `daq`,
`kepco`, `safety`, `defaults`. In code the multimeter section is still `cfg.keithley` (alias
`cfg.multimeter`). `Instruments` builds real or simulated drivers from it; `daq.monitors` and
`daq.ai_hall` switch the optional inputs on.

**Two currents.** `I_set` = V_programmed / nominal R (always available, reproducible) and `I_magnet`
= Kepco current monitor (only with monitors, NaN otherwise). `Point.x_current` picks the measured one
when present. A calibration stores `current_source` and `CoilCalibration.field_for()` uses the matching
current, so the field is right whichever current is plotted.

**Threads.** Instruments are touched from background threads only: `SweepThread` (measurements),
`FuncThread` (connect, diagnostics, Keithley apply, live readout). `AppState.io_lock` gives one user at a
time; the live readout skips a tick instead of waiting. Drivers have their own locks too. Results come
back through Qt signals; the plots redraw at most every 50 ms.

**Safety layers.** AO clamp to the device range; magnet voltage clamp to min(v_max, i_max·R, |gain|·AO);
slew-rate ramp on every change; ramp to 0 on stop, error, close, `atexit` and the excepthook; with
monitors: current-mismatch watchdog (re-measured twice before stopping, because the USB-6002 sometimes
returns one bad capture), resistance rise (overheating) warn/stop, current-limit warning.

**Data files.** `storage.raw_columns(monitors)`: point, time_s, cycle, V_programmed_V, I_set_A [X],
[V_kepco_V, I_magnet_A [X]], signal_V [Y], then B_mT [X]/H_Oe [X] (calibration) and B_hall_mT [X]/H_hall_Oe [X]
(Hall probe). The header (`# ` + YAML) has `columns:` (plain-English meaning of each column), the
calibration used, sweep settings and a snapshot of the setup. `[X]`/`[Y]` tags mark axis candidates.
Raw files are never processed; the averaged file and Analysis apply drift correction etc.

**Calibrations.** `calibrations/<coil>_<date>.yaml`, one per fit, plus `active.yaml`. Shared by all setups
of this app folder (they belong to the magnet, not to the setup file). Never deleted: "Delete coil"
moves files to `calibrations/deleted/`.

**Per-PC memory.** `QSettings("USM", "MOKEapp")` (registry) keeps `setup_path` and `x_mode`. Tests pass
`prefs=None` (or a fake) so nothing is written.

## Conventions
- English in the UI, code, comments and files; the lab talks Spanish with the developer.
- Decimal point everywhere (the app forces an English locale; Windows here is Spanish).
- New behaviour comes with a simulated test; `core/` stays free of Qt.
- Atomic writes (`.tmp` + replace) for setups and calibrations.
- Keep `SimKeithley`/`SimDaq` in step with the real driver interfaces.
- Bump `mokeapp/__init__.py` `__version__` and add a CHANGELOG entry for every hand-over.

## How to…
- **Support another multimeter model:** add a `Profile` to `PROFILES` in drivers/multimeter.py (IDN model
  substrings, arm/configure/read/terminal commands, filter support), a case in
  `test_multimeter_profile_detection`, and try it with Run diagnostics (it lists command errors).
- **Another NI DAQ:** nothing in code; choose it in Setup. Check the AI terminal configuration it supports
  and the differential pairs in its pinout.
- **Another supply:** `KepcoBOP` only needs an AO-programmable bipolar supply; set gain/limits in Setup.
  Without current monitors, set `monitors: false`.
- **Add a CSV column:** storage.py (name + description), acquisition `_row()`/`_save_average()`, Analysis
  `_columns()` if it is an axis, and a test.

## Testing
```
.venv\Scripts\python.exe -m pytest -q tests
```
All tests use simulated instruments (no hardware, safe to run any time). Before handing a version to the
lab, also run on the real setup: Connect → Run diagnostics → a 0.5 V hysteresis loop, and check the magnet
ends at 0 V (Live readout).

README screenshots are made with the simulated instruments at 1366×768 (`docs/screenshots/`); refresh them
when a screen changes visibly.

## Known limits / ideas
- Non-2000 multimeter profiles and the USB-6421 are untested on real hardware (second MOKE).
- Kepco current mode (programming input sets the current) is not implemented; it would make I_set exact
  without monitors.
- Iron-yoke remanence: the field at a given current depends on the sweep direction; a Hall probe read
  live (already supported) is the real fix for soft samples (Hc ~ 1 Oe).
