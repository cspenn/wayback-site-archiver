import gzip

import pytest

from civil_service.exceptions import TransientFetchError, UnsafeTargetError
from civil_service.urls import (
    extract_links,
    host_port,
    is_ip_literal,
    is_same_domain,
    normalize_url,
    parse_sitemap,
    read_sitemap_bytes,
    validate_public_host,
)

SCHEMES = ("http", "https")


def test_normalize_and_extract_links_filters_schemes_and_fragments() -> None:
    html = """
    <p>Text</p>
    <a>No href</a>
    <a href="">Empty</a>
    <a class="btn" href="/about#team">About</a>
    <a href="/about#dup">About Again</a>
    <a href="mailto:hi@example.com">Mail</a>
    <a href="javascript:void(0)">JS</a>
    <a href="#top">Top</a>
    <a href="https://other.org/ref/">External</a>
    """
    links = extract_links(html, "https://example.com/blog/post", SCHEMES)
    assert links == ["https://example.com/about", "https://other.org/ref/"]
    assert normalize_url("https://example.com", "ftp://example.com/a", SCHEMES) is None
    assert normalize_url("https://example.com", "http:///missing-host", SCHEMES) is None
    assert normalize_url("https://example.com", "   ", SCHEMES) is None
    assert normalize_url("https://example.com", "http://[oops/", SCHEMES) is None
    for with_credentials in (
        "https://user:secret@example.com/private",
        "https://user@example.com/",
        "https://@example.com/",
    ):
        assert normalize_url("https://example.com", with_credentials, SCHEMES) is None
    assert extract_links(
        '<a href="https://u:p@a.com/x">c</a><a href="/ok">ok</a>', "https://a.com/", SCHEMES
    ) == ["https://a.com/ok"]
    assert extract_links(
        '<a href="http://[oops/">bad</a><a href="/ok">ok</a>', "https://a.com/", SCHEMES
    ) == ["https://a.com/ok"]


def test_extract_links_collects_page_tags_and_ignores_assets() -> None:
    html = """
    <map><area href="/map-page"></map>
    <iframe src="/embedded"></iframe>
    <iframe></iframe>
    <frame src="/framed">
    <link rel="canonical" href="https://example.com/canon">
    <link rel="Alternate" hreflang="de" href="/de/">
    <link rel="stylesheet" href="/style.css">
    <link href="/no-rel">
    <link rel="canonical">
    <img src="/pic.png" srcset="/pic2.png 2x">
    <script src="/app.js"></script>
    <a href="/map-page">dup</a>
    """
    assert extract_links(html, "https://example.com/", SCHEMES) == [
        "https://example.com/map-page",
        "https://example.com/embedded",
        "https://example.com/framed",
        "https://example.com/canon",
        "https://example.com/de/",
    ]


def test_unicode_hosts_become_ascii_so_the_check_and_the_fetch_use_one_name() -> None:
    assert (
        normalize_url("https://a.com/", "https://faß.example/x", SCHEMES)
        == "https://fass.example/x"
    )
    assert (
        normalize_url("https://a.com/", "https://BÜCHER.example:8443/p?q=1", SCHEMES)
        == "https://xn--bcher-kva.example:8443/p?q=1"
    )
    assert normalize_url("https://a.com/", "https://" + "é" * 70 + ".example/", SCHEMES) is None
    assert normalize_url("https://a.com/", "https://ex\u00e9:99999999/", SCHEMES) is None
    assert normalize_url("https://a.com/", "https://:8080é/", SCHEMES) is None


def test_ip_literals_are_recognized() -> None:
    assert is_ip_literal("127.0.0.1")
    assert is_ip_literal("::1")
    assert not is_ip_literal("example.com")


def test_is_same_domain_subdomains() -> None:
    assert is_same_domain("https://example.com/a", "example.com", include_subdomains=False) is True
    assert (
        is_same_domain("https://sub.example.com/a", "example.com", include_subdomains=False)
        is False
    )
    assert (
        is_same_domain("https://sub.example.com/a", "example.com", include_subdomains=True) is True
    )


def test_host_port_uses_scheme_defaults_and_rejects_bad_input() -> None:
    assert host_port("https://example.com/a") == ("example.com", 443)
    assert host_port("http://example.com/a") == ("example.com", 80)
    assert host_port("http://example.com:8080/a") == ("example.com", 8080)
    with pytest.raises(UnsafeTargetError):
        host_port("http://example.com:notaport/")
    with pytest.raises(UnsafeTargetError):
        host_port("http:///no-host")
    with pytest.raises(UnsafeTargetError):
        host_port("http://[oops/")


def test_validate_public_host_blocks_private_and_link_local_ips() -> None:
    assert validate_public_host("https://example.com/") == ("93.184.216.34",)
    assert validate_public_host("https://dual.example/") == (
        "2606:2800:220:1::1946",
        "93.184.216.34",
    )
    with pytest.raises(UnsafeTargetError):
        validate_public_host("https://mixed.example/")
    with pytest.raises(UnsafeTargetError):
        validate_public_host("http://meta.internal/latest")
    with pytest.raises(UnsafeTargetError):
        validate_public_host("http://private.internal/")
    for failing_resolver in ("unresolvable", "oserror", "dns-flaky"):
        with pytest.raises(TransientFetchError):  # a resolver can fail while the network is down
            validate_public_host(f"http://{failing_resolver}.invalid/")
    for bad_host in ("http://a..b.com/", "http://" + "a" * 70 + ".com/"):
        with pytest.raises(UnsafeTargetError):
            validate_public_host(bad_host)
    with pytest.raises(UnsafeTargetError):
        validate_public_host("http:///no-host")
    with pytest.raises(UnsafeTargetError):
        validate_public_host("http://example.com:99999999/")


def test_parse_sitemap_reads_urlset_and_index() -> None:
    urlset = (
        "<urlset><url><loc> https://example.com/a?x=1&amp;y=2 </loc></url>"
        "<URL><LOC>https://example.com/b</LOC></URL></urlset>"
    )
    assert parse_sitemap(urlset) == (
        False,
        ["https://example.com/a?x=1&y=2", "https://example.com/b"],
    )
    index = "<sitemapindex><sitemap><loc>https://example.com/s1.xml</loc></sitemap></sitemapindex>"
    assert parse_sitemap(index) == (True, ["https://example.com/s1.xml"])
    assert parse_sitemap("<loc><loc><loc>" * 50_000) == (False, [])


def test_read_sitemap_bytes_handles_plain_gzip_bomb_and_garbage() -> None:
    assert read_sitemap_bytes(b"<urlset/>", cap=100) == "<urlset/>"
    assert read_sitemap_bytes(gzip.compress(b"<urlset/>"), cap=100) == "<urlset/>"
    bomb = gzip.compress(b"a" * 1_000_000)
    assert len(bomb) < 5_000
    assert read_sitemap_bytes(bomb, cap=1_000) is None
    assert read_sitemap_bytes(b"\x1f\x8bnot-gzip-data", cap=1_000) is None
