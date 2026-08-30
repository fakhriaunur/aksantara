"""FastAPI router for Aksantara Pramana retrieval.

Endpoints (all cited with provenance):
- GET /entries/{lema}           — exact lookup
- GET /entries?q=               — prefix search (query-param form)
- GET /search/semantic?q=       — semantic vector search (fail-closed)
- GET /relations/nonstandard/{word} — nonstandard → standard
- GET /versions/current         — live release version (config/current_version)
- GET /health                   — liveness probe

Dependency injection
--------------------
The router is built with injectable factories so unit tests can supply
in-memory fakes without credentials:

    app = create_app(
        exact_lookup=ExactLookup(index=my_index),
        prefix_lookup=PrefixLookup(index=my_index),
        semantic_retriever=SemanticRetriever(... fakes ...),
        version_provider=lambda: {"version": "2026-08-30.1", ...},
    )

Alternatively, FastAPI's ``app.dependency_overrides`` may be used directly
with the ``get_*`` dependency functions exposed below.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from aksantara.domain.models import KBBIEntry
from aksantara.retrieve.citations import RetrievalInfo, render_citation
from aksantara.retrieve.exact import ExactLookup, InMemoryExactIndex
from aksantara.retrieve.prefix import PrefixLookup
from aksantara.retrieve.semantic import SemanticRetriever

__all__ = [
    "create_app",
    "create_router",
    "get_exact_lookup",
    "get_prefix_lookup",
    "get_semantic_retriever",
    "get_version_provider",
]

# ---------------------------------------------------------------------------
# Dependency singletons (overridable)
# ---------------------------------------------------------------------------

_default_index: InMemoryExactIndex = InMemoryExactIndex()
_default_exact: ExactLookup = ExactLookup(index=_default_index)
_default_prefix: PrefixLookup = PrefixLookup(index=_default_index)
# Semantic retriever is left unset by default so the app can still boot
# without Vertex/Firestore credentials. Calls without a retriever return
# an empty semantic result (fail-closed).
_default_semantic: SemanticRetriever | None = None
_default_version: dict[str, Any] = {
    "version": "2026-08-30.1",
    "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    "edition": "VI",
    "entries_count": 0,
    "embedding": {
        "model": "gemini-embedding-001",
        "dimensions": 768,
        "distance_measure": "DOT_PRODUCT",
    },
    "source": None,
}
_default_firestore_client: Any | None = None


def get_exact_lookup() -> ExactLookup:
    return _default_exact


def get_prefix_lookup() -> PrefixLookup:
    return _default_prefix


def get_semantic_retriever() -> SemanticRetriever | None:
    return _default_semantic


def get_version_provider() -> Any:
    """Return a callable or dict that resolves to the current version record."""
    return _default_version


def get_firestore_client() -> Any | None:
    return _default_firestore_client


# -- test helpers ----------------------------------------------------------


def _set_test_overrides(
    *,
    exact: ExactLookup | None = None,
    prefix: PrefixLookup | None = None,
    semantic: SemanticRetriever | None = None,
    version: Any | None = None,
    firestore_client: Any | None = None,
    index: InMemoryExactIndex | None = None,
) -> None:
    """Mutate module-level defaults for tests (not for production code)."""
    global \
        _default_exact, \
        _default_prefix, \
        _default_semantic, \
        _default_version, \
        _default_index, \
        _default_firestore_client
    if index is not None:
        _default_index = index
        _default_exact = ExactLookup(index=index)
        _default_prefix = PrefixLookup(index=index)
    if exact is not None:
        _default_exact = exact
    if prefix is not None:
        _default_prefix = prefix
    if semantic is not None:
        _default_semantic = semantic
    if version is not None:
        _default_version = version
    if firestore_client is not None:
        _default_firestore_client = firestore_client


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _entry_to_api_dict(entry: KBBIEntry) -> dict[str, Any]:
    """Project canonical KBBIEntry to API shape including source provenance."""
    d = entry.model_dump(mode="json")
    # Ensure retrievedAt is formatted as ISO Z string (model_dump emits ISO already).
    # Normalize contentHash key casing for API schema.
    src = d.get("source", {})
    if isinstance(src, dict):
        # model_dump uses snake_case source_content_hash? Actually SourceRef has content_hash field
        # which serializes as content_hash. Normalize to API's contentHash/retrievedAt.
        api_source = {
            "url": src.get("url", ""),
            "edition": src.get("edition", "VI"),
            "source_version": src.get("source_version", src.get("sourceVersion", "VI")),
            "retrievedAt": src.get("retrieved_at") or src.get("retrievedAt") or "",
            "contentHash": src.get("content_hash") or src.get("contentHash") or "",
            "parserVersion": src.get("parser_version")
            or src.get("parserVersion")
            or entry.parser_version,
        }
        # retrieved_at may be a string already; ensure Z suffix.
        ra = api_source["retrievedAt"]
        if isinstance(ra, str) and ra and not ra.endswith("Z") and "T" in ra:
            # Attempt to parse and reformat.
            try:
                dt = datetime.fromisoformat(ra.replace("Z", "+00:00"))
                api_source["retrievedAt"] = _iso_utc(dt)
            except Exception:
                pass
        d["source"] = api_source
    return d


def _build_entry_result(entry: KBBIEntry, retrieval: RetrievalInfo) -> dict[str, Any]:
    entry_dict = _entry_to_api_dict(entry)
    citation = render_citation(entry, retrieval=retrieval)
    retrieval_dict = retrieval.to_dict()
    # distance alias at result top for backward compat / docs expectation.
    top_distance = retrieval_dict.get("distance")
    result: dict[str, Any] = {
        "entry": {**entry_dict, "citation": citation, "retrieval": retrieval_dict},
        "citation": citation,
        "retrieval": retrieval_dict,
    }
    if top_distance is not None:
        result["vector_distance"] = top_distance
        result["entry"]["vector_distance"] = top_distance
    return result


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

_API_PREFIX = ""  # mounted directly; caller may prefix via include_router


def create_router(
    *,
    exact_lookup: ExactLookup | None = None,
    prefix_lookup: PrefixLookup | None = None,
    semantic_retriever: SemanticRetriever | None = None,
    version_provider: Any | None = None,
    firestore_client: Any | None = None,
) -> APIRouter:
    """Create a router with optional injected dependencies.

    When a dependency arg is supplied it closes over that instance; otherwise
    the module-level ``get_*`` functions are used (overridable via
    ``app.dependency_overrides``).
    """
    router = APIRouter()

    # Resolve injected vs provider-based deps inside each handler so overrides
    # remain discoverable.

    def _exact() -> ExactLookup:
        return exact_lookup if exact_lookup is not None else get_exact_lookup()

    def _prefix() -> PrefixLookup:
        return prefix_lookup if prefix_lookup is not None else get_prefix_lookup()

    def _semantic() -> SemanticRetriever | None:
        return (
            semantic_retriever
            if semantic_retriever is not None
            else get_semantic_retriever()
        )

    def _version() -> Any:
        return (
            version_provider if version_provider is not None else get_version_provider()
        )

    # -- health ------------------------------------------------------------

    @router.get("/health", tags=["ops"])
    def health() -> dict[str, Any]:
        client = (
            firestore_client if firestore_client is not None else get_firestore_client()
        )
        firestore_state = "available" if client is not None else "not_configured"
        # Probe Firestore availability without blocking if a client exists.
        if client is not None:
            try:
                # Lightweight probe — do not leak credentials.
                _ = client.collection  # attribute presence check
                firestore_state = "available"
            except Exception:
                firestore_state = "unavailable"
        return {
            "status": "ok",
            "version": "0.1.0",
            "firestore": firestore_state,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }

    # -- versions ----------------------------------------------------------

    @router.get("/versions/current", tags=["release"])
    def versions_current(version_src: Any = Depends(_version)) -> dict[str, Any]:
        # version_src may be a dict, a callable returning a dict, or a provider
        # object with .get() / .read().
        data: Any = version_src
        if callable(data):
            try:
                data = data()
            except Exception as exc:
                raise HTTPException(
                    status_code=500, detail=f"version provider error: {exc}"
                ) from exc
        if hasattr(data, "to_dict"):
            try:
                data = data.to_dict()
            except Exception:
                pass
        if isinstance(data, dict):
            # Support both manifest shape and Firestore doc shape.
            # Normalize created_at alias.
            if "created_at" in data and "createdAt" not in data:
                data = {**data, "createdAt": data["created_at"]}
            return (
                data
                if "version" in data
                else {
                    "version": str(data),
                    "created_at": _iso_utc(datetime.now(UTC)),
                    "edition": "VI",
                    "entries_count": 0,
                }
            )
        # Fallback — treat as version string.
        return {
            "version": str(data),
            "created_at": _iso_utc(datetime.now(UTC)),
            "edition": "VI",
            "entries_count": 0,
        }

    # -- entries by query (prefix) — defined before /entries/{lema} -------

    @router.get("/entries", tags=["retrieval"])
    def entries_by_query(
        q: str | None = Query(default=None, description="Prefix query for lema"),
        limit: int = Query(default=20, ge=1, le=50),
        prefix_resolver: PrefixLookup = Depends(_prefix),
    ) -> dict[str, Any]:
        if q is None or not q.strip():
            raise HTTPException(
                status_code=400,
                detail="query param q is required for prefix search; use /entries/{lema} for exact lookup",
            )
        hits = prefix_resolver.lookup(q, limit=limit)
        results = [_build_entry_result(e, RetrievalInfo(mode="prefix")) for e in hits]
        return {"query": q, "count": len(results), "results": results}

    # -- entries exact -----------------------------------------------------

    @router.get("/entries/{lema}", tags=["retrieval"])
    def entries_exact(
        lema: str,
        exact: ExactLookup = Depends(_exact),
    ) -> dict[str, Any]:
        entry = exact.lookup(lema)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"entry not found: {lema}")
        retrieval = RetrievalInfo(mode="exact")
        result = _build_entry_result(entry, retrieval)
        # For this endpoint we return the single entry as the primary shape
        # plus the pramana contract fields. Consumers that expect a list
        # should use the collection endpoints.
        return {
            **result["entry"],
            **{
                "citation": result["citation"],
                "retrieval": result["retrieval"],
                "vector_distance": result.get("vector_distance"),
            },
        }

    # -- semantic search ---------------------------------------------------

    @router.get("/search/semantic", tags=["retrieval"])
    def search_semantic(
        q: str = Query(..., description="Semantic query"),
        limit: int = Query(default=10, ge=1, le=50),
        semantic: SemanticRetriever | None = Depends(_semantic),
    ) -> dict[str, Any]:
        if not q.strip():
            return {"query": q, "count": 0, "results": []}
        if semantic is None:
            # No embedding/vector backend configured — fail-closed rather than
            # raising 500 so the slice can boot without credentials.
            return {"query": q, "count": 0, "results": []}
        hits = semantic.search(q, limit=limit)
        results = []
        for h in hits:
            retrieval = RetrievalInfo(
                mode="semantic",
                distance=h.distance,
                threshold=h.threshold,
                distance_result_field=h.distance_result_field,
            )
            results.append(_build_entry_result(h.entry, retrieval))
        return {"query": q, "count": len(results), "results": results}

    # -- nonstandard relations ---------------------------------------------

    @router.get("/relations/nonstandard/{word}", tags=["retrieval"])
    def nonstandard_relation(
        word: str,
        exact: ExactLookup = Depends(_exact),
        prefix: PrefixLookup = Depends(_prefix),
    ) -> dict[str, Any]:
        cleaned = word.strip()
        if not cleaned:
            raise HTTPException(status_code=400, detail="word is required")
        lowered = cleaned.lower()

        # Direct exact match first — if the word itself is a lema, return its
        # standard/variant metadata.
        direct = exact.lookup(lowered)
        if direct is not None:
            retrieval = RetrievalInfo(mode="nonstandard")
            citation = render_citation(direct, retrieval=retrieval)
            entry_dict = _entry_to_api_dict(direct)
            return {
                "word": cleaned,
                "standard_form": direct.lema
                if not direct.bentuk_baku
                else direct.bentuk_baku,
                "variants": direct.bentuk_tidak_baku,
                "entry": {
                    **entry_dict,
                    "citation": citation,
                    "retrieval": retrieval.to_dict(),
                },
                "citation": citation,
            }

        # Reverse index over bentuk_tidak_baku: scan canonical index for any
        # entry that lists this word as a nonstandard variant.
        # We expose the index via the exact lookup's internal index when
        # available; otherwise scan Firestore when a client is injected.
        candidates: list[KBBIEntry] = []

        idx = getattr(exact, "_index", None)
        if idx is not None and hasattr(idx, "all_entries"):
            try:
                for e in idx.all_entries():  # type: ignore[attr-defined]
                    if any(v.lower() == lowered for v in e.bentuk_tidak_baku):
                        candidates.append(e)
                    # Also handle bentuk_baku pointer: entry is nonstandard variant record.
                    if e.bentuk_baku and e.bentuk_baku.lower() == lowered:
                        # This entry points to its standard; try resolving standard.
                        std = exact.lookup(e.bentuk_baku)
                        if std is not None:
                            candidates.append(std)
            except Exception:
                pass

        # If no in-memory candidates, try a Firestore scan for bentuk_tidak_baku.
        if not candidates:
            client = (
                firestore_client
                if firestore_client is not None
                else get_firestore_client()
            )
            if client is not None:
                try:
                    # Firestore array-contains query on bentuk_tidak_baku.
                    try:
                        from google.cloud.firestore import (
                            FieldFilter,  # type: ignore[import-untyped]
                        )

                        q = client.collection("entries").where(
                            filter=FieldFilter(
                                "bentuk_tidak_baku", "array_contains", cleaned
                            )
                        )
                        snaps = list(q.stream() if hasattr(q, "stream") else q.get())  # type: ignore[attr-defined]
                    except Exception:
                        q = client.collection("entries").where(
                            "bentuk_tidak_baku", "array_contains", cleaned
                        )  # type: ignore[call-arg]
                        snaps = list(q.stream() if hasattr(q, "stream") else [])  # type: ignore[attr-defined]
                    for snap in snaps:
                        data = snap.to_dict() if hasattr(snap, "to_dict") else {}
                        if isinstance(data, dict) and data:
                            try:
                                candidates.append(KBBIEntry.model_validate(data))
                            except Exception:
                                continue
                except Exception:
                    pass

        if candidates:
            # Prefer canonical standard (one where lema matches lowercased
            # variant target). At most one standard is expected per variant.
            best = candidates[0]
            # If Pebruari→Februari: Pebruari is variant, Februari is standard.
            # The entry with bentuk_tidak_baku containing Pebruari is Februari.
            retrieval = RetrievalInfo(mode="nonstandard")
            citation = render_citation(best, retrieval=retrieval)
            entry_dict = _entry_to_api_dict(best)
            return {
                "word": cleaned,
                "standard_form": best.lema,
                "variants": best.bentuk_tidak_baku,
                "entry": {
                    **entry_dict,
                    "citation": citation,
                    "retrieval": retrieval.to_dict(),
                },
                "citation": citation,
            }

        # No relation found — return 404 with provenance-free body so callers
        # can distinguish "no standard mapping" from server error.
        raise HTTPException(
            status_code=404, detail=f"no nonstandard relation for: {cleaned}"
        )

    return router


def create_app(
    *args: Any,
    exact_lookup: ExactLookup | None = None,
    prefix_lookup: PrefixLookup | None = None,
    semantic_retriever: SemanticRetriever | None = None,
    version_provider: Any | None = None,
    firestore_client: Any | None = None,
    entry_store_override: Any | None = None,
    vector_store_override: Any | None = None,
    embed_client_override: Any | None = None,
) -> Any:
    # Back-compat: legacy tests call create_app(es, vs, vc) positional
    if args:
        # args[0]=InMemoryEntryStore, args[1]=InMemoryVectorStore, args[2]=VertexEmbeddingClient
        if len(args) >= 1 and entry_store_override is None:
            entry_store_override = args[0]
        if len(args) >= 2 and vector_store_override is None:
            vector_store_override = args[1]
        if len(args) >= 3 and embed_client_override is None:
            embed_client_override = args[2]
    # Legacy InMemory adaptors -> new ExactLookup/SemanticRetriever
    if entry_store_override is not None and exact_lookup is None:
        try:
            from aksantara.retrieve.exact import InMemoryEntryStore as _InMem

            if isinstance(entry_store_override, _InMem):
                try:
                    # try to wrap — fallback to direct index
                    getattr(entry_store_override, "_by_id", None)
                except Exception:
                    pass
                # Build ExactLookup around legacy store
                from aksantara.retrieve.exact import InMemoryEntryStore  # noqa

                # Use the legacy store as index for PrefixLookup as well
                class _LegacyExactWrapper:
                    def __init__(self, store):  # type: ignore[no-untyped-def]
                        self._store = store
                        self._index = store

                    def lookup(self, lema: str):  # type: ignore[no-untyped-def]
                        return self._store.get_by_lema(lema)

                exact_lookup = _LegacyExactWrapper(entry_store_override)  # type: ignore[assignment]
                if prefix_lookup is None:
                    from aksantara.retrieve.prefix import PrefixLookup as _PL

                    prefix_lookup = _PL(index=entry_store_override)
                if (
                    semantic_retriever is None
                    and vector_store_override is not None
                    and embed_client_override is not None
                ):
                    # Build SemanticRetriever from legacy components
                    class _LegacyEmbed:
                        def __init__(self, c):  # type: ignore[no-untyped-def]
                            self._c = c

                        def embed_query(self, q: str):  # type: ignore[no-untyped-def]
                            try:
                                return self._c.embed(q, task_type="RETRIEVAL_QUERY")  # type: ignore[attr-defined]
                            except TypeError:
                                return self._c.embed(q)  # type: ignore[attr-defined]

                    # vector_store has find_nearest; wrap to match heavy API
                    class _LegacyStoreWrapper:
                        def __init__(self, s):  # type: ignore[no-untyped-def]
                            self._s = s

                        def find_nearest(
                            self, qvec, limit=5, distance_threshold=0.6, **kwargs
                        ):  # type: ignore[no-untyped-def]
                            try:
                                return self._s.find_nearest(
                                    qvec,
                                    limit=limit,
                                    distance_threshold=distance_threshold,
                                    **kwargs,
                                )  # type: ignore[attr-defined]
                            except TypeError:
                                return self._s.find_nearest(
                                    qvec,
                                    limit=limit,
                                    distance_threshold=distance_threshold,
                                )  # type: ignore[attr-defined]

                    semantic_retriever = SemanticRetriever(  # type: ignore[call-arg]
                        embedding_client=_LegacyEmbed(embed_client_override),
                        vector_store=_LegacyStoreWrapper(vector_store_override),
                        canonical_index=entry_store_override,
                    )
        except Exception:
            pass
    """Create a FastAPI app with the Aksantara router.

    All dependencies are optional and injectable for tests. When omitted the
    module-level defaults are used.

    Returns:
        Configured FastAPI application.
    """
    from fastapi import FastAPI

    app = FastAPI(
        title="Aksantara Pramana API",
        description="KBBI-first exact / prefix / semantic retrieval with provenance citations",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Optionally stash injected clients on app.state for health/version probes
    # and for retrieval hydration fallback.
    if firestore_client is not None:
        app.state.firestore_client = firestore_client  # type: ignore[attr-defined]
        # Also seed module-level fallback so health sees it even without Depends override.
        _set_test_overrides(firestore_client=firestore_client)

    router = create_router(
        exact_lookup=exact_lookup,
        prefix_lookup=prefix_lookup,
        semantic_retriever=semantic_retriever,
        version_provider=version_provider,
        firestore_client=firestore_client,
    )
    app.include_router(router)

    # Expose a simple root redirect for convenience.
    @app.get("/", include_in_schema=False)
    def root() -> JSONResponse:
        return JSONResponse(
            {"service": "aksantara", "docs": "/docs", "health": "/health"}
        )

    return app
