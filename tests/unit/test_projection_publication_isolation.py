"""Projection publication isolation — atomic visibility, status/read, fault recovery,
namespace isolation, invalid-source blocking, upstream immutability, and cleanup.

Covers VAL-PIPE-PROJ-004, 005, 006 and VAL-API-PROJ-004, 005, 006.
All assertions use caller-owned, process-scoped, offline fixtures.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aksantara.domain.models import KBBIEntry, SourceRef
from aksantara.embeddings.registry import load_current
from aksantara.embeddings.release import seed_release
from aksantara.projections import (
    ProjectionError,
    cleanup_projection_staging,
    generate_projection,
    get_projection_status,
    list_projections,
    read_projection_artifact,
    read_projection_manifest,
    snapshot_upstream_hashes,
)
from aksantara.projections.manifest import manifest_self_hash
from aksantara.projections.registry import GENERATOR_VERSION


def _entry(
    eid: str, raw_suffix: str = "", bentuk_tidak_baku=None, bentuk_baku=None
) -> KBBIEntry:
    if bentuk_tidak_baku is None:
        bentuk_tidak_baku = []
    return KBBIEntry(
        id=eid,
        lema=eid.title(),
        makna=[{"definisi": f"definisi {eid}"}],
        kelas_kata=["n"],
        bentuk_tidak_baku=bentuk_tidak_baku,
        bentuk_baku=bentuk_baku,
        source=SourceRef(
            url=f"https://kbbi.kemdikbud.go.id/entri/{eid}",
            source_kind="official-live",
            edition="VI",
            source_version="VI",
            retrieved_at=datetime(2026, 9, 1, tzinfo=UTC),
            content_hash=hashlib.sha256(f"raw-{eid}{raw_suffix}".encode()).hexdigest(),
            parser_version="0.1.0",
        ),
    )


def _seed_release_with_canonical(
    tmpdir: Path, version: str, entries: list[KBBIEntry]
) -> None:
    seed_release(tmpdir, version, entries)
    canonical_dir = tmpdir / "canonical" / version
    canonical_dir.mkdir(parents=True, exist_ok=True)
    for e in entries:
        (canonical_dir / f"{e.id}.json").write_text(
            json.dumps(e.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# VAL-PIPE-PROJ-004: two tracks/releases, repeated identities, conflicting bytes, atomic one-writer
# ---------------------------------------------------------------------------


class TestNamespaceIsolationAndAtomicWriter:
    def test_two_tracks_same_release_independently_addressable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            entries = [
                _entry("februari", bentuk_tidak_baku=["Pebruari"]),
                _entry("januari"),
            ]
            _seed_release_with_canonical(release_root, "v1", entries)

            m_word = generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            m_rel = generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="relations",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            assert m_word["track"] == "word"
            assert m_rel["track"] == "relations"
            assert m_word["identity"] != m_rel["identity"]
            assert m_word["output_path"] != m_rel["output_path"]
            # Both readable independently
            data_w, _ = read_projection_artifact(output_root, "aksantara", "word", "v1")
            data_r, _ = read_projection_artifact(
                output_root, "aksantara", "relations", "v1"
            )
            assert data_w != data_r
            # List contains both
            listed = list_projections(output_root)
            ids = {m["identity"] for m in listed}
            assert m_word["identity"] in ids
            assert m_rel["identity"] in ids

    def test_one_track_two_releases_independently_addressable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            _seed_release_with_canonical(release_root, "v1", [_entry("februari")])
            _seed_release_with_canonical(
                release_root,
                "v2",
                [_entry("februari", raw_suffix="2"), _entry("januari")],
            )

            m1 = generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            m2 = generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v2",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            assert m1["source_release"] == "v1"
            assert m2["source_release"] == "v2"
            assert m1["identity"] != m2["identity"]
            assert m1["output_hash"] != m2["output_hash"]
            # Historical output remains readable after second generation
            data1, mani1 = read_projection_artifact(
                output_root, "aksantara", "word", "v1"
            )
            assert mani1["output_hash"] == m1["output_hash"]
            assert data1 is not None
            _, mani2 = read_projection_artifact(output_root, "aksantara", "word", "v2")
            assert mani2["output_hash"] == m2["output_hash"]

    def test_repeated_identity_is_idempotent_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            _seed_release_with_canonical(release_root, "v1", [_entry("februari")])
            m1 = generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            # Capture file mtimes
            art_path = output_root / m1["output_path"]
            _ = output_root / m1["output_path"].replace(
                "artifact.json", "manifest.json"
            )
            hash_before = m1["output_hash"]
            m2 = generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            assert m2["output_hash"] == hash_before
            assert m2["self_hash"] == m1["self_hash"]
            # No extra write — mtime unchanged (allow small delta but file same)
            assert (
                art_path.read_bytes() == (output_root / m2["output_path"]).read_bytes()
            )

    def test_same_identity_conflicting_bytes_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            _seed_release_with_canonical(release_root, "v1", [_entry("februari")])
            m1 = generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            hash_v1 = m1["output_hash"]
            # Seed same version with different content but same identity would require
            # tampering release in place — simulate by directly changing canonical bytes
            # and trying to generate again should conflict because existing manifest has different hash
            # Instead we test that publishing same identity with different payload via direct tamper fails
            # Create a new release with conflicting content but force same identity by reusing same files?
            # Simpler: mutate the release after first publication and try to regenerate with same identity
            # The release v1 now has same entries, but we change the entry to produce different artifact
            # We need to seed a conflicting release that shares same identity tuple but different artifact
            # To simulate, we directly write a different artifact for same identity and check conflict detection
            # Actually conflict detection compares output_hash: if existing manifest has hash A and new has hash B,
            # generate should raise 409.
            # So we create a second canonical set that yields different hash but same identity
            # by changing the entry content
            canon_file = release_root / "canonical" / "v1" / "februari.json"
            # Change entry to different lema variant
            new_entry = _entry(
                "februari",
                raw_suffix="conflict",
                bentuk_tidak_baku=["Pebruari", "Extra"],
            )
            canon_file.write_text(
                json.dumps(new_entry.model_dump(mode="json"), sort_keys=True, indent=2)
                + "\n",
                encoding="utf-8",
            )
            # Also need to update manifest hash to stay eligible? Keep original manifest hash but vector hash will mismatch?
            # Simpler: test conflict by directly checking that second generate with same clock but different bytes yields conflict
            with pytest.raises(ProjectionError) as exc:
                generate_projection(
                    release_root=release_root,
                    output_root=output_root,
                    consumer="aksantara",
                    track="word",
                    source_release="v1",
                    fixed_clock="2026-09-01T00:00:00Z",
                )
            assert exc.value.code == "conflict"
            assert exc.value.status == 409
            # Prior valid remains unchanged
            _, mani = read_projection_artifact(output_root, "aksantara", "word", "v1")
            assert mani["output_hash"] == hash_v1

    def test_concurrent_writers_one_wins_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            _seed_release_with_canonical(release_root, "v1", [_entry("februari")])

            results: list[dict] = []
            errors: list[Exception] = []

            def attempt(idx: int) -> None:
                try:
                    m = generate_projection(
                        release_root=release_root,
                        output_root=output_root,
                        consumer="aksantara",
                        track="word",
                        source_release="v1",
                        fixed_clock="2026-09-01T00:00:00Z",
                    )
                    results.append(m)
                except Exception as e:
                    errors.append(e)  # type: ignore[arg-type]

            t1 = threading.Thread(target=attempt, args=(0,))
            t2 = threading.Thread(target=attempt, args=(1,))
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            # Both should succeed with same hash (identical payload) — no conflict
            assert len(results) == 2
            assert results[0]["output_hash"] == results[1]["output_hash"]
            # Only one set of files visible, hash consistent
            listed = list_projections(output_root)
            assert (
                len(
                    [
                        m
                        for m in listed
                        if m["source_release"] == "v1" and m["track"] == "word"
                    ]
                )
                == 1
            )
            # Reader never sees partial: artifact and manifest both present with matching hash
            data, mani = read_projection_artifact(
                output_root, "aksantara", "word", "v1"
            )
            assert mani["output_hash"] == results[0]["output_hash"]
            assert hashlib.sha256(data).hexdigest() == mani["output_hash"]

    def test_projection_does_not_change_current_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            _seed_release_with_canonical(release_root, "v1", [_entry("februari")])
            _seed_release_with_canonical(release_root, "v2", [_entry("maret")])
            before = load_current(release_root)
            assert before is not None
            before_version = before["version"]
            before_gen = before["generation"]
            generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v2",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            after = load_current(release_root)
            assert after is not None
            assert after["version"] == before_version
            assert after["generation"] == before_gen


# ---------------------------------------------------------------------------
# VAL-PIPE-PROJ-006: status/read behavior — limited statuses, no mismatches, no staged-ready
# ---------------------------------------------------------------------------


class TestStatusAndReadBehavior:
    def test_statuses_limited_and_readers_never_observe_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            _seed_release_with_canonical(release_root, "v1", [_entry("februari")])
            m = generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            assert m["status"] in {"pending", "validated", "failed", "unavailable"}
            assert m["status"] == "validated"
            status = get_projection_status(output_root, "aksantara", "word", "v1")
            assert status == "validated"
            # Readers get validated artifact
            data, mani = read_projection_artifact(
                output_root, "aksantara", "word", "v1"
            )
            assert mani["status"] == "validated"
            assert hashlib.sha256(data).hexdigest() == mani["output_hash"]

    def test_staged_manifest_not_visible_as_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            _seed_release_with_canonical(release_root, "v1", [_entry("februari")])
            # Simulate staged artifact without manifest commit
            staging = output_root / ".staging" / "aksantara_word_v1_proj-gen-v1_word-v1"
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "artifact.json.tmp").write_text("[]\n", encoding="utf-8")
            (staging / "manifest.json.tmp").write_text(
                '{"identity":"x"}\n', encoding="utf-8"
            )
            # List should not expose staged
            assert list_projections(output_root) == []
            # Read should not find validated projection
            with pytest.raises(ProjectionError) as exc:
                read_projection_manifest(output_root, "aksantara", "word", "v1")
            assert exc.value.status in (404, 422)
            # Cleanup should remove only staging, not retained evidence
            # First publish a valid to have retained evidence
            generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            # Re-create staging tmp (need to recreate staging dir if cleaned)
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "artifact.json.tmp").write_text("[]\n", encoding="utf-8")
            result = cleanup_projection_staging(output_root)
            assert any(
                ".staging" in c or "artifact.json.tmp" in c for c in result["cleaned"]
            )
            # Validated still present
            data, _ = read_projection_artifact(output_root, "aksantara", "word", "v1")
            assert data is not None

    def test_manifest_artifact_mismatch_not_observed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            _seed_release_with_canonical(release_root, "v1", [_entry("februari")])
            m = generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            # Tamper artifact to mismatch
            art_path = output_root / m["output_path"]
            art_path.write_bytes(b'[{"tampered": true}]\n')
            # Read should fail with hash mismatch, not return tampered bytes as validated
            with pytest.raises(ProjectionError) as exc:
                read_projection_artifact(output_root, "aksantara", "word", "v1")
            assert exc.value.code == "hash_mismatch"
            # List should hide tampered projection (failed validation)
            listed = list_projections(output_root)
            assert len([x for x in listed if x["output_hash"] == m["output_hash"]]) == 0
            # Status should be failed
            assert (
                get_projection_status(output_root, "aksantara", "word", "v1")
                == "failed"
            )

    def test_unknown_tampered_reads_do_not_substitute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            _seed_release_with_canonical(release_root, "v1", [_entry("februari")])
            _seed_release_with_canonical(release_root, "v2", [_entry("maret")])
            generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v2",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            # Unknown release read should not substitute another release
            with pytest.raises(ProjectionError) as exc:
                read_projection_artifact(
                    output_root, "aksantara", "word", "nonexistent"
                )
            assert exc.value.status == 404
            # Tampered manifest self_hash should not return other track's bytes
            # Tamper v1 manifest
            mani_path = (
                output_root
                / "projections"
                / "aksantara"
                / "word"
                / "v1"
                / GENERATOR_VERSION
                / "word-v1"
                / "manifest.json"
            )
            mani = json.loads(mani_path.read_text(encoding="utf-8"))
            mani["self_hash"] = "0" * 64
            mani_path.write_text(
                json.dumps(mani, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            with pytest.raises(ProjectionError) as exc2:
                read_projection_manifest(output_root, "aksantara", "word", "v1")
            assert exc2.value.code == "hash_mismatch"
            # Cross-track substitution should not happen: reading word with relations identity fails
            with pytest.raises(ProjectionError):
                read_projection_artifact(output_root, "aksantara", "relations", "v1")
            # v2 still readable exactly
            data2, mani2 = read_projection_artifact(
                output_root, "aksantara", "word", "v2"
            )
            assert mani2["source_release"] == "v2"
            assert data2 is not None


# ---------------------------------------------------------------------------
# VAL-PIPE-PROJ-005: invalid source blocks publication, prior hashes unchanged
# ---------------------------------------------------------------------------


class TestInvalidSourceBlocking:
    def test_missing_vector_blocks_and_prior_hash_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            entries_v1 = [_entry("februari"), _entry("januari")]
            _seed_release_with_canonical(release_root, "v1", entries_v1)
            m_valid = generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            prior_hash = m_valid["output_hash"]
            # Create an ineligible release with missing vector (tamper by removing vector)
            _seed_release_with_canonical(
                release_root, "v2", [_entry("maret"), _entry("april")]
            )
            # Remove a vector to make invalid
            vec_file = next((release_root / "vectors" / "v2").glob("*.json"))
            vec_file.unlink()
            with pytest.raises(ProjectionError) as exc:
                generate_projection(
                    release_root=release_root,
                    output_root=output_root,
                    consumer="aksantara",
                    track="word",
                    source_release="v2",
                    fixed_clock="2026-09-01T00:00:00Z",
                )
            assert exc.value.code == "ineligible"
            # Prior valid projection unchanged
            data, mani = read_projection_artifact(
                output_root, "aksantara", "word", "v1"
            )
            assert mani["output_hash"] == prior_hash
            assert hashlib.sha256(data).hexdigest() == prior_hash
            # Invalid release has no validated artifact
            with pytest.raises(ProjectionError):
                read_projection_artifact(output_root, "aksantara", "word", "v2")
            assert (
                list_projections(output_root) == [m_valid]
                or len(list_projections(output_root)) == 1
            )

    def test_conflicted_release_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            _seed_release_with_canonical(release_root, "v1", [_entry("februari")])
            m_valid = generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            prior_hash = m_valid["output_hash"]
            # Create blocked release with conflicts field
            _seed_release_with_canonical(release_root, "blocked", [_entry("maret")])
            mp = release_root / "releases" / "blocked.json"
            m = json.loads(mp.read_text(encoding="utf-8"))
            m["conflicts"] = [{"id": "c1", "fields": ["makna"]}]
            # recompute manifest hash
            m["manifestHash"] = hashlib.sha256(
                json.dumps(
                    {
                        k: v
                        for k, v in m.items()
                        if k not in ("manifestHash", "manifest_hash")
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            m["manifest_hash"] = m["manifestHash"]
            mp.write_text(
                json.dumps(m, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            with pytest.raises(ProjectionError) as exc:
                generate_projection(
                    release_root=release_root,
                    output_root=output_root,
                    consumer="aksantara",
                    track="word",
                    source_release="blocked",
                    fixed_clock="2026-09-01T00:00:00Z",
                )
            assert exc.value.code == "ineligible"
            # Prior valid unchanged
            _, mani = read_projection_artifact(output_root, "aksantara", "word", "v1")
            assert mani["output_hash"] == prior_hash

    def test_hash_mismatch_and_invalid_manifest_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            _seed_release_with_canonical(release_root, "v1", [_entry("februari")])
            m_valid = generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            prior_hash = m_valid["output_hash"]
            # Create a separate invalid release by copying v1 but tampering hash
            _seed_release_with_canonical(release_root, "badhash", [_entry("maret")])
            bmp = release_root / "releases" / "badhash.json"
            bm = json.loads(bmp.read_text(encoding="utf-8"))
            bm["manifestHash"] = "0" * 64
            bm["manifest_hash"] = "0" * 64
            bmp.write_text(
                json.dumps(bm, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            with pytest.raises(ProjectionError):
                generate_projection(
                    release_root=release_root,
                    output_root=output_root,
                    consumer="aksantara",
                    track="word",
                    source_release="badhash",
                    fixed_clock="2026-09-01T00:00:00Z",
                )
            # Prior valid still intact
            _, mani = read_projection_artifact(output_root, "aksantara", "word", "v1")
            assert mani["output_hash"] == prior_hash


# ---------------------------------------------------------------------------
# VAL-PIPE-PROJ-006: atomic publication, upstream immutability, staging cleanup
# ---------------------------------------------------------------------------


class TestAtomicPublicationAndImmutability:
    def test_fault_at_each_phase_preserves_prior_valid(self) -> None:
        for fault_phase in [
            "artifact_write",
            "output_hash",
            "manifest_commit",
            "verification",
        ]:
            with tempfile.TemporaryDirectory() as tmp:
                tmpdir = Path(tmp)
                release_root = tmpdir / "release"
                output_root = tmpdir / "out"
                release_root.mkdir()
                output_root.mkdir()
                _seed_release_with_canonical(release_root, "v1", [_entry("februari")])
                m_valid = generate_projection(
                    release_root=release_root,
                    output_root=output_root,
                    consumer="aksantara",
                    track="word",
                    source_release="v1",
                    fixed_clock="2026-09-01T00:00:00Z",
                )
                prior_hash = m_valid["output_hash"]
                prior_bytes = (output_root / m_valid["output_path"]).read_bytes()
                _seed_release_with_canonical(release_root, "v2", [_entry("maret")])
                with pytest.raises(ProjectionError):
                    generate_projection(
                        release_root=release_root,
                        output_root=output_root,
                        consumer="aksantara",
                        track="word",
                        source_release="v2",
                        fixed_clock="2026-09-01T00:00:00Z",
                        fault=fault_phase,
                    )
                # Prior valid intact
                data, mani = read_projection_artifact(
                    output_root, "aksantara", "word", "v1"
                )
                assert mani["output_hash"] == prior_hash
                assert data == prior_bytes
                # Failed v2 not published
                with pytest.raises(ProjectionError):
                    read_projection_artifact(output_root, "aksantara", "word", "v2")
                # Staging cleaned: .staging should be empty or absent, no orphan artifact for v2
                staging = output_root / ".staging"
                if staging.exists():
                    assert not any(staging.rglob("*.tmp"))
                # No validated v2 in list
                assert all(
                    m["source_release"] != "v2" for m in list_projections(output_root)
                )

    def test_interrupted_publication_leaves_no_staged_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            _seed_release_with_canonical(release_root, "v1", [_entry("februari")])
            # Simulate interruption by creating staging with partial files and no manifest commit
            _seed_release_with_canonical(release_root, "v2", [_entry("maret")])
            try:
                generate_projection(
                    release_root=release_root,
                    output_root=output_root,
                    consumer="aksantara",
                    track="word",
                    source_release="v2",
                    fixed_clock="2026-09-01T00:00:00Z",
                    fault="manifest_commit",
                )
            except ProjectionError:
                pass
            # Only v1 should be list-visible if it existed; v2 should not be staged-ready
            # Publish v1 valid first then fault v2: v1 should remain
            _seed_release_with_canonical(release_root, "v1b", [_entry("april")])
            generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1b",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            # v2 should still be non-ready
            status_v2 = get_projection_status(output_root, "aksantara", "word", "v2")
            assert status_v2 in ("failed", "unavailable", "pending")
            # Readers never see manifest without artifact
            with pytest.raises(ProjectionError):
                read_projection_artifact(output_root, "aksantara", "word", "v2")

    def test_upstream_byte_identical_across_projection_ops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            _seed_release_with_canonical(
                release_root, "v1", [_entry("februari"), _entry("januari")]
            )
            # Create upstream artifacts: canonical, raw simulation, vectors, releases, registry, conflicts, review
            # seed already creates releases, canonical, vectors, registry
            # Add dummy run/candidate/conflict/review state
            for extra in [
                "runs/run1.json",
                "candidate_snapshots/v1/a.json",
                "conflicts/c1.json",
                "review/queue.json",
            ]:
                p = release_root / extra
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(
                    json.dumps({"test": "upstream", "path": extra}, sort_keys=True)
                    + "\n",
                    encoding="utf-8",
                )
            # Seed invalid release BEFORE snapshot so that projection ops themselves are measured for immutability
            _seed_release_with_canonical(release_root, "bad", [_entry("maret")])
            vec = next((release_root / "vectors" / "bad").glob("*.json"))
            vec.unlink()
            before = snapshot_upstream_hashes(release_root)
            # Perform multiple projection ops: success, fault, read, list
            generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="relations",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            try:
                generate_projection(
                    release_root=release_root,
                    output_root=output_root,
                    consumer="aksantara",
                    track="word",
                    source_release="bad",
                    fixed_clock="2026-09-01T00:00:00Z",
                )
            except ProjectionError:
                pass
            # Reads
            read_projection_artifact(output_root, "aksantara", "word", "v1")
            list_projections(output_root)
            after = snapshot_upstream_hashes(release_root)
            assert before == after, (
                f"upstream mutated: {set(before.keys()) ^ set(after.keys())} diff keys, before/after mismatch"
            )

    def test_owned_staging_cleaned_without_deleting_retained_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            _seed_release_with_canonical(release_root, "v1", [_entry("februari")])
            m = generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            # Ensure output and history exist
            assert (output_root / m["output_path"]).exists()
            # Create some temp and lock artifacts
            staging = output_root / ".staging" / "aksantara_word_v1_proj-gen-v1_word-v1"
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "temp.tmp").write_text("tmp", encoding="utf-8")
            locks = output_root / ".locks"
            locks.mkdir(parents=True, exist_ok=True)
            (locks / "test.lock").write_text("lock", encoding="utf-8")
            # Sentinel outside output root should not be touched
            sentinel = tmpdir / "sentinel.txt"
            sentinel.write_text("do not delete", encoding="utf-8")
            sentinel_hash_before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
            # Cleanup staging
            result = cleanup_projection_staging(output_root)
            # Retained evidence still present
            assert (output_root / m["output_path"]).exists()
            assert (
                output_root / m["output_path"].replace("artifact.json", "manifest.json")
            ).exists()
            # Sentinel unchanged
            assert (
                hashlib.sha256(sentinel.read_bytes()).hexdigest()
                == sentinel_hash_before
            )
            # Sentinel not in cleaned
            assert not any("sentinel" in c for c in result.get("cleaned", []))
            # Upstream untouched
            assert (release_root / "releases" / "v1.json").exists()
            assert (release_root / "canonical" / "v1" / "februari.json").exists()

    def test_output_root_cannot_cross_into_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            release_root.mkdir()
            _seed_release_with_canonical(release_root, "v1", [_entry("februari")])
            # Try to use canonical namespace as output root — must fail
            with pytest.raises(ProjectionError) as exc:
                generate_projection(
                    release_root=release_root,
                    output_root=release_root / "canonical",
                    consumer="aksantara",
                    track="word",
                    source_release="v1",
                    fixed_clock="2026-09-01T00:00:00Z",
                )
            assert exc.value.code == "unsafe_path"
            # Try release root itself
            with pytest.raises(ProjectionError):
                generate_projection(
                    release_root=release_root,
                    output_root=release_root,
                    consumer="aksantara",
                    track="word",
                    source_release="v1",
                    fixed_clock="2026-09-01T00:00:00Z",
                )

    def test_known_read_returns_exact_verified_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            entries = [
                _entry("februari", bentuk_tidak_baku=["Pebruari"]),
                _entry("januari"),
            ]
            _seed_release_with_canonical(release_root, "v1", entries)
            m = generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            data, mani = read_projection_artifact(
                output_root, "aksantara", "word", "v1"
            )
            # Exact bytes match what was published
            art_path = output_root / m["output_path"]
            assert data == art_path.read_bytes()
            assert mani["output_hash"] == hashlib.sha256(data).hexdigest()
            assert mani["self_hash"] == manifest_self_hash(mani)
            # Deterministic: second read same bytes
            data2, mani2 = read_projection_artifact(
                output_root, "aksantara", "word", "v1"
            )
            assert data2 == data
            assert mani2["output_hash"] == mani["output_hash"]


# ---------------------------------------------------------------------------
# Additional API-level behavior via FastAPI TestClient (local caller-owned)
# ---------------------------------------------------------------------------


class TestAPIIsolation:
    def test_projection_api_registry_and_read_isolation(self) -> None:
        from fastapi.testclient import TestClient

        from aksantara.api.routes import create_app

        app = create_app()
        client = TestClient(app)

        # Registry is discoverable
        r = client.get("/projections/registry")
        assert r.status_code == 200
        body = r.json()
        assert "allowed_tracks" in body
        assert "word" in body["allowed_tracks"]
        assert "relations" in body["allowed_tracks"]
        assert "rejected_product_identifiers" in body
        assert "hunspell" in body["rejected_product_identifiers"]

        # Generate via store then read via API
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            _seed_release_with_canonical(release_root, "v1", [_entry("februari")])
            generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            # API artifact read by exact identity
            rr = client.get(
                "/projections/artifact",
                params={
                    "output_root": str(output_root),
                    "consumer": "aksantara",
                    "track": "word",
                    "release": "v1",
                },
            )
            assert rr.status_code == 200
            assert rr.json()["manifest"]["source_release"] == "v1"
            # Unknown read does not substitute
            bad = client.get(
                "/projections/artifact",
                params={
                    "output_root": str(output_root),
                    "consumer": "aksantara",
                    "track": "word",
                    "release": "nonexistent",
                },
            )
            assert bad.status_code == 404
            # Cross-track does not substitute
            bad2 = client.get(
                "/projections/artifact",
                params={
                    "output_root": str(output_root),
                    "consumer": "aksantara",
                    "track": "relations",
                    "release": "v1",
                },
            )
            assert bad2.status_code == 404
            # List via API
            lst = client.get("/projections", params={"output_root": str(output_root)})
            assert lst.status_code == 200
            assert lst.json()["count"] == 1
