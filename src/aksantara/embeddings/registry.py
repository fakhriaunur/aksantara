"""Local release registry with approval-bearing atomic pointer promotion.

Provides strict verification, approval-bearing CAS promotion, rollback,
history, and generation handling for the validated release pointer.
All operations are caller-owned, local-only, and preserve canonical/vector
history bytes across failures. Promotion/rollback use file-locked CAS
with opaque generation tokens for ABA safety.
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aksantara.embeddings.release import verify_release

__all__ = [
    "load_current",
    "load_history",
    "load_registry",
    "promote_release",
    "rollback_release",
    "next_generation",
    "ApprovalError",
    "ConflictError",
    "NotFoundError",
    "UnavailableError",
    "VerificationError",
]


class ApprovalError(ValueError):
    pass


class ConflictError(ValueError):
    pass


class NotFoundError(ValueError):
    pass


class UnavailableError(ValueError):
    pass


class VerificationError(ValueError):
    pass


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _lock_path(root: Path) -> Path:
    return Path(root).expanduser().resolve() / "registry" / ".registry.lock"


@contextmanager
def _registry_lock(root: Path):  # type: ignore[no-untyped-def]
    p = _lock_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch(exist_ok=True)
    try:
        import fcntl  # type: ignore[import-untyped]

        fd = p.open("r+")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
            yield fd
        finally:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            fd.close()
    except ImportError:
        # Fallback for non-POSIX (CI is POSIX)
        import threading

        _fallback_lock = threading.RLock()
        _fallback_lock.acquire()
        try:
            yield None
        finally:
            _fallback_lock.release()


def _current_path(root: Path) -> Path:
    return Path(root).expanduser().resolve() / "registry" / "current.json"


def _history_path(root: Path) -> Path:
    return Path(root).expanduser().resolve() / "registry" / "history.json"


def _audit_path(root: Path) -> Path:
    return Path(root).expanduser().resolve() / "registry" / "audit.json"


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def next_generation(current_gen: str) -> str:
    """Increment opaque generation token ABA-safe: gen-N -> gen-(N+1)."""
    try:
        if current_gen.startswith("gen-"):
            n = int(current_gen.split("-", 1)[1])
            return f"gen-{n + 1}"
    except Exception:
        pass
    # fallback: hash
    return f"gen-{int(time.time() * 1000) % 1000000}"


def load_current(root: Path) -> dict[str, Any] | None:
    val = _read_json(_current_path(root), None)
    return val  # type: ignore[no-any-return]


def load_history(root: Path) -> dict[str, Any]:
    data = _read_json(_history_path(root), None)
    if data is None:
        return {"releases": [], "events": []}
    if "releases" not in data:
        data["releases"] = []  # type: ignore[assignment]
    if "events" not in data:
        data["events"] = []  # type: ignore[assignment]
    return data  # type: ignore[no-any-return]


def load_registry(root: Path) -> dict[str, Any]:
    return {"current": load_current(root), "history": load_history(root)}


def _validate_approval(
    approval: dict[str, Any] | None, target_manifest_hash: str | None
) -> None:
    if not approval:
        raise ApprovalError("human approval required: approval missing")
    reviewer = approval.get("reviewer") or approval.get("actor") or ""
    reason = approval.get("reason") or ""
    policy = approval.get("policy") or approval.get("policy_version") or ""
    target = approval.get("target_manifest_hash") or approval.get("manifest_hash") or ""
    if not reviewer or not isinstance(reviewer, str) or not reviewer.strip():
        raise ApprovalError("approval reviewer/actor required")
    if not reason or not isinstance(reason, str) or not reason.strip():
        raise ApprovalError("approval reason required")
    if not policy or not isinstance(policy, str) or not policy.strip():
        raise ApprovalError("approval policy required")
    if target_manifest_hash and target and target != target_manifest_hash:
        raise ApprovalError(
            f"approval target_manifest_hash {target!r} != candidate {target_manifest_hash!r}"
        )
    if not target and target_manifest_hash:
        # require explicit target hash when candidate exists
        raise ApprovalError("approval target_manifest_hash required")


def _record_audit(root: Path, audit: dict[str, Any]) -> None:
    path = _audit_path(root)
    existing: list[Any] = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                existing = data
            elif isinstance(data, dict) and isinstance(data.get("events"), list):
                existing = data["events"]
        except Exception:
            existing = []
    existing.append(audit)
    _atomic_write(path, existing)


def promote_release(
    root: Path,
    candidate_version: str,
    *,
    expected_version: str,
    expected_generation: str,
    approval: dict[str, Any],
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Atomic approval-bearing CAS promotion."""
    root = Path(root).expanduser().resolve()
    op_id = operation_id or f"promote-{candidate_version}-{int(time.time() * 1000)}"
    with _registry_lock(root):
        current = load_current(root)
        history = load_history(root)
        # Existence checks
        if current is None:
            err = {"error": "no current pointer", "type": "not_found"}
            _record_audit(
                root,
                {
                    "operation_id": op_id,
                    "type": "promote_failed",
                    "reason": "no_current",
                    "candidate": candidate_version,
                    "at": _iso_now(),
                },
            )
            return {"success": False, "status": 404, "error": err, "current": None}
        cur_ver = current.get("version")
        cur_gen = current.get("generation")
        # Idempotency: same operation_id already applied -> idempotent success
        for ev in history.get("events", []):
            if (
                ev.get("operation_id") == op_id
                and ev.get("to_version") == candidate_version
            ):
                return {
                    "success": True,
                    "idempotent": True,
                    "from_version": ev.get("from_version"),
                    "to_version": candidate_version,
                    "generation": ev.get("generation"),
                    "operation_id": op_id,
                    "current": load_current(root),
                }
        # CAS check: version + generation must match (ABA-safe)
        if cur_ver != expected_version or cur_gen != expected_generation:  # type: ignore[no-redef]
            err_cas: dict[str, Any] = {  # type: ignore[no-redef]
                "error": "conflict: expected version/generation mismatch",
                "type": "conflict",
                "expected_version": expected_version,
                "expected_generation": expected_generation,
                "current_version": cur_ver,
                "current_generation": cur_gen,
            }
            _record_audit(
                root,
                {
                    "operation_id": op_id,
                    "type": "promote_failed",
                    "reason": "stale_cas",
                    "detail": err_cas,
                    "at": _iso_now(),
                },
            )
            return {
                "success": False,
                "status": 409,
                "error": err_cas,
                "current": current,
            }  # type: ignore[return-value]
        # Verify candidate strictly before promotion
        # Check store availability: if release missing, unavailable
        manifest_path = root / "releases" / f"{candidate_version}.json"
        if not manifest_path.exists():
            err2: dict[str, Any] = {
                "error": f"candidate manifest not found: {candidate_version}",
                "type": "not_found",
            }  # type: ignore[typeddict-item]
            _record_audit(
                root,
                {
                    "operation_id": op_id,
                    "type": "promote_failed",
                    "reason": "candidate_not_found",
                    "candidate": candidate_version,
                    "at": _iso_now(),
                },
            )
            return {"success": False, "status": 404, "error": err2, "current": current}  # type: ignore[return-value]
        ver = verify_release(root, candidate_version)
        if not ver.get("valid") or not ver.get("eligible"):
            err3: dict[str, Any] = {
                "error": "candidate verification failed",
                "type": "verification_failed",
                "detail": ver,
            }
            _record_audit(
                root,
                {
                    "operation_id": op_id,
                    "type": "promote_failed",
                    "reason": "verification_failed",
                    "detail": ver,
                    "at": _iso_now(),
                },
            )
            return {"success": False, "status": 422, "error": err3, "current": current}  # type: ignore[return-value]
        manifest_hash = ver.get("manifestHash") or ver.get("manifest_hash") or ""
        # Approval check
        try:
            _validate_approval(approval, manifest_hash)
        except ApprovalError as exc:
            err = {"error": str(exc), "type": "approval_required"}
            _record_audit(
                root,
                {
                    "operation_id": op_id,
                    "type": "promote_failed",
                    "reason": "approval",
                    "detail": str(exc),
                    "at": _iso_now(),
                },
            )
            return {"success": False, "status": 422, "error": err, "current": current}
        # Approval target hash already validated; also ensure approval target matches manifest hash
        approval_target = (
            approval.get("target_manifest_hash") or approval.get("manifest_hash") or ""
        )
        if approval_target and approval_target != manifest_hash:
            err = {
                "error": "approval target_manifest_hash mismatch",
                "type": "approval_required",
                "candidate_hash": manifest_hash,
                "approval_hash": approval_target,
            }
            _record_audit(
                root,
                {
                    "operation_id": op_id,
                    "type": "promote_failed",
                    "reason": "approval_hash_mismatch",
                    "detail": err,
                    "at": _iso_now(),
                },
            )
            return {"success": False, "status": 422, "error": err, "current": current}
        # All checks passed -> atomic pointer change
        new_gen = next_generation(cur_gen) if cur_gen else "gen-1"
        new_current = {
            "version": candidate_version,
            "generation": new_gen,
            "updated_at": _iso_now(),
            "fence": hashlib.sha256(f"{op_id}{new_gen}".encode()).hexdigest()[:16],
        }
        # Ensure releases entry
        releases = history.get("releases", [])
        if not any(r.get("version") == candidate_version for r in releases):
            releases.append(
                {
                    "version": candidate_version,
                    "manifestHash": manifest_hash,
                    "status": "validated",
                }
            )
        # Append event
        event = {
            "type": "promote",
            "from_version": str(cur_ver),
            "to_version": str(candidate_version),
            "generation": str(new_gen),
            "expected_generation": str(expected_generation),
            "operation_id": str(op_id),
            "manifest_hash": str(manifest_hash),
            "approval": {
                "reviewer": str(
                    approval.get("reviewer") or approval.get("actor") or ""
                ),
                "reason": str(approval.get("reason") or ""),
                "policy": str(
                    approval.get("policy") or approval.get("policy_version") or ""
                ),
                "target_manifest_hash": str(manifest_hash),
            },
            "timestamp": _iso_now(),
        }
        history["releases"] = releases
        history.setdefault("events", []).append(event)
        # Atomic writes
        _atomic_write(_current_path(root), new_current)
        _atomic_write(_history_path(root), history)
        return {
            "success": True,
            "from_version": cur_ver,
            "to_version": candidate_version,
            "generation": new_gen,
            "expected_generation": expected_generation,
            "operation_id": op_id,
            "current": new_current,
            "event": event,
            "manifest_hash": manifest_hash,
        }


