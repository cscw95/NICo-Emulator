"""F08 (NOCP writes on the NICo REST surface, bridge call log) and F12 (machine
state events → DSX Exchange) — incl. contract checks against the upstream
AsyncAPI (docs/upstream/nico-machine-state.asyncapi.yaml) and, when the ref
tree is present, the infra-controller OpenAPI spec."""
from pathlib import Path

import pytest
import yaml

from app import aiinfra
from tests.schema_check import validate

P = "/v2/org/neocloud/nico"
ASYNCAPI = yaml.safe_load(open(Path(__file__).parent.parent / "docs/upstream/"
                               "nico-machine-state.asyncapi.yaml"))


def _spec():
    for d in Path(__file__).resolve().parents:
        f = d / "ref/upstream/nico/rest-api/openapi/spec.yaml"
        if f.exists():
            return yaml.safe_load(open(f))
    pytest.skip("ref/upstream NICo spec.yaml not found")


@pytest.fixture
def published(monkeypatch):
    seen = []
    monkeypatch.setattr(aiinfra, "exchange_publish",
                        lambda topic, type_, subject, data, **k: seen.append(
                            {"topic": topic, "type": type_, "subject": subject, "data": data}))
    for n in ("attach_dpu", "reset_power", "provision"):
        monkeypatch.setattr(aiinfra, n, lambda *a, _n=n, **k: seen.append({"ai": _n, "args": a})
                            or {"attachment_id": "att-x"})
    return seen


def _vpc(client, tenant="t-a"):
    return client.post(f"{P}/vpc", json={"name": f"vpc-{tenant}", "siteId": "gasan",
                                         "tenantId": tenant}).json()


def _it(client):
    return next(i for i in client.get(f"{P}/instance/type?siteId=gasan").json()
                if i["name"] == "vr-nvl72")


