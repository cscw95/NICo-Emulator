"""F23 POST /rack (bring-up registration) and F25 machine attestation."""
P = "/v2/org/neocloud/nico"
RID = "bu-sn-new-0001"


def _register(client, **kw):
    body = {"name": RID, "siteId": "gasan", "serial": "SN-NEW-0001",
            "location": {"row": "R3", "position": "07"}, **kw}
    return client.post(f"{P}/rack", json=body)


def test_register_rack_creates_trays_machines_domain(client):
    r = _register(client)
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["id"] == RID and v["operationStatus"] == "Initializing"
    assert v["health"]["trayStatusCounts"]["Compute"]["total"] == 18
    assert v["health"]["trayStatusCounts"]["NVSwitch"]["total"] == 9
    assert v["health"]["trayStatusCounts"]["PowerShelf"]["total"] == 4
    assert client.get(f"{P}/rack/{RID}").status_code == 200
    ms = client.get(f"{P}/machine", params={"rackId": RID, "pageSize": 50}).json()
    assert len(ms) == 18 and {m["lifecycleState"] for m in ms} == {"expected"}
    assert not any(m["isUsableByTenant"] for m in ms)
    d = client.get(f"{P}/domain/nvlink/nvl-{RID}")
    assert d.status_code == 200


def test_register_rack_validation(client):
    assert client.post(f"{P}/rack", json={"name": "x"}).status_code == 400
    assert _register(client, siteId="nowhere").status_code == 404
    assert _register(client).status_code == 201
    assert _register(client).status_code == 409                       # same name
    assert _register(client, name="other").status_code == 409         # same serial
    assert client.post(f"{P}/rack", json={"name": "su-1-rack-00", "siteId": "gasan",
                                          "serial": "S"}).status_code == 409


def test_validation_makes_rack_ready(client):
    _register(client)
    ms = client.get(f"{P}/machine", params={"rackId": RID, "pageSize": 50}).json()
    bad = ms[0]["id"]
    r = client.post(f"{P}/machine/{bad}/validation/run", json={"failTests": ["DcgmFullShort"]})
    assert r.json()["status"]["state"] == "Failed"
    assert client.get(f"{P}/rack/{RID}").json()["operationStatus"] == "Error"
    for m in ms:
        client.post(f"{P}/machine/{m['id']}/validation/run", json={})
    assert client.get(f"{P}/rack/{RID}").json()["operationStatus"] == "Ready"
    ms = client.get(f"{P}/machine", params={"rackId": RID, "pageSize": 50}).json()
    assert {m["status"] for m in ms} == {"Ready"}


def test_attestation_default_and_forced(client):
    a = client.get(f"{P}/machine/nh-su-1-rack-00-tray-00/attestation").json()
    assert a["measuredBoot"] == "Passed" and a["attested"] is True
    assert set(a["tpm"]["pcrs"]) >= {"0", "2", "4", "7"}
    assert a["dpu"]["attested"] is True and a["verifiedAt"]
    # tray id accepted as alias
    assert client.get(f"{P}/machine/su-1-rack-00-tray-00/attestation").status_code == 200
    mid = "nh-su-1-rack-00-tray-01"
    golden = client.get(f"{P}/machine/{mid}/attestation").json()["tpm"]["pcrs"]["0"]
    f = client.put(f"/emulator/v1/rest/machine/{mid}/attestation",
                   json={"measuredBoot": "Failed"}).json()
    assert f["measuredBoot"] == "Failed" and f["attested"] is False
    assert f["tpm"]["pcrs"]["0"] != golden
    f = client.put(f"/emulator/v1/rest/machine/{mid}/attestation",
                   json={"dpuAttested": False}).json()
    assert f["measuredBoot"] == "Passed" and f["dpu"]["attested"] is False
    assert f["attested"] is False
    assert client.put(f"/emulator/v1/rest/machine/{mid}/attestation",
                      json={"measuredBoot": "Maybe"}).status_code == 400
    assert client.get(f"{P}/machine/nope/attestation").status_code == 404


