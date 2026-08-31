"""Tests for the Proxmox collector: node extraction from cluster resources."""

import httpx
import respx

from homelab_live_status.collectors.proxmox import (
    collect_proxmox,
    nodes_from_cluster_resources,
)
from homelab_live_status.models import Status

RESOURCES_FIXTURE = [
    {"type": "node", "node": "pve2", "status": "online", "uptime": 1234567},
    {"type": "node", "node": "pve4", "status": "online", "uptime": 7654321},
    {"type": "node", "node": "pve6", "status": "offline"},
    {"type": "qemu", "node": "pve4", "vmid": 100, "name": "k3s-0"},
    {"type": "lxc", "node": "pve4", "vmid": 103, "name": "postgres-lxc"},
    {"type": "storage", "node": "pve2", "storage": "local"},
]

EXPECTED_ONLINE = 2
EXPECTED_NODE_COUNT = 3
PVE2_UPTIME_SECONDS = 1234567


def test_nodes_from_cluster_resources_filters_to_nodes() -> None:
    """Only type=node resources become Node objects — guests are skipped."""
    nodes = nodes_from_cluster_resources(RESOURCES_FIXTURE)
    assert len(nodes) == EXPECTED_NODE_COUNT
    assert all(n.type == "proxmox_node" for n in nodes)


def test_online_node_has_uptime() -> None:
    """Online nodes report up status and real uptime."""
    nodes = nodes_from_cluster_resources(RESOURCES_FIXTURE)
    pve2 = next(n for n in nodes if n.name == "pve2")
    assert pve2.status == Status.UP
    assert pve2.uptime_seconds == PVE2_UPTIME_SECONDS


def test_offline_node_is_down_without_uptime() -> None:
    """Offline nodes report down status with no uptime."""
    nodes = nodes_from_cluster_resources(RESOURCES_FIXTURE)
    pve6 = next(n for n in nodes if n.name == "pve6")
    assert pve6.status == Status.DOWN
    assert pve6.uptime_seconds is None


@respx.mock
async def test_collect_proxmox_sends_token_auth() -> None:
    """The collector calls the cluster resources API with PVEAPIToken auth."""
    route = respx.get("https://pve.example:8006/api2/json/cluster/resources").mock(
        return_value=httpx.Response(200, json={"data": RESOURCES_FIXTURE})
    )
    async with httpx.AsyncClient() as client:
        nodes = await collect_proxmox(
            client, "https://pve.example:8006", "root@pam!collector=secret"
        )
    assert len(nodes) == EXPECTED_ONLINE + 1
    auth = route.calls[0].request.headers["Authorization"]
    assert auth == "PVEAPIToken=root@pam!collector=secret"
