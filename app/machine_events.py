"""F12 — NICo managed-host state changes published to DSX Exchange.

Message shape follows the upstream AsyncAPI document
dsx-ai-factory/dsx-exchange@3358e108d9ad schemas/asyncapi/nico/nico.yaml
(copy: docs/upstream/nico-machine-state.asyncapi.yaml): channel
``NICO/v1/machine/{machineId}/state``, payload ``ManagedHostStateChangeMessage``
``{machine_id, timestamp, managed_host_state:{state, ...}}``. State names come
from the host state machine in infra-controller@9ace631fa361
crates/machine-controller/src/handler.rs (ready, assigned{instance_state},
waitingforcleanup{cleanup_state}, failed{details}, decommissioning, created,
hostinit). The payload carries no ``from``; subscribers diff against the last
state they saw (NOCP app/nico_events.py does).

On the emulated bus each message travels as a CloudEvents 1.0 envelope
(``specversion "1.0"``, ``subject`` = the MQTT channel address, ``data`` = the
AsyncAPI payload) to AI-Infra ``POST /exchange/v1/publish``. Delivery is
best-effort and off the request thread; ``NICO_EXCHANGE_PUBLISH=0`` keeps the
outbox only. ``GET /emulator/v1/machine-events`` lists the outbox.
"""
import os
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from . import aiinfra
from .store import STORE

router = APIRouter(prefix="/emulator/v1", tags=["nico-machine-events"])

CHANNEL = "NICO/v1/machine/{machineId}/state"
CE_TYPE = "com.nvidia.nico.v1.machine.state"
DATASCHEMA = ("https://github.com/dsx-ai-factory/dsx-exchange/blob/3358e108d9ad/"
              "schemas/asyncapi/nico/nico.yaml#/components/messages/"
              "ManagedHostStateChangeMessage")

OUTBOX: deque = deque(maxlen=2000)
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nico-exchange")


def reset() -> None:
    OUTBOX.clear()


def _ts() -> str:
    from .rest.common import now
    return now()


def managed_host_state(lc: str, machine_id: str, *, instance_state: str = "ready",
                       message: str = "") -> Optional[Dict[str, Any]]:
    """Emulator lifecycle → upstream ManagedHostState object (None = no event)."""
    if lc in ("expected", "discovered"):
        return {"state": "created"}
    if lc == "validating":
        return {"state": "hostinit", "machine_state": {"state": "init"}}
    if lc == "ready":
        return {"state": "ready"}
    if lc == "assigned":
        return {"state": "assigned", "instance_state": {"state": instance_state}}
    if lc == "sanitizing":
        return {"state": "waitingforcleanup", "cleanup_state": {"state": "init"}}
    if lc == "failed":
        return {"state": "failed", "machine_id": machine_id,
                "details": {"cause": {"machinevalidation": {"err": message or "failed"}},
                            "failed_at": _ts(), "source": "statemachine"}}
    if lc == "decommissioned":
        return {"state": "decommissioning",
                "decommissioning_state": {"state": "suppressingsiteexplorer"}}
    return None


def envelope(machine_id: str, state: Dict[str, Any], site_id: str = "") -> Dict[str, Any]:
    ts = _ts()
    return {"specversion": "1.0", "id": str(uuid.uuid4()),
            "source": f"nico://neocloud/site/{site_id or 'default'}",
            "type": CE_TYPE, "subject": CHANNEL.format(machineId=machine_id),
            "time": ts, "datacontenttype": "application/json",
            "dataschema": DATASCHEMA,
            "data": {"machine_id": machine_id, "timestamp": ts,
                     "managed_host_state": state}}


def _send(ev: Dict[str, Any]) -> None:
    try:
        aiinfra.exchange_publish(ev["subject"], ev["type"], ev["subject"], ev)
        ev["_delivered"] = True
    except aiinfra.AIInfraError as e:
        ev["_delivered"] = False
        ev["_error"] = str(e)[:160]


def emit(m: Dict[str, Any], lc: str, **kw) -> Optional[Dict[str, Any]]:
    """Record + publish one state change for machine ``m`` entering ``lc``."""
    state = managed_host_state(lc, m["id"], **kw)
    if state is None:
        return None
    ev = envelope(m["id"], state, m.get("siteId", ""))
    OUTBOX.append(ev)
    STORE.event("info", "NICo.2.3.MachineStateChanged", [m["id"], state["state"]])
    if os.environ.get("NICO_EXCHANGE_PUBLISH", "1") != "0":
        if os.environ.get("NICO_EXCHANGE_SYNC") == "1":
            _send(ev)
        else:
            _POOL.submit(_send, ev)
    return ev


def _public(ev: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in ev.items() if not k.startswith("_")}


@router.get("/machine-events")
def list_machine_events(machineId: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    evs = [e for e in OUTBOX if not machineId or e["data"]["machine_id"] == machineId]
    return [_public(e) for e in evs[-limit:]]
