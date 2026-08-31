"""PoC entrypoint: collect from all sources, probe, write a snapshot.

Configuration via environment variables:
    TRAEFIK_API_URL   base URL of the Traefik API (default: dashboard route)
    TRAEFIK_USER      optional basic-auth username
    TRAEFIK_PASSWORD  optional basic-auth password
    EXCLUDE_PATTERNS  comma-separated plumbing patterns to exclude
                      (default: "-metrics,otel-collector,cloudflared,authentik-outpost")
    ADGUARD_API_URL   base URL of AdGuard primary (default: 192.168.86.214)
    ADGUARD_USER      AdGuard basic-auth username
    ADGUARD_PASSWORD  AdGuard basic-auth password
    ADGUARD_EXCLUDE   comma-separated rewrite names to never treat as hosts
    ARGOCD_API_URL    base URL of ArgoCD (default: argocd.local.timmybtech.com)
    ARGOCD_API_KEY    ArgoCD API key (account token)
    PROXMOX_API_URL   base URL of any Proxmox node (default: pve on .160)
    PROXMOX_API_TOKEN API token: "<user>@<realm>!<token-id>=<secret>"
    KUBECONFIG        path to kubeconfig for the k3s collector (optional)
    STATIC_HOSTS_JSON JSON array of extra hosts:
                      [{"name": "coredns", "probe_type": "dns",
                        "target": "192.168.86.174:53/adguard.homelab.lan"}]
    SNAPSHOT_OUT      output path for the snapshot JSON (default: snapshot.json)
    SNAPSHOT_PUBLIC_OUT output path for the redacted snapshot
                      (default: <SNAPSHOT_OUT stem>-public.json)
    INTERNAL_DOMAIN   internal-only domain to redact (default: local.timmybtech.com)
    OUTPUT_MODE       "file" (default) or "configmap" (in-cluster k8s API)
    CONFIGMAP_FULL_NAME    ConfigMap for the full snapshot
                           (default: homelab-status-full)
    CONFIGMAP_PUBLIC_NAME  ConfigMap for the redacted snapshot
                           (default: homelab-status-public)
"""

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
from rich.console import Console
from rich.table import Table

from homelab_live_status import __version__
from homelab_live_status.collectors.adguard import JoinContext, collect_adguard
from homelab_live_status.collectors.argocd import collect_argocd, enrich_services
from homelab_live_status.differ import diff_snapshot
from homelab_live_status.collectors.k3s import collect_k3s
from homelab_live_status.collectors.proxmox import collect_proxmox
from homelab_live_status.collectors.traefik import collect_traefik
from homelab_live_status.models import (
    Host,
    Node,
    Probe,
    ProbeType,
    Service,
    Snapshot,
    SourceHealth,
    Status,
)
from homelab_live_status.configmap_store import ConfigMapStore
from homelab_live_status.prober import probe_all
from homelab_live_status.redact import redact_snapshot

DEFAULT_EXCLUDE_PATTERNS = [
    "-metrics",
    "otel-collector",
    "cloudflared",
    "authentik-outpost",
]

console = Console()

SECONDS_PER_DAY = 86400
DAYS_PER_YEAR = 365


def load_previous_snapshot(out: Path) -> Snapshot | None:
    """Load the previous snapshot for diffing; None on first run or corruption."""
    if not out.exists():
        return None
    try:
        return Snapshot.model_validate_json(out.read_text())
    except (ValueError, OSError) as exc:
        console.print(
            f"[yellow]previous snapshot unreadable ({exc}); starting fresh[/yellow]"
        )
        return None


def parse_static_hosts(raw: str | None) -> list[Host]:
    """Parse the STATIC_HOSTS_JSON env var into Host objects."""
    if not raw:
        return []
    hosts: list[Host] = []
    for entry in json.loads(raw):
        hosts.append(
            Host(
                id=f"static/host/{entry['name']}",
                name=entry["name"],
                probe=Probe(
                    type=ProbeType(entry.get("probe_type", "tcp")),
                    target=entry["target"],
                ),
            )
        )
    return hosts


def status_cell(status: Status) -> str:
    """Render a colored status cell for rich tables."""
    style = "green" if status == Status.UP else "red"
    return f"[{style}]{status}[/{style}]"


def render_services(services: list[Service]) -> None:
    """Print the services table grouped by Kubernetes vs External."""
    table = Table(title=f"Services ({len(services)})")
    table.add_column("Service")
    table.add_column("Group")
    table.add_column("Status")
    table.add_column("HTTP")
    table.add_column("ArgoCD")
    table.add_column("URL", overflow="fold")
    for service in services:
        code = (
            str(service.probe.last_status_code)
            if service.probe and service.probe.last_status_code is not None
            else "-"
        )
        argocd = (
            f"{service.platform.argocd.sync}/{service.platform.argocd.health}"
            if service.platform.argocd
            else "-"
        )
        table.add_row(
            service.name,
            service.group,
            status_cell(service.status),
            code,
            argocd,
            service.url or "-",
        )
    console.print(table)