def _create(client, vpc, **extra):
    r = client.post(f"{P}/instance", json={"name": "i", "tenantId": vpc["tenantId"],
                                           "vpcId": vpc["id"], "instanceTypeId": _it(client)["id"],
                                           **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _states(published, mid):
    return [e["data"]["data"]["managed_host_state"] for e in published
            if "subject" in e and e["data"]["data"]["machine_id"] == mid]


def test_instance_ready_publishes_one_ready_event(client, published):
    inst = _create(client, _vpc(client))
    st = _states(published, inst["machineId"])
    ready = [s for s in st if s["state"] == "assigned" and s["instance_state"]["state"] == "ready"]
    assert len(ready) == 1
    assert st[0] == {"state": "assigned", "instance_state": {"state": "init"}}
    ev = next(e for e in published if "subject" in e)
    assert ev["topic"] == ev["subject"] == f"NICO/v1/machine/{inst['machineId']}/state"
    assert ev["data"]["specversion"] == "1.0" and ev["data"]["type"] == "com.nvidia.nico.v1.machine.state"
    # instance boot drives the AI-Infra twin (power cycle + PXE) like the old bridge step
    assert {"reset_power", "provision", "attach_dpu"} <= {e.get("ai") for e in published}


def test_delete_publishes_cleanup_then_ready(client, published):
    inst = _create(client, _vpc(client))
    client.delete(f"{P}/instance/{inst['id']}")
    client.get(f"{P}/machine/{inst['machineId']}")         # lazy cleanup end (0 s in tests)
    names = [s["state"] for s in _states(published, inst["machineId"])]
    assert names[-2:] == ["waitingforcleanup", "ready"]


def test_events_match_upstream_asyncapi(client, published):
    inst = _create(client, _vpc(client))
    client.delete(f"{P}/instance/{inst['id']}")
    client.get(f"{P}/machine/{inst['machineId']}")
    from app import machine_events
    failed = machine_events.managed_host_state("failed", "nh-x", message="dcgm")
    msg = ASYNCAPI["components"]["messages"]["ManagedHostStateChangeMessage"]["payload"]
    payloads = [e["data"]["data"] for e in published if "subject" in e]
    payloads.append({"machine_id": "nh-x", "timestamp": "2026-11-10T00:00:00Z",
                     "managed_host_state": failed})
    payloads.append({"machine_id": "nh-x", "timestamp": "2026-11-10T00:00:00Z",
                     "managed_host_state": machine_events.managed_host_state("decommissioned", "nh-x")})
    assert len(payloads) >= 4
    for p in payloads:
        assert validate(p, msg, ASYNCAPI) == [], p
    assert validate({"machine_id": "x", "timestamp": "t",
                     "managed_host_state": {"state": "bogus"}}, msg, ASYNCAPI)   # checker bites


def test_machine_events_outbox(client, published):
    inst = _create(client, _vpc(client))
    evs = client.get(f"/emulator/v1/machine-events?machineId={inst['machineId']}").json()
    assert [e["data"]["managed_host_state"]["state"] for e in evs] == ["assigned", "assigned"]
    assert "_delivered" not in evs[0]


def test_terminated_instance_status_history(client, published):
    inst = _create(client, _vpc(client))
    assert client.delete(f"{P}/instance/{inst['id']}").status_code == 202
    assert client.get(f"{P}/instance/{inst['id']}").status_code == 404     # gone, like upstream
    hist = client.get(f"{P}/instance/{inst['id']}/status-history").json()
    assert [h["status"] for h in hist][-2:] == ["Terminating", "Terminating"]
    assert hist[-1]["message"].startswith("Terminated: machine")


def test_requested_partition_key_and_ib_attach(client, published):
    vpc = _vpc(client)
    ib = client.post(f"{P}/infiniband-partition", json={
        "name": "ib-t-a", "siteId": "gasan", "tenantId": "t-a",
        "labels": {"neocloud.io/partition-key": "0x8123"}}).json()
    assert ib["partitionKey"] == "0x8123"
    dup = client.post(f"{P}/infiniband-partition", json={
        "name": "ib2", "siteId": "gasan", "labels": {"neocloud.io/partition-key": "0x8123"}}).json()
    assert dup["partitionKey"] != "0x8123"                  # taken → auto-assigned
    inst = _create(client, vpc)
    n = len([e for e in published if e.get("ai") == "attach_dpu"])
    r = client.patch(f"{P}/instance/{inst['id']}",
                     json={"infinibandInterfaces": [{"partitionId": ib["id"], "isPhysical": True}]})
    assert r.status_code == 200 and r.json()["infinibandInterfaces"][0]["partitionId"] == ib["id"]
    att = [e for e in published if e.get("ai") == "attach_dpu"]
    assert len(att) == n + 1 and att[-1]["args"][2]["ib_pkey"] == 0x8123


def test_held_machine_not_autopicked(client, published):
    vpc = _vpc(client)
    first = client.get(f"{P}/machine?siteId=gasan&status=Ready&pageSize=1").json()[0]
    client.patch(f"{P}/machine/{first['id']}", json={"labels": {"nocp.state": "reserved"}})
    inst = _create(client, vpc)
    assert inst["machineId"] != first["id"]
    inst2 = _create(client, vpc, machineId=first["id"])     # targeted create still works
    assert inst2["machineId"] == first["id"]


def test_bridge_call_log(client):
    assert client.get("/emulator/v1/bridge-calls").json()["count"] == 0
    client.get(f"{P}/machine?pageSize=1")
    assert client.get("/emulator/v1/bridge-calls").json()["count"] == 0
    client.get("/nico-bridge/hosts/nh-x")
    b = client.get("/emulator/v1/bridge-calls").json()
    assert b["count"] == 1 and b["calls"][0]["path"] == "/nico-bridge/hosts/nh-x"
    ev = client.get("/emulator/v1/events").json()
    assert any(e["message_id"] == "NICo.2.3.DeprecatedBridgeCall" for e in ev)
    client.post("/emulator/v1/reset")
    assert client.get("/emulator/v1/bridge-calls").json()["count"] == 0


def test_rest_responses_match_spec(client, published):
    spec = _spec()
    sc = spec["components"]["schemas"]
    vpc = _vpc(client)
    alloc = client.post(f"{P}/allocation", json={
        "name": "a", "tenantId": "t-a", "siteId": "gasan",
        "allocationConstraints": [{"resourceType": "InstanceType", "resourceTypeId": _it(client)["id"],
                                   "constraintType": "Reserved", "constraintValue": 2}]}).json()
    ib = client.post(f"{P}/infiniband-partition", json={"name": "ib", "siteId": "gasan"}).json()
    inst = _create(client, vpc)
    client.delete(f"{P}/instance/{inst['id']}")
    hist = client.get(f"{P}/instance/{inst['id']}/status-history").json()
    for obj, name in ((alloc, "Allocation"), (ib, "InfiniBandPartition"), (vpc, "VPC"),
                      (inst, "Instance")):
        errs = [e for e in validate(obj, sc[name], spec) if "expected" in e or "not in" in e]
        assert errs == [], (name, errs[:5])
    for h in hist:
        assert validate(h, sc["StatusDetail"], spec) == []


def test_cpu_pool_machine_on_rest(client, published):
    m = client.get(f"{P}/machine/nh-cpu-node-03")
    assert m.status_code == 200 and m.json()["controllerMachineType"] == "cpu-epyc"
    assert m.json()["lifecycleState"] == "ready"
    assert client.get(f"{P}/machine/nh-cpu-node-x").status_code == 404
