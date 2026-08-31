"""k3s collector: cluster node readiness and roles (PLAN.md §2 Node).

Node status comes from the k8s API (Ready condition + role labels), NOT
from Proxmox VM state — a VM can be "running" while the node is NotReady,
and that distinction is exactly the failure worth surfacing. Uses kubectl
so no in-cluster service account is needed for the PoC.
"""

import asyncio
import json
from datetime import UTC, datetime

from homelab_live_status.models import Node, Status

KUBECTL_TIMEOUT = 15


def role_for_labels(labels: dict[str, str]) -> str:
    """Derive a node role from k8s node labels."""
    if "node-role.kubernetes.io/control-plane" in labels:
        return "control-plane"
    return "worker"


def is_ready(node: dict) -> bool:
    """Check the Ready condition on a k8s node object."""
    return any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in node.get("status", {}).get("conditions", [])
    )


def nodes_from_kubectl(payload: dict) -> list[Node]:
    """Normalize `kubectl get nodes -o json` output into Node objects."""
    nodes: list[Node] = []
    for item in payload.get("items", []):
        name = item["metadata"]["name"]
        ready = is_ready(item)
        created_at = None
        if created := item["metadata"].get("creationTimestamp"):
            # when the node joined the cluster — displayed as node "age"
            created_at = datetime.fromisoformat(created)
        nodes.append(
            Node(
                id=f"k3s/node/{name}",
                type="k3s_node",
                name=name,
                role=role_for_labels(item["metadata"].get("labels", {})),
                status=Status.UP if ready else Status.DOWN,
                created_at=created_at,
                last_seen=datetime.now(UTC),
            )
        )
    return sorted(nodes, key=lambda n: n.name)


async def collect_k3s(kubeconfig: str | None = None) -> list[Node]:
    """Fetch k3s nodes via kubectl and normalize them."""
    cmd = ["kubectl", "get", "nodes", "-o", "json"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        process.communicate(), timeout=KUBECTL_TIMEOUT
    )
    if process.returncode != 0:
        raise RuntimeError(f"kubectl failed: {stderr.decode().strip()}")
    return nodes_from_kubectl(json.loads(stdout))
