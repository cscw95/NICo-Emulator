"""NICo REST surface (/v2/org/{org}/nico) — spec.yaml paths, shared state
with /nico-bridge, v2.3 rack/tray/NVLink, OOB actions, firmware drift."""
import json

import pytest

from app import aiinfra

P = "/v2/org/neocloud/nico"


@pytest.fixture
def calls(monkeypatch):
    seen = []
    for name in ("chassis_reset", "gpu_reset", "reset_power", "attach_dpu", "detach_dpu"):
        def rec(*a, _n=name, **k):
            seen.append((_n,) + a)
            return {"attachment_id": "att-x"} if _n == "attach_dpu" else {}
        monkeypatch.setattr(aiinfra, name, rec)
    return seen


def _vpc(client, site="gasan", tenant="t-a", **extra):
    r = client.post(f"{P}/vpc", json={"name": "vpc-a", "siteId": site, "tenantId": tenant, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _it(client, site="gasan", sku="vr-nvl72"):
    return next(i for i in client.get(f"{P}/instance/type?siteId={site}").json() if i["name"] == sku)


def test_versions_and_metadata(client):
    v = client.get("/versions").json()
    assert v["nico"] == "2.3.0" and v["dsx_os"] == "2026.05" and v["k8s"] == ["1.33", "1.34"]
    assert client.get(f"{P}/metadata").json()["version"] == "2.3.0"
    assert client.get("/v2/org/any-org/nico/metadata").status_code == 200


def test_site_list_pagination_and_alias(client):
    r = client.get(f"{P}/site?includeMachineStats=true")
    assert r.status_code == 200 and isinstance(r.json(), list)
    pg = json.loads(r.headers["X-Pagination"])
    assert pg["total"] == 2 and pg["pageNumber"] == 1
    total = sum(s["machineStats"]["total"] for s in r.json())
    assert total == 2520
    gasan = client.get(f"{P}/site/gasan").json()
    assert client.get(f"{P}/site/{gasan['id']}").json()["name"] == gasan["name"]
    assert gasan["status"] == "Registered"


def test_error_shape_404(client):
    r = client.get(f"{P}/vpc/nope")
    assert r.status_code == 404
    assert r.json()["detail"]["source"] == "nico"


def test_machine_list_filters_and_page(client):
    r = client.get(f"{P}/machine?siteId=gasan&pageSize=5&pageNumber=2")
    assert len(r.json()) == 5
    assert json.loads(r.headers["X-Pagination"])["total"] == 648
    m = r.json()[0]
    assert m["id"].startswith("nh-su-") and m["status"] == "Ready" and m["lifecycleState"] == "ready"
    assert m["machineCapabilities"][0]["count"] == 4
    assert len(client.get(f"{P}/machine?rackId=su-1-rack-00&pageSize=100").json()) == 18


def test_network_crud(client):
    nsg = client.post(f"{P}/network-security-group",
                      json={"name": "nsg", "siteId": "gasan", "rules": [{"action": "Deny"}]}).json()
    assert nsg["ruleCount"] == 1
    nvl = client.post(f"{P}/nvlink-logical-partition", json={"name": "nvl", "siteId": "gasan"}).json()
    vpc = _vpc(client, networkSecurityGroupId=nsg["id"], nvLinkLogicalPartitionId=nvl["id"])
    assert vpc["vni"] >= 20001 and vpc["status"] == "Ready"
    sub = client.post(f"{P}/subnet", json={"name": "s", "vpcId": vpc["id"], "prefixLength": 24}).json()
    assert sub["siteId"] == vpc["siteId"] and sub["ipv4Gateway"]
    ib = client.post(f"{P}/infiniband-partition", json={"name": "ib", "siteId": "gasan"}).json()
    assert ib["partitionKey"] == "0x8001"
    sx = client.post(f"{P}/spectrumx-partition", json={"name": "sx", "siteId": "ansan"})
    assert sx.status_code == 201 and sx.json()["vni"]
    assert client.patch(f"{P}/spectrumx-partition/{sx.json()['id']}", json={}).status_code == 405
    # referential checks
    assert client.delete(f"{P}/vpc/{vpc['id']}").status_code == 400          # has subnet
    assert client.delete(f"{P}/network-security-group/{nsg['id']}").status_code == 400
    assert client.delete(f"{P}/subnet/{sub['id']}").status_code == 202
    assert client.delete(f"{P}/vpc/{vpc['id']}").status_code == 202
    assert client.get(f"{P}/vpc").json() == []
    assert client.post(f"{P}/vpc", json={"name": "x"}).status_code == 400   # siteId required


def test_instance_lifecycle_shares_bridge_state(client, calls):
    vpc = _vpc(client)
    it = _it(client)
    r = client.post(f"{P}/instance", json={"name": "i1", "tenantId": "t-a", "vpcId": vpc["id"],
                                           "instanceTypeId": it["id"]})
    assert r.status_code == 201, r.text
    inst = r.json()
    assert inst["status"] == "Ready" and inst["siteId"] == vpc["siteId"]
    m = client.get(f"{P}/machine/{inst['machineId']}").json()
    assert m["status"] == "InUse" and m["lifecycleState"] == "assigned" and m["instanceId"] == inst["id"]
    assert any(c[0] == "attach_dpu" for c in calls)            # DPU isolation on AI-Infra
    # same record through the deprecated bridge
    h = client.get(f"/nico-bridge/hosts/{inst['machineId']}").json()
    assert h["state"] == "allocated" and h["instance_id"] == inst["id"]
    assert _it(client)["allocationStats"]["used"] == 1
    # delete → sanitizing → ready (NICO_SANITIZE_SECONDS=0 in tests)
    assert client.delete(f"{P}/instance/{inst['id']}").status_code == 202
    m = client.get(f"{P}/machine/{inst['machineId']}").json()
    assert m["status"] == "Ready" and m["instanceId"] is None
    hist = [s["message"] for s in client.get(f"{P}/machine/{inst['machineId']}/status-history").json()]
    assert any("sanitizing" in x for x in hist) and "sanitize complete" in hist


def test_sanitizing_visible_while_in_progress(client, calls, monkeypatch):
    monkeypatch.setenv("NICO_SANITIZE_SECONDS", "60")
    vpc = _vpc(client)
    inst = client.post(f"{P}/instance", json={"name": "i", "tenantId": "t-a", "vpcId": vpc["id"],
                                              "instanceTypeId": _it(client)["id"]}).json()
    client.delete(f"{P}/instance/{inst['id']}")
    m = client.get(f"{P}/machine/{inst['machineId']}").json()
    assert m["status"] == "Reset" and m["lifecycleState"] == "sanitizing"


def test_bridge_allocation_visible_in_rest(client, calls):
    hid = "nh-su-2-rack-01-tray-03"
    client.post("/nico-bridge/instances", json={"host_id": hid, "tenant_ref": "t-b"})
    insts = client.get(f"{P}/instance?tenantId=t-b").json()
    assert len(insts) == 1 and insts[0]["machineId"] == hid
    assert client.get(f"{P}/machine/{hid}").json()["status"] == "InUse"
    # REST delete releases the bridge allocation too
    client.delete(f"{P}/instance/{insts[0]['id']}")
    assert client.get(f"/nico-bridge/hosts/{hid}").json()["instance_id"] is None


def test_allocation_constraint_quota(client, calls):
    it = _it(client)
    a = client.post(f"{P}/allocation", json={
        "name": "a", "tenantId": "t-q", "siteId": "gasan",
        "allocationConstraints": [{"resourceType": "InstanceType", "resourceTypeId": it["id"],
                                   "constraintType": "Reserved", "constraintValue": 1}]})
    assert a.status_code == 201 and a.json()["status"] == "Registered"
    a = a.json()
    vpc = _vpc(client, tenant="t-q")
    body = {"name": "i", "tenantId": "t-q", "vpcId": vpc["id"], "instanceTypeId": it["id"]}
    assert client.post(f"{P}/instance", json=body).status_code == 201
    assert client.post(f"{P}/instance", json=body).status_code == 409       # quota exhausted
    cid = a["allocationConstraints"][0]["id"]
    assert client.patch(f"{P}/allocation/{a['id']}/constraint/{cid}",
                        json={"constraintValue": 0}).status_code == 400      # below usage
    assert client.patch(f"{P}/allocation/{a['id']}/constraint/{cid}",
                        json={"constraintValue": 2}).json()["constraintValue"] == 2
    assert client.post(f"{P}/instance", json=body).status_code == 201
    assert _it(client)["allocationStats"]["allocated"] == 2


def test_expected_machine_day0_flow(client):
    r = client.post(f"{P}/expected-machine", json={
        "siteId": "ansan", "bmcMacAddress": "aa:bb:cc:00:00:01", "chassisSerialNumber": "CSN1",
        "serverName": "new-node-01", "defaultBmcPassword": "secret"})
    assert r.status_code == 201 and "defaultBmcPassword" not in r.json()
    mid = r.json()["machineId"]
    m = client.get(f"{P}/machine/{mid}").json()
    assert m["status"] == "Initializing" and m["lifecycleState"] == "expected"
    assert client.post(f"/emulator/v1/rest/machines/{mid}/advance").json()["lifecycleState"] == "discovered"
    run = client.post(f"{P}/machine/{mid}/validation/run", json={}).json()
    assert run["status"]["state"] == "Success"
    assert client.get(f"{P}/machine/{mid}").json()["lifecycleState"] == "ready"
    res = client.get(f"{P}/machine/{mid}/validation/result").json()
    assert {x["name"] for x in res} >= {"DcgmFullShort", "NcclAllReduce"}
    run = client.post(f"{P}/machine/{mid}/validation/run", json={"failTests": ["NvlinkBandwidth"]}).json()
    assert run["status"]["state"] == "Failed"
    assert client.get(f"{P}/machine/{mid}").json()["status"] == "Error"
    assert client.delete(f"{P}/expected-machine/{r.json()['id']}").status_code == 202
    assert client.get(f"{P}/machine/{mid}").status_code == 404


def test_machine_oob_actions(client, calls):
    mid = "nh-su-1-rack-00-tray-05"
    r = client.patch(f"{P}/machine/{mid}/power", json={"action": "ACPowercycle"})
    assert r.status_code == 202 and r.json()["delegated"]
    assert ("chassis_reset", "su-1-rack-00-tray-05", "PowerCycle") in calls
    client.patch(f"{P}/machine/{mid}/power", json={"action": "ForceRestart"})
    assert ("reset_power", "su-1-rack-00-tray-05", "ForceRestart") in calls
    assert client.post(f"{P}/machine/{mid}/gpu/reset", json={}).status_code == 202
    assert ("gpu_reset", "su-1-rack-00-tray-05") in calls
    assert client.patch(f"{P}/machine/{mid}/chassis/su-1-rack-00-tray-05/reset").status_code == 202
    assert client.patch(f"{P}/machine/{mid}/power", json={"action": "Bogus"}).status_code == 400


def test_power_on_assigned_machine_needs_ack(client, calls):
    vpc = _vpc(client)
    inst = client.post(f"{P}/instance", json={"name": "i", "tenantId": "t-a", "vpcId": vpc["id"],
                                              "instanceTypeId": _it(client)["id"]}).json()
    mid = inst["machineId"]
    assert client.post(f"{P}/machine/{mid}/gpu/reset", json={}).status_code == 400
    assert client.post(f"{P}/machine/{mid}/gpu/reset",
                       json={"acknowledgeAttachedInstance": True}).status_code == 202


def test_ai_infra_down_is_graceful(client, monkeypatch):
    def boom(*a, **k):
        raise aiinfra.AIInfraError("down")
    monkeypatch.setattr(aiinfra, "chassis_reset", boom)
    r = client.patch(f"{P}/tray/su-1-rack-00-tray-00/power", json={"siteId": "x", "state": "ACPowerCycle"})
    assert r.status_code == 200 and r.json()["delegated"] is False


def test_health_report_blocks_allocation(client, calls):
    mid = "nh-su-1-rack-00-tray-00"
    r = client.put(f"{P}/machine/{mid}/health-report", json={
        "source": "nvsentinel", "mode": "Override",
        "alerts": [{"id": "GpuXid79", "message": "GPU fell off the bus",
                    "classifications": ["PreventAllocations"]}]})
    assert r.status_code == 200
    m = client.get(f"{P}/machine/{mid}").json()
    assert m["isUsableByTenant"] is False and m["health"]["alerts"][0]["id"] == "GpuXid79"
    vpc = _vpc(client)
    assert client.post(f"{P}/instance", json={"name": "i", "tenantId": "t-a", "vpcId": vpc["id"],
                                              "machineId": mid}).status_code == 400
    assert client.delete(f"{P}/machine/{mid}/health-report/nvsentinel").status_code == 202
    assert client.get(f"{P}/machine/{mid}").json()["isUsableByTenant"] is True


def test_rack_tray_nvlink_and_leak(client):
    racks = client.get(f"{P}/rack?siteId=gasan&pageSize=100")
    assert json.loads(racks.headers["X-Pagination"])["total"] == 36
    rack = client.get(f"{P}/rack/su-1-rack-00").json()
    assert rack["nvLinkDomainIds"] == ["nvl-su-1-rack-00"] and len(rack["components"]) == 35
    assert rack["health"]["status"] in ("OK", "Warning")
    shelves = client.get(f"{P}/tray?rackId=su-1-rack-00&type=PowerShelf").json()
    assert len(shelves) == 8 and shelves[0]["nvLinkDomainId"] is None
    dom = client.get(f"{P}/domain/nvlink/nvl-su-1-rack-00").json()
    assert dom["gpus"] == 72 and dom["topology"] == "NVL72"
    # leak on a compute tray → Flow shuts it down, rack Critical, machine unusable
    t = client.post("/emulator/v1/rest/trays/su-1-rack-00-tray-02/leak",
                    json={"leakStatus": "Leaking"}).json()
    assert t["leakStatus"] == "Leaking" and t["leakHandlingStatus"] == "Down" and t["powerState"] == "Off"
    rack = client.get(f"{P}/rack/su-1-rack-00").json()
    assert rack["health"]["status"] == "Critical" and rack["health"]["leakStatus"] == "Leaking"
    assert client.get(f"{P}/machine/nh-su-1-rack-00-tray-02").json()["isUsableByTenant"] is False
    assert client.get(f"{P}/domain/nvlink/nvl-su-1-rack-00").json()["operationStatus"] == "Degraded"
    probe = client.get(f"{P}/site/gasan/health-probe").json()
    leak = next(p for p in probe["probes"] if p["id"] == "LeakDetection")
    assert leak["status"] == "Critical" and probe["status"] == "Critical"
    client.post("/emulator/v1/rest/trays/su-1-rack-00-tray-02/leak", json={"leakStatus": "NoLeak"})
    assert client.get(f"{P}/tray/su-1-rack-00-tray-02").json()["operationStatus"] == "Ready"


def test_leak_from_ai_infra_redfish(client, monkeypatch):
    monkeypatch.setattr(aiinfra, "thermal",
                        lambda tid: {"LeakDetection": {"Status": {"Health": "Critical"}}}
                        if tid.endswith("tray-07") else {"LeakDetection": {"Status": {"Health": "OK"}}})
    assert client.get(f"{P}/tray/su-1-rack-00-tray-07").json()["leakStatus"] == "Leaking"
    assert client.get(f"{P}/tray/su-1-rack-00-tray-06").json()["leakStatus"] == "NoLeak"


def test_rack_tray_health_reports_and_power(client, calls):
    r = client.put(f"{P}/rack/su-1-rack-01/health-report",
                   json={"source": "rms", "mode": "Merge",
                         "alerts": [{"id": "PowerShelfFault", "message": "PSU 3 failed"}]})
    assert r.status_code == 200
    reports = client.get(f"{P}/rack/su-1-rack-01/health-report").json()
    assert reports[0]["source"] == "rms"
    assert client.get(f"{P}/rack/su-1-rack-01").json()["health"]["status"] == "Critical"
    assert client.delete(f"{P}/rack/su-1-rack-01/health-report/rms").status_code == 202
    client.put(f"{P}/tray/su-1-rack-01-ps-03/health-report",
               json={"source": "rms", "mode": "Merge", "alerts": [{"id": "PsuFault", "message": "x"}]})
    assert client.get(f"{P}/tray/su-1-rack-01-ps-03").json()["health"]["status"] == "Critical"
    r = client.patch(f"{P}/rack/su-1-rack-01/power", json={"siteId": "gasan", "state": "acpowercycle"})
    assert r.json()["state"] == "ACPowerCycle" and r.json()["trays"] == 18
    assert sum(1 for c in calls if c[0] == "chassis_reset") == 18
    assert client.get(f"{P}/tray/su-1-rack-01-tray-00/power").json()["powerState"] == "On"
    assert client.patch(f"{P}/tray/su-1-rack-01-tray-00/power",
                        json={"siteId": "gasan", "state": "Off"}).json()["state"] == "Off"
    assert client.get(f"{P}/tray/su-1-rack-01-tray-00/power").json()["powerState"] == "Off"


def test_firmware_baseline_and_drift(client):
    d = client.get(f"{P}/firmware/drift").json()
    assert d["total"] > 0
    tray = d["trays"][0]
    rid = tray["rackId"]
    assert client.get(f"{P}/rack/{rid}/validation").json()["totalDiffs"] >= 1
    r = client.patch(f"{P}/rack/{rid}/firmware", json={"siteId": tray["siteId"]}).json()
    assert tray["trayId"] in r["updated"]
    assert client.get(f"{P}/firmware/drift?rackId={rid}").json()["total"] == 0
    # raising the baseline makes the whole fleet drift for that component
    client.put(f"{P}/firmware/baseline", json={"baseline": {"PowerShelf": {"PSU": "3.5.0"}}})
    assert client.get(f"{P}/firmware/baseline").json()["baseline"]["PowerShelf"]["PSU"] == "3.5.0"
    assert client.get(f"{P}/firmware/drift").json()["total"] >= 140 * 8
    assert client.put(f"{P}/firmware/baseline", json={"Bogus": {}}).status_code == 400


def test_os_sshkey_and_instance_refs(client, calls):
    oss = client.get(f"{P}/operating-system").json()
    assert {o["name"] for o in oss} >= {"ubuntu-24.04-dgx", "nvke-node-1.34"}
    k = client.post(f"{P}/sshkey", json={"name": "k", "publicKey": "ssh-ed25519 AAAA test"}).json()
    assert client.post(f"{P}/sshkey", json={"name": "k", "publicKey": "nope"}).status_code == 400
    g = client.post(f"{P}/sshkeygroup", json={"name": "g", "sshKeyIds": [k["id"]],
                                              "siteIds": ["gasan"]}).json()
    assert g["sshKeys"][0]["id"] == k["id"]
    vpc = _vpc(client)
    body = {"name": "i", "tenantId": "t-a", "vpcId": vpc["id"], "instanceTypeId": _it(client)["id"],
            "operatingSystemId": oss[0]["id"], "sshKeyGroupIds": [g["id"]]}
    assert client.post(f"{P}/instance", json=body).status_code == 201
    assert client.post(f"{P}/instance", json={**body, "sshKeyGroupIds": ["x"]}).status_code == 400
    assert client.post(f"{P}/instance", json={**body, "vpcId": "x"}).status_code == 400


def test_reset_clears_rest_state(client):
    _vpc(client)
    client.post("/emulator/v1/rest/trays/su-1-rack-00-tray-02/leak", json={})
    client.post("/emulator/v1/reset")
    assert client.get(f"{P}/vpc").json() == []
    assert client.get(f"{P}/tray/su-1-rack-00-tray-02").json()["leakStatus"] == "NoLeak"
