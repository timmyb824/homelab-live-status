"""Pydantic models for the homelab live-status snapshot schema (PLAN.md §2)."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Status(StrEnum):
    """Health status of a service, host, or node."""

    UP = "up"
    DOWN = "down"
    UNKNOWN = "unknown"


class EventType(StrEnum):
    """Change-feed event kinds (PLAN.md §2 Event)."""

    ADDED = "added"
    REMOVED = "removed"
    DOWN = "down"
    UP = "up"
    CHANGED = "changed"


class Event(BaseModel):
    """A single change-feed entry."""

    timestamp: datetime
    type: EventType
    resource_id: str
    summary: str


class ProbeType(StrEnum):
    """Supported probe mechanisms."""

    HTTP = "http"
    TCP = "tcp"
    DNS = "dns"


class Probe(BaseModel):
    """Probe configuration and last result for a service or host."""

    type: ProbeType
    target: str
    last_status_code: int | None = None
    last_error: str | None = None  # failure reason, e.g. "ReadTimeout", "HTTP 503"
    consecutive_failures: int = 0


class ArgoCDHealth(BaseModel):
    """ArgoCD sync/health enrichment for k3s-backed services."""

    sync: str
    health: str
    revision: str | None = None


class PlatformInfo(BaseModel):
    """Platform-provided health enrichment for a service."""

    argocd: ArgoCDHealth | None = None


class ServiceMetadata(BaseModel):
    """Traefik-derived metadata for a discovered service."""

    provider: str
    upstream: str | None = None
    entrypoint: str | None = None
    middlewares: list[str] = Field(default_factory=list)


class Service(BaseModel):
    """A user-facing service discovered from Traefik (primary display unit)."""

    id: str
    type: str = "service"
    name: str
    group: str
    url: str | None = None  # None in the redacted public view (PLAN.md §5)
    status: Status = Status.UNKNOWN
    up_since: datetime | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    probe: Probe | None = None
    platform: PlatformInfo = Field(default_factory=PlatformInfo)
    metadata: ServiceMetadata


class HostMetadata(BaseModel):
    """Discovery metadata for a standalone host."""

    rewrite: str | None = None
    ip: str | None = None


class Host(BaseModel):
    """A standalone host (tier 1): discovered via AdGuard rewrites or static."""

    id: str
    type: str = "host"
    name: str
    status: Status = Status.UNKNOWN
    up_since: datetime | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    probe: Probe | None = None
    metadata: HostMetadata = Field(default_factory=HostMetadata)


class Node(BaseModel):
    """A cluster node: Proxmox physical host (tier 1) or k3s member (tier 2)."""

    id: str
    type: str  # "proxmox_node" | "k3s_node"
    name: str
    role: str | None = None
    status: Status = Status.UNKNOWN
    uptime_seconds: int | None = None  # platform uptime (Proxmox)
    created_at: datetime | None = None  # cluster join date -> node age (k3s)
    last_seen: datetime | None = None


class SourceHealth(BaseModel):
    """Per-source collection health, surfaced so stale data is explicit."""

    ok: bool
    last_poll: datetime
    error: str | None = None


class Snapshot(BaseModel):
    """Top-level snapshot written to storage and served by the API."""

    generated_at: datetime
    collector_version: str
    sources: dict[str, SourceHealth]
    services: list[Service] = Field(default_factory=list)
    hosts: list[Host] = Field(default_factory=list)
    nodes: list[Node] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
