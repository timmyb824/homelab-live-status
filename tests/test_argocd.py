"""Tests for the ArgoCD collector: matching, normalization, enrichment."""

import httpx
import respx

from homelab_live_status.collectors.argocd import (
    collect_argocd,
    enrich_services,
    health_from_application,
    match_services,
    normalize,
)
from homelab_live_status.collectors.traefik import collect_traefik
from homelab_live_status.models import Probe, ProbeType, Service, ServiceMetadata

EXPECTED_APP_COUNT = 3

APPS_FIXTURE = [
    {
        "metadata": {"name": "immich"},
        "status": {
            "sync": {"status": "Synced", "revision": "abc1234deadbeef"},
            "health": {"status": "Healthy"},
        },
    },
    {
        "metadata": {"name": "paperless-ngx"},
        "status": {
            "sync": {"status": "OutOfSync", "revision": "deadbeef1234567"},
            "health": {"status": "Degraded"},
        },
    },
    {
        "metadata": {"name": "metallb"},  # no matching service (no route)
        "status": {
            "sync": {"status": "Synced", "revision": "1111111"},
            "health": {"status": "Healthy"},
        },
    },
]


def make_k8s_service(name: str, host: str | None = None) -> Service:
    """Build a minimal Kubernetes-group service for enrichment tests."""
    hostname = host or f"{name}.local.timmybtech.com"
    return Service(
        id=f"traefik/kubernetescrd/{hostname}",
        name=name,
        group="Kubernetes",
        url=f"https://{hostname}",
        probe=Probe(type=ProbeType.HTTP, target=f"https://{hostname}"),
        metadata=ServiceMetadata(provider="kubernetescrd"),
    )


def test_normalize_strips_non_alphanumerics() -> None:
    """Dashes, case, and underscores don't affect matching."""
    assert normalize("paperless-ngx") == "paperlessngx"
    assert normalize("Open-WebUI") == "openwebui"


def test_health_from_application() -> None:
    """Sync/health/revision are extracted; revision is shortened."""
    health = health_from_application(APPS_FIXTURE[1])
    assert health.sync == "OutOfSync"
    assert health.health == "Degraded"
    assert health.revision == "deadbee"


def test_health_from_application_missing_status() -> None:
    """An app with no status block yields Unknown, not a crash."""
    health = health_from_application({"metadata": {"name": "x"}})
    assert health.sync == "Unknown"
    assert health.revision is None


def test_match_exact() -> None:
    """Exact normalized match wins."""
    services = [make_k8s_service("immich")]
    assert match_services("immich", services) == services


def test_match_prefix_containment() -> None:
    """App paperless-ngx matches service paperless by prefix."""
    service = make_k8s_service("paperless")
    assert match_services("paperless-ngx", [service]) == [service]


def test_match_ambiguous_prefix_returns_nothing() -> None:
    """registry matching registry AND registry-ui is ambiguous: no match."""
    services = [make_k8s_service("registry"), make_k8s_service("registry-ui")]
    assert match_services("registry", services) == [services[0]]  # exact wins
    assert match_services("registr", services) == []  # ambiguous prefix


def test_match_dual_domain_services_both_enriched() -> None:
    """A service exposed on two domains shares a name; both get enriched."""
    services = [
        make_k8s_service("immich", "immich.local.timmybtech.com"),
        make_k8s_service("immich", "immich.timmybtech.com"),
    ]
    matched = enrich_services(APPS_FIXTURE, services)
    assert matched == 1  # only the immich app has a matching service
    assert all(s.platform.argocd is not None for s in services)
    assert services[0].platform.argocd is not None
    assert services[0].platform.argocd.sync == "Synced"


def test_enrich_ignores_external_services() -> None:
    """External-group services are never ArgoCD-matched."""
    external = Service(
        id="traefik/file/immich.local.timmybtech.com",
        name="immich",
        group="External",
        url="https://immich.local.timmybtech.com",
        metadata=ServiceMetadata(provider="file"),
    )
    matched = enrich_services(APPS_FIXTURE, [external])
    assert matched == 0
    assert external.platform.argocd is None


@respx.mock
async def test_collect_argocd_sends_bearer_token() -> None:
    """The collector calls the applications API with a bearer token."""
    route = respx.get("https://argocd.example/api/v1/applications").mock(
        return_value=httpx.Response(200, json={"items": APPS_FIXTURE})
    )
    async with httpx.AsyncClient() as client:
        apps = await collect_argocd(client, "https://argocd.example", "secret-key")
    assert len(apps) == EXPECTED_APP_COUNT
    assert route.calls[0].request.headers["Authorization"] == "Bearer secret-key"


@respx.mock
async def test_shared_client_sends_traefik_basic_and_argocd_bearer() -> None:
    """Regression: per-request auth on a shared client keeps headers distinct.

    First live run put Traefik basic auth on the shared client; httpx
    client-level auth overwrites per-request Authorization headers, so ArgoCD
    got Basic credentials and returned 401. Auth is now per-request.
    """
    respx.get("https://traefik.example/api/http/routers").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get("https://traefik.example/api/http/services").mock(
        return_value=httpx.Response(200, json=[])
    )
    argocd_route = respx.get("https://argocd.example/api/v1/applications").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    async with httpx.AsyncClient() as client:
        await collect_traefik(
            client, "https://traefik.example", auth=("traefik-user", "pw")
        )
        await collect_argocd(client, "https://argocd.example", "secret-key")
    argocd_auth = argocd_route.calls[0].request.headers["Authorization"]
    assert argocd_auth == "Bearer secret-key"
