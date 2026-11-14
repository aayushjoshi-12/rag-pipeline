"""SSRF guard: a table of hostile inputs, resolver games (mixed answers, rebinding), s3 links."""

from __future__ import annotations

import pytest
from app.services.urlguard import (
    UrlRejected,
    check_http_target,
    parse_legacy_ipv4,
    parse_s3_link,
    parse_url,
    redact_url,
)

SCHEMES = ("http", "https", "s3")
PUBLIC = "93.184.216.34"


def _fake(mapping: dict[str, list[str]]):
    def resolve(host: str, port: int) -> list[str]:
        if host not in mapping:
            raise UrlRejected(f"Could not resolve host '{host}'.")
        return mapping[host]

    return resolve


def _check(url: str, resolver=None, allow_private: bool = False):
    return check_http_target(
        url,
        schemes=SCHEMES,
        allow_private=allow_private,
        resolver=resolver or _fake({"example.test": [PUBLIC]}),
    )


BLOCKED_LITERALS = [
    # IPv4 in every notation for 127.0.0.1
    "http://127.0.0.1/",
    "http://127.1/",
    "http://127.0.1/",
    "http://2130706433/",  # decimal
    "http://0x7f000001/",  # hex
    "http://0x7f.0.0.1/",
    "http://0177.0.0.1/",  # octal
    "http://017700000001/",  # octal whole
    "http://0177.0.0.01/",
    "http://127.000.000.001/",
    "http://0/",
    "http://0.0.0.0/",
    "http://127。0。0。1/",  # ideographic full stop
    # private / link-local / CGNAT / metadata
    "http://10.0.0.5/",
    "http://172.16.0.1/",
    "http://172.31.255.255/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://169.254.170.2/",
    "http://2852039166/",  # 169.254.169.254 decimal
    "http://0xa9fea9fe/",
    "http://100.64.0.1/",
    "http://100.100.100.200/",  # alibaba metadata (CGNAT)
    "http://168.63.129.16/",  # azure wireserver (public-looking)
    "http://192.0.0.192/",  # oracle metadata
    "http://224.0.0.1/",
    "http://255.255.255.255/",
    "http://198.18.0.1/",
    # IPv6
    "http://[::1]/",
    "http://[::]/",
    "http://[0:0:0:0:0:0:0:1]/",
    "http://[fe80::1]/",
    "http://[fc00::1]/",
    "http://[fd00:ec2::254]/",  # aws ipv6 metadata
    "http://[ff02::1]/",
    "http://[::ffff:127.0.0.1]/",  # IPv4-mapped
    "http://[::ffff:7f00:1]/",
    "http://[::ffff:169.254.169.254]/",
    "http://[::ffff:10.0.0.1]/",
    "http://[64:ff9b::7f00:1]/",  # NAT64 -> 127.0.0.1
    "http://[2002:7f00:1::]/",  # 6to4 -> 127.0.0.1
    "http://[2002:a9fe:a9fe::1]/",  # 6to4 -> metadata
    "http://[::127.0.0.1]/",  # IPv4-compatible
    # names
    "http://localhost/",
    "http://LOCALHOST/",
    "http://localhost./",
    "http://foo.localhost/",
    "http://printer.local/",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://db.internal/",
    "http://metadata/",
]


@pytest.mark.parametrize("url", BLOCKED_LITERALS)
def test_blocks_hostile_literals_and_names(url: str) -> None:
    with pytest.raises(UrlRejected):
        _check(url)


def test_allow_private_really_allows_loopback() -> None:
    t = _check("http://127.0.0.1:8123/x?y=1", allow_private=True)
    assert t.ips == ("127.0.0.1",) and t.port == 8123 and t.path_query == "/x?y=1"


