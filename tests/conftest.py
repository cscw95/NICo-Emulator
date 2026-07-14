"""Shared pytest fixtures for the NICo Emulator (control-plane) test suite.

NICo delegates physical state to the AI Infra Emulator (:9100). Integration
tests that need real physical effects require :9100 to be running; they are
skipped automatically when it is unreachable.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import STORE
from app import bridge, aiinfra

# Probe AI Infra once at collection time.
_AI = aiinfra.ping()
AI_INFRA_UP = bool(_AI.get("reachable"))


def _reset_nico():
    STORE.reset()
    bridge.reset_bridge()


@pytest.fixture
def client():
    """Fresh TestClient with NICo control-plane state reset before/after."""
    _reset_nico()
    with TestClient(app) as c:
        yield c
    _reset_nico()


@pytest.fixture
def require_ai_infra():
    """Skip the test unless the AI Infra Emulator (:9100) is reachable."""
    if not AI_INFRA_UP:
        pytest.skip(f"AI Infra Emulator unreachable at {aiinfra.AI_INFRA_URL}")
