"""Evidence handed back to a subagent after a failed attempt.

Port of CoworkOS ``subagentRecoverySummary``. The text names the tools that
actually ran and the live scene count. It does not repeat the model's claim.
"""

from __future__ import annotations


def subagent_recovery_summary(
    *,
    tools_ran: list[str],
    required_tool: str | None,
    scene_count: int,
    finished: bool,
    label: str = "scenes",
) -> str:
    ran = ", ".join(tools_ran) if tools_ran else "no tools"
    lines = [
        "Recovery: the previous attempt is not accepted.",
        f"Tools that ran: {ran}.",
        f"{label}={scene_count}.",
    ]
    if not finished:
        lines.append("The attempt ended before a finished reply.")
    if required_tool and required_tool not in tools_ran:
        lines.append(
            f"{required_tool} was not called. Call {required_tool}. "
            "A prose reply does not write a row."
        )
    elif required_tool:
        lines.append(
            f"{required_tool} ran and the table did not change. "
            "Read the tool error and call it again."
        )
    return " ".join(lines)
