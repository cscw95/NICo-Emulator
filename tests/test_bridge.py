"""NOCP /nico-bridge contract tests — control-plane orchestration with
physical effects delegated to the AI Infra Emulator (:9100)."""
import uuid

from app import aiinfra


def _nonce():
    return uuid.uuid4().hex[:8]


# ── full-fleet host list (needs AI Infra to enumerate the 2,520 trays) ──

def test_list_hosts_graceful_when_ai_infra_down(client, monkeypatch):
    """If AI Infra is unreachable, /hosts returns the NICo overlay, not 500."""
    def _boom(*a, **k):
        raise aiinfra.AIInfraError("simulated AI Infra outage")
    monkeypatch.setattr(aiinfra, "list_dpus", _boom)
    r = client.get("/nico-bridge/hosts")
    assert r.status_code == 200
    assert isinstance(r.json(), list)   # empty overlay is fine


# ── provision drives AI Infra (Redfish reset + PXE/DHCP provisioning) ───


