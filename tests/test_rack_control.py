"""Rack control plane — 전체 랙 제어 API·텔레메트리 반영 검증."""
from fastapi.testclient import TestClient

from app.main import app
from app.store import STORE

client = TestClient(app)
OBS = "/emulator/v1/obs"


def _rack(rid):
    return next(r for r in client.get(f"{OBS}/racks").json()
                if r["rack_id"] == rid)


def _first_rack_id():
    return next(iter(STORE.racks))


def test_power_off_zeroes_gpus_and_marks_rack():
    rid = _first_rack_id()
    r = client.post(f"{OBS}/racks/{rid}/control", json={"action": "power_off"})
    assert r.status_code == 200 and r.json()["state"]["power_state"] == "off"
    view = _rack(rid)
    assert view["power_state"] == "off"
    assert view["it_power_kw"] < 1.0
    gpus = client.get(f"{OBS}/dcgm/gpus", params={"rack": rid, "limit": 5}).json()
    assert all(g["power_w"] == 0 and g["util_pct"] == 0 for g in gpus["gpus"])
    # 원복
    client.post(f"{OBS}/racks/{rid}/control", json={"action": "power_on"})
    assert _rack(rid)["power_state"] == "on"


def test_bulk_power_cap_sets_throttle_reason():
    r = client.post(f"{OBS}/racks/control", json={
        "scope": {"all": True}, "action": "power_cap",
        "params": {"cap_pct": 40}})
    body = r.json()
    assert r.status_code == 200
    assert body["matched"] == len(STORE.racks) == body["applied"]
    views = client.get(f"{OBS}/racks").json()
    assert all(v["power_cap_kw"] for v in views)
    summary = client.get(f"{OBS}/summary").json()
    assert summary["racks_capped"] == len(STORE.racks)
    # 활성(테넌트) GPU가 있으면 power_cap 스로틀 사유 노출
    gpus = client.get(f"{OBS}/dcgm/gpus",
                      params={"state": "active", "limit": 50}).json()["gpus"]
    if gpus:
        assert any("power_cap" in g["throttle_reasons"] for g in gpus)
    client.post(f"{OBS}/racks/control", json={
        "scope": {"all": True}, "action": "power_uncap"})
    assert client.get(f"{OBS}/summary").json()["racks_capped"] == 0


def test_cordon_flags_and_alerts():
    rid = _first_rack_id()
    client.post(f"{OBS}/racks/{rid}/control",
                json={"action": "cordon", "params": {"reason": "PM 점검"}})
    assert _rack(rid)["cordoned"] is True
    alerts = client.get(f"{OBS}/alerts").json()
    assert any("RACK_CORDONED" in a["summary"] and a["state"] == "firing"
               for a in alerts)
    client.post(f"{OBS}/racks/{rid}/control", json={"action": "uncordon"})
    assert _rack(rid)["cordoned"] is False


def test_restart_converges_back_to_on():
    rid = _first_rack_id()
    client.post(f"{OBS}/racks/{rid}/control", json={"action": "restart"})
    assert _rack(rid)["power_state"] == "mixed"
    # 강제 tick 2회(제어 엔드포인트가 force tick) → 부트 완료
    client.post(f"{OBS}/racks/{rid}/control", json={"action": "workload",
                "params": {"profile": "steady"}})
    client.post(f"{OBS}/racks/{rid}/control", json={"action": "workload",
                "params": {"profile": "steady"}})
    assert _rack(rid)["power_state"] == "on"


def test_invalid_action_and_scope_rejected():
    rid = _first_rack_id()
    assert client.post(f"{OBS}/racks/{rid}/control",
                       json={"action": "explode"}).status_code == 422
    assert client.post(f"{OBS}/racks/control",
                       json={"scope": {}, "action": "power_on"}).status_code == 422
    assert client.post(f"{OBS}/racks/{rid}/control",
                       json={"action": "workload",
                             "params": {"profile": "warp"}}).status_code == 422


def test_workload_profile_drives_idle_racks():
    # 미할당 랙에도 데모 부하 적용 가능
    rid = _first_rack_id()
    client.post(f"{OBS}/racks/{rid}/control",
                json={"action": "workload", "params": {"profile": "train"}})
    gpus = client.get(f"{OBS}/dcgm/gpus",
                      params={"rack": rid, "limit": 10}).json()["gpus"]
    assert sum(g["util_pct"] for g in gpus) / len(gpus) > 30
    client.post(f"{OBS}/racks/{rid}/control",
                json={"action": "workload", "params": {"profile": "idle"}})
    gpus = client.get(f"{OBS}/dcgm/gpus",
                      params={"rack": rid, "limit": 10}).json()["gpus"]
    assert sum(g["util_pct"] for g in gpus) / len(gpus) < 10