def render_hosts(hosts: list[Host]) -> None:
    """Print the standalone hosts table."""
    table = Table(title=f"Hosts ({len(hosts)})")
    table.add_column("Host")
    table.add_column("Status")
    table.add_column("Probe")
    table.add_column("IP")
    for host in hosts:
        table.add_row(
            host.name,
            status_cell(host.status),
            host.probe.target if host.probe else "-",
            host.metadata.ip or "-",
        )
    console.print(table)


def format_node_longevity(node: Node) -> str:
    """Proxmox nodes show platform uptime; k3s nodes show cluster-join age.

    The k8s API has no OS uptime — creationTimestamp (kubectl's AGE) is the
    honest proxy for "how long this node has been around".
    """
    if node.uptime_seconds is not None:
        return f"{node.uptime_seconds / SECONDS_PER_DAY:.1f}d up"
    if node.created_at is not None:
        days = (datetime.now(UTC) - node.created_at).days
        if days >= DAYS_PER_YEAR:
            return f"{days / DAYS_PER_YEAR:.1f}y old"
        return f"{days}d old"
    return "-"


def render_nodes(nodes: list[Node]) -> None:
    """Print the cluster nodes table (Proxmox + k3s)."""
    table = Table(title=f"Nodes ({len(nodes)})")
    table.add_column("Node")
    table.add_column("Type")
    table.add_column("Role")
    table.add_column("Status")
    table.add_column("Uptime/Age")
    for node in nodes:
        table.add_row(
            node.name,
            node.type,
            node.role or "-",
            status_cell(node.status),
            format_node_longevity(node),
        )
    console.print(table)


def render_sources(sources: dict[str, SourceHealth]) -> None:
    """Print per-source collection health."""
    for name, health in sources.items():
        if health.ok:
            console.print(f"[green]{name}[/green] poll ok at {health.last_poll}")
        else:
            console.print(f"[red]{name}[/red] poll failed: {health.error}")


def source_ok() -> SourceHealth:
    """Build a healthy per-source poll record."""
    return SourceHealth(ok=True, last_poll=datetime.now(UTC))


def source_error(error: str) -> SourceHealth:
    """Build a failed per-source poll record."""
    return SourceHealth(ok=False, last_poll=datetime.now(UTC), error=error)


async def collect_traefik_source(
    client: httpx.AsyncClient,
    base_url: str,
    exclude_patterns: list[str],
    auth: tuple[str, str] | None = None,
) -> tuple[list[Service], SourceHealth]:
    """Collect services from Traefik, capturing failure as source health."""
    try:
        return (
            await collect_traefik(client, base_url, exclude_patterns, auth),
            source_ok(),
        )
    except httpx.HTTPError as exc:
        return [], source_error(str(exc))


async def collect_proxmox_source(
    client: httpx.AsyncClient,
) -> tuple[list[Node], SourceHealth]:
    """Collect Proxmox nodes; unconfigured shows as an explicit failure."""
    if not (token := os.environ.get("PROXMOX_API_TOKEN")):
        return [], source_error("not configured")
    url = os.environ.get("PROXMOX_API_URL", "https://192.168.86.160:8006")
    try:
        return await collect_proxmox(client, url, token), source_ok()
    except httpx.HTTPError as exc:
        return [], source_error(str(exc))


async def collect_k3s_source() -> tuple[list[Node], SourceHealth]:
    """Collect k3s nodes via kubectl, capturing failure as source health."""
    try:
        return await collect_k3s(os.environ.get("KUBECONFIG")), source_ok()
    except (
        RuntimeError,
        TimeoutError,
        FileNotFoundError,
        json.JSONDecodeError,
    ) as exc:
        return [], source_error(str(exc))


async def collect_adguard_source(
    services: list[Service], nodes: list[Node]
) -> tuple[list[Host], SourceHealth]:
    """Discover standalone hosts from AdGuard rewrites via the name-join."""
    user = os.environ.get("ADGUARD_USER")
    password = os.environ.get("ADGUARD_PASSWORD")
    if not (user and password):
        return [], source_error("not configured")
    url = os.environ.get("ADGUARD_API_URL", "http://192.168.86.214")
    exclude = {
        n.strip() for n in os.environ.get("ADGUARD_EXCLUDE", "").split(",") if n.strip()
    }
    join = JoinContext(
        service_names=frozenset(s.name for s in services),
        cluster_names=frozenset(n.name for n in nodes),
        exclude=frozenset(exclude),
    )
    try:
        async with httpx.AsyncClient(auth=(user, password)) as adguard_client:
            return await collect_adguard(adguard_client, url, join), source_ok()
    except httpx.HTTPError as exc:
        return [], source_error(str(exc))


