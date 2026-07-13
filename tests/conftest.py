"""Shared pytest fixtures for the NICo Emulator test suite."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import STORE


@pytest.fixture
def client():
    """Fresh TestClient with the store reset to the seeded twin before/after."""
    STORE.reset()
    with TestClient(app) as c:
        yield c
    STORE.reset()
