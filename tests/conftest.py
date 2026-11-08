"""Make the integration's ``core`` package importable without Home Assistant.

``core`` is plain Python. Importing it through ``custom_components.pv_forecast_correction``
would run the integration's ``__init__`` (which needs Home Assistant), so it is loaded
on its own under the name ``pvfc_core``, exactly as ``tools/replay.py`` does.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "custom_components" / "pv_forecast_correction" / "core"

if "pvfc_core" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "pvfc_core", CORE / "__init__.py", submodule_search_locations=[str(CORE)]
    )
    assert _spec is not None
    assert _spec.loader is not None
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["pvfc_core"] = _module
    _spec.loader.exec_module(_module)
