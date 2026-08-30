"""Retrieval/Normalization agent (ADK 2.8.0 LlmAgent stub).

Bounded, least-privilege agent that builds embedding documents, proposes
anomaly mappings, and evaluates retrieval. It CANNOT promote proposals to
canonical truth — promotion requires human review and the validator gate.

Graph definition only — no live LLM call on import. Deterministic helpers
(``build_embedding_document``, ``render_citation``) are called as tools; any
generation step is grounded only in retrieved canonical records.
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
# Bounded tools — retrieval may build docs, propose, and evaluate, but not
# promote to canonical or write raw snapshots.
# ---------------------------------------------------------------------------


def _tool_build_embedding_document(lema: str) -> dict[str, Any]:
    """Retrieval tool: build compact embedding document for a canonical entry.

    Delegates to ``aksantara.embeddings.document.build_embedding_document``.
    No raw HTML is ever included; deterministic and reviewable.
    """
    return {
        "lema": lema,
        "document_fields": [
            "Lema",
            "Ejaan",
            "Kelas Kata",
            "Makna",
            "Contoh",
            "Bentuk Tidak Baku",
        ],
    }


def _tool_propose_anomaly(entry_id: str, note: str) -> dict[str, Any]:
    """Retrieval tool: propose an anomaly mapping for human review.

    AI proposals never mutate canonical truth; they enter the review queue.
    """
    return {
        "entry_id": entry_id,
        "proposal": note,
        "status": "pending_review",
        "canonical_mutation": False,
    }


def _tool_evaluate_retrieval(
    query: str, mode: str, distance: float | None = None
) -> dict[str, Any]:
    """Retrieval tool: evaluate a retrieval hit (citation, threshold, fail-closed).

    Used to verify that unknown queries return no authoritative result and
    that weak semantic hits are gated out.
    """
    return {
        "query": query,
        "mode": mode,
        "distance": distance,
        "fail_closed": distance is None or distance < 0.70,
    }


RETRIEVAL_TOOLS: list[Any] = [
    _tool_build_embedding_document,
    _tool_propose_anomaly,
    _tool_evaluate_retrieval,
]

RETRIEVAL_TOOL_NAMES: list[str] = [
    getattr(t, "__name__", str(t)) for t in RETRIEVAL_TOOLS
]

RETRIEVAL_INSTRUCTION: str = """You are the Retrieval/Normalization agent for Aksantara.

Responsibilities:
- Build embedding documents via build_embedding_document (deterministic, no raw HTML).
- Embed with gemini-embedding-001 (768d, RETRIEVAL_DOCUMENT/RETRIEVAL_QUERY) and store via EmbeddingStore.
- Evaluate retrieval: exact → prefix → semantic order; fail-closed on unknown/weak queries.
- Propose anomaly mappings (e.g., Pebruari→Februari) for human review; never promote to canonical.

Constraints:
- Grounded generation only from retrieved canonical records (gemini-3.7-flash with citations).
- Do not mutate entries/, vector_entries/, or config/current_version directly.
- Do not invent lemmas, meanings, or standard forms; every answer carries source {url, edition, contentHash} and retrieval {mode, distance}.
"""

RETRIEVAL_DESCRIPTION: str = "Retrieval/Normalization — embedding docs, anomaly proposals, retrieval evaluation (cannot promote to canonical)."


def create_retrieval_agent(
    *,
    model: str = "gemini-3.7-flash",
    tools: list[Any] | None = None,
    sub_agents: list[Any] | None = None,
    name: str = "retrieval_normalization",
) -> LlmAgent:
    """Create the Retrieval/Normalization LlmAgent.

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
        description=RETRIEVAL_DESCRIPTION,
        instruction=RETRIEVAL_INSTRUCTION,
        tools=list(tools) if tools is not None else list(RETRIEVAL_TOOLS),
        sub_agents=list(sub_agents or []),
        output_key="retrieval_state",
    )


retrieval_agent: LlmAgent = create_retrieval_agent()

__all__ = [
    "RETRIEVAL_DESCRIPTION",
    "RETRIEVAL_INSTRUCTION",
    "RETRIEVAL_TOOLS",
    "RETRIEVAL_TOOL_NAMES",
    "LlmAgent",
    "create_retrieval_agent",
    "retrieval_agent",
]
