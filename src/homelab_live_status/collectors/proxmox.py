"""Proxmox collector: cluster node status and uptime (PLAN.md §2 Node).

Queries the cluster resource endpoint for node entries only — guest
enumeration was deliberately cut (services cover what guests run). Status
here is honest node status from the Proxmox API, with real uptime.
"""

from datetime import UTC, datetime

import httpx

from homelab_live_status.models import Node, Status


def nodes_from_cluster_resources(resources: list[dict]) -> list[Node]:
    """Normalize Proxmox cluster resources into Node objects (nodes only)."""
    nodes: list[Node] = []
    for resource in resources:
        if resource.get("type") != "node":
            continue
        online = resource.get("status") == "online"
        nodes.append(
            Node(
                id=f"proxmox/node/{resource['node']}",
                type="proxmox_node",
                name=resource["node"],
                status=Status.UP if online else Status.DOWN,
                uptime_seconds=resource.get("uptime") if online else None,
                last_seen=datetime.now(UTC),
            )
        )
    return sorted(nodes, key=lambda n: n.name)


async def collect_proxmox(
    client: httpx.AsyncClient, base_url: str, api_token: str
) -> list[Node]:
    """Fetch cluster resources from the Proxmox API and extract nodes.

    api_token format: "<user>@<realm>!<token-id>=<secret>".
    """
    response = await client.get(
        f"{base_url}/api2/json/cluster/resources",
        params={"type": "node"},
        headers={"Authorization": f"PVEAPIToken={api_token}"},
        timeout=10,
    )
    response.raise_for_status()
    return nodes_from_cluster_resources(response.json()["data"])
