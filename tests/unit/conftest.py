"""Shared fixtures for the unit-test suite."""

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ai_e2e_harness.py"


@pytest.fixture(scope="session")
def h():
    """Import the harness module by path (it is not a package).

    Session-scoped on purpose: ``ai_e2e_run`` does ``import ai_e2e_harness``
    and binds the module object at import time, so every test file that
    patches harness attributes must share the one copy in ``sys.modules``.
    A per-module fixture would hand later files a fresh copy whose patches
    the already-imported ``ai_e2e_run`` never sees.
    """
    spec = importlib.util.spec_from_file_location("ai_e2e_harness", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ai_e2e_harness"] = module
    spec.loader.exec_module(module)
    return module
