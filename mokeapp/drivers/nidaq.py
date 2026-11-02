"""Generic NI-DAQmx device: one AO channel (Kepco programming) and, optionally, AI channels for the
Kepco monitors and a Hall probe.

Works with any NI DAQ supported by NI-DAQmx (USB-6002, USB-6421, ...): only the setup changes. The AO
range is read from the device instead of being assumed.
"""
import logging
import threading

import nidaqmx
import nidaqmx.system
from nidaqmx.constants import AcquisitionType, TerminalConfiguration

from mokeapp.config import DaqConfig

log = logging.getLogger(__name__)

AO_LIMIT_V = 10.0   # never program more than ±10 V, even if the DAQ could
NAN = float("nan")


def device_ao_limit(dev) -> float:
    """Largest symmetric AO range the device supports, capped at AO_LIMIT_V (±10 V if unknown)."""
    try:
        rng = list(dev.ao_voltage_rngs)
        highs = [min(abs(lo), hi) for lo, hi in zip(rng[0::2], rng[1::2]) if lo < 0 < hi]
        if highs:
            return min(AO_LIMIT_V, max(highs))
    except Exception:
        log.debug("Could not read the AO ranges of %s", getattr(dev, "name", dev), exc_info=True)
    return AO_LIMIT_V


class NiDaq:
    def __init__(self, cfg: DaqConfig):
        self.cfg = cfg
        self.product_type = ""
        self.serial = ""
        self.ao_limit = AO_LIMIT_V
        self._ao = None
        self._ai = None
        self._ai_roles: list[str] = []      # what each AI channel is: "current", "voltage", "hall"
        self._lock = threading.RLock()
        self.last_hall_V: float | None = None   # Hall probe voltage of the last capture, if one is wired

    @property
    def connected(self) -> bool:
        return self._ao is not None

    def connect(self) -> None:
        self.close()
        try:
            self._connect_once()
        except Exception:
            # after a USB drop-out the device often needs a reset before tasks can be created again
            log.warning("DAQ connection failed, resetting %s and retrying", self.cfg.device, exc_info=True)
            nidaqmx.system.Device(self.cfg.device).reset_device()
            self._connect_once()

    def _connect_once(self) -> None:
        dev = nidaqmx.system.Device(self.cfg.device)
        self.product_type = dev.product_type
        self.serial = str(dev.serial_num or "")
        self.ao_limit = device_ao_limit(dev)
        try:
            self._ao = nidaqmx.Task()
            self._ao.ao_channels.add_ao_voltage_chan(self.cfg.ao_channel, min_val=-self.ao_limit,
                                                     max_val=self.ao_limit)
            channels = []
            if self.cfg.monitors:
                channels += [("current", self.cfg.ai_current), ("voltage", self.cfg.ai_voltage)]
            if self.cfg.hall_enabled:
                channels.append(("hall", self.cfg.ai_hall))
            self._ai_roles = [role for role, _ in channels]
            if channels:
                self._ai = nidaqmx.Task()
                term = TerminalConfiguration[self.cfg.ai_terminal]
                for _, ch in channels:
                    self._ai.ai_channels.add_ai_voltage_chan(ch, terminal_config=term, min_val=-10, max_val=10)
                self._ai.timing.cfg_samp_clk_timing(
                    rate=self.cfg.ai_rate, sample_mode=AcquisitionType.FINITE,
                    samps_per_chan=self.cfg.ai_samples)
        except Exception:
            self.close()
            raise

    def write_ao(self, volts: float) -> None:
        volts = max(-self.ao_limit, min(self.ao_limit, float(volts)))
        with self._lock:
            self._ao.write(volts)

    def start_capture(self) -> None:
        """Start a finite capture of the AI channels (runs in hardware while the multimeter reads)."""
        with self._lock:
            if self._ai is None:
                return
            self._ai.stop()  # in case a previous capture was interrupted
            self._ai.start()

    def finish_capture(self) -> tuple[float, float]:
        """Wait for the capture; mean (current monitor V, voltage monitor V), NaN without monitors."""
        with self._lock:
            if self._ai is None:
                self.last_hall_V = None
                return NAN, NAN
            try:
                data = self._ai.read(number_of_samples_per_channel=self.cfg.ai_samples, timeout=5.0)
            finally:
                self._ai.stop()
        if len(self._ai_roles) == 1:        # one channel: NI-DAQmx returns a flat list
            data = [data]
        means = {role: sum(d) / len(d) for role, d in zip(self._ai_roles, data)}
        self.last_hall_V = means.get("hall")
        return means.get("current", NAN), means.get("voltage", NAN)

    def read_monitors(self) -> tuple[float, float]:
        with self._lock:    # start + finish as one step, so another thread cannot restart the capture
            self.start_capture()
            return self.finish_capture()

    def close(self) -> None:
        with self._lock:
            for task in (self._ao, self._ai):
                if task is not None:
                    try:
                        task.close()
                    except Exception:
                        pass
            self._ao = None
            self._ai = None
