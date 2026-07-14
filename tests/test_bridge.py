"""NOCP /nico-bridge contract tests — control-plane orchestration with
physical effects delegated to the AI Infra Emulator (:9100)."""
import uuid

from app import aiinfra


def _nonce():
    return uuid.uuid4().hex[:8]


# ── full-fleet host list (needs AI Infra to enumerate the 2,520 trays) ──
def test_list_hosts_full_fleet(client, require_ai_infra):
    r = client.get("/nico-bridge/hosts")
    assert r.status_code == 200
    hosts = r.json()
    assert len(hosts) == 2520, f"expected full 2,520-tray fleet, got {len(hosts)}"
    h = hosts[0]
    # NicoHost contract shape
    for k in ("host_id", "tray_id", "sku", "site", "state", "firmware_ok",
              "attested", "cordoned", "instance_id"):
        assert k in h, f"missing NicoHost field {k}"
    assert h["host_id"] == f"nh-{h['tray_id']}"
    assert h["sku"] == "vr-nvl72"


def test_list_hosts_graceful_when_ai_infra_down(client, monkeypatch):
    """If AI Infra is unreachable, /hosts returns the NICo overlay, not 500."""
    def _boom(*a, **k):
        raise aiinfra.AIInfraError("simulated AI Infra outage")
    monkeypatch.setattr(aiinfra, "list_dpus", _boom)
    r = client.get("/nico-bridge/hosts")
    assert r.status_code == 200
    assert isinstance(r.json(), list)   # empty overlay is fine


# ── provision drives AI Infra (Redfish reset + PXE/DHCP provisioning) ───
def test_provision_reflects_on_ai_infra(client, require_ai_infra):
    host = "nh-su-1-rack-01-tray-03"
    tray = host[3:]
    client.post(f"/nico-bridge/hosts/{host}/reserve")
    r = client.post(f"/nico-bridge/hosts/{host}/provision", json={"image_ref": "os-img-1"})
    assert r.status_code == 200
    job = r.json()
    assert job["op"] == "provision" and job["state"] == "succeeded"
    # AI Infra should now show the tray provisioning with a DHCP lease
    prov = aiinfra.get_provision(tray)
    assert prov["lifecycle_state"] == "Provisioning"
    assert prov["lease"]["tray_id"] == tray


# ── allocate creates a real DPU tenant attachment on AI Infra ───────────
def test_allocate_creates_dpu_attachment(client, require_ai_infra):
    host = "nh-su-1-rack-02-tray-05"
    did = f"{host[3:]}-dpu-0"
    tenant = f"tnt-{_nonce()}"
    client.post(f"/nico-bridge/hosts/{host}/reserve")
    r = client.post("/nico-bridge/instances",
                    json={"host_id": host, "tenant_ref": tenant})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "allocated"
    assert body["tenant_ref"] == tenant
    iid = body["instance_id"]
    # verify the isolation attachment landed on the AI Infra DPU
    dpu = aiinfra.get_dpu(did)
    assert tenant in dpu.get("tenants", []), "tenant not isolated on AI Infra DPU"

    # ── release tears the attachment back down ──────────────────────────
    rr = client.delete(f"/nico-bridge/instances/{iid}")
    assert rr.status_code == 200
    dpu2 = aiinfra.get_dpu(did)
    assert tenant not in dpu2.get("tenants", []), "attachment not detached on release"


# ── segment creates attachments across host DPUs, delete tears down ─────
def test_segment_isolation_via_ai_infra(client, require_ai_infra):
    hosts = ["nh-su-1-rack-03-tray-00", "nh-su-1-rack-03-tray-01"]
    tenant = f"seg-{_nonce()}"
    r = client.post("/nico-bridge/segments", json={
        "tenant_ref": tenant, "vrf": tenant, "l3vni": 55555,
        "converged_vni": 66666, "host_ids": hosts})
    assert r.status_code == 200
    seg = r.json()
    sid = seg["segment_id"]
    assert seg["vrf_dataplane"] == "vpc_55555"
    assert seg["host_ids"] == hosts
    # both host DPUs should now carry the tenant
    for h in hosts:
        did = f"{h[3:]}-dpu-0"
        assert tenant in aiinfra.get_dpu(did).get("tenants", [])
    # delete tears every attachment back down
    dr = client.delete(f"/nico-bridge/segments/{sid}")
    assert dr.status_code == 200
    for h in hosts:
        did = f"{h[3:]}-dpu-0"
        assert tenant not in aiinfra.get_dpu(did).get("tenants", [])
