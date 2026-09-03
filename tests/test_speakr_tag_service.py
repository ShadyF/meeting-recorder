"""Headless coordination tests for explicit Speakr tag catalog requests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Event
import tempfile

from meeting_recorder.speakr_domain import Tag
from meeting_recorder.speakr_http import (
    InvalidTagCatalog,
    TagDiscoveryRejected,
    TagDiscoveryUnavailable,
)
from meeting_recorder.speakr_tag_cache import SpeakrTagCache, TagCatalogSnapshot
from meeting_recorder.speakr_tag_service import (
    SpeakrTagService,
    TagCatalogOutcome,
    TagCatalogSource,
)


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
URL_A = "https://one.example"
URL_B = "https://two.example"
TOKEN = "private-token"


class _Transport:
    """Script one result per worker without a network dependency."""

    def __init__(self, results: list[object], gate: Event | None = None) -> None:
        self.results = results
        self.gate = gate
        self.started = Event()
        self.calls: list[tuple[str, str]] = []

    def list_tags(self, instance_url: str, token: str) -> tuple[Tag, ...]:
        # Record request identity while never retaining it in coordinator outcomes.
        self.calls.append((instance_url, token))
        self.started.set()
        if self.gate is not None:
            self.gate.wait(2)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]


class _Dispatcher:
    """Queue idle callbacks so tests control the UI-thread delivery point."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, callback: object, *args: object) -> int:
        # Retain only callback objects and safe outcomes after the worker finishes.
        self.calls.append((callback, *args))
        return len(self.calls)

    def deliver_all(self) -> None:
        """Run each queued idle callback once on this test's main thread."""
        # Copy then clear to mirror GLib removing a callback that returns False.
        calls, self.calls = self.calls, []
        for callback, *args in calls:
            assert callable(callback)
            assert callback(*args) is False


def _wait_for_dispatch(dispatcher: _Dispatcher) -> None:
    """Wait briefly for a worker to schedule exactly one dispatcher callback."""
    # Poll only a bounded number of short waits because the worker owns I/O timeout.
    for _ in range(100):
        if dispatcher.calls:
            return
        Event().wait(0.01)
    raise AssertionError("tag worker did not schedule a callback")


def _service(transport: _Transport, cache: SpeakrTagCache, dispatcher: _Dispatcher) -> SpeakrTagService:
    """Build a deterministic service with a fixed UTC fetched-at timestamp."""
    return SpeakrTagService(transport, cache, dispatcher, clock=lambda: NOW)


def test_fresh_empty_and_stale_catalog_outcomes() -> None:
    # Fresh non-empty and empty responses both replace the active origin catalog.
    with tempfile.TemporaryDirectory() as temporary:
        cache = SpeakrTagCache(Path(temporary) / "cache")
        dispatcher = _Dispatcher()
        service = _service(_Transport([(Tag(2, "Second"),), ()]), cache, dispatcher)
        received: list[TagCatalogOutcome] = []
        service.request(URL_A, TOKEN, received.append)
        _wait_for_dispatch(dispatcher)
        dispatcher.deliver_all()
        assert received[0].source is TagCatalogSource.FRESH
        assert received[0].tags == (Tag(2, "Second"),)
        service.request(URL_A, TOKEN, received.append)
        _wait_for_dispatch(dispatcher)
        dispatcher.deliver_all()
        assert received[1].source is TagCatalogSource.FRESH and received[1].tags == ()
        assert cache.load(URL_A).tags == ()  # type: ignore[union-attr]

        # Seed a catalog and confirm only a transient failure uses it as stale data.
        cache.store(TagCatalogSnapshot(URL_A, NOW, (Tag(3, "Cached"),)))
        stale_dispatcher = _Dispatcher()
        stale_service = _service(_Transport([TagDiscoveryUnavailable()]), cache, stale_dispatcher)
        stale_received: list[TagCatalogOutcome] = []
        stale_service.request(URL_A, TOKEN, stale_received.append)
        _wait_for_dispatch(stale_dispatcher)
        stale_dispatcher.deliver_all()
        assert stale_received[0].source is TagCatalogSource.STALE
        assert stale_received[0].tags == (Tag(3, "Cached"),)
        assert not stale_received[0].unavailable_notice


