"""F28 Health Validation rules (CEL) → NodeVerdict → machine validation run."""
import json
import pathlib

import pytest

from app import cel_lite
from tests.schema_check import validate

P = "/v2/org/neocloud/nico"
HV = f"{P}/x-neocloud/health-validation/rules"
VERDICT_SCHEMA = json.loads((pathlib.Path(__file__).parent / "contracts" /
                             "node-verdict.schema.json").read_text())
# spec.yaml components/schemas/MachineValidationResult.required (infra-controller 9ace631fa361)
RESULT_REQUIRED = {"name", "description", "command", "args", "stdOut", "stdErr", "context",
                   "exitCode", "startTime", "endTime", "validationID", "testID"}


def _mid(client):
    return client.get(f"{P}/machine?siteId=gasan&pageSize=1").json()[0]["id"]


def _verdict(client, mid):
    v = client.get(f"{P}/machine/{mid}/x-neocloud/health-verdict").json()
    assert validate(v, VERDICT_SCHEMA) == [], validate(v, VERDICT_SCHEMA)
    return v


# ── CEL subset ────────────────────────────────────────────────────────
@pytest.mark.parametrize("expr,env,want", [
    ("a + b * 2 == 7", {"a": 1, "b": 3}, True),
    ("x > 3 && !(y == 'z')", {"x": 4, "y": "w"}, True),
    ("xs.exists(v, v > 5) || false", {"xs": [1, 9]}, True),
    ("xs.all(v, v > 0) && size(xs) == 2", {"xs": [1, 9]}, True),
    ("xs.filter(v, v > 1).size()", {"xs": [1, 2, 3]}, 2),
    ("m.k in [1, 2] ? 'hit' : 'miss'", {"m": {"k": 2}}, "hit"),
    ("has(m.k) && !has(m.q)", {"m": {"k": 1}}, True),
    ("max(xs) - min(xs)", {"xs": [4, 9, 1]}, 8),
    ("name.startsWith('gpu') && name.matches('^gpu-[0-9]+$')", {"name": "gpu-12"}, True),
    ("false && missing > 1", {}, False),              # CEL commutative logic absorbs the error
    ("{'a': 1}['a'] + int('2')", {}, 3),
])
def test_cel_subset(expr, env, want):
    assert cel_lite.run(expr, env) == want


@pytest.mark.parametrize("expr", ["missing > 1", "1 +", "'a' < 3", "__import__('os')",
                                  "x.y", "1 / 0", "(" * 80 + "1" + ")" * 80])
def test_cel_errors_are_cel_errors(expr):
    with pytest.raises(cel_lite.CelError):
        cel_lite.run(expr, {"x": 1})


def test_identifiers_skip_macro_variables():
    ast = cel_lite.compile_expr("gpus.exists(g, g.T > thresholds.t) && n == 1")
    assert cel_lite.identifiers(ast) == {"gpus", "thresholds", "n"}


# ── rule CRUD (x-neocloud) ────────────────────────────────────────────
def test_seeded_rules_and_extension_marker(client):
    r = client.get(HV)
    assert r.status_code == 200 and r.headers["X-NeoCloud-Extension"] == "health-validation-rules"
    names = {x["name"] for x in r.json()}
    assert {"gpu-xid-critical", "gpu-thermal", "ib-link-down", "node-leak"} <= names
    assert all(x["x-neocloud"] is True and {"name", "expr", "sensors"} <= set(x) for x in r.json())
    spec = client.get("/openapi.json").json()["paths"]
    assert spec["/v2/org/{org}/nico/x-neocloud/health-validation/rules"]["get"]["x-neocloud"] is True


def test_put_rejects_bad_rules(client):
    r = client.put(f"{HV}/bad", json={"expr": "DCGM_FI_DEV_XID_ERROR >"})
    assert r.status_code == 400 and "expr" in r.json()["detail"]["data"]
    r = client.put(f"{HV}/bad", json={"expr": "gpu_count == 4", "sensors": ["ib_ports_up"]})
    assert r.status_code == 400 and "sensors" in r.json()["detail"]["data"]
    r = client.put(f"{HV}/bad", json={"expr": "gpu_count == 4", "severity": "PASS"})
    assert r.status_code == 400


def test_custom_rule_lifecycle(client):
    mid = _mid(client)
    body = {"expr": "gpus.exists(g, g.DCGM_FI_DEV_POWER_USAGE > thresholds.max_w)",
            "sensors": ["DCGM_FI_DEV_POWER_USAGE"], "severity": "WARNING",
            "category": "gpu", "thresholds": {"max_w": 2000}}
    r = client.put(f"{HV}/gpu-power", json=body)
    assert r.status_code == 201 and r.json()["sensors"] == ["DCGM_FI_DEV_POWER_USAGE"]
    assert client.put(f"{HV}/gpu-power", json={**body, "thresholds": {"max_w": 100}}).status_code == 200
    v = _verdict(client, mid)
    chk = next(c for c in v["check_results"] if c["check_id"] == "gpu-power")
    assert chk["severity"] == "WARNING" and len(chk["affected_entities"]) == 4
    assert chk["thresholds"]["max_w"] == {"value": 100, "original_value": 2000,
                                          "category": "ADJUSTABLE", "overridden": True}
    assert v["overall_severity"] == "WARNING"
    # disabled → skipped, unknown sensor → SENSOR_MISSING
    client.put(f"{HV}/gpu-power", json={**body, "enabled": False})
    client.put(f"{HV}/needs-ib-temp", json={"expr": "ib_temp_c > 1", "sensors": ["ib_temp_c"]})
    v = _verdict(client, mid)
    skipped = {c["check_id"]: c["skip_reason"] for c in v["skipped_checks"]}
    assert skipped == {"gpu-power": "CHECK_DISABLED", "needs-ib-temp": "SENSOR_MISSING"}
    assert v["overall_severity"] == "PASS"
    assert client.delete(f"{HV}/gpu-power").status_code == 202
    assert client.get(f"{HV}/gpu-power").status_code == 404


