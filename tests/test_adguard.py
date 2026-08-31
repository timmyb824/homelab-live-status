"""Tests for the AdGuard collector: rewrite parsing and the name-join."""

import httpx
import respx

from homelab_live_status.collectors.adguard import (
    JoinContext,
    classify_rewrites,
    collect_adguard,
    rewrite_name,
)

REWRITES_FIXTURE = [
    {"domain": "filebrowser.homelab.lan", "answer": "192.168.86.33"},
    {"domain": "pihole2.homelab.lan", "answer": "192.168.86.174"},
    {"domain": "hp-laptop-ubuntu.homelab.lan", "answer": "192.168.86.99"},
    {"domain": "pve4.homelab.lan", "answer": "192.168.86.217"},
    {"domain": "k3s-0.homelab.lan", "answer": "192.168.86.50"},
    {"domain": "homelab-oci02.homelab.lan", "answer": "150.1.2.3"},
    {"domain": "nas.timmybtech.com", "answer": "192.168.86.44"},
    {"domain": "cnameservice.homelab.lan", "answer": "other.host.example"},
]


def test_rewrite_name_matches_internal_domain() -> None:
    """Names are extracted only from the internal rewrite domain."""
    assert rewrite_name("pihole2.homelab.lan", "homelab.lan") == "pihole2"
    assert rewrite_name("nas.timmybtech.com", "homelab.lan") is None
    assert rewrite_name("homelab.lan", "homelab.lan") is None


def test_classify_excludes_service_backends_by_name() -> None:
    """A rewrite whose name matches a Traefik service is not a host."""
    hosts = classify_rewrites(
        REWRITES_FIXTURE, JoinContext(service_names=frozenset({"filebrowser"}))
    )
    names = {h.name for h in hosts}
    assert "filebrowser" not in names
    assert "pihole2" in names


def test_classify_excludes_cluster_members() -> None:
    """Proxmox and k3s node names are excluded — they have own collectors."""
    hosts = classify_rewrites(
        REWRITES_FIXTURE,
        JoinContext(
            service_names=frozenset({"filebrowser"}),
            cluster_names=frozenset({"pve4", "k3s-0"}),
        ),
    )
    names = {h.name for h in hosts}
    assert "pve4" not in names
    assert "k3s-0" not in names


def test_classify_skips_non_ip_answers() -> None:
    """CNAME-style rewrites (non-IP answers) are skipped."""
    hosts = classify_rewrites(REWRITES_FIXTURE, JoinContext())
    assert "cnameservice" not in {h.name for h in hosts}


def test_classify_skips_other_domains() -> None:
    """Rewrites outside the internal domain are ignored."""
    hosts = classify_rewrites(REWRITES_FIXTURE, JoinContext())
    assert all(
        h.metadata.rewrite and h.metadata.rewrite.endswith(".homelab.lan")
        for h in hosts
    )


def test_classify_respects_explicit_exclude() -> None:
    """The static exclude list wins over discovery."""
    hosts = classify_rewrites(
        REWRITES_FIXTURE,
        JoinContext(
            service_names=frozenset({"filebrowser"}),
            exclude=frozenset({"pihole2"}),
        ),
    )
    assert "pihole2" not in {h.name for h in hosts}


def test_classified_host_has_tcp_probe_and_metadata() -> None:
    """Discovered hosts default to a tcp/22 probe with rewrite metadata."""
    hosts = classify_rewrites(
        REWRITES_FIXTURE, JoinContext(service_names=frozenset({"filebrowser"}))
    )
    pihole2 = next(h for h in hosts if h.name == "pihole2")
    assert pihole2.probe is not None
    assert pihole2.probe.target == "192.168.86.174:22"
    assert pihole2.metadata.ip == "192.168.86.174"


@respx.mock
async def test_collect_adguard_fetches_and_classifies() -> None:
    """The collector calls the AdGuard API and returns Host objects."""
    respx.get("http://adguard.example/control/rewrite/list").mock(
        return_value=httpx.Response(200, json=REWRITES_FIXTURE)
    )
    async with httpx.AsyncClient() as client:
        hosts = await collect_adguard(
            client,
            "http://adguard.example",
            JoinContext(
                service_names=frozenset({"filebrowser"}),
                cluster_names=frozenset({"pve4", "k3s-0"}),
            ),
        )
    names = {h.name for h in hosts}
    assert names == {"pihole2", "hp-laptop-ubuntu", "homelab-oci02"}
