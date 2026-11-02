import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mokeapp.config import load_config  # noqa: E402
from mokeapp.drivers import Instruments  # noqa: E402
from mokeapp.drivers.simulated import SimLab  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    c = load_config(ROOT / "config" / "example_setup.yaml")     # the setup shipped in git
    c.kepco.slew_rate_V_per_s = 1e6  # no ramp delays in tests
    # never touch the lab's real calibration folder
    c.calibration_path = tmp_path / "calibrations"
    c.calibration = None
    return c


@pytest.fixture
def sim(cfg):
    inst = Instruments(cfg, simulate=True, sim_lab=SimLab(cfg.kepco))
    inst.connect()
    yield inst
    inst.close()
