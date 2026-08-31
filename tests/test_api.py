"""Tests for the status API: snapshot loading, routes, failure modes."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homelab_live_status.api import app
from homelab_live_status.models import (
    Event,
    EventType,
    Service,
    ServiceMetadata,
    Snapshot,
    SourceHealth,
    Status,
)

NOW = datetime.now(UTC)
HTTP_503 = 503
HTTP_404 = 404


def _snapshot() -> Snapshot:
    """A minimal but complete snapshot for API round-trips."""
    return Snapshot(
        generated_at=NOW,
        collector_version="0.1.0",
        sources={"traefik": SourceHealth(ok=True, last_poll=NOW)},
        services=[
            Service(
                id="traefik/file/filebrowser",
                name="filebrowser",
                group="External",
                url="https://filebrowser.example.com",
                status=Status.UP,
                metadata=ServiceMetadata(provider="file"),
            )
        ],
        events=[
            Event(
                timestamp=NOW,
                type=EventType.UP,
                resource_id="traefik/file/filebrowser",
                summary="filebrowser recovered",
            )
        ],
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A test client whose snapshot file lives in a temp dir."""
    path = tmp_path / "snapshot.json"
    path.write_text(_snapshot().model_dump_json())
    monkeypatch.setenv("SNAPSHOT_FILE", str(path))
    return TestClient(app)


def test_health_is_no_store(client: TestClient) -> None:
    """Liveness returns ok and forbids caching."""
    response = client.get("/api/health")
    assert response.status_code == 200  # noqa: PLR2004
    assert response.json() == {"status": "ok"}
    assert response.headers["Cache-Control"] == "no-store"


def test_status_returns_snapshot_without_events(client: TestClient) -> None:
    """/api/v1/status carries services + sources; events have their own route."""
    body = client.get("/api/v1/status").json()
    assert body["services"][0]["name"] == "filebrowser"
    assert body["sources"]["traefik"]["ok"] is True
    assert body["events"] == []


def test_events_returns_feed(client: TestClient) -> None:
    """/api/v1/events returns the rolling feed."""
    body = client.get("/api/v1/events").json()
    assert len(body) == 1
    assert body[0]["summary"] == "filebrowser recovered"


def test_reads_fresh_per_request(client: TestClient, tmp_path: Path) -> None:
    """A rewritten snapshot file is reflected without restarting the app."""
    updated = _snapshot().model_copy(update={"events": []})
    (tmp_path / "snapshot.json").write_text(updated.model_dump_json())
    assert client.get("/api/v1/events").json() == []


def test_missing_snapshot_is_503(client: TestClient, tmp_path: Path) -> None:
    """No snapshot file (collector hasn't run) degrades to 503, not 500."""
    (tmp_path / "snapshot.json").unlink()
    response = client.get("/api/v1/status")
    assert response.status_code == HTTP_503
    assert "not available" in response.json()["detail"]


def test_corrupt_snapshot_is_503(client: TestClient, tmp_path: Path) -> None:
    """A corrupt/in-flight snapshot degrades to 503, not 500."""
    (tmp_path / "snapshot.json").write_text("{not json")
    assert client.get("/api/v1/status").status_code == HTTP_503


def test_openapi_served_under_api_prefix(client: TestClient) -> None:
    """The generated spec lives at /api/v1/openapi.json."""
    spec = client.get("/api/v1/openapi.json").json()
    assert "/api/v1/status" in spec["paths"]


def test_interactive_docs_disabled(client: TestClient) -> None:
    """No Swagger/ReDoc UI on the public instance."""
    assert client.get("/docs").status_code == HTTP_404


def test_cors_disabled_by_default(client: TestClient) -> None:
    """Without CORS_ORIGINS, cross-origin requests get no allow header."""
    response = client.get("/api/health", headers={"Origin": "https://example.com"})
    assert "access-control-allow-origin" not in response.headers


def test_cors_allows_configured_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With CORS_ORIGINS set, the configured origin is allowed."""
    import importlib

    import homelab_live_status.api as api_module

    path = tmp_path / "snapshot.json"
    path.write_text(_snapshot().model_dump_json())
    monkeypatch.setenv("SNAPSHOT_FILE", str(path))
    monkeypatch.setenv("CORS_ORIGINS", "https://timothybryantjr.com")
    importlib.reload(api_module)
    try:
        cors_client = TestClient(api_module.app)
        allowed = cors_client.get(
            "/api/health", headers={"Origin": "https://timothybryantjr.com"}
        )
        assert (
            allowed.headers["access-control-allow-origin"]
            == "https://timothybryantjr.com"
        )
        denied = cors_client.get(
            "/api/health", headers={"Origin": "https://evil.example.com"}
        )
        assert "access-control-allow-origin" not in denied.headers
    finally:
        monkeypatch.delenv("CORS_ORIGINS")
        importlib.reload(api_module)
