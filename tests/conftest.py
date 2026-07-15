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


@pytest.fixture(autouse=True)
def _isolate_ai_infra(monkeypatch):
    """Default: stub the AI Infra client so unit tests never touch the live
    :9100 twin (no shared-state pollution). Tests that explicitly request the
    ``require_ai_infra`` fixture opt back into the real emulator."""
    monkeypatch.setattr(aiinfra, "list_racks", lambda **k: {"racks": []})
    monkeypatch.setattr(aiinfra, "list_leases", lambda **k: [])
    monkeypatch.setattr(aiinfra, "list_dpus", lambda **k: [])
    monkeypatch.setattr(aiinfra, "get_dpu", lambda *a, **k: {})
    monkeypatch.setattr(aiinfra, "reset_power", lambda *a, **k: {})
    monkeypatch.setattr(aiinfra, "provision", lambda *a, **k: {})
    monkeypatch.setattr(aiinfra, "attach_dpu",
                        lambda *a, **k: {"attachment_id": "att-test"})
    monkeypatch.setattr(aiinfra, "detach_dpu", lambda *a, **k: {})
    monkeypatch.setattr(aiinfra, "send_traffic", lambda *a, **k: {})
    monkeypatch.setattr(aiinfra, "inject_fault", lambda *a, **k: {})
    monkeypatch.setattr(aiinfra, "recover_dpu", lambda *a, **k: {})


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
