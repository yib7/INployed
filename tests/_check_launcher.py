import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "local"))

spec = importlib.util.spec_from_file_location(
    "open_dashboard", ROOT / "local" / "open_dashboard.pyw"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

paths, err = m._resolve_sources()
print("error:", err)
print("sources:", [str(p) for p in paths])
assert err is None, err
assert paths, "no sources resolved"
print("LAUNCHER RESOLVE OK")
