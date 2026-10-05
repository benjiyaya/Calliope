"""``calliope-cli`` -- the authoring bridge for opencode.

Contract summary (plan §0.1):

- Read and write every project's content: the novel source text, beats, cast,
  script, clips, continuity ledger, notes, and the audit log.
- Create and modify only. There is no delete verb at any level of the command
  tree, and the web UI is the only place a project can be deleted.
- Never call the model. ``agent.llm`` and the generator agents are not
  importable from here, on purpose: rendering and generation stay in the UI.
- Every write lands in ``cli_audit`` in the same transaction as the data.
- Writes preflight the rows they touch, so a web-UI edit that happened after the
  caller read is caught instead of silently overwritten.

The layering: ``cli/`` has no SQL; ``authoring/`` has all of it and never commits.
"""
from __future__ import annotations

__all__ = ["main"]


def main(argv=None) -> int:
    from calliope.cli.main import main as _main

    return _main(argv)