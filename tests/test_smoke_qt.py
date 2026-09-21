"""Run the dashboard smoke check with the suite's isolated data and environment."""
import runpy
from pathlib import Path


def test_dashboard_smoke(capsys):
    smoke = Path(__file__).with_name("smoke_qt.py")
    namespace = runpy.run_path(str(smoke))
    assert namespace["main"]() == 0
    assert "SMOKE TEST OK" in capsys.readouterr().out
