"""Headless asynchronous coordination for one explicit Speakr tag refresh."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from threading import Lock, Thread, current_thread
import time
from typing import Callable, Protocol

from .speakr_domain import Tag, normalize_speakr_url
from .speakr_http import (
    InvalidTagCatalog,
    TagDiscoveryRejected,
    TagDiscoveryUnavailable,
)
from .speakr_tag_cache import SpeakrTagCache, TagCatalogSnapshot


class TagCatalogSource(str, Enum):
    """Identify whether an outcome came from Speakr or the local cache."""

    FRESH = "fresh"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class TagCatalogTransport(Protocol):
    """Narrow transport boundary required by explicit catalog discovery."""

    def list_tags(self, instance_url: str, token: str) -> tuple[Tag, ...]:
        ...


@dataclass(frozen=True)
class TagCatalogOutcome:
    """The complete safe result delivered for one requested tag catalog."""

    tags: tuple[Tag, ...]
    source: TagCatalogSource
    fetched_at_utc: datetime | None
    unavailable_notice: bool

    def __post_init__(self) -> None:
        # Keep callback consumers from receiving a partial or ambiguous result.
        if not isinstance(self.tags, tuple) or any(not isinstance(tag, Tag) for tag in self.tags):
            raise ValueError("tag catalog outcome is invalid")
        if not isinstance(self.source, TagCatalogSource):
            raise ValueError("tag catalog source is invalid")
        if self.fetched_at_utc is not None and (
            self.fetched_at_utc.tzinfo is None
            or self.fetched_at_utc.utcoffset() != timezone.utc.utcoffset(None)
        ):
            raise ValueError("tag catalog timestamp is invalid")
        if not isinstance(self.unavailable_notice, bool):
            raise ValueError("tag catalog notice is invalid")


class TagCatalogRequest:
    """Cancellation handle for one asynchronous tag catalog request."""

    def __init__(self, generation: int) -> None:
        self.generation = generation
        self._cancelled = False
        self._delivered = False
        self._lock = Lock()

    def cancel(self) -> None:
        """Suppress a pending or queued callback without interrupting cache work."""
        # Leave the worker running so a valid result can seed the next request.
        with self._lock:
            self._cancelled = True

    def _claim_delivery(self) -> bool:
        """Claim the one permitted callback when this request remains active."""
        # Serialize cancellation and dispatch execution into one exact-once decision.
        with self._lock:
            if self._cancelled or self._delivered:
                return False
            self._delivered = True
            return True


class SpeakrTagService:
    """Run caller-requested tag discovery without owning UI or retry policy."""

    def __init__(
        self,
        transport: TagCatalogTransport,
        cache: SpeakrTagCache,
        dispatcher: Callable[..., object],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        # Validate injected boundaries before a worker can observe them.
        if not callable(getattr(transport, "list_tags", None)) or not isinstance(cache, SpeakrTagCache) or not callable(dispatcher):
            raise ValueError("tag catalog service dependency is invalid")
        if clock is not None and not callable(clock):
            raise ValueError("tag catalog clock is invalid")
        self._transport = transport
        self._cache = cache
        self._dispatcher = dispatcher
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = Lock()
        self._generation = 0
        self._closed = False
        self._workers: set[Thread] = set()
        self._requests: set[TagCatalogRequest] = set()

    def activate(self, instance_url: str | None) -> str | None:
        """Set the active origin and delete a catalog from a prior configuration."""
        # Delegate durable origin cleanup to the cache before a caller starts work.
        return self._cache.activate(instance_url)

    def request(
        self,
        instance_url: str,
        token: str,
        callback: Callable[[TagCatalogOutcome], object],
    ) -> TagCatalogRequest:
        """Start one explicit asynchronous fetch and return its cancellation handle."""
        # Validate the origin locally, then delete data from any previous origin.
        origin = normalize_speakr_url(instance_url)
        if not isinstance(token, str) or not token or not callable(callback):
            raise ValueError("tag catalog request is invalid")
        self._cache.activate(origin)

        # Register a single daemon worker unless shutdown has already begun.
        with self._lock:
            if self._closed:
                raise RuntimeError("tag catalog service is shut down")
            self._generation += 1
            request = TagCatalogRequest(self._generation)
            worker = Thread(
                target=self._run_request,
                args=(request, origin, token, callback),
                daemon=True,
            )
            self._workers.add(worker)
            self._requests.add(request)
        worker.start()
        return request

    def shutdown(self, timeout_seconds: float = 5) -> bool:
        """Cancel queued UI delivery and join workers within one caller-provided bound."""
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds < 0:
            raise ValueError("tag catalog shutdown timeout is invalid")

        # Freeze new requests and take a stable worker snapshot before joining.
        with self._lock:
            self._closed = True
            workers = tuple(self._workers)
            requests = tuple(self._requests)

        # Suppress queued callbacks while still allowing valid cache writes to finish.
        for request in requests:
            request.cancel()
        deadline = time.monotonic() + float(timeout_seconds)

        # Share one total deadline across all workers rather than extending shutdown.
        for worker in workers:
            remaining = max(0.0, deadline - time.monotonic())
            worker.join(remaining)
        return all(not worker.is_alive() for worker in workers)

    def _run_request(
        self,
        request: TagCatalogRequest,
        origin: str,
        token: str,
        callback: Callable[[TagCatalogOutcome], object],
    ) -> None:
        """Fetch one catalog, optionally fall back, and marshal one safe outcome."""
        try:
            # A complete valid response is authoritative even when UI delivery was cancelled.
            tags = self._transport.list_tags(origin, token)
            fetched_at = self._utc_now()
            try:
                self._cache.store(TagCatalogSnapshot(origin, fetched_at, tags))
            except (OSError, ValueError):
                pass
            outcome = TagCatalogOutcome(tags, TagCatalogSource.FRESH, fetched_at, False)
        except TagDiscoveryUnavailable:
            outcome = self._fallback(origin)
        except TagDiscoveryRejected as exc:
            outcome = self._fallback(origin) if exc.status == 429 or exc.status >= 500 else self._unavailable()
        except InvalidTagCatalog:
            outcome = self._unavailable()
        except Exception:
            outcome = self._unavailable()

        # Always release worker tracking before scheduling an optional UI callback.
        with self._lock:
            self._workers.discard(current_thread())

        # Marshal callback execution through the UI-compatible dispatcher only.
        try:
            self._dispatcher(self._deliver, request, callback, outcome)
        except Exception:
            pass

    def _fallback(self, origin: str) -> TagCatalogOutcome:
        """Use only a matching cached catalog after an explicitly transient failure."""
        # The cache enforces origin matching and returns no partial malformed state.
        snapshot = self._cache.load(origin)
        if snapshot is not None:
            return TagCatalogOutcome(snapshot.tags, TagCatalogSource.STALE, snapshot.fetched_at_utc, False)
        return self._unavailable()

    def _unavailable(self) -> TagCatalogOutcome:
        """Build the secret-free empty result used when cache fallback is prohibited."""
        return TagCatalogOutcome((), TagCatalogSource.UNAVAILABLE, None, True)

    def _utc_now(self) -> datetime:
        """Validate the injected clock before persisting a fetched-at timestamp."""
        # Treat a broken test or runtime clock as a failed cache write, not a bad callback.
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError("tag catalog clock is invalid")
        return value

    def _deliver(
        self,
        request: TagCatalogRequest,
        callback: Callable[[TagCatalogOutcome], object],
        outcome: TagCatalogOutcome,
    ) -> bool:
        """Deliver one active request on the dispatcher's thread and remove its source."""
        # Check cancellation at execution time so queued stale UI work stays suppressed.
        if request._claim_delivery():
            try:
                callback(outcome)
            except Exception:
                pass
        # Release completed handles after delivery or cancellation has been observed.
        with self._lock:
            self._requests.discard(request)
        return False


__all__ = [
    "SpeakrTagService",
    "TagCatalogOutcome",
    "TagCatalogRequest",
    "TagCatalogSource",
    "TagCatalogTransport",
]
