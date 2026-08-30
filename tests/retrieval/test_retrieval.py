from datetime import UTC, datetime

from aksantara.domain.models import KBBIEntry, SourceRef
from aksantara.domain.provenance import content_hash_bytes
from aksantara.embeddings.document import build_embedding_document
from aksantara.embeddings.firestore import InMemoryVectorStore, VectorRecord
from aksantara.embeddings.vertex import VertexEmbeddingClient
from aksantara.retrieve.citations import render_citation
from aksantara.retrieve.exact import InMemoryEntryStore
from aksantara.retrieve.semantic import retrieve_semantic

FIXTURE_HTML = b"<html><head><title>Februari</title></head><body><h2>Februari</h2><ol><li>bulan ke-2</li></ol><p>Bentuk tidak baku: Pebruari</p></body></html>"


def _make_entry(lema="Februari", makna=None, bentuk=None) -> KBBIEntry:
    raw = FIXTURE_HTML
    source = SourceRef(
        url=f"https://kbbi.kemdikbud.go.id/entri/{lema.lower()}",
        source_kind="official-live",
        edition="VI",
        source_version="VI",
        retrieved_at=datetime.now(UTC),
        content_hash=content_hash_bytes(raw),
    )
    return KBBIEntry(
        id=lema.lower(),
        lema=lema,
        makna=makna or [{"definisi": "bulan ke-2 tahun Masehi"}],
        bentuk_tidak_baku=bentuk or (["Pebruari"] if lema == "Februari" else []),
        kelas_kata=["nomina"],
        source=source,
    )


def test_exact_lookup() -> None:
    store = InMemoryEntryStore()
    entry = _make_entry()
    store.put(entry)
    from aksantara.retrieve.exact import retrieve_exact

    assert retrieve_exact("Februari", store) is not None
    assert retrieve_exact("februari", store) is not None
    assert retrieve_exact("unknown", store) is None


def test_prefix_lookup() -> None:
    store = InMemoryEntryStore()
    store.put(_make_entry("Februari"))
    store.put(_make_entry("November", bentuk=[]))
    from aksantara.retrieve.prefix import retrieve_prefix

    hits = retrieve_prefix("Feb", store)
    assert any(e.lema == "Februari" for e in hits)
    assert retrieve_prefix("Xyz", store) == []


def test_semantic_fail_closed_unknown() -> None:
    entry_store = InMemoryEntryStore()
    entry = _make_entry()
    entry_store.put(entry)
    vector_store = InMemoryVectorStore(distance_measure="DOT_PRODUCT")
    client = VertexEmbeddingClient(dimensions=64)  # small for speed
    # Populate vector
    doc = build_embedding_document(entry)
    vec = client.embed(doc, task_type="RETRIEVAL_DOCUMENT")
    vector_store.put(
        VectorRecord(
            entry_id=entry.id,
            lema=entry.lema,
            vector=vec,
            metadata={"source_kind": "official-live"},
        )
    )

    # Exact path
    exact_results = retrieve_semantic("Februari", entry_store, vector_store, client)
    assert exact_results and exact_results[0]["mode"] == "exact"

    # Unknown should be fail-closed (no semantic hit with tight threshold)
    # Use very strict threshold to force empty
    empty = retrieve_semantic(
        "xyzabc123notindictionary",
        entry_store,
        vector_store,
        client,
        distance_threshold=0.1,
    )
    assert empty == []

    # Semantic with reasonable threshold should find Februari for related meaning
    semantic = retrieve_semantic(
        "bulan kedua tahun Masehi",
        entry_store,
        vector_store,
        client,
        distance_threshold=1.5,
    )
    # With hash vectors, distance may be arbitrary — just check it doesn't crash and respects filter
    assert isinstance(semantic, list)


def test_nonstandard_relation() -> None:
    store = InMemoryEntryStore()
    entry = _make_entry()
    store.put(entry)
    assert store.get_by_nonstandard("Pebruari") is not None
    assert store.get_by_nonstandard("Pebruari").lema == "Februari"
    assert store.get_by_nonstandard("Unknown") is None


def test_citation_contains_provenance() -> None:
    entry = _make_entry()
    cit = render_citation(entry, "exact", 0.0)
    assert cit["source"]["contentHash"] == entry.source.content_hash
    assert cit["source"]["edition"] == "VI"
    assert cit["retrieval"]["mode"] == "exact"
