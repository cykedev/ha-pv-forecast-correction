"""The core package must not depend on Home Assistant."""

import subprocess
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[2] / "custom_components" / "pv_forecast_correction" / "core"

SCRIPT = f"""
import builtins, importlib.util, sys
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "homeassistant" or name.startswith("homeassistant."):
        raise ImportError("core must not import Home Assistant")
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
spec = importlib.util.spec_from_file_location(
    "standalone_core", r"{CORE / "__init__.py"}", submodule_search_locations=[r"{CORE}"]
)
module = importlib.util.module_from_spec(spec)
sys.modules["standalone_core"] = module
spec.loader.exec_module(module)
for name in ("archive", "replay", "score", "engine"):
    __import__("standalone_core." + name)
"""


def test_core_imports_without_home_assistant():
    subprocess.run([sys.executable, "-c", SCRIPT], check=True)
