"""Shared HTTP plumbing for every external source.

Everything this agent reads is an undocumented or best-effort endpoint with no
SLA. So the base client assumes failure is normal: it rate-limits itself, caches
aggressively, retries with backoff, and -- most importantly -- distinguishes
"this source is down" from "this source says no". The rest of the system needs
that distinction to decide between DATA UNAVAILABLE and a real answer.

Note the espn-api library ships no rate-limit handling of its own (verified by
reading its request layer), so throttling is our responsibility, not its.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from ff.db.store import content_hash
from ff.logging_setup import get_logger, scrub

log = get_logger(__name__)

# A browser user-agent, because site.api.espn.com 403s unfamiliar clients (the
# fantasy lm-api endpoints don't care). Same public endpoints a browser hits.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class SourceError(RuntimeError):
    """Base for anything that went wrong talking to an external source."""


class SourceUnavailable(SourceError):
    """Transient: network, 5xx, timeout, rate limit. Cached data may be used."""


class SourceAuthError(SourceError):
    """Credentials missing, wrong, or expired. Needs a human, not a retry."""


class SourceNotFound(SourceError):
    """The thing we asked for does not exist (bad league id, retired player)."""


@dataclass
class Response:
    data: Any
    from_cache: bool
    fetched_at: float

    @property
    def age_seconds(self) -> float:
        return time.time() - self.fetched_at

    def is_stale(self, max_age: float) -> bool:
        return self.age_seconds > max_age


class HttpClient:
    """Rate-limited, caching, retrying JSON client."""

    def __init__(
        self,
        cache_dir: Path | str,
        *,
        min_interval: float = 0.6,
        timeout: float = 20.0,
        max_retries: int = 3,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self._last_call: dict[str, float] = {}
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    # -- throttle ----------------------------------------------------------

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last_call.get(host)
        if last is not None:
            wait = self.min_interval - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_call[host] = time.time()

    # -- cache -------------------------------------------------------------

    def _cache_path(self, url: str, params: dict[str, Any] | None) -> Path:
        return self.cache_dir / f"{content_hash([url, params or {}])}.json"

    def _read_cache(self, path: Path, ttl: float) -> Response | None:
        if not path.exists():
            return None
        try:
            blob = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        fetched_at = blob.get("fetched_at", 0)
        if ttl >= 0 and time.time() - fetched_at > ttl:
            return None
        return Response(blob.get("data"), from_cache=True, fetched_at=fetched_at)

    def _write_cache(self, path: Path, data: Any) -> None:
        try:
            path.write_text(json.dumps({"fetched_at": time.time(), "data": data}))
        except OSError as exc:  # a full disk must not kill the run
            log.warning("cache write failed: %s", exc)

    # -- fetch -------------------------------------------------------------

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        ttl: float = 300.0,
        allow_stale_on_failure: bool = True,
    ) -> Response:
        """GET and parse JSON.

        On failure, falls back to expired cache when available rather than
        exploding -- a stale roster clearly marked as stale beats no roster.
        """
        cache_path = self._cache_path(url, params)
        cached = self._read_cache(cache_path, ttl)
        if cached is not None:
            return cached

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle(url)
            try:
                resp = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    cookies=cookies,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = SourceUnavailable(f"{url}: {exc}")
                self._backoff(attempt)
                continue

            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise SourceError(f"{url}: response was not JSON ({exc})") from exc
                self._write_cache(cache_path, data)
                return Response(data, from_cache=False, fetched_at=time.time())

            if resp.status_code in (401, 403):
                # Not retryable and not maskable -- a human has to fix it.
                raise SourceAuthError(
                    f"{urlparse(url).netloc} returned {resp.status_code}. "
                    "Credentials are missing, wrong, or expired."
                )
            if resp.status_code == 404:
                raise SourceNotFound(f"{url} returned 404")

            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if (retry_after or "").isdigit() else None
                last_error = SourceUnavailable(f"{url}: HTTP {resp.status_code}")
                self._backoff(attempt, override=delay)
                continue

            raise SourceError(f"{url}: unexpected HTTP {resp.status_code}")

        # Everything failed. Prefer stale data over nothing, but say so.
        if allow_stale_on_failure:
            stale = self._read_cache(cache_path, ttl=-1)
            if stale is not None:
                log.warning(
                    "%s unreachable; serving cached data %.0fs old",
                    urlparse(url).netloc,
                    stale.age_seconds,
                )
                return stale
        raise last_error or SourceUnavailable(f"{url}: exhausted retries")

    def _backoff(self, attempt: int, override: float | None = None) -> None:
        delay = override if override is not None else (2**attempt) + random.uniform(0, 0.5)
        log.debug("backing off %.1fs (attempt %d)", delay, attempt + 1)
        time.sleep(min(delay, 30.0))

    def close(self) -> None:
        self.session.close()


def safe_url(url: str) -> str:
    """A URL fit for logging -- query string stripped, secrets scrubbed."""
    parsed = urlparse(url)
    return scrub(f"{parsed.scheme}://{parsed.netloc}{parsed.path}")
