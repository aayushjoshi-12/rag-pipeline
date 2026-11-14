"""SSRF guard for "ingest from a link" (ticket 22).

Everything that decides whether a URL may be fetched lives here, so it can be tested without a
network. The guarantees:

* scheme allow-list; no ``user:pass@`` (credentials in a URL end up in logs and traces);
* the host is parsed by us (IPv4 in decimal / hex / octal / short forms, IPv6, IPv4-mapped,
  NAT64, 6to4, IDNA) - never left to the HTTP client to interpret;
* a hostname is resolved by us and **every** address it resolves to must be allowed (a name with
  one public and one private A record is refused);
* the caller connects to the address returned here, not to the hostname, so a second DNS answer
  (rebinding) cannot redirect the request;
* each redirect hop goes through the same checks (see ``fetcher.py``).
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

MAX_URL_LENGTH = 2048
DEFAULT_PORTS = {"http": 80, "https": 443}

#: Resolver seam: (host, port) -> textual IP addresses. Tests inject fakes (rebinding etc.).
Resolver = Callable[[str, int], list[str]]


class UrlRejected(ValueError):
    """The URL must not be fetched. The message is safe to show to the user (no secrets)."""


_V4_BLOCKED = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",  # "this network"; 0.0.0.0 reaches localhost on Linux
        "10.0.0.0/8",
        "100.64.0.0/10",  # carrier-grade NAT (also Alibaba metadata 100.100.100.200)
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local incl. cloud metadata 169.254.169.254 / 169.254.170.2
        "172.16.0.0/12",
        "192.0.0.0/24",  # IETF protocol assignments (Oracle metadata 192.0.0.192)
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved + broadcast
    )
]
#: Public-looking addresses that are nevertheless cloud control planes.
_V4_BLOCKED_HOSTS = [ipaddress.ip_address(a) for a in ("168.63.129.16",)]  # Azure wireserver
_V6_BLOCKED = [
    ipaddress.ip_network(n)
    for n in (
        "::/96",  # unspecified, loopback, deprecated IPv4-compatible
        "fc00::/7",  # unique local (AWS metadata fd00:ec2::254)
        "fe80::/10",  # link-local
        "fec0::/10",  # deprecated site-local
        "ff00::/8",  # multicast
        "100::/64",  # discard-only
        "2001::/32",  # Teredo
        "2001:db8::/32",  # documentation
        "64:ff9b:1::/48",  # local-use NAT64
    )
]
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIXTOFOUR = ipaddress.ip_network("2002::/16")

_BLOCKED_NAMES = {"localhost", "metadata", "metadata.google.internal", "instance-data"}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")


def blocked_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why ``ip`` may not be fetched, or None when it is a public unicast address."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return blocked_reason(ip.ipv4_mapped)
        if ip in _NAT64:
            return blocked_reason(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip in _SIXTOFOUR:
            return blocked_reason(ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF))
        if any(ip in net for net in _V6_BLOCKED):
            return "a private, loopback or link-local IPv6 address"
    else:
        if any(ip in net for net in _V4_BLOCKED) or ip in _V4_BLOCKED_HOSTS:
            return "a private, loopback, link-local or metadata IPv4 address"
    if not ip.is_global:
        return "a non-public address"
    return None


_NUMERIC_PART = re.compile(r"0[xX][0-9a-fA-F]*|[0-9]+")


def _numeric_part(part: str) -> int:
    if part[:2] in ("0x", "0X"):
        return int(part[2:] or "0", 16)
    if len(part) > 1 and part.startswith("0"):
        return int(part, 8)  # raises for 08/09 like inet_aton
    return int(part)


def parse_legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Parse the forms ``inet_aton`` accepts (``2130706433``, ``0x7f.1``, ``0177.0.0.1``, ``127.1``).

    Returns None when ``host`` is an ordinary hostname; raises ``UrlRejected`` when it is made of
    numeric parts but is not a valid address (refused rather than guessed at).
    """
    parts = host.split(".")
    if not 1 <= len(parts) <= 4 or not all(_NUMERIC_PART.fullmatch(p) for p in parts):
        return None
    try:
        nums = [_numeric_part(p) for p in parts]
    except ValueError:
        raise UrlRejected("The host looks like a malformed IP address.") from None
    *head, last = nums
    if any(n > 255 for n in head) or last >= 256 ** (5 - len(parts)):
        raise UrlRejected("The host looks like a malformed IP address.")
    value = last
    for i, n in enumerate(head):
        value |= n << (8 * (3 - i))
    return ipaddress.IPv4Address(value)


def normalise_host(raw: str) -> str:
    host = raw.strip().rstrip(".").lower()
    if not host:
        raise UrlRejected("The URL has no host.")
    if any(c.isspace() or ord(c) < 32 or c in "\\@" for c in host):
        raise UrlRejected("The URL host contains invalid characters.")
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise UrlRejected("The URL host is not a valid internationalised name.") from None
    return host


def ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a host string denotes when it is an IP literal in any notation, else None."""
    if ":" in host:
        if "%" in host:
            raise UrlRejected("IPv6 zone identifiers are not allowed.")
        try:
            return ipaddress.IPv6Address(host)
        except ValueError:
            raise UrlRejected("The host is not a valid IPv6 address.") from None
    return parse_legacy_ipv4(host)


def _name_blocked(host: str) -> bool:
    return host in _BLOCKED_NAMES or host.endswith(_BLOCKED_SUFFIXES)


def default_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UrlRejected(f"Could not resolve host '{host}'.") from exc
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


@dataclass(frozen=True)
class Target:
    """A validated http(s) hop: connect to ``ips[0]`` (fall back along ``ips``), send ``host``."""

    scheme: str
    host: str  # normalised hostname or textual IP, used for Host header + TLS name
    port: int
    path_query: str
    ips: tuple[str, ...]

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == DEFAULT_PORTS[self.scheme] else f"{host}:{self.port}"


def redact_url(url: str) -> str:
    """The URL without userinfo, query and fragment: safe for logs, DB rows, API responses.

    Presigned URLs carry their secret in the query string, so the query is always dropped.
    """
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = host + (f":{parts.port}" if parts.port else "")
        path = parts.path[:300]
        return urlunsplit((parts.scheme, netloc, path, "", "")) + ("?…" if parts.query else "")
    except ValueError:
        return "<invalid url>"


@dataclass(frozen=True)
class ParsedUrl:
    scheme: str
    host: str
    port: int
    path_query: str


def parse_url(url: str, *, schemes: tuple[str, ...]) -> ParsedUrl:
    """Syntax + scheme checks (no DNS). Raises ``UrlRejected``."""
    if not isinstance(url, str) or not url.strip():
        raise UrlRejected("Enter a URL.")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise UrlRejected(f"The URL is longer than {MAX_URL_LENGTH} characters.")
    if any(ord(c) < 32 or ord(c) == 127 or c.isspace() for c in url):
        raise UrlRejected("The URL contains whitespace or control characters.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UrlRejected("The URL is not valid.") from None
    scheme = parts.scheme.lower()
    if scheme not in schemes:
        allowed = ", ".join(f"{s}://" for s in schemes)
        raise UrlRejected(f"Unsupported URL scheme '{scheme or '(none)'}'. Allowed: {allowed}.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UrlRejected("URLs with embedded credentials (user:password@host) are not allowed.")
    if parts.hostname is None:
        raise UrlRejected("The URL has no host.")
    if port is not None and not 1 <= port <= 65535:
        raise UrlRejected("The URL port is out of range.")
    host = normalise_host(parts.hostname)
    path_query = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return ParsedUrl(scheme, host, port or DEFAULT_PORTS.get(scheme, 0), path_query)


def check_http_target(
    url: str,
    *,
    schemes: tuple[str, ...],
    allow_private: bool,
    resolver: Resolver | None = None,
) -> Target:
    """Validate one http(s) hop and return the addresses it may connect to."""
    parsed = parse_url(url, schemes=tuple(s for s in schemes if s in DEFAULT_PORTS))
    host = parsed.host
    literal = ip_literal(host)
    if literal is not None:
        ips = [str(literal)]
        host = str(literal)
        candidates = [literal]
    else:
        if not allow_private and _name_blocked(host):
            raise UrlRejected("That host name is not allowed (local or internal name).")
        raw = (resolver or default_resolver)(host, parsed.port)
        if not raw:
            raise UrlRejected(f"Could not resolve host '{host}'.")
        try:
            candidates = [ipaddress.ip_address(a) for a in raw]
        except ValueError:
            raise UrlRejected("The host resolved to an invalid address.") from None
        ips = [str(a) for a in candidates]
    if not allow_private:
        for ip in candidates:
            reason = blocked_reason(ip)
            if reason is not None:
                raise UrlRejected(
                    "That URL points at a private or internal network address and cannot be fetched."
                )
    return Target(parsed.scheme, host, parsed.port, parsed.path_query, tuple(dict.fromkeys(ips)))


@dataclass(frozen=True)
class S3Link:
    bucket: str
    key: str


def parse_s3_link(url: str, *, allowed_buckets: tuple[str, ...]) -> S3Link:
    """``s3://bucket/key`` -> (bucket, key); only buckets in the allow-list are readable."""
    from urllib.parse import unquote

    parsed = parse_url(url, schemes=("s3",))
    parts = urlsplit(url.strip())
    bucket = (parts.hostname or "").lower()
    key = unquote(parts.path.lstrip("/"))
    if parts.query:
        raise UrlRejected("s3:// links must not have a query string.")
    if not bucket or not key or key.endswith("/") or "\x00" in key or ".." in key.split("/"):
        raise UrlRejected("Use s3://<bucket>/<object key>.")
    if bucket not in {b.lower() for b in allowed_buckets}:
        raise UrlRejected(f"The bucket '{bucket}' is not allowed for s3:// ingest.")
    del parsed
    return S3Link(bucket, key)
