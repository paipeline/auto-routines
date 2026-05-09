"""
pytest configuration — load scripts/sanity-check.py as an importable module
even though its filename has a hyphen.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANITY_PATH = ROOT / "scripts" / "sanity-check.py"
STATUS_PATH = ROOT / "scripts" / "status.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


sanity = _load_module("sanity_check", SANITY_PATH)
status = _load_module("status", STATUS_PATH)
