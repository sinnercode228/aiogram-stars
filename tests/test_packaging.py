"""The installed distribution: extras and version."""

from __future__ import annotations

import importlib.util
import sys
from importlib.metadata import requires, version
from pathlib import Path

import pytest
from packaging.requirements import Requirement

import aiogram_stars
import aiogram_stars.sqlalchemy

DISTRIBUTION = "aiogram-stars"


def extra_requirements(extra: str) -> set[str]:
    found = set()
    for line in requires(DISTRIBUTION) or []:
        requirement = Requirement(line)
        if requirement.marker is not None and requirement.marker.evaluate({"extra": extra}):
            found.add(requirement.name.lower())
    return found


@pytest.mark.parametrize(("extra", "driver"), [("sqlite", "aiosqlite"), ("postgres", "asyncpg")])
def test_driver_extras_also_install_sqlalchemy(extra: str, driver: str) -> None:
    # pip install "aiogram-stars[postgres]" must be enough for SqlAlchemyStorage on PostgreSQL.
    assert extra_requirements(extra) >= {"sqlalchemy", driver}


def test_version_matches_the_distribution() -> None:
    assert aiogram_stars.__version__ == version(DISTRIBUTION)


def test_missing_sqlalchemy_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sqlalchemy", None)  # makes "import sqlalchemy" fail
    path = Path(aiogram_stars.sqlalchemy.__file__ or "")
    spec = importlib.util.spec_from_file_location("aiogram_stars._sqlalchemy_probe", path)
    assert spec is not None
    assert spec.loader is not None

    with pytest.raises(ModuleNotFoundError, match=r'pip install "aiogram-stars\[sqlalchemy\]"'):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
