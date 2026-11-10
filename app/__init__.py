"""NICo Emulator — standalone NVIDIA Infra Controller + Vera Rubin NVL72 digital twin.

Independent software (not part of nocp). Emulates the NICo control plane surface
that NeoCloud OS (NOCP) integrates with: Redfish BMC, provisioning (DHCP/PXE),
fabric (NVLink/IB/Ethernet), and DPU-enforced tenant isolation with a fault engine.
"""
import os

__version__ = "0.2.0"        # this emulator's own version

# The upstream NVIDIA Infra Controller (NICo) this emulator reproduces.
# Override the GitHub URL with NICO_GITHUB_URL if you host a fork/mirror.
EMULATED_NICO = {
    "name": "NVIDIA Infra Controller (NICo)",
    "repo": "dsx-ai-factory/infra-controller",
    "github": os.environ.get("NICO_GITHUB_URL",
                             "https://github.com/dsx-ai-factory/infra-controller"),
    "ref": "v2.3.0",
    "as_of": "2026-11",
    "rest_prefix": "/v2/org/{org}/nico",
    "note": "REST paths/resources follow rest-api/openapi/spec.yaml "
            "(see docs/nico-rest-paths.md); /nico-bridge is deprecated.",
}
