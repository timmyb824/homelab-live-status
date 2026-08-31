"""Tests for the diffing engine: debounce, transitions, up_since, retention."""

from datetime import UTC, datetime, timedelta

from homelab_live_status.differ import (
    MAX_EVENTS,
    cap_events,
    diff_probed_items,
    diff_snapshot,
    merge_events,
)
from homelab_live_status.models import (
    ArgoCDHealth,
    Event,
    EventType,
    Host,
    Node,
    PlatformInfo,
    Probe,
    ProbeType,
    Service,
    ServiceMetadata,
    Snapshot,
    Status,
)

T0 = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
T2 = T1 + timedelta(minutes=5)

DOWN_FAILURES = 2


def make_service(name: str, status: Status, failures: int = 0) -> Service:
    """Build a Service in a given probe state for diffing tests."""
    return Service(
        id=f"traefik/file/{name}.example.com",
        name=name,
        group="External",
        url=f"https://{name}.example.com",
        status=status,
        probe=Probe(
            type=ProbeType.HTTP,
            target=f"https://{name}.example.com",
            consecutive_failures=failures,
        ),
        metadata=ServiceMetadata(provider="file"),
    )


def test_first_run_seeds_first_seen_without_events() -> None:
    """With no previous snapshot, everything is seeded; no spurious events."""
    snap = Snapshot(
        generated_at=T0,
        collector_version="0.1.0",
        sources={},
        services=[make_service("app", Status.UP)],
    )
    result = diff_snapshot(snap, None)
    assert result.services[0].first_seen == T0
    assert result.services[0].up_since == T0
    assert result.events == []


def test_single_failure_is_a_blip_not_an_event() -> None:
    """One failed poll holds the previous status — no event, no flip."""
    previous = [make_service("app", Status.UP)]
    current = [make_service("app", Status.DOWN)]
    events = diff_probed_items(current, previous, T1)
    assert events == []
    assert current[0].status == Status.UP
    assert current[0].probe is not None
    assert current[0].probe.consecutive_failures == 1


def test_second_failure_flips_down_with_event() -> None:
    """Two consecutive failed polls flip status to down and fire an event."""
    previous = [make_service("app", Status.UP, failures=1)]
    current = [make_service("app", Status.DOWN)]
    events = diff_probed_items(current, previous, T1)
    assert current[0].status == Status.DOWN
    assert current[0].probe is not None
    assert current[0].probe.consecutive_failures == DOWN_FAILURES
    assert len(events) == 1
    assert events[0].type == EventType.DOWN
    assert f"{DOWN_FAILURES} consecutive polls" in events[0].summary


def test_single_success_recovers_immediately() -> None:
    """One success after down flips up immediately and sets up_since."""
    previous = [make_service("app", Status.DOWN, failures=2)]
    current = [make_service("app", Status.UP)]
    events = diff_probed_items(current, previous, T1)
    assert current[0].status == Status.UP
    assert current[0].up_since == T1
    assert current[0].probe is not None
    assert current[0].probe.consecutive_failures == 0
    assert events[0].type == EventType.UP


def test_added_and_removed_events() -> None:
    """New ids fire added; vanished ids fire removed."""
    previous = [make_service("old", Status.UP)]
    current = [make_service("new", Status.UP)]
    events = diff_probed_items(current, previous, T1)
    types = {e.resource_id: e.type for e in events}
    assert types["traefik/file/new.example.com"] == EventType.ADDED
    assert types["traefik/file/old.example.com"] == EventType.REMOVED
    assert current[0].first_seen == T1


def test_first_seen_and_up_since_persist_across_runs() -> None:
    """Stable items keep their original first_seen and up_since."""
    previous = [make_service("app", Status.UP)]
    previous[0].first_seen = T0
    previous[0].up_since = T0
    current = [make_service("app", Status.UP)]
    events = diff_probed_items(current, previous, T1)
    assert events == []
    assert current[0].first_seen == T0
    assert current[0].up_since == T0


def test_node_status_flip_fires_immediately() -> None:
    """Node (platform-derived) status changes are not debounced."""
    previous = [
        Node(id="k3s/node/k3s-0", type="k3s_node", name="k3s-0", status=Status.UP)
    ]
    current = [
        Node(id="k3s/node/k3s-0", type="k3s_node", name="k3s-0", status=Status.DOWN)
    ]
    snap = Snapshot(
        generated_at=T1, collector_version="0.1.0", sources={}, nodes=current
    )
    prev_snap = Snapshot(
        generated_at=T0, collector_version="0.1.0", sources={}, nodes=previous
    )
    result = diff_snapshot(snap, prev_snap)
    assert len(result.events) == 1
    assert result.events[0].type == EventType.DOWN


