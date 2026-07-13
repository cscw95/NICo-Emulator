"""Digital-twin shape: 18 compute trays, 18 DPUs, 72 GPUs."""
from app.store import COMPUTE_TRAYS, GPU_PER_TRAY


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "ok"
    assert d["compute_trays"] == COMPUTE_TRAYS == 18
    assert d["dpus"] == 18


def test_twin_summary(client):
    d = client.get("/emulator/v1/twin").json()
    assert d["model"] == "Vera Rubin NVL72"
    assert d["compute_trays"] == 18
    assert d["dpus"] == 18
    assert d["gpu_per_tray"] == GPU_PER_TRAY == 4
    assert d["gpus"] == 18 * 4 == 72


def test_dpus_collection(client):
    dpus = client.get("/emulator/v1/dpus").json()
    assert len(dpus) == 18
    ids = {d["dpu_id"] for d in dpus}
    assert "vr-rack-001-ct-01-dpu-0" in ids
    assert "vr-rack-001-ct-18-dpu-0" in ids
    for d in dpus:
        assert d["operating_mode"] == "DPU"
        assert d["health"] == "ok"


def test_single_dpu_has_telemetry(client):
    d = client.get("/emulator/v1/dpus/vr-rack-001-ct-01-dpu-0").json()
    assert d["dpu_id"] == "vr-rack-001-ct-01-dpu-0"
    assert d["telemetry"]["dpu_arm_up"] == 1
    assert d["telemetry"]["dpu_intertenant_drops_total"] == 0
