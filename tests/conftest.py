from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from aiogram_stars import MemoryStorage, Storage
from tests.helpers import Clock, connect_sqlalchemy

POSTGRES_URL = os.environ.get("STARS_TEST_POSTGRES_URL", "")

requires_postgres = pytest.mark.skipif(
    not POSTGRES_URL, reason="set STARS_TEST_POSTGRES_URL to run the PostgreSQL tests"
)


def database_url(kind: str, tmp_path: Path) -> str:
    """The URL of an empty database of ``kind``: a fresh SQLite file or the PostgreSQL one."""
    if kind == "sqlite":
        return f"sqlite+aiosqlite:///{tmp_path / 'stars.db'}"
    return POSTGRES_URL


@pytest.fixture(
    params=[
        "memory",
        "sqlite",
        pytest.param("postgres", marks=requires_postgres),
    ]
)
def backend(request: pytest.FixtureRequest) -> str:
    """Every storage test runs against each backend."""
    kind: str = request.param
    return kind


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=requires_postgres)])
def sql_backend(request: pytest.FixtureRequest) -> str:
    """Tests that need a database shared by several connections or processes."""
    kind: str = request.param
    return kind


@pytest.fixture
async def storage(backend: str, tmp_path: Path) -> AsyncIterator[Storage]:
    if backend == "memory":
        yield MemoryStorage()
        return
    async with connect_sqlalchemy(database_url(backend, tmp_path), fresh=True) as sql:
        yield sql


@pytest.fixture
def clock() -> Clock:
    return Clock()
