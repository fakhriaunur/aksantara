"""API package — FastAPI router and schemas."""

from aksantara.api.routes import create_app, create_router
from aksantara.api.schemas import (
    CitationSchema,
    EntriesResponseSchema,
    EntryResultSchema,
    HealthResponseSchema,
    KBBIEntrySchema,
    NonStandardRelationSchema,
    RetrievalSchema,
    SemanticSearchResponseSchema,
    SourceProvenanceSchema,
    VersionResponseSchema,
)

__all__ = [
    "CitationSchema",
    "EntriesResponseSchema",
    "EntryResultSchema",
    "HealthResponseSchema",
    "KBBIEntrySchema",
    "NonStandardRelationSchema",
    "RetrievalSchema",
    "SemanticSearchResponseSchema",
    "SourceProvenanceSchema",
    "VersionResponseSchema",
    "create_app",
    "create_router",
]