def test_attestation_uses_ai_infra_dpu_state(client, monkeypatch):
    from app import aiinfra
    monkeypatch.setattr(aiinfra, "get_dpu",
                        lambda *a, **k: {"attestation_state": "FIRMWARE_UNTRUSTED"})
    a = client.get(f"{P}/machine/nh-su-1-rack-00-tray-02/attestation").json()
    assert a["dpu"] == {"dpuMachineId": "su-1-rack-00-tray-02-dpu-0", "attested": False,
                        "state": "FIRMWARE_UNTRUSTED", "source": "ai-infra"}
    assert a["attested"] is False


# ── upstream contract (infra-controller spec.yaml / measured_boot.proto) ──
from tests._oas import check, load  # noqa: E402

S = load("nico_spec_subset.json")


def _site_uuid(client):
    return next(s["id"] for s in client.get(f"{P}/site").json() if s["name"] == "STT 가산")


def test_expected_rack_matches_upstream_schema(client):
    sid = _site_uuid(client)
    req = {"siteId": sid, "rackId": "rack-new-07", "name": "Rack 07",
           "description": "row R3",
           "labels": {"chassis.manufacturer": "NVIDIA", "chassis.serial-number": "NVRK0007",
                      "chassis.model": "VR-NVL72", "location.room": "R3",
                      "location.position": "07"}}
    assert check(req, S["ExpectedRackCreateRequest"], S) == []
    r = client.post(f"{P}/expected-rack", json=req)
    assert r.status_code == 201, r.text
    assert check(r.json(), S["ExpectedRack"], S) == []
    assert r.json()["rackId"] == "rack-new-07" and r.json()["siteId"] == sid
    # rack discovered with machines expected
    assert client.get(f"{P}/rack/rack-new-07").json()["serialNumber"] == "NVRK0007"
    assert client.get(f"{P}/expected-rack").json()[0]["rackId"] == "rack-new-07"
    assert client.get(f"{P}/expected-rack/rack-new-07").status_code == 200
    dup = client.post(f"{P}/expected-rack", json=req)
    assert dup.status_code == 409
    assert check(dup.json()["detail"], S["NICoAPIError"], S) == []
    assert client.post(f"{P}/expected-rack", json={"siteId": sid}).status_code == 400


def test_rack_bringup_matches_upstream_schema(client):
    sid = _site_uuid(client)
    client.post(f"{P}/expected-rack", json={"siteId": sid, "rackId": "rack-b1"})
    body = {"siteId": sid, "description": "Initial bring up"}
    assert check(body, S["BringUpRackRequest"], S) == []
    r = client.post(f"{P}/rack/rack-b1/bringup", json=body)
    assert r.status_code == 200
    assert check(r.json(), S["BringUpRackResponse"], S) == [] and len(r.json()["taskIds"]) == 1
    assert client.get(f"{P}/rack/rack-b1").json()["taskStats"]["total"] == 1
    assert client.post(f"{P}/rack/nope/bringup", json=body).status_code == 404
    assert client.post(f"{P}/rack/rack-b1/bringup", json={}).status_code == 400


def _proto_fields(message):
    import json
    from pathlib import Path
    src = Path(__file__).parent / "fixtures" / "measured_boot_fields.json"
    return json.loads(src.read_text())[message]


def test_attestation_report_matches_measured_boot_proto(client):
    a = client.get(f"{P}/machine/nh-su-1-rack-00-tray-00/attestation").json()
    camel = lambda f: f.split("_")[0] + "".join(w.title() for w in f.split("_")[1:])  # noqa: E731
    assert set(map(camel, _proto_fields("MeasurementReportPb"))) <= set(a["report"])
    assert set(map(camel, _proto_fields("MeasurementReportValueRecordPb"))) <= \
        set(a["report"]["values"][0])
    assert a["state"] in _proto_fields("MeasurementMachineStatePb")
    assert a["report"]["machineId"] == a["machineId"]
