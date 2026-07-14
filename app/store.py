"""NICo control-plane state (slim).

NICo is now a site-local control plane only. The physical Vera Rubin NVL72
twin (racks / trays / DPUs / attachments / provisioning / DHCP / fabric) has
moved to the AI Infra Emulator (:9100) and is reached over REST via
``app.aiinfra``. This module keeps *only* NICo's own orchestration state:

  * the control-plane event log (bridge lifecycle, segments, scenarios)
  * an id generator
  * the static site topology metadata (which SUs each site's NICo manages)

Site metadata (CLUSTER) is legitimately NICo-owned: each AI-factory site runs
its own NICo instance managing a known set of scalable units. It is used to
label hosts and to render the per-site controller view.
"""
import itertools
import time
from collections import deque
from threading import RLock
from typing import Dict, Optional

# ── VR NVL72 constants (kept for labelling / display) ─────────────────
COMPUTE_TRAYS = 18          # per rack — NVL72: 18 compute trays × 4 Rubin GPU
GPU_PER_TRAY = 4
RACK_ID = "su-1-rack-00"    # first rack (back-compat handle for scenarios)

# ── Cluster topology — mirrors the NeoCloud OS (NOCP) Phase-1 fleet ────
#   140 racks · 2,520 trays · 10,080 GPU · 11 SUs · 2 sites
#   host_id = "nh-{tray_id}", tray_id = "su-{S}-rack-{RR}-tray-{TT}"
CLUSTER = [
    {"id": "gasan", "name": "STT 가산", "region": "Seoul",
     "sus": [("su-1", 16), ("su-2", 8), ("su-3", 12)]},                 # 36 racks
    {"id": "ansan", "name": "IGIS 안산", "region": "Ansan",
     "sus": [("su-4", 16), ("su-5", 16), ("su-6", 16), ("su-7", 6),
             ("su-8", 16), ("su-9", 16), ("su-10", 16), ("su-11", 2)]}, # 104 racks
]

# su_id -> (site_id, site_name)
_SU_INDEX: Dict[str, tuple] = {
    su: (site["id"], site["name"])
    for site in CLUSTER for su, _ in site["sus"]
}


def su_of_tray(tray_id: str) -> str:
    """su-1-rack-00-tray-00 -> su-1"""
    parts = tray_id.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else tray_id


def site_of_tray(tray_id: str):
    """Return (site_id, site_name) for a tray_id, from CLUSTER metadata."""
    return _SU_INDEX.get(su_of_tray(tray_id), ("", ""))


def site_name_of_tray(tray_id: str) -> str:
    return site_of_tray(tray_id)[1]


def _iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Store:
    """NICo control-plane in-memory state — event log + id generator only."""

    def __init__(self):
        self.lock = RLock()
        self.started = _iso()
        self.events = deque(maxlen=1000)
        self._ids = itertools.count(1)

    def nid(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids):04d}"

    def event(self, severity: str, message_id: str, args=None, **extra):
        e = {"at": _iso(), "severity": severity, "message_id": message_id,
             "args": args or [], **extra}
        self.events.append(e)
        return e

    def reset(self):
        with self.lock:
            self.events.clear()


STORE = Store()
