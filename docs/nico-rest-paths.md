# NICo REST paths (emulated)

Source: `dsx-ai-factory/infra-controller` → `rest-api/openapi/spec.yaml`
(fetched 2026-11-10 from `main`; `info.title` "NVIDIA Infra Controller REST API",
`info.version` 2.0.0, OpenAPI 3.1, 167 paths). This file is the contract NOCP's
`NicoHttpAdapter` (`NOCP_NICO_API=rest`) codes against.

## Conventions (same as the real API)

| Item | Value |
|------|-------|
| **Path prefix** | **`/v2/org/{org}/nico`** — e.g. `GET http://127.0.0.1:9000/v2/org/neocloud/nico/machine` |
| `{org}` | Any string is accepted (emulator default org: `neocloud`) |
| Auth | Real API: JWT bearer (Keycloak). Emulator: `Authorization` optional, never checked |
| Field case | camelCase (`siteId`, `tenantId`, `instanceTypeId`, ...) |
| List responses | Plain JSON **array** + header `X-Pagination: {"pageNumber":1,"pageSize":20,"total":N,"orderBy":"CREATED_DESC"}`; query `pageNumber`, `pageSize` (max 100) |
| Status codes | `POST` create → 201, `GET`/`PATCH` → 200, `DELETE` → 202, async actions (power, chassis reset, validation run) → 202 / 200 as in spec |
| Errors | `{"source":"nico","message":"...","data":null}` with 400/404/409 |
| IDs | Site: deterministic UUID; also accepts slug alias `gasan` / `ansan`. Machine id = `nh-{trayId}` (same id as `/nico-bridge` host_id). Rack id `su-{S}-rack-{RR}`, tray id `su-{S}-rack-{RR}-tray-{TT}` (compute), `...-nvsw-{N}`, `...-ps-{N}` |

## Implemented paths (all under the prefix)

Spec = present in spec.yaml. **Ext** = emulator extension, not in spec.yaml (kept under the same prefix so NOCP has one base URL).

### Provider, site, metadata
| Method | Path | Spec | Notes |
|---|---|---|---|
| GET | `/metadata` | Spec | `{version, buildTime}` |
| GET | `/infrastructure-provider/current` | Spec | |
| GET | `/tenant/current` | Spec | Tenant from `X-Tenant-Id` header (emulator), else `default` |
| GET, POST | `/site` | Spec | `includeMachineStats=true` adds `machineStats` |
| GET, PATCH, DELETE | `/site/{siteId}` | Spec | |
| GET | `/site/{siteId}/status-history` | Spec | |
| GET | `/site/{siteId}/health-probe` | **Ext** | Site health probe: control-plane services, AI-Infra reachability, machine/rack health roll-up, leaks |

### Allocation
| Method | Path | Spec |
|---|---|---|
| GET, POST | `/allocation` | Spec — body `{name, tenantId, siteId, allocationConstraints:[{resourceType:"InstanceType", resourceTypeId, constraintType:"Reserved"\|"OnDemand"\|"Preemptible", constraintValue}]}` |
| GET, PATCH, DELETE | `/allocation/{allocationId}` | Spec |
| PATCH | `/allocation/{allocationId}/constraint/{allocationConstraintId}` | Spec — `{constraintValue}` |

### Compute
| Method | Path | Spec | Notes |
|---|---|---|---|
| GET, POST | `/instance/type` | Spec | Seeded: `vr-nvl72-tray` (4× Rubin GPU) per site, `cpu-epyc` |
| GET, PATCH, DELETE | `/instance/type/{instanceTypeId}` | Spec | |
| GET, POST | `/instance/type/{instanceTypeId}/machine` | Spec | Machine ↔ InstanceType association `{machineIds:[...]}` |
| GET, POST | `/expected-machine` | Spec | Creates a Machine in lifecycle `expected` |
| GET, PATCH, DELETE | `/expected-machine/{expectedMachineId}` | Spec | |
| GET, POST | `/expected-power-shelf` | Spec | |
| GET | `/machine` | Spec | Filters: `siteId, status, instanceTypeId, tenantId, hasInstance, rackId(ext)` |
| GET, PATCH | `/machine/{machineId}` | Spec | PATCH `{maintenanceMessage, labels, instanceTypeId}` |
| PATCH | `/machine/{machineId}/power` | Spec | `{action: On\|GracefulShutdown\|ForceOff\|GracefulRestart\|ForceRestart\|ACPowercycle}` → AI-Infra Redfish (`ACPowercycle` → `Chassis.Reset PowerCycle`) |
| PATCH | `/machine/{machineId}/chassis/{chassisId}/reset` | Spec | → AI-Infra `Chassis.Reset {"ResetType":"PowerCycle"}` |
| PATCH | `/machine/{machineId}/bmc/reset` | Spec | |
| POST | `/machine/{machineId}/gpu/reset` | **Ext** | OOB AdminGpuReset (Core gRPC in v2.3) → AI-Infra `POST /redfish/v1/Systems/{id}/Actions/GpuReset` |
| GET, PUT | `/machine/{machineId}/health-report` | Spec | `MachineHealthReportEntry[]` |
| DELETE | `/machine/{machineId}/health-report/{source}` | Spec | |
| GET, POST | `/machine/{machineId}/validation/run` | Spec | POST runs DCGM-style tests; moves lifecycle `validating → ready` (or `failed`) |
| GET | `/machine/{machineId}/validation/result` | Spec | `MachineValidationResult[]` |
| GET | `/machine/{machineId}/status-history` | Spec | |
| POST | `/machine/{machineId}/decommission` | Spec | |
| GET | `/machine/gpu/stats` | Spec | |
| GET, POST | `/instance` | Spec | Create picks a `Ready` machine of `instanceTypeId` (or `machineId`); drives the same allocation as `/nico-bridge/instances` |
| GET, PATCH, DELETE | `/instance/{instanceId}` | Spec | DELETE → machine `sanitizing` → `ready` |
| GET | `/instance/{instanceId}/status-history` | Spec | |
| GET, POST | `/operating-system` | Spec | Seeded: `ubuntu-24.04-dgx`, `nvke-node-1.34` |
| GET, PATCH, DELETE | `/operating-system/{operatingSystemId}` | Spec | |
| GET, POST | `/sshkeygroup` · GET, PATCH, DELETE `/sshkeygroup/{sshKeyGroupId}` | Spec | |
| GET, POST | `/sshkey` · GET, DELETE `/sshkey/{sshKeyId}` | Spec | |

