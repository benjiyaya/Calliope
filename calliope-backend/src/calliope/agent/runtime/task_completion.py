"""Mechanical completion gate.

Port of CoworkOS ``assessTaskCompletion``. It checks outstanding work and
whether the model finished its response. It does not read the database and
it does not treat the word "Done" as evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CompletionReason = Literal[
    "ready",
    "pending_jobs",
    "pending_subagents",
    "pending_callbacks",
    "pending_human",
    "pending_subagent_questions",
    "parked_for_wake",
    "incomplete_response",
]


@dataclass(frozen=True)
class CompletionAssessment:
    ready: bool
    reason: CompletionReason


def assess_task_completion(
    *,
    pending_calls: int = 0,
    pending_jobs: int = 0,
    pending_callbacks: int = 0,
    pending_human_requests: int = 0,
    pending_subagent_questions: int = 0,
    parked_for_wake: bool = False,
    has_final_answer: bool = True,
) -> CompletionAssessment:
    if pending_jobs > 0:
        return CompletionAssessment(False, "pending_jobs")
    if pending_calls > 0:
        return CompletionAssessment(False, "pending_subagents")
    if pending_callbacks > 0:
        return CompletionAssessment(False, "pending_callbacks")
    if pending_human_requests > 0:
        return CompletionAssessment(False, "pending_human")
    if pending_subagent_questions > 0:
        return CompletionAssessment(False, "pending_subagent_questions")
    if parked_for_wake:
        return CompletionAssessment(False, "parked_for_wake")
    if not has_final_answer:
        return CompletionAssessment(False, "incomplete_response")
    return CompletionAssessment(True, "ready")
