"""Tests for the probers: status classification, TCP/DNS probes, concurrency."""

import asyncio

import httpx
import respx

from homelab_live_status.models import (
    Host,
    Probe,
    ProbeType,
    Service,
    ServiceMetadata,
    Status,
)
from homelab_live_status.prober import (
    build_dns_query,
    is_up,
    probe_all,
    probe_dns,
    probe_one,
    probe_tcp,
)

HTTP_OK = 200
DNS_HEADER_BYTES = 12


def make_service(name: str, url: str) -> Service:
    """Build a minimal Service for probing tests."""
    return Service(
        id=f"traefik/file/{name}",
        name=name,
        group="External",
        url=url,
        probe=Probe(type=ProbeType.HTTP, target=url),
        metadata=ServiceMetadata(provider="file"),
    )


def test_is_up_classification() -> None:
    """Any response except the bad-gateway family (502/503/504) counts as up."""
    for code in (200, 204, 301, 302, 400, 401, 403, 404, 415, 500):
        assert is_up(code), code
    for code in (502, 503, 504):
        assert not is_up(code), code


@respx.mock
async def test_probe_one_up() -> None:
    """A 200 response marks the service up with the status code recorded."""
    respx.get("https://app.example/").mock(return_value=httpx.Response(200))
    service = make_service("app", "https://app.example/")
    async with httpx.AsyncClient() as client:
        result = await probe_one(client, service, asyncio.Semaphore(1))
    assert result.status == Status.UP
    assert result.probe is not None and result.probe.last_status_code == HTTP_OK


@respx.mock
async def test_probe_one_authentik_redirect_is_up() -> None:
    """A 302 to Authentik still proves the chain works."""
    respx.get("https://gated.example/").mock(return_value=httpx.Response(302))
    service = make_service("gated", "https://gated.example/")
    async with httpx.AsyncClient() as client:
        result = await probe_one(client, service, asyncio.Semaphore(1))
    assert result.status == Status.UP


@respx.mock
async def test_probe_one_404_is_up() -> None:
    """A 404 means the app answered — the chain works, so the service is up."""
    respx.get("https://noroot.example/").mock(return_value=httpx.Response(404))
    service = make_service("noroot", "https://noroot.example/")
    async with httpx.AsyncClient() as client:
        result = await probe_one(client, service, asyncio.Semaphore(1))
    assert result.status == Status.UP


@respx.mock
async def test_probe_one_503_is_down() -> None:
    """A 503 (Traefik cannot reach the backend) marks the service down."""
    respx.get("https://sick.example/").mock(return_value=httpx.Response(503))
    service = make_service("sick", "https://sick.example/")
    async with httpx.AsyncClient() as client:
        result = await probe_one(client, service, asyncio.Semaphore(1))
    assert result.status == Status.DOWN


@respx.mock
async def test_probe_one_timeout_is_down() -> None:
    """A connection failure marks the service down with no status code."""
    respx.get("https://dead.example/").mock(side_effect=httpx.ConnectError("refused"))
    service = make_service("dead", "https://dead.example/")
    async with httpx.AsyncClient() as client:
        result = await probe_one(client, service, asyncio.Semaphore(1))
    assert result.status == Status.DOWN
    assert result.probe is not None and result.probe.last_status_code is None


@respx.mock
async def test_probe_all_probes_everything() -> None:
    """All services get probed, and results are independent."""
    respx.get("https://a.example/").mock(return_value=httpx.Response(200))
    respx.get("https://b.example/").mock(return_value=httpx.Response(503))
    services = [
        make_service("a", "https://a.example/"),
        make_service("b", "https://b.example/"),
    ]
    async with httpx.AsyncClient() as client:
        results = await probe_all(client, services)
    assert [s.status for s in results] == [Status.UP, Status.DOWN]


def make_tcp_host(name: str, target: str) -> Host:
    """Build a minimal Host with a TCP probe for probing tests."""
    return Host(
        id=f"adguard/host/{name}",
        name=name,
        probe=Probe(type=ProbeType.TCP, target=target),
    )


async def test_probe_tcp_open_port_is_up() -> None:
    """A TCP connect to a listening port succeeds."""
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        status = await probe_tcp(Probe(type=ProbeType.TCP, target=f"127.0.0.1:{port}"))
    assert status == Status.UP


async def test_probe_tcp_closed_port_is_down() -> None:
    """A TCP connect to a closed port fails."""
    probe = Probe(type=ProbeType.TCP, target="127.0.0.1:1")
    assert await probe_tcp(probe) == Status.DOWN


async def test_probe_tcp_unparseable_target_is_down() -> None:
    """A malformed target fails gracefully rather than raising."""
    probe = Probe(type=ProbeType.TCP, target="no-port-here")
    assert await probe_tcp(probe) == Status.DOWN


async def test_probe_one_dispatches_tcp() -> None:
    """probe_one routes TCP-probed hosts through the TCP prober."""
    host = make_tcp_host("testbox", "127.0.0.1:1")
    async with httpx.AsyncClient() as client:
        result = await probe_one(client, host, asyncio.Semaphore(1))
    assert result.status == Status.DOWN


def test_build_dns_query_structure() -> None:
    """A DNS query packet has a 12-byte header and a terminated QNAME."""
    packet = build_dns_query("adguard.homelab.lan")
    assert len(packet) > DNS_HEADER_BYTES
    assert packet[4:6] == b"\x00\x01"  # QDCOUNT = 1
    assert b"\x07adguard\x07homelab\x03lan\x00" in packet


async def test_probe_dns_real_server() -> None:
    """A DNS query to a real resolver gets a well-formed response."""
    probe = Probe(type=ProbeType.DNS, target="1.1.1.1:53/example.com")
    assert await probe_dns(probe) == Status.UP


async def test_probe_dns_dead_server() -> None:
    """A DNS query to a non-listening address fails."""
    probe = Probe(type=ProbeType.DNS, target="127.0.0.1:53999/example.com")
    assert await probe_dns(probe) == Status.DOWN


@respx.mock
async def test_probe_records_error_reason_on_bad_status() -> None:
    """A bad-gateway response records 'HTTP 503' as the last error."""
    respx.get("https://sick.example/").mock(return_value=httpx.Response(503))
    service = make_service("sick", "https://sick.example/")
    async with httpx.AsyncClient() as client:
        result = await probe_one(client, service, asyncio.Semaphore(1))
    assert result.probe is not None
    assert result.probe.last_error == "HTTP 503"


@respx.mock
async def test_probe_records_exception_type_on_failure() -> None:
    """A connection failure records the exception type as the last error."""
    respx.get("https://dead.example/").mock(side_effect=httpx.ConnectError("refused"))
    service = make_service("dead", "https://dead.example/")
    async with httpx.AsyncClient() as client:
        result = await probe_one(client, service, asyncio.Semaphore(1))
    assert result.probe is not None
    assert result.probe.last_error == "ConnectError"


@respx.mock
async def test_probe_clears_error_on_success() -> None:
    """A successful probe clears any previously recorded error."""
    respx.get("https://well.example/").mock(return_value=httpx.Response(200))
    service = make_service("well", "https://well.example/")
    service.probe.last_error = "ReadTimeout"
    async with httpx.AsyncClient() as client:
        result = await probe_one(client, service, asyncio.Semaphore(1))
    assert result.probe is not None
    assert result.probe.last_error is None
