---
name: character-consistency
description: "Use when writing or improving character image prompts — sheets, portraits, and reference images that stay consistent across scenes."
version: 1.0.0
license: MIT
metadata:
  author: Calliope
  tags: [characters, image, prompts, consistency]
---

# Character Consistency

## Overview

Pattern guide for the character **Image prompt** field (Assets) — the prompt
that generates the reference sheet all scene videos will reuse. Consistency is
decided here, not at video time.

## When to Use

- Creating a character whose look must hold across many scenes
- A generated sheet drifted (hair/clothing changes between generations)
- The user describes a character loosely and needs the prompt firmed up

## Prompt Patterns

1. **Anchor identity with stable traits first**: age range, build, hair color
   and style, eye color, skin tone. These go early — image models weight
   leading tokens heavily.
2. **One signature detail** that disambiguates this character from every other
   in the project ("a faded scar across the left eyebrow", "always wears a
   broken wristwatch"). Reuse the exact wording in every scene prompt.
3. **Clothing described as a set, not a list** — "a charcoal utility jacket
   over a bone-white shirt, sleeves pushed to the forearm" beats five
   comma-separated adjectives.
4. **Sheet intent in the prompt**: for the reference image say "character
   reference sheet, front and side view, neutral pose, plain background" —
   scenes want the identity, not the dramatic lighting.
5. **Negative prompt discipline**: put *contradictions* there (e.g. "multiple
   people, text, watermark"), not style preferences you haven't stated
   positively.

## When Drift Happens

- Compare the drifted artifact with the sheet: if the *sheet* is ambiguous
  ("long hair" → curly vs straight), fix the sheet prompt and regenerate the
  reference — scene prompts inherit the fix.
- If only one scene drifted, edit that scene's prompt instead (usually a
  paraphrased identity phrase).
