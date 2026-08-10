"""Residential proxy pool with per-domain sticky assignment.

The owner has a handful of residential proxies. We pin one proxy to a website and keep using it
until it's banned, then rotate to another — sticky assignment minimises the "new IP" churn that
trips bot-detection and session checks. Proxies are loaded from the BROWSER_PROXIES env var (the
lines a Webshare "download list" URL returns: `host:port:username:password`, one per line), so the
provider API token never lives in this app — you curl the URL once and paste the lines into .env.

Ban detection is the caller's job: when a proxy gets blocked on a site, call `mark_banned(host)` and
the next assignment for that site rotates. There is no reliable automatic "you are banned" signal.
"""

from urllib.parse import urlsplit

from baski.env import get_env
from pydantic import BaseModel


class ProxyServer(BaseModel):
    """One proxy in Playwright's context-proxy shape (`server`/`username`/`password`)."""

    server: str
    username: str
    password: str


def parse_proxies(text: str) -> list[ProxyServer]:
    """Parse Webshare `host:port:username:password` proxies into a list; ignore blanks/comments.

    Accepts newline- or comma-separated entries — the Webshare download is newline-separated, but a
    single-line `.env` value (the Makefile can't carry multiline) joins them with commas.
    """
    proxies: list[ProxyServer] = []
    for raw in text.replace(",", "\n").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        host, port, username, password = line.split(":", 3)
        proxies.append(ProxyServer(server=f"http://{host}:{port}", username=username, password=password))
    return proxies


class ProxyPool:
    """Pins one proxy per site (host), rotating only when the current one is marked banned."""

    def __init__(self, proxies: list[ProxyServer]) -> None:
        """Hold the available proxies; assignment + ban state build up as sites are visited."""
        self._proxies = proxies
        self._assigned: dict[str, int] = {}  # host -> index into _proxies
        self._banned: dict[str, set[int]] = {}  # host -> proxy indices banned on that host

    def for_url(self, url: str) -> ProxyServer | None:
        """The proxy pinned to this URL's host, assigning the least-loaded non-banned one on first visit."""
        host = urlsplit(url).hostname
        if not self._proxies or not host:
            return None
        if host not in self._assigned:
            index = self._pick(host)
            if index is None:
                return None
            self._assigned[host] = index
        return self._proxies[self._assigned[host]]

    def mark_banned(self, url: str) -> None:
        """Record that the current proxy is banned on this URL's host, so the next lookup rotates."""
        host = urlsplit(url).hostname
        if not host or host not in self._assigned:
            return
        self._banned.setdefault(host, set()).add(self._assigned.pop(host))

    def _pick(self, host: str) -> int | None:
        """Least-loaded proxy not already banned on this host (None when all are burned)."""
        banned = self._banned.get(host, set())
        candidates = [i for i in range(len(self._proxies)) if i not in banned]
        if not candidates:
            return None
        load = [0] * len(self._proxies)
        for index in self._assigned.values():
            load[index] += 1
        return min(candidates, key=lambda i: load[i])


def load_proxy_pool() -> ProxyPool:
    """Build the pool from the required BROWSER_PROXIES env (Webshare host:port:user:pass lines)."""
    return ProxyPool(parse_proxies(str(get_env("BROWSER_PROXIES"))))
