"""Validate package — schema, conflicts, quarantine, replay.

Exports both Stream C spec names and Stream D / test-suite expected aliases
for deterministic replay and quarantine.
"""

from aksantara.validate.conflicts import (
    detect_conflict,
    detect_conflicts,
    diff_versions,
    has_substantive_conflict,
)
from aksantara.validate.quarantine import (
    QuarantineRecord,
    QuarantineStore,
    clear_review_queue,
    get_review_queue,
    is_quarantined,
    quarantine,
    quarantine_entry,
    quarantine_from_error,
    queue_size,
)
from aksantara.validate.replay import (
    assert_deterministic,
    replay_raw,
    verify_replay,
)
from aksantara.validate.schema import validate_entry

__all__ = [
    "QuarantineRecord",
    "QuarantineStore",
    "assert_deterministic",
    "clear_review_queue",
    "detect_conflict",
    "detect_conflicts",
    "diff_versions",
    "get_review_queue",
    "has_substantive_conflict",
    "is_quarantined",
    "quarantine",
    "quarantine_entry",
    "quarantine_from_error",
    "queue_size",
    "replay_raw",
    "validate_entry",
    "verify_replay",
]
