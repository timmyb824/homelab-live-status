"""Concurrent probers: the universal up/down baseline (PLAN.md §6 #2, #13).

Services are probed over HTTP through their public route hostname, testing
the real user path (DNS → Traefik → upstream). Any HTTP response except the
bad-gateway family (502/503/504) counts as up — a 404 from Loki is Loki
being alive.

Hosts are probed with a TCP connect (default port 22, SSH); DNS probes send
a real query (for the CoreDNS nameserver callout).
"""

import asyncio
import socket
from typing import Protocol, TypeVar

import httpx

from homelab_live_status.models import Probe, ProbeType, Status

DOWN_STATUS_CODES = frozenset({502, 503, 504})

PROBE_TIMEOUT = 5
MAX_CONCURRENT = 20
DNS_HEADER_BYTES = 12


class Probeable(Protocol):
    """Anything with a probe and a status (Service or Host)."""

    status: Status
    probe: Probe | None


T = TypeVar("T", bound=Probeable)


def is_up(status_code: int) -> bool:
    """Decide whether an HTTP status code means the service chain is up.

    Any HTTP response proves liveness — a 404 from Loki is Loki being alive.
    Only the bad-gateway family (502/503/504), which Traefik returns when it
    cannot reach a backend, counts as down.
    """
    return status_code not in DOWN_STATUS_CODES


async def probe_http(client: httpx.AsyncClient, probe: Probe) -> Status:
    """Probe an HTTP target; record the status code and any failure reason."""
    try:
        response = await client.get(
            probe.target,
            timeout=PROBE_TIMEOUT,
            follow_redirects=False,
        )
        probe.last_status_code = response.status_code
        if is_up(response.status_code):
            probe.last_error = None
            return Status.UP
        probe.last_error = f"HTTP {response.status_code}"
        return Status.DOWN
    except httpx.HTTPError as exc:
        probe.last_status_code = None
        probe.last_error = type(exc).__name__
        return Status.DOWN


async def probe_tcp(probe: Probe) -> Status:
    """Probe a host:port target with a TCP connect."""
    host, _, port = probe.target.rpartition(":")
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port)), timeout=PROBE_TIMEOUT
        )
        writer.close()
        await writer.wait_closed()
        probe.last_error = None
        return Status.UP
    except (TimeoutError, OSError, ValueError) as exc:
        probe.last_error = type(exc).__name__
        return Status.DOWN


async def probe_dns(probe: Probe) -> Status:
    """Probe a DNS server by sending a real query over UDP.

    Target format: "<server-ip>:<port>/<query-name>", e.g.
    "192.168.86.174:53/adguard.homelab.lan". A well-formed response (even
    NXDOMAIN) counts as up.
    """
    server, _, query = probe.target.partition("/")
    host, _, port = server.rpartition(":")
    request = build_dns_query(query or "localhost")
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        await loop.sock_connect(sock, (host, int(port or 53)))
        await loop.sock_sendall(sock, request)
        response = await asyncio.wait_for(
            loop.sock_recv(sock, 512), timeout=PROBE_TIMEOUT
        )
        if len(response) >= DNS_HEADER_BYTES:
            probe.last_error = None
            return Status.UP
        probe.last_error = "short response"
        return Status.DOWN
    except (TimeoutError, OSError) as exc:
        probe.last_error = type(exc).__name__
        return Status.DOWN
    finally:
        sock.close()


def build_dns_query(name: str) -> bytes:
    """Build a minimal DNS A-record query packet for the given name."""
    header = (0x1234).to_bytes(2, "big") + b"\x01\x00" + b"\x00\x01" + b"\x00" * 6
    question = b"".join(
        bytes([len(label)]) + label.encode() for label in name.split(".")
    )
    return header + question + b"\x00" + b"\x00\x01" + b"\x00\x01"


async def probe_one(
    client: httpx.AsyncClient, item: T, semaphore: asyncio.Semaphore
) -> T:
    """Probe a single item (Service or Host) and set its status."""
    if item.probe is None:
        return item
    async with semaphore:
        if item.probe.type == ProbeType.HTTP:
            item.status = await probe_http(client, item.probe)
        elif item.probe.type == ProbeType.TCP:
            item.status = await probe_tcp(item.probe)
        elif item.probe.type == ProbeType.DNS:
            item.status = await probe_dns(item.probe)
    return item


async def probe_all(client: httpx.AsyncClient, items: list[T]) -> list[T]:
    """Probe all items concurrently, bounded by a semaphore.

    Probing must be concurrent: ~90 sequential probes with 5s timeouts could
    exceed the 5-minute collection interval in the worst case.
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    return await asyncio.gather(*(probe_one(client, item, semaphore) for item in items))