async def enrich_from_argocd(
    client: httpx.AsyncClient, services: list[Service]
) -> SourceHealth:
    """Enrich k3s-backed services with ArgoCD sync/health."""
    if not (api_key := os.environ.get("ARGOCD_API_KEY")):
        return source_error("not configured")
    url = os.environ.get("ARGOCD_API_URL", "https://argocd.local.timmybtech.com")
    try:
        apps = await collect_argocd(client, url, api_key)
        matched = enrich_services(apps, services)
        console.print(f"argocd: enriched {matched}/{len(apps)} apps")
        return source_ok()
    except httpx.HTTPError as exc:
        return source_error(str(exc))


async def run() -> Snapshot:
    """Run one collection cycle across all sources, then probe everything."""
    traefik_url = os.environ.get(
        "TRAEFIK_API_URL", "https://traefik.local.timmybtech.com"
    )
    traefik_auth = (
        (user, password)
        if (user := os.environ.get("TRAEFIK_USER"))
        and (password := os.environ.get("TRAEFIK_PASSWORD"))
        else None
    )
    exclude_patterns = [
        p.strip()
        for p in os.environ.get(
            "EXCLUDE_PATTERNS", ",".join(DEFAULT_EXCLUDE_PATTERNS)
        ).split(",")
        if p.strip()
    ]

    sources: dict[str, SourceHealth] = {}
    services: list[Service] = []
    hosts: list[Host] = []
    nodes: list[Node] = []

    # Traefik has a real cert (verify=True); Proxmox is self-signed
    # (verify=False). Host/service probes go through Traefik's valid TLS.
    # Auth is passed per-request: client-level basic auth would overwrite
    # per-request Authorization headers (e.g. the ArgoCD bearer token).
    async with (
        httpx.AsyncClient(verify=True) as tls_client,
        httpx.AsyncClient(verify=False) as client,
    ):
        # Phase 1: discover services and cluster nodes (names feed the join)
        services, sources["traefik"] = await collect_traefik_source(
            tls_client, traefik_url, exclude_patterns, traefik_auth
        )
        proxmox_nodes, sources["proxmox"] = await collect_proxmox_source(client)
        nodes += proxmox_nodes
        k3s_nodes, sources["k3s"] = await collect_k3s_source()
        nodes += k3s_nodes

        # Phase 2: host discovery via the name-join (needs phase-1 names)
        adguard_hosts, sources["adguard"] = await collect_adguard_source(
            services, nodes
        )
        hosts += adguard_hosts
        hosts += parse_static_hosts(os.environ.get("STATIC_HOSTS_JSON"))

        # Phase 3: ArgoCD enrichment for k3s-backed services
        sources["argocd"] = await enrich_from_argocd(tls_client, services)

        # Phase 4: probe everything concurrently (services ride Traefik TLS)
        await probe_all(tls_client, services)
        await probe_all(client, hosts)

    return Snapshot(
        generated_at=datetime.now(UTC),
        collector_version=__version__,
        sources=sources,
        services=services,
        hosts=hosts,
        nodes=nodes,
    )


def public_out_path(out: Path) -> Path:
    """Derive the redacted snapshot path next to the full one."""
    return Path(os.environ.get("SNAPSHOT_PUBLIC_OUT", f"{out.stem}-public{out.suffix}"))


def write_local(snapshot: Snapshot, public: Snapshot, out: Path) -> None:
    """Write both snapshots to local files and report their sizes."""
    full_json = snapshot.model_dump_json(indent=2)
    public_json = public.model_dump_json(indent=2)
    out.write_text(full_json)
    public_out = public_out_path(out)
    public_out.write_text(public_json)
    console.print(f"Snapshot written to {out} ({len(full_json) / 1024:.1f} KiB)")
    console.print(
        f"Public snapshot written to {public_out} ({len(public_json) / 1024:.1f} KiB)"
    )


async def run_cycle() -> Snapshot:
    """Collect, diff against the previous snapshot, write both views."""
    internal_domain = os.environ.get("INTERNAL_DOMAIN", "local.timmybtech.com")
    if os.environ.get("OUTPUT_MODE", "file") == "configmap":
        store = ConfigMapStore.from_env()
        previous = await store.load_previous()
        snapshot = diff_snapshot(await run(), previous)
        await store.write(snapshot, redact_snapshot(snapshot, internal_domain))
        console.print(
            f"Snapshots written to ConfigMaps "
            f"{store.full_name}/{store.public_name} in {store.namespace}"
        )
        return snapshot
    out = Path(os.environ.get("SNAPSHOT_OUT", "snapshot.json"))
    previous = load_previous_snapshot(out)
    snapshot = diff_snapshot(await run(), previous)
    write_local(snapshot, redact_snapshot(snapshot, internal_domain), out)
    return snapshot


def main() -> None:
    """Entrypoint: run one cycle, diff against previous, write the snapshot."""
    snapshot = asyncio.run(run_cycle())
    render_nodes(snapshot.nodes)
    render_hosts(snapshot.hosts)
    render_services(snapshot.services)
    if snapshot.events:
        console.print(f"Event feed: {len(snapshot.events)} events (newest first)")
        for event in snapshot.events[:5]:
            console.print(f"  [{event.type}] {event.summary}")
    render_sources(snapshot.sources)


if __name__ == "__main__":
    main()
