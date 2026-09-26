"""Export a recorded agent session as a deterministic replay fixture.

The golden-scenario harness (tests/test_golden_replay.py) replays a session's
event log through run_turn / orchestrate with the LLM mocked to REPLAY the
recorded assistant outputs, then asserts structural invariants (every
assistant tool_calls has matching tool results, turn/end present with a valid
status, no dangling call ids). This script dumps the raw material: the
session's user/assistant/tool events in order.

Usage:
    python scripts/export_session_replay.py --db data/calliope.db --session-id 12 --out tests/fixtures/replay/my_case.json

Only ever run against a COPY of a database or a test DB — never against the
live calliope.db while the app is writing to it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from calliope.db import get_db, migrate_db  # noqa: E402
from calliope.config import settings  # noqa: E402
from calliope.agent.harness import log as session_log  # noqa: E402


def export_session(db_path: Path, session_id: int) -> dict:
    settings.data_dir = db_path.parent
    asyncio.run(migrate_db(db_path))
    events = session_log.read_events(session_id)
    if not events:
        raise SystemExit(f"Session {session_id} has no events in {db_path}")
    out = {"session_id": session_id, "events": []}
    for e in events:
        out["events"].append({"seq": e.seq, "type": e.type, "data": e.data})
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to a calliope SQLite DB (a copy)")
    parser.add_argument("--session-id", type=int, required=True)
    parser.add_argument("--out", required=True, help="Output fixture path (.json)")
    args = parser.parse_args()

    fixture = export_session(Path(args.db), args.session_id)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Exported session {args.session_id} -> {out_path} ({len(fixture['events'])} events)")


if __name__ == "__main__":
    main()