def rollback_release(
    root: Path,
    target_version: str,
    *,
    expected_version: str,
    expected_generation: str,
    approval: dict[str, Any],
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Re-verified rollback: changes only pointer plus one append-only event."""
    root = Path(root).expanduser().resolve()
    op_id = operation_id or f"rollback-{target_version}-{int(time.time() * 1000)}"
    with _registry_lock(root):
        current = load_current(root)
        history = load_history(root)
        if current is None:
            err = {"error": "no current pointer", "type": "not_found"}
            _record_audit(
                root,
                {
                    "operation_id": op_id,
                    "type": "rollback_failed",
                    "reason": "no_current",
                    "at": _iso_now(),
                },
            )
            return {"success": False, "status": 404, "error": err, "current": None}
        cur_ver = current.get("version")
        cur_gen = current.get("generation")
        # Idempotency: already at target -> idempotent no-op
        if cur_ver == target_version:
            # Check if last event already rolled back to target with same expected
            # idempotent if same operation_id already recorded
            for ev in history.get("events", []):
                if (
                    ev.get("operation_id") == op_id
                    and ev.get("to_version") == target_version
                ):
                    return {
                        "success": True,
                        "idempotent": True,
                        "from_version": ev.get("from_version"),
                        "to_version": target_version,
                        "generation": ev.get("generation"),
                        "operation_id": op_id,
                        "current": current,
                    }
            # If already at target but no prior op, treat as idempotent no-op (no second event)
            if cur_gen == expected_generation:
                # same generation means retry of same rollback?
                return {
                    "success": True,
                    "idempotent": True,
                    "from_version": cur_ver,
                    "to_version": target_version,
                    "generation": cur_gen,
                    "operation_id": op_id,
                    "current": current,
                }
            # If already current, but caller expected different version/generation, it's still idempotent if target is current
            # BUT spec says repeat already-current rollback is no-op/idempotent
            return {
                "success": True,
                "idempotent": True,
                "from_version": cur_ver,
                "to_version": target_version,
                "generation": cur_gen,
                "operation_id": op_id,
                "current": current,
            }
        # CAS check
        if cur_ver != expected_version or cur_gen != expected_generation:  # type: ignore[no-redef]
            err_rb_cas: dict[str, Any] = {
                "error": "conflict: expected version/generation mismatch",
                "type": "conflict",
                "expected_version": expected_version,
                "expected_generation": expected_generation,
                "current_version": cur_ver,
                "current_generation": cur_gen,
            }
            _record_audit(
                root,
                {
                    "operation_id": op_id,
                    "type": "rollback_failed",
                    "reason": "stale_cas",
                    "detail": err_rb_cas,
                    "at": _iso_now(),
                },
            )
            return {
                "success": False,
                "status": 409,
                "error": err_rb_cas,
                "current": current,
            }  # type: ignore[return-value]
        # Verify target exists and is validated
        found = False
        target_hash = None
        for r in history.get("releases", []):
            if r.get("version") == target_version:
                if r.get("status") not in ("validated", "current"):
                    err_rb_val: dict[str, Any] = {
                        "error": f"target {target_version} not validated",
                        "type": "verification_failed",
                    }
                    _record_audit(
                        root,
                        {
                            "operation_id": op_id,
                            "type": "rollback_failed",
                            "reason": "not_validated",
                            "at": _iso_now(),
                        },
                    )
                    return {
                        "success": False,
                        "status": 422,
                        "error": err_rb_val,
                        "current": current,
                    }  # type: ignore[return-value]
                found = True
                target_hash = r.get("manifestHash") or r.get("manifest_hash")
                break
        if not found:
            err_rb: dict[str, Any] = {
                "error": f"target release not in validated history: {target_version}",
                "type": "not_found",
            }
            _record_audit(
                root,
                {
                    "operation_id": op_id,
                    "type": "rollback_failed",
                    "reason": "target_not_in_history",
                    "at": _iso_now(),
                },
            )
            return {
                "success": False,
                "status": 404,
                "error": err_rb,
                "current": current,
            }  # type: ignore[return-value]
        # Re-verify exact target strictly (side-effect-free)
        manifest_path = root / "releases" / f"{target_version}.json"
        if not manifest_path.exists():
            err_rb2: dict[str, Any] = {
                "error": f"target manifest not found: {target_version}",
                "type": "not_found",
            }
            _record_audit(
                root,
                {
                    "operation_id": op_id,
                    "type": "rollback_failed",
                    "reason": "target_manifest_missing",
                    "at": _iso_now(),
                },
            )
            return {
                "success": False,
                "status": 404,
                "error": err_rb2,
                "current": current,
            }  # type: ignore[return-value]
        ver = verify_release(root, target_version)
        if not ver.get("valid") or not ver.get("eligible"):
            err_rb3: dict[str, Any] = {
                "error": "target verification failed",
                "type": "verification_failed",
                "detail": ver,
            }
            _record_audit(
                root,
                {
                    "operation_id": op_id,
                    "type": "rollback_failed",
                    "reason": "verification_failed",
                    "detail": ver,
                    "at": _iso_now(),
                },
            )
            return {
                "success": False,
                "status": 422,
                "error": err_rb3,
                "current": current,
            }  # type: ignore[return-value]
        manifest_hash = (
            ver.get("manifestHash") or ver.get("manifest_hash") or target_hash
        )
        # Approval check for rollback
        try:
            _validate_approval(approval, manifest_hash)  # type: ignore[arg-type]
        except ApprovalError as exc:
            err_rb4: dict[str, Any] = {"error": str(exc), "type": "approval_required"}
            _record_audit(
                root,
                {
                    "operation_id": op_id,
                    "type": "rollback_failed",
                    "reason": "approval",
                    "detail": str(exc),
                    "at": _iso_now(),
                },
            )
            return {
                "success": False,
                "status": 422,
                "error": err_rb4,
                "current": current,
            }  # type: ignore[return-value]
        # Atomic pointer change only
        new_gen = next_generation(cur_gen) if cur_gen else "gen-1"
        new_current = {
            "version": target_version,
            "generation": new_gen,
            "updated_at": _iso_now(),
            "fence": hashlib.sha256(f"{op_id}{new_gen}".encode()).hexdigest()[:16],
        }
        event = {
            "type": "rollback",
            "from_version": str(cur_ver),
            "to_version": str(target_version),
            "generation": str(new_gen),
            "expected_generation": str(expected_generation),
            "operation_id": str(op_id),
            "manifest_hash": str(manifest_hash),
            "approval": {
                "reviewer": str(
                    approval.get("reviewer") or approval.get("actor") or ""
                ),
                "reason": str(approval.get("reason") or ""),
                "policy": str(
                    approval.get("policy") or approval.get("policy_version") or ""
                ),
                "target_manifest_hash": str(manifest_hash),
            },
            "timestamp": _iso_now(),
        }
        history.setdefault("events", []).append(event)
        _atomic_write(_current_path(root), new_current)
        _atomic_write(_history_path(root), history)
        return {
            "success": True,
            "from_version": cur_ver,
            "to_version": target_version,
            "generation": new_gen,
            "expected_generation": expected_generation,
            "operation_id": op_id,
            "current": new_current,
            "event": event,
            "manifest_hash": manifest_hash,
        }
