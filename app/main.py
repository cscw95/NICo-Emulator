"""NICo Emulator — Site-Local Control Plane (standalone, port 9000).

NICo is the site-local, zero-trust control plane. It owns orchestration
(NOCP /nico-bridge host lifecycle, jobs, segments), the per-site controller
view, and the DPU-isolation validation scenarios. It does NOT own the
physical Vera Rubin NVL72 twin — that lives in the AI Infra Emulator (:9100),
which NICo drives over REST (``app.aiinfra``).

    Consoles(:8090) → NOCP(:8000) → NICo(:9000) → AI Infra(:9100)
"""
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import __version__
from .store import STORE
from . import scenarios, bridge, sites
from . import aiinfra

STATIC = Path(__file__).parent.parent / "static"

app = FastAPI(
    title="NICo Emulator — Site-Local Control Plane",
    version=__version__,
    description="Standalone NVIDIA Infra Controller (NICo) site-local control "
                "plane. Orchestrates the fleet via NeoCloud OS (NOCP) and "
                "delegates physical state to the AI Infra Emulator (:9100).")

# NeoCloud OS control-plane (nocp :8000) + consoles (:8090) may call this service
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://127.0.0.1:8090",
                   "http://localhost:8000", "http://localhost:8090"],
    allow_methods=["*"], allow_headers=["*"],
)

app.include_router(bridge.router)
app.include_router(sites.router)
app.include_router(scenarios.router)


@app.get("/healthz")
def healthz():
    from . import EMULATED_NICO
    return {"status": "ok", "version": __version__,
            "role": "site-local control plane",
            "ai_infra": aiinfra.ping(),
            "emulated_nico": EMULATED_NICO}


@app.get("/emulator/v1/twin")
def twin():
    """Physical twin is owned by AI Infra — proxy its overview (or link)."""
    try:
        return {"delegated_to": aiinfra.AI_INFRA_URL,
                "twin": aiinfra.obs_summary()}
    except aiinfra.AIInfraError as e:
        return {"delegated_to": aiinfra.AI_INFRA_URL,
                "reachable": False, "detail": str(e)}


@app.post("/emulator/v1/reset")
def reset():
    """Reset NICo control-plane state (hosts/jobs/segments/events).
    Does NOT reset the AI Infra physical twin."""
    STORE.reset()
    bridge.reset_bridge()
    return {"status": "reset", "scope": "nico-control-plane"}


@app.get("/emulator/v1/events")
def events(limit: int = 50):
    with STORE.lock:
        return list(STORE.events)[-limit:][::-1]


if STATIC.exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def dashboard():
    idx = STATIC / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    return {"service": "NICo Emulator — Site-Local Control Plane",
            "version": __version__, "docs": "/docs",
            "ai_infra": aiinfra.AI_INFRA_URL}