@pytest.mark.parametrize("url", ["http://8.8.8.8/", "https://[2606:4700:4700::1111]/", "http://1.1.1.1:8080/a"])
def test_public_literals_pass(url: str) -> None:
    assert _check(url).ips


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.test/a.pdf",
        "file:///etc/passwd",
        "gopher://example.test/",
        "javascript:alert(1)",
        "data:text/plain,hi",
        "dict://example.test:11211/",
        "//example.test/a",
        "example.test/a.pdf",
        "",
        "   ",
        "http://",
        "http:///a",
    ],
)
def test_bad_schemes_and_shapes(url: str) -> None:
    with pytest.raises(UrlRejected):
        _check(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pw@example.test/a.pdf",
        "http://user@example.test/a.pdf",
        "http://example.test@127.0.0.1/",
        "http://127.0.0.1#@example.test/",
        "http://example.test\\@127.0.0.1/",
        "http://exa mple.test/",
        "http://example.test/a\r\nHost: evil",
        "http://example.test:99999/",
        "http://example.test:0/",
        "http://[::1%25eth0]/",
        "http://0x7f.0.0.256/",
        "http://08.0.0.1/",
        "http://1.2.3.4.5/" ,
    ],
)
def test_credentials_confusion_and_malformed(url: str) -> None:
    try:
        target = _check(url, resolver=_fake({"1.2.3.4.5": [PUBLIC]}))
    except UrlRejected:
        return
    # the only acceptable "accept" is a hostname that resolved to a public address
    assert url == "http://1.2.3.4.5/" and target.ips == (PUBLIC,)


def test_url_length_limit() -> None:
    with pytest.raises(UrlRejected):
        _check("http://example.test/" + "a" * 3000)


def test_mixed_public_and_private_answers_are_refused() -> None:
    r = _fake({"example.test": [PUBLIC, "10.0.0.7"]})
    with pytest.raises(UrlRejected):
        _check("http://example.test/", resolver=r)
    r6 = _fake({"example.test": [PUBLIC, "::1"]})
    with pytest.raises(UrlRejected):
        _check("http://example.test/", resolver=r6)


def test_name_resolving_to_private_is_refused_and_public_is_accepted() -> None:
    with pytest.raises(UrlRejected):
        _check("http://rebind.test/", resolver=_fake({"rebind.test": ["192.168.0.10"]}))
    ok = _check("https://example.test/a.pdf")
    assert ok.ips == (PUBLIC,) and ok.scheme == "https" and ok.port == 443


def test_resolver_failure_is_a_rejection() -> None:
    with pytest.raises(UrlRejected):
        _check("http://nxdomain.test/", resolver=_fake({}))


def test_dns_rebinding_resolver_is_asked_once_and_target_is_pinned() -> None:
    """A rebinding DNS server answers public first, private afterwards. The guard resolves once and
    hands out the validated address, so the caller never needs to ask again."""
    answers = [[PUBLIC], ["127.0.0.1"], ["127.0.0.1"]]
    calls: list[str] = []

    def resolver(host: str, port: int) -> list[str]:
        calls.append(host)
        return answers[len(calls) - 1]

    target = _check("http://rebind.test/x", resolver=resolver)
    assert target.ips == (PUBLIC,) and len(calls) == 1
    # a second validation (the next hop) sees the private answer and is refused
    with pytest.raises(UrlRejected):
        _check("http://rebind.test/x", resolver=resolver)


def test_host_header_and_ipv6() -> None:
    assert _check("http://8.8.8.8:8080/").host_header == "8.8.8.8:8080"
    assert _check("http://8.8.8.8/").host_header == "8.8.8.8"
    assert _check("https://[2606:4700:4700::1111]/").host_header == "[2606:4700:4700::1111]"


def test_idna_host_is_punycoded_before_resolving() -> None:
    seen: list[str] = []

    def resolver(host: str, port: int) -> list[str]:
        seen.append(host)
        return [PUBLIC]

    _check("http://bücher.example/", resolver=resolver)
    assert seen == ["xn--bcher-kva.example"]


@pytest.mark.parametrize(
    ("host", "expected"),
    [("2130706433", "127.0.0.1"), ("0x7f.1", "127.0.0.1"), ("0177.0.0.1", "127.0.0.1"), ("127.1", "127.0.0.1"), ("1.2.3.4", "1.2.3.4")],
)
def test_legacy_ipv4_parser(host: str, expected: str) -> None:
    assert str(parse_legacy_ipv4(host)) == expected


