# Pytest configuration for backend API tests.
# Forces a local SQLite DB, disables Redis, and wires JWT secrets for deterministic runs.
import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

# Test-time environment: local SQLite DB, Redis disabled, predictable JWT secret
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("STAYCIRCLE_JWT_SECRET", "test-secret")

import sys
# Ensure backend/ is on sys.path so 'app' resolves when running pytest from the repo root
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.main import app  # noqa: E402
from app.db import Base, engine  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_db() -> Iterator[None]:
    """
    Session-level database bootstrap using a local SQLite file.

    Drops and recreates schema once per test session to ensure a clean slate.
    """
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _clean_db() -> Iterator[None]:
    """
    Function-level isolation: drop and recreate schema before each test.

    Simple but effective for this small suite; avoids transactional complexity.
    """
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """
    FastAPI TestClient bound to the application for HTTP-level tests.
    """
    with TestClient(app) as c:
        yield c
