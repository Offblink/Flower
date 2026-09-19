"""One way out to the network, carrying the proxy the user actually chose.

`urllib` runs `proxy_bypass` against every host before it uses a proxy, and it
does that even for an explicitly supplied `ProxyHandler` — so a proxy the user
switched on can silently do nothing and the request goes direct. Flower forces
that check off for the duration of every request it makes. With the proxy off it
installs an empty handler rather than asking the environment, which is the other
half of the same rule: the route is what the interface says it is.
"""

from __future__ import annotations

import contextlib
import urllib.error
import urllib.request
from collections.abc import Iterator

USER_AGENT = "Flower/0.1"
DEFAULT_TIMEOUT_S = 30.0


class NetError(OSError):
    """A request that could not be made: no route, no proxy, DNS, refused."""


@contextlib.contextmanager
def _bypass_forced_off() -> Iterator[None]:
    original = urllib.request.proxy_bypass
    urllib.request.proxy_bypass = lambda _host: False
    try:
        yield
    finally:
        urllib.request.proxy_bypass = original


class Client:
    """Requests through one proxy setting; redirects are followed as usual."""

    def __init__(self, proxy: str | None = None, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self.proxy = proxy
        self.timeout = timeout
        proxies = {"http": proxy, "https": proxy} if proxy else {}
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))

    def open(self, url: str, headers: dict | None = None, method: str | None = None):
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, **(headers or {})}, method=method
        )
        try:
            with _bypass_forced_off():
                return self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError:
            raise  # a status code is an answer, not a transport failure
        except urllib.error.URLError as exc:
            raise NetError(f"{url}: {exc.reason}") from exc


def as_int(value: str | None) -> int | None:
    """A header number, or None when the server sent something unhelpful."""
    if value is None:
        return None
    text = value.strip()
    return int(text) if text.isdigit() else None


def content_range_total(value: str | None) -> int | None:
    """The total in `bytes 0-0/12345`; None when the server phrased it otherwise."""
    if not value or "/" not in value:
        return None
    return as_int(value.rsplit("/", 1)[1])
