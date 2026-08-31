from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from aksantara.api.routes import create_app
from aksantara.domain.models import SourceRef
from aksantara.domain.provenance import (
    canonical_content_hash,
    canonical_record_bytes,
    content_hash_bytes,
)
from aksantara.parse.parser_contract import PARSER_VERSION, parse_kbbi

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "replay" / "fixtures" / "februari.html"
FIXTURE_HASH = "35a7028aa2ef140e54ea9a783ee0c87e9e79729ed51e914352e14ee099d703c5"


def _source(raw: bytes) -> SourceRef:
    return SourceRef(
        url="https://kbbi.kemdikbud.go.id/entri/februari",
        source_kind="official-snapshot",
        edition="VI",
        source_version="VI",
        retrieved_at=datetime(2026, 8, 31, tzinfo=UTC),
        content_hash=content_hash_bytes(raw),
        parser_version=PARSER_VERSION,
    )


def test_canonical_record_hash_is_for_published_bytes() -> None:
    raw = FIXTURE.read_bytes()
    entry = parse_kbbi(raw, _source(raw))
    published = canonical_record_bytes(entry)

    assert published.endswith(b"\n")
    assert canonical_content_hash(entry) == content_hash_bytes(published)
    assert canonical_content_hash(entry) != content_hash_bytes(raw)


def test_public_replay_cli_is_read_only_and_hash_checked(tmp_path: Path) -> None:
    fixture_copy = tmp_path / "februari.html"
    fixture_copy.write_bytes(FIXTURE.read_bytes())
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "replay.py"),
            "februari",
            "--root",
            str(tmp_path),
            "--raw",
            str(fixture_copy),
            "--retrieved-at",
            "2026-08-31T00:00:00Z",
            "--source-version",
            "VI",
            "--json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["deterministic"] is True
    assert body["read_only"] is True
    assert body["raw"]["raw_content_hash"] == FIXTURE_HASH
    assert body["canonical"]["entry"]["id"] == "februari"
    assert body["canonical"]["entry"]["lema"] == "Februari"
    assert body["canonical"]["entry"]["kelas_kata"] == ["n"]
    assert "bulan ke-2 tahun Masehi" in str(body["canonical"]["entry"]["makna"])
    assert body["canonical"]["entry"]["contoh"] == ["Ia lahir pada bulan Februari."]
    assert body["canonical"]["entry"]["bentuk_tidak_baku"] == ["Pebruari"]

    changed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "replay.py"),
            "februari",
            "--root",
            str(tmp_path),
            "--raw",
            "changed.html",
            "--json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert changed.returncode != 0
    assert json.loads(changed.stdout)["error"]["code"] in {
        "replay_raw_not_found",
        "replay_raw_hash_mismatch",
    }


def test_public_replay_api_is_documented_and_read_only() -> None:
    client = TestClient(create_app())
    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    paths = openapi.json()["paths"]
    assert "/replay" in paths
    assert "post" in paths["/replay"]
    assert paths["/replay"]["post"]["operationId"] == "replay_snapshot"

    raw = FIXTURE.read_bytes()
    source = _source(raw)
    response = client.post(
        "/replay",
        json={
            "root": str(ROOT),
            "raw_path": str(FIXTURE),
            "source_ref": source.model_dump(mode="json"),
            "expected_raw_hash": FIXTURE_HASH,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deterministic"] is True
    assert body["read_only"] is True
    assert body["writes"]["count"] == 0


def test_no_backend_semantic_response_is_exact_fail_closed_shape() -> None:
    client = TestClient(create_app())
    response = client.get("/search/semantic", params={"q": "bulan kedua"})
    assert response.status_code == 200
    assert response.json() == {"results": []}


def test_public_replay_rejects_malformed_source_reference_as_json_error() -> None:
    client = TestClient(create_app())
    raw = FIXTURE.read_bytes()
    source = _source(raw).model_dump(mode="json")
    source["retrieved_at"] = "not-an-iso-timestamp"
    response = client.post(
        "/replay",
        json={
            "root": str(ROOT),
            "raw_path": str(FIXTURE),
            "source_ref": source,
            "expected_raw_hash": FIXTURE_HASH,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "replay_source_ref_invalid"
