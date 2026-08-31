"""ConfigMap storage: read/write snapshots when running in-cluster.

PLAN.md §4 — two ConfigMaps (full + redacted) so the public API pod mounts
only the redacted one. Uses the pod's service account against the in-cluster
API over httpx; no kubernetes client dependency. The full ConfigMap doubles
as the "previous snapshot" the differ compares against.
"""

import os
from pathlib import Path

import httpx

from homelab_live_status.models import Snapshot

SA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount")
API_SERVER = "https://kubernetes.default.svc"
SNAPSHOT_KEY = "snapshot.json"
REQUEST_TIMEOUT = 15


def in_cluster() -> bool:
    """True when a service-account token is mounted (running in a pod)."""
    return (SA_PATH / "token").exists()


class ConfigMapStore:
    """Upsert-style read/write of snapshot ConfigMaps in one namespace."""

    def __init__(self, namespace: str, full_name: str, public_name: str) -> None:
        """Build an authenticated client from the mounted service account."""
        token = (SA_PATH / "token").read_text().strip()
        self.namespace = namespace
        self.full_name = full_name
        self.public_name = public_name
        self._client = httpx.AsyncClient(
            base_url=f"{API_SERVER}/api/v1/namespaces/{namespace}/configmaps",
            headers={"Authorization": f"Bearer {token}"},
            verify=str(SA_PATH / "ca.crt"),
            timeout=REQUEST_TIMEOUT,
        )

    @classmethod
    def from_env(cls) -> "ConfigMapStore":
        """Configure from the downward-API namespace file plus env vars."""
        namespace = (SA_PATH / "namespace").read_text().strip()
        return cls(
            namespace,
            os.environ.get("CONFIGMAP_FULL_NAME", "homelab-status-full"),
            os.environ.get("CONFIGMAP_PUBLIC_NAME", "homelab-status-public"),
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def _get(self, name: str) -> httpx.Response:
        """GET a single ConfigMap by name."""
        return await self._client.get(f"/{name}")

    async def load_previous(self) -> Snapshot | None:
        """Load the previous full snapshot from its ConfigMap, if present."""
        response = await self._get(self.full_name)
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        response.raise_for_status()
        raw = response.json().get("data", {}).get(SNAPSHOT_KEY)
        return Snapshot.model_validate_json(raw) if raw else None

    async def _apply(self, name: str, snapshot: Snapshot) -> None:
        """Create or replace a ConfigMap holding one snapshot JSON."""
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": self.namespace},
            "data": {SNAPSHOT_KEY: snapshot.model_dump_json()},
        }
        existing = await self._get(name)
        if existing.status_code == httpx.codes.NOT_FOUND:
            response = await self._client.post(self._client.base_url, json=body)
            response.raise_for_status()
            return
        existing.raise_for_status()
        body["metadata"]["resourceVersion"] = existing.json()["metadata"][
            "resourceVersion"
        ]
        (await self._client.put(f"/{name}", json=body)).raise_for_status()

    async def write(self, full: Snapshot, public: Snapshot) -> None:
        """Write both snapshots to their ConfigMaps."""
        try:
            await self._apply(self.full_name, full)
            await self._apply(self.public_name, public)
        finally:
            await self.close()
