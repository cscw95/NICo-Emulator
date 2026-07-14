# NICo Emulator — Site-Local Control Plane

> **분리 이력**: 이 프로젝트는 [NeoCloud OS Control-Plane(nocp)](https://github.com/cscw95/NeoCloud-Control-Plane)
> 내부의 NICo 에뮬레이션 기능을 **독립 신규 기능으로 분리**한 저장소다.
> nocp은 `NOCP_NICO_URL`로 이 서비스(:9000)의 `/nico-bridge`에 접속해 실연동한다.

Standalone emulator of the **NVIDIA Infra Controller (NICo)** *site-local control
plane*. NICo owns **orchestration** — the NOCP host lifecycle (`/nico-bridge`),
the per-site controller view, tenant segments, and DPU-isolation validation
scenarios. It does **not** own the physical twin: the Vera Rubin NVL72 digital
twin (racks / trays / DPUs / attachments / provisioning / DHCP / fabric) lives in
the separate **AI Infra Emulator (:9100)**, which NICo drives over REST.

```
Consoles(:8090) → NOCP(:8000) → NICo(:9000) → AI Infra(:9100, physical twin)
```

## Quick Start

```bash
bash run.sh        # http://127.0.0.1:9000  (dashboard at /, OpenAPI at /docs)
```

Uses the NOCP virtualenv by default (`~/nocp/.venv`); set `PYTHON=...` to override.
The AI Infra Emulator location is `AI_INFRA_URL` (default `http://127.0.0.1:9100`).
NICo runs without :9100, but physical effects and fleet views are unavailable
until it is reachable (see `/healthz` → `ai_infra.reachable`).

## What it owns (control plane)

| Domain | Surface | Highlights |
|--------|---------|-----------|
| **NOCP bridge** | `/nico-bridge/...` | The exact contract NOCP's `NicoHttpAdapter` speaks: `/hosts` (full 2,520-tray fleet), `/instances`, `/jobs`, `/segments` with NicoHost/NicoJob/NicoSegment shapes |
| **Site controllers** | `/emulator/v1/sites/...` | Per-site NICo instance view; fleet aggregated from AI Infra racks |
| **Scenarios** | `/emulator/v1/scenarios/...` | 5 built-in DPU-isolation fault scenarios (design §12), executed against AI Infra DPUs |
| **Health / twin proxy** | `/healthz`, `/emulator/v1/twin` | AI Infra reachability + delegated physical overview |
| **Events** | `/emulator/v1/events` | NICo control-plane event log |

Physical surfaces (Redfish BMC, DPU isolation engine, provisioning/DHCP/PXE,
fabric, observability, storage) now live in the **AI Infra Emulator (:9100)** and
are reached through `app/aiinfra.py`.

## How delegation works

- `list_hosts` → enumerates the full fleet from AI Infra `GET /emulator/v1/dpus`
  (2,520 trays), overlaid with NICo's own lifecycle registry.
- `provision` → AI Infra Redfish `ComputerSystem.Reset` (PXE) + `POST /provision`
  (DHCP lease + boot progression).
- `allocate` → AI Infra `POST /dpus/{dpu}/tenant-attachments` (the isolating VF +
  default-deny policy); the attachment id is tracked NICo-side.
- `release` → AI Infra `DELETE /dpus/{dpu}/tenant-attachments/{att}`.
- `segments` → one AI Infra attachment per host DPU; delete tears them all down.

NICo's orchestration state (`_hosts` / `_jobs` / `_segments` / event log) stays
in NICo memory; every physical mutation is best-effort delegated to AI Infra and
degrades gracefully when :9100 is unreachable.

## Built-in fault scenarios (design §12)

```bash
curl -X POST http://127.0.0.1:9000/emulator/v1/scenarios/inter-tenant-isolation/run -d '{}'
```

| name | validates |
|------|-----------|
| `inter-tenant-isolation` | tenant-A→tenant-B traffic 100% dropped (INTER_TENANT_DENY) |
| `mac-spoof-quarantine` | spoofed source MAC dropped, spoof counter raised |
| `policy-rollback-lkg` | flow-programming failure rolls back to last-known-good generation |
| `arm-os-fail-closed` | DPU Arm OS crash → fail-closed (health critical, dpu_arm_up=0), then recover |
| `ipsec-sa-expiry` | IPsec rekey failure raises the auth-failure counter |

Each scenario drives the real AI Infra isolation engine over REST and returns
`{passed, steps, assertions, telemetry_delta}`.

## Integration with NeoCloud OS (NOCP)

```bash
# in the nocp repo
NOCP_NICO_URL=http://127.0.0.1:9000/nico-bridge ./run.sh
```

NOCP's provisioning lifecycle (reserve → provision → allocate → cordon → sanitize)
drives NICo, which reflects the physical effects on AI Infra (:9100).

## Tests

```bash
~/nocp/.venv/bin/python -m pytest tests/ -q      # 16 passed
```

Integration tests that need real physical effects auto-skip when :9100 is down.

## Layout

```
app/aiinfra.py       AI Infra Emulator (:9100) REST client (physical delegation)
app/store.py         NICo control-plane state (event log + id gen + site topology)
app/bridge.py        NOCP NicoHttpAdapter-compatible bridge (delegates to AI Infra)
app/sites.py         per-site NICo controller view (fleet from AI Infra)
app/scenarios.py     DPU-isolation fault scenarios (run against AI Infra DPUs)
app/main.py          FastAPI app (CORS for :8000/:8090) + healthz + dashboard
static/index.html    single-page control-plane dashboard
tests/               pytest suite (bridge / sites / scenarios contract)
```
