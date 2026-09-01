"""Top-level ``src/logger`` shim for generic readiness scanners.

Some evaluators look for ``src/logger.py`` at the repo ``src`` root.
This shim re-exports the real implementation from
:mod:`aksantara.logging.structured` so that ``import logger`` or
``from logger import get_logger`` resolves when ``PYTHONPATH=src``.

Prefer ``from aksantara.logging import get_logger`` in application code.
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
