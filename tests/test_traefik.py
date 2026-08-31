"""Tests for the Traefik collector: rule parsing, dedupe, normalization."""

import httpx
import pytest
import respx

from homelab_live_status.collectors.traefik import (
    collect_traefik,
    display_name,
    group_for_provider,
    parse_host,
    routers_to_services,
    scheme_for_entrypoints,
)

KUMA_AUTH_RULE = (
    "Host(`uptime-kuma.local.timmybtech.com`) && PathPrefix(`/outpost.goauthentik.io/`)"
)

EXPECTED_SERVICE_COUNT = 4

ROUTERS_FIXTURE = [
    {
        "name": "websecure-filebrowser@file",
        "rule": "Host(`filebrowser.local.timmybtech.com`)",
        "service": "filebrowser",
        "entryPoints": ["websecure"],
        "status": "enabled",
        "provider": "file",
    },
    {
        "name": "uptime-kuma@file",
        "rule": "Host(`uptime-kuma.local.timmybtech.com`)",
        "service": "uptime-kuma",
        "entryPoints": ["websecure"],
        "middlewares": ["authentik"],
        "status": "enabled",
        "provider": "file",
    },
    {
        "name": "uptime-kuma-auth@file",
        "rule": KUMA_AUTH_RULE,
        "service": "authentik",
        "entryPoints": ["websecure"],
        "status": "enabled",
        "provider": "file",
    },
    {
        "name": "immich-immich-server-websecure-0a2f6a75a55139eb9152@kubernetescrd",
        "rule": "Host(`immich.timmybtech.com`)",
        "service": "immich-immich-server",
        "entryPoints": ["websecure"],
        "status": "enabled",
        "provider": "kubernetescrd",
    },
    {
        "name": "web-calculators@file",
        "rule": "Host(`calculators.local.timmybtech.com`)",
        "service": "calculators",
        "entryPoints": ["web"],
        "status": "enabled",
        "provider": "file",
    },
    {
        "name": "disabled-route@file",
        "rule": "Host(`disabled.local.timmybtech.com`)",
        "service": "disabled",
        "entryPoints": ["websecure"],
        "status": "disabled",
        "provider": "file",
    },
    {
        "name": "dashboard@internal",
        "rule": "PathPrefix(`/dashboard`)",
        "service": "dashboard@internal",
        "status": "enabled",
        "provider": "internal",
    },
]

SERVICES_FIXTURE = [
    {
        "name": "filebrowser@file",
        "loadBalancer": {"servers": [{"url": "http://192.168.86.33:8080"}]},
        "status": "enabled",
        "provider": "file",
    },
    {
        "name": "uptime-kuma@file",
        "loadBalancer": {"servers": [{"url": "http://192.168.86.174:3001"}]},
        "status": "enabled",
        "provider": "file",
    },
    {
        "name": "immich-immich-server@kubernetescrd",
        "loadBalancer": {"servers": [{"url": "http://10.42.3.17:2283"}]},
        "status": "enabled",
        "provider": "kubernetescrd",
    },
]


def test_parse_host_simple_rule() -> None:
    """A bare Host rule yields its hostname."""
    assert parse_host("Host(`filebrowser.local.timmybtech.com`)") == (
        "filebrowser.local.timmybtech.com"
    )


def test_parse_host_compound_rule() -> None:
    """A compound rule still yields the hostname."""
    assert parse_host(KUMA_AUTH_RULE) == "uptime-kuma.local.timmybtech.com"


def test_parse_host_no_host_rule() -> None:
    """A rule without Host(...) yields None."""
    assert parse_host("PathPrefix(`/api`)") is None


def test_display_name() -> None:
    """Display name is the hostname's first label."""
    assert display_name("immich.timmybtech.com") == "immich"


def test_group_for_provider() -> None:
    """File provider maps to External, kubernetesCRD to Kubernetes."""
    assert group_for_provider("file") == "External"
    assert group_for_provider("kubernetescrd") == "Kubernetes"


def test_scheme_for_entrypoints() -> None:
    """websecure wins when both entrypoints are present."""
    assert scheme_for_entrypoints(["web", "websecure"]) == "https"
    assert scheme_for_entrypoints(["web"]) == "http"


def test_routers_to_services_normalizes() -> None:
    """Routers normalize into Service objects with expected fields."""
    services = routers_to_services(ROUTERS_FIXTURE, SERVICES_FIXTURE)
    by_id = {s.id: s for s in services}
    assert len(by_id) == len(services), "ids must be unique per hostname"

    fb = by_id["traefik/file/filebrowser.local.timmybtech.com"]
    assert fb.group == "External"
    assert fb.url == "https://filebrowser.local.timmybtech.com"
    assert fb.metadata.upstream == "http://192.168.86.33:8080"
    assert fb.probe is not None and fb.probe.target == fb.url

    immich = by_id["traefik/kubernetescrd/immich.timmybtech.com"]
    assert immich.group == "Kubernetes"
    assert immich.url == "https://immich.timmybtech.com"


def test_routers_to_services_dedupes_shadow_routes() -> None:
    """An Authentik outpost shadow route does not duplicate its service."""
    services = routers_to_services(ROUTERS_FIXTURE, SERVICES_FIXTURE)
    kuma = [s for s in services if s.name == "uptime-kuma"]
    assert len(kuma) == 1
    assert kuma[0].metadata.upstream == "http://192.168.86.174:3001"


def test_routers_to_services_filters_disabled_and_internal() -> None:
    """Disabled routers and the internal provider are excluded."""
    services = routers_to_services(ROUTERS_FIXTURE, SERVICES_FIXTURE)
    names = {s.name for s in services}
    assert "disabled" not in names
    assert all(s.metadata.provider != "internal" for s in services)


def test_routers_to_services_excludes_plumbing_patterns() -> None:
    """Routes matching an exclude pattern are dropped from the inventory."""
    routers = ROUTERS_FIXTURE + [
        {
            "name": "argocd-metrics@kubernetescrd",
            "rule": "Host(`argocd-metrics.local.timmybtech.com`)",
            "service": "argocd-metrics",
            "entryPoints": ["websecure"],
            "status": "enabled",
            "provider": "kubernetescrd",
        }
    ]
    services = routers_to_services(routers, SERVICES_FIXTURE, ["-metrics"])
    assert all("metrics" not in s.name for s in services)


def test_routers_to_services_web_scheme() -> None:
    """A web-entrypoint router gets an http URL."""
    services = routers_to_services(ROUTERS_FIXTURE, SERVICES_FIXTURE)
    calc = next(s for s in services if s.name == "calculators")
    assert calc.url == "http://calculators.local.timmybtech.com"


@respx.mock
async def test_collect_traefik_fetches_and_normalizes() -> None:
    """The collector hits both API endpoints and returns Service objects."""
    respx.get("https://traefik.example/api/http/routers").mock(
        return_value=httpx.Response(200, json=ROUTERS_FIXTURE)
    )
    respx.get("https://traefik.example/api/http/services").mock(
        return_value=httpx.Response(200, json=SERVICES_FIXTURE)
    )
    async with httpx.AsyncClient() as client:
        services = await collect_traefik(client, "https://traefik.example")
    assert len(services) == EXPECTED_SERVICE_COUNT


@respx.mock
async def test_collect_traefik_raises_on_error() -> None:
    """A failed Traefik API call propagates as an HTTP error."""
    respx.get("https://traefik.example/api/http/routers").mock(
        return_value=httpx.Response(500)
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await collect_traefik(client, "https://traefik.example")
