"""Traefik collector: discovers services from the Traefik API (PLAN.md §1).

Traefik fronts nearly the whole homelab: k3s apps via IngressRoutes
(kubernetesCRD provider) and ~35 external LXC/VM/podman services via the
file provider. One poll of /api/http/routers + /api/http/services yields the
full service inventory with public hostname and upstream URL.
"""

import re

import httpx

from homelab_live_status.models import (
    Probe,
    ProbeType,
    Service,
    ServiceMetadata,
)

HOST_RULE_RE = re.compile(r"Host\(`([^`]+)`\)")

GROUP_BY_PROVIDER = {
    "file": "External",
    "kubernetescrd": "Kubernetes",
    "kubernetes": "Kubernetes",
}

SCHEME_BY_ENTRYPOINT = {
    "websecure": "https",
    "web": "http",
}


def parse_host(rule: str) -> str | None:
    """Extract the first Host(`...`) value from a Traefik router rule."""
    if match := HOST_RULE_RE.search(rule):
        return match.group(1)
    return None


def display_name(host: str) -> str:
    """Derive a display name from a hostname's first label."""
    return host.split(".")[0]


def group_for_provider(provider: str) -> str:
    """Map a Traefik provider name to the service group (k3s vs external)."""
    return GROUP_BY_PROVIDER.get(provider, "External")


def scheme_for_entrypoints(entrypoints: list[str]) -> str:
    """Pick the URL scheme for a router, preferring websecure."""
    for entrypoint in ("websecure", "web"):
        if entrypoint in entrypoints:
            return SCHEME_BY_ENTRYPOINT[entrypoint]
    return "https"


def upstream_for(router: dict, services_by_name: dict[str, dict]) -> str | None:
    """Resolve a router's backing service to its first loadBalancer server URL."""
    service_key = f"{router.get('service')}@{router.get('provider')}"
    if not (service := services_by_name.get(service_key)):
        return None
    servers = service.get("loadBalancer", {}).get("servers", [])
    return servers[0].get("url") if servers else None


def is_excluded(name: str, host: str, exclude_patterns: list[str]) -> bool:
    """Check whether a route matches a plumbing exclude pattern.

    Metrics endpoints and internal plumbing (e.g. otel-collector) duplicate
    their parent apps or can never serve a root page; they are excluded from
    the service inventory entirely.
    """
    return any(pattern in name or pattern in host for pattern in exclude_patterns)


def routers_to_services(
    routers: list[dict],
    services: list[dict],
    exclude_patterns: list[str] | None = None,
) -> list[Service]:
    """Normalize Traefik API routers + services into Service objects.

    Routers are deduplicated by hostname, preferring the simplest rule
    (a bare Host(...) rule over Host(...) && PathPrefix(...) variants, e.g.
    an Authentik outpost shadow route).
    """
    excludes = exclude_patterns or []
    services_by_name = {s["name"]: s for s in services if "name" in s}
    by_host: dict[str, dict] = {}
    for router in routers:
        if router.get("provider") == "internal" or router.get("status") != "enabled":
            continue
        rule = router.get("rule", "")
        if not (host := parse_host(rule)):
            continue
        if is_excluded(display_name(host), host, excludes):
            continue
        existing = by_host.get(host)
        if existing is None or len(rule) < len(existing.get("rule", "")):
            by_host[host] = router

    discovered: list[Service] = []
    for host, router in sorted(by_host.items()):
        provider = router.get("provider", "unknown")
        name = display_name(host)
        entrypoints = router.get("entryPoints", [])
        url = f"{scheme_for_entrypoints(entrypoints)}://{host}"
        discovered.append(
            Service(
                id=f"traefik/{provider}/{host}",
                name=name,
                group=group_for_provider(provider),
                url=url,
                probe=Probe(type=ProbeType.HTTP, target=url),
                metadata=ServiceMetadata(
                    provider=provider,
                    upstream=upstream_for(router, services_by_name),
                    entrypoint=entrypoints[0] if entrypoints else None,
                    middlewares=router.get("middlewares", []),
                ),
            )
        )
    return discovered


async def collect_traefik(
    client: httpx.AsyncClient,
    base_url: str,
    exclude_patterns: list[str] | None = None,
    auth: tuple[str, str] | None = None,
) -> list[Service]:
    """Fetch routers and services from the Traefik API and normalize them.

    Auth is per-request, not client-level: a shared client's basic auth
    would overwrite per-request Authorization headers (e.g. ArgoCD bearer).
    """
    routers_resp = await client.get(
        f"{base_url}/api/http/routers", timeout=10, auth=auth
    )
    routers_resp.raise_for_status()
    services_resp = await client.get(
        f"{base_url}/api/http/services", timeout=10, auth=auth
    )
    services_resp.raise_for_status()
    return routers_to_services(
        routers_resp.json(), services_resp.json(), exclude_patterns
    )
