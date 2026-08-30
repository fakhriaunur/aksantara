"""Vertex AI embedding client — gemini-embedding-001 with 768d, truncation, retry.

Firestore max dim 2048, gemini-embedding-001 native 3072 → request outputDimensionality=768.
Uses google-genai (preferred) with deterministic hash fallback for offline tests.

Exports both the spec-required ``VertexGeminiEmbedding`` (with RETRIEVAL_DOCUMENT/
RETRIEVAL_QUERY, truncate at ~1.5k tokens, retry) and the legacy
``VertexEmbeddingClient`` alias for earlier slices / offline demos.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

TaskType = Literal[
    "RETRIEVAL_DOCUMENT",
    "RETRIEVAL_QUERY",
    "SEMANTIC_SIMILARITY",
    "CLASSIFICATION",
    "CLUSTERING",
]

# Canonical spec constants
DEFAULT_MODEL: str = "gemini-embedding-001"
DEFAULT_DIMS: int = 768
MAX_CHARS_PER_REQUEST: int = 6000  # ~1500 tokens * 4 chars
DEFAULT_RETRIES: int = 3
DEFAULT_BACKOFF_BASE_S: float = 0.5
TASK_DOCUMENT: str = "RETRIEVAL_DOCUMENT"
TASK_QUERY: str = "RETRIEVAL_QUERY"
DISTANCE_MEASURE: str = "DOT_PRODUCT"


def truncate_for_embedding(text: str, max_chars: int = MAX_CHARS_PER_REQUEST) -> str:
    """Truncate to approximate token ceiling, deterministic."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars].rstrip()
    logger.warning("vertex_truncate: %d -> %d chars", len(text), len(truncated))
    return truncated


def _hash_vector(text: str, dimensions: int = DEFAULT_DIMS) -> list[float]:
    """Deterministic offline vector: sha256-seeded, L2-normalized."""
    h = hashlib.sha256(text.encode("utf-8")).digest()
    vec: list[float] = []
    for i in range(dimensions):
        byte = h[i % len(h)]
        v = (byte / 127.5) - 1.0
        vec.append(v * 0.1 + (i % 7) * 0.001)
    norm = sum(x * x for x in vec) ** 0.5
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


# ---------------------------------------------------------------------------
# Core client with offline fallback — satisfies both legacy and spec paths.
# ---------------------------------------------------------------------------


