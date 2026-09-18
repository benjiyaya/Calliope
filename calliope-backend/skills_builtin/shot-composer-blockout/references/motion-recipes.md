# Motion Recipes

Timing and choreography judgment for keyframed blockout motion, adapted from
classic animation-timing practice (anime.js/GSAP timeline choreography and the
12-principles shorthand) translated into `shot_*` tool calls. Times are
seconds; the timeline max is 60.

## Core pattern: stage → commit

Every recipe is the same loop:

1. `set_transform` (and/or `set_joint`) — stage the pose
2. `add_keyframe(object_id, time)` — pin it
3. change only `time` between commits — the interpolation engine does the rest

Spacing between keyframes controls speed; what you stage controls meaning.

## Easing without an easing control

Interpolation between two keyframes is smoothstep (ease-in-out). Use that as
the default motion feel. To fake other easings with plain keyframes:

- **Ease-out (decelerate to rest)** — sample the curve: for a 3s move to
  x=6, commit at t=0 (x=0), t=0.6 (x≈3.4), t=1.5 (x≈5.4), t=3 (x=6). Two
  early keyframes close together, then a long glide.
- **Constant speed (linear)** — 3+ evenly spaced keyframes flatten the
  smoothstep: t=0, 1, 2, 3 at equal position steps.
- **Anticipation (cartoon read)** — before the main move, commit a small pose
  in the OPPOSITE direction: crouch at t=1 (y −0.15), launch at t=1.4,
  land at t=2.6. The beat before the action sells the action.

## Canonical recipes

**Walk-across (A → B):** spawn key at t=0 (auto); `set_transform` B →
`add_keyframe` at t=d. Add a mid key at t=d/2 with a slight y bounce
(±0.05) if the character should feel alive. Legs won't step — this is
blockout framing, not final animation.

**Jump:** crouch (y −0.2, knees bent via `set_joint`) at t=0 → apex
(y +1, legs extended) at t=0.5 → ground (y 0, knees bent landing) at t=1 →
settle (standing posture) at t=1.3. The crouch IS the anticipation.

**Head turn / look:** keep position fixed; `set_joint` head y ±60°, key at
t=0.2, then to the new heading at t=0.8. Small and slow beats big and fast
for reads.

**Door / prop interaction:** character walks to the prop (walk-across),
pause key 0.3s, arm extends (`set_joint` l_arm) + key, prop transform changes
(`update_keyframe` or its own track) on the same beat.

**Two objects crossing:** keyframe object A on its track first (all its
times), then object B. Overlap the mid times so the crossing happens near
the same beat — the eye reads simultaneous motion as intentional.

## Camera vs objects

The export renders **both** object tracks and the camera track — the viewport
samples every tracked object at the playhead, so keyframed object motion
(walk-across, jump, head turn) appears in the exported clip. The camera track
adds camera moves on top (or holds static). Keys for the camera come from the
timeline UI (◉), not from `shot_*` tools — the agent animates objects; the
user frames the camera.

## Timing vocabulary (rule of thumb)

| Read | Key spacing |
|---|---|
| Snap / impact | ≤ 0.2s between keys |
| Normal action | 0.5–1.5s |
| Drift / ambience | 3s+ |
| Hold | duplicate the same pose at a later time (never delete the last key) |

## Failure handling

- Object jumps back to an old position mid-playback: a stale keyframe is
  being sampled — `get_scene`, find it, `update_keyframe` or
  `move_keyframe_time` (don't re-stage over it with `set_transform`).
- `add_keyframe` refused at t>60: the timeline caps at 60s; widen the
  choreography, don't fight the cap.
- Character vanishes after posing: a malformed posture array NaNs
  mannequin-js silently — `reset_pose` restores it; never hand-build
  posture data.

## Video → blockout (attached clip recreation)

An attached video arrives as ~8 evenly-spaced frames, each stamped with its
time in the clip (`frame at 1.25s`). The frames ARE the motion storyboard —
recreate it, don't improvise a new choreography.

**Workflow:**

1. **Read the frames in order** — identify subjects (characters vs moving
   props), roughly where each starts/ends, and the big pose changes between
   consecutive frames.
2. **Build the cast** — `add_object` for each subject, positioned to match
   the FIRST frame. Name them after their role in the clip (e.g.
   "Fighter A").
3. **Key the opening pose** — stage pose/transform to match frame 1, then
   `add_keyframe(subject, 0)`.
4. **Walk the frames** — for each later frame at time t: stage the pose
   change you see (`set_transform` for travel, `set_joint` for limb work),
   then `add_keyframe(subject, t)` using the FRAME's timestamp, not a
   rounded number. Key EVERY subject on the SAME timestamps so their tracks
   stay synchronized with each other and with the video clock.
5. **Finish** — `set_playback(duration=<clip length>)`, then tell the user
   what you captured and what you approximated.

**Fidelity rules:**

- Fast motion (a punch, a spin) between two frames: 2–3 keys on the travel
  path beat one heroic guess — read the timing vocabulary table above.
- Camera moves in the clip are perspective changes, not subject motion —
  block the SUBJECTS; do not move the camera for them (the camera track is
  the user's, keyed from the timeline UI).
- 8 frames can't hold every beat. Prioritize the pose changes that define
  the action (start, contact, recoil, end); say so in your summary.
- If the frames are ambiguous (motion blur, off-screen action), take the
  simple read and note it — never fabricate precise choreography you can't
  see.
