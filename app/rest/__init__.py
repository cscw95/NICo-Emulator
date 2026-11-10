"""NICo REST surface — ``/v2/org/{org}/nico`` (dsx-ai-factory/infra-controller
rest-api/openapi/spec.yaml). See docs/nico-rest-paths.md."""
from . import common, inventory, network, core, rack
from . import health_rules

ROUTERS = [core.router, network.router, rack.router, rack.hooks]
ROUTERS += [health_rules.router, health_rules.hooks]     # F28 (x-neocloud)


def reset() -> None:
    common.reset_objs()
    inventory.reset()
    network.reset()
    health_rules.reset()
