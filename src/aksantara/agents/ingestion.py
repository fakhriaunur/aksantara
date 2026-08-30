"""Ingestion agent (ADK 2.8.0 LlmAgent stub).

Bounded, least-privilege agent that selects transport, applies rate limits,
archives raw bytes, computes contentHash, and manages resumable batches. It
does NOT interpret meanings or mutate semantic fields — that belongs to the
parser/validator and retrieval/normalization paths.

Graph definition only — no live LLM or network call on import.
"""

from __future__ import annotations

from typing import Any

try:
    from google.adk.agents import LlmAgent  # type: ignore[import-untyped]
except Exception:  # pragma: no cover

    class LlmAgent:  # type: ignore[no-redef]
        def __init__(
            self,
            *,
            name: str,
            model: str = "gemini-3.7-flash",
            description: str = "",
            instruction: str = "",
            tools: list[Any] | None = None,
            sub_agents: list[Any] | None = None,
            output_key: str | None = None,
            **kwargs: Any,
        ) -> None:
            self.name = name
            self.model = model
            self.description = description
            self.instruction = instruction
            self.tools: list[Any] = list(tools or [])
            self.sub_agents: list[Any] = list(sub_agents or [])
            self.output_key = output_key
            self.extra: dict[str, Any] = dict(kwargs)

        def __repr__(self) -> str:
            return f"LlmAgent(name={self.name!r}, model={self.model!r}, tools={len(self.tools)})"


# ---------------------------------------------------------------------------
# Bounded tools — Ingestion may fetch, hash, and snapshot but not interpret
# meanings, run embeddings, or write vector entries.
# ---------------------------------------------------------------------------


def _tool_fetch_kbbi(url: str) -> dict[str, Any]:
    """Ingestion tool: fetch raw snapshot from KBBI (httpx low concurrency).

    In production this delegates to ``aksantara.ingest.official``; the agent
    only declares the intent — deterministic fetch logic lives outside the LLM.
    """
    return {"requested_url": url, "transport": "httpx", "rate_limit": "low_concurrency"}


def _tool_archive_and_hash(raw_bytes_len: int, url: str) -> dict[str, Any]:
    """Ingestion tool: record content hash and GCS archive intent."""
    return {
        "url": url,
        "bytes": raw_bytes_len,
        "archive": "gs://<bucket>/raw/<hash>.html",
        "hash_algo": "sha256",
    }


def _tool_resume_batch(batch_id: str, cursor: str | None = None) -> dict[str, Any]:
    """Ingestion tool: resume a batched import from a cursor."""
    return {"batch_id": batch_id, "cursor": cursor, "resumable": True}


INGESTION_TOOLS: list[Any] = [
    _tool_fetch_kbbi,
    _tool_archive_and_hash,
    _tool_resume_batch,
]

INGESTION_TOOL_NAMES: list[str] = [
    getattr(t, "__name__", str(t)) for t in INGESTION_TOOLS
]

INGESTION_INSTRUCTION: str = """You are the Ingestion agent for Aksantara.

Responsibilities:
- Fetch official KBBI entries via httpx with low concurrency, bounded retries, and backoff.
- Archive raw bytes immutably (contentHash = hex sha256), record SourceRef, never parse meanings.
- Resume batched imports from cursors; report fetch failures and rate-limit signals.
- Label fallback sources explicitly; do not relabel enrichment as canonical.

Constraints:
- Do not interpret definitions, embeddings, or retrieval relevance.
- Do not mutate validated canonical entries — only the parser/validator pipeline asserts lexical facts.
- Stay within transport/hashing/resume scope; escalate authority or schema conflicts to Lead.
"""

INGESTION_DESCRIPTION: str = "Ingestion — transport, rate limits, raw snapshots, hashes, resume (does not interpret meanings)."


def create_ingestion_agent(
    *,
    model: str = "gemini-3.7-flash",
    tools: list[Any] | None = None,
    sub_agents: list[Any] | None = None,
    name: str = "ingestion",
) -> LlmAgent:
    """Create the Ingestion LlmAgent.

    Args:
        model: Gemini model id.
        tools: override tool list.
        sub_agents: sub-agent graph edges.
        name: agent name.

    Returns:
        Configured ``LlmAgent`` — no network on construction.
    """
    return LlmAgent(
        name=name,
        model=model,
        description=INGESTION_DESCRIPTION,
        instruction=INGESTION_INSTRUCTION,
        tools=list(tools) if tools is not None else list(INGESTION_TOOLS),
        sub_agents=list(sub_agents or []),
        output_key="ingestion_state",
    )


ingestion_agent: LlmAgent = create_ingestion_agent()

__all__ = [
    "INGESTION_DESCRIPTION",
    "INGESTION_INSTRUCTION",
    "INGESTION_TOOLS",
    "INGESTION_TOOL_NAMES",
    "LlmAgent",
    "create_ingestion_agent",
    "ingestion_agent",
]