def test_dry_run_evaluate(client):
    r = client.post(f"{HV}:evaluate", json={"expr": "gpu_count != 4",
                                            "observations": {"node": {"gpu_count": 3}}})
    assert r.status_code == 200 and r.json()["severity"] == "CRITICAL"
    r = client.post(f"{HV}:evaluate", json={"expr": "gpu_count != 4", "machineId": _mid(client)})
    assert r.json()["severity"] == "PASS"


# ── verdict + validation run ──────────────────────────────────────────
def test_healthy_node_passes(client):
    mid = _mid(client)
    v = _verdict(client, mid)
    assert v["schema_version"] == "0.5" and v["overall_severity"] == "PASS"
    assert "recommended_action" not in v and v["node_id"] == mid
    run = client.post(f"{P}/machine/{mid}/validation/run", json={}).json()
    assert run["status"]["state"] == "Success"
    res = client.get(f"{P}/machine/{mid}/validation/result").json()
    hv = [x for x in res if x["testID"].startswith("neocloud_hv_")]
    assert len(hv) >= 8 and all(set(x) == RESULT_REQUIRED for x in res)
    assert run["status"]["total"] == len(res)


def test_injected_xid_fails_validation_and_drains(client):
    mid = _mid(client)
    obs = client.get(f"{P}/machine/{mid}/x-neocloud/observations").json()
    gpu1 = obs["gpus"][1]["uuid"]
    r = client.put(f"/emulator/v1/rest/machines/{mid}/observations",
                   json={"signals": {"DCGM_FI_DEV_XID_ERROR": 79}, "gpu": 1})
    assert r.status_code == 200
    v = _verdict(client, mid)
    assert v["overall_severity"] == "CRITICAL" and v["severity_by_domain"]["xid"] == "CRITICAL"
    chk = next(c for c in v["check_results"] if c["check_id"] == "gpu-xid-critical")
    assert chk["affected_entities"] == [gpu1]
    assert v["recommended_action"] == {"operational_state": "drain",
                                       "remediation_actions": ["RESET_GPU", "REBOOT_NODE", "REPLACE_TRAY"]}
    run = client.post(f"{P}/machine/{mid}/validation/run", json={}).json()
    assert run["status"]["state"] == "Failed"
    res = {x["testID"]: x for x in client.get(f"{P}/machine/{mid}/validation/result").json()}
    assert res["neocloud_hv_gpu-xid-critical"]["exitCode"] == 1
    assert res["forge_DcgmFullShort"]["exitCode"] == 0          # legacy tests unaffected
    # filter: only legacy tests → health rules not run
    run = client.post(f"{P}/machine/{mid}/validation/run",
                      json={"allowedTests": ["DcgmFullShort"]}).json()
    assert run["status"]["state"] == "Success" and run["status"]["total"] == 1
    assert client.delete(f"/emulator/v1/rest/machines/{mid}/observations").status_code == 200
    assert _verdict(client, mid)["overall_severity"] == "PASS"


def test_warning_does_not_fail_run_and_bad_signal_rejected(client):
    mid = _mid(client)
    client.put(f"/emulator/v1/rest/machines/{mid}/observations",
               json={"signals": {"DCGM_FI_DEV_GPU_TEMP_CELSIUS": 91}})
    v = _verdict(client, mid)
    assert v["overall_severity"] == "WARNING"
    assert client.post(f"{P}/machine/{mid}/validation/run", json={}).json()["status"]["state"] == "Success"
    r = client.put(f"/emulator/v1/rest/machines/{mid}/observations", json={"signals": {"nope": 1}})
    assert r.status_code == 400


def test_leak_on_tray_is_critical(client):
    mid = _mid(client)
    tray = client.get(f"{P}/machine/{mid}").json()["metadata"]["trayId"]
    assert client.post(f"/emulator/v1/rest/trays/{tray}/leak", json={"leaking": True}).status_code == 200
    v = _verdict(client, mid)
    assert "node-leak" in [c["check_id"] for c in v["check_results"] if c["severity"] == "CRITICAL"]


def test_rules_match_neocloud_ext_schema(client):
    schema = json.loads((pathlib.Path(__file__).parent / "contracts" /
                         "health-validation-rule.schema.json").read_text())
    for rule in client.get(HV).json():
        assert validate(rule, schema) == [], (rule["name"], validate(rule, schema))
