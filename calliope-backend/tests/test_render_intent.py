"""Regression tests for render-intent detection (policy.is_render_request).

The failure that motivated this: "all 20 scenes use this text2video workflow.
Do not overthinking, no image need." read as NON-render (compound word
"text2video" unmatched + a message-global negation veto from "Do not"/"no"),
so the registry hid enqueue_video_jobs/run_workflow, and the agent — unable
to say why — fabricated job ids instead of admitting the tools were hidden.
"""
from __future__ import annotations

import pytest

from calliope.agent.harness.policy import is_render_request


@pytest.mark.parametrize(
    ("text", "want"),
    [
        # The exact messages from the canvas/62 incident
        ("all 20 scenes use this text2video workflow. Do not overthinking, no image need.", True),
        ("you are not doing the job go away", False),
        # Compound / model-name cues
        ("make a text2video clip of the establishing shot", True),
        ("txt2vid for scene 3 please", True),
        ("use fastvideoH3_t2v-API for all 20 scenes", True),
        ("image-to-video on scene 2", True),
        # Negation must stay authoritative when it governs the render cue
        ("do not generate videos", False),
        ("no images please", False),
        ("don't render anything", False),
        ("no render, just fix the script", False),
        ("i said do not render", False),
        # …but a negated clause must not veto an unrelated render clause
        ("fix the script but render afterwards", True),
        ("no image need, render all scenes", True),
        # Plain positive cues
        ("render all scenes now", True),
        ("yes, generate the images", True),
        # Non-render text must not flip positive
        ("generate the story", False),
        ("provide the lyrics", False),
    ],
)
def test_render_intent(text: str, want: bool):
    assert is_render_request(text) is want, text