def test_legacy_parser_leaves_hostnames_alone() -> None:
    assert parse_legacy_ipv4("example.com") is None
    assert parse_legacy_ipv4("1.2.3.4.example.com") is None


def test_scheme_allow_list_is_configurable() -> None:
    with pytest.raises(UrlRejected):
        check_http_target("http://8.8.8.8/", schemes=("https",), allow_private=False)
    with pytest.raises(UrlRejected):  # redirects may not hop to s3://
        check_http_target("s3://bucket/key", schemes=SCHEMES, allow_private=False)


def test_redact_url_drops_credentials_and_query() -> None:
    out = redact_url("https://user:pw@host.test:8443/a/b.pdf?X-Amz-Signature=SECRET&t=1#frag")
    assert "SECRET" not in out and "pw" not in out and "user" not in out and "frag" not in out
    assert out.startswith("https://host.test:8443/a/b.pdf")
    assert redact_url("https://host.test/a.pdf") == "https://host.test/a.pdf"
    assert redact_url("http://[::1]:80/x") == "http://[::1]:80/x"


@pytest.mark.parametrize(
    "url",
    [
        "s3://other/key.pdf",
        "s3://ok/",
        "s3://ok",
        "s3://ok/a/../b.pdf",
        "s3://ok/key.pdf?versionId=1",
        "s3://user:pw@ok/key.pdf",
        "s3:///key.pdf",
    ],
)
def test_s3_links_rejected(url: str) -> None:
    with pytest.raises(UrlRejected):
        parse_s3_link(url, allowed_buckets=("ok",))


def test_s3_link_ok_and_percent_decoded() -> None:
    link = parse_s3_link("s3://OK/dir/my%20file.pdf", allowed_buckets=("ok",))
    assert (link.bucket, link.key) == ("ok", "dir/my file.pdf")


def test_parse_url_requires_allowed_scheme() -> None:
    with pytest.raises(UrlRejected, match="Allowed"):
        parse_url("ftp://x/y", schemes=("http",))


@pytest.mark.parametrize(
    "ip",
    ["3fff::1", "2001:2::1", "2001:10::1"],
)
def test_non_global_addresses_outside_the_named_tables_are_still_refused(ip: str) -> None:
    """Belt and braces: anything Python knows is not globally routable is refused too."""
    host = f"[{ip}]" if ":" in ip else ip
    with pytest.raises(UrlRejected):
        _check(f"http://{host}/")


@pytest.mark.parametrize(
    "name",
    ["localhost", "LocalHost", "app.localhost", "printer.local", "db.internal", "metadata.google.internal",
     "metadata", "instance-data", "nas.localdomain", "router.home.arpa", "localhost."],
)
def test_reserved_names_are_refused_even_if_the_resolver_says_public(name: str) -> None:
    """A poisoned/misconfigured resolver must not turn `localhost` into a fetchable name."""
    with pytest.raises(UrlRejected, match="not allowed"):
        _check(f"http://{name}/", resolver=lambda host, port: [PUBLIC])


def test_ipv6_zone_identifier_is_refused_for_a_public_address() -> None:
    with pytest.raises(UrlRejected, match="zone"):
        _check("http://[2606:4700:4700::1111%25eth0]/")


def test_ipv4_mapped_and_6to4_unwrap_decides_on_the_embedded_address() -> None:
    import ipaddress

    from app.services.urlguard import blocked_reason

    assert blocked_reason(ipaddress.ip_address("::ffff:93.184.216.34")) is None  # public inside the mapping
    assert blocked_reason(ipaddress.ip_address("::ffff:10.1.1.1")) is not None
    assert blocked_reason(ipaddress.ip_address("2002:5db8:d822::1")) is None  # 6to4 of a public v4
    assert blocked_reason(ipaddress.ip_address("2002:0a01:0101::1")) is not None  # 6to4 of 10.1.1.1
    assert blocked_reason(ipaddress.ip_address("64:ff9b::5db8:d822")) is None  # NAT64 of a public v4
    assert blocked_reason(ipaddress.ip_address("64:ff9b::a01:101")) is not None  # NAT64 of 10.1.1.1