class VertexEmbeddingClient:
    """Thin wrapper; offline mode returns deterministic pseudo-vector for tests."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        dimensions: int = DEFAULT_DIMS,
        task_type: TaskType = "RETRIEVAL_DOCUMENT",  # type: ignore[assignment]
        project: str | None = None,
        location: str | None = None,
    ) -> None:
        self.model = model
        self.dimensions = dimensions
        self.task_type: TaskType = task_type  # type: ignore[assignment]
        self.project = project or os.getenv("GOOGLE_CLOUD_PROJECT")
        self.location = (
            location or os.getenv("GOOGLE_CLOUD_LOCATION") or "asia-southeast1"
        )

    def _hash_vector(self, text: str) -> list[float]:
        return _hash_vector(text, self.dimensions)

    def embed(self, text: str, task_type: TaskType | None = None) -> list[float]:
        """Embed single text; Vertex if credentials present, else hash fallback."""
        truncated = truncate_for_embedding(text, MAX_CHARS_PER_REQUEST)
        if not self.project or os.getenv("AKSANTARA_OFFLINE_EMBED", "1") == "1":
            return self._hash_vector(truncated)
        try:
            from google import genai  # type: ignore[import-not-found]
            from google.genai import types  # type: ignore[import-not-found]

            client = genai.Client(
                vertexai=True, project=self.project, location=self.location
            )
            config = types.EmbedContentConfig(
                task_type=task_type or self.task_type,
                output_dimensionality=self.dimensions,  # type: ignore[arg-type]
            )
            resp = client.models.embed_content(
                model=self.model, contents=[truncated], config=config
            )
            emb = resp.embeddings[0]
            values = emb.values if hasattr(emb, "values") else emb  # type: ignore[no-any-return]
            if len(values) != self.dimensions:
                values = list(values)[: self.dimensions]
            return list(values)
        except Exception as exc:
            logger.warning("vertex embed fallback to hash: %s", exc)
            return self._hash_vector(truncated)

    def embed_batch(
        self, texts: list[str], task_type: TaskType | None = None
    ) -> list[list[float]]:
        return [self.embed(t, task_type=task_type) for t in texts]

    # Spec-order aliases
    def embed_query(self, text: str) -> list[float]:
        return self.embed(text, task_type="RETRIEVAL_QUERY")  # type: ignore[arg-type]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed_batch(texts, task_type="RETRIEVAL_DOCUMENT")  # type: ignore[arg-type]

    @property
    def model_info(self) -> dict[str, str | int]:
        return {
            "model": self.model,
            "dimensions": self.dimensions,
            "task_type": self.task_type,
        }


# ---------------------------------------------------------------------------
# Spec-required wrapper with retry, explicit config, and port protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class EmbeddingClient(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


@dataclass(frozen=True, slots=True)
class VertexGeminiEmbeddingConfig:
    model: str = DEFAULT_MODEL
    output_dimensionality: int = DEFAULT_DIMS
    task_type_document: str = TASK_DOCUMENT
    task_type_query: str = TASK_QUERY
    location: str = "global"
    max_chars: int = MAX_CHARS_PER_REQUEST
    retries: int = DEFAULT_RETRIES
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S


class VertexGeminiEmbedding:
    """Spec-compliant wrapper with retry and explicit truncation.

    Wraps ``VertexEmbeddingClient`` for the offline hash path, and
    ``google.genai`` directly for the online path with exponential backoff.
    Construction does not open a network client.
    """

    def __init__(
        self,
        project: str | None = None,
        *,
        config: VertexGeminiEmbeddingConfig | None = None,
        client_override: Any | None = None,
    ) -> None:
        self._project = project
        self._config = config or VertexGeminiEmbeddingConfig()
        self._client_override = client_override
        self._client: Any | None = client_override
        # Reuse the hash-capable client for offline mode.
        self._fallback = VertexEmbeddingClient(
            model=self._config.model,
            dimensions=self._config.output_dimensionality,
            project=project,
            location=self._config.location,
        )

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        # Prefer offline hash path when no project or when explicitly offline.
        if not self._project or os.getenv("AKSANTARA_OFFLINE_EMBED", "1") == "1":
            return self._fallback
        try:
            from google import genai  # type: ignore[import-untyped]

            self._client = genai.Client(
                vertexai=True, project=self._project, location=self._config.location
            )
            return self._client
        except Exception as exc:
            logger.warning("Vertex client init fallback to hash: %s", exc)
            return self._fallback

    def _with_retry(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self._config.retries + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                if any(
                    kw in msg
                    for kw in (
                        "permission",
                        "not found",
                        "unauthenticated",
                        "invalid argument",
                    )
                ):
                    raise
                if attempt >= self._config.retries:
                    raise
                backoff = self._config.backoff_base_s * (2**attempt)
                logger.warning(
                    "vertex retry %d/%d backoff %.2fs: %s",
                    attempt + 1,
                    self._config.retries,
                    backoff,
                    exc,
                )
                time.sleep(backoff)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("vertex retry fell through")

    def _embed_one(self, text: str, *, task_type: str) -> list[float]:
        truncated = truncate_for_embedding(text, self._config.max_chars)
        client = self._get_client()
        # If the client is the fallback hash client, delegate.
        if isinstance(client, VertexEmbeddingClient):
            return client.embed(truncated, task_type=task_type)  # type: ignore[arg-type]

        try:
            from google.genai import (
                types as genai_types,  # type: ignore[import-untyped]
            )

            config_obj = genai_types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=self._config.output_dimensionality,
            )
        except Exception:
            config_obj = None  # type: ignore[assignment]

        def _call() -> Any:
            models_ns = getattr(client, "models", None)
            if models_ns is not None and hasattr(models_ns, "embed_content"):
                if config_obj is not None:
                    return models_ns.embed_content(
                        model=self._config.model,
                        contents=[truncated],
                        config=config_obj,
                    )
                return models_ns.embed_content(
                    model=self._config.model,
                    contents=[truncated],
                    config={
                        "task_type": task_type,
                        "outputDimensionality": self._config.output_dimensionality,
                    },
                )
            if hasattr(client, "embed_content"):
                return client.embed_content(
                    model=self._config.model, contents=[truncated], config=config_obj
                )
            raise AttributeError("embedding client has no embed_content")

        response = self._with_retry(_call)

        # Normalize shapes
        if isinstance(response, list):
            if response and isinstance(response[0], (int, float)):  # type: ignore[redundant-expr]
                return [float(x) for x in response]  # type: ignore[arg-type]
            if response and isinstance(response[0], list):
                return [float(x) for x in response[0]]  # type: ignore[arg-type]
            return [float(x) for x in response]  # type: ignore[arg-type]
        if hasattr(response, "embeddings"):
            emb_list = response.embeddings  # type: ignore[attr-defined]
            if emb_list:
                first = emb_list[0]
                vals = getattr(first, "values", first)
                if isinstance(vals, dict) and "values" in vals:
                    vals = vals["values"]
                return [float(x) for x in vals]  # type: ignore[arg-type]
        if hasattr(response, "values"):
            return [float(x) for x in response.values]  # type: ignore[attr-defined]
        if isinstance(response, dict) and "values" in response:
            return [float(x) for x in response["values"]]
        if isinstance(response, dict) and "embedding" in response:
            emb = response["embedding"]
            if isinstance(emb, dict) and "values" in emb:
                return [float(x) for x in emb["values"]]
            return [float(x) for x in emb]  # type: ignore[arg-type]
        return self._fallback.embed(truncated, task_type=task_type)  # type: ignore[arg-type]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return [
            self._embed_one(t, task_type=self._config.task_type_document) for t in texts
        ]

    def embed_query(self, text: str) -> list[float]:
        if not text.strip():
            raise ValueError("embed_query requires non-empty text")
        return self._embed_one(text, task_type=self._config.task_type_query)

    def embed(
        self, texts: list[str], *, task_type: str | None = None
    ) -> list[list[float]]:
        tt = task_type or self._config.task_type_document
        return [self._embed_one(t, task_type=tt) for t in texts]

    # Legacy single-text alias
    def embed_single(self, text: str, task_type: TaskType | None = None) -> list[float]:  # type: ignore[assignment]
        return self._embed_one(text, task_type=task_type or TASK_DOCUMENT)  # type: ignore[arg-type]

    def embed_batch(
        self, texts: list[str], task_type: TaskType | None = None
    ) -> list[list[float]]:  # type: ignore[assignment]
        tt = task_type or self._config.task_type_document  # type: ignore[assignment]
        return [self._embed_one(t, task_type=tt) for t in texts]


__all__ = [
    "DEFAULT_DIMS",
    "DEFAULT_MODEL",
    "DISTANCE_MEASURE",
    "MAX_CHARS_PER_REQUEST",
    "TASK_DOCUMENT",
    "TASK_QUERY",
    "VertexEmbeddingClient",
    "VertexGeminiEmbedding",
    "VertexGeminiEmbeddingConfig",
    "truncate_for_embedding",
]
