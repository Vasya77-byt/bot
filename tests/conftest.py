import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    """Изолирует FileTTLCache в tmp каждый тест: cross-user persistent
    cache (DaData/FNS/SBIS/ZCHB) не должен делиться между тестами и не
    должен трогать реальную .cache/ репозитория."""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
