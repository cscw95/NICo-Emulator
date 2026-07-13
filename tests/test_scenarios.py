"""Built-in fault scenarios: list endpoint + all 5 pass end-to-end."""
import pytest

from app.scenarios import SCENARIOS

SCENARIO_NAMES = [s["name"] for s in SCENARIOS]


def test_scenarios_list(client):
    r = client.get("/emulator/v1/scenarios")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 5
    required = {"name", "title", "category", "severity", "description"}
    for s in data:
        assert required <= set(s), s
    assert {s["name"] for s in data} == set(SCENARIO_NAMES)


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_scenario_runs_and_passes(client, name):
    r = client.post(f"/emulator/v1/scenarios/{name}/run", json={})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["name"] == name
    assert data["dpu_id"]
    assert data["steps"]
    assert data["assertions"]
    for a in data["assertions"]:
        assert a["ok"] is True, (name, a)
    for s in data["steps"]:
        assert s["ok"] is True, (name, s)
    assert data["passed"] is True, data


def test_scenario_accepts_target_dpu(client):
    dpu = "vr-rack-001-ct-10-dpu-0"
    r = client.post("/emulator/v1/scenarios/inter-tenant-isolation/run",
                    json={"dpu_id": dpu})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["dpu_id"] == dpu
    assert data["passed"] is True
    assert data["telemetry_delta"]["dpu_intertenant_drops_total"] >= 1000


def test_unknown_scenario_404(client):
    r = client.post("/emulator/v1/scenarios/does-not-exist/run", json={})
    assert r.status_code == 404
