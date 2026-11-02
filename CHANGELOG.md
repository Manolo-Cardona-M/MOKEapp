# Changelog

All notable changes to MOKEapp. Dates are when the version was handed to the lab.

## 0.2.3 — 2026-11-02
- Citation metadata for the archived release on Zenodo (DOI): authors' ORCID iDs and the current
  version in `CITATION.cff`, more keywords.

## 0.2.2 — 2026-11-02
- README rewritten around a measurement, step by step, with screenshots of the interface
  (`docs/screenshots/`); MOKEapp described for longitudinal and polar MOKE.
- The repository keeps only what is needed to install, use and modify the app: lab-specific setups,
  the LabVIEW notes and the hardware bring-up scripts stay local; `config/example_setup.yaml` is the
  shared template. Lab notes for future sessions move to `CLAUDE.local.md` (local); `CLAUDE.md` keeps the
  project's working rules.

## 0.2.1 — 2026-11-02
- **Fix:** field calibrations were stored per setup file (`config/calibrations_<setup>.yaml`), so saving
  a setup under a new name ("L-MOKE") made the calibration in use disappear (it was not deleted).
  Calibrations now live in the app's `calibrations/` folder, one file per calibration, shared by every
  setup; `active.yaml` names the one in use. Old per-setup files are imported automatically and left
  untouched.
- Calibration tab: **Load from file...** (use any saved or older calibration again, also from an old
  multi-calibration file) and **Open folder**. Deleting a coil moves its files to `calibrations/deleted/`.
- Project reorganised: `docs/` (developer guide, LabVIEW migration notes, screenshots), `tools/hardware_checks/`
  (former `pruebas/`), `CLAUDE.md` handoff notes, this changelog.

## 0.2.0 — 2026-11-02
- **Instruments → Setup...**: pick the multimeter (VISA scan, *IDN? identification) and any NI-DAQmx device
  and its channels; Kepco limits and nominal magnet R; validation before saving; the app restarts with
  the new setup and remembers the last setup per PC. A new PC opens Setup on first start.
- Multimeter profiles chosen from *IDN?: Keithley 2000 family (tested), DMM6500/7510, 2100/2110, generic SCPI.
- **Kepco monitors optional**: without them the current is the programmed V / nominal R (`I_set_A [X]`,
  always saved), diagnostics warn (or check the magnet with a Hall probe), current/overheating checks off.
- X axis switch: **I measured | I programmed | Field (mT) | Field (Oe)**, remembered. Calibrations record
  whether they were made with the measured or programmed current and always use that one.
- Manual magnet control: **Set current** (V = I·R). DAQ AO range read from the device.
- Drift warning only when a real loop is above the noise. `install/setup_pc.ps1` for new PCs; pinned
  package versions.

## 0.1.7 — 2026-10-30
- "Even part" checkbox with a guide to read it; the magnet-drive plot keeps a ±0.25 V minimum span.

## 0.1.6 — 2026-10-30
- Odd/even **symmetrization** in Analysis (odd part, even part shown for diagnosis).

## 0.1.5 — 2026-10-29
- Optional **robust drift correction** (saturation plateaus, outliers ignored) next to "close each cycle";
  measured drift shown in Analysis; drift warning during runs.
- **Laser stability test** in Continuous (drift %/min, noise, drift per cycle).
- Hall probe support on a DAQ input (disabled until configured).

## 0.1.4 — 2026-10-29
- Renamed **MOKEapp**. Decimal point everywhere. X axis in A, mT or Oe; `H_Oe [X]` column.
- "Force B = 0 at I = 0" fit option and warnings for one-sided calibrations / offsets.
- Panels fit a 1366×720 screen without clipping.

## 0.1.3 — 2026-10-29
- **Calibration tab** (Hall-probe points, linear/polynomial fit, library per coil); `B_mT [X]` column with
  an English description of every column in the CSV header. Header STOP button removed. Auto-scale in Analysis.

## 0.1.2 — 2026-10-29
- Red Stop buttons; Continuous shows the signal and the magnet drive (no saving); single Auto-scale
  switch; Analysis opens and saves the last measurement even if it was not saved.

## 0.1.1 — 2026-10-29
- Live readout off the GUI thread; Keithley timeout follows NPLC/filter; monitor glitches re-measured;
  DAQ reset on reconnect; plots auto-scale and sit side by side; no mouse-wheel tab switching.

## 0.1.0 — 2026-10-28
- First version replacing the LabVIEW VI: Instruments (diagnostics), Continuous, Hysteresis, Analysis tabs;
  CSV with metadata; simulated instruments and tests.
