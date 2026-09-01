"""Tests for release verification, promotion, rollback, and pointer CAS."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from aksantara.domain.models import KBBIEntry, SourceRef
from aksantara.embeddings.registry import (
    load_current,
    load_history,
    promote_release,
    rollback_release,
)
from aksantara.embeddings.release import seed_release, verify_release


def _entry(entry_id: str, definisi: str) -> KBBIEntry:
    raw = f"definisi {definisi}".encode()
    ch = hashlib.sha256(raw).hexdigest()
    return KBBIEntry(
        id=entry_id,
        lema=entry_id.title(),
        makna=[{"definisi": definisi}],
        source=SourceRef(
            url=f"https://kbbi.kemdikbud.go.id/entri/{entry_id}",
            source_kind="official-live",
            edition="VI",
            source_version="VI",
            retrieved_at=datetime.now(UTC),
            content_hash=ch,
            parser_version="0.1.0",
        ),
    )


def _seed(root: Path, version: str, entries: list[KBBIEntry]) -> dict:
    return seed_release(root, version, entries)


# ---------------------------------------------------------------------------
# Verification strictness
# ---------------------------------------------------------------------------


def test_verify_fails_on_tampered_manifest(tmp_path: Path) -> None:
    root = tmp_path / "r1"
    root.mkdir()
    e = _entry("x", "definisi x")
    _seed(root, "v1", [e])
    # tamper manifest
    p = root / "releases" / "v1.json"
    data = json.loads(p.read_text())
    data["entries_count"] = 999
    p.write_text(json.dumps(data))
    res = verify_release(root, "v1")
    assert res["valid"] is False
    assert "manifestHash" in res["reason"] or "mismatch" in res["reason"]


def test_verify_fails_on_missing_vector(tmp_path: Path) -> None:
    root = tmp_path / "r2"
    root.mkdir()
    e = _entry("x", "definisi x")
    _seed(root, "v1", [e])
    # remove vector
    next(iter((root / "vectors" / "v1").glob("*.json"))).unlink()
    res = verify_release(root, "v1")
    assert res["valid"] is False


def test_verify_fails_on_extra_vector(tmp_path: Path) -> None:
    root = tmp_path / "r3"
    root.mkdir()
    e = _entry("x", "definisi x")
    _seed(root, "v1", [e])
    # add extra vector file
    extra = root / "vectors" / "v1" / "extra_v1.json"
    extra.write_text(json.dumps({"id": "extra", "model": "gemini-embedding-001"}))
    res = verify_release(root, "v1")
    assert res["valid"] is False
    assert "extra" in res["reason"]


def test_verify_fails_on_duplicate_vector(tmp_path: Path) -> None:
    root = tmp_path / "r4"
    root.mkdir()
    e = _entry("x", "definisi x")
    _seed(root, "v1", [e])
    # duplicate file same entry id but different filename? Our duplicate detection checks same stem prefix; create second file with same entry id plus different suffix but same prefix
    # Actually duplicate detection uses stem.split("_")[0]; so two files with same prefix will be duplicate
    # Create file that also maps to x but different version suffix?
    # Simplest: copy existing file to x_v1_dup.json which still has stem x -> but split gives x, still duplicate? But we count unique ids, so two files both x will be duplicate count 2 vs set 1 => we detect duplicate.
    # Our current duplicate detection checks seen_ids counts; two files both with prefix x -> seen_ids[x]=2 => dups
    src = next((root / "vectors" / "v1").glob("*.json"))
    dup = root / "vectors" / "v1" / "x_v1_dup.json"
    dup.write_text(src.read_text())
    res = verify_release(root, "v1")
    assert res["valid"] is False
    assert "duplicate" in res["reason"]


def test_verify_fails_on_tampered_vector_dims(tmp_path: Path) -> None:
    root = tmp_path / "r5"
    root.mkdir()
    e = _entry("x", "definisi x")
    _seed(root, "v1", [e])
    vf = next((root / "vectors" / "v1").glob("*.json"))
    data = json.loads(vf.read_text())
    data["embedding"] = [0.1] * 10  # wrong dims
    data["dimensions"] = 10
    vf.write_text(json.dumps(data))
    res = verify_release(root, "v1")
    assert res["valid"] is False
    assert "dims" in res["reason"] or "768" in res["reason"]


def test_verify_is_side_effect_free(tmp_path: Path) -> None:
    root = tmp_path / "r6"
    root.mkdir()
    e = _entry("x", "definisi x")
    _seed(root, "v1", [e])
    before_hist = load_history(root)
    before_cur = load_current(root)
    before_manifest = (root / "releases" / "v1.json").read_bytes()
    # tamper vector and verify (should fail but not change manifest/history/pointer)
    vf = next((root / "vectors" / "v1").glob("*.json"))
    data = json.loads(vf.read_text())
    data["model"] = "wrong-model"
    vf.write_text(json.dumps(data))
    # inject tamper into manifest copy-on-write? No, verify should not write
    res = verify_release(root, "v1")
    assert res["valid"] is False
    assert (root / "releases" / "v1.json").read_bytes() == before_manifest
    assert load_history(root) == before_hist
    assert load_current(root) == before_cur


def test_verify_unavailable_store(tmp_path: Path) -> None:
    root = tmp_path / "nonexistent_root_123"
    res = verify_release(root, "v1")
    assert res["valid"] is False
    assert "unavailable" in res.get("reason", "") or res.get("code") == "unavailable"


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


def test_promotion_requires_approval(tmp_path: Path) -> None:
    root = tmp_path / "p1"
    root.mkdir()
    _seed(root, "v1", [_entry("a", "definisi a")])
    _seed(root, "v2", [_entry("b", "definisi b")])
    cur = load_current(root)
    assert cur is not None
    res = promote_release(
        root,
        "v2",
        expected_version=cur["version"],
        expected_generation=cur["generation"],
        approval={"reviewer": "", "reason": "r", "policy": "p"},
    )
    assert res["success"] is False
    assert res["status"] == 422
    # pointer unchanged
    assert load_current(root) == cur


def test_promotion_stale_generation_fails(tmp_path: Path) -> None:
    root = tmp_path / "p2"
    root.mkdir()
    _seed(root, "v1", [_entry("a", "definisi a")])
    _seed(root, "v2", [_entry("b", "definisi b")])
    cur = load_current(root)
    assert cur is not None
    # first promote succeeds
    res1 = promote_release(
        root,
        "v2",
        expected_version=cur["version"],
        expected_generation=cur["generation"],
        approval={
            "reviewer": "r",
            "reason": "rs",
            "policy": "p",
            "target_manifest_hash": json.loads(
                (root / "releases" / "v2.json").read_text()
            )["manifestHash"],
        },
    )
    assert res1["success"] is True
    # stale attempt with old generation should fail
    res2 = promote_release(
        root,
        "v2",
        expected_version="v1",
        expected_generation="gen-1",
        approval={
            "reviewer": "r",
            "reason": "rs",
            "policy": "p",
            "target_manifest_hash": res1["manifest_hash"],
        },
        operation_id="op-stale",
    )
    assert res2["success"] is False
    assert res2["status"] == 409
    assert load_current(root)["version"] == "v2"


def test_promotion_idempotent_same_operation(tmp_path: Path) -> None:
    root = tmp_path / "p3"
    root.mkdir()
    _seed(root, "v1", [_entry("a", "definisi a")])
    _seed(root, "v2", [_entry("b", "definisi b")])
    cur = load_current(root)
    assert cur is not None
    mh = json.loads((root / "releases" / "v2.json").read_text())["manifestHash"]
    res1 = promote_release(
        root,
        "v2",
        expected_version=cur["version"],
        expected_generation=cur["generation"],
        approval={
            "reviewer": "r",
            "reason": "rs",
            "policy": "p",
            "target_manifest_hash": mh,
        },
        operation_id="op-idem",
    )
    assert res1["success"] is True
    hist_len = len(load_history(root)["events"])
    res2 = promote_release(
        root,
        "v2",
        expected_version=cur["version"],
        expected_generation=cur["generation"],
        approval={
            "reviewer": "r",
            "reason": "rs",
            "policy": "p",
            "target_manifest_hash": mh,
        },
        operation_id="op-idem",
    )
    assert res2["success"] is True
    assert res2.get("idempotent") is True
    assert len(load_history(root)["events"]) == hist_len  # no second event


def test_promotion_aba_safe(tmp_path: Path) -> None:
    root = tmp_path / "p4"
    root.mkdir()
    _seed(root, "v1", [_entry("a", "definisi a")])
    _seed(root, "v2", [_entry("b", "definisi b")])
    cur = load_current(root)
    assert cur is not None
    mh1 = json.loads((root / "releases" / "v1.json").read_text())["manifestHash"]
    mh2 = json.loads((root / "releases" / "v2.json").read_text())["manifestHash"]
    # v1 -> v2
    r1 = promote_release(
        root,
        "v2",
        expected_version="v1",
        expected_generation="gen-1",
        approval={
            "reviewer": "r",
            "reason": "a",
            "policy": "p",
            "target_manifest_hash": mh2,
        },
        operation_id="op1",
    )
    assert r1["success"] is True
    assert r1["generation"] == "gen-2"
    # v2 -> v1 (rollback via promote-like)
    r2 = rollback_release(
        root,
        "v1",
        expected_version="v2",
        expected_generation="gen-2",
        approval={
            "reviewer": "r",
            "reason": "a",
            "policy": "p",
            "target_manifest_hash": mh1,
        },
        operation_id="op2",
    )
    assert r2["success"] is True
    assert r2["generation"] == "gen-3"
    # v1 -> v2 again should succeed with new generation despite ABA
    r3 = promote_release(
        root,
        "v2",
        expected_version="v1",
        expected_generation="gen-3",
        approval={
            "reviewer": "r",
            "reason": "a",
            "policy": "p",
            "target_manifest_hash": mh2,
        },
        operation_id="op3",
    )
    assert r3["success"] is True
    assert r3["generation"] == "gen-4"


def test_concurrent_promotion_one_wins(tmp_path: Path) -> None:
    root = tmp_path / "p5"
    root.mkdir()
    _seed(root, "v1", [_entry("a", "definisi a")])
    _seed(root, "v2", [_entry("b", "definisi b")])
    cur = load_current(root)
    assert cur is not None
    mh = json.loads((root / "releases" / "v2.json").read_text())["manifestHash"]
    results: list[dict] = []

    def attempt(op_id: str) -> None:
        r = promote_release(
            root,
            "v2",
            expected_version="v1",
            expected_generation="gen-1",
            approval={
                "reviewer": "r",
                "reason": "a",
                "policy": "p",
                "target_manifest_hash": mh,
            },
            operation_id=op_id,
        )
        results.append(r)

    t1 = threading.Thread(target=attempt, args=("op-c1",))
    t2 = threading.Thread(target=attempt, args=("op-c2",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    successes = [r for r in results if r["success"]]
    failures = [r for r in results if not r["success"]]
    # One should succeed, one should conflict 409
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0]["status"] == 409
    assert load_current(root)["version"] == "v2"
    assert len(load_history(root)["events"]) == 1


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def test_rollback_preserves_history_and_data(tmp_path: Path) -> None:
    root = tmp_path / "rb1"
    root.mkdir()
    _seed(root, "v1", [_entry("a", "definisi a")])
    _seed(root, "v2", [_entry("b", "definisi b")])
    cur = load_current(root)
    assert cur is not None
    mh2 = json.loads((root / "releases" / "v2.json").read_text())["manifestHash"]
    promote_release(
        root,
        "v2",
        expected_version="v1",
        expected_generation="gen-1",
        approval={
            "reviewer": "r",
            "reason": "a",
            "policy": "p",
            "target_manifest_hash": mh2,
        },
        operation_id="op1",
    )
    # capture bytes before rollback
    v1_manifest_before = (root / "releases" / "v1.json").read_bytes()
    v2_manifest_before = (root / "releases" / "v2.json").read_bytes()
    v1_vec_before = sorted((root / "vectors" / "v1").glob("*.json"))
    v1_bytes = {p.name: p.read_bytes() for p in v1_vec_before}
    v2_vec_before = sorted((root / "vectors" / "v2").glob("*.json"))
    v2_bytes = {p.name: p.read_bytes() for p in v2_vec_before}
    history_before = load_history(root)
    # rollback
    mh1 = json.loads((root / "releases" / "v1.json").read_text())["manifestHash"]
    cur2 = load_current(root)
    assert cur2 is not None
    rb = rollback_release(
        root,
        "v1",
        expected_version=cur2["version"],
        expected_generation=cur2["generation"],
        approval={
            "reviewer": "r",
            "reason": "rb",
            "policy": "p",
            "target_manifest_hash": mh1,
        },
        operation_id="rb1",
    )
    assert rb["success"] is True
    # only pointer + one event changed
    assert load_current(root)["version"] == "v1"
    assert len(load_history(root)["events"]) == len(history_before["events"]) + 1
    # previous events preserved
    assert load_history(root)["events"][0] == history_before["events"][0]
    # data preserved byte-identical
    assert (root / "releases" / "v1.json").read_bytes() == v1_manifest_before
    assert (root / "releases" / "v2.json").read_bytes() == v2_manifest_before
    for p in (root / "vectors" / "v1").glob("*.json"):
        assert p.read_bytes() == v1_bytes[p.name]
    for p in (root / "vectors" / "v2").glob("*.json"):
        assert p.read_bytes() == v2_bytes[p.name]


def test_rollback_repeats_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "rb2"
    root.mkdir()
    _seed(root, "v1", [_entry("a", "definisi a")])
    _seed(root, "v2", [_entry("b", "definisi b")])
    mh1 = json.loads((root / "releases" / "v1.json").read_text())["manifestHash"]
    mh2 = json.loads((root / "releases" / "v2.json").read_text())["manifestHash"]
    promote_release(
        root,
        "v2",
        expected_version="v1",
        expected_generation="gen-1",
        approval={
            "reviewer": "r",
            "reason": "a",
            "policy": "p",
            "target_manifest_hash": mh2,
        },
        operation_id="op1",
    )
    cur = load_current(root)
    assert cur is not None
    rb1 = rollback_release(
        root,
        "v1",
        expected_version=cur["version"],
        expected_generation=cur["generation"],
        approval={
            "reviewer": "r",
            "reason": "rb",
            "policy": "p",
            "target_manifest_hash": mh1,
        },
        operation_id="rb1",
    )
    assert rb1["success"] is True
    hist_len = len(load_history(root)["events"])
    # repeat same rollback already-current should be idempotent
    rb2 = rollback_release(
        root,
        "v1",
        expected_version="v1",
        expected_generation=rb1["generation"],
        approval={
            "reviewer": "r",
            "reason": "rb",
            "policy": "p",
            "target_manifest_hash": mh1,
        },
        operation_id="rb1",
    )
    assert rb2["success"] is True
    assert rb2.get("idempotent") is True
    assert len(load_history(root)["events"]) == hist_len


def test_rollback_invalid_target_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "rb3"
    root.mkdir()
    _seed(root, "v1", [_entry("a", "definisi a")])
    _seed(root, "v2", [_entry("b", "definisi b")])
    cur = load_current(root)
    assert cur is not None
    mh2 = json.loads((root / "releases" / "v2.json").read_text())["manifestHash"]
    promote_release(
        root,
        "v2",
        expected_version="v1",
        expected_generation="gen-1",
        approval={
            "reviewer": "r",
            "reason": "a",
            "policy": "p",
            "target_manifest_hash": mh2,
        },
        operation_id="op1",
    )
    cur2 = load_current(root)
    assert cur2 is not None
    # tampered target missing manifest
    res = rollback_release(
        root,
        "missing",
        expected_version=cur2["version"],
        expected_generation=cur2["generation"],
        approval={"reviewer": "r", "reason": "rb", "policy": "p"},
        operation_id="rb-bad",
    )
    assert res["success"] is False
    assert res["status"] in (404, 422)
    assert load_current(root)["version"] == "v2"  # unchanged


# ---------------------------------------------------------------------------
# Local truthful failure
# ---------------------------------------------------------------------------


def test_cloud_unavailable_does_not_promote(tmp_path: Path) -> None:
    # Simulate cloud unavailable path via registry: promotion with invalid candidate should not change pointer
    root = tmp_path / "unavail"
    root.mkdir()
    _seed(root, "v1", [_entry("a", "definisi a")])
    cur = load_current(root)
    assert cur is not None
    # try promote to non-existent candidate
    res = promote_release(
        root,
        "missing",
        expected_version=cur["version"],
        expected_generation=cur["generation"],
        approval={"reviewer": "r", "reason": "a", "policy": "p"},
        operation_id="op-unavail",
    )
    assert res["success"] is False
    assert load_current(root) == cur
    # ensure no hash fallback created missing release
    assert not (root / "releases" / "missing.json").exists()
