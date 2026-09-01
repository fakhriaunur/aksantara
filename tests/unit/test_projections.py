"""Unit tests for deterministic generic word/relations projection generation."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aksantara.domain.models import KBBIEntry, SourceRef
from aksantara.embeddings.release import seed_release, verify_release
from aksantara.projections.generator import (
    artifact_bytes,
    artifact_hash,
    build_relations_artifact,
    build_word_artifact,
)
from aksantara.projections.manifest import (
    build_projection_manifest,
    manifest_self_hash,
    projection_identity,
)
from aksantara.projections.registry import (
    ALLOWED_CONSUMERS,
    ALLOWED_TRACKS,
    GENERATOR_VERSION,
    REJECTED_PRODUCT_IDENTIFIERS,
    SCHEMA_VERSIONS,
    is_rejected_product,
    registry_snapshot,
    validate_selector,
)
from aksantara.projections.schemas import (
    RELATIONS_SCHEMA_V1,
    WORD_SCHEMA_V1,
    validate_relation_record,
    validate_word_record,
)
from aksantara.projections.store import (
    ProjectionError,
    generate_projection,
    list_projections,
    read_projection_artifact,
    read_projection_manifest,
)


def _make_entry(
    eid: str,
    lema: str | None = None,
    bentuk_tidak_baku: list[str] | None = None,
    bentuk_baku: str | None = None,
    raw_suffix: str = "",
) -> KBBIEntry:
    return KBBIEntry(
        id=eid,
        lema=lema or eid.title(),
        makna=[{"definisi": f"definisi {eid}"}],
        kelas_kata=["n"],
        bentuk_tidak_baku=bentuk_tidak_baku or [],
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


def _seed_release_with_canonical(tmpdir: Path, version: str, entries: list[KBBIEntry]) -> None:
    seed_release(tmpdir, version, entries)
    canonical_dir = tmpdir / "canonical" / version
    canonical_dir.mkdir(parents=True, exist_ok=True)
    for e in entries:
        (canonical_dir / f"{e.id}.json").write_text(
            json.dumps(e.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Registry / help discovery (VAL-PIPE-PROJ-001, VAL-API-PROJ-001)
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_registry_snapshot_contains_required_fields(self) -> None:
        snap = registry_snapshot()
        assert "allowed_consumers" in snap
        assert "allowed_tracks" in snap
        assert "rejected_product_identifiers" in snap
        assert "generator_version" in snap
        assert "schema_versions" in snap
        assert snap["generator_version"] == GENERATOR_VERSION
        assert set(snap["allowed_tracks"]) == set(ALLOWED_TRACKS)  # type: ignore[arg-type]
        assert "serialization_rules" in snap
        assert "relation_rule" in snap

    def test_rejected_products_include_hunspell_cspell_babel(self) -> None:
        assert is_rejected_product("hunspell")
        assert is_rejected_product("cspell")
        assert is_rejected_product("babel")
        assert is_rejected_product("polyglossia")
        assert is_rejected_product("rabu-baku")
        assert is_rejected_product("rabu_baku")

    def test_allowed_tracks_are_word_and_relations(self) -> None:
        assert "word" in ALLOWED_TRACKS
        assert "relations" in ALLOWED_TRACKS
        assert len(ALLOWED_TRACKS) == 2

    def test_validate_selector_rejects_hunspell(self) -> None:
        errors = validate_selector("hunspell", "word", "v1")
        assert any("hunspell" in e.lower() or "unsupported" in e.lower() for e in errors)

    def test_validate_selector_rejects_babel(self) -> None:
        errors = validate_selector("aksantara", "babel", "v1")
        assert len(errors) > 0

    def test_validate_selector_rejects_path_like(self) -> None:
        errors = validate_selector("aksantara", "word", "../etc/passwd")
        assert any("path" in e.lower() for e in errors)

    def test_validate_selector_rejects_missing(self) -> None:
        errors = validate_selector("", "word", "v1")
        assert any("consumer" in e.lower() for e in errors)
        errors2 = validate_selector("aksantara", "", "v1")
        assert any("track" in e.lower() for e in errors2)

    def test_schema_versions_map(self) -> None:
        assert SCHEMA_VERSIONS["word"] == "word-v1"
        assert SCHEMA_VERSIONS["relations"] == "relations-v1"

    def test_word_and_relations_schemas_published(self) -> None:
        assert WORD_SCHEMA_V1["schema_version"] == "word-v1"
        assert RELATIONS_SCHEMA_V1["schema_version"] == "relations-v1"
        assert "serialization" in WORD_SCHEMA_V1
        assert "rules" in RELATIONS_SCHEMA_V1


# ---------------------------------------------------------------------------
# Manifest lineage (VAL-PIPE-PROJ-002)
# ---------------------------------------------------------------------------


class TestManifestLineage:
    def test_projection_manifest_carries_exact_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            entries = [_make_entry("februari", bentuk_tidak_baku=["Pebruari"]), _make_entry("januari")]
            _seed_release_with_canonical(release_root, "v1", entries)

            manifest = generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            # Check required lineage fields
            assert manifest["consumer"] == "aksantara"
            assert manifest["track"] == "word"
            assert manifest["source_release"] == "v1"
            assert "source_manifest_hash" in manifest
            assert len(manifest["source_manifest_hash"]) == 64
            assert manifest["generator_version"] == GENERATOR_VERSION
            assert manifest["schema_version"] == "word-v1"
            assert "output_path" in manifest
            assert not manifest["output_path"].startswith("/")
            assert ".." not in manifest["output_path"]
            assert "output_hash" in manifest
            assert len(manifest["output_hash"]) == 64
            assert "self_hash" in manifest
            assert len(manifest["self_hash"]) == 64
            assert manifest["status"] == "validated"
            # Sorted entry IDs
            assert manifest["sorted_entry_ids"] == sorted(manifest["sorted_entry_ids"])
            assert manifest["sorted_entry_ids"] == ["februari", "januari"]
            # Source entries carry exact hashes
            for entry in manifest["source_entries"]:
                assert "id" in entry
                assert "canonical_content_hash" in entry
                assert len(entry["canonical_content_hash"]) == 64
                assert "raw_content_hash" in entry
                assert len(entry["raw_content_hash"]) == 64
                assert "source_release" in entry
                assert entry["source_release"] == "v1"

    def test_manifest_self_hash_recomputes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            entries = [_make_entry("februari")]
            _seed_release_with_canonical(release_root, "v1", entries)
            manifest = generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            recomputed = manifest_self_hash(manifest)
            assert manifest["self_hash"] == recomputed

    def test_manifest_hashes_recompute_from_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            entries = [_make_entry("februari")]
            _seed_release_with_canonical(release_root, "v1", entries)
            manifest = generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            # Artifact hash recomputes
            artifact_path = output_root / manifest["output_path"]
            data = artifact_path.read_bytes()
            assert hashlib.sha256(data).hexdigest() == manifest["output_hash"]

    def test_sorted_entry_ids_no_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            entries = [_make_entry("februari"), _make_entry("januari"), _make_entry("maret")]
            _seed_release_with_canonical(release_root, "v1", entries)
            manifest = generate_projection(
                release_root=release_root,
                output_root=output_root,
                consumer="aksantara",
                track="word",
                source_release="v1",
                fixed_clock="2026-09-01T00:00:00Z",
            )
            ids = manifest["sorted_entry_ids"]
            assert ids == sorted(ids)
            assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Deterministic bytes (VAL-PIPE-PROJ-003)
# ---------------------------------------------------------------------------


class TestDeterministicBytes:
    def test_fixed_inputs_and_clock_produce_byte_identical_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
                tmpdir = Path(tmp)
                release_root = tmpdir / "release"
                release_root.mkdir()
                entries = [_make_entry("februari", bentuk_tidak_baku=["Pebruari"]), _make_entry("januari")]
                _seed_release_with_canonical(release_root, "v1", entries)

                # Generate twice with same clock but different dict order
                dict_normal = {e.id: e for e in entries}
                dict_reversed = {e.id: e for e in reversed(entries)}

                words_normal = build_word_artifact(dict_normal, "v1")
                words_reversed = build_word_artifact(dict_reversed, "v1")
                assert artifact_bytes(words_normal) == artifact_bytes(words_reversed)
                assert artifact_hash(artifact_bytes(words_normal)) == artifact_hash(artifact_bytes(words_reversed))

                rels_normal = build_relations_artifact(dict_normal, "v1")
                rels_reversed = build_relations_artifact(dict_reversed, "v1")
                assert artifact_bytes(rels_normal) == artifact_bytes(rels_reversed)

    def test_word_artifact_source_backed(self) -> None:
        entries = {"februari": _make_entry("februari", bentuk_tidak_baku=["Pebruari"])}
        words = build_word_artifact(entries, "v1")
        assert len(words) == 1
        w = words[0]
        errors = validate_word_record(w)
        assert errors == [], f"word validation failed: {errors}"
        assert w["id"] == "februari"
        assert w["source_release"] == "v1"
        assert len(w["canonical_content_hash"]) == 64
        assert len(w["raw_content_hash"]) == 64

    def test_relations_explicit_semantics_and_witnesses(self) -> None:
        entries = {
            "februari": _make_entry("februari", bentuk_tidak_baku=["Pebruari"]),
            "maret": _make_entry("maret", bentuk_baku=None),
        }
        # Add variant entry with bentuk_baku
        variant = _make_entry("pebruari", lema="Pebruari", bentuk_baku="Februari")
        entries["pebruari"] = variant

        rels = build_relations_artifact(entries, "v1")
        # Should have relations
        assert len(rels) >= 1
        for r in rels:
            errs = validate_relation_record(r)
            assert errs == [], f"relation validation failed: {errs} for {r}"
            assert r["type"] == "nonstandard_variant"
            assert r["canonical_field"] in ("bentuk_tidak_baku", "bentuk_baku")
            assert len(r["source_hash"]) == 64
            assert "source_entry_id" in r
            assert "source_release" in r

    def test_bentuk_tidak_baku_and_bentuk_baku_explicit_direction(self) -> None:
        # bentuk_tidak_baku: variant -> lema
        # bentuk_baku: lema -> standard
        entries = {
            "februari": _make_entry("februari", bentuk_tidak_baku=["Pebruari"]),
        }
        rels = build_relations_artifact(entries, "v1")
        assert any(r["from"] == "Pebruari" and r["to"] == "Februari" and r["canonical_field"] == "bentuk_tidak_baku" for r in rels)

        entries2 = {
            "pebruari": _make_entry("pebruari", lema="Pebruari", bentuk_baku="Februari"),
        }
        rels2 = build_relations_artifact(entries2, "v1")
        assert any(r["from"] == "Pebruari" and r["to"] == "Februari" and r["canonical_field"] == "bentuk_baku" for r in rels2)

    def test_no_raw_html_or_invented_endpoint(self) -> None:
        entries = {"februari": _make_entry("februari", bentuk_tidak_baku=["Pebruari"])}
        words = build_word_artifact(entries, "v1")
        rels = build_relations_artifact(entries, "v1")
        # No HTML tags in word/relation output
        for w in words:
            for v in w.values():
                if isinstance(v, str):
                    assert "<" not in v or ">" not in v or v.startswith("http")
        for r in rels:
            assert "<" not in r["from"]
            assert "<" not in r["to"]

    def test_duplicate_relations_deduplicated(self) -> None:
        entries = {
            "februari": _make_entry("februari", bentuk_tidak_baku=["Pebruari"]),
            "pebruari": _make_entry("pebruari", lema="Pebruari", bentuk_baku="Februari"),
        }
        rels = build_relations_artifact(entries, "v1")
        keys = [(r["from"], r["to"], r["type"]) for r in rels]
        assert len(keys) == len(set(keys)), "duplicate (from,to,type) should be deduplicated"

    def test_output_files_byte_identical_with_fixed_clock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            for trial in range(2):
                release_root = tmpdir / f"release{trial}"
                output_root = tmpdir / f"out{trial}"
                release_root.mkdir()
                output_root.mkdir()
                entries = [_make_entry("februari", bentuk_tidak_baku=["Pebruari"]), _make_entry("januari")]
                _seed_release_with_canonical(release_root, "v1", entries)
                generate_projection(
                    release_root=release_root,
                    output_root=output_root,
                    consumer="aksantara",
                    track="word",
                    source_release="v1",
                    fixed_clock="2026-09-01T00:00:00Z",
                )
            # Compare artifact bytes
            data0 = (tmpdir / "out0" / "projections" / "aksantara" / "word" / "v1" / GENERATOR_VERSION / "word-v1" / "artifact.json").read_bytes()
            data1 = (tmpdir / "out1" / "projections" / "aksantara" / "word" / "v1" / GENERATOR_VERSION / "word-v1" / "artifact.json").read_bytes()
            assert data0 == data1


# ---------------------------------------------------------------------------
# Identity isolation (VAL-PIPE-PROJ-004 partial, also CLI/API)
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_collision_safe_identity_contains_all_tuple(self) -> None:
        identity = projection_identity("aksantara", "word", "v1", GENERATOR_VERSION, "word-v1")
        assert "aksantara" in identity
        assert "word" in identity
        assert "v1" in identity
        assert GENERATOR_VERSION in identity
        assert "word-v1" in identity

    def test_different_tracks_have_different_identities(self) -> None:
        id_word = projection_identity("aksantara", "word", "v1", GENERATOR_VERSION, "word-v1")
        id_rel = projection_identity("aksantara", "relations", "v1", GENERATOR_VERSION, "relations-v1")
        assert id_word != id_rel

    def test_different_releases_have_different_identities(self) -> None:
        id_v1 = projection_identity("aksantara", "word", "v1", GENERATOR_VERSION, "word-v1")
        id_v2 = projection_identity("aksantara", "word", "v2", GENERATOR_VERSION, "word-v1")
        assert id_v1 != id_v2

    def test_generate_preserves_historical_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            # v1
            entries_v1 = [_make_entry("februari")]
            _seed_release_with_canonical(release_root, "v1", entries_v1)
            m1 = generate_projection(
                release_root=release_root, output_root=output_root, consumer="aksantara", track="word", source_release="v1", fixed_clock="2026-09-01T00:00:00Z"
            )
            hash_v1 = m1["output_hash"]
            # v2 with different content
            entries_v2 = [_make_entry("februari", raw_suffix="-changed"), _make_entry("januari")]
            _seed_release_with_canonical(release_root, "v2", entries_v2)
            m2 = generate_projection(
                release_root=release_root, output_root=output_root, consumer="aksantara", track="word", source_release="v2", fixed_clock="2026-09-01T00:00:00Z"
            )
            # v1 still exists and unchanged
            data_v1, mani_v1 = read_projection_artifact(output_root, "aksantara", "word", "v1")
            assert mani_v1["output_hash"] == hash_v1
            assert data_v1 == (output_root / m1["output_path"]).read_bytes()


# ---------------------------------------------------------------------------
# Invalid source blocking (VAL-PIPE-PROJ-005 partial)
# ---------------------------------------------------------------------------


class TestInvalidSourceBlocking:
    def test_missing_release_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            with pytest.raises(ProjectionError) as exc:
                generate_projection(
                    release_root=release_root, output_root=output_root, consumer="aksantara", track="word", source_release="nonexistent"
                )
            assert exc.value.code == "not_found"
            assert exc.value.status == 404

    def test_unvalidated_release_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            entries = [_make_entry("februari")]
            _seed_release_with_canonical(release_root, "v1", entries)
            # Tamper with conflicts to make ineligible
            mp = release_root / "releases" / "v1.json"
            m = json.loads(mp.read_text(encoding="utf-8"))
            m["conflicts"] = [{"id": "c1"}]
            m["manifestHash"] = hashlib.sha256(
                json.dumps({k: v for k, v in m.items() if k not in ("manifestHash", "manifest_hash")}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            ).hexdigest()
            m["manifest_hash"] = m["manifestHash"]
            mp.write_text(json.dumps(m, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            with pytest.raises(ProjectionError) as exc:
                generate_projection(
                    release_root=release_root, output_root=output_root, consumer="aksantara", track="word", source_release="v1"
                )
            assert exc.value.code == "ineligible"

    def test_rejected_product_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            entries = [_make_entry("februari")]
            _seed_release_with_canonical(release_root, "v1", entries)
            with pytest.raises(ProjectionError) as exc:
                generate_projection(
                    release_root=release_root, output_root=output_root, consumer="hunspell", track="word", source_release="v1"
                )
            assert "hunspell" in str(exc.value).lower()

    def test_empty_release_succeeds_with_empty_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            # Create empty valid release
            releases_dir = release_root / "releases"
            releases_dir.mkdir(parents=True, exist_ok=True)
            empty_manifest: dict[str, object] = {"version": "empty-v1", "created_at": "2026-09-01T00:00:00Z", "entries_count": 0, "artifactHashes": {}, "canonicalHashes": {}}  # type: ignore[dict-item]
            empty_manifest["manifestHash"] = hashlib.sha256(  # type: ignore[attr-defined]
                json.dumps({k: v for k, v in empty_manifest.items() if k not in ("manifestHash", "manifest_hash")}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            ).hexdigest()
            empty_manifest["manifest_hash"] = empty_manifest["manifestHash"]  # type: ignore[attr-defined]
            (releases_dir / "empty-v1.json").write_text(json.dumps(empty_manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            (release_root / "vectors" / "empty-v1").mkdir(parents=True, exist_ok=True)
            (release_root / "canonical" / "empty-v1").mkdir(parents=True, exist_ok=True)

            mani = generate_projection(
                release_root=release_root, output_root=output_root, consumer="aksantara", track="word", source_release="empty-v1", fixed_clock="2026-09-01T00:00:00Z"
            )
            assert mani["entry_count"] == 0
            assert mani["sorted_entry_ids"] == []
            data, _ = read_projection_artifact(output_root, "aksantara", "word", "empty-v1")
            assert json.loads(data) == []


# ---------------------------------------------------------------------------
# No write path to canonical (VAL-PIPE-PROJ-006 partial)
# ---------------------------------------------------------------------------


class TestNoCanonicalWrite:
    def test_projection_does_not_mutate_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            entries = [_make_entry("februari")]
            _seed_release_with_canonical(release_root, "v1", entries)
            canonical_file = release_root / "canonical" / "v1" / "februari.json"
            manifest_file = release_root / "releases" / "v1.json"
            before_canonical = hashlib.sha256(canonical_file.read_bytes()).hexdigest()
            before_manifest = hashlib.sha256(manifest_file.read_bytes()).hexdigest()

            generate_projection(
                release_root=release_root, output_root=output_root, consumer="aksantara", track="word", source_release="v1", fixed_clock="2026-09-01T00:00:00Z"
            )

            assert hashlib.sha256(canonical_file.read_bytes()).hexdigest() == before_canonical
            assert hashlib.sha256(manifest_file.read_bytes()).hexdigest() == before_manifest

    def test_output_root_cannot_be_canonical_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            release_root.mkdir()
            entries = [_make_entry("februari")]
            _seed_release_with_canonical(release_root, "v1", entries)
            with pytest.raises(ProjectionError) as exc:
                generate_projection(
                    release_root=release_root, output_root=release_root / "canonical", consumer="aksantara", track="word", source_release="v1"
                )
            assert exc.value.code == "unsafe_path"

    def test_idempotent_repeat_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            release_root = tmpdir / "release"
            output_root = tmpdir / "out"
            release_root.mkdir()
            output_root.mkdir()
            entries = [_make_entry("februari")]
            _seed_release_with_canonical(release_root, "v1", entries)
            m1 = generate_projection(
                release_root=release_root, output_root=output_root, consumer="aksantara", track="word", source_release="v1", fixed_clock="2026-09-01T00:00:00Z"
            )
            m2 = generate_projection(
                release_root=release_root, output_root=output_root, consumer="aksantara", track="word", source_release="v1", fixed_clock="2026-09-01T00:00:00Z"
            )
            assert m1["self_hash"] == m2["self_hash"]
            assert m1["output_hash"] == m2["output_hash"]
