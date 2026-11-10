"""Health Validation rule engine (F28) — CEL rules → NodeVerdict.

Upstream: dsx-ai-factory/health-validation @ cc26732a510f (Apache-2.0)
  catalog/health/node-verdict.schema.json  (schema_version 0.5) — output shape
  collect/proto/health/v1/observation.proto — signals are observations keyed by signal_id
  catalog/catalog.go, catalog/checks.go     — a check = rule(s) over signals + thresholds;
                                               a *matching* rule means the condition was
                                               detected and sets the check's severity.
Copy of the verdict schema: ref/upstream/health-validation/.

NeoCloud extension (x-neocloud): rules are editable at runtime (upstream ships them in
the catalog). Rule = {name, expr, sensors, severity, category, thresholds, ...}; ``expr``
is a CEL predicate (app/cel_lite.py) that is **true when the fault is present**.

Signals use DCGM field names (dcgm-exporter, decision 6) per GPU, plus node signals.
Per-GPU signals are also exposed at node level as the max across GPUs, so a rule can be
written either as ``DCGM_FI_DEV_XID_ERROR > 0`` or ``gpus.exists(g, g.DCGM_FI_DEV_XID_ERROR > 0)``.
Pure module: no web framework; NICo REST (app/rest/health_rules.py) and NOCP
(app/health_rules.py, a copy) both use it.
"""
import copy
import hashlib
import time
import uuid
from typing import Any, Dict, List, Optional

from . import cel_lite

SCHEMA_VERSION = "0.5"
CATALOG_VERSION = "neocloud-hv-catalog+cc26732a510f"
SEVERITIES = ["PASS", "INFO", "WARNING", "UNKNOWN", "CRITICAL"]
CATEGORIES = ["gpu", "nvlink", "pcie", "infiniband", "storage", "node", "xid", "cpu"]
SCOPES = ["NODE", "DOMAIN", "CLUSTER"]
GPU_SIGNALS = ["DCGM_FI_DEV_GPU_TEMP_CELSIUS", "DCGM_FI_DEV_XID_ERROR",
               "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL", "DCGM_FI_DEV_ROW_REMAP_FAILED",
               "DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_TOTAL", "DCGM_FI_DEV_PCIE_REPLAY_TOTAL",
               "DCGM_FI_DEV_POWER_USAGE"]
NODE_SIGNALS = ["gpu_count", "ib_ports_up", "ib_ports_total", "leak_detected",
                "nccl_allreduce_busbw_gbps"]
SIGNALS = GPU_SIGNALS + NODE_SIGNALS
# XIDs NVIDIA documents as needing GPU reset / node drain (XID catalog, public docs)
CRITICAL_XIDS = [48, 63, 64, 74, 79, 92, 94, 95, 119, 120]

_DRAIN = ["RESET_GPU", "REBOOT_NODE", "REPLACE_TRAY"]

