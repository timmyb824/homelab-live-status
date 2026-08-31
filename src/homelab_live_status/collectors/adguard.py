"""AdGuard collector: discovers standalone hosts from DNS rewrites (PLAN.md §6 #12).

AdGuard is the host registry: nearly every physical/virtual host has a
rewrite on the internal domain. A rewrite is classified as a service backend
(excluded) when its *name* matches a discovered Traefik service — the naming
discipline "LXC named X runs service X" makes this work. Name-based, not
IP-based: pihole2's IP is the upstream for ~10 services and must survive as
a host. Cluster members (Proxmox nodes, k3s nodes) are also excluded since
they are covered by their own collectors.
"""

import ipaddress
from dataclasses import dataclass

import httpx

from homelab_live_status.models import Host, HostMetadata, Probe, ProbeType

DEFAULT_REWRITE_DOMAIN = "homelab.lan"
DEFAULT_SSH_PORT = 22


@dataclass(frozen=True)
class JoinContext:
    """Names used by the name-join classification (PLAN.md §6 #12)."""

    service_names: frozenset[str] = frozenset()
    cluster_names: frozenset[str] = frozenset()
    exclude: frozenset[str] = frozenset()


def rewrite_name(domain: str, rewrite_domain: str) -> str | None:
    """Extract the host name from a rewrite domain on the internal TLD."""
    suffix = f".{rewrite_domain}"
    if domain.endswith(suffix):
        return domain[: -len(suffix)]
    return None


def classify_rewrites(
    rewrites: list[dict],
    join: JoinContext,
    rewrite_domain: str = DEFAULT_REWRITE_DOMAIN,
) -> list[Host]:
    """Classify AdGuard rewrites into standalone hosts via the name-join.

    A rewrite becomes a Host unless its name matches a discovered service
    (it is that service's backend box) or a cluster member (covered by the
    Proxmox/k3s collectors), or it is explicitly excluded.
    """
    hosts: list[Host] = []
    for rewrite in rewrites:
        domain = rewrite.get("domain", "")
        if not (name := rewrite_name(domain, rewrite_domain)):
            continue
        if (
            name in join.service_names
            or name in join.cluster_names
            or name in join.exclude
        ):
            continue
        answer = rewrite.get("answer", "")
        try:
            ipaddress.ip_address(answer)
        except ValueError:
            continue
        hosts.append(
            Host(
                id=f"adguard/host/{name}",
                name=name,
                probe=Probe(
                    type=ProbeType.TCP,
                    target=f"{answer}:{DEFAULT_SSH_PORT}",
                ),
                metadata=HostMetadata(rewrite=domain, ip=answer),
            )
        )
    return sorted(hosts, key=lambda h: h.name)


async def collect_adguard(
    client: httpx.AsyncClient,
    base_url: str,
    join: JoinContext,
    rewrite_domain: str = DEFAULT_REWRITE_DOMAIN,
) -> list[Host]:
    """Fetch rewrites from the AdGuard Home API and classify hosts."""
    response = await client.get(f"{base_url}/control/rewrite/list", timeout=10)
    response.raise_for_status()
    return classify_rewrites(response.json(), join, rewrite_domain)
