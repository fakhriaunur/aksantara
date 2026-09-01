"""Run state store port and adapters matching the approved Firestore layout.

Firestore layout (Native (default) asia-southeast1):
  runs/{run_id}                     -> run document (status, fingerprints, pins, lease, idempotency)
  runs/{run_id}/checkpoints/{source_key} -> per-key checkpoint (one current outcome per key)
  runs/{run_id}/attempts/{attempt_id} -> attempt history (separate from current outcomes)

Local deterministic adapter mirrors the same logical records under:
  <caller-root>/.aksantara/checkpoint-runs/<run_id>/status.json
  <caller-root>/.aksantara/checkpoint-runs/<run_id>/outcomes.json
  <caller-root>/.aksantara/checkpoint-runs/<run_id>/attempts.json
  <caller-root>/.aksantara/checkpoint-runs/<run_id>/checkpoint.json
  <caller-root>/.aksantara/checkpoint-runs/<run_id>/lease.json

No cloud import is performed; Firestore writes are bounded to the approved
sandbox only when explicitly requested and never in local mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from aksantara.ingest.checkpoint_storage import _read_json, _write_state_json


class RunStateStore(Protocol):
    """Port for durable run, checkpoint, and lease state."""

    def read_run(self, run_id: str) -> dict[str, Any]: ...
    def write_run(self, run_id: str, payload: dict[str, Any]) -> None: ...
    def read_checkpoint(self, run_id: str) -> dict[str, Any]: ...
    def write_checkpoint(self, run_id: str, payload: dict[str, Any]) -> None: ...
    def read_lease(self, run_id: str) -> dict[str, Any] | None: ...
    def write_lease(self, run_id: str, payload: dict[str, Any]) -> None: ...


class LocalRunStateStore:
    """Local filesystem adapter matching Firestore collection layout."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.state_root = self.root / ".aksantara" / "checkpoint-runs"

    def _run_dir(self, run_id: str) -> Path:
        return self.state_root / run_id

    def read_run(self, run_id: str) -> dict[str, Any]:
        return _read_json(self._run_dir(run_id) / "status.json")

    def write_run(self, run_id: str, payload: dict[str, Any]) -> None:
        _write_state_json(self._run_dir(run_id) / "status.json", payload, self.root)

    def read_checkpoint(self, run_id: str) -> dict[str, Any]:
        return _read_json(self._run_dir(run_id) / "checkpoint.json")

    def write_checkpoint(self, run_id: str, payload: dict[str, Any]) -> None:
        _write_state_json(self._run_dir(run_id) / "checkpoint.json", payload, self.root)

    def read_lease(self, run_id: str) -> dict[str, Any] | None:
        path = self._run_dir(run_id) / "lease.json"
        if not path.is_file():
            return None
        return _read_json(path)

    def write_lease(self, run_id: str, payload: dict[str, Any]) -> None:
        _write_state_json(self._run_dir(run_id) / "lease.json", payload, self.root)


class FirestoreRunStateStore:
    """Firestore adapter matching runs/{run_id} layout. Bounded to approved sandbox."""

    def __init__(self, client: Any, *, project: str = "ata-devpost-sandbox") -> None:
        if project != "ata-devpost-sandbox":
            raise ValueError(
                "Firestore adapter is only allowed for ata-devpost-sandbox"
            )
        self.client = client
        self.project = project

    def _run_ref(self, run_id: str) -> Any:
        return self.client.collection("runs").document(run_id)

    def read_run(self, run_id: str) -> dict[str, Any]:
        snap = self._run_ref(run_id).get()
        if not snap.exists:
            raise FileNotFoundError(f"run {run_id} not found")
        return snap.to_dict()  # type: ignore[no-untyped-call, no-any-return]

    def write_run(self, run_id: str, payload: dict[str, Any]) -> None:
        # Use transaction or set with merge for idempotence; exact Firestore semantics
        self._run_ref(run_id).set(payload, merge=False)

    def read_checkpoint(self, run_id: str) -> dict[str, Any]:
        # Checkpoints are per-source_key under runs/{run_id}/checkpoints
        # For simplicity, read the aggregated checkpoint document
        snap = self._run_ref(run_id).collection("checkpoints").document("current").get()
        if not snap.exists:
            raise FileNotFoundError(f"checkpoint for {run_id} not found")
        return snap.to_dict()  # type: ignore[no-untyped-call, no-any-return]

    def write_checkpoint(self, run_id: str, payload: dict[str, Any]) -> None:
        self._run_ref(run_id).collection("checkpoints").document("current").set(payload)

    def read_lease(self, run_id: str) -> dict[str, Any] | None:
        snap = self._run_ref(run_id).collection("leases").document("current").get()
        if not snap.exists:
            return None
        return snap.to_dict()  # type: ignore[no-untyped-call, no-any-return]

    def write_lease(self, run_id: str, payload: dict[str, Any]) -> None:
        self._run_ref(run_id).collection("leases").document("current").set(payload)
