"""NICo REST surface — ``/v2/org/{org}/nico`` (dsx-ai-factory/infra-controller
rest-api/openapi/spec.yaml). See docs/nico-rest-paths.md."""
from . import common, inventory, network, core, rack

ROUTERS = [core.router, network.router, rack.router, rack.hooks]


def reset() -> None:
    common.reset_objs()
    inventory.reset()
    network.reset()
