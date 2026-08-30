"""Lead Orchestrator agent (ADK 2.8.0 LlmAgent stub).

Bounded, least-privilege orchestrator that owns run lifecycle, policy gates,
retries, manifests, and escalation. It CANNOT mutate lexical values — only
canonical writers (Ingestion) may assert KBBI fields.

Graph definition only — no live LLM call is issued during unit tests. The
agent is instantiated via ``create_lead_agent``; import this module and
inspect ``lead_agent`` to verify wiring without credentials.

ADK availability
----------------
When ``google-adk`` is installed the real ``LlmAgent`` is used; otherwise a
compatible shim is provided so ``pytest`` without GCP dependencies still
passes.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Shim for ADK LlmAgent when google-adk is not installed in the test env.
# ---------------------------------------------------------------------------

try:
    from google.adk.agents import LlmAgent  # type: ignore[import-untyped]
except Exception:  # pragma: no cover — fallback shim

    class LlmAgent:  # type: ignore[no-redef]
        """Minimal shim matching the ADK 2.8.0 constructor surface used here."""

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
# Bounded tools — lead orchestrator may NOT mutate lexical KBBI fields.
# ---------------------------------------------------------------------------


def _tool_validate_manifest_inputs(version: str, entries_count: int) -> dict[str, Any]:
    """Lead tool: validate manifest inputs before publication."""
    if not version:
        raise ValueError("version is required")
    if entries_count < 0:
        raise ValueError("entries_count must be >= 0")
    return {"ok": True, "version": version, "entries_count": entries_count}


def _tool_escalate_if_blocked(reason: str, details: str = "") -> dict[str, Any]:
    """Lead tool: escalate when a stop condition is triggered."""
    return {
        "escalated": True,
        "reason": reason,
        "details": details,
        "next_step": "human_review",
    }


def _tool_flip_version_pointer(version: str) -> dict[str, Any]:
    """Lead tool: atomic pointer flip for rollback / release.

    The actual Firestore write is performed by the caller-provided
    ``FirestoreVectorStore``/config adapter; this tool only records the
    intent so the LLM must not invent a version.
    """
    return {"pointer": "config/current_version", "version": version, "atomic": True}


LEAD_TOOLS: list[Any] = [
    _tool_validate_manifest_inputs,
    _tool_escalate_if_blocked,
    _tool_flip_version_pointer,
]

# Keep a name-index for introspection / tests.
LEAD_TOOL_NAMES: list[str] = [getattr(t, "__name__", str(t)) for t in LEAD_TOOLS]


LEAD_INSTRUCTION: str = """You are the Lead Orchestrator for Aksantara.

Responsibilities:
- Own run lifecycle: plan slices, gate checkouts, enforce ValidationPolicy.
- Approve or block publication; quarantine on conflict, mismatch, or unknown semantic overreach.
- Never mutate lexical fields (lema, makna, kelas_kata, contoh, bentuk_tidak_baku) — only Ingestion may assert them from KBBI.
- Escalate to human review when authority is unestablishable, parser non-deterministic, or embedding hash mismatches.
- Call tools for manifest validation and version-pointer flips; do not invent versions, hashes, or citations.

Fail-closed, provenance-preserving, and reviewable. If blocked, report the gap instead of guessing.
"""

LEAD_DESCRIPTION: str = "Lead Orchestrator — lifecycle, policy gates, manifests, escalation (cannot mutate lexical values)."


def create_lead_agent(
    *,
    model: str = "gemini-3.7-flash",
    tools: list[Any] | None = None,
    sub_agents: list[Any] | None = None,
    name: str = "lead_orchestrator",
) -> LlmAgent:
    """Create the Lead Orchestrator LlmAgent.

    Args:
        model: Gemini model id (default gemini-3.7-flash, no retirement announced).
        tools: override tool list (for tests).
        sub_agents: optional sub-agent graph edges (kept explicit).
        name: agent name.

    Returns:
        Configured ``LlmAgent`` — no network call on construction.
    """
    return LlmAgent(
        name=name,
        model=model,
        description=LEAD_DESCRIPTION,
        instruction=LEAD_INSTRUCTION,
        tools=list(tools) if tools is not None else list(LEAD_TOOLS),
        sub_agents=list(sub_agents or []),
        output_key="lead_state",
    )


# Default singleton for import-time graph inspection.
lead_agent: LlmAgent = create_lead_agent()

__all__ = [
    "LEAD_DESCRIPTION",
    "LEAD_INSTRUCTION",
    "LEAD_TOOLS",
    "LEAD_TOOL_NAMES",
    "LlmAgent",
    "create_lead_agent",
    "lead_agent",
]
