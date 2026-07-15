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




# ── cascading reset + segment host attach (Managed K8s CP parity) ──────────
def test_reset_cascade_reports_ai_infra(client, monkeypatch):
    """?cascade=true propagates to AI Infra — unreachable is surfaced, not fatal."""
    from app import aiinfra as ai
    monkeypatch.setattr(ai, "AI_INFRA_URL", "http://127.0.0.1:1")   # 닫힌 포트
    r = client.post("/emulator/v1/reset?cascade=true").json()
    assert r["scope"] == "nico-control-plane+ai-infra"
    assert r["ai_infra"]["status"] == "unreachable"
    # 캐스케이드 없이 부르면 기존 계약 그대로
    r = client.post("/emulator/v1/reset").json()
    assert r["scope"] == "nico-control-plane" and "ai_infra" not in r


def test_segment_attach_hosts_for_k8s_cp(client):
    """NOCP Managed K8s CP(CPU) 노드가 Converged로 세그먼트에 합류하는 계약."""
    seg = client.post("/nico-bridge/segments", json={
        "tenant_ref": "tnt-k8s", "vrf": "VRF-k8s", "l3vni": 20001,
        "converged_vni": 30001,
        "host_ids": ["nh-su-1-rack-00-tray-00"]}).json()
    cp = ["nh-cpu-node-01", "nh-cpu-node-02", "nh-cpu-node-03"]
    r = client.patch(f"/nico-bridge/segments/{seg['segment_id']}/hosts",
                     json={"host_ids": cp, "purpose": "k8s-control-plane"})
    assert r.status_code == 200
    body = r.json()
    assert all(h in body["host_ids"] for h in cp)
    # 멱등: 재호출해도 중복 추가 없음
    r2 = client.patch(f"/nico-bridge/segments/{seg['segment_id']}/hosts",
                      json={"host_ids": cp}).json()
    assert r2["host_ids"].count("nh-cpu-node-01") == 1
    # CPU 풀 호스트는 sku로 구분된다
    h = client.get("/nico-bridge/hosts/nh-cpu-node-01").json()
    assert h["sku"] == "cpu-epyc"
    # 미존재 세그먼트는 404
    assert client.patch("/nico-bridge/segments/seg-none/hosts",
                        json={"host_ids": cp}).status_code == 404
