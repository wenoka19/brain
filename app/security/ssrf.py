"""SSRF defence: DNS resolution + per-hop private-IP validation.

Every outbound fetch in `brain` goes through :func:`validate_url` (or the
convenience wrapper :func:`safe_get`). Automatic redirects are *disabled*;
each hop is re-resolved and re-validated so an attacker cannot 302 us from a
public host onto 169.254.169.254.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol
from urllib.parse import urlsplit, urlunsplit

from app.config import settings

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443})

#: Networks that must never be reachable from the fetchers.
BLOCKED_V4 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",  # CGNAT
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local + cloud metadata
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
BLOCKED_V6 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "::/128",
        "::1/128",
        "fc00::/7",  # unique-local
        "fe80::/10",  # link-local
        "ff00::/8",  # multicast
        "2001:db8::/32",
    )
)


class SSRFBlocked(Exception):
    """Raised when a URL (or one of its redirect hops) is not safe to fetch."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class Resolver(Protocol):
    def __call__(self, host: str) -> list[str]:  # pragma: no cover - protocol
        ...


def system_resolver(host: str) -> list[str]:
    """Resolve ``host`` to every A/AAAA address the OS knows about."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise SSRFBlocked("dns_failure", f"{host}: {exc}") from exc
    addrs = {info[4][0] for info in infos}
    if not addrs:
        raise SSRFBlocked("dns_failure", host)
    return sorted(addrs)


#: Offline resolution table used when ``BRAIN_ENV=test`` so the suite never
#: touches a real DNS server. Hosts absent from the table are treated as
#: unresolvable rather than silently hitting the network.
TEST_DNS: dict[str, list[str]] = {
    "www.youtube.com": ["142.250.1.1"],
    "youtube.com": ["142.250.1.1"],
    "youtu.be": ["142.250.1.2"],
    "github.com": ["140.82.121.3"],
    "raw.githubusercontent.com": ["185.199.108.133"],
    "example.com": ["93.184.216.34"],
    "blog.example.com": ["93.184.216.35"],
    # Deliberately dangerous fixtures for the security tests.
    "internal.example.com": ["10.1.2.3"],
    "metadata.example.com": ["169.254.169.254"],
    "localhost.example.com": ["127.0.0.1"],
    "v6-internal.example.com": ["fd00::1"],
}


def test_resolver(host: str) -> list[str]:
    try:
        return list(TEST_DNS[host])
    except KeyError as exc:
        raise SSRFBlocked("dns_failure", f"{host} not in offline DNS table") from exc


def default_resolver() -> Resolver:
    return test_resolver if settings().is_test else system_resolver


def is_blocked_ip(raw: str) -> bool:
    """True when ``raw`` is an address the fetchers must refuse."""
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return True  # unparseable == untrusted
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        return True
    if ip.is_reserved or ip.is_unspecified:
        return True
    nets: Iterable = BLOCKED_V4 if ip.version == 4 else BLOCKED_V6
    return any(ip in net for net in nets)


@dataclass(frozen=True)
class ValidatedTarget:
    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]


def validate_url(raw_url: str, *, resolver: Resolver | None = None) -> ValidatedTarget:
    """Validate scheme/credentials/port, then resolve and screen every address."""
    if not raw_url or len(raw_url) > 2048:
        raise SSRFBlocked("malformed_url", "empty or oversized")

    parts = urlsplit(raw_url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SSRFBlocked("scheme_not_allowed", scheme or "<none>")
    if parts.username or parts.password:
        raise SSRFBlocked("credentials_in_url", parts.hostname or "")

    host = (parts.hostname or "").strip().rstrip(".")
    if not host:
        raise SSRFBlocked("malformed_url", "no host")

    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise SSRFBlocked("malformed_url", f"bad port: {exc}") from exc
    if port not in ALLOWED_PORTS:
        raise SSRFBlocked("port_not_allowed", str(port))

    # An IP literal needs no DNS, but still needs screening.
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        addresses = (resolver or default_resolver())(host)

    for addr in addresses:
        if is_blocked_ip(addr):
            raise SSRFBlocked("private_address", f"{host} -> {addr}")

    normalized = urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))
    return ValidatedTarget(
        url=normalized,
        scheme=scheme,
        host=host,
        port=port,
        addresses=tuple(addresses),
    )


def safe_get(
    raw_url: str,
    *,
    resolver: Resolver | None = None,
    headers: dict[str, str] | None = None,
    max_bytes: int | None = None,
):
    """Fetch ``raw_url`` with redirects disabled and every hop re-validated.

    Returns ``(final_url, content_type, body_bytes)``. Only used in prod mode;
    the test providers never reach the network.
    """
    import httpx  # imported lazily so test runs do not need the dependency

    cfg = settings()
    cap = max_bytes or cfg.max_fetch_bytes
    hops = 0
    url = raw_url
    merged_headers = {"User-Agent": "brain/0.1 (+personal-knowledge-server)"}
    merged_headers.update(headers or {})

    with httpx.Client(follow_redirects=False, timeout=cfg.http_timeout_sec) as client:
        while True:
            target = validate_url(url, resolver=resolver)
            response = client.get(target.url, headers=merged_headers)
            if response.is_redirect:
                hops += 1
                if hops > cfg.max_redirect_hops:
                    raise SSRFBlocked("too_many_redirects", str(hops))
                location = response.headers.get("location")
                if not location:
                    raise SSRFBlocked("malformed_redirect", "no Location header")
                url = str(response.url.join(location))
                continue

            response.raise_for_status()
            body = response.content
            if len(body) > cap:
                raise SSRFBlocked("response_too_large", f"{len(body)} > {cap}")
            return (
                str(response.url),
                response.headers.get("content-type", ""),
                body,
            )
