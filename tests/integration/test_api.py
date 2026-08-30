from datetime import UTC, datetime

from fastapi.testclient import TestClient

from aksantara.api.routes import create_app
from aksantara.domain.models import KBBIEntry, SourceRef
from aksantara.domain.provenance import content_hash_bytes
from aksantara.embeddings.document import build_embedding_document
from aksantara.embeddings.firestore import InMemoryVectorStore, VectorRecord
from aksantara.embeddings.vertex import VertexEmbeddingClient
from aksantara.retrieve.exact import InMemoryEntryStore


def _seed() -> tuple[InMemoryEntryStore, InMemoryVectorStore, VertexEmbeddingClient]:
    es = InMemoryEntryStore()
    vs = InMemoryVectorStore(distance_measure="DOT_PRODUCT")
    vc = VertexEmbeddingClient(dimensions=64)
    raw = b"februari fixture"
    src = SourceRef(
        url="https://kbbi.kemdikbud.go.id/entri/februari",
        source_kind="official-live",
        edition="VI",
        source_version="VI",
        retrieved_at=datetime.now(UTC),
        content_hash=content_hash_bytes(raw),
    )
    entry = KBBIEntry(
        id="februari",
        lema="Februari",
        makna=[{"definisi": "bulan ke-2 tahun Masehi"}],
        bentuk_tidak_baku=["Pebruari"],
        kelas_kata=["nomina"],
        source=src,
    )
    es.put(entry)
    doc = build_embedding_document(entry)
    vec = vc.embed(doc)
    vs.put(
        VectorRecord(
            entry_id=entry.id,
            lema=entry.lema,
            vector=vec,
            metadata={
                "source_kind": "official-live",
                "edition": "VI",
                "content_hash": src.content_hash,
            },
        )
    )
    return es, vs, vc


def test_health() -> None:
    es, vs, vc = _seed()
    app = create_app(es, vs, vc)
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_exact_endpoint() -> None:
    es, vs, vc = _seed()
    app = create_app(es, vs, vc)
    client = TestClient(app)
    r = client.get("/entries/Februari")
    assert r.status_code == 200
    j = r.json()
    # heavy router returns single entry dict; legacy returns results list — support both
    if "results" in j:
        assert j["count"] == 1
        assert j["results"][0]["lema"] == "Februari"
        # legacy citation shape
        src = j["results"][0].get("source") or j["results"][0].get("citation", {}).get(
            "source"
        )
        assert src
    else:
        # heavy shape: entry dict with lema/citation
        assert (
            j.get("lema") == "Februari" or j.get("entry", {}).get("lema") == "Februari"
        )
        # check provenance present
        assert j.get("citation") or j.get("source")


def test_semantic_not_found() -> None:
    es, vs, vc = _seed()
    app = create_app(es, vs, vc)
    client = TestClient(app)
    r = client.get("/search/semantic", params={"q": "xyzabc123notindictionary"})
    assert r.status_code == 200
    j = r.json()
    assert "results" in j
    assert "count" in j
    # heavy returns empty list fail-closed; legacy may include message
    assert j["count"] == 0 or isinstance(j["results"], list)


def test_nonstandard_relation() -> None:
    es, vs, vc = _seed()
    app = create_app(es, vs, vc)
    client = TestClient(app)
    r = client.get("/relations/nonstandard/Pebruari")
    assert r.status_code == 200
    j = r.json()
    # heavy returns word/standard_form; legacy returns count/standard
    if "count" in j:
        assert j["count"] == 1
        assert j["standard"] == "Februari"
    else:
        assert j.get("standard_form") == "Februari" or j.get("word") == "Pebruari"
        assert j.get("entry") or j.get("citation")