DEFAULT_RULES: List[Dict[str, Any]] = [
    {"name": "gpu-xid-critical", "category": "xid", "severity": "CRITICAL",
     "description": "GPU reported an XID that requires reset or drain",
     "expr": "gpus.exists(g, g.DCGM_FI_DEV_XID_ERROR in thresholds.critical_xids)",
     "sensors": ["DCGM_FI_DEV_XID_ERROR"], "thresholds": {"critical_xids": CRITICAL_XIDS},
     "conditionScope": "NODE", "remediation": _DRAIN},
    {"name": "gpu-ecc-dbe", "category": "gpu", "severity": "CRITICAL",
     "description": "Volatile double-bit ECC errors",
     "expr": "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL > thresholds.max_dbe",
     "sensors": ["DCGM_FI_DEV_ECC_DBE_VOL_TOTAL"], "thresholds": {"max_dbe": 0},
     "conditionScope": "NODE", "remediation": _DRAIN},
    {"name": "gpu-row-remap-failed", "category": "gpu", "severity": "CRITICAL",
     "description": "HBM row remapping failed",
     "expr": "DCGM_FI_DEV_ROW_REMAP_FAILED > 0",
     "sensors": ["DCGM_FI_DEV_ROW_REMAP_FAILED"], "thresholds": {},
     "conditionScope": "NODE", "remediation": ["REPLACE_TRAY"]},
    {"name": "gpu-thermal", "category": "gpu", "severity": "WARNING",
     "description": "GPU temperature at or above the slowdown margin",
     "expr": "DCGM_FI_DEV_GPU_TEMP_CELSIUS >= thresholds.max_temp_c",
     "sensors": ["DCGM_FI_DEV_GPU_TEMP_CELSIUS"], "thresholds": {"max_temp_c": 85},
     "conditionScope": "NODE", "remediation": []},
    {"name": "nvlink-crc-flit", "category": "nvlink", "severity": "WARNING",
     "description": "NVLink CRC flit errors above threshold",
     "expr": "DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_TOTAL > thresholds.max_crc_flit",
     "sensors": ["DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_TOTAL"], "thresholds": {"max_crc_flit": 100},
     "conditionScope": "DOMAIN", "remediation": []},
    {"name": "ib-link-down", "category": "infiniband", "severity": "CRITICAL",
     "description": "InfiniBand / Spectrum-X port down",
     "expr": "ib_ports_up < ib_ports_total",
     "sensors": ["ib_ports_up", "ib_ports_total"], "thresholds": {},
     "conditionScope": "CLUSTER", "remediation": ["REBOOT_NODE"]},
    {"name": "gpu-count", "category": "gpu", "severity": "CRITICAL",
     "description": "Visible GPU count differs from the tray SKU",
     "expr": "gpu_count != thresholds.expected_gpus",
     "sensors": ["gpu_count"], "thresholds": {"expected_gpus": 4},
     "conditionScope": "NODE", "remediation": ["REBOOT_NODE", "REPLACE_TRAY"]},
    {"name": "node-leak", "category": "node", "severity": "CRITICAL",
     "description": "Liquid cooling leak on the tray",
     "expr": "leak_detected == true",
     "sensors": ["leak_detected"], "thresholds": {},
     "conditionScope": "NODE", "remediation": ["POWER_OFF", "REPLACE_TRAY"]},
]


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _h(key: str) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)


class RuleError(ValueError):
    def __init__(self, message: str, data: Optional[dict] = None):
        super().__init__(message)
        self.data = data


