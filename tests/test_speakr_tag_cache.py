"""Private storage tests for the Speakr tag catalog cache."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

import meeting_recorder.speakr_tag_cache as tag_cache
from meeting_recorder.speakr_domain import Tag
from meeting_recorder.speakr_tag_cache import SpeakrTagCache, TagCatalogSnapshot


NOW = datetime(2026, 8, 20, 12, 34, 56, tzinfo=timezone.utc)
URL_A = "https://speakr.example"
URL_B = "https://other.example"


def _snapshot(url: str = URL_A, tags: tuple[Tag, ...] = (Tag(7, "Private"),)) -> TagCatalogSnapshot:
    """Create a valid catalog without credentials for cache-only tests."""
    return TagCatalogSnapshot(url, NOW, tags)


def _mode(path: Path) -> int:
    """Return only Unix permission bits for a filesystem assertion."""
    return path.stat().st_mode & 0o777


def test_store_load_permissions_and_secret_free_document() -> None:
    # Store one active catalog and require the documented owner-only modes.
    with tempfile.TemporaryDirectory() as temporary:
        cache = SpeakrTagCache(Path(temporary) / "cache")
        cache.store(_snapshot(tags=(Tag(7, "First"), Tag(3, "Second"))))
        assert cache.load(URL_A) == _snapshot(tags=(Tag(7, "First"), Tag(3, "Second")))
        assert _mode(cache.root) == 0o700
        assert _mode(cache.path) == 0o600
        assert _mode(cache.lock_path) == 0o600

        # Ensure URL identity and tag data are persisted without token material.
        payload = cache.path.read_text(encoding="utf-8")
        assert "token" not in payload.casefold()
        assert json.loads(payload)["origin"] == URL_A


def test_store_replaces_origin_atomically_and_clear_handles_url_change() -> None:
    # Keep exactly one origin and atomically replace its complete document.
    with tempfile.TemporaryDirectory() as temporary:
        cache = SpeakrTagCache(Path(temporary) / "cache")
        cache.store(_snapshot(URL_A))
        before = cache.path.read_bytes()
        original_replace = tag_cache.os.replace
        tag_cache.os.replace = lambda source, destination: (_ for _ in ()).throw(OSError("replace failed"))  # type: ignore[assignment]
        try:
            try:
                cache.store(_snapshot(URL_B))
            except OSError:
                pass
            else:
                raise AssertionError("replace failure was accepted")
        finally:
            tag_cache.os.replace = original_replace
        assert cache.path.read_bytes() == before

        # A successful URL change replaces the prior active catalog, not both.
        cache.store(_snapshot(URL_B, (Tag(8, "Other"),)))
        assert cache.load(URL_A) is None
        assert cache.load(URL_B) == _snapshot(URL_B, (Tag(8, "Other"),))
        cache.clear(URL_A)
        assert cache.load(URL_B) is not None
        cache.clear(URL_B)
        assert not cache.path.exists()


def test_malformed_or_oversized_cache_is_isolated_and_lock_is_exclusive() -> None:
    # Discard invalid local cache data instead of exposing a partial catalog.
    with tempfile.TemporaryDirectory() as temporary:
        cache = SpeakrTagCache(Path(temporary) / "cache")
        cache.root.mkdir(mode=0o700)
        cache.path.write_text('{"token":"secret"}', encoding="utf-8")
        assert cache.load(URL_A) is None
        cache.path.write_bytes(b"x" * (tag_cache.MAX_CATALOG_BYTES + 1))
        assert cache.load(URL_A) is None

        # A held operation lock prevents a concurrent nonblocking cache operation.
        with cache.operation_lock(blocking=True) as acquired:
            assert acquired
            with cache.operation_lock(blocking=False) as concurrent:
                assert not concurrent
