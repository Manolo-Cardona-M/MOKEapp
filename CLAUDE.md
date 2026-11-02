# MOKEapp — how to work on this project

Desktop app (Python, PySide6) that runs a longitudinal or polar MOKE: an SCPI multimeter reads the
photodetector, an NI DAQ programs a bipolar supply (Kepco BOP) that drives the electromagnet.
Read [docs/DEVELOPER.md](docs/DEVELOPER.md) for the architecture and [CHANGELOG.md](CHANGELOG.md) for the
history. Lab-specific notes, if present, are in `CLAUDE.local.md` (not in git).

## Run and test
```
.venv\Scripts\python.exe main.py --sim          # simulated instruments, safe anywhere
.venv\Scripts\python.exe -m pytest -q tests     # all tests use the simulated instruments
```
New PC: `install\setup_pc.ps1` (drivers check, venv, desktop shortcut), then Instruments → Setup.

## How we work (the author's rules)
1. **Plan first.** Explain what will change and wait for the author's go-ahead before big changes.
   Small fixes that were asked for can go straight in.
2. **Show the UI before shipping it.** Screenshot changed screens at 1366×689 (the lab PC's maximised
   window), with the simulated instruments, and check nothing is clipped.
3. **The real instruments come last and carefully.**
   - Check that MOKEapp is closed (`Get-Process pythonw`) and ask before touching the hardware.
   - Use small amplitudes (≤ 0.5–1 V at the magnet), and the magnet must end at 0 V.
   - Never change Windows or system settings yourself: give the author the command.
4. **Nothing is lost.**
   - Raw data is never modified.
   - Setups and calibrations are written atomically.
   - Deleting moves to a `deleted/` folder.
   - Tests use temporary folders and never the real `config/`, `calibrations/` or registry (`prefs=None`).
5. **English** in the UI, code, comments and files; decimal point everywhere.
6. **Every change has a test** with the simulated instruments; `mokeapp/core` has no Qt code.
7. **Every hand-over:**
   - bump `mokeapp/__init__.py` and add a CHANGELOG entry;
   - run the tests;
   - commit, tag `vX.Y.Z` and push (`git push --follow-tags`);
   - when driver or acquisition code changed: run Connect → diagnostics → a 0.5 V loop on the real setup.

## Instrument quirks worth knowing
- **Keithley 2000:**
  - `*CLS`, `:INIT:CONT OFF`, `:ABOR` before `:READ?`, otherwise errors −213/−350.
  - Return to local with GPIB GTL, never `:SYST:LOC` (gives −221).
  - NPLC 10 + repeat filter 10 ≈ 8 s per reading.
  - The first GPIB open of a session can fail; a retry is built in.
- **USB DAQs and GPIB-USB adapters:** they can return a bad single capture or drop off USB when Windows
  suspends them (USB selective suspend). The app re-measures before any safety stop and resets the DAQ on
  reconnect.
- **Kepco BOP:** about −10 mV output at 0 V programmed (normal offset).
- **Iron-yoke remanence:** it can be as large as the coercive field of soft films (~1 Oe). For absolute
  fields, calibrate in both sweep directions or read a Hall probe live (supported through `daq.ai_hall`).

## Open items
- Validate other multimeter profiles (DMM6500, 2100) and other DAQs (e.g. USB-6421) on real hardware.
- Kepco current mode (would make the programmed current exact without monitors).
- Calibration with separate up/down branches (yoke remanence).