def normalize_rule(body: Dict[str, Any], name: Optional[str] = None,
                   base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Validate a rule body (create or replace) → stored rule. Raises RuleError."""
    if not isinstance(body, dict):
        raise RuleError("rule body must be an object")
    rule = copy.deepcopy(base) if base else {}
    rule.update({k: v for k, v in body.items() if k != "x-neocloud"})
    rule["name"] = name or rule.get("name")
    if not rule["name"] or not isinstance(rule["name"], str) or len(rule["name"]) > 63:
        raise RuleError("name is required (≤63 characters)", {"name": "required"})
    try:
        ast = cel_lite.compile_expr(rule.get("expr"))
    except cel_lite.CelError as e:
        raise RuleError(f"invalid CEL expression: {e}", {"expr": str(e)}) from None
    reads = cel_lite.identifiers(ast) - {"thresholds", "gpus"}
    sensors = rule.get("sensors")
    if sensors is None:
        sensors = sorted(reads)
    if not isinstance(sensors, list) or not all(isinstance(s, str) for s in sensors):
        raise RuleError("sensors must be a list of signal names", {"sensors": "list"})
    missing = sorted(reads - set(sensors))
    if missing:
        raise RuleError(f"expr reads signals not listed in sensors: {missing}",
                        {"sensors": f"missing {missing}"})
    sev = rule.get("severity", "CRITICAL")
    if sev not in SEVERITIES or sev == "PASS":
        raise RuleError(f"severity must be one of {SEVERITIES[1:]}", {"severity": sev})
    cat = rule.get("category", "node")
    if cat not in CATEGORIES:
        raise RuleError(f"category must be one of {CATEGORIES}", {"category": cat})
    scope = rule.get("conditionScope", "NODE")
    if scope not in SCOPES:
        raise RuleError(f"conditionScope must be one of {SCOPES}", {"conditionScope": scope})
    th = rule.get("thresholds") or {}
    if not isinstance(th, dict):
        raise RuleError("thresholds must be an object", {"thresholds": "object"})
    rule.update(sensors=sensors, severity=sev, category=cat, conditionScope=scope,
                thresholds=th, enabled=bool(rule.get("enabled", True)),
                description=str(rule.get("description", "")),
                remediation=list(rule.get("remediation") or []),
                maturityLevel=rule.get("maturityLevel", "stable"),
                updated=now())
    rule.setdefault("defaults", {"thresholds": copy.deepcopy(th)})
    rule["x-neocloud"] = True
    return rule


def seed_rules() -> Dict[str, Dict[str, Any]]:
    out = {}
    for r in DEFAULT_RULES:
        rule = normalize_rule(r)
        rule["nvidiaEndorsed"] = False
        out[rule["name"]] = rule
    return out


def baseline_observations(node_id: str, gpu_count: int = 4, healthy: bool = True,
                          leak: bool = False) -> Dict[str, Any]:
    """Deterministic healthy readings for a node (seeded by its id)."""
    h = _h(node_id)
    gpus = []
    for i in range(gpu_count):
        gpus.append({"index": i,
                     "uuid": "GPU-" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"gpu/{node_id}/{i}")),
                     "DCGM_FI_DEV_GPU_TEMP_CELSIUS": 48 + (h >> i) % 12,
                     "DCGM_FI_DEV_XID_ERROR": 0, "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL": 0,
                     "DCGM_FI_DEV_ROW_REMAP_FAILED": 0,
                     "DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_TOTAL": (h >> (i + 3)) % 7,
                     "DCGM_FI_DEV_PCIE_REPLAY_TOTAL": 0,
                     "DCGM_FI_DEV_POWER_USAGE": 1180 + (h >> i) % 90})
    if not healthy and gpus:
        gpus[0]["DCGM_FI_DEV_XID_ERROR"] = 79      # fallen off the bus
    return {"node": {"gpu_count": gpu_count, "ib_ports_up": 4, "ib_ports_total": 4,
                     "leak_detected": leak, "nccl_allreduce_busbw_gbps": 742.0},
            "gpus": gpus}


def apply_overrides(obs: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    """overrides = {"node": {sig: v}, "gpus": {"<index>": {sig: v}}, "all_gpus": {sig: v}}."""
    out = copy.deepcopy(obs)
    out["node"].update(overrides.get("node") or {})
    for g in out["gpus"]:
        g.update(overrides.get("all_gpus") or {})
        g.update((overrides.get("gpus") or {}).get(str(g["index"])) or {})
    return out


def merge_override(cur: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    """Merge an injection body {signals:{...}, gpu?: int} into stored overrides."""
    signals = body.get("signals")
    if not isinstance(signals, dict) or not signals:
        raise RuleError("signals must be a non-empty object", {"signals": "object"})
    unknown = sorted(set(signals) - set(SIGNALS))
    if unknown:
        raise RuleError(f"unknown signals {unknown}; known: {SIGNALS}", {"signals": unknown})
    cur = copy.deepcopy(cur or {})
    node = {k: v for k, v in signals.items() if k in NODE_SIGNALS}
    gpu = {k: v for k, v in signals.items() if k in GPU_SIGNALS}
    if node:
        cur.setdefault("node", {}).update(node)
    if gpu:
        if body.get("gpu") is None:
            cur.setdefault("all_gpus", {}).update(gpu)
        else:
            cur.setdefault("gpus", {}).setdefault(str(int(body["gpu"])), {}).update(gpu)
    return cur


def _env(obs: Dict[str, Any]) -> Dict[str, Any]:
    env = dict(obs["node"])
    for sig in GPU_SIGNALS:
        vals = [g[sig] for g in obs["gpus"] if sig in g]
        if vals:
            env[sig] = max(vals)
    env["gpus"] = obs["gpus"]
    return env


def _worst(sevs) -> str:
    return max(sevs, key=SEVERITIES.index) if sevs else "PASS"


def evaluate_rule(rule: Dict[str, Any], obs: Dict[str, Any], trace_id: str,
                  ts: Optional[str] = None) -> Dict[str, Any]:
    """One rule → checkVerdict ($defs/checkVerdict)."""
    ts = ts or now()
    v = {"timestamp": ts, "check_id": rule["name"],
         "span_id": uuid.uuid5(uuid.NAMESPACE_URL, f"{trace_id}/{rule['name']}").hex[:16],
         "trace_id": trace_id, "category": rule["category"],
         "condition_scope": rule["conditionScope"],
         "maturity_level": rule.get("maturityLevel", "stable"),
         "nvidia_endorsed": bool(rule.get("nvidiaEndorsed", False))}
    if not rule.get("enabled", True):
        return {**v, "severity": "PASS", "skipped": True, "skip_reason": "CHECK_DISABLED",
                "skip_detail": "rule disabled by operator"}
    env = _env(obs)
    missing = [s for s in rule["sensors"] if s not in env]
    if missing:
        return {**v, "severity": "PASS", "skipped": True, "skip_reason": "SENSOR_MISSING",
                "skip_detail": f"no observation for {missing}"}
    defaults = (rule.get("defaults") or {}).get("thresholds") or {}
    v["thresholds"] = {k: {"value": val, "original_value": defaults.get(k, val),
                           "category": "ADJUSTABLE", "overridden": defaults.get(k, val) != val}
                       for k, val in rule["thresholds"].items()}
    v["observed_values"] = {s: env[s] for s in rule["sensors"]}
    env["thresholds"] = rule["thresholds"]
    try:
        hit = cel_lite.run(rule["expr"], env)
        if not isinstance(hit, bool):
            raise cel_lite.CelError(f"rule must evaluate to bool, got {type(hit).__name__}")
    except cel_lite.CelError as e:
        return {**v, "severity": "UNKNOWN", "skipped": False,
                "messages": [f"measurement unavailable: {e}"],
                "recommended_action": {"operational_state": "monitor"}}
    if not hit:
        return {**v, "severity": "PASS", "skipped": False}
    entities = []
    per_gpu = set(rule["sensors"]) & set(GPU_SIGNALS)
    for g in obs["gpus"] if per_gpu else []:     # which GPUs trip the rule on their own
        try:
            if cel_lite.run(rule["expr"], {**env, **g, "gpus": [g]}) is True:
                entities.append(g["uuid"])
        except cel_lite.CelError:
            pass
    drain = rule["severity"] == "CRITICAL"
    action = {"operational_state": "drain" if drain else "monitor"}
    if drain:
        action["remediation_actions"] = list(rule.get("remediation") or [])
    v.update(severity=rule["severity"], skipped=False, recommended_action=action,
             messages=[f"{rule['name']}: {rule.get('description') or rule['expr']} "
                       f"(observed {v['observed_values']})"])
    if entities:
        v["affected_entities"] = entities
    return v


def node_verdict(node_id: str, rules: List[Dict[str, Any]], obs: Dict[str, Any],
                 trace_id: Optional[str] = None) -> Dict[str, Any]:
    """Aggregate per-rule verdicts → NodeVerdict (node-verdict.schema.json 0.5)."""
    ts = now()
    trace_id = trace_id or uuid.uuid4().hex
    results = [evaluate_rule(r, obs, trace_id, ts) for r in rules]
    ran = [r for r in results if not r["skipped"]]
    skipped = [r for r in results if r["skipped"]]
    by_domain: Dict[str, str] = {}
    for r in ran:
        by_domain[r["category"]] = _worst([by_domain.get(r["category"], "PASS"), r["severity"]])
    out = {"schema_version": SCHEMA_VERSION, "catalog_version": CATALOG_VERSION,
           "trace_id": trace_id, "timestamp": ts, "node_id": node_id,
           "overall_severity": _worst([r["severity"] for r in ran]),
           "severity_by_domain": by_domain, "check_results": ran or None}
    if skipped:
        out["skipped_checks"] = skipped
    comps = []
    for cat in sorted({r["category"] for r in ran if r["severity"] != "PASS"}):
        bad = [r for r in ran if r["category"] == cat and r["severity"] != "PASS"]
        c = {"check_ids": [r["check_id"] for r in bad], "category": cat,
             "max_severity": _worst([r["severity"] for r in bad])}
        ents = sorted({e for r in bad for e in r.get("affected_entities", [])})
        if ents:
            c["entities"] = ents
        comps.append(c)
    if comps:
        out["affected_components"] = comps
    drains = [r for r in ran if (r.get("recommended_action") or {}).get("operational_state") == "drain"
              and r["recommended_action"].get("remediation_actions")]
    if drains:
        best = sorted(drains, key=lambda r: (-SEVERITIES.index(r["severity"]), r["check_id"]))[0]
        out["recommended_action"] = copy.deepcopy(best["recommended_action"])
    return out


def ready(verdict: Dict[str, Any]) -> bool:
    """Readiness gate: a node passes when nothing reached UNKNOWN or CRITICAL."""
    return SEVERITIES.index(verdict["overall_severity"]) < SEVERITIES.index("UNKNOWN")


def failing_checks(verdict: Dict[str, Any]) -> List[str]:
    return [r["check_id"] for r in verdict.get("check_results") or []
            if r["severity"] in ("UNKNOWN", "CRITICAL")]