def test_cap_events_by_count() -> None:
    """The feed is capped at MAX_EVENTS, newest first."""
    events = [
        Event(
            timestamp=T0 + timedelta(minutes=i),
            type=EventType.UP,
            resource_id=f"r{i}",
            summary=f"event {i}",
        )
        for i in range(MAX_EVENTS + 50)
    ]
    capped = cap_events(events, T2 + timedelta(hours=5))
    assert len(capped) == MAX_EVENTS
    assert capped[0].timestamp > capped[-1].timestamp


def test_cap_events_by_age() -> None:
    """Events older than 30 days are dropped regardless of count."""
    old = Event(
        timestamp=T0 - timedelta(days=31),
        type=EventType.UP,
        resource_id="old",
        summary="ancient",
    )
    recent = Event(timestamp=T0, type=EventType.UP, resource_id="new", summary="fresh")
    capped = cap_events([old, recent], T1)
    assert [e.resource_id for e in capped] == ["new"]


def test_merge_events_appends_and_caps() -> None:
    """New events prepend to the rolling feed (newest first)."""
    previous = [Event(timestamp=T0, type=EventType.UP, resource_id="a", summary="a up")]
    new = [Event(timestamp=T1, type=EventType.DOWN, resource_id="a", summary="a down")]
    merged = merge_events(previous, new, T2)
    assert [e.summary for e in merged] == ["a down", "a up"]


def test_hosts_get_debounce_too() -> None:
    """Hosts are probe-bearing and share the service debounce path."""
    previous = [
        Host(
            id="adguard/host/pihole2",
            name="pihole2",
            status=Status.UP,
            probe=Probe(type=ProbeType.TCP, target="192.168.86.174:22"),
        )
    ]
    current = [
        Host(
            id="adguard/host/pihole2",
            name="pihole2",
            status=Status.DOWN,
            probe=Probe(type=ProbeType.TCP, target="192.168.86.174:22"),
        )
    ]
    events = diff_probed_items(current, previous, T1)
    assert events == []
    assert current[0].status == Status.UP


def with_argocd(service: Service, sync: str, health: str) -> Service:
    """Attach ArgoCD platform state to a test service."""
    service.platform = PlatformInfo(argocd=ArgoCDHealth(sync=sync, health=health))
    return service


def test_argocd_outofsync_fires_changed_event() -> None:
    """A Synced->OutOfSync transition fires an immediate changed event."""
    previous = [with_argocd(make_service("immich", Status.UP), "Synced", "Healthy")]
    current = [with_argocd(make_service("immich", Status.UP), "OutOfSync", "Healthy")]
    events = diff_probed_items(current, previous, T1)
    assert len(events) == 1
    assert events[0].type == EventType.CHANGED
    assert "Synced->OutOfSync" in events[0].summary


def test_argocd_stable_state_fires_nothing() -> None:
    """Unchanged sync/health produces no event."""
    previous = [with_argocd(make_service("immich", Status.UP), "Synced", "Healthy")]
    current = [with_argocd(make_service("immich", Status.UP), "Synced", "Healthy")]
    assert diff_probed_items(current, previous, T1) == []


def test_argocd_recovery_fires_changed_event() -> None:
    """Recovery back to Synced/Healthy is also a changed event."""
    previous = [with_argocd(make_service("immich", Status.UP), "OutOfSync", "Degraded")]
    current = [with_argocd(make_service("immich", Status.UP), "Synced", "Healthy")]
    events = diff_probed_items(current, previous, T1)
    assert len(events) == 1
    assert events[0].type == EventType.CHANGED


def test_argocd_absent_on_either_side_fires_nothing() -> None:
    """No platform data on either side means no comparison, no event."""
    previous = [make_service("immich", Status.UP)]
    current = [with_argocd(make_service("immich", Status.UP), "Synced", "Healthy")]
    assert diff_probed_items(current, previous, T1) == []


def test_down_summary_names_probe_target_and_error() -> None:
    """The down event says WHAT failed: probe type, target, and reason."""
    previous = [make_service("app", Status.UP, failures=1)]
    current = [make_service("app", Status.DOWN)]
    current[0].probe.last_error = "ReadTimeout"
    events = diff_probed_items(current, previous, T1)
    assert len(events) == 1
    summary = events[0].summary
    assert "http probe to https://app.example.com" in summary
    assert "ReadTimeout" in summary
    assert f"{DOWN_FAILURES} consecutive polls" in summary


def test_recovered_summary_names_probe_target() -> None:
    """The recovery event names the probe that succeeded."""
    previous = [make_service("app", Status.DOWN, failures=2)]
    current = [make_service("app", Status.UP)]
    events = diff_probed_items(current, previous, T1)
    assert events[0].summary == (
        "app recovered (http probe to https://app.example.com ok)"
    )
