from aksantara.domain.provenance import (
    canonical_json_hash,
    content_hash,
    content_hash_bytes,
    verify_content_hash,
)


def test_content_hash_deterministic() -> None:
    assert content_hash_bytes(b"hello") == content_hash("hello")
    assert (
        content_hash("hello")
        == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_canonical_json_hash_sorted() -> None:
    h1 = canonical_json_hash({"b": 1, "a": 2})
    h2 = canonical_json_hash({"a": 2, "b": 1})
    assert h1 == h2


def test_verify_case_insensitive() -> None:
    data = b"test"
    h = content_hash_bytes(data)
    assert verify_content_hash(data, h.upper())
    assert not verify_content_hash(data, "0" * 64)
