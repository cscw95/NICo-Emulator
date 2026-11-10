"""F26 NICo `GET /metrics` — machine lifecycle · instances · REST objects.

Exposition per `app/prom.py` (Prometheus text 0.0.4; OpenMetrics 1.0 via Accept).
NVIDIA infra-controller publishes no metric-name catalogue in the vendored refs
(`ref/upstream/nico/`), so names are NeoCloud's (`nico_` prefix, Prometheus naming
guide); machine status values are the REST `MachineStatus` enum from
`rest-api/openapi/spec.yaml` (Initializing, Ready, InUse, Reset, Error,
Decommissioned) as mapped in app/rest/inventory.py `LIFECYCLE_STATUS`.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from . import __version__
from .prom import CONTENT_TYPE_OM, CONTENT_TYPE_TEXT, Registry, wants_openmetrics
from .rest import common, inventory
from .store import STORE

router = APIRouter(tags=["observability"])


def collect() -> Registry:
    reg = Registry()
    reg.gauge("nico_build_info", "NICo emulator build information.").add(1, version=__version__)
    by: dict = {}
    racks: dict = {}
    for m in inventory.inv()["machines"].values():
        st = inventory.LIFECYCLE_STATUS[inventory.lifecycle(m)]
        k = (m.get("siteId"), st)
        by[k] = by.get(k, 0) + 1
        rid = m.get("rackId") or m.get("_rackId")
        if rid:
            racks.setdefault(m.get("siteId"), set()).add(rid)
    g = reg.gauge("nico_machines", "Machines by site and REST MachineStatus.")
    for (site, st), n in sorted(by.items(), key=lambda x: (str(x[0][0]), x[0][1])):
        g.add(n, site_id=site, status=st)
    r = reg.gauge("nico_racks", "Racks with machines, by site.")
    for site, ids in sorted(racks.items(), key=lambda x: str(x[0])):
        r.add(len(ids), site_id=site)
    o = reg.gauge("nico_objects", "REST objects created through the NICo API, by kind and status.")
    oc: dict = {}
    for kind, items in common.OBJ.items():
        for x in items.values():
            k = (kind, x.get("status") or "unknown")
            oc[k] = oc.get(k, 0) + 1
    for (kind, st), n in sorted(oc.items()):
        o.add(n, kind=kind, status=st)
    reg.gauge("nico_event_log_entries", "Control-plane event log entries held (ring of 1000).").add(
        len(STORE.events))
    return reg


@router.get("/metrics")
def metrics(request: Request) -> Response:
    """Prometheus scrape target (text 0.0.4; OpenMetrics 1.0 via Accept)."""
    om = wants_openmetrics(request.headers.get("accept"))
    return Response(collect().render(openmetrics=om),
                    media_type=CONTENT_TYPE_OM if om else CONTENT_TYPE_TEXT)
