from __future__ import annotations

from typing import Any

import pytest

from aksantara.embeddings.firestore import (
    VECTOR_FIELD,
    FirestoreVectorStore,
    InMemoryVectorStore,
    VectorRecord,
)


class _FakeDocument:
    def __init__(self, collection: _FakeCollection, doc_id: str) -> None:
        self.collection = collection
        self.id = doc_id


class _FakeCollection:
    def __init__(self, name: str) -> None:
        self.name = name

    def document(self, doc_id: str) -> _FakeDocument:
        return _FakeDocument(self, doc_id)


class _FakeBatch:
    def __init__(self) -> None:
        self.writes: list[tuple[_FakeDocument, dict[str, Any], bool]] = []
        self.committed = False

    def set(
        self, reference: _FakeDocument, document_data: dict[str, Any], merge: bool
    ) -> None:
        self.writes.append((reference, document_data, merge))

    def commit(self) -> list[object]:
        self.committed = True
        return []


class _FakeFirestoreClient:
    def __init__(self) -> None:
        self.batches: list[_FakeBatch] = []

    def collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(name)

    def batch(self) -> _FakeBatch:
        batch = _FakeBatch()
        self.batches.append(batch)
        return batch


def _record(
    entry_id: str = "februari",
    version: str = "2026-08-30.1",
    vector: tuple[float, ...] = (1.0, 0.0, 0.0),
) -> VectorRecord:
    return VectorRecord(
        id=entry_id,
        version=version,
        lema=entry_id.title(),
        embedding=vector,
        dimensions=len(vector),
        content_hash="a" * 64,
    )


def test_in_memory_put_many_is_empty_safe_and_idempotent() -> None:
    store = InMemoryVectorStore()

    store.put_many([])
    assert store.count() == 0

    record = _record()
    store.put_many([record, record])
    store.put_many([record])

    assert store.count() == 1
    assert store._store[record.doc_id] is record


def test_in_memory_put_many_rejects_conflicting_duplicate_document_ids() -> None:
    store = InMemoryVectorStore()
    first = _record()
    conflicting = _record(vector=(0.0, 1.0, 0.0))

    with pytest.raises(ValueError, match="conflicting records"):
        store.put_many([first, conflicting])

    assert store.count() == 0


def test_firestore_put_many_commits_schema_with_deterministic_ids() -> None:
    client = _FakeFirestoreClient()
    store = FirestoreVectorStore(client_override=client, dimensions=3)
    records = [_record(), _record("Maret", vector=(0.0, 1.0, 0.0))]

    store.put_many(records)

    assert len(client.batches) == 1
    batch = client.batches[0]
    assert batch.committed is True
    assert [write[0].id for write in batch.writes] == [
        "februari_2026-08-30.1",
        "Maret_2026-08-30.1",
    ]
    assert all(write[2] is True for write in batch.writes)
    assert all(
        write[1]["id"] == write[0].id.rsplit("_", 1)[0] for write in batch.writes
    )
    assert all(
        list(write[1][VECTOR_FIELD]) == list(record.vector_as_list())
        for write, record in zip(batch.writes, records, strict=True)
    )


def test_firestore_put_many_collapses_idempotent_duplicates() -> None:
    client = _FakeFirestoreClient()
    store = FirestoreVectorStore(client_override=client, dimensions=3)
    record = _record()

    store.put_many([record, record])

    assert len(client.batches) == 1
    assert len(client.batches[0].writes) == 1


def test_firestore_put_many_empty_batch_does_not_create_or_commit_batch() -> None:
    client = _FakeFirestoreClient()
    store = FirestoreVectorStore(client_override=client, dimensions=3)

    store.put_many([])

    assert client.batches == []
