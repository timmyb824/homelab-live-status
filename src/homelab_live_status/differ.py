"""Diffing engine: previous-snapshot comparison and event generation (PLAN.md §3).

Each collector run diffs the fresh state against the previous snapshot:

- new ids -> `added`, missing ids -> `removed` (immediate; deliberate changes)
- probe-based status flips are debounced asymmetrically: 3 consecutive failed
  polls -> `down` event and status flip; 1 success -> `up` (immediate recovery,
  a successful probe is unambiguous)
- platform-derived node status flips (Proxmox offline, k3s NotReady) fire
  immediately — they are not probe blips
- `up_since` is set on the down->up transition; `first_seen` persists

Events are capped at 30 days AND 200 entries, whichever hits first.
"""

from datetime import datetime, timedelta

from homelab_live_status.models import (
    Event,
    EventType,
    Host,
    Node,
    Service,
    Snapshot,
    Status,
)

DOWN_THRESHOLD = 3
MAX_EVENTS = 200
MAX_EVENT_AGE = timedelta(days=30)

# Probe-bearing items (Service, Host) get debounce; Node status is platform truth.
ProbeableItem = Service | Host


def platform_change_event(
    current: ProbeableItem, previous: ProbeableItem, now: datetime
) -> Event | None:
    """Emit a `changed` event when ArgoCD sync/health transitions.

    Fires on any sync/health transition (Synced->OutOfSync, Degraded->Healthy,
    etc.). Config-derived, so no debounce — these are deliberate states.
    """
    if not isinstance(current, Service) or not isinstance(previous, Service):
        return None
    curr = current.platform.argocd
    prev = previous.platform.argocd
    if curr is None or prev is None:
        return None
    if curr.sync == prev.sync and curr.health == prev.health:
        return None
    return Event(
        timestamp=now,
        type=EventType.CHANGED,
        resource_id=current.id,
        summary=(
            f"{current.name}: sync {prev.sync}->{curr.sync}, "
            f"health {prev.health}->{curr.health}"
        ),
    )


def apply_debounce(
    current: ProbeableItem, previous: ProbeableItem | None
) -> ProbeableItem:
    """Apply asymmetric debounce to a probed item's fresh status.

    Failure count accumulates across runs; status only flips to down at the
    threshold. A single success resets the count and flips up immediately.
    """
    if current.probe is None:
        return current
    prev_failures = (
        previous.probe.consecutive_failures if previous and previous.probe else 0
    )

    if current.status == Status.DOWN:
        current.probe.consecutive_failures = prev_failures + 1
        if previous and current.probe.consecutive_failures < DOWN_THRESHOLD:
            current.status = previous.status  # blip: hold previous status
    elif current.status == Status.UP:
        current.probe.consecutive_failures = 0
    return current


def down_summary(item: ProbeableItem) -> str:
    """Event summary for a down transition, with probe detail.

    Names the probe target and the observed failure (e.g. ReadTimeout,
    HTTP 503) so the feed says WHAT failed, not just that something did.
    """
    if item.probe is None:
        return f"{item.name} is down"
    reason = f": {item.probe.last_error}" if item.probe.last_error else ""
    return (
        f"{item.name} is down ({item.probe.type} probe to {item.probe.target} "
        f"failed{reason}, {item.probe.consecutive_failures} consecutive polls)"
    )


def recovered_summary(item: ProbeableItem) -> str:
    """Event summary for a recovery, naming the probe that succeeded."""
    if item.probe is None:
        return f"{item.name} recovered"
    return f"{item.name} recovered ({item.probe.type} probe to {item.probe.target} ok)"


def diff_probed_items(
    current_items: list[ProbeableItem],
    previous_items: list[ProbeableItem],
    now: datetime,
) -> list[Event]:
    """Diff probed items (services or hosts) against their previous state."""
    previous_by_id = {item.id: item for item in previous_items}
    events: list[Event] = []

    for item in current_items:
        previous = previous_by_id.pop(item.id, None)
        apply_debounce(item, previous)
        item.last_seen = now

        if previous is None:
            item.first_seen = now
            item.up_since = now if item.status == Status.UP else None
            events.append(
                Event(
                    timestamp=now,
                    type=EventType.ADDED,
                    resource_id=item.id,
                    summary=f"{item.name} discovered",
                )
            )
            continue

        item.first_seen = previous.first_seen or now
        item.up_since = previous.up_since

        if previous.status == item.status:
            if changed := platform_change_event(item, previous, now):
                events.append(changed)
            continue
        if item.status == Status.DOWN:
            events.append(
                Event(
                    timestamp=now,
                    type=EventType.DOWN,
                    resource_id=item.id,
                    summary=down_summary(item),
                )
            )
            item.up_since = None
        elif item.status == Status.UP:
            events.append(
                Event(
                    timestamp=now,
                    type=EventType.UP,
                    resource_id=item.id,
                    summary=recovered_summary(item),
                )
            )
            item.up_since = now

    for removed in previous_by_id.values():
        events.append(
            Event(
                timestamp=now,
                type=EventType.REMOVED,
                resource_id=removed.id,
                summary=f"{removed.name} no longer discovered",
            )
        )
    return events


def diff_nodes(
    current_nodes: list[Node], previous_nodes: list[Node], now: datetime
) -> list[Event]:  # sourcery skip: for-append-to-extend
    """Diff platform-derived node status — flips fire immediately."""
    previous_by_id = {node.id: node for node in previous_nodes}
    events: list[Event] = []

    for node in current_nodes:
        previous = previous_by_id.pop(node.id, None)
        if previous is None:
            events.append(
                Event(
                    timestamp=now,
                    type=EventType.ADDED,
                    resource_id=node.id,
                    summary=f"node {node.name} joined",
                )
            )
        elif previous.status != node.status:
            events.append(
                Event(
                    timestamp=now,
                    type=EventType.DOWN if node.status == Status.DOWN else EventType.UP,
                    resource_id=node.id,
                    summary=f"node {node.name} is {node.status}",
                )
            )

    for removed in previous_by_id.values():
        events.append(
            Event(
                timestamp=now,
                type=EventType.REMOVED,
                resource_id=removed.id,
                summary=f"node {removed.name} left the cluster",
            )
        )
    return events


def cap_events(events: list[Event], now: datetime) -> list[Event]:
    """Cap the event feed: newest-first, max count, max age."""
    newest_first = sorted(events, key=lambda e: e.timestamp, reverse=True)
    capped = [event for event in newest_first if now - event.timestamp <= MAX_EVENT_AGE]
    return capped[:MAX_EVENTS]


def merge_events(previous: list[Event], new: list[Event], now: datetime) -> list[Event]:
    """Append new events to the rolling feed and apply retention."""
    return cap_events(previous + new, now)


def diff_snapshot(current: Snapshot, previous: Snapshot | None) -> Snapshot:
    """Diff a fresh snapshot against the previous one, in place.

    Carries first_seen/up_since forward, applies debounce, and returns the
    current snapshot with the merged, capped event feed attached.
    """
    now = current.generated_at
    if previous is None:
        for item in (*current.services, *current.hosts):
            item.first_seen = now
            item.last_seen = now
            item.up_since = now if item.status == Status.UP else None
        return current

    events = diff_probed_items(current.services, previous.services, now)
    events += diff_probed_items(current.hosts, previous.hosts, now)
    events += diff_nodes(current.nodes, previous.nodes, now)
    current.events = merge_events(previous.events, events, now)
    return current
