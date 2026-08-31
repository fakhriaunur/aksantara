"""Prefix retrieval for Aksantara.

Second in the ``exact → prefix → semantic`` cascade. Uses lexicographic
prefix scan over canonical lema keys (Firestore range query when available,
in-memory linear scan otherwise). Results are capped and ordered
lexicographically by lema for determinism.
"""

from __future__ import annotations

from typing import Any

from aksantara.domain.models import KBBIEntry

__all__ = ["PrefixLookup", "retrieve_prefix"]

_DEFAULT_LIMIT: int = 20


def retrieve_prefix(
    query: str, store: Any, limit: int = _DEFAULT_LIMIT
) -> list[KBBIEntry]:
    """Back-compat wrapper for simple InMemoryEntryStore (used by tests)."""
    # store is InMemoryEntryStore
    try:
        from aksantara.retrieve.exact import InMemoryEntryStore as _Store

        if isinstance(store, _Store):
            q = query.strip().lower()
            if not q:
                return []
            results: list[KBBIEntry] = []
            for entry in store.all_entries():
                if entry.status != "active" or entry.source.source_kind not in {
                    "official-live",
                    "official-snapshot",
                }:
                    continue
                if entry.lema.lower().startswith(q):
                    results.append(entry)
                for nb in entry.bentuk_tidak_baku:
                    if nb.lower().startswith(q) and entry not in results:
                        results.append(entry)
                if len(results) >= limit:
                    break
            return results[:limit]
    except Exception:
        pass
    # fallback to class-based lookup
    lookup = PrefixLookup(index=store)
    return lookup.lookup(query, limit=limit)


class PrefixLookup:
    """Prefix resolver.

    Args:
        index: in-memory index (tests / fallback).
        firestore_client: optional Firestore client for native range query.
        collection: entries collection name.
    """

    def __init__(
        self,
        *,
        index: Any | None = None,
        firestore_client: Any | None = None,
        collection: str = "entries",
    ) -> None:
        self._index = index
        self._client = firestore_client
        self._collection = collection

    def lookup(self, prefix: str, *, limit: int = _DEFAULT_LIMIT) -> list[KBBIEntry]:
        """Return entries whose lema starts with ``prefix`` (case-insensitive).

        An empty prefix returns an empty list (no broad dump).

        Args:
            prefix: query prefix.
            limit: maximum results (capped at 50).

        Returns:
            Sorted, capped list of matching canonical entries.
        """
        cleaned = prefix.strip().lower()
        if not cleaned:
            return []
        cap = min(max(1, limit), 50)

        # Try Firestore range query when client present.
        if self._client is not None:
            try:
                col = self._client.collection(self._collection)
                # Firestore range: where lema >= prefix and lema < prefix + '\uf8ff'
                # We try the FieldFilter path when SDK supports it, otherwise
                # legacy positional args.
                try:
                    from google.cloud.firestore import (
                        FieldFilter,  # type: ignore[import-untyped]
                    )

                    q = (
                        col.where(filter=FieldFilter("lema", ">=", cleaned))
                        .where(filter=FieldFilter("lema", "<", cleaned + "\uf8ff"))
                        .order_by("lema")
                        .limit(cap)
                    )
                except Exception:
                    q = (
                        col.where("lema", ">=", cleaned)
                        .where("lema", "<", cleaned + "\uf8ff")
                        .order_by("lema")
                        .limit(cap)
                    )  # type: ignore[call-arg]

                snaps = list(q.stream() if hasattr(q, "stream") else q.get())  # type: ignore[attr-defined]
                out: list[KBBIEntry] = []
                for snap in snaps:
                    data = snap.to_dict() if hasattr(snap, "to_dict") else {}
                    if not isinstance(data, dict) or not data:
                        continue
                    try:
                        entry = KBBIEntry.model_validate(data)
                        if entry.status == "active" and entry.source.source_kind in {
                            "official-live",
                            "official-snapshot",
                        }:
                            out.append(entry)
                    except Exception:
                        continue
                if out:
                    return out[:cap]
            except Exception:
                pass

        # In-memory fallback — linear scan over lema keys.
        if self._index is None:
            return []
        # Support both InMemoryExactIndex and plain dict / list.
        entries: list[KBBIEntry] = []
        if hasattr(self._index, "all_entries"):
            entries = list(self._index.all_entries())  # type: ignore[attr-defined]
        elif isinstance(self._index, dict):
            entries = list(self._index.values())  # type: ignore[arg-type]
        elif isinstance(self._index, list):
            entries = list(self._index)  # type: ignore[arg-type]
        elif hasattr(self._index, "values"):
            try:
                entries = list(self._index.values())  # type: ignore[attr-defined]
            except Exception:
                entries = []

        matched = [
            e
            for e in entries
            if e.status == "active"
            and e.source.source_kind in {"official-live", "official-snapshot"}
            and e.lema.lower().startswith(cleaned)
        ]
        matched.sort(key=lambda e: e.lema.lower())
        return matched[:cap]

    def __call__(self, prefix: str, *, limit: int = _DEFAULT_LIMIT) -> list[KBBIEntry]:
        return self.lookup(prefix, limit=limit)
