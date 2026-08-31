"""ArgoCD collector: sync/health enrichment for k3s-backed services (PLAN.md §2).

ArgoCD application names do not reliably match Traefik service names
(app "paperless-ngx" vs service "paperless"; app "open-webui" vs service
"openui"), so matching is best-effort: normalized exact match first, then
prefix containment either way. Unmatched apps simply don't enrich anything —
the probe remains the universal status baseline.
"""

import re

import httpx

from homelab_live_status.models import ArgoCDHealth, Service

NON_ALNUM_RE = re.compile(r"[^a-z0-9]")


def normalize(name: str) -> str:
    """Normalize a name for fuzzy matching: lowercase, alphanumerics only."""
    return NON_ALNUM_RE.sub("", name.lower())


def health_from_application(app: dict) -> ArgoCDHealth:
    """Extract sync/health/revision from an ArgoCD Application object."""
    status = app.get("status", {})
    return ArgoCDHealth(
        sync=status.get("sync", {}).get("status", "Unknown"),
        health=status.get("health", {}).get("status", "Unknown"),
        revision=(status.get("sync", {}).get("revision") or "")[:7] or None,
    )


def match_services(app_name: str, services: list[Service]) -> list[Service]:
    """Match an ArgoCD app to k3s services: exact, then prefix containment.

    Returns a list because dual-domain services (immich.local + immich.)
    share a name and should both be enriched.
    """
    normalized = normalize(app_name)
    k8s_services = [s for s in services if s.group == "Kubernetes"]
    exact = [s for s in k8s_services if normalize(s.name) == normalized]
    if exact:
        return exact
    candidates = [
        s
        for s in k8s_services
        if normalize(s.name).startswith(normalized)
        or normalized.startswith(normalize(s.name))
    ]
    # Prefix matches only count when they resolve to a single distinct name,
    # otherwise the match is ambiguous (e.g. "registry" vs "registry-ui").
    distinct_names = {s.name for s in candidates}
    return candidates if len(distinct_names) == 1 else []


def enrich_services(apps: list[dict], services: list[Service]) -> int:
    """Attach ArgoCD sync/health to matched services. Returns app match count."""
    matched = 0
    for app in apps:
        name = app.get("metadata", {}).get("name", "")
        if matched_services := match_services(name, services):
            health = health_from_application(app)
            for service in matched_services:
                service.platform.argocd = health
            matched += 1
    return matched


async def collect_argocd(
    client: httpx.AsyncClient, base_url: str, api_key: str
) -> list[dict]:
    """Fetch all ArgoCD applications via the API."""
    response = await client.get(
        f"{base_url}/api/v1/applications",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    response.raise_for_status()
    return response.json().get("items", [])
