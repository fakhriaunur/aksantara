from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aksantara.domain.models import KBBIEntry, SourceRef


def _source(hash_val="a" * 64) -> SourceRef:
    return SourceRef(
        url="https://kbbi.kemdikbud.go.id/entri/februari",
        source_kind="official-live",
        edition="VI",
        source_version="VI",
        retrieved_at=datetime.now(UTC),
        content_hash=hash_val,
    )


def test_source_ref_requires_64_hex() -> None:
    with pytest.raises(ValidationError):
        SourceRef(
            url="https://example.com",
            source_kind="official-live",
            edition="VI",
            source_version="VI",
            retrieved_at=datetime.now(UTC),
            content_hash="bad",
        )


def test_kbbi_entry_requires_makna() -> None:
    with pytest.raises(ValidationError):
        KBBIEntry(
            id="februari",
            lema="Februari",
            makna=[],
            source=_source(),
        )


def test_kbbi_entry_valid() -> None:
    e = KBBIEntry(
        id="februari",
        lema="Februari",
        makna=[{"definisi": "bulan ke-2"}],
        bentuk_tidak_baku=["Pebruari"],
        source=_source(),
    )
    assert e.lema == "Februari"
    assert e.bentuk_tidak_baku == ["Pebruari"]
