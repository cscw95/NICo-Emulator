"""Redfish BMC surface: service root + 18-member Systems collection,
plus the compute-tray power reset state machine."""
from app.store import STORE


def test_service_root(client):
    r = client.get("/redfish/v1")
    assert r.status_code == 200
    body = r.json()
    assert "Systems" in body
    assert body["Systems"]["@odata.id"] == "/redfish/v1/Systems"


def test_systems_collection_has_18(client):
    r = client.get("/redfish/v1/Systems")
    assert r.status_code == 200
    members = r.json()["Members"]
    assert len(members) == 18
    ids = {m["@odata.id"] for m in members}
    assert "/redfish/v1/Systems/vr-rack-001-ct-01" in ids


def test_reset_power_off_on_reflected(client):
    """Force-off then power-on must be reflected by the tray power state."""
    tray = STORE.trays["vr-rack-001-ct-01"]

    STORE.set_power(tray, "ForceOff")
    assert STORE.power_state(tray) == "Off"

    STORE.set_power(tray, "On")
    # immediately after power-on the tray is PoweringOn (settles to On after ~1s)
    assert STORE.power_state(tray) in ("PoweringOn", "On")
    assert tray.power_target == "On"
