# NICo Emulator — Site-Local Control Plane

> **Split history**: this project is a repository that **split the NICo emulation
> functionality out of [NeoCloud OS Control-Plane (nocp)](https://github.com/cscw95/NeoCloud-Control-Plane)
> into a standalone new component**. nocp connects to this service's (:9000)
> `/nico-bridge` via `NOCP_NICO_URL` for live integration.

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

Uses `./.venv` when present, else `python3`; set `PYTHON=...` to override.
The AI Infra Emulator location is `AI_INFRA_URL` (default `http://127.0.0.1:9100`).
NICo runs without :9100, but physical effects and fleet views are unavailable
until it is reachable (see `/healthz` → `ai_infra.reachable`).

## DSX OS baseline version matrix

Same matrix in all four NeoCloud repos (`GET /versions`):

| Component | Version |
|-----------|---------|
| DSX OS | 2026.05 |
| NICo (NVIDIA Infra Controller) | 2.3.0 |
| NVKE | 0.23.1 |
| DPS (Dynamic Power Software) | 0.10 |
| Mission Control | 2.5 |
| DOCA | 3.x |
| GPU Operator | AICR recipe |
| Kubernetes | 1.33, 1.34 |
| Platform | Vera Rubin NVL72 |

## NICo REST API (`/v2/org/{org}/nico`, NICo v2.3)

Paths and resource names follow the real
[`dsx-ai-factory/infra-controller`](https://github.com/dsx-ai-factory/infra-controller)
`rest-api/openapi/spec.yaml`. Full path table, conventions and the machine
state mapping: **[docs/nico-rest-paths.md](docs/nico-rest-paths.md)**.
`{org}` is any string (default `neocloud`); auth headers are accepted but not
checked; lists are JSON arrays with an `X-Pagination` header.

| Area | Endpoints (under `/v2/org/{org}/nico`) |
|------|-----------|
| Provider / site | `GET /metadata`, `GET /infrastructure-provider/current`, `GET /tenant/current`, `GET\|POST /site`, `GET\|PATCH\|DELETE /site/{siteId}`, `GET /site/{siteId}/status-history`, `GET /site/{siteId}/health-probe`* |
| Allocation | `GET\|POST /allocation`, `GET\|PATCH\|DELETE /allocation/{id}`, `PATCH /allocation/{id}/constraint/{constraintId}` |
| Instance type | `GET\|POST /instance/type`, `GET\|PATCH\|DELETE /instance/type/{id}`, `GET\|POST /instance/type/{id}/machine` |
| Expected inventory | `GET\|POST /expected-machine`, `GET\|PATCH\|DELETE /expected-machine/{id}`, `GET\|POST /expected-power-shelf`, `GET\|PATCH\|DELETE /expected-power-shelf/{id}` |
| Machine | `GET /machine`, `GET\|PATCH\|DELETE /machine/{id}`, `PATCH /machine/{id}/power` (`ACPowercycle` …), `PATCH /machine/{id}/chassis/{chassisId}/reset`, `PATCH /machine/{id}/bmc/reset`, `POST /machine/{id}/gpu/reset`*, `GET\|PUT /machine/{id}/health-report`, `DELETE /machine/{id}/health-report/{source}`, `GET\|POST /machine/{id}/validation/run`, `GET /machine/{id}/validation/result`, `GET /machine/{id}/status-history`, `POST /machine/{id}/decommission`, `GET /machine/gpu/stats` |
| Instance | `GET\|POST /instance`, `GET\|PATCH\|DELETE /instance/{id}`, `GET /instance/{id}/status-history` |
| OS / SSH | `GET\|POST /operating-system`, `GET\|PATCH\|DELETE /operating-system/{id}`, `GET\|POST /sshkeygroup`, `GET\|PATCH\|DELETE /sshkeygroup/{id}`, `GET\|POST /sshkey`, `GET\|PATCH\|DELETE /sshkey/{id}` |
| Network | `vpc`, `subnet`, `network-security-group`, `infiniband-partition`, `nvlink-logical-partition` (each `GET\|POST /x`, `GET\|PATCH\|DELETE /x/{id}`), `spectrumx-partition` (`GET\|POST`, `GET\|DELETE /{id}`) |
| Rack (v2.3) | `GET /rack`, `GET /rack/{id}`, `GET /rack/validation`, `GET\|PUT /rack/{id}/health-report`, `DELETE /rack/{id}/health-report/{source}`, `GET /rack/{id}/validation`, `PATCH /rack/{id}/power`, `PATCH /rack/{id}/firmware` |
| Tray (v2.3) | `GET /tray` (`type=Compute\|NVSwitch\|PowerShelf`, `rackId`), `GET /tray/{id}` (`leakStatus`), `GET /tray/validation`, `GET\|PUT /tray/{id}/health-report`, `DELETE /tray/{id}/health-report/{source}`, `GET /tray/{id}/validation`, `GET\|PATCH /tray/{id}/power`, `PATCH /tray/{id}/firmware` |
| NVLink domain | `GET /domain/nvlink`, `GET /domain/nvlink/{id}` |
| Firmware | `GET\|PUT /firmware/baseline`*, `GET /firmware/drift`* |

\* emulator extension (not in spec.yaml). Outside the prefix: `GET /versions`,
`POST /emulator/v1/rest/trays/{id}/leak` (leak fault hook),
`POST /emulator/v1/rest/machines/{id}/advance` (Day-0 lifecycle step).

**Machine lifecycle.** `status` is the spec enum; `lifecycleState` (extension)
follows `expected → discovered → validating → ready → assigned → sanitizing →
ready | failed`. Expected machines start at `expected`; a validation run moves
them to `ready` (or `failed` → `Error`). Creating an Instance assigns a `Ready`
machine (`InUse`); deleting it sanitizes the machine (`Reset`, `NICO_SANITIZE_SECONDS`,
default 2) and returns it to `Ready`. Alerts classified `PreventAllocations`
(health reports, leaks) block allocation.

**OOB actions → AI Infra Redfish.** `ACPowercycle` / chassis reset →
`POST /redfish/v1/Chassis/{tray}/Actions/Chassis.Reset {"ResetType":"PowerCycle"}`;
GPU reset → `POST /redfish/v1/Systems/{tray}/Actions/GpuReset`; other power
actions → `ComputerSystem.Reset`; tray leak status reads
`GET /redfish/v1/Chassis/{tray}/ThermalSubsystem` (`LeakDetection`). All are
best-effort: when :9100 is down the response says `"delegated": false`.

**Shared state with the bridge.** Machine id = bridge host id (`nh-{trayId}`).
A REST Instance runs the bridge allocation (AI Infra DPU tenant attachment) and
uses the bridge instance id, so `/nico-bridge` and REST see the same hosts and
instances; either side can release.

## What it owns (control plane)

| Domain | Surface | Highlights |
|--------|---------|-----------|
| **NICo REST** | `/v2/org/{org}/nico/...` | spec.yaml paths (see above) — the surface NOCP uses with `NOCP_NICO_API=rest` |
| **NOCP bridge** *(deprecated)* | `/nico-bridge/...` | The exact contract NOCP's `NicoHttpAdapter` speaks: `/hosts` (full 2,520-tray fleet), `/instances`, `/jobs`, `/segments` with NicoHost/NicoJob/NicoSegment shapes |
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
NOCP_NICO_URL=http://127.0.0.1:9000/nico-bridge ./run.sh      # deprecated bridge
# NICo REST (NOCP_NICO_API=rest): base http://127.0.0.1:9000/v2/org/neocloud/nico
```

NOCP's provisioning lifecycle (reserve → provision → allocate → cordon → sanitize)
drives NICo, which reflects the physical effects on AI Infra (:9100).

## Tests

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt   # Python >= 3.10 (starlette 1.x)
.venv/bin/python -m pytest tests/ -q            # 38 passed
```

Integration tests that need real physical effects auto-skip when :9100 is down.

## Layout

```
app/aiinfra.py       AI Infra Emulator (:9100) REST client (physical delegation)
app/store.py         NICo control-plane state (event log + id gen + site topology)
app/bridge.py        NOCP NicoHttpAdapter-compatible bridge (delegates to AI Infra)
app/sites.py         per-site NICo controller view (fleet from AI Infra)
app/scenarios.py     DPU-isolation fault scenarios (run against AI Infra DPUs)
app/rest/            NICo REST /v2/org/{org}/nico (inventory, core, network, rack)
app/versions.py      DSX OS baseline version matrix (/versions)
app/main.py          FastAPI app (CORS for :8000/:8090) + healthz + dashboard
static/index.html    single-page control-plane dashboard
docs/nico-rest-paths.md  implemented spec.yaml paths (NOCP contract)
tests/               pytest suite (bridge / sites / scenarios / REST contract)
```

## Security hardening (opt-in, `app/security.py`)

Defaults are off; same env names as AI-Infra-Emulator.

| Env | Effect |
|---|---|
| `EMU_API_KEY` | POST/PUT/PATCH/DELETE (incl. scenarios inject/recover, `/nico-bridge/*`) need `X-API-Key` or `Authorization: Bearer` |
| `AI_INFRA_API_KEY` | sent as `X-API-Key` on calls to AI-Infra when that service enforces `EMU_API_KEY` |
| `EMU_OUTBOUND_ALLOWLIST` | hosts / CIDRs / `.suffix` allowed for `AI_INFRA_URL` (default `localhost,127.0.0.1,::1`); link-local / metadata IPs always refused; path ids are percent-encoded |
| `EMU_CORS_ORIGINS` | allowed browser origins (default localhost :8000/:8090) |
| `EMU_MAX_BODY_BYTES` (1 MiB), `EMU_MAX_PAGE` (10000), `NICO_MAX_OBJECTS` (10000 per kind) | request and store caps |