def test_failure_classes_never_leak_secrets_or_use_cache_when_permanent() -> None:
    # Preserve cache fallback strictly for the typed transient transport failures.
    with tempfile.TemporaryDirectory() as temporary:
        cache = SpeakrTagCache(Path(temporary) / "cache")
        cache.store(TagCatalogSnapshot(URL_A, NOW, (Tag(1, "Cached"),)))
        cases = (
            (TagDiscoveryRejected(429), TagCatalogSource.STALE),
            (TagDiscoveryRejected(503), TagCatalogSource.STALE),
            (TagDiscoveryRejected(408), TagCatalogSource.UNAVAILABLE),
            (TagDiscoveryRejected(401), TagCatalogSource.UNAVAILABLE),
            (TagDiscoveryRejected(403), TagCatalogSource.UNAVAILABLE),
            (TagDiscoveryRejected(422), TagCatalogSource.UNAVAILABLE),
            (InvalidTagCatalog(), TagCatalogSource.UNAVAILABLE),
            (RuntimeError("private body private-token"), TagCatalogSource.UNAVAILABLE),
        )
        for failure, source in cases:
            dispatcher = _Dispatcher()
            service = _service(_Transport([failure]), cache, dispatcher)
            received: list[TagCatalogOutcome] = []
            service.request(URL_A, TOKEN, received.append)
            _wait_for_dispatch(dispatcher)
            dispatcher.deliver_all()
            outcome = received[0]
            assert outcome.source is source
            assert outcome.unavailable_notice is (source is TagCatalogSource.UNAVAILABLE)
            assert "private" not in repr(outcome)

        # Switching origins removes old state, and disabling removes the active state.
        service = _service(_Transport([]), cache, _Dispatcher())
        assert service.activate(URL_B) == URL_B and cache.load(URL_A) is None
        cache.store(TagCatalogSnapshot(URL_B, NOW, (Tag(4, "Other"),)))
        assert service.activate(None) is None and cache.load(URL_B) is None


def test_cancellation_marshalling_generations_and_cache_update() -> None:
    # Cancel before fetch completion and still retain its valid catalog for later calls.
    with tempfile.TemporaryDirectory() as temporary:
        cache = SpeakrTagCache(Path(temporary) / "cache")
        gate = Event()
        dispatcher = _Dispatcher()
        transport = _Transport([(Tag(7, "Saved"),), (Tag(8, "Newer"),)], gate)
        service = _service(transport, cache, dispatcher)
        received: list[TagCatalogOutcome] = []
        first = service.request(URL_A, TOKEN, received.append)
        assert transport.started.wait(1)
        first.cancel()
        gate.set()
        _wait_for_dispatch(dispatcher)
        dispatcher.deliver_all()
        assert received == [] and cache.load(URL_A).tags == (Tag(7, "Saved"),)  # type: ignore[union-attr]

        # Two explicit calls receive distinct generations and one callback each.
        second = service.request(URL_A, TOKEN, received.append)
        assert second.generation > first.generation
        _wait_for_dispatch(dispatcher)
        dispatcher.deliver_all()
        dispatcher.deliver_all()
        assert len(received) == 1 and received[0].tags == (Tag(8, "Newer"),)


def test_cancel_after_fetch_and_bounded_shutdown() -> None:
    # Suppress queued callback after fetch while allowing that response to update cache.
    with tempfile.TemporaryDirectory() as temporary:
        cache = SpeakrTagCache(Path(temporary) / "cache")
        dispatcher = _Dispatcher()
        service = _service(_Transport([(Tag(5, "Queued"),)]), cache, dispatcher)
        received: list[TagCatalogOutcome] = []
        request = service.request(URL_A, TOKEN, received.append)
        _wait_for_dispatch(dispatcher)
        request.cancel()
        dispatcher.deliver_all()
        assert received == [] and cache.load(URL_A).tags == (Tag(5, "Queued"),)  # type: ignore[union-attr]

        # Bound shutdown while an in-flight request remains under transport ownership.
        gate = Event()
        blocked = _service(_Transport([(Tag(6, "Late"),)], gate), cache, _Dispatcher())
        blocked.request(URL_A, TOKEN, lambda _outcome: None)
        assert not blocked.shutdown(0)
        gate.set()
        assert blocked.shutdown(1)
