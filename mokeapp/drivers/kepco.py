"""Kepco BOP bipolar supply driven by a DAQ analog output, read back through its rear monitors.

All values here are at the magnet: volts at the Kepco output and amperes through the magnet.
"""
import math
import threading
import time

from mokeapp.config import KepcoConfig

RAMP_STEP_S = 0.005


class KepcoBOP:
    def __init__(self, cfg: KepcoConfig, daq, max_voltage: float):
        self.cfg = cfg
        self.daq = daq
        self.max_voltage = max_voltage
        self.resistance_ohm = cfg.magnet_resistance_ohm
        self.output_V = 0.0
        self._lock = threading.Lock()

    def clamp(self, volts: float) -> float:
        return max(-self.max_voltage, min(self.max_voltage, volts))

    def set_voltage(self, volts: float) -> float:
        """Ramp the magnet voltage to `volts` (clamped) at the configured slew rate. Returns the value set."""
        target = self.clamp(volts)
        with self._lock:
            max_step = self.cfg.slew_rate_V_per_s * RAMP_STEP_S
            delta = target - self.output_V
            n = max(1, math.ceil(abs(delta) / max_step))
            for k in range(1, n + 1):
                v = self.output_V + delta * k / n if k < n else target
                self.daq.write_ao(v / self.cfg.gain)
                if k < n:
                    time.sleep(RAMP_STEP_S)
            self.output_V = target
        return target

    def ramp_to_zero(self) -> None:
        self.set_voltage(0.0)

    def to_physical(self, i_mon: float, v_mon: float) -> tuple[float, float]:
        """Monitor volts -> (magnet volts, magnet amperes)."""
        return v_mon * self.cfg.voltage_monitor_V_per_V, i_mon * self.cfg.current_monitor_A_per_V

    def read(self) -> tuple[float, float]:
        """Measured (volts, amperes) at the magnet."""
        return self.to_physical(*self.daq.read_monitors())

    def expected_current(self, volts: float) -> float:
        return volts / self.resistance_ohm

    def programmed_current(self, volts: float) -> float:
        """Current we ask for: V / nominal R from the setup. Fixed and reproducible (it does not follow the
        R measured each day), so a field calibration made against it stays valid."""
        return volts / self.cfg.magnet_resistance_ohm
