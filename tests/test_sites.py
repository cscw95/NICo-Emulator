"""Site-controller view tests — per-site fleet aggregation from AI Infra."""


def test_healthz_reports_ai_infra_status(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["role"] == "site-local control plane"
    ai = body["ai_infra"]
    assert "reachable" in ai and "url" in ai


def test_sites_list_shape(client):
    r = client.get("/emulator/v1/sites")
    assert r.status_code == 200
    body = r.json()
    ids = {s["site_id"] for s in body["sites"]}
    assert {"gasan", "ansan"} <= ids
    for s in body["sites"]:
        assert s["nico_instance"] == f"nico-{s['site_id']}"
        assert s["service_total"] == len(s["services"])


def test_site_fleet_aggregated_from_ai_infra(client, require_ai_infra):
    r = client.get("/emulator/v1/sites/gasan")
    assert r.status_code == 200
    body = r.json()
    f = body["fleet"]
    assert f["ai_infra"] is True
    # gasan = su-1(16)+su-2(8)+su-3(12) = 36 racks · 648 trays
    assert f["racks"] == 36, f"expected 36 gasan racks, got {f['racks']}"
    assert f["trays"] == 648
    assert f["gpus"] == 648 * 4
    assert body["scalable_units"], "per-SU roll-up should be populated"
