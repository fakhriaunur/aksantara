"""Retrieve package — exact, prefix, semantic, citations."""

from aksantara.retrieve.citations import RetrievalInfo, render_citation
from aksantara.retrieve.exact import (
    ExactLookup,
    InMemoryEntryStore,
    InMemoryExactIndex,
    retrieve_exact,
)
from aksantara.retrieve.prefix import PrefixLookup, retrieve_prefix
from aksantara.retrieve.semantic import (
    SemanticHit,
    SemanticRetriever,
    retrieve_semantic,
)

__all__ = [
    "ExactLookup",
    "InMemoryEntryStore",
    "InMemoryExactIndex",
    "PrefixLookup",
    "RetrievalInfo",
    "SemanticHit",
    "SemanticRetriever",
    "render_citation",
    "retrieve_exact",
    "retrieve_prefix",
    "retrieve_semantic",
]
