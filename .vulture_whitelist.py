# Vulture whitelist — intentionally unused symbols that are part of public API or future wiring.
# See: https://github.com/jendrikseipp/vulture
# ruff: noqa: F821

# Agents are instantiated via ADK factory, not direct import
lead = lead
ingestion = ingestion
retrieval_normalization = retrieval_normalization

# Projections are future downstream tracks, currently stubbed
projections = projections

# FastAPI dependency overrides for tests
_get_test_overrides = _get_test_overrides

# Firestore/Vertex offline fallback constructors
FirestoreVectorStore = FirestoreVectorStore
VertexEmbeddingClient = VertexEmbeddingClient

# False positives — pydantic validators and context managers (vulture min-confidence 80)
cls = cls
exc_tb = exc_tb
exc_type = exc_type
exc_val = exc_val
raw_hash = raw_hash
