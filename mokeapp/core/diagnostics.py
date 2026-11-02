"""Preflight checks run from the Instruments tab before measuring."""
import logging
import statistics
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

TEST_VOLTAGE = 1.0      # at the magnet (~0.46 A with 2.19 Ω)
GAIN_TOLERANCE = 0.05
MIN_TEST_CURRENT_A = 0.1
R_RANGE = (0.5, 2.0)    # measured R must be within this factor of the configured R
MIN_HALL_CHANGE_MT = 0.1   # the ±1 V test pulse must change the Hall reading at least this much
MONITOR_READS = 3      # median of several captures: one bad USB-6002 capture must not fail the check
# Near extinction (crossed polarizers) a few mV is a normal signal, so only ~0 V is suspicious.
# The PDA36A dark level is a few mV to -20 mV depending on gain, so this can't prove the laser is off.
DARK_SIGNAL_V = 0.002


@dataclass
class CheckResult:
    name: str
    status: str         # pass | warn | fail
    message: str


@dataclass
class DiagnosticsReport:
    checks: list[CheckResult]
    resistance_ohm: float | None = None

    @property
    def passed(self) -> bool:
        return all(c.status != "fail" for c in self.checks)


def _keithley(inst) -> list[CheckResult]:
    k = inst.keithley
    if not k.connected:
        return [CheckResult("Multimeter", "fail", "Not connected")]
    out = []
    try:
        k.errors()
        value = k.read()
        errs = k.errors()
        model = k.idn.split(',')[1].strip() if ',' in k.idn else k.idn
        if errs:
            out.append(CheckResult("Multimeter", "warn", f"{model}: read OK ({value:.5f} V) but errors: "
                                                         f"{'; '.join(errs)}"))
        else:
            out.append(CheckResult("Multimeter", "pass", f"{model}, test read {value:.5f} V, no errors"))
        term = k.input_terminal()
        expected = inst.cfg.keithley.terminals.upper()
        if term is not None and expected in ("FRONT", "REAR"):
            out.append(CheckResult("Multimeter input", "pass" if term == expected else "warn",
                                   f"{term} terminals selected" + ("" if term == expected else
                                                                   f" (the setup says the photodetector is on "
                                                                   f"{expected})")))
    except Exception as e:
        out.append(CheckResult("Multimeter", "fail", f"{type(e).__name__}: {e}"))
    return out


def _daq(inst) -> CheckResult:
    d = inst.daq
    if not d.connected:
        return CheckResult("DAQ", "fail", "Not connected")
    expected = inst.cfg.daq.model
    if d.product_type != expected:
        return CheckResult("DAQ", "warn", f"Found {d.product_type}, config expects {expected}")
    return CheckResult("DAQ", "pass", f"{d.product_type} S/N {d.serial}")


