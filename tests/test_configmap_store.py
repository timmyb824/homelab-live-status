"""Tests for configmap_store.py: upsert semantics against the k8s API."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from homelab_live_status.configmap_store import ConfigMapStore
from homelab_live_status.models import Snapshot, SourceHealth

NOW = datetime.now(UTC)
BASE = "https://kubernetes.default.svc/api/v1/namespaces/status-ns/configmaps"
TWO_CONFIGMAPS = 2


def _snapshot() -> Snapshot:
    """A minimal snapshot for store round-trips."""
    return Snapshot(
        generated_at=NOW,
        collector_version="0.1.0",
        sources={"traefik": SourceHealth(ok=True, last_poll=NOW)},
    )


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ConfigMapStore:
    """A store whose client hits respx instead of a real API server."""
    instance = ConfigMapStore.__new__(ConfigMapStore)
    instance.namespace = "status-ns"
    instance.full_name = "homelab-status-full"
    instance.public_name = "homelab-status-public"
    instance._client = httpx.AsyncClient(base_url=BASE)
    return instance


@respx.mock
async def test_load_previous_missing_configmap(store: ConfigMapStore) -> None:
    """A 404 on the full ConfigMap means first run: no previous snapshot."""
    respx.get(f"{BASE}/homelab-status-full").mock(
        return_value=httpx.Response(404, json={})
    )
    assert await store.load_previous() is None
    await store.close()


@respx.mock
async def test_load_previous_round_trip(store: ConfigMapStore) -> None:
    """An existing ConfigMap parses back into a Snapshot."""
    respx.get(f"{BASE}/homelab-status-full").mock(
        return_value=httpx.Response(
            200, json={"data": {"snapshot.json": _snapshot().model_dump_json()}}
        )
    )
    previous = await store.load_previous()
    assert previous is not None
    assert previous.collector_version == "0.1.0"
    await store.close()


@respx.mock
async def test_write_creates_when_missing(store: ConfigMapStore) -> None:
    """First write POSTs both ConfigMaps."""
    for name in ("homelab-status-full", "homelab-status-public"):
        respx.get(f"{BASE}/{name}").mock(return_value=httpx.Response(404, json={}))
    created = respx.post(f"{BASE}/").mock(return_value=httpx.Response(201, json={}))
    await store.write(_snapshot(), _snapshot())
    assert len(created.calls) == TWO_CONFIGMAPS
    names = {c.request.read() for c in created.calls}
    assert all(b"homelab-status-" in body for body in names)


@respx.mock
async def test_write_replaces_when_present(store: ConfigMapStore) -> None:
    """Subsequent writes PUT with the current resourceVersion."""
    for name in ("homelab-status-full", "homelab-status-public"):
        respx.get(f"{BASE}/{name}").mock(
            return_value=httpx.Response(
                200, json={"metadata": {"resourceVersion": "42"}}
            )
        )
    replaced = respx.put(f"{BASE}/homelab-status-full").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.put(f"{BASE}/homelab-status-public").mock(
        return_value=httpx.Response(200, json={})
    )
    await store.write(_snapshot(), _snapshot())
    body = replaced.calls[0].request.read()
    assert b'"resourceVersion":"42"' in body
