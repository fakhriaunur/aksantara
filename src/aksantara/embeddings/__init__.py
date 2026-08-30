"""Embeddings package — document, Vertex, Firestore, manifests."""

from aksantara.embeddings.document import build_embedding_document
from aksantara.embeddings.firestore import (
    EmbeddingStore,
    FirestoreVectorStore,
    InMemoryVectorStore,
    NearestResult,
    VectorRecord,
)
from aksantara.embeddings.manifests import (
    DEFAULT_EMBEDDING_CONFIG,
    build_manifest,
    manifest_hash,
)
from aksantara.embeddings.vertex import (
    DEFAULT_DIMS,
    DEFAULT_MODEL,
    VertexEmbeddingClient,
    VertexGeminiEmbedding,
    VertexGeminiEmbeddingConfig,
    truncate_for_embedding,
)

__all__ = [
    "DEFAULT_DIMS",
    "DEFAULT_EMBEDDING_CONFIG",
    "DEFAULT_MODEL",
    "EmbeddingStore",
    "FirestoreVectorStore",
    "InMemoryVectorStore",
    "NearestResult",
    "VectorRecord",
    "VertexEmbeddingClient",
    "VertexGeminiEmbedding",
    "VertexGeminiEmbeddingConfig",
    "build_embedding_document",
    "build_manifest",
    "manifest_hash",
    "truncate_for_embedding",
]