def _kepco(inst) -> tuple[list[CheckResult], float | None]:
    if not inst.daq.connected:
        return [CheckResult("Kepco / magnet", "fail", "DAQ not connected")], None
    no_monitors = CheckResult(
        "Kepco monitors", "warn",
        f"Not wired (setup): the magnet is not verified and the current is the programmed V/R "
        f"(R = {inst.cfg.kepco.magnet_resistance_ohm:g} Ω). Current and overheating checks are off.")
    if not inst.monitors and not inst.hall_enabled:
        return [no_monitors], None
    kepco = inst.kepco
    out = []
    results = []
    halls = []
    try:
        kepco.read()    # throw-away capture: wakes the DAQ after idle time
        for v in (TEST_VOLTAGE, -TEST_VOLTAGE):
            kepco.set_voltage(v)
            time.sleep(0.15)
            reads, h = [], []
            for _ in range(MONITOR_READS):
                reads.append(kepco.read())
                h.append(inst.last_hall_mT())
            results.append((v, statistics.median(r[0] for r in reads), statistics.median(r[1] for r in reads)))
            if inst.hall_enabled:
                halls.append(statistics.median(h))
    except Exception as e:
        return [CheckResult("Kepco / magnet", "fail", f"{type(e).__name__}: {e}")], None
    finally:
        inst.safe_zero()

    if halls:
        sees_field = abs(halls[0] - halls[1]) >= MIN_HALL_CHANGE_MT
        # without monitors the Hall probe is the only proof that the magnet responds
        status = "pass" if sees_field else ("warn" if inst.monitors else "fail")
        hall_check = CheckResult("Hall probe", status,
                                 f"{halls[0]:+.3f} / {halls[1]:+.3f} mT at ±{TEST_VOLTAGE:g} V"
                                 + ("" if sees_field else ": the field does not change. Check the magnet, the "
                                                          "probe position, its wiring and hall_mT_per_V"))
    if not inst.monitors:
        return [no_monitors, hall_check], None
    gains_ok = all(abs(vm - v) <= GAIN_TOLERANCE * abs(v) + 0.02 for v, vm, _ in results)
    vtxt = ", ".join(f"{v:+.1f} V → {vm:+.3f} V" for v, vm, _ in results)
    out.append(CheckResult("Kepco voltage", "pass" if gains_ok else "fail",
                           vtxt if gains_ok else f"{vtxt}. Output does not follow the setpoint: "
                                                  "check Kepco power, mode and the AO0 cable / gain in config"))
    if halls:
        out.append(hall_check)
    currents = [i for _, _, i in results]
    if any(abs(i) < MIN_TEST_CURRENT_A for i in currents):
        out.append(CheckResult("Magnet current", "fail",
                               f"Current {currents[0]:+.3f} / {currents[1]:+.3f} A: magnet disconnected "
                               f"or current monitor cable ({inst.cfg.daq.ai_current}) not working"))
        return out, None
    r = sum(vm / i for _, vm, i in results) / len(results)
    r_cfg = inst.cfg.kepco.magnet_resistance_ohm
    r_ok = R_RANGE[0] * r_cfg <= r <= R_RANGE[1] * r_cfg
    out.append(CheckResult("Magnet current", "pass", f"{currents[0]:+.3f} / {currents[1]:+.3f} A"))
    out.append(CheckResult("Magnet resistance", "pass" if r_ok else "fail",
                           f"{r:.3f} Ω (config {r_cfg:.2f} Ω)"))
    return out, (r if r_ok else None)


def _laser(inst) -> CheckResult:
    name = "Photodetector signal"
    k = inst.keithley
    if not k.connected:
        return CheckResult(name, "fail", "Keithley not connected")
    try:
        value = k.read()
    except Exception as e:
        return CheckResult(name, "fail", f"{type(e).__name__}: {e}")
    rng = inst.cfg.keithley.range_V
    if abs(value) < DARK_SIGNAL_V:
        return CheckResult(name, "warn", f"{value * 1000:.2f} mV: almost no light. Normal only very close to "
                                         "extinction; otherwise check the laser and the alignment")
    if rng != "auto" and abs(value) > 0.9 * float(rng):
        return CheckResult(name, "warn", f"{value:.4f} V: near the {rng} V range, increase the range")
    if rng != "auto":
        better = [r for r in (0.1, 1.0, 10.0, 100.0) if r < float(rng) and abs(value) < 0.8 * r]
        if better:
            return CheckResult(name, "pass", f"{value:.4f} V. Tip: the {better[0]:g} V range gives "
                                             f"{float(rng) / better[0]:.0f}× finer resolution")
    return CheckResult(name, "pass", f"{value:.4f} V")


def run_diagnostics(inst) -> DiagnosticsReport:
    checks = _keithley(inst)
    checks.append(_daq(inst))
    kepco_checks, r = _kepco(inst)
    checks += kepco_checks
    checks.append(_laser(inst))
    for c in checks:
        log.info("Diagnostics %s: %s — %s", c.name, c.status, c.message)
    return DiagnosticsReport(checks, r)
