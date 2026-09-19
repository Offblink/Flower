"""What the server will give: how big, whether it does ranges, what to call it.

Asked once, and believed only where it can be acted on. The one-byte range probe
is the load-bearing piece: a 206 both proves ranges work and carries the total,
so a host that lies about HEAD still gets split into windows.
"""

from __future__ import annotations

import re
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from .net import Client, NetError, as_int, content_range_total

FALLBACK_NAME = "download.bin"
FORBIDDEN = '<>:"/\\|?*'
_EXTENDED_NAME = re.compile(r"filename\*\s*=\s*([\w-]*)'[^']*'([^;]+)", re.IGNORECASE)
_PLAIN_NAME = re.compile(r'filename\s*=\s*"?([^";]+)"?', re.IGNORECASE)


@dataclass(frozen=True)
class Probe:
    """A URL, understood: where the bytes come from, how many, and their name."""

    url: str
    size: int | None
    ranges: bool
    filename: str


def probe(client: Client, url: str) -> Probe:
    """Settle size, range support and file name, the cheap way first.

    HEAD costs nothing when a host answers it honestly, so it goes first. Plenty
    do not — GitHub's asset chain redirects to a signed URL that only a GET is
    good for — and that is not a verdict, so a useless HEAD is followed by a
    `Range: bytes=0-0` GET. Hosts that omit `Accept-Ranges` but honour ranges
    are the common case this catches.
    """
    headers, final = _head(client, url)
    size, ranges = _length_and_ranges(headers)
    if size is None or not ranges:
        headers, final, size, ranges = _range_peek(client, url, headers, final, size)
    return Probe(final, size, ranges, filename_from(headers, final))


def _head(client: Client, url: str) -> tuple[object, str]:
    """The HEAD answer, or empty headers when the host refused or answered badly."""
    try:
        with client.open(url, method="HEAD") as response:
            return response.headers, response.geturl()
    except (NetError, urllib.error.HTTPError):
        return {}, url


def _range_peek(
    client: Client, url: str, headers: object, final: str, size: int | None
) -> tuple[object, str, int | None, bool]:
    """The one-byte GET: reads the total off `Content-Range` when the answer is 206.

    A 200 here means the host ignored the range and is sending the whole file;
    the body is never read (the response is closed on the spot), it only speaks
    for `Content-Length`.
    """
    with client.open(url, {"Range": "bytes=0-0"}) as response:
        headers, final = response.headers, response.geturl()
        if getattr(response, "status", 200) == 206:
            total = content_range_total(headers.get("Content-Range"))
            if total is not None:
                return headers, final, total, True
        elif size is None:
            size = _length_and_ranges(headers)[0]
    return headers, final, size, False


def _length_and_ranges(headers) -> tuple[int | None, bool]:
    """Size and range support as the headers state them; 0 length means "unknown"."""
    size = as_int(headers.get("Content-Length"))
    if size == 0:
        size = None
    return size, (headers.get("Accept-Ranges") or "").strip().lower() == "bytes"


def filename_from(headers, url: str) -> str:
    """`Content-Disposition` first (RFC 5987 form included), then the URL's last segment."""
    disposition = headers.get("Content-Disposition") or ""
    raw = _from_disposition(disposition) or unquote(Path(urlparse(url).path).name)
    return safe_name(raw) or FALLBACK_NAME


def _from_disposition(value: str) -> str:
    extended = _EXTENDED_NAME.search(value)
    if extended:
        charset = extended.group(1) or "utf-8"
        encoded = extended.group(2).strip()
        try:
            return unquote(encoded, encoding=charset, errors="replace")
        except LookupError:  # a charset nobody knows; the bytes are still percent-encoded
            return unquote(encoded)
    plain = _PLAIN_NAME.search(value)
    return plain.group(1).strip() if plain else ""


def safe_name(name: str) -> str:
    """The last path segment only, minus what Windows refuses and control characters."""
    base = Path(name.replace("\\", "/")).name
    kept = "".join(char for char in base if char not in FORBIDDEN and char >= " ")
    return kept.strip(" .")
