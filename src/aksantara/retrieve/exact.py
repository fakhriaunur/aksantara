"""Exact lookup for Aksantara retrieval — spec + legacy compat.

Provides both the spec API (ExactLookup + InMemoryExactIndex with lower-cased
lema index) and the legacy InMemoryEntryStore / retrieve_exact used by
earlier slice demos.
"""

from __future__ import annotations

from typing import Any

from aksantara.domain.models import KBBIEntry

__all__ = ["ExactLookup", "InMemoryEntryStore", "InMemoryExactIndex", "retrieve_exact"]


def _is_active_official_entry(entry: KBBIEntry) -> bool:
    """Keep public retrieval helpers inside the active official boundary."""
    return entry.status == "active" and entry.source.source_kind in {
        "official-live",
        "official-snapshot",
    }


class InMemoryExactIndex:
    """Minimal in-memory exact index for tests and local smoke (spec name)."""

    def __init__(self, entries: list[KBBIEntry] | None = None) -> None:
        self._by_lema: dict[str, KBBIEntry] = {}
        self._by_id: dict[str, KBBIEntry] = {}
        if entries:
            for e in entries:
                self.add(e)

    def add(self, entry: KBBIEntry) -> None:
        self._by_lema[entry.lema.lower()] = entry
        self._by_id[entry.id] = entry

    def put(self, entry: KBBIEntry) -> None:
        self.add(entry)

    def get_by_lema(self, lema: str) -> KBBIEntry | None:
        return self._by_lema.get(lema.strip().lower())

    def get_by_id(self, entry_id: str) -> KBBIEntry | None:
        return self._by_id.get(entry_id)

    def get_by_nonstandard(self, word: str) -> KBBIEntry | None:
        lowered = word.strip().lower()
        for e in self._by_id.values():
            if _is_active_official_entry(e) and any(
                v.lower() == lowered for v in e.bentuk_tidak_baku
            ):
                return e
        return None

    def all_entries(self) -> list[KBBIEntry]:
        return list(self._by_lema.values())

    def clear(self) -> None:
        self._by_lema.clear()
        self._by_id.clear()


# Legacy alias — same store, different name for old tests
class InMemoryEntryStore(InMemoryExactIndex):
    """Legacy alias for InMemoryExactIndex."""

    def put(self, entry: KBBIEntry) -> None:  # type: ignore[override]
        super().add(entry)
        # also index nonstandard forms with sentinel for legacy nonstandard lookup
        for nb in entry.bentuk_tidak_baku:
            self._by_lema[f"__nonstandard__{nb.lower()}"] = entry

    def get_by_lema(self, lema: str) -> KBBIEntry | None:  # type: ignore[override]
        # Check nonstandard sentinel first
        sentinel = self._by_lema.get(f"__nonstandard__{lema.lower()}")
        if sentinel is not None and lema.lower().startswith("__nonstandard__") is False:
            # Only return sentinel if direct miss; prefer direct hit
            direct = self._by_lema.get(lema.lower())
            if direct is not None:
                return direct
            # Do not auto-normalize nonstandard via exact lookup — nonstandard
            # endpoint handles that separately. So ignore sentinel here.
            pass
        return super().get_by_lema(lema)

    def get_by_nonstandard(self, word: str) -> KBBIEntry | None:  # type: ignore[override]
        # Check sentinel first, then scan
        hit = self._by_lema.get(f"__nonstandard__{word.lower()}")
        if hit is not None:
            return hit
        return super().get_by_nonstandard(word)


def retrieve_exact(
    query: str, store: InMemoryEntryStore | InMemoryExactIndex
) -> KBBIEntry | None:
    """Exact lema lookup (case-insensitive) — legacy helper."""
    entry = store.get_by_lema(query.strip())  # type: ignore[arg-type]
    return entry if entry is not None and _is_active_official_entry(entry) else None


class ExactLookup:
    """Exact retrieval adapter (spec shape) with Firestore + in-memory fallback."""

    def __init__(
        self,
        *,
        index: InMemoryExactIndex | InMemoryEntryStore | None = None,
        firestore_client: Any | None = None,
        collection: str = "entries",
    ) -> None:
        self._index = index or InMemoryExactIndex()
        self._client = firestore_client
        self._collection = collection

    def lookup(self, lema: str) -> KBBIEntry | None:
        cleaned = lema.strip().lower()
        if not cleaned:
            return None
        if self._client is not None:
            try:
                doc = self._client.collection(self._collection).document(cleaned).get()
                exists = getattr(doc, "exists", True)
                if callable(exists):
                    has = doc.exists  # type: ignore[attr-defined]
                else:
                    has = bool(exists)
                if has:
                    data = doc.to_dict() if hasattr(doc, "to_dict") else {}
                    if isinstance(data, dict) and data:
                        try:
                            entry = KBBIEntry.model_validate(data)
                            if _is_active_official_entry(entry):
                                return entry
                        except Exception:
                            pass
            except Exception:
                pass
        indexed_entry = self._index.get_by_lema(cleaned)  # type: ignore[attr-defined]
        if indexed_entry is None:
            return None
        return indexed_entry if _is_active_official_entry(indexed_entry) else None

    def __call__(self, lema: str) -> KBBIEntry | None:
        return self.lookup(lema)
