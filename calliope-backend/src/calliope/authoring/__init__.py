"""Authoring service layer: the ONLY place SQL lives for the CLI bridge.

Design contract (docs/plans/2026-10-05-cli-authoring-bridge.md §3.1):

- Every function takes a ``sqlite3.Connection`` that the caller obtained from
  ``db.get_db()``. They never open their own connection and never commit --
  the transaction is owned by ``cli.context.cli_txn`` so the audit row and the
  data it describes land in the SAME transaction. A log that can disagree with
  the data is worse than no log.
- No LLM. ``ensure_continuity_plan`` and friends are permanently off limits
  here; ``authoring.context`` may only use the pure functions from
  ``agent.continuity`` (``load_board`` / ``basis_hash`` / ``deterministic_plan``).
- No filesystem writes outside ``<data_dir>/audit/`` and
  ``<data_dir>/cli_snapshots/``.
"""

from calliope.authoring.service import (
    AuthoringError,
    OrderingError,
    ValidationFailed,
    bump_revision,
    next_order_index,
    project_or_404,
    resolve_project,
)

__all__ = [
    "AuthoringError",
    "OrderingError",
    "ValidationFailed",
    "bump_revision",
    "next_order_index",
    "project_or_404",
    "resolve_project",
]
