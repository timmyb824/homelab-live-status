"""Redaction by omission: derive the public snapshot from the full one.

PLAN.md §5 — the collector produces the redacted view; the public API pod
physically cannot leak what it doesn't have. Scope (per user decision):
keep every service/host/node by name, strip network detail — internal URLs,
upstream IPs, probe targets, host IPs/rewrites, and any IPs or internal
hostnames embedded in source-health error strings.
"""

import re

from homelab_live_status.models import (
    Event,
    Host,
    Node,
    Service,
    Snapshot,
    SourceHealth,
)

_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def is_internal_url(url: str | None, internal_domain: str) -> bool:
    """True when a service URL lives on the internal-only domain."""
    return url is not None and url.split("//")[-1].split("/")[0].endswith(
        internal_domain
    )


def _scrub(text: str, internal_domain: str) -> str:
    """Remove internal domain references and IPv4 addresses from a string."""
    return _IP_RE.sub("[redacted]", text.replace(internal_domain, "[redacted]"))


def _redact_service_id(service: Service) -> str:
    """Strip the hostname from a service id, keeping provider and name."""
    prefix = service.id.rsplit("/", 1)[0]
    return f"{prefix}/{service.name}"


def _redact_service(service: Service, internal_domain: str, new_id: str) -> Service:
    """Copy a service with internal URLs, probes, and upstreams omitted."""
    internal = is_internal_url(service.url, internal_domain)
    return service.model_copy(
        update={
            "id": new_id,
            "url": None if internal else service.url,
            "probe": None,
            "metadata": service.metadata.model_copy(
                update={"upstream": None, "entrypoint": None, "middlewares": []}
            ),
        }
    )


def _redact_host(host: Host) -> Host:
    """Copy a host with probe target, rewrite, and IP omitted."""
    return host.model_copy(
        update={
            "probe": None,
            "metadata": host.metadata.model_copy(update={"rewrite": None, "ip": None}),
        }
    )


def _redact_services(
    services: list[Service], internal_domain: str
) -> tuple[list[Service], dict[str, str], set[str]]:
    """Redact services, dropping local-domain duplicates of public ones.

    Returns the kept services, a map of kept old id -> new id for event
    remapping, and the ids of dropped duplicates. Dropped-duplicate events
    are NOT remapped onto the public sibling: a flapping internal route is
    not the public service failing.
    """
    public_names = {
        s.name for s in services if not is_internal_url(s.url, internal_domain)
    }
    kept: list[Service] = []
    id_map: dict[str, str] = {}
    dropped_ids: set[str] = set()
    for service in services:
        new_id = _redact_service_id(service)
        if is_internal_url(service.url, internal_domain) and (
            service.name in public_names
        ):
            dropped_ids.add(service.id)  # public sibling survives
            continue
        id_map[service.id] = new_id
        kept.append(_redact_service(service, internal_domain, new_id))
    return kept, id_map, dropped_ids


def _redact_events(
    events: list[Event],
    id_map: dict[str, str],
    dropped_ids: set[str],
    valid_ids: set[str],
    internal_domain: str,
) -> list[Event]:
    """Keep events for live entities, remapped, with summaries scrubbed.

    Events from dropped local-domain duplicates are dropped too (see
    _redact_services). Summaries may embed probe targets with internal
    URLs/IPs — scrub them like source errors.
    """
    kept: list[Event] = []
    for event in events:
        if event.resource_id in dropped_ids:
            continue
        new_id = id_map.get(event.resource_id, event.resource_id)
        if new_id in valid_ids:
            kept.append(
                event.model_copy(
                    update={
                        "resource_id": new_id,
                        "summary": _scrub(event.summary, internal_domain),
                    }
                )
            )
    return kept


def _redact_sources(
    sources: dict[str, SourceHealth], internal_domain: str
) -> dict[str, SourceHealth]:
    """Scrub internal domains/IPs out of source error strings."""
    return {
        name: health.model_copy(
            update={
                "error": (
                    _scrub(health.error, internal_domain) if health.error else None
                )
            }
        )
        for name, health in sources.items()
    }


def redact_snapshot(snapshot: Snapshot, internal_domain: str) -> Snapshot:
    """Produce the public snapshot: names and status kept, detail omitted."""
    services, id_map, dropped_ids = _redact_services(snapshot.services, internal_domain)
    hosts = [_redact_host(h) for h in snapshot.hosts]
    nodes: list[Node] = list(snapshot.nodes)  # names kept, no network detail
    valid_ids = {s.id for s in services} | {h.id for h in hosts} | {n.id for n in nodes}
    return snapshot.model_copy(
        update={
            "services": services,
            "hosts": hosts,
            "nodes": nodes,
            "events": _redact_events(
                snapshot.events, id_map, dropped_ids, valid_ids, internal_domain
            ),
            "sources": _redact_sources(snapshot.sources, internal_domain),
        }
    )
