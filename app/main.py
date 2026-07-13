"""NICo Emulator — FastAPI app entrypoint (standalone, port 9000)."""
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .store import STORE, RACK_ID, COMPUTE_TRAYS, GPU_PER_TRAY
from . import dpu, redfish, provisioning, fabric, scenarios, bridge

STATIC = Path(__file__).parent.parent / "static"

app = FastAPI(title="NICo Emulator", version=__version__,
              description="Standalone NVIDIA Infra Controller + Vera Rubin "
                          "NVL72 digital twin. Integrates with NeoCloud OS (VRCM).")

# NeoCloud OS control-plane (vrcm :8000) + consoles (:8090) may call this service
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://127.0.0.1:8090",
                   "http://localhost:8000", "http://localhost:8090"],
    allow_methods=["*"], allow_headers=["*"],
)

app.include_router(dpu.router)
app.include_router(redfish.router)
app.include_router(provisioning.router)
app.include_router(fabric.router)
app.include_router(scenarios.router)
app.include_router(bridge.router)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__, "rack": RACK_ID,
            "compute_trays": len(STORE.trays), "dpus": len(STORE.dpus)}


@app.get("/emulator/v1/twin")
def twin():
    with STORE.lock:
        return {
            "rack_id": RACK_ID, "model": "Vera Rubin NVL72",
            "compute_trays": len(STORE.trays), "gpu_per_tray": GPU_PER_TRAY,
            "gpus": len(STORE.trays) * GPU_PER_TRAY, "dpus": len(STORE.dpus),
            "tenant_networks": len(STORE.tenant_networks),
            "attachments": len(STORE.attachments),
            "tenants": sorted({a["tenant_id"] for a in STORE.attachments.values()}),
        }


@app.get("/emulator/v1/events")
def events(limit: int = 50):
    with STORE.lock:
        return list(STORE.events)[-limit:][::-1]


@app.post("/emulator/v1/reset")
def reset():
    STORE.reset()
    return {"status": "reset", "compute_trays": len(STORE.trays)}


@app.get("/metrics")
def metrics():
    """Prometheus text exposition of DPU telemetry counters."""
    lines = []
    with STORE.lock:
        for d in STORE.dpus.values():
            for k, v in d.telemetry.items():
                lines.append(f'{k}{{dpu="{d.dpu_id}"}} {v}')
    return "\n".join(lines) + "\n"


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def dashboard():
    idx = STATIC / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    return {"service": "NICo Emulator", "version": __version__,
            "docs": "/docs", "twin": "/emulator/v1/twin"}
