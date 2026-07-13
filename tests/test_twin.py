"""Cluster twin shape: 140 racks / 2,520 trays / 2,520 DPUs / 10,080 GPUs."""
from app.store import COMPUTE_TRAYS, GPU_PER_TRAY


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "ok"
    assert d["compute_trays"] == 2520
    assert d["dpus"] == 2520


def test_twin_summary(client):
    d = client.get("/emulator/v1/twin").json()
    assert d["model"] == "Vera Rubin NVL72"
    assert d["compute_trays"] == 2520
    assert d["dpus"] == 2520
    assert d["gpu_per_tray"] == GPU_PER_TRAY == 4
    assert d["gpus"] == 2520 * 4 == 10080


def test_dpus_collection(client):
    dpus = client.get("/emulator/v1/dpus").json()
    assert len(dpus) >= 2520
    ids = {d["dpu_id"] for d in dpus}
    assert "su-1-rack-00-tray-00-dpu-0" in ids
    assert "su-1-rack-00-tray-17-dpu-0" in ids
    for d in dpus:
        assert d["operating_mode"] == "DPU"
        assert d["health"] == "ok"


def test_single_dpu_has_telemetry(client):
    d = client.get("/emulator/v1/dpus/su-1-rack-00-tray-00-dpu-0").json()
    assert d["dpu_id"] == "su-1-rack-00-tray-00-dpu-0"
    assert d["telemetry"]["dpu_arm_up"] == 1
    assert d["telemetry"]["dpu_intertenant_drops_total"] == 0
