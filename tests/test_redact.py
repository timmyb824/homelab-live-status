"""Tests for redact.py: the public snapshot must omit internal detail."""

from datetime import UTC, datetime

from homelab_live_status.models import (
    Event,
    EventType,
    Host,
    HostMetadata,
    Node,
    Probe,
    ProbeType,
    Service,
    ServiceMetadata,
    Snapshot,
    SourceHealth,
    Status,
)
from homelab_live_status.redact import is_internal_url, redact_snapshot

DOMAIN = "local.timmybtech.com"
NOW = datetime.now(UTC)


def _service(name: str, host: str, upstream: str) -> Service:
    """Build a service discovered from a file-provider route."""
    return Service(
        id=f"traefik/file/{host}",
        name=name,
        group="External",
        url=f"https://{host}",
        status=Status.UP,
        up_since=NOW,
        probe=Probe(type=ProbeType.HTTP, target=f"https://{host}"),
        metadata=ServiceMetadata(
            provider="file", upstream=upstream, entrypoint="websecure"
        ),
    )


def _snapshot() -> Snapshot:
    """A snapshot with dual-domain, local-only, and public-only services."""
    return Snapshot(
        generated_at=NOW,
        collector_version="0.1.0",
        sources={
            "traefik": SourceHealth(ok=True, last_poll=NOW),
            "argocd": SourceHealth(
                ok=False,
                last_poll=NOW,
                error=(
                    "connect error: https://argocd.local.timmybtech.com (192.168.86.10)"
                ),
            ),
        },
        services=[
            _service("searxng", "searxng.timmybtech.com", "http://10.0.0.1:8080"),
            _service("searxng", f"searxng.{DOMAIN}", "http://10.0.0.1:8080"),
            _service("mealie", f"mealie.{DOMAIN}", "http://10.0.0.2:9000"),
        ],
        hosts=[
            Host(
                id="adguard/host/pihole2",
                name="pihole2",
                status=Status.UP,
                probe=Probe(type=ProbeType.TCP, target="192.168.86.174:22"),
                metadata=HostMetadata(
                    rewrite="pihole2.homelab.lan", ip="192.168.86.174"
                ),
            )
        ],
        nodes=[
            Node(
                id="proxmox/node/pve4",
                type="proxmox_node",
                name="pve4",
                status=Status.UP,
                uptime_seconds=123,
            )
        ],
        events=[
            Event(
                timestamp=NOW,
                type=EventType.DOWN,
                resource_id=f"traefik/file/searxng.{DOMAIN}",
                summary="searxng is down (probe failed 2 consecutive polls)",
            ),
            Event(
                timestamp=NOW,
                type=EventType.UP,
                resource_id="traefik/file/searxng.timmybtech.com",
                summary="searxng recovered",
            ),
            Event(
                timestamp=NOW,
                type=EventType.DOWN,
                resource_id="adguard/host/pihole2",
                summary="pihole2 is down (probe failed 2 consecutive polls)",
            ),
            Event(
                timestamp=NOW,
                type=EventType.REMOVED,
                resource_id="traefik/file/ghost.local.timmybtech.com",
                summary="ghost no longer discovered",
            ),
        ],
    )


def test_is_internal_url() -> None:
    """Internal-domain URLs are internal; public and None are not."""
    assert is_internal_url(f"https://mealie.{DOMAIN}", DOMAIN)
    assert not is_internal_url("https://searxng.timmybtech.com", DOMAIN)
    assert not is_internal_url(None, DOMAIN)


def test_dual_domain_local_duplicate_dropped() -> None:
    """A service on both domains keeps only its public entry."""
    public = redact_snapshot(_snapshot(), DOMAIN)
    searxng = [s for s in public.services if s.name == "searxng"]
    assert len(searxng) == 1
    assert searxng[0].url == "https://searxng.timmybtech.com"
    assert searxng[0].id == "traefik/file/searxng"


def test_local_only_service_kept_without_url() -> None:
    """Local-only services survive by name/status with URL stripped."""
    public = redact_snapshot(_snapshot(), DOMAIN)
    mealie = next(s for s in public.services if s.name == "mealie")
    assert mealie.id == "traefik/file/mealie"
    assert mealie.url is None
    assert mealie.status == Status.UP


def test_service_probe_and_upstream_stripped() -> None:
    """Probes, upstreams, entrypoints, and middlewares never leak."""
    public = redact_snapshot(_snapshot(), DOMAIN)
    for service in public.services:
        assert service.probe is None
        assert service.metadata.upstream is None
        assert service.metadata.entrypoint is None
        assert service.metadata.middlewares == []


def test_host_detail_stripped_name_kept() -> None:
    """Hosts keep name/status; probe target, rewrite, and IP are omitted."""
    public = redact_snapshot(_snapshot(), DOMAIN)
    host = public.hosts[0]
    assert host.name == "pihole2"
    assert host.status == Status.UP
    assert host.probe is None
    assert host.metadata.ip is None
    assert host.metadata.rewrite is None


def test_nodes_kept_with_names() -> None:
    """Nodes pass through intact (names deliberately shown, no IPs)."""
    public = redact_snapshot(_snapshot(), DOMAIN)
    assert public.nodes == _snapshot().nodes


def test_events_remapped_and_filtered() -> None:
    """Events map onto redacted ids; dropped duplicates' events are dropped."""
    public = redact_snapshot(_snapshot(), DOMAIN)
    ids = [e.resource_id for e in public.events]
    # the public searxng event survives on the redacted id
    assert ids.count("traefik/file/searxng") == 1
    # the local dup's down event is dropped, NOT remapped onto the sibling:
    # a flapping internal route is not the public service failing
    assert not any(
        e.type == EventType.DOWN and "searxng" in e.resource_id for e in public.events
    )
    assert "adguard/host/pihole2" in ids
    # ghost was removed before redaction ran — its event has no live entity
    assert not any("ghost" in i for i in ids)
    assert not any(DOMAIN in i for i in ids)


def test_event_summaries_scrubbed() -> None:
    """Probe targets in event summaries lose internal domains and IPs."""
    snap = _snapshot()
    snap.events.append(
        Event(
            timestamp=NOW,
            type=EventType.DOWN,
            resource_id=f"traefik/file/mealie.{DOMAIN}",
            summary=(
                f"mealie is down (http probe to https://mealie.{DOMAIN} "
                "failed: ReadTimeout, 2 consecutive polls)"
            ),
        )
    )
    public = redact_snapshot(snap, DOMAIN)
    mealie_events = [e for e in public.events if e.resource_id.endswith("mealie")]
    assert len(mealie_events) == 1
    summary = mealie_events[0].summary
    assert DOMAIN not in summary
    assert "ReadTimeout" in summary  # failure reason survives
    assert "failed" in summary


def test_source_error_scrubbed() -> None:
    """Source-health errors lose internal domains and IPv4 addresses."""
    public = redact_snapshot(_snapshot(), DOMAIN)
    error = public.sources["argocd"].error
    assert error is not None
    assert DOMAIN not in error
    assert "192.168.86.10" not in error
    assert "[redacted]" in error
    assert public.sources["traefik"].error is None


def test_full_snapshot_untouched() -> None:
    """Redaction returns a copy; the full snapshot keeps its detail."""
    full = _snapshot()
    redact_snapshot(full, DOMAIN)
    assert full.services[1].url == f"https://searxng.{DOMAIN}"
    assert full.hosts[0].metadata.ip == "192.168.86.174"
