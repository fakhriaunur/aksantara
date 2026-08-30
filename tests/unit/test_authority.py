import pytest

from aksantara.domain.authority import DEFAULT_VALIDATION_POLICY, AuthorityLayer
from aksantara.domain.errors import AuthorityViolationError


def test_canonical_writer_only_official() -> None:
    policy = DEFAULT_VALIDATION_POLICY
    policy.assert_canonical_writer(AuthorityLayer.KBBI_OFFICIAL_LIVE)
    policy.assert_canonical_writer(AuthorityLayer.KBBI_OFFICIAL_SNAPSHOT)
    with pytest.raises(AuthorityViolationError):
        policy.assert_canonical_writer(AuthorityLayer.ENRICHMENT)
    with pytest.raises(AuthorityViolationError):
        policy.assert_canonical_writer(AuthorityLayer.AI_PROPOSAL)


def test_fallback_is_not_canonical() -> None:
    assert AuthorityLayer.MIRROR_FALLBACK.is_fallback
    assert not AuthorityLayer.MIRROR_FALLBACK.is_canonical_writer
