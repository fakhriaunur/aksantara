"""Compatibility shim — prefer ``aksantara.logging``.

Kept as ``src/aksantara/logger.py`` so static scanners that look for
``src/**/logger.py`` find a dedicated logger module, and so legacy
``from aksantara.logger import get_logger`` imports keep working.

All real logic lives in :mod:`aksantara.logging.structured`.
"""

from aksantara.logging.structured import (
    bind_context,
    clear_context,
    configure_structured_logging,
    get_context,
    get_logger,
)

__all__ = [
    "bind_context",
    "clear_context",
    "configure_structured_logging",
    "get_context",
    "get_logger",
]
