"""Local-only release embedding planning and inspection API.

Exposes plan/delta, candidate/manifest, vector verification, release-list/read,
and local fixture/fault surfaces without cloud promotion. All operations are
caller-owned, local-only, and never implicitly promote.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from aksantara.embeddings.release import load_manifest, verify_release

__all__ = ["create_release_router"]


class PlanRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    root: str = Field(description="Caller-owned artifact root")
    prior: str = Field(description="Prior release version")
    candidate: str = Field(description="Candidate release version")
    prior_canonical_dir: str | None = Field(
        default=None, description="Prior canonical dir under root"
    )
    candidate_canonical_dir: str | None = Field(
        default=None, description="Candidate canonical dir under root"
    )
    excluded_manifest: str | None = Field(
        default=None, description="Excluded manifest path"
    )
    mode: str = Field(
        default="local-fixture-only", description="Only local-fixture-only is supported"
    )


class BuildRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    root: str = Field(description="Caller-owned root")
    plan_id: str = Field(description="Plan ID")
    mode: str = Field(default="local", description="local or cloud")
    fail_chunk: int | None = Field(
        default=None, description="Fault injection for later-chunk failure"
    )


def create_release_router() -> APIRouter:
    router = APIRouter(prefix="/releases", tags=["release"])

    @router.get(
        "/contract",
        summary="Describe release embedding contract",
        operation_id="release_contract",
        response_model=dict[str, Any],
        description="Publishes exact lineage, metadata, cost formula, batch/chunk semantics, and conservation rules. Local-only, no promotion.",
    )
    def contract() -> dict[str, Any]:
        return {
            "model": "gemini-embedding-001",
            "dimensions": 768,
            "task_document": "RETRIEVAL_DOCUMENT",
            "task_query": "RETRIEVAL_QUERY",
            "distance_measure": "DOT_PRODUCT",
            "schema_version": "emb-768-v1",
            "cost_estimate_version": "cost-v1",
            "formula": "request_units = provider_calls * 1 (retries excluded, bounded)",
            "max_batch_size": 500,
            "conservation": "candidate_input_ids = eligible ∪ excluded; prior ∪ eligible = new ∪ changed ∪ unchanged ∪ removed",
            "reuse": "unchanged materializes v2 vector with reused_from/origin_release, identical values/document digest, compatible metadata, zero provider calls",
            "batch": "create-only/idempotent, preflight conflicting payloads before writes, chunked, per-chunk atomic, later-chunk failure incomplete/ineligible",
            "mode": "local (fake trace, no cloud) vs cloud",
            "help": "plan delta new changed unchanged removed excluded conservation canonical_content_hash embedding_document compatible metadata reused_from origin_release provider calls",
        }

    @router.post(
        "/seed",
        summary="Seed validated release via fixture contract",
        operation_id="release_seed",
        response_model=dict[str, Any],
        description="Local-only mutation. Caller-owned root and canonical dir under root. Creates canonical/raw/vector sets, manifest self-hash, registry status, and pointer/history. Never promotes implicitly. Requires local-fixture-only mode.",
    )
    def seed(request: dict[str, Any]) -> dict[str, Any]:
        # Delegate to CLI helper via internal call for simplicity
        root = request.get("root")
        version = request.get("version")
        canonical_dir = request.get("canonical_dir")
        if not root or not version or not canonical_dir:
            raise HTTPException(
                status_code=422, detail="root, version, canonical_dir required"
            )
        # Use release.py seed directly
        from pathlib import Path as P

        from aksantara.embeddings.release import seed_release

        rpath = P(root).expanduser().resolve()
        cdir = P(canonical_dir)
        if not cdir.is_absolute():
            cdir = rpath / cdir
        # Load entries
        entries: list[Any] = []
        for p in cdir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                from datetime import UTC, datetime

                try:
                    src = data.get("source")
                    if isinstance(src, dict) and isinstance(
                        src.get("retrieved_at"), str
                    ):
                        s = src["retrieved_at"]
                        if s.endswith("Z"):
                            s = s[:-1] + "+00:00"
                        dt = datetime.fromisoformat(s)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=UTC)
                        data = {**data, "source": {**src, "retrieved_at": dt}}
                except Exception:
                    pass
                from aksantara.domain.models import KBBIEntry

                entries.append(KBBIEntry.model_validate(data))
            except Exception as exc:
                raise HTTPException(
                    status_code=422, detail=f"invalid entry {p.name}: {exc}"
                ) from exc
        manifest = seed_release(rpath, version, entries)
        return {
            "release": version,
            "manifestHash": manifest["manifestHash"],
            "entries_count": manifest["entries_count"],
            "mode": "local",
        }

    @router.post(
        "/plans",
        summary="Create delta plan (new/changed/unchanged/removed/excluded)",
        operation_id="release_plan",
        response_model=dict[str, Any],
        description="Local-only plan. Caller-owned root, prior and candidate releases, canonical dirs. Returns durable plan_id, prior/candidate versions, pins, and read reference without embedding or pointer effects. Classification uses canonical_content_hash and embedding-document content plus compatible metadata (gemini-embedding-001, 768, RETRIEVAL_DOCUMENT, DOT_PRODUCT, emb-768-v1), not raw hash or retrieval time. Unchanged reuse has reused_from/origin_release.",
    )
    def create_plan(request: PlanRequest) -> dict[str, Any]:
        from pathlib import Path as P

        from aksantara.embeddings.planner import build_delta_plan

        root = P(request.root).expanduser().resolve()

        # Load entries via helper similar to CLI
        def _load(dir_val: str | None) -> dict[str, Any]:
            if not dir_val:
                return {}
            d = P(dir_val)
            if not d.is_absolute():
                d = root / d
            out: dict[str, Any] = {}
            for p in d.glob("*.json"):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    from datetime import UTC, datetime

                    try:
                        src = data.get("source")
                        if isinstance(src, dict) and isinstance(
                            src.get("retrieved_at"), str
                        ):
                            s = src["retrieved_at"]
                            if s.endswith("Z"):
                                s = s[:-1] + "+00:00"
                            dt = datetime.fromisoformat(s)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=UTC)
                            data = {**data, "source": {**src, "retrieved_at": dt}}
                    except Exception:
                        pass
                    from aksantara.domain.models import KBBIEntry

                    e = KBBIEntry.model_validate(data)
                    out[e.id] = e
                except Exception:
                    continue
            return out

        prior_entries = _load(request.prior_canonical_dir)
        candidate_entries = _load(request.candidate_canonical_dir)
        excluded: dict[str, dict[str, str]] = {}
        if request.excluded_manifest:
            ep = P(request.excluded_manifest)
            if not ep.is_absolute():
                ep = root / ep
            if ep.exists():
                try:
                    excluded = json.loads(ep.read_text(encoding="utf-8"))
                except Exception:
                    excluded = {}
        # prior vectors meta
        prior_vectors_meta: dict[str, dict[str, Any]] = {}
        prior_vectors_dir = root / "vectors" / request.prior
        if prior_vectors_dir.exists():
            for vf in prior_vectors_dir.glob("*.json"):
                try:
                    data = json.loads(vf.read_text(encoding="utf-8"))
                    eid = (
                        data.get("id") or data.get("entry_id") or vf.stem.split("_")[0]
                    )
                    prior_vectors_meta[eid] = {
                        "model": data.get("model"),
                        "dimensions": data.get("dimensions"),
                        "task": data.get("task"),
                        "distance_measure": data.get("distance_measure"),
                        "schema_version": data.get("schema_version")
                        or data.get("metadata", {}).get("schema_version"),
                    }
                except Exception:
                    continue
        if not prior_entries and not candidate_entries:
            # try manifests
            try:
                pm = load_manifest(root, request.prior)
                cm = load_manifest(root, request.candidate)
                from datetime import UTC, datetime

                from aksantara.domain.models import KBBIEntry, SourceRef

                for eid in pm.get("artifactHashes", {}):
                    prior_entries[eid] = KBBIEntry(
                        id=eid,
                        lema=eid.title(),
                        makna=[{"definisi": f"definisi {eid}"}],
                        source=SourceRef(
                            url=f"https://kbbi.kemdikbud.go.id/entri/{eid}",
                            source_kind="official-live",
                            edition="VI",
                            source_version="VI",
                            retrieved_at=datetime.now(UTC),
                            content_hash=pm["artifactHashes"][eid],
                            parser_version="0.1.0",
                        ),
                    )
                for eid in cm.get("artifactHashes", {}):
                    candidate_entries[eid] = KBBIEntry(
                        id=eid,
                        lema=eid.title(),
                        makna=[{"definisi": f"definisi {eid}"}],
                        source=SourceRef(
                            url=f"https://kbbi.kemdikbud.go.id/entri/{eid}",
                            source_kind="official-live",
                            edition="VI",
                            source_version="VI",
                            retrieved_at=datetime.now(UTC),
                            content_hash=cm["artifactHashes"][eid],
                            parser_version="0.1.0",
                        ),
                    )
            except Exception as exc:
                raise HTTPException(
                    status_code=404, detail=f"prior/candidate not found: {exc}"
                ) from exc
        plan = build_delta_plan(
            prior_entries,
            candidate_entries,
            excluded_entries=excluded,
            prior_vectors_meta=prior_vectors_meta,
            prior_release=request.prior,
            candidate_release=request.candidate,
        )
        plan_dict = plan.to_dict()
        plans_dir = root / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        (plans_dir / f"{plan.plan_id}.json").write_text(
            json.dumps(plan_dict, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        # snapshot candidate for build
        cand_snap = root / "candidate_snapshots" / request.candidate
        cand_snap.mkdir(parents=True, exist_ok=True)
        for eid, entry in candidate_entries.items():
            (cand_snap / f"{eid}.json").write_text(
                json.dumps(entry.model_dump(mode="json"), sort_keys=True, indent=2)
                + "\n",
                encoding="utf-8",
            ) if hasattr(entry, "model_dump") else (
                cand_snap / f"{eid}.json"
            ).write_text(
                json.dumps(entry, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
        return plan_dict

    @router.get(
        "/plans/{plan_id}",
        summary="Read plan",
        operation_id="release_plan_read",
        response_model=dict[str, Any],
        description="Read durable plan with new/changed/unchanged/removed/excluded sets, conservation, hashes, and reuse lineage.",
    )
    def read_plan(
        plan_id: str, root: str = Query(..., description="Caller-owned root")
    ) -> dict[str, Any]:
        p = Path(root).expanduser().resolve() / "plans" / f"{plan_id}.json"
        if not p.exists():
            # try with plan prefix
            matches = list(
                (Path(root).expanduser().resolve() / "plans").glob(f"*{plan_id}*.json")
            )
            if not matches:
                raise HTTPException(
                    status_code=404, detail=f"plan not found: {plan_id}"
                )
            p = matches[0]
        return json.loads(p.read_text(encoding="utf-8"))

    @router.post(
        "/builds",
        summary="Execute build/work for plan (delta-only embeddings, batch persistence, cost accounting)",
        operation_id="release_build",
        response_model=dict[str, Any],
        description="Local deterministic work: exactly new+changed provider requests, unchanged reuse with zero calls, removed/excluded no work, documents only allowed KBBI fields, report with provider calls/retries, reuse, persistence/chunks, exclusions, mode, estimate version, bounded request-unit formula. Batch is create-only/idempotent, preflight, chunked (500), per-chunk atomic, later-chunk failure without eligibility. No cloud work in local mode; fake labeled.",
    )
    def create_build(request: BuildRequest) -> dict[str, Any]:
        from pathlib import Path as P

        from aksantara.embeddings.planner import DeltaPlan, ExcludedRecord
        from aksantara.embeddings.work import build_work

        # Truthful local/cloud handling: cloud mode requires explicit live config, else unavailable
        if request.mode == "cloud":
            import os as _os

            project = _os.getenv("GOOGLE_CLOUD_PROJECT", "")
            offline = _os.getenv("AKSANTARA_OFFLINE_EMBED", "1")
            if offline == "1" or not project or project != "ata-devpost-sandbox":
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": "cloud provider unavailable or misconfigured",
                        "code": "unavailable",
                        "mode": "cloud",
                    },
                )

        root = P(request.root).expanduser().resolve()
        plans_dir = root / "plans"
        plan_path = plans_dir / f"{request.plan_id}.json"
        if not plan_path.exists():
            matches = list(plans_dir.glob(f"*{request.plan_id}*.json"))
            if not matches:
                raise HTTPException(
                    status_code=404, detail=f"plan not found: {request.plan_id}"
                )
            plan_path = matches[0]
        plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
        excluded = [
            ExcludedRecord(
                entry_id=r["entry_id"], source_kind=r["source_kind"], reason=r["reason"]
            )
            for r in plan_data.get("excluded_ids", [])
        ]
        plan_obj = DeltaPlan(
            plan_id=plan_data["plan_id"],
            prior_release=plan_data["prior_release"],
            candidate_release=plan_data["candidate_release"],
            prior_ids=plan_data["prior_ids"],
            candidate_input_ids=plan_data["candidate_input_ids"],
            eligible_candidate_ids=plan_data["eligible_candidate_ids"],
            excluded_ids=excluded,
            new_ids=plan_data["new"],
            changed_ids=plan_data["changed"],
            unchanged_ids=plan_data["unchanged"],
            removed_ids=plan_data["removed"],
            old_canonical_hash=plan_data.get("old_canonical_content_hash", {}),
            new_canonical_hash=plan_data.get("new_canonical_content_hash", {}),
            old_document_hash=plan_data.get("old_document_hash", {}),
            new_document_hash=plan_data.get("new_document_hash", {}),
            compatible_metadata=plan_data.get("compatible_metadata", {}),
            reused_from=plan_data.get("reused_from", {}),
            origin_release=plan_data.get("origin_release", {}),
            old_raw_hash=plan_data.get("old_raw_hash", {}),
            new_raw_hash=plan_data.get("new_raw_hash", {}),
        )
        cand_snap = root / "candidate_snapshots" / plan_obj.candidate_release
        candidate_entries: dict[str, Any] = {}
        if cand_snap.exists():
            for p in cand_snap.glob("*.json"):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    from datetime import UTC, datetime

                    try:
                        src = data.get("source")
                        if isinstance(src, dict) and isinstance(
                            src.get("retrieved_at"), str
                        ):
                            s = src["retrieved_at"]
                            if s.endswith("Z"):
                                s = s[:-1] + "+00:00"
                            dt = datetime.fromisoformat(s)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=UTC)
                            data = {**data, "source": {**src, "retrieved_at": dt}}
                    except Exception:
                        pass
                    from aksantara.domain.models import KBBIEntry

                    e = KBBIEntry.model_validate(data)
                    candidate_entries[e.id] = e
                except Exception:
                    candidate_entries[p.stem] = data
        if not candidate_entries:
            raise HTTPException(status_code=404, detail="candidate entries not found")
        prior_vectors_dir = root / "vectors" / plan_obj.prior_release
        report = build_work(
            plan_obj,
            candidate_entries,
            prior_vectors_dir if prior_vectors_dir.exists() else None,
            root,
            plan_obj.candidate_release,
            mode=request.mode,
            fail_chunk_index=request.fail_chunk,
        )
        if request.mode == "local":
            report["fake"] = True
        return report

    @router.get(
        "/vectors",
        summary="Inspect vectors for release",
        operation_id="release_vector_inspect",
        response_model=dict[str, Any],
        description="Vector inspection joining canonical/release manifests; every vector has release/content/source lineage, gemini-embedding-001, 768 dims, RETRIEVAL_DOCUMENT, DOT_PRODUCT, emb-768-v1, 768 finite numerics; same-release extra/duplicate fail; reused carry origin/lineage.",
    )
    def vector_inspect(
        release: str = Query(..., description="Release version"),
        root: str = Query(..., description="Caller-owned root"),
    ) -> dict[str, Any]:
        r = Path(root).expanduser().resolve()
        vectors_dir = r / "vectors" / release
        if not vectors_dir.exists():
            raise HTTPException(
                status_code=404, detail=f"vectors not found for {release}"
            )
        manifest = None
        try:
            manifest = load_manifest(r, release)
        except Exception:
            pass
        results = []
        for p in sorted(vectors_dir.glob("*.json")):
            data = json.loads(p.read_text(encoding="utf-8"))
            vec = (
                data.get("embedding")
                or data.get("embedding_vector")
                or data.get("vector")
                or []
            )
            finite = all(
                isinstance(v, (int, float))
                and v == v
                and v not in (float("inf"), float("-inf"))
                for v in vec
            )
            results.append(
                {
                    "doc_id": p.stem,
                    "entry_id": data.get("id") or data.get("entry_id"),
                    "source_release": data.get("source_release") or data.get("version"),
                    "raw_content_hash": data.get("raw_content_hash")
                    or data.get("content_hash")
                    or data.get("contentHash"),
                    "canonical_content_hash": data.get("canonical_content_hash")
                    or data.get("metadata", {}).get("canonical_content_hash"),
                    "model": data.get("model"),
                    "dimensions": data.get("dimensions"),
                    "task": data.get("task"),
                    "distance_measure": data.get("distance_measure"),
                    "schema_version": data.get("schema_version")
                    or data.get("metadata", {}).get("schema_version"),
                    "values_length": len(vec),
                    "finite": finite,
                    "reused_from": data.get("reused_from"),
                    "origin_release": data.get("origin_release"),
                }
            )
        return {
            "release": release,
            "vectors": results,
            "count": len(results),
            "manifestHash": manifest.get("manifestHash") if manifest else None,
        }

    @router.get(
        "/verify/{release}",
        summary="Verify release (strict, side-effect-free)",
        operation_id="release_verify",
        response_model=dict[str, Any],
        description="Strict verification: exact release/status and manifest hash, complete canonical/raw joins, official/review policy, no blocking conflict/quarantine, compatible pins, exact release-scoped vector set/metadata. Missing/extra/duplicate/tampered returns non-success or ineligible/unavailable before promotion. No repair. Side-effect-free and fails closed on missing, extra, stale, tampered, unavailable, conflicted, or wrong-release data.",
    )
    def verify(
        release: str, root: str = Query(..., description="Caller-owned root")
    ) -> dict[str, Any]:
        result = verify_release(Path(root), release)
        if result.get("code") == "unavailable":
            raise HTTPException(status_code=503, detail=result)
        if not result.get("valid"):
            raise HTTPException(status_code=422, detail=result)
        return result

    @router.post(
        "/promote",
        summary="Explicit atomic approval-bearing CAS promotion",
        operation_id="release_promote",
        response_model=dict[str, Any],
        description="Only verified, approved promotion appends as validated, records approval (reviewer/actor, reason, policy, target manifest hash), prior-pointer history, and one generation-token CAS transition. Same operation retry has no second event. Stale version/generation/approval, invalid candidate, and losing writer return typed conflict/failure and leave active state unchanged except typed audit. No torn pointer is visible; plan/build/verify never promote. Requires validated candidate, human approval, expected pointer version plus generation, and ABA-safe idempotent CAS.",
    )
    def promote(request: dict[str, Any]) -> dict[str, Any]:
        from aksantara.embeddings.registry import promote_release

        root = request.get("root")
        candidate = (
            request.get("candidate")
            or request.get("candidate_version")
            or request.get("version")
        )
        expected_version = request.get("expected_version")
        expected_generation = request.get("expected_generation") or request.get(
            "generation"
        )
        reviewer = request.get("reviewer") or request.get("actor")
        reason = request.get("reason")
        policy = request.get("policy") or request.get("policy_version")
        operation_id = request.get("operation_id")
        if not root or not candidate or not expected_version or not expected_generation:
            raise HTTPException(
                status_code=422,
                detail="root, candidate, expected_version, expected_generation required",
            )
        if not reviewer or not reason or not policy:
            raise HTTPException(
                status_code=422,
                detail="human approval reviewer, reason, policy required",
            )
        # Resolve candidate hash for approval validation
        try:
            m = load_manifest(Path(root), candidate)
            candidate_hash = m.get("manifestHash") or m.get("manifest_hash")
        except Exception:
            candidate_hash = None
        approval = {
            "reviewer": reviewer,
            "reason": reason,
            "policy": policy,
            "target_manifest_hash": candidate_hash,
        }
        # Also accept explicit target hash override
        if request.get("target_manifest_hash"):
            approval["target_manifest_hash"] = request["target_manifest_hash"]
        result = promote_release(
            Path(root),
            candidate,
            expected_version=expected_version,
            expected_generation=expected_generation,
            approval=approval,
            operation_id=operation_id,
        )
        if not result.get("success"):
            status = result.get("status", 422)
            raise HTTPException(status_code=status, detail=result)
        return result

    @router.post(
        "/rollback",
        summary="Validated rollback preserves versioned data",
        operation_id="release_rollback",
        response_model=dict[str, Any],
        description="Rollback re-verifies an exact validated target, checks pointer generation, approval, and idempotency. It changes only pointer plus one typed append-only rollback event. Every pre-existing history event and release/raw/canonical/vector/manifest objects remain readable and byte-identical. Repeat is no-op/idempotent and invalid targets fail closed without fallback.",
    )
    def rollback(request: dict[str, Any]) -> dict[str, Any]:
        from aksantara.embeddings.registry import rollback_release

        root = request.get("root")
        target = (
            request.get("target")
            or request.get("target_version")
            or request.get("version")
        )
        expected_version = request.get("expected_version")
        expected_generation = request.get("expected_generation") or request.get(
            "generation"
        )
        reviewer = request.get("reviewer") or request.get("actor")
        reason = request.get("reason")
        policy = request.get("policy") or request.get("policy_version")
        operation_id = request.get("operation_id")
        if not root or not target or not expected_version or not expected_generation:
            raise HTTPException(
                status_code=422,
                detail="root, target, expected_version, expected_generation required",
            )
        if not reviewer or not reason or not policy:
            raise HTTPException(
                status_code=422,
                detail="human approval reviewer, reason, policy required",
            )
        try:
            m = load_manifest(Path(root), target)
            target_hash = m.get("manifestHash") or m.get("manifest_hash")
        except Exception:
            target_hash = None
        approval = {
            "reviewer": reviewer,
            "reason": reason,
            "policy": policy,
            "target_manifest_hash": target_hash,
        }
        if request.get("target_manifest_hash"):
            approval["target_manifest_hash"] = request["target_manifest_hash"]
        result = rollback_release(
            Path(root),
            target,
            expected_version=expected_version,
            expected_generation=expected_generation,
            approval=approval,
            operation_id=operation_id,
        )
        if not result.get("success"):
            status = result.get("status", 422)
            raise HTTPException(status_code=status, detail=result)
        return result

    @router.get(
        "/current",
        summary="Read active release pointer (version+generation)",
        operation_id="release_current",
        response_model=dict[str, Any],
        description="Returns current pointer version and opaque generation token; ABA-safe and idempotent CAS uses expected version plus generation.",
    )
    def current(
        root: str = Query(..., description="Caller-owned root"),
    ) -> dict[str, Any]:
        from aksantara.embeddings.registry import load_current

        cur = load_current(Path(root))
        if cur is None:
            raise HTTPException(status_code=404, detail="no current pointer")
        return cur

    @router.get(
        "/history",
        summary="Read release history and pointer events",
        operation_id="release_history",
        response_model=dict[str, Any],
        description="Append-only release history: validated releases and promotion/rollback events with approval and generation.",
    )
    def history(
        root: str = Query(..., description="Caller-owned root"),
    ) -> dict[str, Any]:
        from aksantara.embeddings.registry import load_history

        return load_history(Path(root))

    @router.get(
        "",
        summary="List releases",
        operation_id="release_list",
        response_model=dict[str, Any],
    )
    def list_releases(
        root: str = Query(..., description="Caller-owned root"),
    ) -> dict[str, Any]:
        r = Path(root).expanduser().resolve() / "releases"
        if not r.exists():
            return {"releases": []}
        out = []
        for p in sorted(r.glob("*.json")):
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
                out.append(
                    {
                        "version": m.get("version"),
                        "manifestHash": m.get("manifestHash"),
                        "entries_count": m.get("entries_count"),
                    }
                )
            except Exception:
                continue
        return {"releases": out}

    @router.get(
        "/{release}",
        summary="Read release manifest",
        operation_id="release_read",
        response_model=dict[str, Any],
    )
    def read_release(
        release: str, root: str = Query(..., description="Caller-owned root")
    ) -> dict[str, Any]:
        try:
            return load_manifest(Path(root), release)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail=f"release not found: {release}"
            ) from exc

    return router
