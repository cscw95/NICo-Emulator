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


