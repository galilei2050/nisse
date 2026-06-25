"""ProxyPool: parse Webshare lines, pin one proxy per host, rotate only on ban (no browser).

The point under test is stickiness — the same host keeps the same proxy across calls — and that a
ban rotates to a different, non-banned proxy until the pool is exhausted.
"""

import pytest

from app.browser.proxy import ProxyPool, load_proxy_pool, parse_proxies

_LINES = """
1.1.1.1:8000:user:pass
2.2.2.2:8000:user:pass
# a comment line is ignored

3.3.3.3:8000:user:pass
""".strip()


def _pool() -> ProxyPool:
    return ProxyPool(parse_proxies(_LINES))


def test_parse_skips_blanks_and_comments() -> None:
    proxies = parse_proxies(_LINES)
    assert [p.server for p in proxies] == ["http://1.1.1.1:8000", "http://2.2.2.2:8000", "http://3.3.3.3:8000"]
    assert proxies[0].username == "user" and proxies[0].password == "pass"


def test_password_with_colons_is_kept_whole() -> None:
    [proxy] = parse_proxies("h:9:u:p:a:s:s")
    assert proxy.password == "p:a:s:s"


def test_comma_separated_entries() -> None:
    # single-line .env value: the Makefile can't carry a multiline value, so proxies are comma-joined
    proxies = parse_proxies("1.1.1.1:80:u:p,2.2.2.2:80:u:p")
    assert [p.server for p in proxies] == ["http://1.1.1.1:80", "http://2.2.2.2:80"]


def test_sticky_per_host() -> None:
    pool = _pool()
    first = pool.for_url("https://www.safeway.com/shop")
    again = pool.for_url("https://www.safeway.com/cart")
    assert first is not None
    assert again.server == first.server  # same host -> same proxy across calls


def test_different_hosts_spread_across_proxies() -> None:
    pool = _pool()
    a = pool.for_url("https://safeway.com")
    b = pool.for_url("https://doordash.com")
    assert a.server != b.server  # least-loaded pick spreads the second host onto a different proxy


def test_ban_rotates_to_a_different_proxy() -> None:
    pool = _pool()
    url = "https://www.doordash.com"
    banned = pool.for_url(url)
    pool.mark_banned(url)
    rotated = pool.for_url(url)
    assert rotated is not None
    assert rotated.server != banned.server


def test_exhaustion_returns_none() -> None:
    pool = ProxyPool(parse_proxies("1.1.1.1:8000:u:p"))
    url = "https://x.com"
    assert pool.for_url(url) is not None
    pool.mark_banned(url)
    assert pool.for_url(url) is None  # only proxy banned on this host -> nothing left


def test_empty_pool_returns_none() -> None:
    assert ProxyPool([]).for_url("https://x.com") is None


def test_load_proxy_pool_reads_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_PROXIES", "1.1.1.1:8000:u:p")
    proxy = load_proxy_pool().for_url("https://x.com")
    assert proxy is not None
    assert proxy.server == "http://1.1.1.1:8000"
