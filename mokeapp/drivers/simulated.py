"""Simulated Keithley and DAQ so the app and the tests run without hardware.

SimLab models the magnet (resistor, optional disconnection) and a sample with a square-ish
hysteresis loop plus slow laser drift. Same public interface as the real drivers.
"""
import math
import random
import threading
import time

from mokeapp.config import DaqConfig, KeithleyConfig, KepcoConfig


class SimLab:
    def __init__(self, kepco: KepcoConfig, coercive_A: float = 0.6, width_A: float = 0.15,
                 read_delay_s: float = 0.0):
        self.kepco = kepco
        self.ao = 0.0
        self.resistance_ohm = kepco.magnet_resistance_ohm
        self.magnet_connected = True
        self.monitor_glitches = 0   # the next N monitor captures read 0 (like the USB-6002 sometimes does)
        self.drift_V_per_s = 0.002  # slow laser drift
        self.coercive_A = coercive_A
        self.width_A = width_A
        self.read_delay_s = read_delay_s
        self.t0 = time.monotonic()
        self._last_i = 0.0
        self._branch = 1
        self._lock = threading.Lock()

    def output_voltage(self) -> float:
        return self.kepco.gain * self.ao

    def current(self) -> float:
        if not self.magnet_connected:
            return 0.0
        i = self.output_voltage() / self.resistance_ohm
        return max(-self.kepco.i_max, min(self.kepco.i_max, i))

    def signal(self) -> float:
        with self._lock:
            i = self.current()
            if abs(i - self._last_i) > 1e-6:
                self._branch = 1 if i > self._last_i else -1
                self._last_i = i
            m = math.tanh((i - self._branch * self.coercive_A) / self.width_A)
        drift = self.drift_V_per_s * (time.monotonic() - self.t0)
        return 1.0 + 0.05 * m + drift + random.gauss(0, 2e-4)


class SimKeithley:
    def __init__(self, cfg: KeithleyConfig, lab: SimLab):
        from mokeapp.drivers.multimeter import PROFILES
        self.cfg = cfg
        self.lab = lab
        self.idn = ""
        self.profile = PROFILES["k2000"]
        self.connected = False

    def connect(self) -> None:
        from mokeapp.drivers.multimeter import PROFILES, detect_profile
        self.idn = "SIMULATED,MODEL 2000,0,0"
        key = detect_profile(self.idn) if self.cfg.profile == "auto" else self.cfg.profile
        self.profile = PROFILES[key]
        self.connected = True

    def configure(self, range_V, nplc, filter, filter_count) -> None:
        self.cfg.range_V, self.cfg.nplc = range_V, nplc
        self.cfg.filter, self.cfg.filter_count = filter, filter_count

    @property
    def read_time_s(self) -> float:
        return self.lab.read_delay_s

    def read(self) -> float:
        if self.lab.read_delay_s:
            time.sleep(self.lab.read_delay_s)
        return self.lab.signal()

    def input_terminal(self) -> str:
        return "REAR"

    def errors(self) -> list[str]:
        return []

    def close(self) -> None:
        self.connected = False


SIM_MT_PER_A = 50.0    # field of the simulated magnet, seen by the simulated Hall probe


class SimDaq:
    def __init__(self, cfg: DaqConfig, lab: SimLab):
        self.cfg = cfg
        self.lab = lab
        self.product_type = "SIMULATED"
        self.serial = "0"
        self.connected = False
        self.last_hall_V = None
        self.ao_limit = 10.0

    def connect(self) -> None:
        self.product_type = self.cfg.model
        self.connected = True

    def write_ao(self, volts: float) -> None:
        self.lab.ao = max(-self.ao_limit, min(self.ao_limit, float(volts)))

    def start_capture(self) -> None:
        pass

    def finish_capture(self) -> tuple[float, float]:
        if self.lab.monitor_glitches > 0:
            self.lab.monitor_glitches -= 1
            return 0.0, 0.0
        k = self.lab.kepco
        if self.cfg.hall_enabled:
            b = SIM_MT_PER_A * self.lab.current()
            self.last_hall_V = (b - self.cfg.hall_offset_mT) / self.cfg.hall_mT_per_V + random.gauss(0, 1e-4)
        if not self.cfg.monitors:
            return float("nan"), float("nan")
        i_mon = self.lab.current() / k.current_monitor_A_per_V + random.gauss(0, 2e-4)
        v_mon = self.lab.output_voltage() / k.voltage_monitor_V_per_V + random.gauss(0, 2e-4)
        return i_mon, v_mon

    def read_monitors(self) -> tuple[float, float]:
        return self.finish_capture()

    def close(self) -> None:
        self.connected = False
