"""Tests for the k3s collector: Ready condition and role parsing."""

from homelab_live_status.collectors.k3s import (
    is_ready,
    nodes_from_kubectl,
    role_for_labels,
)
from homelab_live_status.models import Status

EXPECTED_NODE_COUNT = 3
FIXTURE_JOIN_YEAR = 2022

KUBECTL_FIXTURE = {
    "items": [
        {
            "metadata": {
                "name": "k3s-0",
                "labels": {"node-role.kubernetes.io/control-plane": "true"},
                "creationTimestamp": "2022-05-01T10:00:00Z",
            },
            "status": {
                "conditions": [
                    {"type": "Ready", "status": "True"},
                    {"type": "MemoryPressure", "status": "False"},
                ]
            },
        },
        {
            "metadata": {"name": "k3s-0-worker", "labels": {}},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        },
        {
            "metadata": {"name": "k3s-1-worker", "labels": {}},
            "status": {"conditions": [{"type": "Ready", "status": "False"}]},
        },
    ]
}


def test_role_for_labels() -> None:
    """Control-plane label maps to control-plane role; default is worker."""
    assert role_for_labels({"node-role.kubernetes.io/control-plane": "true"}) == (
        "control-plane"
    )
    assert role_for_labels({}) == "worker"


def test_is_ready() -> None:
    """Ready condition True means ready; anything else is not."""
    assert is_ready(KUBECTL_FIXTURE["items"][0])
    assert not is_ready(KUBECTL_FIXTURE["items"][2])


def test_nodes_from_kubectl_normalizes() -> None:
    """kubectl output normalizes into Node objects with role and status."""
    nodes = nodes_from_kubectl(KUBECTL_FIXTURE)
    assert len(nodes) == EXPECTED_NODE_COUNT
    cp = next(n for n in nodes if n.name == "k3s-0")
    assert cp.role == "control-plane"
    assert cp.status == Status.UP
    not_ready = next(n for n in nodes if n.name == "k3s-1-worker")
    assert not_ready.status == Status.DOWN
    assert not_ready.role == "worker"


def test_nodes_from_kubectl_captures_cluster_join_date() -> None:
    """creationTimestamp lands in created_at (displayed as node age)."""
    nodes = nodes_from_kubectl(KUBECTL_FIXTURE)
    cp = next(n for n in nodes if n.name == "k3s-0")
    assert cp.created_at is not None
    assert cp.created_at.year == FIXTURE_JOIN_YEAR
    # fixture has no creationTimestamp for the workers
    worker = next(n for n in nodes if n.name == "k3s-0-worker")
    assert worker.created_at is None
