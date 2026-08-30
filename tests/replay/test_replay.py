from datetime import UTC, datetime
from pathlib import Path

from aksantara.domain.models import SourceRef
from aksantara.domain.provenance import content_hash_bytes
from aksantara.parse.parser_contract import parse_kbbi
from aksantara.validate.replay import assert_deterministic, replay_raw
from aksantara.validate.schema import validate_entry


def _load_fixture(name: str) -> tuple[bytes, SourceRef]:
    path = Path(__file__).parent / "fixtures" / f"{name.lower()}.html"
    raw = path.read_bytes()
    source = SourceRef(
        url=f"https://kbbi.kemdikbud.go.id/entri/{name.lower()}",
        source_kind="official-live",
        edition="VI",
        source_version="VI",
        retrieved_at=datetime.now(UTC),
        content_hash=content_hash_bytes(raw),
    )
    return raw, source


def test_februari_replay_deterministic() -> None:
    raw, source = _load_fixture("februari")
    assert_deterministic(raw, source)
    entry = parse_kbbi(raw, source)
    assert entry.lema == "Februari"
    assert any(
        "bulan" in str(s).lower()
        and ("kedua" in str(s).lower() or "ke-2" in str(s).lower())
        for s in entry.makna
    )
    assert "Pebruari" in entry.bentuk_tidak_baku
    validate_entry(entry)
    # second replay with expected
    replay_raw(raw, source, expected=entry)


def test_februari_hash_stable() -> None:
    raw, source = _load_fixture("februari")
    assert len(source.content_hash) == 64
    entry = parse_kbbi(raw, source)
    assert entry.source.content_hash == source.content_hash
    assert entry.parser_version == "0.1.0"
