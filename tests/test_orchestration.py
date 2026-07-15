"""Site-controller orchestration-state tests.

These exercise NICo's *own* orchestration view (host lifecycle state machine,
jobs, tenant segments, HA) surfaced on the per-site controller endpoints. They
drive the NOCP bridge in-memory and do NOT require AI Infra (:9100): the
physical effects are best-effort and skipped gracefully when it is down, while
the control-plane state machine still advances.
"""
import pytest

from app import aiinfra

GASAN_HOST = "nh-su-1-rack-00-tray-00"   # su-1 -> gasan
ANSAN_HOST = "nh-su-4-rack-00-tray-00"   # su-4 -> ansan



def _gasan(client):
    body = client.get("/emulator/v1/sites").json()
    return next(s for s in body["sites"] if s["site_id"] == "gasan")


def test_orchestration_block_shape(client):
    """Every site instance carries an orchestration block with the full shape."""
    s = _gasan(client)
    orch = s["orchestration"]
    assert set(orch) >= {"hosts_by_state", "managed_hosts", "active_jobs",
                         "recent_jobs", "segments", "tenants_served"}
    assert set(orch["hosts_by_state"]) == {
        "pool_ready", "reserved", "provisioning", "provisioned",
        "allocated", "released"}
    # untouched fleet -> nothing orchestrated yet
    assert orch["managed_hosts"] == 0
    assert orch["tenants_served"] == []


def test_host_state_aggregation_by_site(client):
    """Driving the bridge lifecycle is reflected in the correct site's counts,
    and hosts belonging to other sites are not mixed in."""
    client.post(f"/nico-bridge/hosts/{GASAN_HOST}/reserve")
    client.post(f"/nico-bridge/hosts/{ANSAN_HOST}/reserve")
    client.post("/nico-bridge/instances",
                json={"host_id": GASAN_HOST, "tenant_ref": "tnt-orch"})

    s = _gasan(client)
    orch = s["orchestration"]
    assert orch["managed_hosts"] == 1           # only the gasan host
    assert orch["hosts_by_state"]["allocated"] == 1
    assert orch["hosts_by_state"]["reserved"] == 0
    assert "tnt-orch" in orch["tenants_served"]


def test_segments_scoped_to_site(client):
    """A tenant segment shows up under the site of its member hosts."""
    client.post("/nico-bridge/segments", json={
        "tenant_ref": "tnt-seg", "vrf": "tnt-seg", "l3vni": 54321,
        "converged_vni": 65432, "host_ids": [GASAN_HOST]})
    orch = _gasan(client)["orchestration"]
    segs = orch["segments"]
    assert len(segs) == 1
    seg = segs[0]
    assert seg["tenant_ref"] == "tnt-seg"
    assert seg["l3vni"] == 54321
    assert seg["host_count"] == 1
    assert "tnt-seg" in orch["tenants_served"]


def test_ha_nodes_raft_view(client):
    """HA block is a 3-node Raft view: one leader, two followers, 3/3 quorum."""
    s = _gasan(client)
    ha = s["ha"]
    assert ha["quorum"] == "3/3"
    assert ha["leader"] == "nico-gasan-0"
    nodes = ha["nodes"]
    assert len(nodes) == 3
    assert [n["role"] for n in nodes] == ["leader", "follower", "follower"]
    assert nodes[0]["name"] == "nico-gasan-0"
    for n in nodes:
        assert n["state"] == "healthy"
        assert isinstance(n["raft_term"], int) and n["raft_term"] >= 1


def test_service_detail_wired_to_orchestration(client):
    """Machine Controller / NICo API Service details reflect live host state."""
    client.post(f"/nico-bridge/hosts/{GASAN_HOST}/reserve")
    client.post("/nico-bridge/instances",
                json={"host_id": GASAN_HOST, "tenant_ref": "tnt-svc"})
    s = _gasan(client)
    svc = {x["name"]: x for x in s["services"]}
    assert "alloc 1" in svc["Machine Controller"]["detail"]
    assert "1 host(s) orchestrated" in svc["NICo API Service"]["detail"]


def test_drilldown_includes_orchestration_and_ha(client):
    """get_site drill-down carries orchestration + ha alongside the roll-up."""
    r = client.get("/emulator/v1/sites/gasan")
    assert r.status_code == 200
    body = r.json()
    assert "orchestration" in body and "ha" in body
    assert "scalable_units" in body
