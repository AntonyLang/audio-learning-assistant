"""Bounded, SSRF-aware page fetcher (doc §4.1).

Only HTTP(S) is accepted, every redirect hop is re-validated, and the response is capped
before it is materialised. Page content is *data*: nothing extracted from it can change
tool permissions or the research budget.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpx
import trafilatura

from app.adapters.base import AdapterError, FetchedPage
from app.config import Settings
from app.services.textproc import normalize_text

_ALLOWED_SCHEMES = {"http", "https"}


class UrlFetcher:
    def __init__(self, settings: Settings, *, fixtures_dir: Path | None = None) -> None:
        self.settings = settings
        self.fixtures_dir = fixtures_dir

    async def fetch(self, url: str) -> FetchedPage:
        if url.startswith("local://"):
            return await self._fetch_fixture(url)
        self._validate_url(url)
        return await self._fetch_http(url)

    # -- local offline web -------------------------------------------------
    async def _fetch_fixture(self, url: str) -> FetchedPage:
        if self.fixtures_dir is None:
            raise AdapterError("fixture_web_disabled", url)
        slug = url.removeprefix("local://")
        path = (self.fixtures_dir / f"{slug}.md").resolve()
        if not str(path).startswith(str(self.fixtures_dir.resolve())) or not path.exists():
            raise AdapterError("fixture_missing", slug)
        raw = await asyncio.to_thread(path.read_text, "utf-8")
        title = slug
        for line in raw.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        return FetchedPage(
            url=url,
            status=200,
            content_type="text/markdown",
            body=raw.encode("utf-8"),
            text=normalize_text(raw),
            title=title,
        )

    # -- guarded HTTP ------------------------------------------------------
    def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in _ALLOWED_SCHEMES:
            raise AdapterError("fetch_scheme_rejected", parsed.scheme)
        if not parsed.hostname:
            raise AdapterError("fetch_host_missing", url)
        if not self.settings.allow_private_hosts:
            self._reject_private_host(parsed.hostname)

    def _reject_private_host(self, host: str) -> None:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise AdapterError("fetch_dns_failed", host) from exc
        for info in infos:
            address = ipaddress.ip_address(info[4][0])
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
                or address.is_unspecified
            ):
                raise AdapterError("fetch_private_address", f"{host} -> {address}")

    async def _fetch_http(self, url: str) -> FetchedPage:
        headers = {"User-Agent": "audio-learning-assistant/0.1 (+personal use)"}
        limit = max(self.settings.max_html_bytes, self.settings.max_pdf_bytes)
        async with httpx.AsyncClient(
            timeout=self.settings.fetch_timeout_seconds,
            follow_redirects=False,
            headers=headers,
        ) as client:
            current = url
            for _ in range(self.settings.max_redirects + 1):
                try:
                    async with client.stream("GET", current) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            target = response.headers.get("location", "")
                            current = str(response.url.join(target))
                            self._validate_url(current)
                            continue
                        if response.status_code >= 400:
                            raise AdapterError(
                                "fetch_http_error",
                                f"{response.status_code} {current}",
                                retryable=response.status_code >= 500,
                            )
                        declared = int(response.headers.get("content-length") or 0)
                        if declared and declared > limit:
                            raise AdapterError("fetch_too_large", f"{declared} bytes")
                        buffer = bytearray()
                        async for chunk in response.aiter_bytes():
                            buffer.extend(chunk)
                            if len(buffer) > limit:
                                raise AdapterError("fetch_too_large", f"> {limit} bytes")
                        content_type = response.headers.get("content-type", "")
                        body = bytes(buffer)
                        text, title = _to_text(body, content_type)
                        return FetchedPage(
                            url=current,
                            status=response.status_code,
                            content_type=content_type,
                            body=body,
                            text=text,
                            title=title,
                        )
                except httpx.TimeoutException as exc:
                    raise AdapterError("fetch_timeout", current, retryable=True) from exc
                except httpx.HTTPError as exc:
                    raise AdapterError("fetch_failed", str(exc), retryable=True) from exc
        raise AdapterError("fetch_too_many_redirects", url)


def _to_text(body: bytes, content_type: str) -> tuple[str, str]:
    if "pdf" in content_type:
        return "", ""
    if "html" in content_type or body.lstrip().startswith(b"<"):
        html = body.decode("utf-8", errors="replace")
        extracted = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
        title = ""
        metadata = trafilatura.extract_metadata(html)
        if metadata is not None and metadata.title:
            title = metadata.title
        return normalize_text(extracted), title
    return normalize_text(body.decode("utf-8", errors="replace")), ""