### Networking
| Method | Path | Spec |
|---|---|---|
| GET, POST · GET, PATCH, DELETE | `/vpc` · `/vpc/{vpcId}` | Spec — `networkVirtualizationType: ETHERNET_VIRTUALIZER\|FNN\|FLAT`, `vni` |
| GET, POST · GET, PATCH, DELETE | `/subnet` · `/subnet/{subnetId}` | Spec — emulator accepts `ipv4Prefix` when no `ipv4BlockId` |
| GET, POST · GET, PATCH, DELETE | `/network-security-group` · `/network-security-group/{networkSecurityGroupId}` | Spec |
| GET, POST · GET, PATCH, DELETE | `/infiniband-partition` · `/infiniband-partition/{infiniBandPartitionId}` | Spec — `partitionKey` auto-assigned (`0x8001`…) |
| GET, POST · GET, PATCH, DELETE | `/nvlink-logical-partition` · `/nvlink-logical-partition/{nvLinkLogicalPartitionId}` | Spec |
| GET, POST · GET, DELETE | `/spectrumx-partition` · `/spectrumx-partition/{spectrumXPartitionId}` | Spec (v2.3) |

### Rack / tray / NVLink domain (v2.3, NICo Flow)
| Method | Path | Spec | Notes |
|---|---|---|---|
| GET | `/rack` · `/rack/{id}` | Spec | `siteId` filter; `health`, `operationStatus`, `nvLinkDomainIds`, `components` |
| GET, PUT | `/rack/{id}/health-report` · DELETE `/rack/{id}/health-report/{source}` | Spec | |
| GET | `/rack/{id}/validation` | Spec | `RackValidationResult` (expected vs actual trays) |
| PATCH | `/rack/{id}/power` | Spec | `{siteId, state: On\|Off\|Cycle\|ForceOff\|ForceCycle\|ACPowerCycle}` |
| PATCH | `/rack/{id}/firmware` | Spec | `{siteId, version}` → updates tray firmware (clears drift) |
| GET | `/tray` · `/tray/{id}` | Spec | `type=Compute\|NVSwitch\|PowerShelf`, `rackId`; `leakStatus: Unknown\|NoLeak\|Leaking`, `leakHandlingStatus` |
| GET, PUT | `/tray/{id}/health-report` · DELETE `/tray/{id}/health-report/{source}` | Spec | |
| GET | `/tray/{id}/validation` | Spec | |
| GET, PATCH | `/tray/{id}/power` | Spec | `ACPowerCycle` → AI-Infra `Chassis.Reset PowerCycle` |
| PATCH | `/tray/{id}/firmware` | Spec | |
| GET | `/domain/nvlink` · `/domain/nvlink/{id}` | Spec | One NVL72 domain per rack |
| GET, PUT | `/firmware/baseline` | **Ext** | Fleet firmware baseline per component (BMC, BIOS, GPU, NVSwitch, PSU, DPU) |
| GET | `/firmware/drift` | **Ext** | Trays whose firmware differs from baseline (`siteId`, `rackId` filters) |

### Outside the prefix
| Method | Path | Notes |
|---|---|---|
| GET | `/versions` | DSX OS baseline matrix (PLAN.md) |
| POST | `/emulator/v1/rest/trays/{id}/leak` | Emulator fault hook: `{"leakStatus":"Leaking"\|"NoLeak"}` |
| POST | `/emulator/v1/rest/machines/{id}/advance` | Emulator hook: step lifecycle `expected → discovered → validating` |
| `*` | `/nico-bridge/*` | **Deprecated**, kept for back-compat. Shares state with the REST surface |

## Machine state

`status` is the spec enum (`Initializing, Ready, Reset, Maintenance, InUse, Error,
Decommissioning, Decommissioned, Unknown`). The emulator also returns
`lifecycleState` (extension field) for the Day0→2 flow in PLAN.md:

| lifecycleState | status |
|---|---|
| `expected`, `discovered`, `validating` | `Initializing` |
| `ready` | `Ready` |
| `assigned` | `InUse` |
| `sanitizing` | `Reset` |
| `failed` | `Error` |

`/nico-bridge` host states map onto the same record: `pool_ready/released → ready`,
`reserved/provisioning/provisioned/allocated → assigned`.

## Not emulated (present in spec.yaml)

tenant account, credential/bmc/uefi/rotation, measured-boot, site-explorer, ipblock,
vpc-peering, vpc-prefix, interface lists, expected-rack/switch/rack-group, sku,
instance batch, dpu, dpu-extension-service, machine-capability, task/task rule,
rack bringup, batch rack/tray/nvlink power & firmware, ipxe-template, audit, user,
firmware-config/host, tenant-identity, `.well-known/*`.
