"""NICo REST networking: VPC, Subnet, NSG, InfiniBand / NVLink logical /
Spectrum-X partitions (spec.yaml resource names)."""
import ipaddress
import itertools
from typing import Any, Dict

from fastapi import APIRouter

from .common import PREFIX, err, objs
from . import crud

router = APIRouter(prefix=PREFIX, tags=["nico-rest-network"])

_vni = itertools.count(20001)
_pkey = itertools.count(0x8001)


def reset() -> None:
    global _vni, _pkey
    _vni = itertools.count(20001)
    _pkey = itertools.count(0x8001)


def _vpc_create(o: Dict[str, Any], body: Dict[str, Any]) -> None:
    o.setdefault("org", "neocloud")
    o.setdefault("networkVirtualizationType", "ETHERNET_VIRTUALIZER")
    o.setdefault("slaacEnabled", False)
    o["requestedVni"] = body.get("vni")
    o["vni"] = body.get("vni") or next(_vni)
    o["controllerVpcId"] = f"ctl-{o['id'][:8]}"
    o.setdefault("labels", {})
    for ref, kind in (("networkSecurityGroupId", "network-security-group"),
                      ("nvLinkLogicalPartitionId", "nvlink-logical-partition")):
        if o.get(ref) and o[ref] not in objs(kind):
            err(400, f"{kind} {o[ref]} not found")


def _vpc_delete(o: Dict[str, Any], _: Dict[str, Any]) -> None:
    if any(s["vpcId"] == o["id"] for s in objs("subnet").values()):
        err(400, "VPC has subnets; delete them first")
    if any(i.get("vpcId") == o["id"] for i in objs("instance").values()):
        err(400, "VPC has instances; delete them first")


def _subnet_create(o: Dict[str, Any], body: Dict[str, Any]) -> None:
    vpc = objs("vpc").get(body["vpcId"])
    if not vpc:
        err(400, f"vpc {body['vpcId']} not found")
    o["siteId"] = vpc["siteId"]
    o["tenantId"] = vpc["tenantId"]
    plen = int(body.get("prefixLength") or 24)
    prefix = body.get("ipv4Prefix") or f"10.{100 + len(objs('subnet')) % 150}.0.0/{plen}"
    try:
        net = ipaddress.ip_network(prefix, strict=False)
    except ValueError:
        err(400, f"invalid ipv4Prefix {prefix}")
    o.update(ipv4Prefix=str(net), prefixLength=net.prefixlen,
             ipv4Gateway=str(next(net.hosts())), mtu=body.get("mtu") or 9000,
             routingType=body.get("routingType") or "DatacenterOnly",
             controllerNetworkSegmentId=f"seg-{o['id'][:8]}")


def _nsg_create(o: Dict[str, Any], _: Dict[str, Any]) -> None:
    o.setdefault("statefulEgress", True)
    o.setdefault("rules", [])
    o["ruleCount"] = len(o["rules"])
    o.setdefault("labels", {})


# x-neocloud: the spec's InfiniBandPartitionCreateRequest has no key field (the
# controller assigns partitionKey). NOCP keeps one UFM P_Key per tenant, so it may
# ask for that value with this label; an invalid or taken key is auto-assigned.
PKEY_LABEL = "neocloud.io/partition-key"


def _requested_pkey(body: Dict[str, Any]) -> str:
    want = (body.get("labels") or {}).get(PKEY_LABEL)
    try:
        k = int(str(want), 16) if want else 0
    except ValueError:
        k = 0
    taken = {p.get("partitionKey") for p in objs("infiniband-partition").values()}
    if k & 0x7FFF not in (0, 0x7FFF) and k <= 0xFFFF and hex(k) not in taken:
        return hex(k)
    return hex(next(_pkey))


def _ib_create(o: Dict[str, Any], body: Dict[str, Any]) -> None:
    o["partitionKey"] = _requested_pkey(body)
    o["partitionName"] = o["name"]
    o["controllerIBPartitionId"] = f"ibp-{o['id'][:8]}"
    o.setdefault("serviceLevel", 0)
    o.setdefault("mtu", 4000)
    o.setdefault("enableSharp", False)
    o.setdefault("labels", {})


def _nvl_create(o: Dict[str, Any], _: Dict[str, Any]) -> None:
    o["nvLinkLogicalPartitionStats"] = {"totalGpus": 0, "nvLinkDomains": 0}


def _sx_create(o: Dict[str, Any], body: Dict[str, Any]) -> None:
    o["vni"] = body.get("vni") or next(_vni)
    o.setdefault("labels", {})


def _in_use(ref: str, kind: str):
    def check(o: Dict[str, Any], _: Dict[str, Any]) -> None:
        if any(v.get(ref) == o["id"] for v in objs("vpc").values()):
            err(400, f"{kind} is referenced by a VPC")
    return check


crud.register(router, "vpc", "vpc", on_create=_vpc_create, on_delete=_vpc_delete)
crud.register(router, "subnet", "subnet", required=("name", "vpcId"),
              filters=("siteId", "vpcId", "tenantId"), on_create=_subnet_create)
crud.register(router, "network-security-group", "network-security-group",
              on_create=_nsg_create,
              on_delete=_in_use("networkSecurityGroupId", "network security group"))
crud.register(router, "infiniband-partition", "infiniband-partition", on_create=_ib_create)
crud.register(router, "nvlink-logical-partition", "nvlink-logical-partition",
              on_create=_nvl_create,
              on_delete=_in_use("nvLinkLogicalPartitionId", "NVLink logical partition"))
crud.register(router, "spectrumx-partition", "spectrumx-partition",
              on_create=_sx_create, methods=("get", "delete"))
